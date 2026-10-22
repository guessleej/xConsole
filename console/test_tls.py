#!/usr/bin/env python3
"""憑證模組的測試：用 openssl 現場產出客戶實際會拿來的各種檔案，逐一匯入。

跑法：python3 test_tls.py（需要 openssl 3 與 cryptography；在 .27 上跑）
這支壞掉代表管理者在網頁上換憑證時，會把壞的套上去、或把好的擋掉。
"""
import http.server
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tls as T  # noqa: E402

W = tempfile.mkdtemp(prefix="xconsole-tls-test-")
HOSTS = ["192.168.11.27", "xconsole.example.tw"]
OK = True


def sh(*args, stdin=None):
    r = subprocess.run(list(args), cwd=W, capture_output=True, input=stdin)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{r.stderr.decode()}")
    return r.stdout


def rd(name):
    with open(os.path.join(W, name), "rb") as fh:
        return fh.read()


def case(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail and not cond else ""))
    OK = OK and bool(cond)


def ext(path, text):
    with open(os.path.join(W, path), "w") as fh:
        fh.write(text)


# ------------------------------------------------------------------ 測試用 CA 與憑證

def make_pki():
    ext("ca.ext", "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n")
    ext("leaf.ext", "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
                    "extendedKeyUsage=serverAuth\nsubjectAltName=IP:192.168.11.27,DNS:xconsole.example.tw\n")
    ext("wild.ext", "basicConstraints=CA:FALSE\nsubjectAltName=DNS:*.ai.example.edu.tw\n")
    # 根 CA → 中繼 CA → 網站
    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "root.key", "-out", "root.crt",
       "-days", "3650", "-subj", "/C=TW/O=Test Root CA/CN=Test Root", "-addext", "basicConstraints=critical,CA:TRUE",
       "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "int.key", "-out", "int.csr",
       "-subj", "/C=TW/O=Test Intermediate/CN=Test Intermediate")
    sh("openssl", "x509", "-req", "-in", "int.csr", "-CA", "root.crt", "-CAkey", "root.key", "-CAcreateserial",
       "-out", "int.crt", "-days", "1825", "-extfile", "ca.ext")
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr",
       "-subj", "/C=TW/O=xCloudinfo/CN=xconsole.example.tw")
    sh("openssl", "x509", "-req", "-in", "leaf.csr", "-CA", "int.crt", "-CAkey", "int.key", "-CAcreateserial",
       "-out", "leaf.crt", "-days", "397", "-extfile", "leaf.ext")
    # 另一把不相干的私鑰（測不成對）
    sh("openssl", "genrsa", "-out", "other.key", "2048")
    # 舊式 PFX（Windows／IIS 匯出常見的 RC2-40 + 3DES）與新式 PFX（AES）
    sh("openssl", "pkcs12", "-export", "-legacy", "-certpbe", "PBE-SHA1-RC2-40", "-keypbe", "PBE-SHA1-3DES",
       "-inkey", "leaf.key", "-in", "leaf.crt", "-certfile", "int.crt", "-out", "legacy.pfx",
       "-passout", "pass:Pfx#2026")
    sh("openssl", "pkcs12", "-export", "-inkey", "leaf.key", "-in", "leaf.crt", "-certfile", "int.crt",
       "-out", "modern.pfx", "-passout", "pass:Pfx#2026")
    # P7B（廠商常寄的整條鏈，不含私鑰）
    sh("openssl", "crl2pkcs7", "-nocrl", "-certfile", "leaf.crt", "-certfile", "int.crt", "-out", "chain.p7b")
    # DER 格式的 .cer（打開是亂碼那種）
    sh("openssl", "x509", "-in", "leaf.crt", "-outform", "DER", "-out", "leaf.der.cer")
    # 加密的 PEM 私鑰
    sh("openssl", "pkey", "-in", "leaf.key", "-aes256", "-out", "leaf.enc.key", "-passout", "pass:Key#2026")
    # 過期憑證：OpenSSL 3.0 的 x509 沒有 -not_after（3.4 才有），改用 cryptography 直接簽
    make_expired()
    # 萬用字元
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "wild.key", "-out", "wild.csr",
       "-subj", "/CN=*.ai.example.edu.tw")
    sh("openssl", "x509", "-req", "-in", "wild.csr", "-CA", "int.crt", "-CAkey", "int.key", "-CAcreateserial",
       "-out", "wild.crt", "-days", "397", "-extfile", "wild.ext")
    # ECC 網站憑證
    sh("openssl", "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "ecc.key",
       "-out", "ecc.csr", "-subj", "/CN=xconsole.example.tw")
    sh("openssl", "x509", "-req", "-in", "ecc.csr", "-CA", "int.crt", "-CAkey", "int.key", "-CAcreateserial",
       "-out", "ecc.crt", "-days", "397", "-extfile", "leaf.ext")
    sh("openssl", "pkcs12", "-export", "-inkey", "ecc.key", "-in", "ecc.crt", "-certfile", "int.crt",
       "-out", "ecc.pfx", "-passout", "pass:Pfx#2026")
    # 弱金鑰 RSA 1024
    sh("openssl", "req", "-newkey", "rsa:1024", "-nodes", "-keyout", "weak.key", "-out", "weak.csr",
       "-subj", "/CN=xconsole.example.tw")
    sh("openssl", "x509", "-req", "-in", "weak.csr", "-CA", "int.crt", "-CAkey", "int.key", "-CAcreateserial",
       "-out", "weak.crt", "-days", "397", "-extfile", "leaf.ext")


