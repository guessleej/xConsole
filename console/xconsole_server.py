#!/usr/bin/env python3
"""xConsole 主控台 — 彙整各機節點代理，對人提供一個畫面。

機密（照 secret-handling）：
  各機代理權杖存在 ~/.xconsole/fleet.json（0600），不走環境變數、不進網址；
  瀏覽器只拿 HMAC 簽章的 session cookie，看不到任何代理權杖；
  寫稽核前一律經過 store.redact()。
憑證（照 https-certificate）：
  第一次啟動自動產暫用自簽；管理者在網頁上匯入正式憑證、熱套用、失敗自動還原；
  連各機代理時用部署時取回的對方憑證驗證，不關驗證。
服務使用報告（照 service-usage-report）：
  GET /api/admin/usage-report?days=30，六塊數字全由稽核與取樣算出。
客戶品牌：
  賣給客戶時，管理者在「設定 → 品牌與外觀」換單位名稱、系統名稱與 Logo；
  頁尾的「Powered by xCloudinfo 云碩科技」寫死在頁面裡，不開放關掉。
"""
import base64
import datetime as dt
import hashlib
import http.client
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recommend  # noqa: E402
import report  # noqa: E402
import tls  # noqa: E402
from store import Store  # noqa: E402

# Windows 以 SYSTEM 帳號開機自啟時沒有一般的家目錄：安裝程式用 --home 指到 C:\ProgramData\xConsole
HOME = (sys.argv[sys.argv.index("--home") + 1] if "--home" in sys.argv[:-1] else None) \
    or os.environ.get("XCONSOLE_HOME") or os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".xconsole")
FLEET = os.path.join(STATE_DIR, "fleet.json")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = int(os.environ.get("XCONSOLE_PORT", "9400"))
SESSION_HOURS = 12
SAMPLE_EVERY = 300
MAX_BODY = 6 * 1024 * 1024
MAX_ANON_BODY = 64 * 1024        # 登入與加入機器不需登入：本文只要幾 KB
HEADER_DEADLINE = 15            # 請求列＋標頭（POST 連同本文）要在幾秒內送完
NODE_REPLY_MAX = 32 * 1024 * 1024
NODE_DEADLINE = 300             # 跟代理的一次呼叫，連同讀完回應的總時限（秒）
PBKDF2_ROUNDS = 200_000
# 登入節流：伺服器是多執行緒的，只在每次失敗後睡一秒擋不住並行猜測。
# 同一來源 15 分鐘內錯 5 次就先擋；全部來源加起來錯 50 次也擋（分散猜測）。
# 不鎖帳號：鎖帳號等於讓內網任何人都能把管理者鎖在門外。
LOGIN_WINDOW = 900
LOGIN_MAX_PER_IP = 5
_LOGIN_LOCK = threading.Lock()
_THROTTLE_LOGGED = {}

os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
STORE = Store(os.path.join(STATE_DIR, "xconsole.db"))
CERTS = tls.CertManager(STATE_DIR, PORT)
CATALOG = recommend.Catalog(os.path.join(STATE_DIR, "catalog.json"),
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog_default.json"))
SD_CATALOG = recommend.Catalog(os.path.join(STATE_DIR, "sd_catalog.json"),
                               os.path.join(os.path.dirname(os.path.abspath(__file__)), "sd_catalog_default.json"),
                               keys=("title", "kind", "desc", "tags", "vendor", "license", "parts", "defaults"))


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def save_json(path, obj, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 設定

_FLEET_LOCK = threading.Lock()


def fleet():
    cfg = load_json(FLEET)
    if cfg is None:
        cfg = {"nodes": [], "auth": {}}
        save_json(FLEET, cfg)
    cfg.setdefault("auth", {})
    cfg.setdefault("nodes", [])
    cfg.setdefault("tls", {})
    cfg.setdefault("report", {})
    return cfg


# ---------------------------------------------------------------- 客戶品牌

BRAND_DIR = os.path.join(STATE_DIR, "branding")
BRAND_DEFAULT = {"org": "云碩科技", "product": "xConsole 模型主控台"}
BRAND_NAME_RX = re.compile(r"[^\x00-\x1f\x7f<>]{1,40}")
LOGO_MAX = 1024 * 1024
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
# 換 Logo 是「寫檔＋刪掉別種格式的舊檔＋改 fleet.json」三步，兩個請求交錯會刪到對方剛寫的檔：整段鎖住
_BRAND_LOCK = threading.Lock()


def _png_info(data):
    """逐個區塊檢查長度與 CRC：第一塊必須是 IHDR、中間要有 IDAT、要以 IEND 收尾。"""
    i, first, idat, size = 8, True, False, None
    while i + 12 <= len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        if n > len(data) or i + 12 + n > len(data):
            return None
        if zlib.crc32(data[i + 4:i + 8 + n]) & 0xFFFFFFFF != struct.unpack(">I", data[i + 8 + n:i + 12 + n])[0]:
            return None
        if first:
            if kind != b"IHDR" or n != 13:
                return None
            size = struct.unpack(">II", data[i + 8:i + 16])
            first = False
        idat = idat or kind == b"IDAT"
        i += 12 + n
        if kind == b"IEND":
            return ("png", *size) if idat else None
    return None


JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_info(data):
    """逐段走到 SOS：尺寸（SOF）要在影像資料之前出現，檔尾要是 EOI。"""
    i, size = 2, None
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:                       # 填充位元組
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        n = struct.unpack(">H", data[i + 2:i + 4])[0]
        if n < 2 or i + 2 + n > len(data):
            return None
        if marker in JPEG_SOF:
            if n < 7:
                return None
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            size = (w, h)
        if marker == 0xDA:                       # 之後是壓縮的影像資料，一路到 EOI
            ok = size and len(data) > i + 2 + n + 2 and data.rstrip(b"\x00").endswith(b"\xff\xd9")
            return ("jpg", *size) if ok else None
        i += 2 + n
    return None


def _webp_info(data):
    """RIFF 宣告的大小要跟實際一樣，每個區塊都不能超出檔案，而且一定要有影像區塊。"""
    if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
        return None
    i, size, image = 12, None, False
    while i + 8 <= len(data):
        kind, n = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        body = data[i + 8:i + 8 + n]
        if len(body) != n:
            return None
        if kind == b"VP8X" and n >= 10:
            size = (1 + int.from_bytes(body[4:7], "little"), 1 + int.from_bytes(body[7:10], "little"))
        elif kind == b"VP8 ":
            if n < 10 or body[3:6] != b"\x9d\x01\x2a":
                return None
            w, h = struct.unpack("<HH", body[6:10])
            size, image = size or (w & 0x3FFF, h & 0x3FFF), True
        elif kind == b"VP8L":
            if n < 5 or body[0] != 0x2F:
                return None
            bits = int.from_bytes(body[1:5], "little")
            size, image = size or ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1), True
        elif kind == b"ANMF":
            image = True
        i += 8 + n + (n & 1)                     # 區塊長度是奇數時後面補一個位元組
    return ("webp", *size) if image and size and i == len(data) else None


def image_info(data):
    """只認 PNG／JPEG／WebP，回 (副檔名, 寬, 高)，認不出來或結構不完整都回 None。
    看內容不看副檔名；不只看檔頭，整個檔案的結構都要對得上（只偽造檔頭的殘缺檔，瀏覽器會顯示破圖）。
    不收 SVG：SVG 能夾帶程式碼，被當成 Logo 顯示就是一個 XSS 入口。"""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return _png_info(data)
        if data[:3] == b"\xff\xd8\xff":
            return _jpeg_info(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 20:
            return _webp_info(data)
    except (struct.error, IndexError):
        return None
    return None


def branding():
    """畫面要的品牌資料：沒設定過就回預設（custom=false，前端照原樣不動）。"""
    b = fleet().get("branding") or {}
    out = {"custom": bool(b.get("org") or b.get("product") or b.get("logo")),
           "org": b.get("org") or BRAND_DEFAULT["org"],
           "product": b.get("product") or BRAND_DEFAULT["product"], "logo": None}
    lg = b.get("logo")
    if lg and os.path.isfile(os.path.join(BRAND_DIR, lg.get("file", ""))):
        out["logo"] = {"url": f"/branding/logo?v={lg['sha'][:12]}", "width": lg["w"], "height": lg["h"]}
    return out


def save_logo(data):
    """驗過的圖檔寫進 ~/.xconsole/branding/，舊的 Logo 一併刪掉。回寫進 fleet.json 的那筆資料。
    呼叫端要持有 _BRAND_LOCK，連同 fleet.json 的更新一起做完才放開。"""
    ext, w, h = image_info(data)
    os.makedirs(BRAND_DIR, mode=0o700, exist_ok=True)
    name = f"logo.{ext}"
    fd, tmp = tempfile.mkstemp(dir=BRAND_DIR, prefix=".upload-")      # 每次不同的暫存檔名，0600
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(BRAND_DIR, name))
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise
    for other in LOGO_TYPES:
        if other != ext:
            try:
                os.remove(os.path.join(BRAND_DIR, f"logo.{other}"))
            except FileNotFoundError:
                pass
    return {"file": name, "type": LOGO_TYPES[ext], "w": w, "h": h,
            "sha": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def node_token(node):
    """代理權杖加密落地（enc:v1:，AES-256-GCM，主金鑰在 ~/.xconsole/master.key）。
    部署腳本寫進來的是明文，主控台啟動時由 migrate_tokens() 轉成密文。"""
    t = node.get("token") or ""
    return tls.unseal(CERTS.p, t).decode() if t.startswith("enc:v1:") else t


def migrate_tokens():
    """一次性遷移：fleet.json 裡還是明文的代理權杖一律換成密文。回傳轉了幾個。"""
    n = 0

    def fn(cfg):
        nonlocal n
        for node in cfg["nodes"]:
            t = node.get("token") or ""
            if t and not t.startswith("enc:v1:"):
                node["token"] = tls.seal(CERTS.p, t.encode())
                n += 1
    update_fleet(fn)
    return n


def update_fleet(fn):
    with _FLEET_LOCK:
        cfg = fleet()
        fn(cfg)
        save_json(FLEET, cfg)
        return cfg


def hostnames():
    """使用者會打的網址。沒設定就只用對外那張網卡的 IP（見 tls.default_hostnames 的說明）。"""
    h = fleet()["tls"].get("hostnames")
    return h if h else tls.default_hostnames()


# ---------------------------------------------------------------- 帳號

def _hash(password, salt):
    return base64.b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)).decode()


def set_credentials(username, password):
    salt = secrets.token_bytes(16)

    def fn(cfg):
        cfg["auth"] = {"username": username, "salt": base64.b64encode(salt).decode(),
                       "hash": _hash(password, salt), "algo": "pbkdf2-sha256-200k",
                       "changed_at": int(time.time())}
        cfg.pop("session_secret", None)                    # 舊版無狀態票證用的，已不需要
    update_fleet(fn)
    STORE.session_end_all()                                  # 換密碼讓既有登入全部失效


def check_credentials(username, password):
    a = fleet().get("auth") or {}
    if not a.get("hash"):
        return False
    want_user = a.get("username", "admin")
    # 帳號與密碼都算一遍再比，免得帳號錯時回得特別快，被拿來猜帳號
    user_ok = hmac.compare_digest(username.encode(), want_user.encode())
    pw_ok = hmac.compare_digest(_hash(password, base64.b64decode(a["salt"])), a["hash"])
    return user_ok and pw_ok


