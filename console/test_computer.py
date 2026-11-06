#!/usr/bin/env python3
"""操作電腦與 Hugging Face 令牌的自我檢查（不連真的桌面、不連外）。
- 模型回答解析：包在 ```json 裡、前後多講話、尾巴多大括號都撈得到
- 任務迴圈：每一步等人按允許；拒絕就換做法；「這次都允許」之後不再問；done 就結束；
  模型連續三次回不出動作就停；這台沒開或沒有桌面時講清楚原因；最多 30 步
- 打字只記字數不記內容（稽核）
- Hugging Face 令牌：加密落地、GET 只回有沒有設、下載與搜尋沒填令牌時自動帶上
跑法：python3 test_computer.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-pc-")
os.environ["HOME"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import computer as C  # noqa: E402
import xconsole_server as x  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def fake_env(script, status=None):
    """script：模型每一輪要回的文字。回 (call_node, model_call, 紀錄)。"""
    rec = {"acts": [], "audit": [], "asked": 0}
    st = status or {"enabled": True, "available": True, "os": "windows"}

    def call_node(node, path, method="GET", body=None, timeout=40):
        if path == "/desktop/status":
            return 200, st
        if path == "/desktop/screenshot":
            return 200, {"png": "iVBORw0KGgo=", "width": 1280, "height": 720, "screen_w": 2560, "screen_h": 1440}
        if path == "/desktop/act":
            rec["acts"].append(body)
            return 200, {"ok": True}
        return 404, {"error": path}

    def model_call(t, req):
        i = rec["asked"]
        rec["asked"] += 1
        img = req["messages"][1]["content"][1]["image_url"]["url"]
        assert img.startswith("data:image/png;base64,")
        text = script[i] if i < len(script) else '{"action":"done","result":"完成"}'
        return 200, {"choices": [{"message": {"content": text}}]}

    def audit(action, target, ok, detail=None):
        rec["audit"].append((action, ok, detail))
    return call_node, model_call, audit, rec


def wait_next(t, old, sec=10):
    """等到出現新的一步要允許（pending 編號換了），或任務結束。"""
    end = time.time() + sec
    while time.time() < end:
        p = t.pending
        if (p and p["id"] != old) or t.state not in ("running", "waiting_approval"):
            return True
        time.sleep(0.05)
    return False


def wait_state(t, want, sec=10):
    end = time.time() + sec
    while time.time() < end:
        if t.state in want:
            return True
        time.sleep(0.05)
    return False


def main():
    # ---- 解析
    for raw, want in (('```json\n{"action":"click","x":1,"y":2}\n```', "click"), ('我先點一下 {"action":"type","text":"你好"} 這樣', "type"),
                      ('{"action":"key","keys":"ctrl+s"} {"x":1}', "key"), ("沒有 JSON", None)):
        got = (C.parse_action(raw) or {}).get("action")
        check(f"解析模型回答：{raw[:24]!r}", got == want, got)

    # ---- 允許、拒絕、這次都允許、完成
    script = ['{"thought":"點開始","action":"click","x":10,"y":700}',
              '{"action":"type","text":"機密 0912345678"}',
              '{"action":"key","keys":"enter"}',
              '{"action":"click","x":5,"y":5}',
              '{"action":"done","result":"記事本已打開並存檔"}']
    cn, mc, au, rec = fake_env(script)
    t = C.ComputerTask("admin", "127.0.0.1", "n1", {"kind": "local"}, "打開記事本", "本機")
    th = threading.Thread(target=C.run, args=(t, cn, mc, au), daemon=True)
    th.start()
    check("第一步停下來等人允許", wait_state(t, ("waiting_approval",)) and t.pending["action"]["action"] == "click")
    check("  等待中還沒有真的動到滑鼠", rec["acts"] == [])
    check("  畫面拿得到截圖", t.public().get("shot") == "iVBORw0KGgo=")
    pend = t.pending["id"]
    check("  允許用的編號不對會被拒絕（避免按到舊的那一步）", C.decide(t, {"allow": True, "pending": "zzzz"})[0] == 409)
    C.decide(t, {"allow": True, "pending": pend})
    check("允許之後才送出動作，座標與截圖尺寸一起送", wait_next(t, pend) and len(rec["acts"]) == 1
          and rec["acts"][0]["x"] == 10 and rec["acts"][0]["screen_w"] == 2560)
    check("  截圖沒變時不重送（after）", "shot" not in t.public(t.shot_id))
    p2 = t.pending["id"]
    C.decide(t, {"allow": False})
    check("拒絕：不送出，換下一步", wait_next(t, p2) and len(rec["acts"]) == 1 and t.pending["action"]["action"] == "key")
    C.decide(t, {"allow": True, "always": True})
    th.join(10)
    check("這次都允許：之後不再問，直到 done", t.state == "done" and [a["action"] for a in rec["acts"]] == ["click", "key", "click"]
          and t.result == "記事本已打開並存檔", (t.state, rec["acts"]))
    typed = [d for a, ok, d in rec["audit"] if a == "computer.act"]
    check("稽核記了每個動作", len(typed) == 3)
    check("  打字不記內容", "0912345678" not in json.dumps(rec["audit"], ensure_ascii=False))

    # ---- 座標超出畫面：問人之前就擋，告訴模型重來
    cn, mc, au, rec = fake_env(['{"action":"click","x":100,"y":950}', '{"action":"key","keys":"win"}', '{"action":"done","result":"好"}'])
    t = C.ComputerTask("admin", "", "n1", {}, "打開開始功能表", "本機")
    t.always = False
    th = threading.Thread(target=C.run, args=(t, cn, mc, au), daemon=True)
    th.start()
    check("座標超出畫面：不問人、直接請模型重來", wait_state(t, ("waiting_approval",)) and t.pending["action"]["action"] == "key"
          and any("超出畫面" in x["text"] for x in t.log), [x["text"] for x in t.log])
    C.decide(t, {"allow": True})
    th.join(10)
    check("  單按 Windows 鍵也收", t.state == "done" and rec["acts"] and rec["acts"][0]["keys"] == "win", rec["acts"])

    # ---- 模型亂回
    cn, mc, au, rec = fake_env(["我不知道", "嗯", "?"])
    t = C.ComputerTask("admin", "", "n1", {}, "做點事", "本機")
    C.run(t, cn, mc, au)
    check("模型連續三次給不出動作就停，講原因", t.state == "failed" and "三次" in (t.error or ""), t.error)

    # ---- action 不是字串、模型呼叫中途按停止、意外例外
    cn, mc, au, rec = fake_env(['{"action":{"x":1}}', '{"action":["click"]}', '{"action":7}'])
    t = C.ComputerTask("admin", "", "n1", {}, "做點事", "本機")
    C.run(t, cn, mc, au)
    check("action 不是字串算沒給動作，不會讓執行緒當掉", t.state == "failed" and "三次" in (t.error or ""), (t.state, t.error))
    cn, mc, au, rec = fake_env(['{"action":"click","x":1,"y":1}'])
    t = C.ComputerTask("admin", "", "n1", {}, "點", "本機")
    t.always = True

    def slow_model(task, req):
        task.stop = True                          # 模擬：模型還在想的時候使用者按了停止
        return mc(task, req)
    C.run(t, cn, slow_model, au)
    check("問模型期間按了停止：回來的動作不執行", t.state == "stopped" and rec["acts"] == [], (t.state, rec["acts"]))

    def boom(*a, **k):
        raise KeyError("width")
    t = C.ComputerTask("admin", "", "n1", {}, "點", "本機")
    C.run(t, lambda *a, **k: (200, {"enabled": True, "available": True}) if a[1] == "/desktop/status" else (200, {}), boom, au)
    check("意外例外：任務轉成失敗，不會一直卡在進行中", t.state == "failed" and "意外" in (t.error or ""), (t.state, t.error))

    # ---- 這台沒開、沒有桌面
    cn, mc, au, rec = fake_env([], {"enabled": False, "available": True})
    t = C.ComputerTask("admin", "", "n1", {}, "做點事", "本機")
    C.run(t, cn, mc, au)
    check("這台沒開操作電腦：講清楚去哪裡開", t.state == "failed" and "這台的設定" in t.error, t.error)
    cn, mc, au, rec = fake_env([], {"enabled": True, "available": False, "why": "桌面小幫手還沒啟動"})
    t = C.ComputerTask("admin", "", "n1", {}, "做點事", "本機")
    C.run(t, cn, mc, au)
    check("沒有桌面：照實說原因", t.state == "failed" and "小幫手" in t.error, t.error)

    # ---- 步數上限與停止
    cn, mc, au, rec = fake_env(['{"action":"wait"}'] * 40)
    real = C.MAX_STEPS
    C.MAX_STEPS = 3
    try:
        t = C.ComputerTask("admin", "", "n1", {}, "一直等", "本機")
        t.always = True
        C.run(t, cn, mc, au)
        check("步數到上限就停", t.state == "failed" and "3 步" in t.error, t.error)
    finally:
        C.MAX_STEPS = real
    cn, mc, au, rec = fake_env(['{"action":"click","x":1,"y":1}'] * 5)
    t = C.ComputerTask("admin", "", "n1", {}, "點", "本機")
    th = threading.Thread(target=C.run, args=(t, cn, mc, au), daemon=True)
    th.start()
    wait_state(t, ("waiting_approval",))
    C.stop(t)
    th.join(10)
    check("按停止：等待中的那一步不會執行", t.state == "stopped" and rec["acts"] == [], (t.state, rec["acts"]))

    # ---- 同時只能一個任務
    with C.LOCK:
        C.TASKS.clear()
    cn, mc, au, rec = fake_env(['{"action":"click","x":1,"y":1}'])
    c1, o1 = C.start("admin", "", "n1", {}, "第一件事", "本機", cn, mc, au)
    c2, o2 = C.start("admin", "", "n1", {}, "第二件事", "本機", cn, mc, au)
    check("同時只能有一個操作電腦的任務", c1 == 200 and c2 == 409, (c1, c2))
    C.stop(C.TASKS[o1["id"]])
    check("交代的事太短擋下", C.start("admin", "", "n1", {}, "x", "本機", cn, mc, au)[0] == 400)

    # ---- Hugging Face 令牌
    x.set_credentials("admin", "pc-test-pw")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    cookie = {}

    def req(method, path, body=None):
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method=method)
        if body is not None:
            r.add_header("Content-Type", "application/json")
        if cookie.get("v"):
            r.add_header("Cookie", "xconsole=" + cookie["v"])
        try:
            resp = urllib.request.urlopen(r, timeout=20)
        except urllib.error.HTTPError as e:
            resp = e
        sc = resp.headers.get("Set-Cookie") or ""
        if sc.startswith("xconsole="):
            cookie["v"] = sc.split(";")[0].split("=", 1)[1]
        return resp.status, json.loads(resp.read() or b"{}")
    req("POST", "/api/login", {"username": "admin", "password": "pc-test-pw"})
    tok = "hf_" + "A" * 30
    st, out = req("POST", "/api/settings/hf", {"token": "not-a-token"})
    check("令牌格式不對擋下", st == 400)
    st, out = req("POST", "/api/settings/hf", {"token": tok})
    check("存令牌", st == 200 and out["token_set"])
    raw = open(os.path.join(TMP, ".xconsole", "fleet.json"), encoding="utf-8").read()
    check("  fleet.json 裡是 enc:v1: 密文，沒有明文", tok not in raw and '"enc:v1:' in raw)
    st, out = req("GET", "/api/settings")
    check("  GET 只回有沒有設", out.get("hf_token_set") is True and tok not in json.dumps(out))
    sent = {}
    real_cn = x.call_node
    x.call_node = lambda nid, path, method="GET", body=None, timeout=40: (sent.__setitem__(path, body), (200, {"items": []}))[1]
    try:
        req("POST", "/api/node/n1/library/hf/search", {"q": "gemma"})
        check("搜尋沒填令牌：自動帶上存著的", (sent.get("/library/hf/search") or {}).get("token") == tok, sent)
        req("POST", "/api/node/n1/library/hf/search", {"q": "gemma", "token": "hf_" + "B" * 30})
        check("  畫面有填就用畫面填的", sent["/library/hf/search"]["token"] == "hf_" + "B" * 30)
    finally:
        x.call_node = real_cn
    audit = json.dumps(x.STORE.audit_list(200), ensure_ascii=False)
    check("  稽核裡沒有令牌", tok not in audit)
    st, out = req("POST", "/api/settings/hf", {"clear": True})
    check("清除令牌", st == 200 and not req("GET", "/api/settings")[1]["hf_token_set"])

    print("全部通過" if OK else "有項目失敗")
    sys.exit(0 if OK else 1)


if __name__ == "__main__":
    main()
