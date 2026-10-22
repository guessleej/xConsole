#!/usr/bin/env python3
"""安全測試：在隔離的主控台與節點代理上，從攻擊者的角度打一輪。

不碰正式環境：主控台與代理都用獨立 HOME、獨立埠，測完就收掉。
本系統是單一租戶（一套主控台只有一位管理者，沒有「別人的資料」可以跨），
多租戶系統的「跨租戶讀取」在這裡對應成三件事，逐一驗：
  1. 沒登入什麼都拿不到（每一支 API）；
  2. 拿到一張票證之外的任何東西都不算數（竄改、過期、登出後、換密碼後）；
  3. 物件層：只能操作登錄檔裡真的存在的機器與欄位，不能用 id 指到任意主機或檔案路徑（IDOR 等價）。
跑法（在 Linux 機器上，需要 openssl 與 cryptography）：python3 test/security/test_security.py
"""
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.join(ROOT, "console", "xconsole_server.py")
AGENT = os.path.join(ROOT, "agent", "xconsole_agent.py")
W = tempfile.mkdtemp(prefix="xc-sec-")
OK = True
PW = "sec-test-pass-2026"
PLAIN_TOKEN = "plain-agent-token-for-test-0001"


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail and not cond else ""))
    OK = OK and bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_tls(port):
    for _ in range(80):
        try:
            ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


class Client:
    """直接用 http.client：要能送任意標頭與任意路徑（urllib 會幫忙正規化路徑）。"""

    def __init__(self, port, cafile):
        self.port = port
        self.ctx = ssl.create_default_context(cafile=cafile)
        self.ctx.check_hostname = False      # 暫用自簽涵蓋的是主機 IP，不是 127.0.0.1；憑證本身仍要驗
        self.cookie = None

    def req(self, method, path, body=None, headers=None, raw_body=None, cookie=None):
        c = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ctx, timeout=30)
        h = {"Content-Type": "application/json"} if (body is not None or raw_body is not None) else {}
        h.update(headers or {})
        ck = self.cookie if cookie is None else cookie
        if ck:
            h["Cookie"] = f"xconsole={ck}"
        data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode("utf-8", "replace")
        sc = r.getheader("Set-Cookie") or ""
        if "xconsole=" in sc:
            v = sc.split("xconsole=", 1)[1].split(";", 1)[0]
            if v:
                self.last_cookie = v
        c.close()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), text

    def login(self, user="admin", pw=PW):
        self.last_cookie = None
        st, _, _ = self.req("POST", "/api/login", {"username": user, "password": pw}, cookie="")
        return st, self.last_cookie


