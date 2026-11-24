#!/usr/bin/env python3
"""Windows工作站執行環境包（「加一台機器」的Windows腳本不必先裝Python、不必連pypi.org）。

一、檢查這一包：不是zip、路徑往上跳、絕對路徑、磁碟代號、python／runtime以外的檔、連結檔、少了必要的檔，一律擋下。
二、上傳：合格的存起來並記sha256；不合格的不動原本那一包。
三、從GitHub下載：只拿跟主控台同一版的附件、核對GitHub公布的sha256；對不上就丟掉並講原因；沒有附件也講原因。
四、主控台端對端：下載端點要有效的加入碼；腳本寫死這一包的sha256（沒有就是空的、改用那台的Python）；
    只有管理員看得到狀態、能下載與上傳；加入碼的回應帶執行環境的狀態。
跑法：python3 test_winrt.py（隔離HOME，GitHub用本機假站）
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TMP = tempfile.mkdtemp(prefix="xc-winrt-")
os.environ["HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent"))
import winrt  # noqa: E402
import updater  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def pack(names=winrt.MUST, extra=None, symlink=None):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n in names:
            z.writestr(n, b"MZ" + n.encode())
        for n, d in (extra or {}).items():
            z.writestr(n, d)
        if symlink:
            i = zipfile.ZipInfo(symlink)
            i.external_attr = 0o120777 << 16
            z.writestr(i, b"/etc/passwd")
    return b.getvalue()


def unit():
    check("合格的執行環境包：通過", winrt.check(pack()) is None)
    check("不是zip：擋下", "不是zip" in (winrt.check(b"not a zip") or ""))
    for bad in ("../evil.dll", "python/../../evil.dll", "/abs/x.dll", "C:/Windows/x.dll"):
        check(f"不安全的路徑 {bad}：擋下", "不安全" in (winrt.check(pack(extra={bad: b"x"})) or ""), winrt.check(pack(extra={bad: b"x"})))
    check("python／runtime以外的檔：擋下", "以外" in (winrt.check(pack(extra={"agent.ps1": b"x"})) or ""))
    check("連結檔：擋下", "連結" in (winrt.check(pack(symlink="python/link.dll")) or ""))
    check("少了vcomp140.dll：擋下並講少了什麼", "vcomp140" in (winrt.check(pack(names=winrt.MUST[:-1])) or ""))

    d = os.path.join(TMP, "st")
    good = pack()
    code, out = winrt.upload(d, good)
    check("上傳合格的：存起來並記sha256", code == 200 and out["sha256"] == hashlib.sha256(good).hexdigest(), out)
    code, out = winrt.upload(d, pack(names=winrt.MUST[:2]))
    check("上傳不合格的：400，原本那一包不動", code == 400 and winrt.meta(d)["sha256"] == hashlib.sha256(good).hexdigest(), out)
    st = winrt.status(d)
    check("狀態：備妥、來源是上傳", st["ready"] and st["source"] == "上傳" and st["size"] == len(good), st)
    open(winrt.path(d), "ab").write(b"x")
    check("檔案被動過（大小不對）：當作沒有", winrt.meta(d) is None)


class FakeGH(BaseHTTPRequestHandler):
    assets = {}
    blobs = {}

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/repos/") and "/releases/tags/" in self.path:
            v = self.path.rsplit("/v", 1)[1]
            if v not in self.assets:
                self.send_response(404)
                self.end_headers()
                return
            b = json.dumps({"assets": self.assets[v]}).encode()
        elif self.path in self.blobs:
            b = self.blobs[self.path]
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def wait_job(d):
    for _ in range(100):
        st = winrt.status(d)
        if not st["job"] or st["job"].get("state") != "downloading":
            return st
        time.sleep(0.1)
    return winrt.status(d)


def github():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    updater.GH_API = base
    v = updater.current_version()
    good = pack(extra={"python/Lib/x.py": b"print(1)"})
    name = f"xconsole-runtime-{v}-win-x64.zip"
    FakeGH.blobs["/dl/good.zip"] = good
    FakeGH.blobs["/dl/tampered.zip"] = good + b"x"
    d = os.path.join(TMP, "gh")

    FakeGH.assets[v] = []
    winrt.fetch(d)
    st = wait_job(d)
    check("GitHub這一版沒有附件：講原因，不留半成品", st["job"] and "沒有" in st["job"]["error"] and not st["ready"], st)

    FakeGH.assets[v] = [{"name": name, "browser_download_url": base + "/dl/tampered.zip",
                         "digest": "sha256:" + hashlib.sha256(good).hexdigest()}]
    winrt.fetch(d)
    st = wait_job(d)
    check("下載的跟GitHub公布的sha256不一樣：丟掉並講原因", st["job"] and "sha256" in st["job"]["error"] and not st["ready"], st)

    FakeGH.assets[v] = [{"name": name, "browser_download_url": base + "/dl/good.zip", "digest": "sha256:" + hashlib.sha256(good).hexdigest()}]
    winrt.fetch(d)
    st = wait_job(d)
    check("對得上：存起來，來源是GitHub、版本是主控台這一版", st["ready"] and st["source"] == "GitHub" and st["version"] == v, st)

    FakeGH.assets[v] = [{"name": name, "browser_download_url": base + "/dl/good.zip"}]
    winrt.fetch(os.path.join(TMP, "gh2"))
    st = wait_job(os.path.join(TMP, "gh2"))
    check("GitHub沒有公布sha256：不下載", st["job"] and "sha256" in st["job"]["error"] and not st["ready"], st)

    winrt.fetch(os.path.join(TMP, "gh3"), quiet=True)
    FakeGH.assets.pop(v)
    st = wait_job(os.path.join(TMP, "gh3"))
    check("啟動時自動試的那一次失敗：不留錯誤（畫面上有按鈕可以再試）", not st["job"], st)


def end_to_end():
    import xconsole_server as x
    x.set_credentials("admin", "winrt-admin-pass-01")
    x.CERTS.ensure(x.hostnames())          # 加入碼要釘主控台憑證：第一次啟動時才會產生，這裡先產
    srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    jar = {}

    def req(method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        r = urllib.request.Request(base + path, data=data, method=method)
        if raw is None and data is not None:
            r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        if jar.get("c"):
            r.add_header("Cookie", "xconsole=" + jar["c"])
        try:
            resp = urllib.request.urlopen(r, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        sc = resp.headers.get("Set-Cookie") or ""
        if sc.startswith("xconsole="):
            jar["c"] = sc.split(";")[0].split("=", 1)[1]
        b = resp.read()
        try:
            return resp.status, json.loads(b or b"{}")
        except ValueError:
            return resp.status, b

    st, _ = req("GET", "/api/winrt")
    check("沒登入：看不到執行環境狀態", st == 401)
    req("POST", "/api/login", {"username": "admin", "password": "winrt-admin-pass-01"})
    st, out = req("GET", "/api/winrt")
    check("管理員看得到狀態（還沒備妥）", st == 200 and out["ready"] is False, out)

    st, out = req("POST", "/api/nodes/join-code", {"name": "W01", "group": "電腦教室A", "console": "127.0.0.1:9400"})
    check("加入碼的回應帶執行環境的狀態", st == 200 and out.get("winrt", {}).get("ready") is False, out.get("winrt"))
    code = out["command"].split("/install/")[1].split("/")[0]
    st, script = req("GET", f"/install/{code}/agent.ps1")
    check("還沒備妥：腳本的 $RT是空的（改用那台的Python）", st == 200 and "$RT = ''" in script.decode("utf-8-sig"), st)
    st, _ = req("GET", f"/install/{code}/winrt.zip")
    check("還沒備妥：下載端點404", st == 404)

    good = pack()
    st, out = req("POST", "/api/winrt/upload", raw=good, headers={"Content-Type": "application/json"})
    check("上傳不是octet-stream：415", st == 415, out)
    st, out = req("POST", "/api/winrt/upload", raw=good, headers={"Content-Type": "application/octet-stream", "X-XConsole-Upload": "1"})
    check("管理員上傳：200", st == 200 and out["sha256"] == hashlib.sha256(good).hexdigest(), out)
    st, script = req("GET", f"/install/{code}/agent.ps1")
    check("備妥之後：腳本寫死這一包的sha256", f"$RT = '{hashlib.sha256(good).hexdigest()}'" in script.decode("utf-8-sig"))
    st, blob = req("GET", f"/install/{code}/winrt.zip")
    check("帶有效加入碼：拿得到同一包", st == 200 and blob == good)
    st, _ = req("GET", "/install/" + "z" * 24 + "/winrt.zip")
    check("加入碼不對：404", st == 404)
    jar.clear()
    st, out = req("POST", "/api/winrt/upload", raw=good, headers={"Content-Type": "application/octet-stream", "X-XConsole-Upload": "1"})
    check("沒登入：不能上傳", st == 401, out)
    aud = json.dumps(x.STORE.audit_list(50), ensure_ascii=False)
    check("稽核記了上傳", "winrt.upload" in aud)
    srv.shutdown()


if __name__ == "__main__":
    unit()
    github()
    end_to_end()
    print("全部通過" if OK else "有失敗")
    sys.exit(0 if OK else 1)