def session_user(sid):
    """票證記在伺服器端：登出、換密碼、到期都會讓它失效。
    （以前是無狀態的簽章票證，登出只是清掉瀏覽器 cookie，被偷走的票證 12 小時內照樣能用。）"""
    return STORE.session_user(sid)


# ---------------------------------------------------------------- API 串接：從主控台經網路打一次

def network_test(node_id, slot_id, key=""):
    """用 IDE 會打的那個網址，從主控台這台經網路實際連一次。
    代理在本機測過只證明服務活著；這一步證明別台電腦也連得到、金鑰保護真的有效。
    key 是管理者這次貼進來的，只用這一次，不存、不寫稽核。"""
    code, info = call_node(node_id, f"/apiinfo/{slot_id}", timeout=20)
    if code != 200:
        return {"ok": False, "url": None, "detail": info.get("error") or f"讀不到設定（{code}）"}
    base = str(info.get("public_url") or "").rstrip("/")
    # 網址是代理回報的：代理被接管時可能塞內網位址或雲端 metadata，主控台會帶著管理者貼的金鑰去打。
    # 只准打「這台機器自己」：網址要是 http(s)，主機解析出來的位址要全部等於這台的位址（資安審查）
    node = next((x for x in fleet()["nodes"] if x["id"] == node_id), None)
    u = urlparse(base)
    if u.scheme not in ("http", "https") or not u.hostname or not node:
        return {"ok": False, "url": base, "detail": "對外網址格式不對，主控台不代為實測"}
    try:
        mine = {i[4][0] for i in socket.getaddrinfo(node["host"], None)}
        theirs = {i[4][0] for i in socket.getaddrinfo(u.hostname, None)}
    except OSError:
        mine, theirs = set(), set()
    if not theirs or not theirs <= mine:
        return {"ok": False, "url": base, "detail": f"對外網址的主機不是這台機器（{node['host']}），主控台不代為實測，免得把金鑰送到別處。"
                                                     "請用 IDE 所在的電腦直接測"}
    # 直接連剛剛檢查過的那個 IP，原本的主機名稱只放在 Host 標頭與 TLS 的 SNI：不讓 DNS 在檢查和連線之間換位址（rebinding）
    ip0 = sorted(theirs)[0]
    port0 = u.port or (443 if u.scheme == "https" else 80)
    path0 = (u.path or "").rstrip("/")
    ctx = None
    if u.scheme == "https":
        ctx = ssl.create_default_context()                                  # 驗證開著，不准略過
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2                        # 不接受 TLS 1.0／1.1
    t0 = time.time()

    def call(method, path, with_key):
        h = {"Content-Type": "application/json", "Host": u.netloc.rsplit("@", 1)[-1],
             **({"Authorization": f"Bearer {key}"} if with_key else {})}
        try:
            raw = socket.create_connection((ip0, port0), timeout=10)
            if ctx:
                raw = ctx.wrap_socket(raw, server_hostname=u.hostname)
            conn = http.client.HTTPConnection(u.hostname, port0, timeout=10)
            conn.sock = raw                                # 不跟轉址：http.client 本來就不跟
            conn.request(method, path0 + path, body=b"{}" if method == "POST" else None, headers=h)
            r = conn.getresponse()
            body = r.read(4 * 1024 * 1024)
            conn.close()
            if r.status >= 400:
                return r.status, None
            try:
                return r.status, json.loads(body or b"{}")
            except ValueError:
                return r.status, None
        except Exception as exc:  # noqa: BLE001
            return 0, type(exc).__name__

    st, data = call("GET", "/models", bool(key))
    ms = int((time.time() - t0) * 1000)
    ids = [m.get("id") for m in (data or {}).get("data") or []] if isinstance(data, dict) else []
    if 300 <= st < 400:
        return {"ok": False, "url": base, "ms": ms,
                "detail": f"{base} 回 {st} 轉址；為了不把金鑰送到別處，實測不跟轉址。請把對外網址改成實際提供 API 的位址"}
    if st == 0:
        return {"ok": False, "url": base, "ms": ms,
                "detail": f"從主控台連不到 {base}（{data}）。IDE 所在的電腦多半也連不到：查防火牆、對外網址與服務綁定的位址"}
    if info["api_key_set"]:
        # 驗金鑰一律打需要金鑰的對話端點（送空內容）：llama.cpp 的 /v1/models 本來就公開，拿它驗會誤判
        auth, _ = call("POST", "/chat/completions", bool(key))
        if not key:
            return {"ok": auth == 401, "url": base, "ms": ms, "partial": True,
                    "detail": "連得到，沒帶金鑰回 401（金鑰保護有效）。要連對話一起驗，請在下面貼上金鑰再測一次"
                    if auth == 401 else f"設了金鑰，但沒帶金鑰打對話端點回 {auth}，沒有被擋"}
        if auth == 401:
            return {"ok": False, "url": base, "ms": ms, "detail": "回 401，金鑰不對"}
    if st == 200 and info.get("model_name") in ids:
        return {"ok": True, "url": base, "ms": ms, "detail": f"從主控台連得到，模型清單有 {info['model_name']}"}
    return {"ok": False, "url": base, "ms": ms,
            "detail": f"回 {st}" + ("，金鑰不對" if st == 401 else "，模型清單裡沒有這個欄位的模型名稱")}


# ---------------------------------------------------------------- 代理呼叫

def trust_file(node_id):
    # 路徑一律確認落在 trust 目錄內：node_id 雖然已比對過機器名單，這裡再擋一次 ../ 之類的寫法
    base = os.path.realpath(CERTS.p.trust)
    full = os.path.realpath(os.path.join(base, f"{node_id}.pem"))
    if not full.startswith(base + os.sep):
        raise ValueError("機器 id 不合法")
    return full


def _read_capped(resp, deadline):
    """讀代理的回應：有大小上限與總時限。被接管的代理回幾 GB、或每 29 秒吐一個位元組，都不能拖垮主控台。"""
    chunks, total = [], 0
    read = getattr(resp, "read1", None) or resp.read          # read1：一次 recv 就回來，不會為了湊滿 64 KB 一直等
    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    while True:
        left = deadline - time.time()
        if left <= 0:
            raise TimeoutError("代理回應太慢")
        if sock is not None:
            try:
                sock.settimeout(max(0.1, min(left, 60)))      # 每次等待也不能超過剩下的時間
            except OSError:
                break                                         # 回應讀完時 http.client 已經把連線關了
        b = read(65536)
        if not b:
            break
        total += len(b)
        if total > NODE_REPLY_MAX:
            raise ValueError("代理回應太大")
        chunks.append(b)
    return json.loads(b"".join(chunks) or b"{}")


def agent_open(req, ctx, timeout):
    """連各機代理：一律不走代理伺服器。代理都在內網；macOS 的 urllib 會讀系統的代理設定，
    單機版連自己的 127.0.0.1 也會被送去代理而連不上。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    return op.open(req, timeout=timeout)


def call_node(node_id, path, method="GET", body=None, timeout=40):
    cfg = fleet()
    node = next((n for n in cfg["nodes"] if n["id"] == node_id), None)
    if not node:
        return 404, {"error": f"沒有這台機器：{node_id}"}
    ctx = tls.client_context(trust_file(node_id))
    if ctx is None:
        # 沒有對方憑證就不連：不准退回不驗證或明文 http
        return 502, {"error": f"還沒信任 {node['name']} 的代理憑證，請重新執行 deploy_console.sh "
                              f"或在「設定 → 機器」匯入該機憑證", "offline": True, "untrusted": True}
    h = node["host"]
    url = f"https://{'[' + h + ']' if ':' in h and not h.startswith('[') else h}:{node.get('agent_port', 9401)}{path}"
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {node_token(node)}", "Content-Type": "application/json"})
    deadline = time.time() + timeout + 60
    try:
        # 單次等待用呼叫端給的時限（換模型要等好幾分鐘才回），讀完整個回應另有總時限
        with agent_open(req, ctx, timeout) as resp:
            return resp.status, _read_capped(resp, deadline)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _read_capped(exc, deadline)
        except Exception:  # noqa: BLE001
            return exc.code, {"error": f"代理回 {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate verify failed" in msg:
            return 502, {"error": f"{node['name']} 送出的憑證跟當初信任的不同，已拒絕連線（可能是代理重裝，"
                                  f"或有人冒充）。確認後請重新執行 deploy_console.sh", "offline": True}
        return 502, {"error": f"連不上 {node['name']} 的代理：{msg}", "offline": True}


def fleet_state():
    nodes = []
    for n in fleet()["nodes"]:
        code, body = call_node(n["id"], "/state", timeout=30)
        nodes.append({"id": n["id"], "name": n["name"], "host": n["host"],
                      "group": n.get("group", "未分類"), "online": code == 200,
                      "error": None if code == 200 else (body.get("error") or f"代理回 {code}"),
                      "state": body if code == 200 else None,
                      # 「流暢執行 XB 以內」與分層長條：跟推薦清單同一條公式，畫面不自己算
                      "capacity": recommend.capacity(body) if code == 200 and body.get("pools") else None})
    return {"ts": time.time(), "nodes": nodes}


# ---------------------------------------------------------------- 背景取樣

def sampler():
    """每 5 分鐘記一筆各機狀態，服務使用報告從這裡算。另外順便檢查憑證到期提醒。"""
    last_prune = 0
    while True:
        try:
            for n in fleet()["nodes"]:
                code, st = call_node(n["id"], "/state", timeout=30)
                c_code, ctr = call_node(n["id"], "/counters", timeout=30) if code == 200 else (0, {})
                STORE.sample(n["id"], code == 200, st if code == 200 else None,
                             (ctr or {}).get("slots") if c_code == 200 else None)
            for tag in CERTS.due_notices():
                cur = CERTS.current() or {}
                STORE.audit("系統", "", "tls.expiry_notice", cur.get("subject", ""), True,
                            {"threshold": tag, "daysLeft": cur.get("daysLeft"),
                             "validToTaipei": cur.get("validToTaipei")})
            if time.time() - last_prune > 86400:
                STORE.prune()
                last_prune = time.time()
        except Exception as exc:  # noqa: BLE001
            print(f"取樣失敗：{exc}", flush=True)
        time.sleep(SAMPLE_EVERY)



# ---------------------------------------------------------------- 加一台機器（網頁上做完）
# 管理者在網頁按「加一台機器」→ 主控台產一次性加入碼與一行安裝指令 → 在那台貼上執行 →
# 腳本裝好代理、把代理的權杖與憑證送回來 → 主控台用那張憑證與權杖實際連一次成功才納管。
# 安裝指令釘住主控台憑證的公鑰（curl --pinnedpubkey），自簽憑證也不用關驗證；加入碼 30 分鐘、只能用一次。
JOIN_TTL = 1800
JOIN_FAIL_MAX = 10
_JOINS = {}                      # 加入碼雜湊 → 狀態（只放記憶體：主控台重啟就作廢，重新產一個即可）
_JOIN_FAILS = {}
_JOIN_LOCK = threading.Lock()
NODE_ID_RX = re.compile(r"[A-Za-z0-9][\w.-]{0,39}")
HOST_RX = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
                     r"|\[[0-9A-Fa-f:]+\])(?::\d{1,5})?")
AGENT_UNIT = """[Unit]
Description=xConsole 節點代理（模型欄位、准入控制、啟停）
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/xconsole_agent.py
Restart=always
RestartSec=5
MemoryMax=256M
CPUQuota=25%

