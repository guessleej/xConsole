"""操作電腦：讓看得懂圖的模型看某一台的螢幕、一步一步控制滑鼠鍵盤，完成使用者交代的事。

迴圈：截圖 → 問模型下一步（只回一個 JSON 動作）→ 等人按允許（或這次都允許）→ 經代理執行 → 再截圖。
預設每一步都要人按允許；最多 30 步、15 分鐘；隨時可以停。模型回不出合格的動作連續 3 次就停。
截圖只放在記憶體（最新一張給畫面看），不落地；每個動作寫稽核（打字只記字數，不記內容）。
"""
import base64
import json
import re
import secrets
import threading
import time

MAX_STEPS = 30
MAX_SEC = 900
APPROVE_WAIT = 600
TASKS = {}
LOCK = threading.Lock()

SYSTEM = (
    "你在操作一台電腦的桌面，替使用者完成一件事。每一輪會給你目前的螢幕截圖（寬 {w}、高 {h} 像素，左上角是 (0,0)）。"
    "一次只做一個動作，只輸出一個 JSON 物件，不要任何其他文字。格式：\n"
    '{{"thought": "看到什麼、打算做什麼（一句話）", "action": "click|double_click|right_click|move|drag|type|key|scroll|wait|done", '
    '"x": 數字, "y": 數字, "x2": 數字, "y2": 數字, "text": "要打的字", "keys": "ctrl+c 這種按鍵組合", "dy": 捲動格數（正數往下）, '
    '"result": "完成或做不到時，給使用者的說明"}}\n'
    "規則：座標用截圖上的像素；要打字前先點輸入框；系統是 {os}（{mod} 是主要的組合鍵）。"
    "螢幕上如果看到 xConsole 主控台的網頁，那是控制你的介面，不要點它、不要關掉它。"
    "事情完成、或發現做不到，就用 done 並在 result 說明。不要做付款、轉帳、刪除檔案、輸入或讀取密碼、關機；遇到這些就用 done 說明需要使用者自己來。"
)


class ComputerTask:
    def __init__(self, user, ip, node, model, goal, label):
        self.id = secrets.token_hex(6)
        self.user, self.ip, self.node, self.model, self.goal, self.label = user, ip, node, model, goal, label
        self.state, self.error, self.result = "running", None, None
        self.log, self.steps = [], 0
        self.shot, self.shot_id, self.size = None, 0, None
        self.pending, self.decision, self.always, self.stop = None, None, False, False
        self.created = self.updated = time.time()
        self.deadline = self.created + MAX_SEC
        self.event = threading.Event()
        self.lock = threading.Lock()

    def add(self, kind, **kw):
        with self.lock:
            self.log.append({"type": kind, "ts": time.time(), **kw})
            del self.log[:-200]
            self.updated = time.time()

    def public(self, shot_after=None):
        with self.lock:
            out = {"id": self.id, "node": self.node, "model": self.label, "goal": self.goal, "state": self.state,
                   "error": self.error, "result": self.result, "steps": self.steps, "max_steps": MAX_STEPS,
                   "always": self.always, "pending": self.pending, "log": list(self.log), "created": self.created,
                   "updated": self.updated, "deadline": self.deadline, "shot_id": self.shot_id, "size": self.size}
            # 截圖很大：畫面帶上一次拿到的編號來問，沒變就不重送
            if self.shot and (shot_after is None or self.shot_id > shot_after):
                out["shot"] = self.shot
            return out


