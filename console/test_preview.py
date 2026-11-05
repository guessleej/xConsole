#!/usr/bin/env python3
"""執行預覽（模型寫出的 HTML 直接跑）的自我檢查：標頭把它關進 sandbox、只有建立者那一個登入看得到、
30 分鐘過期、單筆 2 MB、記憶體裡最多 50 筆、全站其他頁仍是 X-Frame-Options: DENY。
跑法：python3 test_preview.py（另起一個隔離的主控台，純 HTTP，不碰正式環境）
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-preview-")
os.environ["HOME"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xconsole_server as x  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


x.set_credentials("admin", "preview-test-pw")
srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def req(method, path, body=None, cookie=None):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", "xconsole=" + cookie)
    try:
        resp = urllib.request.urlopen(r, timeout=10)
    except urllib.error.HTTPError as e:
        resp = e
    return resp.status, resp.read(), resp.headers


def login():
    st, _, h = req("POST", "/api/login", {"username": "admin", "password": "preview-test-pw"})
    return h.get("Set-Cookie").split(";")[0].split("=", 1)[1] if st == 200 else None


def main():
    a = login()
    b = login()                                # 同一個帳號的另一個登入（另一台電腦）
    check("兩個登入都拿到票證", a and b and a != b)
    st, _, _ = req("POST", "/api/preview", {"html": "<p>x</p>"})
    check("沒登入不能建立預覽", st == 401)
    html = "<!doctype html><script>document.write('跑起來了')</script>"
    st, raw, _ = req("POST", "/api/preview", {"html": html}, a)
    out = json.loads(raw)
    check("建立預覽：回 /preview/<id>", st == 200 and out["url"].startswith("/preview/") and out["expires_at"] > time.time(), out)
    url = out["url"]
    st, raw, h = req("GET", url, cookie=a)
    csp = h.get("Content-Security-Policy") or ""
    check("建立者看得到，內容原樣", st == 200 and raw.decode() == html)
    check("CSP 是 sandbox（不透明來源），允許 scripts／forms／modals／popups",
          csp.startswith("sandbox allow-scripts allow-forms allow-modals allow-popups") and "allow-same-origin" not in csp, csp)
    check("  可以載入外部 CDN 的腳本與 inline 的 script／style", "script-src 'unsafe-inline'" in csp and "https:" in csp
          and "style-src 'unsafe-inline' https:" in csp, csp)
    check("  只准主控台自己的頁面嵌入：X-Frame-Options SAMEORIGIN、frame-ancestors 'self'",
          h.get("X-Frame-Options") == "SAMEORIGIN" and "frame-ancestors 'self'" in csp)
    check("  型別是 HTML、不快取、不送 Referer", h.get("Content-Type", "").startswith("text/html") and h.get("Cache-Control") == "no-store"
          and h.get("Referrer-Policy") == "no-referrer")
    _, _, h2 = req("GET", "/api/me", cookie=a)
    check("全站其他頁仍是 X-Frame-Options: DENY", h2.get("X-Frame-Options") == "DENY" and "frame-ancestors 'none'" in (h2.get("Content-Security-Policy") or ""))
    st, raw, _ = req("GET", url, cookie=b)
    check("別人的登入（同帳號另一個 session）拿不到 → 404", st == 404 and html.encode() not in raw)
    st, raw, _ = req("GET", url)
    check("沒登入拿不到 → 404", st == 404)
    st, _, _ = req("GET", "/preview/" + "A" * 22, cookie=a)
    check("不存在的 id → 404", st == 404)
    pid = url.rsplit("/", 1)[1]
    x._PREVIEWS[pid]["exp"] = time.time() - 1
    st, _, _ = req("GET", url, cookie=a)
    check("過期（30 分鐘）→ 404，而且從記憶體清掉", st == 404 and pid not in x._PREVIEWS)
    check("保留時間是 30 分鐘", x.PREVIEW_TTL == 1800)
    st, raw, _ = req("POST", "/api/preview", {"html": "a" * (2 * 1024 * 1024 + 1)}, a)
    check("單筆上限 2 MB：超過回 400", st == 400 and "2 MB" in json.loads(raw)["error"])
    st, _, _ = req("POST", "/api/preview", {"html": "a" * (2 * 1024 * 1024 - 100)}, a)
    check("  剛好在上限內可以", st == 200)
    st, _, _ = req("POST", "/api/preview", {"html": ""}, a)
    check("空內容 → 400", st == 400)
    ids = []
    for i in range(55):
        st, raw, _ = req("POST", "/api/preview", {"html": f"<p>{i}</p>"}, a)
        ids.append(json.loads(raw)["id"])
    check("記憶體裡最多 50 筆，舊的先丟", len(x._PREVIEWS) == 50 and ids[0] not in x._PREVIEWS and ids[-1] in x._PREVIEWS)
    st, _, _ = req("GET", f"/preview/{ids[0]}", cookie=a)
    check("  被擠掉的那筆 → 404", st == 404)
    req("POST", "/api/logout", {}, a)
    st, _, _ = req("GET", f"/preview/{ids[-1]}", cookie=a)
    check("登出之後自己的預覽也拿不到", st == 404)
    st, raw, _ = req("GET", "/api/admin/audit?limit=200", cookie=b)
    dump = raw.decode()
    check("稽核記了建立預覽（只記大小，不記內容）", "preview.create" in dump and "跑起來了" not in dump)
    srv.shutdown()
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