[Install]
WantedBy=default.target
"""


def valid_host(h):
    """機器位址：IP，或主機名稱（不含埠）。"""
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*", h))


def agent_source():
    """安裝腳本要下載的代理程式：部署時放在主控台旁的 agent/，開發時在 repo 的 agent/，單機版在家目錄。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "agent", "xconsole_agent.py"), os.path.join(here, "..", "agent", "xconsole_agent.py"),
              os.path.join(HOME, "xconsole_agent.py")):
        if os.path.isfile(p):
            return p
    return None


def console_pin():
    """主控台目前憑證的公鑰雜湊（curl --pinnedpubkey 的格式）。換憑證後產的新指令會跟著換。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    with open(CERTS.p.live_cert, "rb") as fh:
        cert = x509.load_pem_x509_certificates(fh.read())[0]
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256//" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def _join_key(code):
    return hashlib.sha256(code.encode()).hexdigest()


def join_lookup(code, reserve=False):
    """回這個加入碼的狀態；過期或已用過回 None。reserve=True 時同時鎖住，驗證完成前別的請求拿不到
    （兩台同時拿同一個指令加入，不能兩台都成功）。驗證失敗要呼叫 join_release() 放回去讓它重試。"""
    with _JOIN_LOCK:
        now = time.time()
        for k in [k for k, v in _JOINS.items() if v["exp"] < now - 3600]:
            _JOINS.pop(k)
        j = _JOINS.get(_join_key(str(code or "")))
        if not j or j["exp"] < now or j.get("node_id") or (reserve and j.get("busy")):
            return None
        if reserve:
            j["busy"] = True
            j["busy_by"] = threading.get_ident()
        return j


def join_release(j):
    with _JOIN_LOCK:
        j["busy"] = False


LAUNCHD_LABEL = "tw.xcloudinfo.xconsole-agent"


def install_script(code, j):
    """Linux 與 macOS 共用的安裝腳本：Linux 建 systemd 使用者服務，macOS 建 launchd 的 LaunchAgent。"""
    url = f"https://{j['console']}"
    return f"""#!/usr/bin/env bash
# xConsole 節點代理安裝（Linux／macOS）：由主控台 {url} 產生。加入碼 30 分鐘內有效、只能用一次。
# 每一段下載都釘住主控台憑證的公鑰，不是關掉驗證。
set -euo pipefail
C='{url}'
PIN='{j["pin"]}'
CODE='{code}'
OSN=$(uname -s)
get() {{ curl -fsSk --pinnedpubkey "$PIN" "$C/install/$CODE/$1"; }}
echo "== 1/4 檢查這台 =="
[ "$(id -u)" = 0 ] && {{ echo "請用一般帳號執行，不要用 root 或 sudo"; exit 1; }}
if [ "$OSN" = Darwin ]; then
  python3 -c 'import ssl' 2>/dev/null || {{ echo "這台沒有 python3。請先執行 xcode-select --install 安裝命令列工具，再貼一次這行指令"; exit 1; }}
else
  for c in python3 openssl curl; do command -v $c >/dev/null || {{ echo "這台沒有 ${{c}}，請先安裝（Ubuntu：sudo apt install ${{c}}）"; exit 1; }}; done
  systemctl --user show-environment >/dev/null 2>&1 || {{ echo "這個帳號沒有 systemd 使用者服務，請用一般登入的帳號（SSH 或桌面登入）執行"; exit 1; }}
fi
PY=$(command -v python3)
mkdir -p -m 700 "$HOME/.xconsole"
if [ "$OSN" = Darwin ]; then
  # 代理用 cryptography 產自己的憑證：macOS 內建的 LibreSSL 版本參差，舊版的 openssl req 不支援 -addext
  echo "準備代理用的 Python 環境（cryptography）…"
  if "$PY" -m venv "$HOME/.xconsole/venv" >/dev/null 2>&1 \
     && "$HOME/.xconsole/venv/bin/python" -m pip install -q --disable-pip-version-check 'cryptography>=42' >/dev/null 2>&1; then
    PY="$HOME/.xconsole/venv/bin/python"
  elif ! openssl req -help 2>&1 | grep -q addext; then
    echo "裝不了 cryptography（這台要能連 pypi.org），系統的 openssl 也太舊、產不了代理憑證。請先讓這台能連外再貼一次"; exit 1
  fi
fi
echo "== 2/4 下載節點代理 =="
get xconsole_agent.py > "$HOME/xconsole_agent.py.new"
"$PY" -m py_compile "$HOME/xconsole_agent.py.new"
mv "$HOME/xconsole_agent.py.new" "$HOME/xconsole_agent.py"
echo "== 3/4 建立服務並啟動 =="
LINGER=yes
if [ "$OSN" = Darwin ]; then
  # launchd：登入後自動啟動、掛了自動重開。機器沒有人登入也要跑的話，到「系統設定 → 使用者與群組」開自動登入
  PL="$HOME/Library/LaunchAgents/{LAUNCHD_LABEL}.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$HOME/xconsole_agent.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>$HOME/.xconsole/agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/.xconsole/agent.log</string>
</dict></plist>
PLIST
  # 有人在桌面登入就放 gui 網域；只有 SSH、沒有桌面登入的 Mac 沒有 gui 網域，改放 user 網域
  LDOM="gui/$(id -u)"; launchctl print "$LDOM" >/dev/null 2>&1 || LDOM="user/$(id -u)"
  launchctl bootout "$LDOM/{LAUNCHD_LABEL}" 2>/dev/null || true
  launchctl bootstrap "$LDOM" "$PL"
  launchctl kickstart -k "$LDOM/{LAUNCHD_LABEL}"
  LOGHINT="tail -n 50 ~/.xconsole/agent.log；launchd 的狀態：launchctl print $LDOM/{LAUNCHD_LABEL}"
else
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/xconsole-agent.service" <<'UNIT'
{AGENT_UNIT}UNIT
  systemctl --user daemon-reload
  systemctl --user enable xconsole-agent.service >/dev/null
  systemctl --user restart xconsole-agent.service
  loginctl enable-linger "$USER" 2>/dev/null || LINGER=no
  LOGHINT="journalctl --user -u xconsole-agent -n 50"
fi
# 等健康檢查真的通過才往下：重新安裝時權杖與憑證檔本來就在，只等檔案出現會在代理重啟到一半時就去連
UP=no
for i in $(seq 1 30); do
  if [ -s "$HOME/.xconsole/token" ] && [ -s "$HOME/.xconsole/agent-cert.pem" ] \\
     && curl -fsS -m 3 --cacert "$HOME/.xconsole/agent-cert.pem" https://127.0.0.1:9401/healthz >/dev/null 2>&1; then UP=yes; break; fi
  sleep 1
done
[ "$UP" = yes ] || {{ echo "代理沒有起來，請看記錄：$LOGHINT"; exit 1; }}
echo "== 4/4 加入主控台 =="
RESP=$(XC_CODE="$CODE" XC_LINGER="$LINGER" "$PY" - <<'PY' | curl -sSk -m 150 --pinnedpubkey "$PIN" -H 'Content-Type: application/json' --data-binary @- -w '\\n%{{http_code}}' "$C/api/join"
{JOIN_PAYLOAD_PY}PY
) || {{ echo "連不到主控台 $C"; exit 1; }}
STATUS=$(printf '%s' "$RESP" | tail -n 1)
printf '%s' "$RESP" | sed '$d' | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("message") or d.get("error") or d)'
[ "$STATUS" = 200 ] || exit 1
[ "$LINGER" = no ] && echo "提醒：這個帳號登出後代理會跟著停。請有 sudo 權限的人執行一次：sudo loginctl enable-linger $USER"
[ "$OSN" = Darwin ] && echo "提醒：macOS 第一次會問「是否允許 python3 接受傳入連線」，請按「允許」，主控台才連得到這台"
exit 0
"""


# 送回主控台的加入資料：權杖、代理憑證、這台的 IP（UDP connect 不送封包，三個系統都能取到對外網卡的 IP）
JOIN_PAYLOAD_PY = """import json, os, socket, sys
home = os.environ.get("XCONSOLE_HOME") or os.path.expanduser("~")
ips = []
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("1.1.1.1", 80)); ips.append(s.getsockname()[0]); s.close()
except OSError:
    pass
print(json.dumps({"code": os.environ["XC_CODE"], "hostname": socket.gethostname(), "port": 9401, "ips": ips,
                  "linger": os.environ.get("XC_LINGER", "yes") == "yes",
                  "token": open(os.path.join(home, ".xconsole", "token")).read().strip(),
                  "cert": open(os.path.join(home, ".xconsole", "agent-cert.pem")).read()}))
"""


def install_script_ps1(code, j):
    """Windows 的安裝腳本（Windows PowerShell 5.1，系統內建）。
    用系統管理員身分執行：裝成開機自啟（SYSTEM 帳號，資料放 C:\\ProgramData\\xConsole，權限只給 SYSTEM 與系統管理員）並開防火牆；
    一般身分執行：裝成這個帳號登入後自啟，防火牆要另外請系統管理員開。"""
    url = f"https://{j['console']}"
    return f"""# xConsole 節點代理安裝（Windows）：由主控台 {url} 產生。加入碼 30 分鐘內有效、只能用一次。
# 不用 Stop：Windows PowerShell 5.1 會把原生程式（python、pip）寫到 stderr 的任何一行當成致命錯誤直接中斷
$ErrorActionPreference = 'Continue'
$C = '{url}'; $CODE = '{code}'; $FP = '{j["fp"]}'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# 只接受主控台那一張憑證（比對整張憑證的 SHA-256），不是關掉驗證
[Net.ServicePointManager]::ServerCertificateValidationCallback = {{ param($s, $cert)
  $h = [Security.Cryptography.SHA256]::Create().ComputeHash($cert.GetRawCertData())
  ([BitConverter]::ToString($h) -replace '-', '') -eq $FP }}