def parse_action(text):
    """從模型的回答撈出第一個 JSON 物件（有的模型會包在 ```json 裡、或前後多講話）。"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    raw = m.group(0)
    for cand in (raw, raw[:raw.rfind("}") + 1]):
        try:
            obj = json.loads(cand)
            return obj if isinstance(obj, dict) else None
        except ValueError:
            continue
    # 尾巴多了其他大括號：一層一層往前縮
    depth = 0
    for i, ch in enumerate(raw):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0:
            try:
                obj = json.loads(raw[:i + 1])
                return obj if isinstance(obj, dict) else None
            except ValueError:
                return None
    return None


def describe(a):
    k = a.get("action")
    if k in ("click", "double_click", "right_click", "move"):
        return f"{ {'click': '點一下', 'double_click': '點兩下', 'right_click': '按右鍵', 'move': '移動滑鼠到'}[k] }（{a.get('x')}, {a.get('y')}）"
    if k == "drag":
        return f"從（{a.get('x')}, {a.get('y')}）拖到（{a.get('x2')}, {a.get('y2')}）"
    if k == "type":
        return f"打字：{a.get('text', '')}"
    if k == "key":
        return f"按鍵：{a.get('keys', '')}"
    if k == "scroll":
        return f"捲動 {a.get('dy')} 格" + (f"（在 {a.get('x')}, {a.get('y')}）" if a.get("x") is not None else "")
    if k == "wait":
        return "等一下"
    return k or "（沒有動作）"


def _wait(t, action):
    with t.lock:
        t.pending = {"id": secrets.token_hex(4), "action": action, "text": describe(action), "asked_at": time.time()}
        t.state, t.decision = "waiting_approval", None
        t.event.clear()
        t.updated = time.time()
    end = time.time() + APPROVE_WAIT
    while not t.event.wait(1):
        if t.stop or time.time() > min(t.deadline, end):
            break
    with t.lock:
        d, t.pending = t.decision, None
        if t.state == "waiting_approval":
            t.state = "running"
    if not d:
        return False
    if d.get("always"):
        t.always = True
    return bool(d.get("allow"))


def run(t, call_node, model_call, audit):
    """在背景執行緒跑。call_node(node, path, method, body, timeout)；model_call(t, req) → (碼, 回應)；audit(動作, 目標, 成敗, 細節)。"""
    os_name, fails, hist = None, 0, []
    try:
        code, st = call_node(t.node, "/desktop/status", "GET", None, 20)
        if code != 200:
            raise RuntimeError(st.get("error") or f"代理回 {code}")
        if not st.get("enabled"):
            raise RuntimeError("這台沒有開「讓模型操作這台的桌面」，請管理者到這台的設定打開")
        if not st.get("available"):
            raise RuntimeError(st.get("why") or "這台現在不能操作桌面")
        os_name = st.get("os")
        mod = "cmd" if os_name == "darwin" else "ctrl"
        while not t.stop:
            if t.steps >= MAX_STEPS:
                raise RuntimeError(f"已經做了 {MAX_STEPS} 步還沒完成，先停下來；可以把事情拆小一點再交代")
            if time.time() > t.deadline:
                raise RuntimeError("超過 15 分鐘還沒完成，先停下來")
            code, shot = call_node(t.node, "/desktop/screenshot", "GET", None, 60)
            if code != 200:
                raise RuntimeError(shot.get("error") or f"截不到螢幕（代理回 {code}）")
            w, h = shot["width"], shot["height"]
            with t.lock:
                t.shot, t.shot_id, t.size = shot["png"], t.shot_id + 1, {"width": w, "height": h}
                t.updated = time.time()
            recent = "\n".join(f"{i + 1}. {x}" for i, x in enumerate(hist[-8:])) or "（還沒有做任何動作）"
            req = {"stream": False, "temperature": 0.1, "max_tokens": 600, "messages": [
                {"role": "system", "content": SYSTEM.format(w=w, h=h, os={"windows": "Windows", "darwin": "macOS"}.get(os_name, "Linux"), mod=mod)},
                {"role": "user", "content": [
                    {"type": "text", "text": f"要完成的事：{t.goal}\n\n已經做過的動作：\n{recent}\n\n這是現在的螢幕，下一步要做什麼？只回 JSON。"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + shot["png"]}}]}]}
            code, out = model_call(t, req)
            if code != 200:
                raise RuntimeError(f"模型回錯：{out.get('error') or code}")
            msg = ((out.get("choices") or [{}])[0]).get("message") or {}
            act = parse_action(msg.get("content") or "") or parse_action(msg.get("reasoning_content") or "")
            if not act or not act.get("action"):
                fails += 1
                t.add("note", text="模型這一輪沒有給出看得懂的動作，再問一次")
                if fails >= 3:
                    raise RuntimeError("模型連續三次給不出看得懂的動作；這顆模型可能不適合操作電腦，請換一顆看圖能力強一點的")
                continue
            fails = 0
            if act["action"] == "done":
                with t.lock:
                    t.result = str(act.get("result") or act.get("thought") or "完成")[:4000]
                    t.state = "done"
                t.add("done", text=t.result)
                audit("computer.done", t.node, True, {"steps": t.steps})
                return
            act = {k: act[k] for k in ("action", "x", "y", "x2", "y2", "text", "keys", "dy", "seconds") if k in act}
            if act["action"] == "type" and len(str(act.get("text") or "")) > 500:
                act["text"] = str(act["text"])[:500]
            thought = str(act_thought(msg) or "")[:300]
            t.add("plan", text=describe(act), thought=thought)
            if not t.always and act["action"] != "wait":
                if not _wait(t, act):
                    if t.stop:
                        break
                    t.add("denied", text=describe(act))
                    hist.append(f"{describe(act)}（使用者拒絕了，換個做法）")
                    continue
            code, res = call_node(t.node, "/desktop/act", "POST", {**act, "width": w, "height": h,
                                                                    "screen_w": shot["screen_w"], "screen_h": shot["screen_h"]}, 60)
            ok = code == 200
            audit("computer.act", t.node, ok, {"action": act["action"], "chars": len(str(act.get("text") or "")) or None,
                                                "message": None if ok else res.get("error")})
            t.steps += 1
            t.add("act" if ok else "error", text=describe(act) if ok else f"{describe(act)}：{res.get('error')}")
            hist.append(describe(act) + ("" if ok else f"（失敗：{res.get('error')}）"))
            time.sleep(0.8)                       # 等畫面反應再截下一張
        with t.lock:
            if t.state not in ("done", "failed"):
                t.state = "stopped"
        t.add("stopped", text="已停止")
    except RuntimeError as ex:
        with t.lock:
            t.state, t.error = "failed", str(ex)
        t.add("error", text=str(ex))
        audit("computer.failed", t.node, False, {"message": str(ex)[:300]})
    finally:
        with t.lock:
            t.pending = None
            t.updated = time.time()


def act_thought(msg):
    obj = parse_action(msg.get("content") or "") or {}
    return obj.get("thought")


def start(user, ip, node, model, goal, label, call_node, model_call, audit):
    goal = str(goal or "").strip()
    if not 2 <= len(goal) <= 2000:
        return 400, {"error": "請說清楚要電腦做什麼（2～2000 字）"}
    with LOCK:
        if any(x.state in ("running", "waiting_approval") for x in TASKS.values()):
            return 409, {"error": "已經有一個操作電腦的任務在進行，等它做完或按停止"}
        t = ComputerTask(user, ip, node, model, goal, label)
        TASKS[t.id] = t
        for k in [k for k, v in TASKS.items() if v.state not in ("running", "waiting_approval") and time.time() - v.updated > 3600]:
            TASKS.pop(k, None)
    threading.Thread(target=run, args=(t, call_node, model_call, audit), daemon=True).start()
    audit("computer.start", node, True, {"task": t.id, "model": label, "goal_chars": len(goal)})
    return 200, {"id": t.id}


def decide(t, body):
    with t.lock:
        if t.state != "waiting_approval" or not t.pending:
            return 409, {"error": "現在沒有等待允許的動作"}
        if body.get("pending") and body["pending"] != t.pending["id"]:
            return 409, {"error": "這個動作已經過期了，畫面上有新的一步"}
        t.decision = {"allow": body.get("allow") is True, "always": body.get("always") is True}
        t.event.set()
    return 200, {"ok": True}


def stop(t):
    t.stop = True
    t.event.set()
    return 200, {"ok": True}


def png_ok(b64):
    try:
        return base64.b64decode(b64[:64])[:8] == b"\x89PNG\r\n\x1a\n"
    except Exception:  # noqa: BLE001
        return False
