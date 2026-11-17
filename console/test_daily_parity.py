#!/usr/bin/env python3
"""對齊 LocModel「每天會碰到」那幾項的自我檢查（docs/LOCMODEL_PARITY.md第34～45項）：
- 有新版要通知：檢查結果、跳過這個版本記在主控台、只有管理員看得到、版本號格式
- 載入中的最後一行記錄：取最後一行有字的、金鑰遮掉、太長截斷
- 思考開關的預設：llama.cpp 欄位回報啟動時的思考模式
- 畫面守衛（掃原始碼）：推薦列速度小於 1、匯出 JSON、拖放與貼上截圖、思考開關、工具的詢問視窗不能點背景關、
  執行頁的引擎未安裝提示與執行日誌只接新增的行、頂列的更新按鈕
跑法：python3 test_daily_parity.py（不連外）
"""
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-daily-")
os.environ["XCONSOLE_HOME"] = TMP
os.environ["HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "agent")]
import xconsole_server as x  # noqa: E402
import xconsole_agent as A  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OK = True
ST = os.path.join(HERE, "static")


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


x.update_fleet(lambda c: c.__setitem__("nodes", []))
x.set_credentials("admin", "daily-test-pw-1")
srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


class Client:
    def __init__(self):
        self.cookie = ""

    def req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if self.cookie:
            r.add_header("Cookie", "xconsole=" + self.cookie)
        try:
            resp = urllib.request.urlopen(r, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        raw = resp.read()
        sc = resp.headers.get("Set-Cookie") or ""
        if sc.startswith("xconsole="):
            self.cookie = sc.split(";")[0].split("=", 1)[1]
        return resp.status, json.loads(raw or b"{}")


def src(name):
    return open(os.path.join(ST, name), encoding="utf-8").read()


def fn_body(text, start):
    i = text.index(start)
    depth, j = 0, text.index("{", i)
    for k in range(j, len(text)):
        depth += {"{": 1, "}": -1}.get(text[k], 0)
        if depth == 0:
            return text[i:k + 1]
    return text[i:]


def main():
    admin = Client()
    admin.req("POST", "/api/login", {"username": "admin", "password": "daily-test-pw-1"})
    st, out = admin.req("POST", "/api/users/create", {"username": "u2@cloudinfo.com.tw", "display": "使用者", "role": "user"})
    user = Client()
    user.req("POST", "/api/login", {"username": "u2@cloudinfo.com.tw", "password": out["password"]})
    user.req("POST", "/api/account/password", {"old": out["password"], "new": "user-pass-00001"})
    user.req("POST", "/api/login", {"username": "u2@cloudinfo.com.tw", "password": "user-pass-00001"})

    # ---- 有新版要通知
    st, n = admin.req("GET", "/api/update/notice")
    check("還沒檢查過：不顯示更新按鈕", st == 200 and n["show"] is False and n["checked"] is None, n)
    real = x.updater.check
    x.updater.check = lambda timeout=20: (200, {"current": "2026.11.17", "latest": "2026.12.1", "newer": True,
                                                "notes": "- 新增思考開關\n- 修正速度顯示", "published": "2026-12-01T00:00:00Z"})
    x.update_poll()
    st, n = admin.req("GET", "/api/update/notice")
    check("有新版：顯示「更新到 v2026.12.1」並帶更新內容", n["show"] and n["latest"] == "2026.12.1" and "思考開關" in n["notes"], n)
    st, _ = user.req("GET", "/api/update/notice")
    check("  一般使用者看不到", st == 403)
    st, _ = user.req("POST", "/api/update/skip", {"version": "2026.12.1"})
    check("  一般使用者不能跳過", st == 403)
    st, out = admin.req("POST", "/api/update/skip", {"version": "../../x"})
    check("  版本號格式不對 → 400", st == 400)
    st, n = admin.req("POST", "/api/update/skip", {"version": "2026.12.1"})
    check("跳過這個版本：記在主控台、按鈕收起", st == 200 and n["show"] is False and x.fleet()["update_skip"] == "2026.12.1", n)
    x.updater.check = lambda timeout=20: (200, {"current": "2026.11.17", "latest": "2026.12.5", "newer": True, "notes": ""})
    x.update_poll()
    check("  再出更新的一版：照樣通知", x.update_notice()["show"] and x.update_notice()["latest"] == "2026.12.5")
    x.updater.check = lambda timeout=20: (200, {"current": "2026.12.5", "latest": "2026.12.5", "newer": False})
    x.update_poll()
    check("  已經是最新版：不通知", not x.update_notice()["show"])
    x.updater.check = lambda timeout=20: (502, {"error": "連不上 GitHub", "offline": True})
    x.update_poll()
    check("  連不上 GitHub：不通知、不報錯", not x.update_notice()["show"] and x.update_notice()["ok"] is False)
    x.updater.check = real
    check("  主控台啟動時會開檢查新版的背景執行緒（每天一次）", "target=update_watch" in open(os.path.join(HERE, "xconsole_server.py"), encoding="utf-8").read()
          and x.UPDATE_EVERY == 86400)

    # ---- 載入中的最後一行記錄
    check("最後一行記錄：取最後一行有字的", A.last_log_line("a\nload_tensors: offloading 33 layers to GPU\n\n  \n") == "load_tensors: offloading 33 layers to GPU")
    check("  像金鑰的值遮掉", "sk-secret" not in A.last_log_line("main: --api-key sk-secret-1234 listening")
          and "[已遮罩]" in A.last_log_line("token=abcdef123 x"))
    check("  太長截斷到 300 字", len(A.last_log_line("x" * 1000)) == 300)
    check("  沒有記錄回空字串", A.last_log_line("") == "" and A.last_log_line(None) == "")
    agent_src = open(os.path.join(os.path.dirname(HERE), "agent", "xconsole_agent.py"), encoding="utf-8").read()
    check("  載入進度回報帶 last_line", 'out["last_line"] = last_log_line(text)' in agent_src)

    # ---- 思考開關的預設
    cfgp = os.path.join(TMP, "llama.json")
    json.dump({"model": "/nope.gguf", "port": 8080, "reasoning": "off"}, open(cfgp, "w"))
    check("llama.cpp 欄位回報啟動時的思考模式（對話頁的預設）", A.llama_config(cfgp)["thinking"] == "off")
    json.dump({"model": "/nope.gguf", "port": 8080}, open(cfgp, "w"))
    check("  沒設＝auto", A.llama_config(cfgp)["thinking"] == "auto")

    # ---- 畫面守衛
    app, pages, chat, html = src("app.js"), src("pages.js"), src("chat.js"), src("index.html")
    rec = app[app.index('const fit = el("div", "rec-fit")'):][:600]
    check("推薦列：速度小於 1 顯示「不到1 tok/s」，不會出現「約 0.0 tok/s」", 'p.tok_s < 1 ? "不到1 tok/s"' in rec, rec[:300])
    check("對話：有匯出 JSON（含收藏、分類、標籤、訊息）", "function exportChatJson" in chat and 'format: "xconsole-chat"' in chat
          and "messages: cur.msgs" in chat)
    check("  匯出 Markdown 與匯出 JSON 兩個按鈕都在", '"匯出Markdown"' in chat and '"匯出JSON"' in chat)
    check("對話：拖檔進輸入區與貼上截圖走同一條附件路", re.search(r'addEventListener\("drop"', chat) and re.search(r'addEventListener\("paste"', chat)
          and chat.count("addFiles(") >= 4)
    add = fn_body(chat, "async function addFiles(")
    check("  沿用附件的大小與數量上限", "4 * 1024 * 1024" in add and "15 * 1024 * 1024" in add and "cur.docs.length >= 5" in add)
    check("對話：思考開關每次請求帶 chat_template_kwargs.enable_thinking", "chat_template_kwargs: { enable_thinking: thinkOn() }" in chat
          and "...thinkBody(t)" in chat and "think: thinkOn()" in chat)
    check("  模型不支援時反灰並說明", "thinkB.disabled = !ok" in chat and "思考：不支援" in chat)
    ask = fn_body(chat, "function askTool(")
    check("工具的詢問視窗：有「本次對話不再詢問同類操作」、不能點背景關閉", "本次對話不再詢問同類操作" in ask and "hasInput: true" in ask)
    check("對話左欄：全部／收藏篩選、分類篩選、訊息數", '"全部"' in chat and '"收藏"' in chat and "chat-cat" in chat and "it.n_msgs" in chat)
    edit = fn_body(chat, "function editChat(")
    check("  編輯對話的視窗不能點背景關閉（有輸入欄位）", "hasInput: true" in edit)
    check("執行頁：沒有推理引擎時直接提示、給一鍵安裝與匯入安裝包", "先安裝推理引擎" in pages and "eng.llama_server === false" in pages
          and "importOpen: true" in pages and "opts.importOpen" in app)
    log = fn_body(pages, "function runLogBox(")
    check("執行頁：執行日誌只接新增的行（不在每次輪詢清空重畫）", "createTextNode" in log and "d.open" in log
          and log.count('pre.textContent = ""') <= 1 and 'addEventListener("toggle"' in log)
    check("  載入中顯示 llama-server 最後一行記錄", "r.last_line" in pages and "load-last" in pages)
    check("頂列有「更新到 vX」按鈕（預設藏起來，只有管理員會打開）", 'id="upd-top"' in html and re.search(r'id="upd-top"[^>]*hidden', html)
          and "if (!isAdmin()) { b.hidden = true; return; }" in app)
    um = fn_body(app, "function updateNoticeModal(")
    check("  彈窗三個按鈕：跳過這個版本／稍後／立即更新", all(k in um for k in ('"跳過這個版本"', '"稍後"', '"立即更新"')))
    check("Agent 頁：任務可以刪除、計畫顯示 N/M、轉圈不重播（只改有變的那幾列）",
          "/delete`" in pages and "planCount" in pages and "r.className !== cls" in pages)
    css = src("style.css")
    check("  進行中那一項有動畫、沒做完的另外標示", ".plan-item.in_progress::before" in css and "animation: spin" in css and ".plan-item.stopped" in css)

    srv.shutdown()
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
