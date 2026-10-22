#!/usr/bin/env python3
"""交付前實測：在隔離的主控台上，經由網頁 API 走完換憑證的完整流程。

不碰正式環境：另起一個主控台（獨立狀態目錄、獨立埠），測完就收掉。
走的是畫面實際呼叫的 API，不是直接呼叫函式：
  設網址 → 上傳舊式 PFX 檢查 → 套用（實際握手核對）→ 上傳不成對的私鑰（必須擋下且服務不中斷）
  → 開 HSTS（標頭出現）→ 還原上一張（HSTS 自動關、標頭消失）→ 產 CSR → 廠商簽回只傳憑證 → 自動配對並套用
  → 稽核紀錄只有指紋、沒有密碼與私鑰。
跑法（在 .27）：python3 test_e2e_tls.py
"""
import base64
import http.cookiejar
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
W = tempfile.mkdtemp(prefix="xc-e2e-")
HOME = os.path.join(W, "home")
os.makedirs(HOME)
OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail and not cond else ""))
    OK = OK and bool(cond)


def sh(*a, stdin=None):
    r = subprocess.run(list(a), cwd=W, capture_output=True, input=stdin)
    if r.returncode:
        raise RuntimeError(r.stderr.decode()[-400:])
    return r.stdout


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def served_fp(port):
    pem = ssl.get_server_certificate(("127.0.0.1", port), timeout=5)
    return sh("openssl", "x509", "-noout", "-fingerprint", "-sha256", stdin=pem.encode()).decode().split("=")[1].strip()


