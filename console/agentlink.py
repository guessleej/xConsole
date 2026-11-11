"""代理主動連回（工作站模式）：主控台這一側的指令佇列與結果配對。

工作站的防火牆擋連入、IP 會換，主控台連不進去；改由代理用 HTTPS 長輪詢連回來拿指令。
上層程式不動：call_node()、node_request() 依節點的 link 決定走法，push 的請求在 agent_open()
交給 LINK.open()，回來的是一個長得像 urllib 回應的物件（status、headers、read、逐行讀），
所以換模型、上傳分塊、對話串流這些呼叫端一行都不用改。

協定（代理那一側在 agent/xconsole_agent.py 的 AgentLink）：
- POST /api/agent/poll：代理帶節點權杖與 seen（它最近收過的指令編號）；主控台最多掛 POLL_HOLD 秒，
  有指令就立刻回一批。上一批送出去、但 seen 裡沒有的，表示代理沒收到（連線半路斷了），再送一次；
  代理端依編號去重，重送不會重做。
- POST /api/agent/result：代理送回結果的一個片段（frame）。第一片帶 status 與 ctype；串流會分很多片，
  最後一片 more=false。結果依 id 叫醒對應的那個呼叫；對不上的（逾時之後才到）只記一筆紀錄。
- 取消是一筆獨立的指令 {"id": 新編號, "cancel": 要取消的編號}：呼叫端提早關掉回應（例如瀏覽器按停止）
  或等到逾時，主控台就送這一筆，代理把那條本機連線關掉。
- 離線不排舊指令：那台超過 ONLINE_GRACE 秒沒來輪詢，呼叫立刻失敗，不留在佇列裡等它開機。
"""
import base64
import io
import queue
import secrets
import threading
import time

POLL_HOLD = 25           # 長輪詢最多掛這麼久
ONLINE_GRACE = 40        # 超過這麼久沒來輪詢就當離線
BATCH_MAX = 48 * 1024 * 1024     # 一次輪詢最多帶這麼多指令內容（上傳分塊一塊 64 MB 時單獨一批）
SEEN_MAX = 512


class Offline(ConnectionError):
    pass


class Deadline(TimeoutError):
    """過了期限那台還沒回：call_node() 回 504。"""


class _Pending:
    __slots__ = ("id", "node", "cmd", "size", "frames", "delivered", "acked", "done", "seq", "closed")

    def __init__(self, node, cmd, size):
        self.id, self.node, self.cmd, self.size = cmd["id"], node, cmd, size
        self.frames = queue.Queue()
        self.delivered = self.acked = self.done = self.closed = False
        self.seq = -1


class _Raw(io.RawIOBase):
    """把一片一片送回來的結果接成一條位元組流。每次等下一片最多等 timeout 秒。"""

    def __init__(self, link, p, timeout, first):
        super().__init__()
        self.link, self.p, self.timeout, self.buf = link, p, timeout, first

    def readable(self):
        return True

    def readinto(self, b):
        while not self.buf:
            if self.p.done and self.p.frames.empty():
                return 0
            try:
                fr = self.p.frames.get(timeout=self.timeout)
            except queue.Empty:
                self.link.cancel(self.p)
                raise Deadline("代理回應太慢") from None
            if fr is None:                 # 結束記號
                return 0
            self.buf = fr
        n = min(len(b), len(self.buf))
        b[:n] = self.buf[:n]
        self.buf = self.buf[n:]
        return n

    def close(self):
        if not self.closed:
            self.link.cancel(self.p)       # 還沒讀完就關（瀏覽器按停止）：叫代理把那條連線也關掉
        super().close()


class Headers(dict):
    """只用到 get("Content-Type")：大小寫不分。"""

    def get(self, k, default=None):
        return super().get(k.lower(), default)

    def get_content_type(self):
        return (self.get("content-type") or "application/json").split(";")[0].strip()


class Response(io.BufferedReader):
    status = 200
    reason = "OK"
    headers = None

    def getcode(self):
        return self.status

    def info(self):
        return self.headers


