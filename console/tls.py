"""xConsole 憑證管理：匯入、檢查、產 CSR、熱套用、還原、到期監控、信任對方憑證。

規範來源：skill https-certificate。這支是那份規格的 Python 版（原版是 Node 的 cert-import.mjs）。

三條鐵則：
  1. 第一次開機就是 https://（沒有憑證就產暫用自簽）。
  2. 換憑證是管理者在網頁上做的事：上傳 → 檢查並講人話 → 套用 → 失敗自動還原。
  3. 連別人時驗證對方：對方是自簽，就匯入對方的憑證當信任依據，不准關掉驗證。

機密：私鑰、PFX 密碼、待配對 CSR 的私鑰都不寫進記錄；CSR 私鑰以 AES-256-GCM 加密落地（enc:v1: 前綴）。
"""
import base64
import datetime as dt
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID

TPE = dt.timezone(dt.timedelta(hours=8))
WARN_DAYS = (30, 14, 7, 1)
TOKEN_TTL = 600            # 檢查結果保留 10 分鐘，私鑰只在記憶體裡
MAX_FILE = 1024 * 1024     # 單檔 1 MB


# ------------------------------------------------------------------ 路徑

class Paths:
    def __init__(self, state_dir):
        self.root = os.path.join(state_dir, "tls")
        self.live_cert = os.path.join(self.root, "cert.pem")
        self.live_key = os.path.join(self.root, "key.pem")
        self.meta = os.path.join(self.root, "meta.json")
        self.history = os.path.join(self.root, "history")
        self.staging = os.path.join(self.root, "staging")
        self.csr = os.path.join(self.root, "csr.json")
        self.trust = os.path.join(state_dir, "trust")
        self.master_key = os.path.join(state_dir, "master.key")
        for d in (self.root, self.history, self.staging, self.trust):
            os.makedirs(d, mode=0o700, exist_ok=True)


def _write(path, data, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode())
    os.replace(tmp, path)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


# ------------------------------------------------------------------ 時間與格式

def _utc(d):
    """cryptography 41 的 not_valid_after 是不帶時區的 UTC。"""
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def valid_from(cert):
    """cryptography 42 起改用 *_utc，舊名在新版會丟棄用警告；兩版都要能跑。"""
    v = getattr(cert, "not_valid_before_utc", None)
    return v if v is not None else _utc(cert.not_valid_before)


def valid_to(cert):
    v = getattr(cert, "not_valid_after_utc", None)
    return v if v is not None else _utc(cert.not_valid_after)


def taipei(d):
    """台北時間字串本身不帶括號，外面要寫「（台北時間）」時自己加，不要包兩層。"""
    return _utc(d).astimezone(TPE).strftime("%Y/%m/%d %H:%M")


def fingerprint256(cert):
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def _name(n, oid):
    try:
        v = n.get_attributes_for_oid(oid)
        return v[0].value if v else ""
    except Exception:  # noqa: BLE001
        return ""


def sans(cert):
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    except x509.ExtensionNotFound:
        return [], []
    return ext.get_values_for_type(x509.DNSName), [str(i) for i in ext.get_values_for_type(x509.IPAddress)]


def is_self_signed(cert):
    if cert.issuer != cert.subject:
        return False
    try:
        cert.verify_directly_issued_by(cert)
        return True
    except Exception:  # noqa: BLE001
        return False


def _pub_der(key_or_cert):
    pub = key_or_cert.public_key()
    return pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _key_desc(pub):
    if isinstance(pub, rsa.RSAPublicKey):
        return "RSA", pub.key_size
    if isinstance(pub, ec.EllipticCurvePublicKey):
        return "ECC", pub.curve.key_size
    return type(pub).__name__, 0