def console_tests(port, cafile, env):
    cl = Client(port, cafile)

    # ---- 1. 沒登入什麼都拿不到
    gets = ["/api/me", "/api/nodes", "/api/fleet", "/api/node/x/state", "/api/node/x/counters",
            "/api/node/x/logs/brain", "/api/node/x/catalog/brain", "/api/tls/status", "/api/tls/csr",
            "/api/admin/usage-report", "/api/admin/audit", "/api/settings"]
    posts = ["/api/node/x/slot/brain/start", "/api/node/x/slot/brain/model", "/api/tls/inspect",
             "/api/tls/apply", "/api/tls/rollback", "/api/tls/bootstrap", "/api/tls/settings",
             "/api/tls/csr", "/api/node/x/trusted-ca", "/api/settings", "/api/account/password"]
    bad = [p for p in gets if cl.req("GET", p, cookie="")[0] != 401]
    bad += [p for p in posts if cl.req("POST", p, {}, cookie="")[0] != 401]
    check(f"未登入打 {len(gets) + len(posts)} 支 API 全部回 401", not bad, bad)
    st, _, text = cl.req("GET", "/api/health", cookie="")
    check("公開的健康檢查只回狀態，不回機器清單與帳號", st == 200 and "nodes" not in text and "admin" not in text)

    # ---- 2. 票證
    st, sid = cl.login()
    check("正確帳密登入", st == 200 and sid)
    cl.cookie = sid
    check("  帶票證可讀", cl.req("GET", "/api/me")[0] == 200)
    _, h, _ = cl.req("POST", "/api/login", {"username": "admin", "password": PW}, cookie="")
    sc = h.get("set-cookie", "")
    check("  票證 cookie 帶 HttpOnly、Secure、SameSite=Strict",
          all(x in sc for x in ("HttpOnly", "Secure", "SameSite=Strict")), sc)
    tampered = sid[:-2] + ("AA" if not sid.endswith("AA") else "BB")
    check("竄改過的票證 → 401", cl.req("GET", "/api/me", cookie=tampered)[0] == 401)
    check("自己編的票證（舊版簽章格式）→ 401",
          cl.req("GET", "/api/me", cookie=f"{int(time.time()) + 9999}:admin.deadbeef")[0] == 401)
    check("超長票證 → 401 不當機", cl.req("GET", "/api/me", cookie="A" * 5000)[0] == 401)

    st, sid2 = cl.login()
    cl.req("POST", "/api/logout", {}, cookie=sid2)
    check("登出後同一張票證在伺服器端作廢（不只是清 cookie）", cl.req("GET", "/api/me", cookie=sid2)[0] == 401)

    # 過期：直接把資料庫裡這張票證的到期時間改成過去
    st, sid3 = cl.login()
    import sqlite3
    db = sqlite3.connect(os.path.join(env["HOME"], ".xconsole", "xconsole.db"))
    db.execute("UPDATE sessions SET expires=? WHERE expires>?", (time.time() - 1, time.time() + 3600 * 11.9))
    db.commit()
    db.close()
    check("過期的票證 → 401", cl.req("GET", "/api/me", cookie=sid3)[0] == 401)
    cl.cookie = cl.login()[1]

    # 同一條 keep-alive 連線：登入前、登入後、登出後，每個請求都要重新認人
    kc = http.client.HTTPSConnection("127.0.0.1", port, context=cl.ctx, timeout=30)

    def kreq(method, path, ck=None, body=None):
        h = {"Content-Type": "application/json"} if body is not None else {}
        if ck:
            h["Cookie"] = f"xconsole={ck}"
        kc.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = kc.getresponse()
        r.read()
        return r.status, r.getheader("Set-Cookie") or ""

    a = kreq("GET", "/api/me")[0]
    _, sc = kreq("POST", "/api/login", body={"username": "admin", "password": PW})
    ksid = sc.split("xconsole=", 1)[1].split(";", 1)[0]
    b = kreq("GET", "/api/me", ksid)[0]
    kreq("POST", "/api/logout", ksid, body={})
    c = kreq("GET", "/api/me", ksid)[0]
    kc.close()
    check("同一條連線上：登入前 401 → 登入後 200 → 登出後 401（不沿用上一個請求的身分）",
          (a, b, c) == (401, 200, 401), (a, b, c))

    # ---- 3. 請求格式：擋跨站表單與畸形輸入
    st, _, _ = cl.req("POST", "/api/settings", raw_body=b"a=1",
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    check("POST 不是 application/json → 415（跨站 <form> 送不出 JSON）", st == 415)
    st, _, _ = cl.req("POST", "/api/settings", raw_body=b"x", headers={"Content-Type": "text/plain"})
    check("  text/plain 也擋", st == 415)
    st, _, text = cl.req("POST", "/api/tls/inspect", raw_body=b"{not json")
    check("壞掉的 JSON 不回堆疊", "Traceback" not in text and 'File "' not in text, text[:200])
    st, _, text = cl.req("POST", "/api/tls/inspect", {"files": [{"name": "x.pem", "data": "!!!不是base64"}]})
    check("  壞掉的上傳檔不回堆疊", st < 500 and "Traceback" not in text, (st, text[:200]))
    tls_ctx = cl.ctx
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with tls_ctx.wrap_socket(raw) as t:
            t.sendall(b"POST /api/settings HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Content-Length: 10737418240\r\nCookie: xconsole=" + cl.cookie.encode() + b"\r\n\r\n")
            line = t.recv(200).decode("latin-1").split("\r\n")[0]
    check("宣稱 10 GB 的請求 → 立刻 413，不等它送", " 413 " in line, line)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with tls_ctx.wrap_socket(raw) as t:
            t.sendall(b"POST /api/settings HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Content-Length: -1\r\nCookie: xconsole=" + cl.cookie.encode() + b"\r\n\r\n")
            try:
                line = t.recv(200).decode("latin-1").split("\r\n")[0]
            except socket.timeout:
                line = "逾時"
    check("  負數 Content-Length 不會把執行緒卡住", line.startswith("HTTP/1.1") and " 5" not in line[8:12], line)

    # ---- 4. 物件層：只能碰登錄檔裡真的有的東西
    st, _, text = cl.req("GET", "/api/node/no-such-node/state")
    check("不存在的機器 id → 404，不會拿 id 去當主機名稱連線", st == 404, (st, text[:120]))
    st, _, _ = cl.req("GET", "/api/node/127.0.0.1/state")
    check("  拿 IP 當機器 id 也是 404（不是 SSRF 跳板）", st == 404)
    st, _, text = cl.req("GET", "/api/node/n1/state")
    check("  登錄的機器但還沒信任它的代理憑證 → 不連（不退回不驗證或明文）",
          st == 502 and "信任" in text and PLAIN_TOKEN not in text, (st, text[:120]))
    st, _, text = cl.req("GET", "/api/nodes")
    check("  機器清單不回代理權杖", st == 200 and "enc:v1:" not in text and PLAIN_TOKEN not in text)
    st, _, _ = cl.req("POST", "/api/node/no-such-node/slot/brain/start", {})
    check("  對不存在的機器下指令 → 404", st == 404)
    st, _, _ = cl.req("POST", "/api/node/no-such-node/trusted-ca", {"pem": "x"})
    check("  替不存在的機器匯入 CA → 404", st == 404, st)

    # ---- 5. 靜態檔：不能走出 static/
    leaks = []
    for p in ("/../xconsole_server.py", "/..%2fxconsole_server.py", "/static/../../tls.py",
              "/%2e%2e/%2e%2e/etc/passwd", "/....//....//etc/passwd", "/.xconsole/fleet.json"):
        st, _, text = cl.req("GET", p, cookie="")
        if st == 200 and ("import " in text or "root:" in text or "nodes" in text):
            leaks.append(p)
    check("靜態檔路徑穿越讀不到原始碼、系統檔與設定檔", not leaks, leaks)

    # ---- 6. 帳號
    st, _, _ = cl.req("POST", "/api/account/password", {"old": "wrong", "new": "whatever-2026"})
    check("換密碼要先對目前的密碼", st == 403, st)
    st, _, _ = cl.req("POST", "/api/account/password", {"old": PW, "new": "short"})
    check("  新密碼太短擋下", st == 400, st)
    old = cl.cookie
    other = cl.login()[1]
    st, _, _ = cl.req("POST", "/api/account/password", {"old": PW, "new": PW + "-new"})
    check("  換密碼成功", st == 200, st)
    check("  換密碼後所有既有登入一起作廢（包含另一個瀏覽器的）",
          cl.req("GET", "/api/me", cookie=old)[0] == 401 and cl.req("GET", "/api/me", cookie=other)[0] == 401)
    st, newsid = cl.login(pw=PW + "-new")
    cl.cookie = newsid
    check("  新密碼登入", st == 200)

    # ---- 7. 回應標頭
    _, h, _ = cl.req("GET", "/", cookie="")
    csp = h.get("content-security-policy", "")
    check("CSP 有 default-src 'self'、frame-ancestors 'none'、object-src 'none'",
          all(x in csp for x in ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'")), csp)
    need = {"x-content-type-options": "nosniff", "x-frame-options": "DENY", "referrer-policy": "same-origin",
            "cross-origin-opener-policy": "same-origin"}
    miss = [k for k, v in need.items() if h.get(k) != v]
    check("安全標頭齊全", not miss and "permissions-policy" in h, miss)
    check("Server 標頭不公布版本號", h.get("server", "").split()[0] == "xconsole"
          and not any(c.isdigit() for c in h.get("server", "")), h.get("server"))
    _, h, _ = cl.req("GET", "/api/me")
    check("API 回應不快取", "no-store" in h.get("cache-control", ""))

    # ---- 8. 傳輸
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"GET /api/me HTTP/1.1\r\nHost: x\r\n\r\n")
    head = s.recv(400).decode("latin-1")
    s.close()
    check("同埠講 http 一律 308 轉 https，不回資料", " 308 " in head.split("\r\n")[0], head[:60])
    old_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    old_ctx.check_hostname = False
    old_ctx.verify_mode = ssl.CERT_NONE        # 這裡只測「舊協定連得上嗎」，不驗對方是誰
    try:
        old_ctx.minimum_version = ssl.TLSVersion.TLSv1
        old_ctx.maximum_version = ssl.TLSVersion.TLSv1_1
        old_ctx.set_ciphers("ALL:@SECLEVEL=0")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with old_ctx.wrap_socket(raw):
                took = True
    except (ssl.SSLError, OSError, ValueError):
        took = False
    check("TLS 1.1 以下連不上", not took)

    # ---- 9. 登入節流（放最後：會把這個來源擋 15 分鐘）
    codes = [cl.login(pw="wrong-guess")[0] for _ in range(6)]
    check("同一來源連錯 5 次後回 429", codes[:5] == [401] * 5 and codes[5] == 429, codes)
    check("  被擋期間連正確密碼也不放行（不洩漏猜對了沒）", cl.login(pw=PW + "-new")[0] == 429)

    # ---- 10. 稽核不留機密
    db = sqlite3.connect(os.path.join(env["HOME"], ".xconsole", "xconsole.db"))
    dump = json.dumps(db.execute("SELECT * FROM audit").fetchall(), ensure_ascii=False)
    sess = db.execute("SELECT sid_hash FROM sessions").fetchall()
    db.close()
    check("稽核紀錄沒有密碼", PW not in dump and "wrong-guess" not in dump)
    check("稽核記到被擋的登入", "auth.throttled" in dump)
    check("資料庫只存票證的雜湊，不存票證本身", newsid and all(newsid not in r[0] for r in sess)
          and all(len(r[0]) == 64 for r in sess))
    fleet = open(os.path.join(env["HOME"], ".xconsole", "fleet.json")).read()
    check("設定檔沒有明文密碼", PW not in fleet)
    toks = [n.get("token", "") for n in json.loads(fleet)["nodes"]]
    check("代理權杖加密落地（enc:v1:），明文已被遷移掉", PLAIN_TOKEN not in fleet and toks
          and all(t.startswith("enc:v1:") for t in toks), toks)
    check("設定檔與資料庫權限 0600", all(oct(os.stat(os.path.join(env["HOME"], ".xconsole", f)).st_mode & 0o777)
                                    == "0o600" for f in ("fleet.json", "xconsole.db")))


def agent_tests():
    port = free_port()
    home = os.path.join(W, "agent-home")
    os.makedirs(os.path.join(home, ".xconsole"))
    cfg = os.path.join(home, "strata.json")
    with open(cfg, "w") as fh:
        json.dump({"model": "x"}, fh)
    reg = {"machine": {"name": "sec-test", "memory_model": "discrete"},
           "slots": [{"id": "brain", "role": "主腦", "unit": "xc-sec-none.service", "kind": "strata",
                      "config": cfg, "log": os.path.join(home, "brain.log")}]}
    with open(os.path.join(home, ".xconsole", "registry.json"), "w") as fh:
        json.dump(reg, fh)
    env = {**os.environ, "HOME": home, "XCONSOLE_AGENT_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, AGENT], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if not wait_tls(port):
            check("節點代理啟動", False, proc.stdout.read1(2000) if proc.poll() is not None else "逾時")
            return
        token = open(os.path.join(home, ".xconsole", "token")).read().strip()
        ctx = ssl.create_default_context(cafile=os.path.join(home, ".xconsole", "agent-cert.pem"))
        ctx.check_hostname = False

        def call(method, path, body=None, tok=token):
            c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=30)
            h = {"Content-Type": "application/json"}
            if tok is not None:
                h["Authorization"] = f"Bearer {tok}"
            c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
            r = c.getresponse()
            t = r.read().decode("utf-8", "replace")
            c.close()
            return r.status, t

        check("代理只對外公開 /healthz", call("GET", "/healthz", tok=None)[0] == 200)
        no = [p for p in ("/state", "/registry", "/counters", "/logs/brain", "/catalog/brain")
              if call("GET", p, tok=None)[0] != 401]
        no += [p for p in ("/slot/brain/start", "/slot/brain/model", "/all/stop", "/admit/brain")
               if call("POST", p, {}, tok=None)[0] != 401]
        check("代理沒帶權杖一律 401", not no, no)
        check("代理權杖錯一個字 → 401", call("GET", "/state", tok=token[:-1] + ("x" if token[-1] != "x" else "y"))[0] == 401)
        check("代理不接受權杖放在網址", call("GET", f"/state?token={token}", tok=None)[0] == 401)
        st, t = call("POST", "/slot/brain/model", {"pack": "../../etc", "native": "/etc/passwd"})
        check("換模型不能指到任意路徑（只接受本機掃到的組合）", st == 400 and "不在" in t, (st, t[:120]))
        st, _ = call("POST", "/registry", {"slots": [{"id": "x", "log": os.path.expanduser("~/.ssh/id_rsa")}]})
        check("沒有經 API 覆寫登錄檔的入口（防把記錄檔指到私鑰再讀出）", st == 404, st)
        st, t = call("GET", "/logs/..%2f..%2fetc%2fpasswd")
        check("讀記錄不能穿越到任意檔案", st in (404, 400) and "root:" not in t, st)
        st, _ = call("GET", "/logs/nobody")
        check("  不存在的欄位 → 404", st == 404)
        mode = oct(os.stat(os.path.join(home, ".xconsole", "token")).st_mode & 0o777)
        check("代理權杖檔權限 0600", mode == "0o600", mode)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def main():
    port = free_port()
    home = os.path.join(W, "console-home")
    os.makedirs(home)
    env = {**os.environ, "HOME": home, "XCONSOLE_PORT": str(port)}
    subprocess.run([sys.executable, SERVER, "--set-credentials"], input=f"admin\n{PW}\n".encode(),
                   env=env, check=True, capture_output=True)
    # 模擬部署腳本寫進來的明文代理權杖：主控台啟動後必須轉成密文
    fp = os.path.join(home, ".xconsole", "fleet.json")
    cfg = json.load(open(fp))
    cfg["nodes"] = [{"id": "n1", "name": ".n1", "host": "127.0.0.1", "agent_port": 1, "token": PLAIN_TOKEN}]
    with open(fp, "w") as fh:
        json.dump(cfg, fh)
    proc = subprocess.Popen([sys.executable, SERVER], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if not wait_tls(port):
            check("隔離主控台啟動", False)
        else:
            console_tests(port, os.path.join(home, ".xconsole", "tls", "cert.pem"), env)
        agent_tests()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(W, ignore_errors=True)
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
