#!/usr/bin/env python3
"""桌面版（xConsole 應用程式帶起來的主控台）的自我檢查。
- 本機通道只收 Host／Origin 是 127.0.0.1:這個埠的請求（別的網頁、DNS rebinding 連不進來）
- 用應用程式給的鑰匙進入：鑰匙不對拒絕；對了拿到登入，cookie 不帶 Secure（本機 http）、不送 HSTS
- 第一次設密碼不用輸入舊密碼（原本是沒有人知道的隨機密碼）；設好之後本機視窗不會被登出
- 區網存取：沒設密碼前打不開；設好之後打得開（主控台重新啟動，測試裡攔下來）
- 一般（HTTPS）通道不能用鑰匙進入
跑法：python3 test_desktop.py
"""
import http.client
import json
import os
import secrets
import sys
import tempfile
import threading

TMP = tempfile.mkdtemp(prefix="xc-desk-")
os.environ["HOME"] = TMP
sys.argv = [sys.argv[0], "--desktop"]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xconsole_server as x  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def main():
    key = secrets.token_urlsafe(32)
    x.DESKTOP_KEY = key
    x.set_credentials("admin", secrets.token_urlsafe(32))
    x.update_fleet(lambda c: c.__setitem__("desktop_pw_unset", True))
    loop = ThreadingHTTPServer(("127.0.0.1", 0), x.LoopHandler)
    x.APP_PORT = loop.server_address[1]
    threading.Thread(target=loop.serve_forever, daemon=True).start()
    plain = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)            # 代表一般通道（不是本機視窗）
    threading.Thread(target=plain.serve_forever, daemon=True).start()
    restarted = []
    x.restart_console = lambda: restarted.append(1)
    jar = {}

    def req(method, path, body=None, host=None, origin=None, port=None, cookie=True):
        c = http.client.HTTPConnection("127.0.0.1", port or x.APP_PORT, timeout=10)
        h = {"Host": host or f"127.0.0.1:{x.APP_PORT}"}
        if body is not None:
            h["Content-Type"] = "application/json"
        if origin:
            h["Origin"] = origin
        if cookie and jar.get("v"):
            h["Cookie"] = "xconsole=" + jar["v"]
        c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
        r = c.getresponse()
        data = r.read()
        sc = r.getheader("Set-Cookie") or ""
        if sc.startswith("xconsole=") and cookie:
            jar["v"] = sc.split(";")[0].split("=", 1)[1]
        try:
            out = json.loads(data or b"{}")
        except ValueError:
            out = {}
        return r.status, out, r, sc

    check("Host 不是 127.0.0.1:這個埠就擋（DNS rebinding）", req("GET", "/api/me", host="evil.example:80")[0] == 421)
    check("  別的網頁的 Origin 也擋", req("POST", "/api/desktop/enter", {"key": key}, origin="http://evil.example")[0] == 421)
    check("鑰匙不對：拒絕", req("POST", "/api/desktop/enter", {"key": "x" * 43})[0] == 403)
    st, out, r, sc = req("POST", "/api/desktop/enter", {"key": key})
    check("用應用程式的鑰匙進入", st == 200 and jar.get("v"), out)
    check("  cookie 是 HttpOnly、SameSite=Strict，不帶 Secure（本機 http）", "HttpOnly" in sc and "SameSite=Strict" in sc and "Secure" not in sc, sc)
    check("  本機通道不送 HSTS", r.getheader("Strict-Transport-Security") is None)
    st, me, _, _ = req("GET", "/api/me")
    check("登入後認得是應用程式的視窗", st == 200 and me.get("app_window") is True and me.get("user") == "admin", me)
    st, sett, _, _ = req("GET", "/api/settings")
    check("設定回報桌面版、區網關著、還沒設密碼", sett.get("desktop") and not sett.get("lan_access") and sett.get("password_unset"), sett)
    st, out, _, _ = req("POST", "/api/settings/lan", {"on": True})
    check("沒設密碼前打不開區網存取", st == 400 and out.get("need_password") and not restarted, out)
    st, out, _, _ = req("POST", "/api/account/password", {"old": "", "new": "desk-pass-1234"})
    check("第一次設密碼不用輸入舊密碼", st == 200, out)
    check("  設好之後本機視窗還是登入的", req("GET", "/api/me")[0] == 200)
    check("  密碼生效", x.check_credentials("admin", "desk-pass-1234"))
    st, out, _, _ = req("POST", "/api/account/password", {"old": "wrong", "new": "desk-pass-5678"})
    check("  第二次改就要舊密碼", st == 403, out)
    st, out, _, _ = req("POST", "/api/settings/lan", {"on": True})
    import time
    time.sleep(1.2)
    check("設好密碼後打得開區網存取（主控台重新啟動來換聽的位址）", st == 200 and x.fleet().get("lan_access") is True and restarted, (st, out))
    st, out, _, _ = req("POST", "/api/desktop/enter", {"key": key}, host=f"127.0.0.1:{plain.server_address[1]}",
                        port=plain.server_address[1], cookie=False)
    check("一般通道不能用鑰匙進入", st == 403, (st, out))
    audit = json.dumps(x.STORE.audit_list(100), ensure_ascii=False)
    check("稽核不記鑰匙與密碼", key not in audit and "desk-pass-1234" not in audit)
    print("全部通過" if OK else "有項目失敗")
    sys.exit(0 if OK else 1)


if __name__ == "__main__":
    main()