def make_expired():
    import datetime as dt
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    ca_key = serialization.load_pem_private_key(rd("int.key"), None)
    ca = x509.load_pem_x509_certificate(rd("int.crt"))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "old.example.tw")]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime(2020, 1, 1)).not_valid_after(dt.datetime(2021, 1, 1))
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("192.168.11.27")), x509.DNSName("xconsole.example.tw")]),
                critical=False)
            .sign(ca_key, hashes.SHA256()))
    with open(os.path.join(W, "old.crt"), "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(W, "old.key"), "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))


def insp(files, pw="", kpw="", hosts=HOSTS, csr_key=None):
    r, b = T.inspect_upload([(n, rd(n)) for n in files], pw, kpw, hosts, csr_key)
    return r, b


# ------------------------------------------------------------------ 匯入檢查

def test_inspect():
    r, b = insp(["legacy.pfx"], "Pfx#2026")
    case("舊式 PFX（RC2-40＋3DES）解得開且可以套用", r["ok"] and b is not None, r["errors"])
    case("  舊式 PFX 自動帶出中繼憑證，鏈長 2", r.get("chainLength") == 2, r.get("chainLength"))

    r, _ = insp(["modern.pfx"], "Pfx#2026")
    case("新式 PFX（AES）可以套用", r["ok"], r["errors"])

    r, _ = insp(["legacy.pfx"])
    case("PFX 沒給密碼 → 要求輸入密碼", not r["ok"] and r["need_password"])

    r, _ = insp(["legacy.pfx"], "錯的密碼")
    case("PFX 密碼錯 → 說密碼不對", not r["ok"] and any("密碼不對" in e for e in r["errors"]), r["errors"])

    r, _ = insp(["int.crt", "leaf.key", "leaf.crt"])
    case("PEM 三檔亂序丟進去也能配對與排鏈", r["ok"] and r["chainLength"] == 2, r["errors"])

    r, _ = insp(["chain.p7b", "leaf.key"])
    case("P7B（整條鏈）＋私鑰可以套用", r["ok"] and r["chainLength"] == 2, r["errors"])

    r, _ = insp(["leaf.der.cer", "int.crt", "leaf.key"])
    case("DER 格式的 .cer 自動判斷", r["ok"], r["errors"])

    r, _ = insp(["leaf.crt", "int.crt", "other.key"])
    case("私鑰不成對 → 「私鑰和憑證不是同一對」",
         not r["ok"] and any("不是同一對" in e for e in r["errors"]), r["errors"])

    r, _ = insp(["leaf.crt", "int.crt", "leaf.enc.key"])
    case("加密私鑰沒給密碼 → 要求私鑰密碼", not r["ok"] and r["need_key_password"])
    r, _ = insp(["leaf.crt", "int.crt", "leaf.enc.key"], kpw="Key#2026")
    case("加密私鑰給對密碼 → 可以套用", r["ok"], r["errors"])

    r, _ = insp(["leaf.crt", "leaf.key"])
    case("沒附中繼憑證 → 可以套用但提醒手機可能連不上",
         r["ok"] and any("中繼憑證" in w for w in r["warnings"]), r["warnings"])

    r, _ = insp(["leaf.crt", "int.crt", "leaf.key"], hosts=["192.168.11.27", "other.example.tw"])
    case("網址沒被涵蓋 → 擋下並列出哪個網址",
         not r["ok"] and any("other.example.tw" in e for e in r["errors"]), r["errors"])

    r, _ = insp(["old.crt", "int.crt", "old.key"])
    case("過期憑證 → 擋下", not r["ok"] and any("過期" in e for e in r["errors"]), r["errors"])

    r, _ = insp(["wild.crt", "int.crt", "wild.key"], hosts=["gpu.ai.example.edu.tw"])
    case("萬用字元涵蓋一層", r["ok"], r["errors"])
    r, _ = insp(["wild.crt", "int.crt", "wild.key"], hosts=["a.b.ai.example.edu.tw"])
    case("萬用字元不涵蓋兩層", not r["ok"], r["errors"])

    r, _ = insp(["ecc.pfx"], "Pfx#2026")
    case("ECC 憑證的 PFX 可以套用", r["ok"] and "ECC" in r["summary"]["key"], r["errors"])

    r, _ = insp(["weak.crt", "int.crt", "weak.key"])
    case("RSA 1024 → 金鑰強度不足", not r["ok"] and any("強度不足" in e for e in r["errors"]), r["errors"])

    # 迴歸：localhost／127.0.0.1 只給本機工具用，不能拿來擋客戶的正式憑證（2026-10-22 踩到）
    r, _ = insp(["leaf.crt", "int.crt", "leaf.key"], hosts=["xconsole.example.tw", "localhost", "127.0.0.1"])
    case("涵蓋檢查不要求 localhost／127.0.0.1", r["ok"], r["errors"])
    d = T.default_hostnames()
    case("預設網址只有對外網卡的 IP，不含 Docker 與迴路位址",
         len(d) == 1 and not d[0].startswith(("127.", "172.17.")), d)

    r, _ = insp(["leaf.csr"])
    case("誤傳 CSR → 說這是申請檔不是憑證", any("CSR" in x for x in r["errors"] + r["warnings"]))