$wc = New-Object Net.WebClient
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Host '== 1/5 檢查這台 =='
$py = $null
$probe = "import sys;print(sys.executable if sys.version_info>=(3,9) else '')"
# py 啟動器優先；python 可能只是 Microsoft Store 的捷徑（執行會開商店），問不出版本就當沒有
try {{ $v = & py -3 -c $probe 2>$null; if ($v -and (Test-Path $v.Trim())) {{ $py = $v.Trim() }} }} catch {{}}
if (-not $py) {{ try {{ $v = & python -c $probe 2>$null; if ($v -and (Test-Path $v.Trim())) {{ $py = $v.Trim() }} }} catch {{}} }}
if (-not $py) {{
  $ans = Read-Host '這台沒有 Python 3.9 以上。要用 winget 安裝 Python 3.12 嗎？(Y/n)'
  if ($ans -match '^(n|N)') {{ Write-Host '請先安裝 Python 3（https://www.python.org），再貼一次這段指令'; exit 1 }}
  $scope = if ($admin) {{ 'machine' }} else {{ 'user' }}   # 一般身分裝不了全機版，改裝到自己的帳號底下
  winget install -e --id Python.Python.3.12 --scope $scope --accept-package-agreements --accept-source-agreements
  $py = (Get-ChildItem 'C:\\Program Files\\Python3*\\python.exe', "$env:LOCALAPPDATA\\Programs\\Python\\Python3*\\python.exe" -ErrorAction SilentlyContinue | Select-Object -Last 1).FullName
  if (-not $py) {{ Write-Host 'Python 裝好了但找不到，請重新開一個 PowerShell 視窗再貼一次'; exit 1 }}
}}
if ($admin) {{ $base = Join-Path $env:ProgramData 'xConsole' }} else {{ $base = $env:USERPROFILE }}
$dir = Join-Path $base 'xconsole'
New-Item -ItemType Directory -Force -Path (Join-Path $base '.xconsole') | Out-Null
New-Item -ItemType Directory -Force -Path $dir | Out-Null
if ($admin) {{
  # ProgramData 預設一般使用者讀得到：權杖與私鑰放在這裡，只留給 SYSTEM 與系統管理員
  icacls $base /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
}}
Write-Host '== 2/5 準備 Python 環境 =='
& $py -m venv (Join-Path $dir '.venv'); if ($LASTEXITCODE) {{ Write-Host '建不了 Python 虛擬環境'; exit 1 }}
$vpy = Join-Path $dir '.venv\\Scripts\\python.exe'
$vpyw = Join-Path $dir '.venv\\Scripts\\pythonw.exe'
& $vpy -m pip install --disable-pip-version-check -q 'cryptography>=42'; if ($LASTEXITCODE) {{ Write-Host '裝不了 cryptography；這台要能連 pypi.org'; exit 1 }}
Write-Host '== 3/5 下載節點代理 =='
$agent = Join-Path $dir 'xconsole_agent.py'
$wc.DownloadFile("$C/install/$CODE/xconsole_agent.py", "$agent.new")
& $vpy -m py_compile "$agent.new"; if ($LASTEXITCODE) {{ throw '代理程式檔損壞' }}
Move-Item -Force "$agent.new" $agent
Write-Host '== 4/5 建立開機自啟並啟動 =='
$task = 'xConsole Agent'
Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
$act = New-ScheduledTaskAction -Execute $vpyw -Argument ('"' + $agent + '" --home "' + $base + '"') -WorkingDirectory $dir
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
if ($admin) {{
  $trg = New-ScheduledTaskTrigger -AtStartup
  $pri = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
  New-NetFirewallRule -DisplayName 'xConsole 節點代理' -Direction Inbound -Protocol TCP -LocalPort 9401 -Action Allow -Profile Domain,Private -ErrorAction SilentlyContinue | Out-Null
  New-NetFirewallRule -DisplayName 'xConsole 模型 API' -Direction Inbound -Protocol TCP -LocalPort 8090-8289 -Action Allow -Profile Domain,Private -ErrorAction SilentlyContinue | Out-Null
}} else {{
  $trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $pri = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
}}
Register-ScheduledTask -TaskName $task -Action $act -Trigger $trg -Principal $pri -Settings $set -ErrorAction Stop | Out-Null
Start-ScheduledTask -TaskName $task
$tok = Join-Path $base '.xconsole\\token'; $crt = Join-Path $base '.xconsole\\agent-cert.pem'
$up = $false
for ($i = 0; $i -lt 40 -and -not $up; $i++) {{
  Start-Sleep 1
  if ((Test-Path $tok) -and (Test-Path $crt)) {{
    $mine = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $crt
    [Net.ServicePointManager]::ServerCertificateValidationCallback = {{ param($s, $cert) $cert.GetRawCertData().Length -eq $mine.RawData.Length -and [Convert]::ToBase64String($cert.GetRawCertData()) -eq [Convert]::ToBase64String($mine.RawData) }}
    try {{ $up = (New-Object Net.WebClient).DownloadString('https://127.0.0.1:9401/healthz') -match 'xconsole-agent' }} catch {{}}
  }}
}}
if (-not $up) {{ Write-Host '代理沒有起來。請在「工作排程器」查看 xConsole Agent 的最後執行結果'; exit 1 }}
Write-Host '== 5/5 加入主控台 =='
[Net.ServicePointManager]::ServerCertificateValidationCallback = {{ param($s, $cert)
  $h = [Security.Cryptography.SHA256]::Create().ComputeHash($cert.GetRawCertData())
  ([BitConverter]::ToString($h) -replace '-', '') -eq $FP }}
$env:XC_CODE = $CODE; $env:XC_LINGER = 'yes'; $env:XCONSOLE_HOME = $base
$payload = @'
{JOIN_PAYLOAD_PY}'@ | & $vpy -
$req = New-Object Net.WebClient; $req.Headers.Add('Content-Type', 'application/json'); $req.Encoding = [Text.Encoding]::UTF8
try {{ $resp = $req.UploadString("$C/api/join", $payload); $ok = $true }} catch [Net.WebException] {{
  $resp = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd(); $ok = $false }}
$d = $resp | ConvertFrom-Json
if ($d.message) {{ Write-Host $d.message }} elseif ($d.error) {{ Write-Host $d.error }} else {{ Write-Host $resp }}
if (-not $ok) {{ exit 1 }}
if (-not $admin) {{ Write-Host '提醒：這次不是用系統管理員身分執行，代理只在這個帳號登入後才會跑，防火牆也沒開 9401 埠。請系統管理員再執行一次這段指令' }}
"""


def console_fp():
    """主控台目前憑證整張的 SHA-256（Windows PowerShell 用來釘住憑證；它拿不到 SubjectPublicKeyInfo）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    with open(CERTS.p.live_cert, "rb") as fh:
        cert = x509.load_pem_x509_certificates(fh.read())[0]
    return cert.fingerprint(hashes.SHA256()).hex().upper()


def windows_command(url):
    """一行貼上就能跑的 Windows 指令。用 -EncodedCommand，貼進 cmd 或 PowerShell 都不會被引號、$ 弄壞；
    內容就是：釘住主控台憑證後下載 agent.ps1 執行（畫面上另外顯示原文，可以先看再貼）。"""
    head = re.sub(r"\s+", " ", f"""$FP='{{fp}}';[Net.ServicePointManager]::SecurityProtocol='Tls12';
      [Net.ServicePointManager]::ServerCertificateValidationCallback={{param($s,$c)
      ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($c.GetRawCertData())) -replace '-','') -eq $FP}};
      iex ((New-Object Net.WebClient).DownloadString('{url}'))""")
    return head


def join_node(handler, body):
    """包一層：預留加入碼之後不論怎麼結束（含沒預料到的例外），沒加入成功就把碼放回去，不會卡在「另一台正在用」。"""
    try:
        return _join_node(handler, body if isinstance(body, dict) else {})
    finally:
        with _JOIN_LOCK:
            for j in _JOINS.values():
                if j.get("busy") and j.get("busy_by") == threading.get_ident():
                    j["busy"] = False


def _join_node(handler, body):
    """安裝腳本送回來的代理資訊：驗加入碼、驗憑證、用那張憑證與權杖實際連一次成功才納管。"""
    ip = handler.ip
    with _JOIN_LOCK:
        fails = [t for t in _JOIN_FAILS.get(ip, []) if t > time.time() - 900]
        _JOIN_FAILS[ip] = fails
    if len(fails) >= JOIN_FAIL_MAX:
        return 429, {"error": "失敗次數過多，請 15 分鐘後再試"}

    def fail(msg, j=None):
        with _JOIN_LOCK:
            _JOIN_FAILS.setdefault(ip, []).append(time.time())
            if j is not None:
                j["error"] = msg
                j["busy"] = False
        STORE.audit("", ip, "node.join", "", False, {"message": msg})
        return 400, {"error": msg}

    j = join_lookup(body.get("code"), reserve=True)
    if not j:
        return fail("加入碼不對、已經用過、過期了，或另一台正在用它加入；請在主控台重新按「加一台機器」")
    token = str(body.get("token") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token):
        return fail("代理的權杖格式不對", j)
    # 只收剛好一張、不是 CA 的憑證（代理自己產的那張）。以前收整串：攻擊者把「別台代理的憑證＋自己的 CA」
    # 串在一起送，指紋算的是第一張（別台的），連線驗證卻信整串，於是能搶走已納管的那台（資安審查重現）
    r, pem = tls.inspect_trusted_ca(str(body.get("cert") or "").encode())
    if not r.get("ok") or r.get("count") != 1:
        return fail("代理的憑證要剛好一張（代理自己產生的那張）", j)
    from cryptography import x509
    try:
        bc = x509.load_pem_x509_certificate(pem).extensions.get_extension_for_class(x509.BasicConstraints).value
        if bc.ca:
            return fail("代理的憑證不能是 CA 憑證", j)
    except x509.ExtensionNotFound:
        pass
    try:
        port = int(body.get("port") or 9401)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        return fail("代理的埠不對", j)
    hostname = re.sub(r"[^\w.-]", "-", str(body.get("hostname") or ""))[:40].strip("-.") or "node"
    ips = body.get("ips") if isinstance(body.get("ips"), list) else []
    cands = [ip] + [x for x in ips[:4] if isinstance(x, str)]
    # 這個端點不需登入：只去連「加入的那台自己的位址」，不讓請求叫主控台去連任意主機。
    # 連線來源一定試；那台回報的位址排除群播、未指定、保留位址；主機名稱只有解析結果正好是上面這些位址之一才試。
    hosts = []
    for h in cands:
        try:
            a = ipaddress.ip_address(h)
        except ValueError:
            continue
        if a.is_loopback or a.is_multicast or a.is_unspecified or a.is_reserved or a.is_link_local:
            continue
        if str(a) not in hosts:
            hosts.append(str(a))
    if ip in ("127.0.0.1", "::1"):
        hosts.insert(0, "127.0.0.1")         # 主控台自己這台
    if valid_host(hostname) and hostname != "node":
        try:
            resolved = {i[4][0] for i in socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)}
        except OSError:
            resolved = set()
        if resolved and resolved <= set(hosts):
            hosts.append(hostname)           # 重新加入時 IP 可能換了：舊憑證的 SAN 還有主機名稱
    tmp = os.path.join(CERTS.p.trust, f".join-{secrets.token_hex(6)}.pem")
    with open(tmp, "wb") as fh:
        fh.write(pem)
    os.chmod(tmp, 0o600)
    good, why = None, ""
    try:
        # 跟日後 call_node 同一套驗證（含主機名稱）：這裡連得上，之後就連得上
        ctx = tls.client_context(tmp)
        for h in hosts:
            url = f"https://{'[' + h + ']' if ':' in h else h}:{port}/state"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            try:
                with agent_open(req, ctx, 10) as resp:
                    if resp.status == 200:
                        good = h
                        break
            except Exception as exc:  # noqa: BLE001
                why = str(exc)[:160]           # 只寫進操作紀錄，不回給請求方（不讓人拿加入端點分辨埠的狀態）
        if not good:
            os.remove(tmp)
            STORE.audit("", ip, "node.join_probe", "", False, {"hosts": hosts, "port": port, "why": why})
            return fail(f"主控台連不到這台的代理（試過 {', '.join(f'{h}:{port}' for h in hosts)}）。"
                        f"請確認防火牆讓主控台連得到這台的 {port} 埠，而且送來的權杖與憑證是這台代理的。", j)
        nid = None

        def put(cfg):
            nonlocal nid
            # 加入一律新增一台；只有管理者產碼時指定「重新安裝哪一台」才覆寫那一筆。
            # 以前自動以位址或憑證比對「同一台」：持碼者在 IP 被接手、或主機名稱解析被偽造時能藉此搶走既有機器（資安審查）
            same = next((n for n in cfg["nodes"] if n["id"] == j.get("replace")), None)
            if same:                                   # 指定重新安裝的那台：換新的權杖、位址與憑證，名稱群組不動
                same.update(host=good, agent_port=port, token=tls.seal(CERTS.p, token.encode()))
                nid = same["id"]
                return
            taken = {n["id"] for n in cfg["nodes"]}
            base = hostname if NODE_ID_RX.fullmatch(hostname) else "node"
            nid = base if base not in taken else next(f"{base}-{i}" for i in range(2, 999) if f"{base}-{i}" not in taken)
            cfg["nodes"].append({"id": nid, "name": j.get("name") or hostname, "host": good,
                                 "group": j.get("group") or "未分類", "agent_port": port,
                                 "token": tls.seal(CERTS.p, token.encode())})
        update_fleet(put)
        os.replace(tmp, trust_file(nid))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    with _JOIN_LOCK:
        j["node_id"] = nid
        j["linger"] = bool(body.get("linger"))
        j["error"] = None
    STORE.audit(j["by"], ip, "node.join", nid, True, {"host": good, "port": port,
                                                       "fingerprint256": r["summary"]["fingerprint256"]})
    name = next((n["name"] for n in fleet()["nodes"] if n["id"] == nid), nid)
    return 200, {"ok": True, "id": nid, "message": f"已加入主控台：{name}（{good}）。回到網頁就看得到這台。"}