class Link:
    def __init__(self, log=print):
        self.log = log
        self._lock = threading.Lock()
        self._nodes = {}                   # 節點 → {"q": [待送或已送未確認], "cond", "last", "polling"}
        self._pending = {}                 # 指令編號 → _Pending

    def _node(self, nid):
        n = self._nodes.get(nid)
        if n is None:
            n = self._nodes[nid] = {"q": [], "cond": threading.Condition(self._lock), "last": 0.0, "polling": 0}
        return n

    def online(self, nid):
        with self._lock:
            n = self._nodes.get(nid)
            return bool(n) and (n["polling"] > 0 or time.time() - n["last"] < ONLINE_GRACE)

    def last_seen(self, nid):
        with self._lock:
            n = self._nodes.get(nid)
            return None if not n or not n["last"] else n["last"]

    # ---- 呼叫端（call_node／agent_open）
    def open(self, nid, method, path, data=None, ctype="application/json", timeout=40):
        """送一筆指令，等第一片結果（狀態碼）回來，回傳 Response。過期限 → Deadline，離線 → Offline。"""
        if not self.online(nid):
            raise Offline("這台目前沒有連回主控台（關機、網路斷了，或代理沒在跑）")
        cmd = {"id": secrets.token_hex(12), "method": method, "path": path, "ctype": ctype,
               "body": base64.b64encode(data).decode() if data else "", "timeout": int(timeout)}
        p = _Pending(nid, cmd, len(cmd["body"]))
        with self._lock:
            self._pending[p.id] = p
            n = self._node(nid)
            n["q"].append(p)
            n["cond"].notify_all()
        try:
            first = p.frames.get(timeout=timeout)
        except queue.Empty:
            self.cancel(p)
            raise Deadline(f"等了 {int(timeout)} 秒那台還沒回") from None
        if first is None:
            raise Offline("這台的代理沒有回結果")
        status, hdrs, chunk = first
        r = Response(_Raw(self, p, timeout, chunk))
        r.status, r.headers = status, Headers({"content-type": hdrs})
        r.reason = "OK" if status < 400 else "Error"
        return r

    def cancel(self, p):
        with self._lock:
            if p.closed:
                return
            p.closed = True
            n = self._node(p.node)
            self._pending.pop(p.id, None)
            if p in n["q"]:
                n["q"].remove(p)
            if p.delivered and not p.done:
                # 已經在那台跑了：另送一筆取消；不需要等它的結果
                c = _Pending(p.node, {"id": secrets.token_hex(12), "cancel": p.id}, 64)
                c.done = True
                n["q"].append(c)
                n["cond"].notify_all()

    def forget(self, nid):
        """機器被移除：還在等的呼叫全部失敗。"""
        with self._lock:
            n = self._nodes.pop(nid, None)
            ps = [p for p in self._pending.values() if p.node == nid]
            for p in ps:
                self._pending.pop(p.id, None)
            if n:
                n["cond"].notify_all()
        for p in ps:
            p.frames.put(None)

    # ---- 代理端（/api/agent/poll、/api/agent/result）
    def wake(self):
        """叫醒所有掛著的輪詢，讓它們重新看 stop()（換了通道憑證或通道設定時用）。"""
        with self._lock:
            for n in self._nodes.values():
                n["cond"].notify_all()

    def poll(self, nid, seen, hold=POLL_HOLD, stop=None):
        """回一批要給這台的指令。seen：代理說它最近收過的編號。stop()：成立就提早回（不必等滿 hold 秒）。"""
        seen = set(list(seen)[:SEEN_MAX]) if isinstance(seen, (list, tuple)) else set()
        end = time.time() + hold
        with self._lock:
            n = self._node(nid)
            n["polling"] += 1
            n["last"] = time.time()
            try:
                for p in n["q"]:
                    if p.delivered and p.id in seen:
                        p.acked = True
                while True:
                    # 還沒送的，加上送了但代理說沒收到的（上一批的回應半路斷了）
                    out, size = [], 0
                    for p in n["q"]:
                        if p.acked or (p.delivered and p.id in seen):
                            continue
                        if out and size + p.size > BATCH_MAX:
                            break
                        out.append(p)
                        size += p.size
                    left = end - time.time()
                    if out or left <= 0 or nid not in self._nodes or (stop is not None and stop()):
                        break
                    n["cond"].wait(left)
                for p in out:
                    p.delivered = True
                    if p.done:                 # 取消指令：送出去就好
                        n["q"].remove(p)
                return [p.cmd for p in out]
            finally:
                n["polling"] -= 1
                n["last"] = time.time()

    def result(self, nid, fr):
        """代理送回的一片結果。回 True 表示有人在等；False 表示對不上（逾時後才到、或編號不對）。"""
        cid = str(fr.get("id") or "")
        try:
            seq = int(fr.get("seq") or 0)
            chunk = base64.b64decode(fr.get("data") or "", validate=True)
        except (TypeError, ValueError):
            return False
        more = bool(fr.get("more"))
        with self._lock:
            p = self._pending.get(cid)
            if p is None or p.node != nid:
                self.log(f"代理 {nid} 送回的結果對不上（{cid[:8]}…，可能已逾時），已丟棄")
                return False
            if seq <= p.seq:
                return True                    # 重送的片段
            if seq != p.seq + 1:               # 中間掉了一片：這筆結果不完整，當成失敗
                self.log(f"代理 {nid} 的結果缺片段（{cid[:8]}…），已中止")
                seq, more, chunk = -1, False, b""
                p.frames.put(None)
            p.seq = seq
            p.acked = True
            if not more:
                p.done = True
                self._pending.pop(cid, None)
                n = self._node(nid)
                if p in n["q"]:
                    n["q"].remove(p)
        if seq < 0:
            return False
        if seq == 0:
            try:
                status = int(fr.get("status") or 502)
            except (TypeError, ValueError):
                status = 502
            p.frames.put((status, str(fr.get("ctype") or "application/json")[:120], chunk))
        elif chunk:
            p.frames.put(chunk)
        if not more:
            p.frames.put(None)
        return True