def test_csr():
    d = tempfile.mkdtemp(dir=W)
    p = T.Paths(d)
    csr_pem = T.generate_csr(p, "xconsole.example.tw", ["192.168.11.27"], org="xCloudinfo")
    ext("mine.csr", csr_pem)
    rec, key = T.pending_csr(p)
    raw = open(p.csr).read()
    case("CSR 私鑰以 enc:v1: 加密落地，檔案裡沒有明文私鑰",
         rec["key_enc"].startswith("enc:v1:") and "PRIVATE KEY" not in raw and key is not None)
    sh("openssl", "x509", "-req", "-in", "mine.csr", "-CA", "int.crt", "-CAkey", "int.key", "-CAcreateserial",
       "-out", "mine.crt", "-days", "397", "-copy_extensions", "copy")
    r, b = T.inspect_upload([("mine.crt", rd("mine.crt")), ("int.crt", rd("int.crt"))], "", "", HOSTS, key)
    case("廠商只寄回憑證 → 自動對上本系統產的 CSR", r["ok"] and b[2] is True, r["errors"])
    case("CSR 主控台私鑰權限 0600", oct(os.stat(p.master_key).st_mode & 0o777) == "0o600")


# ------------------------------------------------------------------ 熱套用、轉址、還原

class Hello(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        b = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b)