# ---------------------------------------------------------------- HTTP

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "xconsole"              # 不公布版本號
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- 回應
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._sec_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, cookie=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                   {"Set-Cookie": cookie} if cookie else None)

    def _sec_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=(), payment=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; script-src 'self'; "
                         "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
                         "frame-ancestors 'none'; object-src 'none'")
        if CERTS.hsts_header():
            # 只有正式憑證且管理者開了開關才送：自簽時送 HSTS 會讓使用者連「略過警告」都沒有
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _static(self, path):
        name = os.path.basename("index.html" if path in ("/", "") else path)
        base = os.path.realpath(STATIC)
        full = os.path.realpath(os.path.join(base, name))
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return self._json(404, {"error": "找不到"})
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._sec_headers()
        self.end_headers()
        self.wfile.write(body)
        return None

    def _logo(self):
        lg = (fleet().get("branding") or {}).get("logo")
        full = os.path.join(BRAND_DIR, lg["file"]) if lg else ""
        if not lg or lg.get("type") not in LOGO_TYPES.values() or not os.path.isfile(full):
            return self._json(404, {"error": "還沒有上傳 Logo"})
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", lg["type"])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._sec_headers()
        self.end_headers()
        self.wfile.write(body)
        return None

    # ---- 工具
    def _cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "xconsole":
                return v
        return None

    @property
    def user(self):
        if not hasattr(self, "_user_cache"):
            self._user_cache = session_user(self._cookie())
        return self._user_cache

    @property
    def ip(self):
        return self.client_address[0] if self.client_address else ""

    def _need_auth(self):
        if self.user:
            return False
        self._json(401, {"error": "請先登入"})
        return True

    def _body(self, limit=None):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        n = max(n, 0)            # 負數交給 read() 會一直讀到連線結束，執行緒就卡住
        if n > (limit or MAX_BODY):
            # 不讀完就回 413 並關連線：照宣稱的長度去讀，對方宣稱 10 GB 卻不送，執行緒就被占住
            self.close_connection = True
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    def _hf_node(self, want):
        """讀 Hugging Face 要透過一台連得上網路的代理（主控台本身不一定能上網）。"""
        ids = [n["id"] for n in fleet()["nodes"]]
        return [want] if want in ids else ids

    def _catalog_save(self, body, all_rows):
        rows = CATALOG.load()
        if all_rows:
            todo = [(r, True) for r in rows]
        else:
            fields, err = recommend.check_fields(body)
            if err:
                return self._json(400, {"error": err})
            cur = next((r for r in rows if r["repo"] == fields["repo"]), None)
            todo = [(fields, cur is None or bool(body.get("refresh")) or not cur.get("quants"))]
        done, errors = [], []
        for fields, need_hf in todo:
            hf = None
            if need_hf:
                last = None
                for nid in self._hf_node(body.get("node")):
                    code, hf = call_node(nid, "/library/hf/profile", "POST",
                                         {"repo": fields["repo"], "match": fields.get("match") or ""}, timeout=180)
                    if code == 200:
                        break
                    last, hf = hf.get("error") or f"代理回 {code}", None
                    if code == 400:          # repo 本身的問題（找不到、沒有 GGUF），換一台也一樣
                        break
                if hf is None:
                    errors.append(f"{fields['repo']}：{last or '沒有連得上的機器可以讀 Hugging Face'}")
                    continue
                if hf["profile"].get("cn_origin"):
                    errors.append(f"{fields['repo']}：檔頭判定為中國來源，政府案不得使用，不列入推薦清單")
                    continue
            done.append(CATALOG.upsert(fields if not all_rows else {"repo": fields["repo"]}, hf)["repo"])
        self._audit("catalog.refresh-all" if all_rows else "catalog.save", "" if all_rows else todo[0][0]["repo"],
                    not errors, {"updated": done, "message": "；".join(errors)[:300] if errors else None})
        if errors and not done:
            return self._json(400, {"error": "；".join(errors)})
        return self._json(200, {"ok": True, "updated": done, "errors": errors})

    def _sd_catalog_save(self, body, all_rows):
        """畫圖與影片清單：每個元件的檔案與大小由代理讀 Hugging Face 的檔案清單（不下載）。"""
        rows = SD_CATALOG.load()
        if all_rows:
            todo = [r for r in rows]
        else:
            fields, err = recommend.check_sd_fields(body)
            if err:
                return self._json(400, {"error": err})
            todo = [fields]
        done, errors = [], []
        for fields in todo:
            trees, last = {}, None
            for repo in dict.fromkeys(p["repo"] for p in fields["parts"]):
                for nid in self._hf_node(body.get("node")):
                    code, t = call_node(nid, "/library/hf/tree", "POST", {"repo": repo}, timeout=60)
                    if code == 200 or code == 400:      # 400＝repo 本身的問題，換一台也一樣
                        break
                if code != 200:
                    last = t.get("error") or f"代理回 {code}"
                    break
                trees[repo] = t["files"]
            if last:
                errors.append(f"{fields['repo']}：{last}")
                continue
            files = recommend.sd_files(fields["parts"], trees)
            if not recommend.sd_resolve({**fields, "files": files}):
                errors.append(f"{fields['repo']}：照填的檔名湊不出一組完整的檔，請確認每個元件的檔名或檔名開頭")
                continue
            done.append(SD_CATALOG.upsert({"repo": fields["repo"]} if all_rows else fields,
                                          extra={"files": files, "refreshed": int(time.time())})["repo"])
        self._audit("sd_catalog.refresh-all" if all_rows else "sd_catalog.save", "" if all_rows else todo[0]["repo"],
                    not errors, {"updated": done, "message": "；".join(errors)[:300] if errors else None})
        if errors and not done:
            return self._json(400, {"error": "；".join(errors)})
        return self._json(200, {"ok": True, "updated": done, "errors": errors})

    def _audit(self, action, target="", ok=True, detail=None):
        STORE.audit(self.user or "", self.ip, action, target, ok, detail)

    # ---- 統一錯誤處理：任何未預期的例外都不把堆疊或內部訊息回給前端，原因寫進服務記錄
    def handle_one_request(self):
        # 同一條 keep-alive 連線上的每個請求共用同一個 Handler 物件：上一個請求查到的使用者
        # 一定要清掉，否則登入前查到的「沒人」會跟到登入後，登出後查到的「有人」也會留著
        # （實際踩到：登入成功後 /api/me 仍回 401，畫面卡在登入頁）
        self.__dict__.pop("_user_cache", None)
        # 請求列、標頭（POST 連同本文）要在 15 秒內送完，閒置的 keep-alive 也 15 秒就關：
        # 以前每次 recv 各等 120 秒，每 100 秒送一個位元組就能無限期佔住一條連線，8 個來源就能佔滿上限（資安審查）
        self._hdr_timer = threading.Timer(HEADER_DEADLINE, self._abort_slow)
        self._hdr_timer.daemon = True
        self._hdr_timer.start()
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            import traceback
            print("未預期的錯誤：\n" + traceback.format_exc(), flush=True)
            try:
                self._json(500, {"error": "伺服器內部錯誤，已記錄"})
            except Exception:  # noqa: BLE001
                pass
            self.close_connection = True
        finally:
            self._hdr_timer.cancel()

    def _abort_slow(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _headers_done(self):
        t = getattr(self, "_hdr_timer", None)
        if t:
            t.cancel()

    def version_string(self):
        return "xconsole"

    # ---- GET
    def do_GET(self):  # noqa: N802
        self._headers_done()
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/healthz":
            return self._json(200, {"ok": True, "service": "xconsole"})
        if path == "/api/health":
            cur = CERTS.current() or {}
            return self._json(200, {"ok": True, "service": "xconsole", "tls": {
                "daysLeft": cur.get("daysLeft"), "validToTaipei": cur.get("validToTaipei"),
                "source": cur.get("source"), "selfSigned": cur.get("selfSigned")}})
        if path == "/api/branding":
            return self._json(200, branding())
        if path == "/branding/logo":
            return self._logo()
        m = re.fullmatch(r"/install/([A-Za-z0-9_-]{20,64})/(agent\.sh|agent\.ps1|xconsole_agent\.py)", path)
        if m:
            # 不需要登入，但一定要帶有效的加入碼；腳本與代理程式本身都不含機密
            j = join_lookup(m.group(1))
            if not j:
                return self._send(404, "加入碼不對、已經用過或過期了，請在主控台重新按「加一台機器」。\n".encode(),
                                  "text/plain; charset=utf-8")
            if m.group(2) == "agent.sh":
                return self._send(200, install_script(m.group(1), j).encode(), "text/x-shellscript; charset=utf-8")
            if m.group(2) == "agent.ps1":
                # 加 BOM：Windows PowerShell 5.1 沒 BOM 會用 ANSI 讀，中文訊息變亂碼
                return self._send(200, "\ufeff".encode() + install_script_ps1(m.group(1), j).encode(), "text/plain; charset=utf-8")
            src = agent_source()
            if not src:
                return self._send(500, "主控台找不到代理程式檔，請重新部署主控台。\n".encode(), "text/plain; charset=utf-8")
            with open(src, "rb") as fh:
                return self._send(200, fh.read(), "text/x-python; charset=utf-8")
        if not path.startswith("/api/"):
            return self._static(path)
        if self._need_auth():
            return None

        if path == "/api/me":
            cur = CERTS.current() or {}
            return self._json(200, {"user": self.user, "tls": {
                "level": cur.get("level"), "daysLeft": cur.get("daysLeft"),
                "validToTaipei": cur.get("validToTaipei"), "selfSigned": cur.get("selfSigned")}})
        if path == "/api/nodes":
            out = []
            for n in fleet()["nodes"]:
                tf = trust_file(n["id"])
                fp = None
                if os.path.exists(tf):
                    r, _ = tls.inspect_trusted_ca(open(tf, "rb").read())
                    fp = (r.get("summary") or {}).get("fingerprint256")
                out.append({**{k: v for k, v in n.items() if k != "token"},
                            "trusted": os.path.exists(tf), "trust_fingerprint": fp})
            return self._json(200, {"nodes": out})
        if path == "/api/fleet":
            return self._json(200, fleet_state())
        m = re.fullmatch(r"/api/nodes/join-code/([0-9a-f]{12})", path)
        if m:
            with _JOIN_LOCK:
                j = next((v for k, v in _JOINS.items() if k.startswith(m.group(1))), None)
            if not j:
                return self._json(404, {"error": "沒有這個加入碼"})
            state = "joined" if j.get("node_id") else "expired" if j["exp"] < time.time() else "waiting"
            return self._json(200, {"state": state, "node_id": j.get("node_id"), "error": j.get("error"),
                                    "linger": j.get("linger"), "expires_at": j["exp"]})
        m = re.fullmatch(r"/api/node/([\w.-]+)/machine", path)
        if m:
            return self._json(*call_node(m.group(1), "/machine", timeout=60))
        m = re.fullmatch(r"/api/node/([\w.-]+)/(llama|sd)/build(/progress)?", path)
        if m:
            return self._json(*call_node(m.group(1), f"/{m.group(2)}/build{m.group(3) or ''}", timeout=60))
        m = re.fullmatch(r"/api/node/([\w.-]+)/apiinfo/([\w.-]+)", path)
        if m:
            return self._json(*call_node(m.group(1), f"/apiinfo/{m.group(2)}", timeout=20))
        m = re.fullmatch(r"/api/node/([\w.-]+)/library", path)
        if m:
            return self._json(*call_node(m.group(1), "/library", timeout=90))
        m = re.fullmatch(r"/api/node/([\w.-]+)/recommend", path)
        if m:
            code, st = call_node(m.group(1), "/state", timeout=30)
            if code != 200:
                return self._json(code, st)
            lc, lib = call_node(m.group(1), "/library", timeout=90)
            out = recommend.recommend(st, lib if lc == 200 else {}, CATALOG.load())
            out["chip"] = st.get("chip")
            out["store"] = {k: lib.get(k) for k in ("root", "free_gib", "total_gib", "hdd", "llama_server")} if lc == 200 else None
            return self._json(200, out)
        if path == "/api/catalog":
            return self._json(200, {"items": CATALOG.load(), "uses": recommend.USES})
        # ---- 畫圖與影片
        m = re.fullmatch(r"/api/node/([\w.-]+)/sd/recommend", path)
        if m:
            code, st = call_node(m.group(1), "/state", timeout=30)
            if code != 200:
                return self._json(code, st)
            lc, lib = call_node(m.group(1), "/sd/library", timeout=60)
            if lc != 200:
                return self._json(lc, lib)
            return self._json(200, recommend.recommend_sd(st, lib, SD_CATALOG.load()))
        if path == "/api/sd/catalog":
            return self._json(200, {"items": SD_CATALOG.load(), "roles": recommend.SD_ROLES, "kinds": recommend.SD_KINDS,
                                    "samplers": recommend.SD_SAMPLERS})
        m = re.fullmatch(r"/api/node/([\w.-]+)/sd/jobs(/[0-9a-f]{8}(?:/output)?)?", path)
        if m:
            return self._json(*call_node(m.group(1), "/sd/jobs" + (m.group(2) or ""), timeout=60))
        m = re.fullmatch(r"/api/node/([\w.-]+)/(state|counters)", path)
        if m:
            return self._json(*call_node(m.group(1), "/" + m.group(2)))
        m = re.fullmatch(r"/api/node/([\w.-]+)/(logs|catalog)/([\w.-]+)", path)
        if m:
            return self._json(*call_node(m.group(1), f"/{m.group(2)}/{m.group(3)}"))

        if path == "/api/tls/status":
            rec, _ = tls.pending_csr(CERTS.p)
            return self._json(200, {
                "current": CERTS.current(), "hostnames": hostnames(),
                "hsts_allowed": CERTS.hsts_allowed(), "history": CERTS.history(),
                "csr": ({"cn": rec["cn"], "hostnames": rec["hostnames"],
                         "createdTaipei": tls.taipei(dt.datetime.fromtimestamp(rec["created"], dt.timezone.utc))}
                        if rec else None)})
        if path == "/api/tls/csr":
            rec, _ = tls.pending_csr(CERTS.p)
            if not rec:
                return self._json(404, {"error": "目前沒有待配對的 CSR"})
            return self._send(200, rec["csr_pem"].encode(), "application/pkcs10",
                              {"Content-Disposition": f'attachment; filename="{rec["cn"]}.csr"'})
        if path == "/api/admin/usage-report":
            try:
                days = max(1, min(400, int((q.get("days") or ["30"])[0])))
            except ValueError:
                days = 30
            return self._json(200, report.build(STORE, CERTS, fleet(), days))
        if path == "/api/admin/audit":
            try:
                limit = max(1, min(500, int((q.get("limit") or ["100"])[0])))
            except ValueError:
                limit = 100
            return self._json(200, {"items": STORE.audit_list(limit)})
        if path == "/api/settings":
            cfg = fleet()
            return self._json(200, {"report": cfg["report"], "hostnames": hostnames(),
                                    "username": cfg["auth"].get("username", "admin")})
        return self._json(404, {"error": "找不到"})

    # ---- POST
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if path.startswith("/api/") and ctype != "application/json":
            # 跨站的 <form> 只能送 form-urlencoded／multipart／text/plain，送不出 application/json
            return self._json(415, {"error": "只接受 application/json"})
        anon = path in ("/api/login", "/api/join", "/api/logout")
        if not anon and not self.user:
            # 先驗登入再讀本文：沒登入的人不能讓主控台讀 6 MB。小的本文照樣讀掉再回 401（沒讀完就關連線，
            # TCP 會送重置，對方連 401 都收不到）；超過 64 KB 的直接關
            if self._body(MAX_ANON_BODY) is None:
                return None
            self._headers_done()
            return self._json(401, {"error": "請先登入"})
        body = self._body(MAX_ANON_BODY if anon else MAX_BODY)
        self._headers_done()             # 本文讀完才停計時：後面像換模型這種要等好幾分鐘的處理不受影響
        if body is None:
            return self._json(413, {"error": "請求內容太大"})

        if path == "/api/login":
            user = str(body.get("username") or "")[:64]
            # 一次只驗一個登入，失敗等一秒才放下一個：同時開幾百條連線也繞不過節流（以前檢查在寫入失敗紀錄之前，
            # 平行送出全部都會通過），全網分散猜密碼的速度上限約每秒一次。
            # 不再做「全部來源加起來錯 50 次就全部封鎖」：內網任何人都能藉此把管理者鎖在門外（資安審查）
            with _LOGIN_LOCK:
                since = time.time() - LOGIN_WINDOW
                if STORE.failed_logins(since, self.ip) >= LOGIN_MAX_PER_IP:
                    if time.time() - _THROTTLE_LOGGED.get(self.ip, 0) > 60:     # 被擋的請求每個來源每分鐘只記一筆，免得灌爆稽核表
                        _THROTTLE_LOGGED[self.ip] = time.time()
                        STORE.audit(user, self.ip, "auth.throttled", user, False)
                    return self._json(429, {"error": "登入失敗次數過多，請 15 分鐘後再試"})
                if not check_credentials(user, str(body.get("password") or "")):
                    STORE.audit(user, self.ip, "auth.login", user, False)
                    time.sleep(1.0)
                    return self._json(401, {"error": "帳號或密碼不對"})
            STORE.audit(user, self.ip, "auth.login", user, True)
            sid = STORE.session_new(user, self.ip, SESSION_HOURS)
            cookie = (f"xconsole={sid}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age={SESSION_HOURS * 3600}; Secure")
            return self._json(200, {"ok": True, "user": user}, cookie=cookie)
        if path == "/api/join":
            return self._json(*join_node(self, body))
        if path == "/api/logout":
            who = self.user                                   # 先記下是誰，作廢之後就查不到了
            STORE.session_end(self._cookie())                 # 伺服器端作廢，不只是清 cookie
            if who:
                STORE.audit(who, self.ip, "auth.logout", who, True)
            return self._json(200, {"ok": True}, cookie="xconsole=; Path=/; HttpOnly; Max-Age=0; Secure")
        if self._need_auth():
            return None

        # ---- 納管的機器：加入、改名與位址、移除
        if path == "/api/nodes/join-code":
            name = str(body.get("name") or "").strip()[:40]
            group = str(body.get("group") or "").strip()[:30]
            replace = str(body.get("replace") or "")
            if replace and not any(n["id"] == replace for n in fleet()["nodes"]):
                return self._json(400, {"error": "要重新安裝的那台不在納管清單裡"})
            host = str(body.get("console") or self.headers.get("Host") or "").strip()
            port_s = host.rsplit(":", 1)[1] if re.search(r":\d+$", host) else "443"
            if not HOST_RX.fullmatch(host) or not 1 <= int(port_s) <= 65535:
                return self._json(400, {"error": "主控台位址格式不對，例：140.135.107.216:9400（埠要在 1～65535）"})
            if not agent_source():
                return self._json(500, {"error": "主控台找不到代理程式檔，請重新部署主控台"})
            code = secrets.token_urlsafe(24)
            with _JOIN_LOCK:
                _JOINS[_join_key(code)] = {"exp": time.time() + JOIN_TTL, "name": name, "group": group, "replace": replace,
                                           "console": host, "pin": console_pin(), "fp": console_fp(), "by": self.user}
            self._audit("node.join_code", host, True, {"name": name, "group": group})
            url = f"https://{host}/install/{code}/agent.sh"
            ps = windows_command(f"https://{host}/install/{code}/agent.ps1").replace("{fp}", _JOINS[_join_key(code)]["fp"])
            return self._json(200, {"id": _join_key(code)[:12], "expires_at": time.time() + JOIN_TTL,
                                    "command": f"curl -fsSk --pinnedpubkey '{_JOINS[_join_key(code)]['pin']}' '{url}' | bash",
                                    "command_windows": "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
                                                       + base64.b64encode(ps.encode("utf-16-le")).decode(),
                                    "command_windows_text": ps})
        m = re.fullmatch(r"/api/nodes/([\w.-]+)/(update|remove)", path)
        if m:
            nid, act = m.group(1), m.group(2)
            node = next((n for n in fleet()["nodes"] if n["id"] == nid), None)
            if not node:
                return self._json(404, {"error": "沒有這台機器"})
            if act == "remove":
                update_fleet(lambda cfg: cfg.__setitem__("nodes", [n for n in cfg["nodes"] if n["id"] != nid]))
                if os.path.exists(trust_file(nid)):
                    os.remove(trust_file(nid))
                self._audit("node.remove", nid, True, {"name": node["name"], "host": node["host"]})
                return self._json(200, {"ok": True})
            name = str(body.get("name", node["name"]) or "").strip()
            group = str(body.get("group", node.get("group", "")) or "").strip() or "未分類"
            host = str(body.get("host", node["host"]) or "").strip()
            try:
                port = int(body.get("agent_port", node.get("agent_port", 9401)))
            except (TypeError, ValueError):
                port = 0
            if not 1 <= len(name) <= 40 or re.search(r"[\x00-\x1f<>]", name):
                return self._json(400, {"error": "名稱要 1～40 個字"})
            if len(group) > 30 or re.search(r"[\x00-\x1f<>]", group):
                return self._json(400, {"error": "群組最多 30 個字"})
            if not valid_host(host):
                return self._json(400, {"error": "位址格式不對，填 IP 或主機名稱（不含埠）"})
            if not 1 <= port <= 65535:
                return self._json(400, {"error": "代理的埠要在 1～65535"})
            moved = (host, port) != (node["host"], int(node.get("agent_port", 9401)))
            old = {k: node.get(k) for k in ("name", "group", "host", "agent_port")}

            def put(cfg):
                for n in cfg["nodes"]:
                    if n["id"] == nid:
                        n.update(name=name, group=group, host=host, agent_port=port)
            update_fleet(put)
            if moved:
                # 換位址先實際連一次，連不上就換回來，免得改一個字就整台失聯
                code, out = call_node(nid, "/healthz", timeout=15)
                if code != 200:
                    update_fleet(lambda cfg: [n.update(old) for n in cfg["nodes"] if n["id"] == nid])
                    self._audit("node.update", nid, False, {"message": out.get("error")})
                    return self._json(400, {"error": f"新位址連不到這台的代理，已改回原本的位址。{out.get('error') or ''}"})
            self._audit("node.update", nid, True, {"from": old, "to": {"name": name, "group": group, "host": host,
                                                                       "agent_port": port}})
            return self._json(200, {"ok": True})
        m = re.fullmatch(r"/api/node/([\w.-]+)/(machine|slots/add|slot/[\w.-]+/(?:edit|delete)|profiles/save|profiles/[a-z][a-z0-9_-]{0,31}/delete|llama/build|llama/build/cancel|sd/build|sd/build/cancel)", path)
        if m:
            code, out = call_node(m.group(1), "/" + m.group(2), "POST", body, timeout=60)
            act = {"machine": "machine.settings", "slots/add": "slot.add", "profiles/save": "profile.save",
                   "llama/build": "llama.build", "llama/build/cancel": "llama.build_cancel",
                   "sd/build": "sd.build", "sd/build/cancel": "sd.build_cancel"}.get(m.group(2)) or (
                "profile.delete" if m.group(2).startswith("profiles/") else
                "slot.delete" if m.group(2).endswith("delete") else "slot.edit")
            self._audit(act, f"{m.group(1)}/{m.group(2)}", code == 200,
                        {k: body.get(k) for k in ("memory_model", "ram_floor_gib", "vram_floor_gib", "bandwidth_gbs", "ram_bandwidth_gbs", "gguf_root",
                                                  "llama_server", "sd_cli", "type", "unit", "role", "tier", "port", "key", "name") if k in body}
                        | ({"message": out.get("error")} if code != 200 else {}))
            return self._json(code, out)

        # ---- 各機動作：轉給代理，並記稽核
        m = re.fullmatch(r"/api/node/([\w.-]+)/slot/([\w.-]+)/(start|stop|restart)", path)
        if m:
            code, out = call_node(m.group(1), f"/slot/{m.group(2)}/{m.group(3)}", "POST", body, timeout=240)
            self._audit(f"slot.{m.group(3)}", f"{m.group(1)}/{m.group(2)}", code == 200,
                        {"force": bool(body.get("force")), "blocked": code == 409 and "admit" in out,
                         "message": out.get("message") or out.get("error")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/slot/([\w.-]+)/model", path)
        if m:
            want = {k: body.get(k) for k in ("pack", "native", "engine", "model", "ctx", "gpus", "force") if k in body}
            # 載幾十 GB 的模型加上失敗時的還原，最久可能各要十幾分鐘
            code, out = call_node(m.group(1), f"/slot/{m.group(2)}/model", "POST", want, timeout=2000)
            self._audit("model.swap", f"{m.group(1)}/{m.group(2)}", code == 200,
                        {"to": out.get("model") or body.get("model") or os.path.basename(str(body.get("pack", ""))),
                         "engine": body.get("engine") or "strata", "switched_engine": out.get("switched_engine"),
                         "gpus": out.get("gpus") if code == 200 else body.get("gpus"),
                         "force": bool(body.get("force")), "blocked": code == 409 and "admit" in out,
                         "reverted": out.get("reverted"), "message": out.get("error")})
            return self._json(code, out)
        # ---- API 串接：網址、模型名稱、上下文、金鑰（改完重啟並驗證，失敗自動換回）
        m = re.fullmatch(r"/api/node/([\w.-]+)/apiinfo/([\w.-]+)/settings", path)
        if m:
            want = {k: body.get(k) for k in ("public_url", "model_name", "max_context", "max_tokens", "key")
                    if k in body}
            # 代理端套用最久等 900 秒、失敗還原再等 900 秒，這裡的逾時要涵蓋整段，否則代理還在做事主控台就回失敗
            code, out = call_node(m.group(1), f"/apiinfo/{m.group(2)}/settings", "POST",
                                  {**want, "force": bool(body.get("force"))}, timeout=2000)
            # 新金鑰只回給這一次的畫面；稽核只記「換了金鑰」，不記內容
            self._audit("api.settings", f"{m.group(1)}/{m.group(2)}", code == 200,
                        {"changed": out.get("changed"), "public_url": want.get("public_url"),
                         "key": want.get("key"), "restarted": out.get("restarted"),
                         "reverted": out.get("reverted"), "message": out.get("error")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/apiinfo/([\w.-]+)/test", path)
        if m:
            code, out = call_node(m.group(1), f"/apiinfo/{m.group(2)}/test", "POST", {}, timeout=420)
            if code == 200:
                out["network"] = network_test(m.group(1), m.group(2), str(body.get("key") or ""))
                out["ok"] = out["ok"] and out["network"]["ok"]
            self._audit("api.test", f"{m.group(1)}/{m.group(2)}", code == 200 and out.get("ok"),
                        {"checks": [(c["name"], c["ok"]) for c in out.get("checks") or []],
                         "network": (out.get("network") or {}).get("ok")})
            return self._json(code, out)
        # ---- 模型庫：查 Hugging Face、下載、取消、刪除（權杖只轉給代理，不寫稽核）
        m = re.fullmatch(r"/api/node/([\w.-]+)/library/(hf|hf/tree|download|delete)", path)
        if m:
            node_id, act = m.groups()
            if act == "hf/tree":
                return self._json(*call_node(node_id, "/library/hf/tree", "POST",
                                             {"repo": body.get("repo"), "token": body.get("token")}, timeout=60))
            if act == "hf":
                return self._json(*call_node(node_id, "/library/hf", "POST",
                                             {"repo": body.get("repo"), "token": body.get("token")}, timeout=60))
            if act == "download":
                want = {k: body.get(k) for k in ("repo", "stem", "mmproj", "token")}
                code, out = call_node(node_id, "/library/download", "POST", want, timeout=90)
                self._audit("library.download", node_id, code == 200,
                            {"repo": want["repo"], "stem": want["stem"], "mmproj": want["mmproj"],
                             "message": out.get("error")})
                return self._json(code, out)
            code, out = call_node(node_id, "/library/delete", "POST", {"id": body.get("id")}, timeout=120)
            self._audit("library.delete", f"{node_id}/{body.get('id')}", code == 200,
                        {"freed_gib": out.get("freed_gib"), "message": out.get("error")})
            return self._json(code, out)
        # ---- 畫圖與影片：清單增改刪、下載（照清單組好規格，或內部測試手動組）、刪除、試算、產圖
        if path in ("/api/sd/catalog/save", "/api/sd/catalog/refresh-all"):
            return self._sd_catalog_save(body, path.endswith("refresh-all"))
        if path in ("/api/sd/catalog/delete", "/api/sd/catalog/move"):
            repo = str(body.get("repo") or "")
            ok = SD_CATALOG.delete(repo) if path.endswith("delete") else SD_CATALOG.move(repo, -1 if body.get("step") == -1 else 1)
            self._audit("sd_catalog." + path.rsplit("/", 1)[1], repo, ok)
            return self._json(200 if ok else 404, {"ok": ok} if ok else {"error": "清單裡沒有這個模型"})
        m = re.fullmatch(r"/api/node/([\w.-]+)/sd/download", path)
        if m:
            if body.get("manual"):
                spec = {**body["manual"], "internal": True} if isinstance(body["manual"], dict) else {}
            else:
                e = next((r for r in SD_CATALOG.load() if r["repo"] == body.get("repo")), None)
                combo = recommend.sd_resolve(e, str(body.get("path") or "")) if e else []
                if not combo:
                    return self._json(404, {"error": "清單裡沒有這顆或這個量化，請重新整理"})
                spec = recommend.sd_spec(e, combo[0])
            code, out = call_node(m.group(1), "/sd/download", "POST", {"spec": spec, "token": body.get("token")}, timeout=120)
            self._audit("sd.download.internal" if body.get("manual") else "sd.download", m.group(1), code == 200,
                        {"model": spec.get("id"), "parts": [f"{p.get('repo')}/{p.get('path')}" for p in spec.get("parts") or []][:8],
                         "message": out.get("error")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/sd/(delete|admit|generate)", path)
        if m:
            node_id, act = m.groups()
            keys = {"delete": ("id",), "admit": ("model", "width", "height", "frames"),
                    "generate": ("model", "prompt", "negative", "width", "height", "steps", "cfg", "sampler", "seed", "frames", "fps")}[act]
            code, out = call_node(node_id, f"/sd/{act}", "POST", {k: body.get(k) for k in keys}, timeout=120)
            if act != "admit":
                self._audit(f"sd.{act}", f"{node_id}/{body.get('id') or body.get('model')}", code == 200,
                            {"message": out.get("error")} | ({"job": out.get("id"), "size": f"{body.get('width')}x{body.get('height')}"}
                                                            if act == "generate" else {"freed_gib": out.get("freed_gib")}))
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/sd/jobs/([0-9a-f]{8})/(cancel|delete)", path)
        if m:
            code, out = call_node(m.group(1), f"/sd/jobs/{m.group(2)}/{m.group(3)}", "POST", {})
            self._audit(f"sd.job.{m.group(3)}", f"{m.group(1)}/{m.group(2)}", code == 200, {"message": out.get("error")})
            return self._json(code, out)
        # ---- 推薦清單：管理者增改刪；大小與架構一律由代理從 Hugging Face 檔頭讀，不手填
        if path in ("/api/catalog/save", "/api/catalog/refresh-all"):
            return self._catalog_save(body, path.endswith("refresh-all"))
        if path in ("/api/catalog/delete", "/api/catalog/move"):
            repo = str(body.get("repo") or "")
            ok = CATALOG.delete(repo) if path.endswith("delete") else CATALOG.move(repo, -1 if body.get("step") == -1 else 1)
            self._audit("catalog." + path.rsplit("/", 1)[1], repo, ok)
            return self._json(200 if ok else 404, {"ok": ok} if ok else {"error": "推薦清單裡沒有這個模型"})
        m = re.fullmatch(r"/api/node/([\w.-]+)/library/download/([0-9a-f]{8})/cancel", path)
        if m:
            code, out = call_node(m.group(1), f"/library/download/{m.group(2)}/cancel", "POST", {})
            self._audit("library.cancel", f"{m.group(1)}/{m.group(2)}", code == 200)
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/admit/([\w.-]+)", path)
        if m:
            return self._json(*call_node(m.group(1), f"/admit/{m.group(2)}", "POST", body))
        m = re.fullmatch(r"/api/node/([\w.-]+)/profile/([\w.-]+)", path)
        if m:
            code, out = call_node(m.group(1), f"/profile/{m.group(2)}", "POST", body, timeout=560)
            self._audit("profile.apply", f"{m.group(1)}/{m.group(2)}", code == 200,
                        {"force": bool(body.get("force")), "errors": out.get("errors")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/all/(start|stop|restart)", path)
        if m:
            code, out = call_node(m.group(1), f"/all/{m.group(2)}", "POST", body, timeout=560)
            self._audit(f"all.{m.group(2)}", m.group(1), code == 200, {"force": bool(body.get("force"))})
            return self._json(code, out)

        # ---- 信任各機代理的憑證（每個串接對象各一份）
        m = re.fullmatch(r"/api/node/([\w.-]+)/trusted-ca", path)
        if m:
            node_id = m.group(1)
            if not any(n["id"] == node_id for n in fleet()["nodes"]):
                return self._json(404, {"error": "沒有這台機器"})
            try:
                data = base64.b64decode(str(body.get("file") or ""))
            except Exception:  # noqa: BLE001
                return self._json(400, {"error": "檔案格式不對"})
            r, pem = tls.inspect_trusted_ca(data)
            if not r["ok"]:
                return self._json(400, r)
            with open(trust_file(node_id) + ".tmp", "wb") as fh:
                fh.write(pem)
            os.chmod(trust_file(node_id) + ".tmp", 0o600)
            os.replace(trust_file(node_id) + ".tmp", trust_file(node_id))
            self._audit("trust.import", node_id, True, {"fingerprint256": r["summary"]["fingerprint256"],
                                                        "subject": r["summary"]["subject"]})
            return self._json(200, r)

        # ---- 憑證
        if path == "/api/tls/inspect":
            files = []
            for f in body.get("files") or []:
                try:
                    files.append((str(f.get("name", "檔案"))[:120], base64.b64decode(f.get("data", ""))))
                except Exception:  # noqa: BLE001
                    return self._json(400, {"error": "有檔案讀不出來"})
            if not files:
                return self._json(400, {"error": "請先選擇檔案"})
            res = CERTS.inspect(files, str(body.get("password") or ""), str(body.get("key_password") or ""),
                                hostnames())
            self._audit("tls.inspect", ",".join(n for n, _ in files), res["ok"],
                        {"fingerprint256": (res.get("summary") or {}).get("fingerprint256"),
                         "errors": res["errors"]})
            return self._json(200, res)
        if path == "/api/tls/apply":
            ok, msg, summary = CERTS.apply(str(body.get("token") or ""))
            self._audit("tls.apply", (summary or {}).get("subject", ""), ok,
                        {"fingerprint256": (summary or {}).get("fingerprint256"),
                         "validToTaipei": (summary or {}).get("validToTaipei"), "message": msg})
            return self._json(200 if ok else 409, {"ok": ok, "message": msg, "current": CERTS.current()})
        if path == "/api/tls/rollback":
            ok, msg = CERTS.rollback()
            cur = CERTS.current() or {}
            self._audit("tls.rollback", cur.get("subject", ""), ok,
                        {"fingerprint256": cur.get("fingerprint256"), "message": msg})
            return self._json(200 if ok else 409, {"ok": ok, "message": msg, "current": cur})
        if path == "/api/tls/csr":
            cn = str(body.get("cn") or "").strip()
            names = [h.strip() for h in body.get("hostnames") or [] if str(h).strip()]
            if not cn:
                return self._json(400, {"error": "請填主要網址（CN）"})
            pem = tls.generate_csr(CERTS.p, cn, names, str(body.get("org") or ""),
                                   "ec256" if body.get("key_type") == "ec256" else "rsa2048")
            self._audit("tls.csr", cn, True, {"hostnames": names})
            return self._json(200, {"ok": True, "csr": pem})
        if path == "/api/tls/bootstrap":
            cur = CERTS.current() or {}
            if cur.get("source") == "imported" and not body.get("confirm"):
                return self._json(409, {"error": "目前是正式憑證，換回暫用自簽會讓使用者看到安全警告；確定要換請再確認一次"})
            ok, msg = CERTS.bootstrap(hostnames())
            self._audit("tls.bootstrap", "", ok, {"message": msg})
            return self._json(200 if ok else 409, {"ok": ok, "message": msg, "current": CERTS.current()})
        if path == "/api/tls/settings":
            if "hostnames" in body:
                names = [h.strip() for h in body.get("hostnames") or [] if str(h).strip()]
                if not names:
                    return self._json(400, {"error": "至少要有一個網址"})
                update_fleet(lambda c: c["tls"].__setitem__("hostnames", names))
                self._audit("tls.hostnames", "", True, {"hostnames": names})
            if "hsts" in body:
                ok, msg = CERTS.set_hsts(bool(body["hsts"]))
                self._audit("tls.hsts", "", ok, {"on": bool(body["hsts"]), "message": msg})
                if not ok:
                    return self._json(409, {"error": msg})
            return self._json(200, {"ok": True, "hostnames": hostnames(), "current": CERTS.current()})

        # ---- 客戶品牌
        if path == "/api/branding":
            org, product = (str(body.get(k) or "").strip() for k in ("org", "product"))
            for label, v in (("單位名稱", org), ("系統名稱", product)):
                if not BRAND_NAME_RX.fullmatch(v):
                    return self._json(400, {"error": f"{label}要 1～40 個字，不能有 < > 或控制字元"})
            update_fleet(lambda c: c.setdefault("branding", {}).update({"org": org, "product": product}))
            self._audit("branding.update", "", True, {"org": org, "product": product})
            return self._json(200, {"ok": True, "branding": branding()})
        if path == "/api/branding/logo":
            if body.get("remove"):
                with _BRAND_LOCK:
                    old = (fleet().get("branding") or {}).get("logo")
                    update_fleet(lambda c: c.setdefault("branding", {}).pop("logo", None))
                    if old:
                        try:
                            os.remove(os.path.join(BRAND_DIR, os.path.basename(old.get("file", ""))))
                        except FileNotFoundError:
                            pass
                self._audit("branding.logo", "移除", True)
                return self._json(200, {"ok": True, "branding": branding()})
            try:
                data = base64.b64decode(str(body.get("data") or ""), validate=True)
            except (ValueError, TypeError):
                return self._json(400, {"error": "檔案內容讀不到"})
            if not data:
                return self._json(400, {"error": "沒有收到檔案"})
            if len(data) > LOGO_MAX:
                return self._json(400, {"error": "Logo 要在 1 MB 以內"})
            info = image_info(data)
            if not info:
                return self._json(400, {"error": "只接受 PNG、JPEG、WebP 圖檔（不收 SVG）"})
            if not (16 <= info[1] <= 4096 and 16 <= info[2] <= 4096):
                return self._json(400, {"error": f"圖片尺寸 {info[1]}×{info[2]} 不合用，寬高要在 16～4096 像素之間"})
            with _BRAND_LOCK:
                meta = save_logo(data)
                update_fleet(lambda c: c.setdefault("branding", {}).__setitem__("logo", meta))
            self._audit("branding.logo", meta["file"], True, {"width": meta["w"], "height": meta["h"],
                                                               "bytes": meta["bytes"], "sha256": meta["sha"][:12]})
            return self._json(200, {"ok": True, "branding": branding()})

        # ---- 帳號與報表設定
        if path == "/api/account/password":
            old, new, user = str(body.get("old") or ""), str(body.get("new") or ""), self.user
            if not check_credentials(user, old):
                time.sleep(1.0)
                self._audit("account.password", user, False, {"reason": "舊密碼不對"})
                return self._json(403, {"error": "目前的密碼不對"})
            if len(new) < 8:
                return self._json(400, {"error": "新密碼至少 8 碼"})
            set_credentials(user, new)             # 內含作廢所有登入
            self._audit("account.password", user, True)
            return self._json(200, {"ok": True, "message": "已變更，請用新密碼重新登入"},
                              cookie="xconsole=; Path=/; HttpOnly; Max-Age=0; Secure")
        if path == "/api/settings":
            rep = body.get("report") or {}

            def num(v):
                if v in (None, ""):
                    return None
                try:
                    return round(float(v), 4)
                except (TypeError, ValueError):
                    raise ValueError(f"「{v}」不是數字")
            try:
                new = {"price_ntd_per_kwh": num(rep.get("price_ntd_per_kwh"))}
                ref = rep.get("cloud_ref") or {}
                if any(ref.get(k) not in (None, "") for k in ("usd_per_mtok_in", "usd_per_mtok_out")):
                    new["cloud_ref"] = {"name": str(ref.get("name") or "")[:60],
                                        "usd_per_mtok_in": num(ref.get("usd_per_mtok_in")),
                                        "usd_per_mtok_out": num(ref.get("usd_per_mtok_out")),
                                        "ntd_per_usd": num(ref.get("ntd_per_usd"))}
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            update_fleet(lambda c: c.__setitem__("report", new))
            self._audit("settings.report", "", True, new)
            return self._json(200, {"ok": True, "report": new})
        return self._json(404, {"error": "找不到"})


def wait_for_address(seconds=60):
    """開機時 user 服務會比網路先起來：這時抓不到對外 IP，hostnames() 退回主機名稱，
    ensure() 就產一張不含 IP 的暫用自簽，網路好了再重啟又重產一張（2026-10-22 中原主機重開機實際踩到）。
    沒自訂網址時，先等到抓得到 IP 再產憑證；等不到就照舊，不擋啟動。"""
    if fleet()["tls"].get("hostnames"):
        return
    cur = CERTS.current()
    if cur and cur.get("source") != "selfsigned":
        return                      # 匯入的正式憑證 ensure() 不會重產，不必等
    for _ in range(seconds):
        if is_ip(tls.default_hostnames()[0]):
            return
        time.sleep(1)


def is_ip(text):
    """真的解析成 IP 才算；只由十六進位字元組成的主機名稱（容器常見的 abcdef1234）不能當成 IP。"""
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def main():
    for st in (sys.stdout, sys.stderr):
        # Windows 的主控台或管線預設 cp1252／cp950，印中文會直接例外結束：一律改 UTF-8，印不出的字換掉
        if st is not None and hasattr(st, "reconfigure"):
            st.reconfigure(encoding="utf-8", errors="replace")
    if sys.stdout is None:
        # Windows 用 pythonw 在背景跑時沒有主控台：訊息寫進記錄檔，不然出錯時什麼都看不到
        sys.stdout = sys.stderr = open(os.path.join(STATE_DIR, "console.log"), "a", encoding="utf-8", buffering=1)
    cfg = fleet()
    if not (cfg.get("auth") or {}).get("hash"):
        print("還沒設帳密。先跑：python3 xconsole_server.py --set-credentials", flush=True)
    moved = migrate_tokens()
    if moved:
        print(f"已把 {moved} 個明文代理權杖轉成密文（fleet.json）", flush=True)
    wait_for_address()
    fresh = CERTS.ensure(hostnames())
    srv = tls.TLSServer(("0.0.0.0", PORT), Handler)
    srv.certs = CERTS
    threading.Thread(target=sampler, daemon=True).start()
    cur = CERTS.current()
    kind = "暫用自簽（請到「設定 → HTTPS 與憑證」換成正式憑證）" if cur["selfSigned"] else cur["issuer"]
    print(f"xConsole 聽在 https://0.0.0.0:{PORT}；憑證：{kind}"
          + ("（剛產生）" if fresh else "") + f"；涵蓋 {', '.join(cur['dns'] + cur['ips'])}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if "--set-credentials" in sys.argv or "--set-password" in sys.argv:
        # 帳密從標準輸入讀（第一行帳號、第二行密碼），不走環境變數也不進指令列——
        # 那兩個地方都會被 ps、/proc/<pid>/environ 與 shell 歷史留下
        if sys.stdin.isatty():
            import getpass
            user = input("帳號（預設 admin）：").strip() or "admin"
            pw = getpass.getpass("密碼：")
        else:
            # Windows PowerShell 用管線送進來、或用記事本存的帳密檔，開頭會多一個 BOM：不去掉的話帳號會變成「\ufeffadmin」，怎麼登入都不對
            lines = sys.stdin.read().lstrip("\ufeff").splitlines()
            user = (lines[0].strip() if lines else "") or "admin"
            pw = lines[1] if len(lines) > 1 else ""
        if len(pw) < 8:
            raise SystemExit("密碼至少 8 碼")
        set_credentials(user, pw)
        STORE.audit(user, "本機", "account.password", user, True, {"via": "cli"})
        print(f"已設定帳號 {user}")
        raise SystemExit(0)
    main()
