#!/usr/bin/env python3
"""代理主動連回（工作站模式）代理這一側的自我檢查：讀連回設定、釘住主控台憑證、
指令轉給本機代理後分片送回（一般、大檔、串流）、同一個編號不重做、取消會把本機連線關掉。
跑法：python3 test_link.py（只用標準函式庫＋代理本身，Linux／macOS／Windows 都能跑）
"""
import base64
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-link-")
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


BIG = os.urandom(9 * 1024 * 1024)


class H(A.Handler):
    """測試用：在代理原本的處理程式上加幾支假的路由。"""

    def do_GET(self):  # noqa: N802
        p = self.path
        if p.startswith("/t/json"):
            return self._send(200, {"ok": True, "auth": self.headers.get("Authorization") == f"Bearer {A.TOKEN}",
                                    "path": p})
        if p == "/t/big":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(BIG)))
            self.end_headers()
            self.wfile.write(BIG)
            return None
        if p.startswith("/t/stream"):
            n, gap = (10, 0.01) if p == "/t/stream" else (200, 0.1)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for i in range(n):
                    self.wfile.write(f"data: {i}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(gap)
            except OSError:
                H.broke = True
            return None
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path == "/t/echo":
            n = int(self.headers.get("Content-Length") or 0)
            return self._send(201, {"got": self.rfile.read(n).decode(), "ctype": self.headers.get("Content-Type")})
        return super().do_POST()


def main():
    A.ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(A.CERT_FILE, A.KEY_FILE)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    A.PORT = s.getsockname()[1]
    s.close()
    srv = A.AgentTLSServer(("127.0.0.1", A.PORT), H)
    srv.ctx = ctx
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ---- 1. 連回設定：沒有檔就不連；網址、節點編號不對就不連
    case("沒有 agent-link.json：不連", A.AgentLink.config() is None)
    with open(A.CERT_FILE) as fh:
        pem = fh.read()
    with open(A.LINK_CERT, "w") as fh:
        fh.write(pem)
    for bad in ({"url": "http://1.2.3.4:9400", "node": "a"}, {"url": "https://1.2.3.4:9400", "node": "../x"},
                {"url": "https://1.2.3.4:9400"}):
        with open(A.LINK_FILE, "w") as fh:
            json.dump(bad, fh)
        case(f"設定不合法不連：{bad}", A.AgentLink.config() is None)
    with open(A.LINK_FILE, "w") as fh:
        json.dump({"url": f"https://127.0.0.1:{A.PORT}", "node": "ws-01"}, fh)
    cfg = A.AgentLink.config()
    case("讀到連回設定", cfg and cfg["node"] == "ws-01" and cfg["port"] == A.PORT and len(cfg["fp"]) == 64, cfg)

    # ---- 2. 釘住主控台憑證：同一張連得上，指紋不同就拒絕（不是關掉驗證）
    try:
        A.AgentLink.connect(cfg, 5).close()
        case("釘住的憑證：連得上", True)
    except Exception as exc:  # noqa: BLE001
        case("釘住的憑證：連得上", False, exc)
    try:
        A.AgentLink.connect({**cfg, "fp": "0" * 64}, 5)
        case("指紋不同：拒絕連線", False)
    except ssl.SSLError as exc:
        case("指紋不同：拒絕連線", "釘住" in str(exc), exc)

    # ---- 3. 執行指令：送回的片段
    link = A.AgentLink()
    sent = []
    link.send = lambda c, fr: sent.append(fr) or True

    def run(cmd):
        sent.clear()
        link.execute(cfg, {"timeout": 20, **cmd})
        return list(sent)

    def data(frs):
        return b"".join(base64.b64decode(f["data"]) for f in frs)

    fr = run({"id": "a" * 24, "method": "GET", "path": "/t/json?x=1"})
    body = json.loads(data(fr))
    case("一般指令：一片、帶狀態碼、最後一片", len(fr) == 1 and fr[0]["status"] == 200 and fr[0]["more"] is False, fr)
    case("  轉給本機代理時帶自己的權杖、查詢字串照帶", body["auth"] and body["path"] == "/t/json?x=1", body)
    fr = run({"id": "b" * 24, "method": "POST", "path": "/t/echo", "ctype": "text/plain",
              "body": base64.b64encode("中文本文".encode()).decode()})
    case("POST：本文與型別照轉、狀態碼照回", fr[0]["status"] == 201 and json.loads(data(fr)) == {"got": "中文本文", "ctype": "text/plain"}, fr)
    fr = run({"id": "c" * 24, "method": "GET", "path": "/t/big"})
    case("大的回應分片送（9 MB → 3 片，編號連續，只有最後一片 more=false）",
         [f["seq"] for f in fr] == [0, 1, 2] and [f["more"] for f in fr] == [True, True, False] and data(fr) == BIG,
         [(f["seq"], f["more"]) for f in fr])
    fr = run({"id": "d" * 24, "method": "GET", "path": "/t/stream"})
    case("串流：第一片帶 event-stream，內容完整，最後一片 more=false",
         "event-stream" in fr[0]["ctype"] and data(fr) == b"".join(f"data: {i}\n\n".encode() for i in range(10))
         and fr[-1]["more"] is False and all(f["more"] for f in fr[:-1]), len(fr))
    fr = run({"id": "e" * 24, "method": "GET", "path": "/t/x y"})
    case("路徑不合法：回 400，不送出去", fr[0]["status"] == 400, fr)
    fr = run({"id": "f" * 24, "method": "GET", "path": "/state", "timeout": 20})
    case("轉到代理原本的路由（/state）", fr[0]["status"] == 200 and "pools" in json.loads(data(fr)), fr[0]["status"])

    # ---- 4. 同一個編號不重做；做完再送來就重送上次的結果
    calls = []
    real = link.execute
    link.execute = lambda c, cmd: (calls.append(cmd["id"]), real(c, cmd))
    sent.clear()
    cid = "1" * 24
    link.dispatch(cfg, {"id": cid, "method": "GET", "path": "/t/json"})
    link.dispatch(cfg, {"id": cid, "method": "GET", "path": "/t/json"})
    time.sleep(1)
    case("同一個編號送兩次：只執行一次", calls == [cid], calls)
    n = len(sent)
    link.dispatch(cfg, {"id": cid, "method": "GET", "path": "/t/json"})
    time.sleep(0.5)
    case("  做完後又送來：重送上次的結果，不重做", calls == [cid] and len(sent) == n + 1 and sent[-1]["id"] == cid, (calls, len(sent)))
    case("  seen 帶著這個編號", cid in link.seen())
    link.dispatch(cfg, {"id": "zz", "path": "/t/json"})
    case("編號格式不對：不理", calls == [cid])

    # ---- 5. 取消：本機那條連線被關掉，執行緒很快結束並送最後一片
    H.broke = False
    sent.clear()
    cid = "2" * 24
    t0 = time.time()
    link.dispatch(cfg, {"id": cid, "method": "GET", "path": "/t/stream-long", "timeout": 60})
    time.sleep(0.8)
    link.dispatch(cfg, {"id": "3" * 24, "cancel": cid})
    for _ in range(50):
        if sent and sent[-1]["more"] is False:
            break
        time.sleep(0.1)
    case("取消：很快結束並送出最後一片", sent and sent[-1]["more"] is False and time.time() - t0 < 6, time.time() - t0)
    time.sleep(0.5)
    case("  本機的串流那一端也斷了（模型服務會跟著停）", H.broke)
    case("  取消指令本身也記進 seen", "3" * 24 in link.seen())

    # ---- 6. 結果送不回主控台：不卡住、照樣收尾
    link.send = lambda c, fr: False
    link.execute = real
    t0 = time.time()
    link.execute(cfg, {"id": "4" * 24, "method": "GET", "path": "/t/json", "timeout": 10})
    case("結果送不回去：執行緒照樣結束", time.time() - t0 < 5 and "4" * 24 not in link.inflight)

    print("全部通過" if OK else "有項目失敗")
    srv.shutdown()
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