def served_fp(port):
    pem = ssl.get_server_certificate(("127.0.0.1", port), timeout=5)
    from cryptography import x509
    return T.fingerprint256(x509.load_pem_x509_certificate(pem.encode()))


def test_live():
    d = tempfile.mkdtemp(dir=W)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cm = T.CertManager(d, port)
    created = cm.ensure(["127.0.0.1", "localhost"])
    case("第一次啟動自動產暫用自簽", created and cm.current()["source"] == "selfsigned")

    srv = T.TLSServer(("127.0.0.1", port), Hello)
    srv.certs = cm
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    # 純 http 打 https 埠 → 308
    c = socket.create_connection(("127.0.0.1", port))
    c.sendall(b"POST /api/x?y=1 HTTP/1.1\r\nHost: 192.168.11.27:9400\r\n\r\n")
    resp = c.recv(500).decode()
    c.close()
    case("用 http:// 打到 https 埠 → 308 轉 https 同埠同路徑",
         "308" in resp.split("\r\n")[0] and "Location: https://192.168.11.27:9400/api/x?y=1" in resp, resp[:120])

    # 用自己的 CA 驗證連線（不關驗證）
    ctx = ssl.create_default_context(cafile=cm.p.live_cert)
    ok = urllib.request.urlopen(f"https://127.0.0.1:{port}/", context=ctx, timeout=5).read() == b"ok"
    case("用存下的對方憑證驗證連線成功（驗證沒有關）", ok)

    before = served_fp(port)
    r = cm.inspect([("legacy.pfx", rd("legacy.pfx"))], "Pfx#2026", "", HOSTS)
    ok, msg, _ = cm.apply(r["token"])
    after = served_fp(port)
    case("熱套用：不重開服務，下一條連線就是新憑證", ok and after != before
         and after == r["summary"]["fingerprint256"], msg)
    case("  套用後來源標為已匯入、HSTS 可以開", cm.current()["source"] == "imported" and cm.hsts_allowed())

    # 壞的私鑰組合：直接塞給 reload_cert 也不能把服務弄壞
    bad_ok, bad_msg = cm.reload_cert(rd("leaf.crt"), rd("other.key"), "imported")
    case("不成對的憑證與私鑰 → 測試失敗不換，服務照常", not bad_ok and served_fp(port) == after, bad_msg)

    ok, msg = cm.rollback()
    case("一鍵還原上一張", ok and served_fp(port) == before, msg)
    case("  還原回自簽後 HSTS 自動關閉、也不准再開", not cm.hsts_header() and not cm.set_hsts(True)[0])

    srv.shutdown()


def test_trust():
    r, pem = T.inspect_trusted_ca(rd("root.crt"))
    case("匯入對方 CA（每個串接對象各一份）", r["ok"] and pem.startswith(b"-----BEGIN CERTIFICATE"))


def test_notice():
    d = tempfile.mkdtemp(dir=W)
    cm = T.CertManager(d, 1)
    cm.ensure(["localhost"])
    import datetime as dt
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=825 - 10)   # 推到到期前 10 天
    n1 = cm.due_notices(soon)
    n2 = cm.due_notices(soon)
    case("到期前 10 天：30／14 天的提醒各發一次", set(n1) == {"d30", "d14"}, n1)
    case("  同一個門檻不重複發", n2 == [], n2)
    case("  卡片等級為警示", cm.current(soon)["level"] == "warn")


if __name__ == "__main__":
    try:
        make_pki()
        test_inspect()
        test_csr()
        test_live()
        test_trust()
        test_notice()
    finally:
        shutil.rmtree(W, ignore_errors=True)
    print("\n全部通過" if OK else "\n有項目失敗")
    sys.exit(0 if OK else 1)