def cert_summary(cert, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    nva = valid_to(cert)
    dns, ips = sans(cert)
    ktype, ksize = _key_desc(cert.public_key())
    return {
        "subject": _name(cert.subject, NameOID.COMMON_NAME) or cert.subject.rfc4514_string(),
        "issuer": _name(cert.issuer, NameOID.ORGANIZATION_NAME)
                  or _name(cert.issuer, NameOID.COMMON_NAME) or cert.issuer.rfc4514_string(),
        "dns": dns, "ips": ips,
        "validFromTaipei": taipei(valid_from(cert)),
        "validToTaipei": taipei(nva),
        "daysLeft": int((nva - now).total_seconds() // 86400),
        "fingerprint256": fingerprint256(cert),
        "key": f"{ktype} {ksize}",
        "selfSigned": is_self_signed(cert),
    }


# ------------------------------------------------------------------ 拆檔

def _try(fn, *a):
    try:
        return fn(*a)
    except Exception:  # noqa: BLE001
        return None


def _split_pem(text):
    blocks, cur = [], None
    for line in text.splitlines():
        if line.startswith("-----BEGIN "):
            cur = [line]
        elif cur is not None:
            cur.append(line)
            if line.startswith("-----END "):
                blocks.append("\n".join(cur) + "\n")
                cur = None
    return blocks


def classify(files, password="", key_password=""):
    """把使用者丟進來的檔案（順序不拘）拆成憑證與私鑰。

    回 (certs, keys, notes)。notes 是需要使用者補的東西或看不懂的檔案，用人話寫。
    支援：PFX／P12（含 Windows 匯出的 RC2／3DES 舊式加密）、PEM 合併或分開、
    DER 的 .cer／.crt／.key、P7B（PEM 或 DER）、加密的 PEM 私鑰。
    """
    certs, keys, notes = [], [], {"need_password": False, "need_key_password": False,
                                  "bad_password": False, "bad_key_password": False, "unknown": []}
    pw = password.encode() if password else None
    kpw = key_password.encode() if key_password else None
    for name, data in files:
        if len(data) > MAX_FILE:
            notes["unknown"].append(f"{name} 超過 1 MB，不像憑證檔")
            continue
        head = data[:200].decode("latin-1", "replace")
        if "-----BEGIN" in head or b"-----BEGIN" in data:
            text = data.decode("utf-8", "replace")
            for blk in _split_pem(text):
                if "CERTIFICATE-----" in blk and "REQUEST" not in blk:
                    c = _try(x509.load_pem_x509_certificate, blk.encode())
                    if c:
                        certs.append(c)
                elif "PKCS7-----" in blk:
                    got = _try(pkcs7.load_pem_pkcs7_certificates, blk.encode())
                    certs.extend(got or [])
                elif "PRIVATE KEY-----" in blk:
                    encrypted = "ENCRYPTED" in blk or "Proc-Type: 4,ENCRYPTED" in blk
                    if encrypted and not kpw:
                        notes["need_key_password"] = True
                        continue
                    k = _try(serialization.load_pem_private_key, blk.encode(), kpw if encrypted else None)
                    if k is None:
                        if encrypted:
                            notes["bad_key_password"] = True
                        else:
                            notes["unknown"].append(f"{name} 裡的私鑰讀不出來")
                    else:
                        keys.append(k)
                elif "CERTIFICATE REQUEST-----" in blk:
                    notes["unknown"].append(f"{name} 是 CSR（憑證申請檔），不是憑證；請上傳廠商簽回來的憑證")
            continue
        # 二進位：DER 憑證 → DER 私鑰 → DER P7B → 最後才當 PFX
        c = _try(x509.load_der_x509_certificate, data)
        if c:
            certs.append(c)
            continue
        k = _try(serialization.load_der_private_key, data, None)
        if k:
            keys.append(k)
            continue
        got = _try(pkcs7.load_der_pkcs7_certificates, data)
        if got:
            certs.extend(got)
            continue
        # PFX：沒有密碼的也試一次（有些匯出時沒設）
        res = _try(pkcs12.load_key_and_certificates, data, pw)
        if res is None and pw is None:
            notes["need_password"] = True
            continue
        if res is None:
            notes["bad_password"] = True
            continue
        k, c, extra = res
        if k:
            keys.append(k)
        if c:
            certs.append(c)
        certs.extend(extra or [])
    # 同一張憑證可能重複出現（PFX 內含、又另外上傳一次）
    seen, uniq = set(), []
    for c in certs:
        fp = fingerprint256(c)
        if fp not in seen:
            seen.add(fp)
            uniq.append(c)
    return uniq, keys, notes


def build_chain(leaf, pool):
    """從網站憑證往上找發行者，排成 網站 → 中繼 → …；回 (chain, 用不到的憑證)。"""
    chain, rest = [leaf], [c for c in pool if c is not leaf]
    cur = leaf
    while not is_self_signed(cur):
        nxt = None
        for c in rest:
            if c.subject == cur.issuer:
                try:
                    cur.verify_directly_issued_by(c)
                    nxt = c
                    break
                except Exception:  # noqa: BLE001
                    continue
        if nxt is None:
            break
        chain.append(nxt)
        rest.remove(nxt)
        cur = nxt
    return chain, rest


def _covers(names_dns, names_ip, host):
    """萬用字元只涵蓋一層：*.ai.example.edu.tw 不涵蓋 a.b.ai.example.edu.tw。"""
    try:
        ip = ipaddress.ip_address(host)
        return str(ip) in names_ip
    except ValueError:
        pass
    h = host.lower().rstrip(".")
    for n in names_dns:
        n = n.lower().rstrip(".")
        if n == h:
            return True
        if n.startswith("*.") and h.count(".") == n.count(".") and h.endswith(n[1:]):
            return True
    return False


# ------------------------------------------------------------------ 檢查

def inspect_upload(files, password="", key_password="", hostnames=(), csr_key=None, now=None):
    """匯入前逐項檢查並用人話說明。回 (結果, bundle)；bundle 只給套用用，不要序列化。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    errors, warns, passes = [], [], []
    certs, keys, notes = classify(files, password, key_password)

    if notes["need_password"]:
        errors.append("這是 PFX／P12 檔，請輸入匯出時設定的密碼")
    if notes["bad_password"]:
        errors.append("PFX 密碼不對。請確認是匯出這個檔案時設定的密碼，不是電腦或帳號的密碼")
    if notes["need_key_password"]:
        errors.append("私鑰有加密，請輸入私鑰的密碼")
    if notes["bad_key_password"]:
        errors.append("私鑰的密碼不對")
    for u in notes["unknown"]:
        warns.append(u)

    base = {"need_password": notes["need_password"] or notes["bad_password"],
            "need_key_password": notes["need_key_password"] or notes["bad_key_password"]}
    if not certs:
        if not errors:
            errors.append("沒有找到憑證。請上傳 .pfx／.p12，或 .cer／.crt／.pem 加上私鑰")
        return {**base, "ok": False, "errors": errors, "warnings": warns, "passes": passes}, None

    # 找網站憑證：跟某把私鑰成對的那張；沒附私鑰時對照系統內待配對的 CSR
    key_pool = list(keys) + ([csr_key] if csr_key is not None else [])
    leaf = key = None
    for c in certs:
        pd = _pub_der(c)
        for k in key_pool:
            if _pub_der(k) == pd:
                leaf, key = c, k
                break
        if leaf:
            break
    if leaf is None:
        # 沒成對：挑一張「不是 CA」的當網站憑證來報告其他項目
        cand = [c for c in certs if not _is_ca(c)] or certs
        leaf = cand[0]
        if keys:
            errors.append("私鑰和憑證不是同一對。請確認私鑰就是當初申請這張憑證時產生的那一把")
        elif csr_key is not None:
            errors.append("這張憑證不是用本系統產生的 CSR 申請的，請一併上傳私鑰")
        else:
            errors.append("缺少私鑰。請一併上傳 .key 檔，或改傳含私鑰的 .pfx；"
                          "若當初是在本系統產生 CSR，請先產生 CSR 再上傳廠商簽回的憑證")
    else:
        passes.append("私鑰和憑證成對" + ("（對上本系統產生的 CSR）" if key is csr_key else ""))

    chain, unused = build_chain(leaf, certs)
    if unused:
        warns.append(f"有 {len(unused)} 張憑證跟這張網站憑證無關，已略過")

    s = cert_summary(leaf, now)
    nvb, nva = valid_from(leaf), valid_to(leaf)
    if nvb > now:
        errors.append(f"憑證還沒生效，要到 {taipei(nvb)}（台北時間）才開始有效")
    if nva < now:
        errors.append(f"憑證已在 {taipei(nva)}（台北時間）過期")
    elif s["daysLeft"] <= 30:
        warns.append(f"憑證只剩 {s['daysLeft']} 天，{s['validToTaipei']}（台北時間）到期，請盡快向廠商續約")
    else:
        passes.append(f"效期內，剩 {s['daysLeft']} 天，{s['validToTaipei']}（台北時間）到期")

    if not s["dns"] and not s["ips"]:
        errors.append("憑證沒有主體別名（SAN）。現在的瀏覽器只看 SAN，不看 CN，這張會被瀏覽器拒絕")
    missing = [h for h in hostnames if h and h not in LOCAL_ONLY and not _covers(s["dns"], s["ips"], h)]
    if missing:
        errors.append("憑證沒有涵蓋這些網址：" + "、".join(missing)
                      + "。使用者用這些網址連線會看到憑證錯誤")
    elif hostnames:
        passes.append("涵蓋所有使用者會打的網址：" + "、".join(h for h in hostnames if h and h not in LOCAL_ONLY))

    if s["selfSigned"]:
        warns.append("這是自簽憑證，瀏覽器會出現安全警告；正式上線請改用憑證廠商或貴單位 CA 簽發的憑證")
    elif not is_self_signed(chain[-1]) and len(chain) == 1:
        warns.append("沒有附上中繼憑證。電腦瀏覽器通常會自己補，但手機、Java、curl 等可能連不上。"
                     "請向憑證廠商下載中繼憑證一起上傳")
    else:
        passes.append(f"憑證鏈完整（{len(chain)} 張）")

    ktype, ksize = _key_desc(leaf.public_key())
    if (ktype == "RSA" and ksize < 2048) or (ktype == "ECC" and ksize < 256):
        errors.append(f"金鑰強度不足（{ktype} {ksize}），至少要 RSA 2048 或 ECC 256")
    else:
        passes.append(f"金鑰強度足夠（{ktype} {ksize}）")
    algo = leaf.signature_hash_algorithm
    if algo is not None and algo.name in ("sha1", "md5"):
        errors.append(f"簽章演算法是 {algo.name.upper()}，已被瀏覽器淘汰")

    result = {**base, "ok": not errors and key is not None, "errors": errors,
              "warnings": warns, "passes": passes, "summary": s, "chainLength": len(chain)}
    bundle = (key, chain, key is csr_key) if result["ok"] else None
    return result, bundle


def _is_ca(cert):
    try:
        return cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value.ca
    except x509.ExtensionNotFound:
        return False


def bundle_pem(key, chain):
    cert_pem = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert_pem, key_pem


# ------------------------------------------------------------------ 加密落地（CSR 私鑰）

def _master(paths):
    if not os.path.exists(paths.master_key):
        _write(paths.master_key, secrets.token_bytes(32))
    with open(paths.master_key, "rb") as fh:
        return fh.read()


def seal(paths, data: bytes) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_master(paths)).encrypt(nonce, data, b"xconsole")
    return "enc:v1:" + base64.b64encode(nonce + ct).decode()


def unseal(paths, token: str) -> bytes:
    if not token.startswith("enc:v1:"):
        raise ValueError("不認得的加密格式")
    raw = base64.b64decode(token[7:])
    return AESGCM(_master(paths)).decrypt(raw[:12], raw[12:], b"xconsole")


# ------------------------------------------------------------------ CSR

def _san_ext(hostnames):
    items = []
    for h in hostnames:
        h = h.strip()
        if not h:
            continue
        try:
            items.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            items.append(x509.DNSName(h))
    return x509.SubjectAlternativeName(items)


def generate_csr(paths, common_name, hostnames, org="", key_type="rsa2048"):
    """在伺服器端產私鑰與 CSR。私鑰加密存在本機，從頭到尾不離開伺服器。"""
    key = ec.generate_private_key(ec.SECP256R1()) if key_type == "ec256" else \
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, common_name), x509.NameAttribute(NameOID.COUNTRY_NAME, "TW")]
    if org:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name(attrs))
           .add_extension(_san_ext([common_name] + list(hostnames)), critical=False)
           .sign(key, hashes.SHA256()))
    key_der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    rec = {"csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode(),
           "key_enc": seal(paths, key_der), "cn": common_name,
           "hostnames": list(hostnames), "created": int(time.time()),
           "key_type": key_type}
    _write(paths.csr, json.dumps(rec, ensure_ascii=False))
    return rec["csr_pem"]


def pending_csr(paths):
    rec = _load_json(paths.csr, None)
    if not rec:
        return None, None
    try:
        key = serialization.load_der_private_key(unseal(paths, rec["key_enc"]), None)
    except Exception:  # noqa: BLE001
        return rec, None
    return rec, key


def clear_csr(paths):
    if os.path.exists(paths.csr):
        os.remove(paths.csr)


# ------------------------------------------------------------------ 暫用自簽

LOCAL_ONLY = {"localhost", "127.0.0.1"}


def default_hostnames():
    """使用者會打的網址，預設只取「對外那張網卡」的 IP（走預設路由出去的那個來源位址）。

    ⚠️ 不要把本機所有 IP 都當成預設：Docker 橋接的 172.17.0.1、127.0.1.1、機器之間的專線位址
    都不是使用者會打的，而匯入檢查要求憑證涵蓋「每一個」網址——任何一張正式憑證都不可能涵蓋
    Docker 的內部 IP，結果是客戶上傳正確的憑證也被擋下來（2026-10-22 實際踩到）。
    """
    import subprocess
    try:
        out = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True,
                             text=True, timeout=5).stdout
        m = __import__("re").search(r"\bsrc\s+(\S+)", out)
        if m:
            return [m.group(1)]
    except Exception:  # noqa: BLE001
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))        # UDP 不會真的送出封包，只是讓系統選出來源位址
        ip = s.getsockname()[0]
        s.close()
        return [ip]
    except Exception:  # noqa: BLE001
        return [socket.gethostname()]


def self_signed_names(hostnames):
    """暫用自簽另外加上 localhost 與 127.0.0.1，給本機的健康檢查與工具用；不列入匯入時的涵蓋檢查。"""
    return sorted(set(hostnames) | LOCAL_ONLY)


def generate_self_signed(hostnames, cn="xconsole", days=825):
    """產一張涵蓋「使用者會打的網址」的暫用自簽。回 (cert_pem, key_pem)。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "xCloudinfo"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(_san_ext(hostnames), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return bundle_pem(key, [cert])


# ------------------------------------------------------------------ 套用與還原（熱套用）

def make_context(cert_path, key_path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2   # 只開 1.2 與 1.3
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


class CertManager:
    """管理主控台自己這張憑證。套用不重開服務：新連線握手時就用新的。"""

    def __init__(self, state_dir, port):
        self.p = Paths(state_dir)
        self.port = port
        self.lock = threading.Lock()
        self.ctx = None
        self.tokens = {}           # 檢查通過的上傳：token → (到期, bundle, 摘要)

    # ---- 狀態
    def meta(self):
        return _load_json(self.p.meta, {"source": "selfsigned", "hsts": False, "notified": []})

    def _save_meta(self, m):
        _write(self.p.meta, json.dumps(m, ensure_ascii=False))

    def current(self, now=None):
        try:
            with open(self.p.live_cert, "rb") as fh:
                certs = x509.load_pem_x509_certificates(fh.read())
        except Exception:  # noqa: BLE001
            return None
        s = cert_summary(certs[0], now)
        m = self.meta()
        s.update({"source": m.get("source", "selfsigned"), "appliedAtTaipei": m.get("applied_at_taipei"),
                  "chainLength": len(certs), "hsts": bool(m.get("hsts"))})
        s["level"] = ("expired" if s["daysLeft"] < 0 else "urgent" if s["daysLeft"] <= 7
                      else "warn" if s["daysLeft"] <= 30 else "ok")
        return s

    def hsts_allowed(self):
        cur = self.current()
        return bool(cur and not cur["selfSigned"] and cur["source"] == "imported" and cur["daysLeft"] >= 0)

    def hsts_header(self):
        m = self.meta()
        return bool(m.get("hsts")) and self.hsts_allowed()

    def set_hsts(self, on):
        if on and not self.hsts_allowed():
            return False, "目前是暫用自簽或憑證已過期，不能開 HSTS——開了之後使用者連「略過警告」都沒有"
        m = self.meta()
        m["hsts"] = bool(on)
        self._save_meta(m)
        return True, "已開啟" if on else "已關閉"

    # ---- 啟動
    def ensure(self, hostnames):
        """沒有憑證就產暫用自簽；暫用自簽沒涵蓋目前網址時重產一張。"""
        cur = self.current()
        need = cur is None
        if cur and cur["source"] == "selfsigned":
            have = set(cur["dns"]) | set(cur["ips"])
            if any(h not in have for h in hostnames):
                need = True
        if need:
            cert_pem, key_pem = generate_self_signed(self_signed_names(hostnames))
            _write(self.p.live_cert, cert_pem)
            _write(self.p.live_key, key_pem)
            m = self.meta()
            m.update({"source": "selfsigned", "applied_at_taipei": taipei(dt.datetime.now(dt.timezone.utc)),
                      "hsts": False})
            self._save_meta(m)
        self.ctx = make_context(self.p.live_cert, self.p.live_key)
        return need

    # ---- 檢查
    def inspect(self, files, password, key_password, hostnames, now=None):
        rec, csr_key = pending_csr(self.p)
        result, bundle = inspect_upload(files, password, key_password, hostnames, csr_key, now)
        if bundle:
            token = secrets.token_urlsafe(24)
            now_t = time.time()
            self.tokens = {k: v for k, v in self.tokens.items() if v[0] > now_t}
            self.tokens[token] = (now_t + TOKEN_TTL, bundle, result["summary"])
            result["token"] = token
        return result

    # ---- 套用
    def reload_cert(self, cert_pem, key_pem, source):
        """先測試再換：載不進新的就不動；換上之後實際握手核對指紋，不對就換回舊的。"""
        with self.lock:
            st_c = os.path.join(self.p.staging, "cert.pem")
            st_k = os.path.join(self.p.staging, "key.pem")
            _write(st_c, cert_pem)
            _write(st_k, key_pem)
            try:
                new_ctx = make_context(st_c, st_k)
            except Exception as exc:  # noqa: BLE001
                return False, f"新憑證載入測試失敗，沒有換：{exc}"
            want = fingerprint256(x509.load_pem_x509_certificates(cert_pem)[0])
            backup = self._backup()
            old_ctx = self.ctx
            os.replace(st_c, self.p.live_cert)
            os.replace(st_k, self.p.live_key)
            self.ctx = new_ctx
            got = self._served_fingerprint()
            if got != want:
                # 先記下原因再還原，免得還原後的結果把原本的錯誤蓋掉
                why = f"套用後實際送出的憑證指紋不符（送出 {got[:23] if got else '無'}），已換回原本那張"
                self._restore(backup)
                self.ctx = old_ctx
                return False, why
            m = self.meta()
            m.update({"source": source, "applied_at_taipei": taipei(dt.datetime.now(dt.timezone.utc)),
                      "notified": []})
            if source != "imported":
                m["hsts"] = False
            self._save_meta(m)
            return True, "已套用，新連線立即使用新憑證，不必重開服務"

    def apply(self, token):
        ent = self.tokens.pop(token, None)
        if not ent or ent[0] < time.time():
            return False, "檢查結果已過期（10 分鐘），請重新上傳檢查", None
        _, (key, chain, from_csr), summary = ent
        cert_pem, key_pem = bundle_pem(key, chain)
        ok, msg = self.reload_cert(cert_pem, key_pem, "imported")
        if ok and from_csr:
            clear_csr(self.p)
        return ok, msg, summary

    def bootstrap(self, hostnames):
        cert_pem, key_pem = generate_self_signed(self_signed_names(hostnames))
        return self.reload_cert(cert_pem, key_pem, "selfsigned")

    def history(self):
        out = []
        for d in sorted(os.listdir(self.p.history), reverse=True):
            c = os.path.join(self.p.history, d, "cert.pem")
            m = _load_json(os.path.join(self.p.history, d, "meta.json"), {})
            try:
                with open(c, "rb") as fh:
                    s = cert_summary(x509.load_pem_x509_certificates(fh.read())[0])
                out.append({"id": d, "source": m.get("source"), "subject": s["subject"],
                            "validToTaipei": s["validToTaipei"], "fingerprint256": s["fingerprint256"]})
            except Exception:  # noqa: BLE001
                continue
        return out

    def rollback(self):
        hs = sorted(os.listdir(self.p.history), reverse=True)
        if not hs:
            return False, "沒有上一張可以還原"
        d = os.path.join(self.p.history, hs[0])
        with open(os.path.join(d, "cert.pem"), "rb") as fh:
            cert_pem = fh.read()
        with open(os.path.join(d, "key.pem"), "rb") as fh:
            key_pem = fh.read()
        src = _load_json(os.path.join(d, "meta.json"), {}).get("source", "imported")
        ok, msg = self.reload_cert(cert_pem, key_pem, src)
        if ok:
            shutil.rmtree(d, ignore_errors=True)   # 還原用掉的那份移除，現在這張已另存進歷史
            return True, "已還原上一張憑證"
        return False, msg

    # ---- 內部
    def _backup(self):
        stamp = dt.datetime.now(TPE).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        d = os.path.join(self.p.history, stamp)
        os.makedirs(d, mode=0o700)
        shutil.copy2(self.p.live_cert, os.path.join(d, "cert.pem"))
        shutil.copy2(self.p.live_key, os.path.join(d, "key.pem"))
        os.chmod(os.path.join(d, "key.pem"), 0o600)
        _write(os.path.join(d, "meta.json"), json.dumps({"source": self.meta().get("source")}))
        for old in sorted(os.listdir(self.p.history))[:-10]:      # 只留最近十張
            shutil.rmtree(os.path.join(self.p.history, old), ignore_errors=True)
        return d

    def _restore(self, d):
        shutil.copy2(os.path.join(d, "cert.pem"), self.p.live_cert)
        shutil.copy2(os.path.join(d, "key.pem"), self.p.live_key)
        os.chmod(self.p.live_key, 0o600)

    def _served_fingerprint(self):
        """連自己一次，看實際送出的是哪張。只取對方送出的憑證比對指紋，不拿來當信任依據。"""
        try:
            pem = ssl.get_server_certificate(("127.0.0.1", self.port), timeout=5)
            return fingerprint256(x509.load_pem_x509_certificate(pem.encode()))
        except Exception:  # noqa: BLE001
            return None

    # ---- 到期提醒（30／14／7／1 天各一次，過期後每天）
    def due_notices(self, now=None):
        cur = self.current(now)
        if not cur:
            return []
        m = self.meta()
        sent = set(m.get("notified", []))
        out = []
        for d in WARN_DAYS:
            if cur["daysLeft"] <= d and f"d{d}" not in sent:
                out.append(f"d{d}")
        if cur["daysLeft"] < 0:
            today = dt.datetime.now(TPE).strftime("%Y%m%d")
            if f"x{today}" not in sent:
                out.append(f"x{today}")
        if out:
            m["notified"] = sorted(sent | set(out))
            self._save_meta(m)
        return out


# ------------------------------------------------------------------ 信任對方（連各機代理用）

def inspect_trusted_ca(data):
    certs, _, notes = classify([("ca", data)])
    if not certs:
        return {"ok": False, "errors": ["沒有找到憑證"]}, None
    pem = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)
    return {"ok": True, "count": len(certs), "summary": cert_summary(certs[0])}, pem


def client_context(trust_file):
    """連對方時一律驗證：信任依據只有我們存下的那份對方憑證，不接受系統以外的任何人冒充。"""
    if not trust_file or not os.path.exists(trust_file):
        return None
    ctx = ssl.create_default_context(cafile=trust_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# ------------------------------------------------------------------ 伺服器：同埠轉址＋執行緒內握手＋熱套用

import re as _re
from http.server import ThreadingHTTPServer


class HTTPSRedirect:
    """有人用 http:// 打到 https 埠時，回 308 轉到 https://（同一個埠）。

    用 308 不用 301／302：POST 轉址後還是 POST，不會變成 GET。
    """

    @staticmethod
    def handle(sock, port):
        try:
            sock.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 8192:
                chunk = sock.recv(2048)
                if not chunk:
                    break
                data += chunk
            line, _, rest = data.partition(b"\r\n")
            parts = line.decode("latin-1", "replace").split()
            path = parts[1] if len(parts) >= 2 and parts[1].startswith("/") else "/"
            if any(ord(ch) < 0x21 or ord(ch) > 0x7e for ch in path):
                path = "/"
            host = ""
            for h in rest.split(b"\r\n"):
                if h.lower().startswith(b"host:"):
                    host = h[5:].strip().decode("latin-1", "replace")
            if not _re.fullmatch(r"[A-Za-z0-9.\-\[\]:]{1,255}", host or ""):
                host = f"localhost:{port}"
            sock.sendall((
                "HTTP/1.1 308 Permanent Redirect\r\n"
                f"Location: https://{host}{path}\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
        except Exception:  # noqa: BLE001
            pass


class TLSServer(ThreadingHTTPServer):
    """在處理執行緒裡才做 TLS 握手，並且每條連線當下才讀憑證內容。

    兩個好處：(1) 換憑證只要換掉 certs.ctx，新連線立刻用新的，不必重開服務；
    (2) 握手前先偷看第一個位元組，不是 TLS 就走 HTTPSRedirect，不會讓瀏覽器看到一堆亂碼錯誤。
    監聽 socket 不先包 TLS：包了的話握手會卡在接受連線的主執行緒，一個慢客戶端就能拖住所有人。
    """
    daemon_threads = True
    certs = None   # CertManager
    # 連線數上限：沒登入的人開一堆連線慢慢送，每條都會佔住一個執行緒；總數與同一來源各設上限，超過的直接關
    MAX_CONNS, MAX_PER_IP = 512, 64
    _conns, _per_ip, _conn_lock = 0, {}, threading.Lock()

    def process_request(self, request, client_address):
        ip = client_address[0] if client_address else ""
        with self._conn_lock:
            if TLSServer._conns >= self.MAX_CONNS or self._per_ip.get(ip, 0) >= self.MAX_PER_IP:
                self.shutdown_request(request)
                return
            TLSServer._conns += 1
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1

        def run():
            try:
                self.process_request_thread(request, client_address)
            finally:
                with self._conn_lock:
                    TLSServer._conns -= 1
                    self._per_ip[ip] -= 1
                    if not self._per_ip[ip]:
                        del self._per_ip[ip]
        threading.Thread(target=run, daemon=True).start()

    def finish_request(self, request, client_address):
        try:
            request.settimeout(15)
            first = request.recv(1, socket.MSG_PEEK)
        except Exception:  # noqa: BLE001
            return
        if not first:
            return
        if first[0] != 0x16:                    # 0x16 = TLS 握手的第一個位元組
            HTTPSRedirect.handle(request, self.server_address[1])
            return
        try:
            tls = self.certs.ctx.wrap_socket(request, server_side=True)
        except Exception:  # noqa: BLE001
            return
        try:
            tls.settimeout(120)
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except Exception:  # noqa: BLE001
                pass