def main():
    port = free_port()
    env = {**os.environ, "HOME": HOME, "XCONSOLE_PORT": str(port)}

    # 測試用 CA 與網站憑證（涵蓋 127.0.0.1）
    with open(os.path.join(W, "leaf.ext"), "w") as fh:
        fh.write("basicConstraints=CA:FALSE\nsubjectAltName=IP:127.0.0.1\n")
    with open(os.path.join(W, "ca.ext"), "w") as fh:
        fh.write("basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign\n")
    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "root.key", "-out", "root.crt",
       "-days", "30", "-subj", "/O=E2E Root/CN=E2E Root", "-addext", "basicConstraints=critical,CA:TRUE")
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr", "-subj", "/CN=127.0.0.1")
    sh("openssl", "x509", "-req", "-in", "leaf.csr", "-CA", "root.crt", "-CAkey", "root.key", "-CAcreateserial",
       "-out", "leaf.crt", "-days", "30", "-extfile", "leaf.ext")
    sh("openssl", "genrsa", "-out", "other.key", "2048")
    sh("openssl", "pkcs12", "-export", "-legacy", "-certpbe", "PBE-SHA1-RC2-40", "-keypbe", "PBE-SHA1-3DES",
       "-inkey", "leaf.key", "-in", "leaf.crt", "-out", "legacy.pfx", "-passout", "pass:E2e#2026")

    # 隔離的主控台
    srv = os.path.join(HERE, "xconsole_server.py")
    subprocess.run([sys.executable, srv, "--set-credentials"], input=b"admin\ne2e-pass-2026\n",
                   env=env, check=True, capture_output=True)
    proc = subprocess.Popen([sys.executable, srv], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        boot_fp = served_fp(port)
        self_signed = os.path.join(HOME, ".xconsole", "tls", "cert.pem")
        bundle = os.path.join(W, "trust.pem")
        with open(bundle, "wb") as fh:
            fh.write(open(self_signed, "rb").read() + open(os.path.join(W, "root.crt"), "rb").read())
        ctx = ssl.create_default_context(cafile=bundle)     # 驗證開著：只信暫用自簽與測試 CA
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                         urllib.request.HTTPCookieProcessor(jar))
        base = f"https://127.0.0.1:{port}"

        def call(path, body=None, raw=False):
            req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            try:
                resp = op.open(req, timeout=60)
                return resp.status, (resp if raw else json.load(resp))
            except urllib.error.HTTPError as e:
                return e.code, (e if raw else json.load(e))

        def f(name):
            return {"name": name, "data": base64.b64encode(open(os.path.join(W, name), "rb").read()).decode()}

        code, _ = call("/api/login", {"username": "admin", "password": "e2e-pass-2026"})
        check("隔離主控台登入", code == 200, code)
        code, st = call("/api/tls/status")
        check("第一次啟動是暫用自簽", st["current"]["source"] == "selfsigned")

        code, _ = call("/api/tls/settings", {"hostnames": ["127.0.0.1"]})
        check("設定使用者會打的網址", code == 200)

        code, r = call("/api/tls/inspect", {"files": [f("legacy.pfx")], "password": ""})
        check("上傳 PFX 沒給密碼 → 畫面要求輸入密碼", r.get("need_password") and not r["ok"])
        code, r = call("/api/tls/inspect", {"files": [f("legacy.pfx")], "password": "E2e#2026"})
        check("上傳舊式 PFX 並檢查", r["ok"] and r.get("token"), r.get("errors"))
        code, a = call("/api/tls/apply", {"token": r["token"]})
        new_fp = served_fp(port)
        check("套用：不重開服務，實際送出的憑證已換", code == 200 and new_fp != boot_fp
              and new_fp.replace(":", "") == r["summary"]["fingerprint256"].replace(":", ""), a.get("message"))
        check("服務沒有重開（同一個行程）", proc.poll() is None)

        code, r = call("/api/tls/inspect", {"files": [f("leaf.crt"), f("other.key")]})
        check("不成對的私鑰 → 「私鑰和憑證不是同一對」",
              not r["ok"] and any("不是同一對" in e for e in r["errors"]), r.get("errors"))
        check("  擋下之後服務照常、憑證沒被換掉", served_fp(port) == new_fp)

        code, _ = call("/api/tls/settings", {"hsts": True})
        code2, resp = call("/api/health", raw=True)
        check("正式憑證下可開 HSTS，回應帶 Strict-Transport-Security",
              code == 200 and "max-age" in (resp.headers.get("Strict-Transport-Security") or ""))

        code, rb = call("/api/tls/rollback", {})
        check("一鍵還原上一張", code == 200 and served_fp(port) == boot_fp, rb.get("message"))
        code2, resp = call("/api/health", raw=True)
        check("  還原成暫用自簽後 HSTS 標頭消失", not resp.headers.get("Strict-Transport-Security"))
        code, h = call("/api/tls/settings", {"hsts": True})
        check("  暫用自簽時不准開 HSTS", code == 409)

        code, c = call("/api/tls/csr", {"cn": "127.0.0.1", "hostnames": ["127.0.0.1"]})
        check("網頁產生 CSR", code == 200 and "CERTIFICATE REQUEST" in c["csr"])
        with open(os.path.join(W, "web.csr"), "w") as fh:
            fh.write(c["csr"])
        sh("openssl", "x509", "-req", "-in", "web.csr", "-CA", "root.crt", "-CAkey", "root.key", "-CAcreateserial",
           "-out", "web.crt", "-days", "30", "-copy_extensions", "copy")
        code, r = call("/api/tls/inspect", {"files": [f("web.crt")]})
        check("廠商只寄回憑證 → 自動對上 CSR 的私鑰", r["ok"] and any("CSR" in p for p in r["passes"]), r.get("errors"))
        code, a = call("/api/tls/apply", {"token": r["token"]})
        check("  套用 CSR 簽回的憑證", code == 200 and served_fp(port) != boot_fp)
        code, st = call("/api/tls/status")
        check("  套用後待配對的 CSR 自動清掉", st["csr"] is None)

        code, au = call("/api/admin/audit?limit=100")
        dump = json.dumps(au, ensure_ascii=False)
        check("稽核有記下套用與還原", all(x in dump for x in ("tls.apply", "tls.rollback", "tls.csr")))
        check("稽核紀錄沒有 PFX 密碼、沒有私鑰", "E2e#2026" not in dump and "PRIVATE KEY" not in dump)
        key_file = os.path.join(HOME, ".xconsole", "tls", "key.pem")
        check("私鑰檔權限 0600", oct(os.stat(key_file).st_mode & 0o777) == "0o600")

        code, rep = call("/api/admin/usage-report?days=30")
        check("服務使用報告端點六塊都在", code == 200 and all(k in rep for k in (
            "people", "spend", "build_vs_buy", "assets", "integrations", "health")))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(W, ignore_errors=True)

    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
