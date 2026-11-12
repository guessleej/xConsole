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
import concurrent.futures
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
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chats  # noqa: E402
import computer  # noqa: E402
import updater  # noqa: E402
import recommend  # noqa: E402
import report  # noqa: E402
import tls  # noqa: E402
import agentlink  # noqa: E402
import linkcert  # noqa: E402
import groups  # noqa: E402
import joincodes  # noqa: E402
from store import Store  # noqa: E402

# Windows 以 SYSTEM 帳號開機自啟時沒有一般的家目錄：安裝程式用 --home 指到 C:\ProgramData\xConsole
HOME = (sys.argv[sys.argv.index("--home") + 1] if "--home" in sys.argv[:-1] else None) \
    or os.environ.get("XCONSOLE_HOME") or os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".xconsole")
FLEET = os.path.join(STATE_DIR, "fleet.json")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = int(os.environ.get("XCONSOLE_PORT", "9400"))
# 代理主動連回用的通道埠：只回代理的兩條路徑，用專用的通道憑證。遠端的筆電要從外面連回來時，對外只開這個埠
LINK_PORT = int(os.environ.get("XCONSOLE_LINK_PORT", "9402"))


def _arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv[:-1] else None


# 桌面版（xConsole 應用程式帶起來的）：視窗走只聽 127.0.0.1 的本機通道、用應用程式給的鑰匙進入，不用輸入密碼；
# 別台電腦要連進來（區網存取）才走 HTTPS＋密碼。鑰匙從標準輸入讀（不進指令列、環境變數、檔案）
DESKTOP = "--desktop" in sys.argv
APP_PORT = int(_arg("--app-port") or 0)
DESKTOP_KEY = None
PARENT_PID = int(_arg("--parent-pid") or 0)
SESSION_HOURS = 12
SAMPLE_EVERY = 300
MAX_BODY = 6 * 1024 * 1024
MAX_ANON_BODY = 64 * 1024        # 登入與加入機器不需登入：本文只要幾 KB
CHAT_BODY = 16 * 1024 * 1024     # 存對話：一則新附圖（6 MB，base64 後 8 MB）加上附件文字
EXTRACT_MAX = 15 * 1024 * 1024    # 對話附件（PDF、Word…）最大 15 MB
EXTRACT_BODY = EXTRACT_MAX * 4 // 3 + 64 * 1024     # base64 之後再加 JSON 外殼
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
RING = linkcert.Ring(STATE_DIR)       # 通道憑證：換管理網頁的憑證不影響它
LINK_LISTEN = {"ok": False, "error": "還沒啟動"}
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

def is_local_address(ip):
    """這個位址是不是本機網卡上的：問系統路由「往這個位址送，來源會用哪個位址」，來源就是它自己才算。
    本機位址走 loopback、來源等於目的；別台的位址來源會是本機出口網卡，對不上。
    不用 bind 判斷：Linux 開了 net.ipv4.ip_nonlocal_bind（HA、透明代理常見）時任何位址都綁得上，
    會把外部網址當成本機、把管理者的金鑰送出去（Codex 審查）。UDP connect 不送封包，三個系統都適用。"""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
        fam = socket.AF_INET6 if a.version == 6 else socket.AF_INET
        with socket.socket(fam, socket.SOCK_DGRAM) as s:
            s.connect((ip, 9))
            src = s.getsockname()[0]
        return ipaddress.ip_address(src.split("%")[0]) == a
    except (OSError, ValueError):
        return False


def same_machine(node_host, url_host):
    """對外網址解析出來的位址是不是全部落在這個節點上；是就回那組位址，不是回空集合。
    單機部署時節點記成 127.0.0.1，對外網址卻是這台的網卡位址，
    只比對 127.0.0.1 會誤判成別台（2026-11-05 客戶單機實測）。節點是本機時，本機網卡上的位址也算同一台；
    遠端節點仍只認它自己解析出來的位址，雲端 metadata、內網別台一律不放行。"""
    try:
        mine = {i[4][0] for i in socket.getaddrinfo(node_host, None)}
        theirs = {i[4][0] for i in socket.getaddrinfo(url_host, None)}
    except OSError:
        return set()
    if mine and all(ipaddress.ip_address(a.split("%")[0]).is_loopback for a in mine):
        mine |= {a for a in theirs if is_local_address(a)}
    return theirs if theirs and theirs <= mine else set()


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
    theirs = same_machine(node["host"], u.hostname)
    if not theirs:
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


LINK = agentlink.Link(log=lambda m: print(m, flush=True))
PUSH = "push"            # node_request() 對主動連回的節點回這個當 TLS 設定：agent_open() 認得它


def link_cfg():
    c = fleet().get("link") or {}
    return {"public": [str(x) for x in c.get("public") or []][:8], "proxy": str(c.get("proxy") or "")}


def link_ver():
    """代理手上那份通道設定的版本：憑證版本＋對外網址與代理伺服器的雜湊。代理回報的不一樣就再發一次。"""
    c = link_cfg()
    return f"{RING.version()}-{hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:8]}"


def link_payload():
    pem, cur = RING.trusted()
    c = link_cfg()
    return {"v": link_ver(), "cert": pem, "current": cur, "public": c["public"], "proxy": c["proxy"]}


def push_ids():
    return [n["id"] for n in fleet()["nodes"] if n.get("link") == "push"]


_LINK_TICK = [0.0]


def link_tick(force=False):
    """所有在線的工作站都收到新通道憑證了就換上；退役超過 30 天的刪掉。輪詢時順手做，每 2 秒最多一次。"""
    if not force and time.time() - _LINK_TICK[0] < 2:
        return
    _LINK_TICK[0] = time.time()
    if RING.check(push_ids(), LINK.online):
        LINK.wake()
        STORE.audit("", "系統", "link.cert", RING.current()["fp"][:16], True, {"version": RING.version()})


def valid_link_addr(a):
    """對外網址：主機:埠，或 [IPv6]:埠。"""
    # 不用正規表示式：使用者輸入配上有回溯的樣式，長字串會跑很久（CodeQL）
    host, sep, port = a[:260].rpartition(":")
    if not sep or len(a) > 255 or not port.isdigit() or len(port) > 5:
        return False
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        if ":" not in host:
            return False                            # 方括號只給 IPv6
    elif ":" in host:
        return False
    return valid_host(host) and 0 < int(port) < 65536


def valid_proxy(u):
    """企業的代理伺服器：http://主機:埠。帳密不收：主控台不保存代理伺服器的密碼，要帳密就用那台的系統設定。"""
    try:
        x = urlparse(u)
        port = x.port
    except ValueError:
        return False
    return x.scheme == "http" and bool(x.hostname) and valid_host(x.hostname) and not x.username \
        and not x.password and x.path in ("", "/") and not x.query and bool(port)


def node_url(node, path):
    """主控台連代理（link=pull，伺服器）用 https；代理主動連回（link=push，工作站）的排進那台的佇列。"""
    if node.get("link") == "push":
        return f"push://{node['id']}{path}"
    h = node["host"]
    return f"https://{'[' + h + ']' if ':' in h and not h.startswith('[') else h}:{node.get('agent_port', 9401)}{path}"


def agent_open(req, ctx, timeout):
    """連各機代理：一律不走代理伺服器。代理都在內網；macOS 的 urllib 會讀系統的代理設定，
    單機版連自己的 127.0.0.1 也會被送去代理而連不上。
    push:// 的請求排進那台的長輪詢佇列，回來的物件跟 urllib 的回應一樣用（4xx／5xx 一樣丟 HTTPError）。"""
    if req.full_url.startswith("push://"):
        nid, _, rest = req.full_url[7:].partition("/")
        resp = LINK.open(nid, req.get_method(), "/" + rest, req.data,
                         req.get_header("Content-type") or "application/json", timeout)
        if resp.status >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status, resp.reason, resp.headers, resp)
        return resp
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    return op.open(req, timeout=timeout)


def node_request(node_id, path, data, ctype="application/json"):
    """組一個送給某台代理的請求（主機只取自 fleet.json、用部署時取回的憑證驗證）。回 (請求, TLS 設定) 或 (None, 錯誤)。"""
    node = next((n for n in fleet()["nodes"] if n["id"] == node_id), None)
    if not node:
        return None, "沒有這台機器"
    ctx = PUSH if node.get("link") == "push" else tls.client_context(trust_file(node_id))
    if ctx is None:
        return None, f"還沒信任 {node['name']} 的代理憑證"
    return urllib.request.Request(node_url(node, path), data=data, method="POST", headers={
        "Authorization": f"Bearer {node_token(node)}", "Content-Type": ctype}), ctx


# ---- 助理（Agent）：讓模型用唯讀的工具查這個主控台管的機器，回答管理者的問題
ASSIST_TOOLS = [
    {"type": "function", "function": {"name": "list_machines", "description": "列出所有納管的機器：是否在線、記憶體架構、可用記憶體、正在跑的模型",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "machine_detail", "description": "某一台機器的詳細狀態：晶片、記憶體、各欄位跑什麼模型、能跑多大",
                                      "parameters": {"type": "object", "properties": {"machine_id": {"type": "string"}}, "required": ["machine_id"]}}},
    {"type": "function", "function": {"name": "recommend_models", "description": "依某台機器的記憶體與頻寬推薦模型，含預估速度與放不放得下",
                                      "parameters": {"type": "object", "properties": {"machine_id": {"type": "string"}}, "required": ["machine_id"]}}},
    {"type": "function", "function": {"name": "installed_models", "description": "某台機器存放區裡已下載的模型與剩餘磁碟",
                                      "parameters": {"type": "object", "properties": {"machine_id": {"type": "string"}}, "required": ["machine_id"]}}},
]
ASSIST_SYSTEM = ("你是 xConsole 模型主控台的助理，用繁體中文（台灣用語）回答管理者的問題。"
                 "需要機器或模型的資料時一律呼叫工具查，不要猜；工具查不到就說查不到。"
                 "你只能查，不能替管理者啟動、停止或下載任何東西；需要動作時，告訴他在網頁哪裡按。回答簡短、條列重點。")


def assist_tool(name, args):
    """唯讀工具。回給模型的資料不含權杖、金鑰、路徑以外的機密。"""
    fs = fleet_state()
    nodes = {n["id"]: n for n in fs["nodes"]}
    if name == "list_machines":
        return [{"machine_id": n["id"], "name": n["name"], "online": n["online"],
                 "memory_model": (n["state"] or {}).get("pools", {}).get("memory_model") if n["online"] else None,
                 "running": [f"{s.get('role')}：{s.get('model')}" for s in ((n["state"] or {}).get("slots") or []) if s.get("busy")]}
                for n in fs["nodes"]]
    n = nodes.get(str(args.get("machine_id") or ""))
    if not n:
        return {"error": "沒有這台機器", "machines": [{"machine_id": k, "name": v["name"]} for k, v in nodes.items()]}
    if not n["online"]:
        return {"error": f"{n['name']} 現在連不上"}
    st = n["state"]
    if name == "machine_detail":
        p = st["pools"]
        return {"name": n["name"], "chip": st.get("chip"), "memory_model": p["memory_model"],
                "pools": {k: p.get(k) for k in ("unified", "vram", "ram") if p.get(k)}, "capacity": n.get("capacity"),
                "slots": [{k: s.get(k) for k in ("role", "tier", "model", "quant", "active", "port", "kind")} for s in st["slots"]]}
    if name == "recommend_models":
        lc, lib = call_node(n["id"], "/library", timeout=90)
        r = recommend.recommend(st, lib if lc == 200 else {}, CATALOG.load())
        rows = []
        for it in r["items"]:
            p = next((x for x in it["plans"] if x["stem"] == (it.get("installed") or it.get("pick"))), None)
            if p:
                rows.append({"model": it["title"], "level": {"smooth": "流暢", "ok": "能跑", "disk": "要用硬碟", "no": "放不下"}.get(p["level"]),
                             "quant": p["quant"], "size_gib": p["size_gib"], "place": p["place"], "tok_s": p["tok_s"],
                             "installed": bool(it.get("installed"))})
        return {"capacity": r["capacity"], "models": rows}
    if name == "installed_models":
        lc, lib = call_node(n["id"], "/library", timeout=90)
        if lc != 200:
            return {"error": lib.get("error")}
        return {"free_gib": lib.get("free_gib"), "models": [{k: m.get(k) for k in ("name", "quant", "size_gib", "arch", "in_use", "cn_origin")}
                                                           for m in lib.get("gguf") or []]}
    return {"error": "沒有這個工具"}


def call_node(node_id, path, method="GET", body=None, timeout=40):
    cfg = fleet()
    node = next((n for n in cfg["nodes"] if n["id"] == node_id), None)
    if not node:
        return 404, {"error": f"沒有這台機器：{node_id}"}
    ctx = PUSH if node.get("link") == "push" else tls.client_context(trust_file(node_id))
    if ctx is None:
        # 沒有對方憑證就不連：不准退回不驗證或明文 http
        return 502, {"error": f"還沒信任 {node['name']} 的代理憑證，請重新執行 deploy_console.sh "
                              f"或在「設定 → 機器」匯入該機憑證", "offline": True, "untrusted": True}
    url = node_url(node, path)
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
    except agentlink.Deadline as exc:
        return 504, {"error": f"{node['name']} 超過期限沒有回：{exc}"}
    except agentlink.Offline as exc:
        return 502, {"error": f"{node['name']} 連不上：{exc}", "offline": True}
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

# ---------------------------------------------------------------- 群組設定對齊（清單 #5）
# 每分鐘（改了群組設定就馬上）把每台所屬群組的設定送過去，附主控台的時間；代理自己分「準備」與「套用」兩段做，
# 回報兩個版本號與每一步。主動連回的機器不在線就跳過（不排舊指令，等它連上再送）。
ALIGN_WAKE = threading.Event()
ALIGN_STATE = {}                  # 機器 → {"at", "state"} 或 {"at", "error"}
ALIGN_EVERY = 60


def align_once():
    cfg = fleet()
    gmap = {g["name"]: g for g in cfg.get("groups", [])}
    for n in cfg["nodes"]:
        g = gmap.get(n.get("group") or groups.DEFAULT)
        if not g:
            continue
        if n.get("link") == "push" and not LINK.online(n["id"]):
            continue
        # 預設設定的群組（例如原本的群組被刪、機器移到未分類）也要送：代理才會放掉舊群組的鎖定與限制
        # ponytail: 一台一台依序送；連不到的伺服器各等 20 秒，機器多了再改並行
        body = {"group_id": g["id"], "version": g["version"], "settings": g["settings"], "now": time.time()}
        if hf_token():
            body["hf_token"] = hf_token()        # 受限模型要下載才帶；代理只放記憶體、不寫檔
        code, out = call_node(n["id"], "/group/sync", "POST", body, timeout=20)
        if code == 200:
            ALIGN_STATE[n["id"]] = {"at": int(time.time()), "state": out}
        else:
            ALIGN_STATE[n["id"]] = {"at": int(time.time()), "error": "這台的代理版本太舊，不支援群組設定，請更新代理"
                                    if code == 404 else (out.get("error") or f"HTTP {code}")}


ALIGN_BUSY = ("queued", "preparing", "applying")


def aligner():
    while True:
        # 有機器正在準備或套用：5 秒後再問一次，畫面上的進度才跟得上；其他時候每分鐘一次
        busy = any((a.get("state") or {}).get("phase") in ALIGN_BUSY for a in list(ALIGN_STATE.values()))
        ALIGN_WAKE.wait(5 if busy else ALIGN_EVERY)
        ALIGN_WAKE.clear()
        try:
            align_once()
        except Exception as exc:  # noqa: BLE001
            print(f"群組對齊出錯：{exc}", flush=True)


def group_members(g, nodes):
    """群組總覽要的每台狀態：已準備、已套用的版本與最後幾步。"""
    out = []
    for n in nodes:
        if (n.get("group") or groups.DEFAULT) != g["name"]:
            continue
        a = ALIGN_STATE.get(n["id"]) or {}
        st = a.get("state") or {}
        same = st.get("group_id") == g["id"]
        out.append({"id": n["id"], "name": n.get("name"), "checked": a.get("at"),
                    "error": a.get("error") or (st.get("error") if same else None),
                    "prepared": st.get("prepared", 0) if same else 0, "applied": st.get("applied", 0) if same else 0,
                    "phase": st.get("phase") if same else None, "steps": (st.get("steps") or [])[-6:] if same else []})
    return out


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
# 安裝指令釘住主控台憑證的公鑰（curl --pinnedpubkey），自簽憑證也不用關驗證。
# 加入碼預設 30 分鐘、一次；教室一次發多台時可設可用次數、有效時間、名稱樣板與限定網段（console/joincodes.py）。
JOIN_FAIL_MAX = 10
JOINS = joincodes.Codes(os.path.join(STATE_DIR, "join_codes.json"))   # 只存雜湊；主控台重啟照樣有效
_JOIN_FAILS = {}
_JOIN_LOCK = threading.Lock()
AGENT_FAIL_MAX = 30
_AGENT_FAILS = {}
_AGENT_FAIL_LOCK = threading.Lock()
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
printf '%s' "$RESP" | sed '$d' | XCONSOLE_HOME="$HOME" "$PY" -c '{JOIN_SAVE_PY}'
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
                  "linger": os.environ.get("XC_LINGER", "yes") == "yes", "push": True,
                  "token": open(os.path.join(home, ".xconsole", "token")).read().strip(),
                  "cert": open(os.path.join(home, ".xconsole", "agent-cert.pem")).read()}))
"""

# 讀主控台對加入的回應：印出訊息；主控台連不進這台時，把通道的位址與通道憑證寫給代理（代理每 5 秒看一次）。
# 改由主控台連這台時，清掉以前留下的連回設定：不然重裝後的代理會繼續連回舊的主控台、聽它的指令。
# 給 bash 的 -c '...' 用，所以整段不能有單引號
JOIN_SAVE_PY = """import json, os, sys
src = open(sys.argv[1], encoding="utf-8-sig") if len(sys.argv) > 1 else sys.stdin
d = json.load(src)
print(d.get("message") or d.get("error") or d)
c = d.get("console") or {}
sd = os.path.join(os.environ.get("XCONSOLE_HOME") or os.path.expanduser("~"), ".xconsole")
if d.get("ok") and d.get("link") != "push":
    for name in ("agent-link.json", "agent-link.pem"):
        if os.path.exists(os.path.join(sd, name)):
            os.remove(os.path.join(sd, name))
if d.get("link") == "push" and c.get("url") and c.get("cert"):
    meta = {k: c.get(k) for k in ("url", "node", "current", "public", "proxy", "v")}
    for name, data in (("agent-link.pem", c["cert"]), ("agent-link.json", json.dumps(meta))):
        tmp = os.path.join(sd, name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(sd, name))
"""
assert "'" not in JOIN_SAVE_PY


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
$tf = Join-Path $base '.xconsole\\join-resp.json'
[IO.File]::WriteAllText($tf, $resp, (New-Object Text.UTF8Encoding $false))
@'
{JOIN_SAVE_PY}'@ | & $vpy - $tf
Remove-Item -Force $tf -ErrorAction SilentlyContinue
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
    """包一層：預留名額之後不論怎麼結束（含沒預料到的例外），沒加入成功就把名額放回去，不會卡在「另一台正在用」。"""
    try:
        return _join_node(handler, body if isinstance(body, dict) else {})
    finally:
        JOINS.release()


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
            JOINS.fail(j, msg)
        STORE.audit("", ip, "node.join", "", False, {"message": msg})
        return 400, {"error": msg}

    j, jname, why = JOINS.reserve(body.get("code"), ip, {n["name"] for n in fleet()["nodes"]})
    if not j:
        return fail(why)
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
        link = "pull"
        if not good:
            os.remove(tmp)
            STORE.audit("", ip, "node.join_probe", "", False, {"hosts": hosts, "port": port, "why": why})
            if not body.get("push"):
                return fail(f"主控台連不到這台的代理（試過 {', '.join(f'{h}:{port}' for h in hosts)}）。"
                            f"請確認防火牆讓主控台連得到這台的 {port} 埠，而且送來的權杖與憑證是這台代理的。", j)
            # 工作站：防火牆擋連入、IP 會換。改由這台用長輪詢連回主控台，權杖由它第一次連回時證明
            link, good = "push", ip
        nid = None

        def put(cfg):
            nonlocal nid
            # 加入一律新增一台；只有管理者產碼時指定「重新安裝哪一台」才覆寫那一筆。
            # 以前自動以位址或憑證比對「同一台」：持碼者在 IP 被接手、或主機名稱解析被偽造時能藉此搶走既有機器（資安審查）
            same = next((n for n in cfg["nodes"] if n["id"] == j.get("replace")), None)
            if same:                                   # 指定重新安裝的那台：換新的權杖、位址與憑證，名稱群組不動
                same.update(host=good, agent_port=port, link=link, token=tls.seal(CERTS.p, token.encode()))
                nid = same["id"]
                return
            taken = {n["id"] for n in cfg["nodes"]}
            # 名稱樣板（A01、A02…）拿來當代號：教室映像的主機名稱常常每台都一樣
            base = next((x for x in ((jname if j.get("names") else ""), hostname) if x and NODE_ID_RX.fullmatch(x)), "node")
            nid = base if base not in taken else next(f"{base}-{i}" for i in range(2, 999) if f"{base}-{i}" not in taken)
            cfg["nodes"].append({"id": nid, "name": jname or hostname, "host": good,
                                 "group": j.get("group") or "未分類", "agent_port": port, "link": link,
                                 "token": tls.seal(CERTS.p, token.encode())})
            groups.ensure(cfg)
        update_fleet(put)
        if link == "pull":
            os.replace(tmp, trust_file(nid))
        elif os.path.exists(trust_file(nid)):
            os.remove(trust_file(nid))             # 重新安裝成主動連回：舊的代理憑證不再用
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    name = next((n["name"] for n in fleet()["nodes"] if n["id"] == nid), nid)
    JOINS.finish(j, nid, name, ip, bool(body.get("linger")))
    STORE.audit(j["by"], ip, "node.join", nid, True, {"host": good, "port": port, "link": link, "code": j["id"],
                                                       "fingerprint256": r["summary"]["fingerprint256"]})
    if link == "pull":
        return 200, {"ok": True, "id": nid, "message": f"已加入主控台：{name}（{good}）。回到網頁就看得到這台。"}
    host = urlparse(f"https://{j['console']}").hostname or ""
    # 安裝腳本把這些寫進 ~/.xconsole/agent-link.json：代理看到就開始連回通道埠，只認發給它的通道憑證
    return 200, {"ok": True, "id": nid, "link": "push",
                 "console": dict(link_payload(), node=nid,
                                 url=f"https://{'[' + host + ']' if ':' in host else host}:{LINK_PORT}"),
                 "message": f"已加入主控台：{name}。主控台連不進這台（防火牆擋連入），改由這台主動連回，"
                            f"幾秒內網頁就看得到它。"}

# ---------------------------------------------------------------- Hugging Face 量化清單附等級
_HF_CACHE = {}                   # (機器, repo, match) → (時間, 代理 hf_profile 的回應)：讀遠端檔頭要抓十幾 MB，10 分鐘內不重抓
_HF_CACHE_LOCK = threading.Lock()


def hf_with_plans(node_id, body):
    """POST /api/node/{n}/library/hf：每個量化附 plan、整體附 pick。模型結構優先用代理讀遠端 GGUF 檔頭（hf_profile），
    讀不到就退回只有檔案清單（hf_files），用稠密模型假設估並標 guess。"""
    repo, token, match = str(body.get("repo") or "").strip(), body.get("token"), str(body.get("match") or "")[:60]
    key = (node_id, repo, match)
    with _HF_CACHE_LOCK:
        hit = _HF_CACHE.get(key)
    if hit and time.time() - hit[0] < 600 and not token:
        code, out = 200, json.loads(json.dumps(hit[1]))
    else:
        code, out = call_node(node_id, "/library/hf/profile", "POST", {"repo": repo, "token": token, "match": match}, timeout=180)
        if code == 200 and not token:
            with _HF_CACHE_LOCK:
                _HF_CACHE[key] = (time.time(), out)
                for k in [k for k, v in _HF_CACHE.items() if time.time() - v[0] > 600]:
                    _HF_CACHE.pop(k, None)
    if code != 200:
        why = out.get("error")
        code, out = call_node(node_id, "/library/hf", "POST", {"repo": repo, "token": token}, timeout=60)
        if code != 200:
            return code, out
        out["profile_error"] = why
    sc, st = call_node(node_id, "/state", timeout=30)
    if sc == 200 and st.get("pools"):
        out = recommend.plan_hf(st, out, out.get("hdd"))
    return 200, out


# ---------------------------------------------------------------- 執行預覽（模型寫出的 HTML 直接跑）
# 預覽頁跟主控台同源，所以一定要用 CSP sandbox 把它變成「不透明來源」：裡面的程式讀不到主控台的 cookie、
# 打不了主控台的 API（就算打了也是跨來源、不帶 cookie）。只有建立者那一個登入看得到、30 分鐘過期、只放記憶體。
PREVIEW_MAX = 2 * 1024 * 1024
PREVIEW_TTL = 1800
PREVIEW_KEEP = 50
_PREVIEWS = {}                   # id → {"html", "sid", "user", "exp"}（插入順序＝建立順序）
_PREVIEW_LOCK = threading.Lock()
PREVIEW_CSP = ("sandbox allow-scripts allow-forms allow-modals allow-popups; "
               "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' https: blob:; "
               "style-src 'unsafe-inline' https:; img-src data: blob: https:; font-src data: https:; "
               "media-src data: blob: https:; connect-src https:; form-action https:; base-uri 'none'; "
               "frame-ancestors 'self'")


def _sid_hash(sid):
    return hashlib.sha256((sid or "").encode()).hexdigest()


def _extract_limits():
    """抽文字的子行程限記憶體（Linux）：解析器碰上惡意檔案吃光記憶體時，被砍的是它，不是主控台。"""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    except Exception:  # noqa: BLE001  macOS 不支援 RLIMIT_AS、Windows 沒有 resource：只靠逾時
        pass


def extract_doc(name, b64):
    """對話附件抽文字：獨立行程跑 extract.py，限時 30 秒。回 (狀態碼, {text, chars, truncated, pages} 或 {error})。"""
    import subprocess
    name = str(name or "")[:200]
    if not name or not isinstance(b64, str):
        return 400, {"error": "要附檔名與內容"}
    try:
        data = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        return 400, {"error": "檔案內容不是有效的 base64"}
    if not data:
        return 400, {"error": "檔案是空的"}
    if len(data) > EXTRACT_MAX:
        return 413, {"error": "附件最大 15 MB"}
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extract.py")
    kw = {"preexec_fn": _extract_limits} if os.name == "posix" and sys.platform.startswith("linux") else {}
    try:
        p = subprocess.run([sys.executable, "-I", script, name], input=data, capture_output=True, timeout=30, **kw)
    except subprocess.TimeoutExpired:
        return 422, {"error": "這個檔案 30 秒內讀不完（太大或結構異常），請拆小一點再附"}
    try:
        out = json.loads(p.stdout.decode("utf-8") or "{}")
    except ValueError:
        out = {}
    if p.returncode != 0 or not out:
        return 422, {"error": "讀不出這個檔案（解析器中止，可能太大或檔案損毀）"}
    return (422 if out.get("error") else 200), out


def preview_put(sid, user, html):
    """回 (id, 錯誤)。"""
    if not isinstance(html, str) or not html.strip():
        return None, "沒有 HTML 內容"
    data = html.encode("utf-8")
    if len(data) > PREVIEW_MAX:
        return None, f"預覽內容最多 2 MB（這次 {len(data) / 1048576:.1f} MB）"
    pid = secrets.token_urlsafe(16)
    now = time.time()
    with _PREVIEW_LOCK:
        for k in [k for k, v in _PREVIEWS.items() if v["exp"] < now]:
            _PREVIEWS.pop(k)
        while len(_PREVIEWS) >= PREVIEW_KEEP:
            _PREVIEWS.pop(next(iter(_PREVIEWS)))
        _PREVIEWS[pid] = {"html": data, "sid": _sid_hash(sid), "user": user, "exp": now + PREVIEW_TTL}
    return pid, None


def preview_get(pid, sid):
    """只有建立者的那一個登入拿得到；別的登入、過期、不存在一律回 None（不分辨是哪一種）。"""
    with _PREVIEW_LOCK:
        p = _PREVIEWS.get(pid)
        if not p or p["exp"] < time.time():
            _PREVIEWS.pop(pid, None)
            return None
        if not sid or not hmac.compare_digest(p["sid"], _sid_hash(sid)):
            return None
        return p["html"]


# ---------------------------------------------------------------- 第三方 API（OpenAI 相容）
# 管理者在「設定 → 第三方 API」加：名稱、網址、金鑰、預設模型。金鑰 AES-256-GCM（enc:v1:）落地，
# 任何 GET 都只回 key_set，不回內容；記錄、稽核、錯誤回應前一律遮罩。
# 網址只收 https；http 只准私有位址與本機（區網裡自架的 vLLM／llama.cpp），不准 169.254（雲端 metadata）。
# 連線不跟轉址（金鑰不能被帶到別的主機）；自建 CA 的服務可以匯入對方的 CA 憑證，不提供關閉驗證。
PROVIDER_MAX = 50
PROVIDER_NAME_RX = re.compile(r"[^\x00-\x1f\x7f<>]{1,40}")
PROVIDER_MODEL_RX = re.compile(r"[\w.:/@+-]{0,120}")
# 中國來源（政府案不得使用）：只標示、不擋，使用者自己決定（內部測試可以用）
CN_HOSTS = ("deepseek", "moonshot", "kimi", "bigmodel", "zhipu", "chatglm", "dashscope", "aliyuncs", "aliyun", "qwen",
            "minimax", "minimaxi", "volces", "volcengine", "doubao", "bytedance", "hunyuan", "tencent", "baichuan",
            "baidu", "bce.", "stepfun", "sensenova", "sensetime", "xfyun", "iflytek", "siliconflow", "lingyiwanwu",
            "01.ai", "infini-ai", "modelscope", ".cn")
PROVIDER_PRESETS = [
    {"key": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "default_model": "",
     "note": "金鑰在 platform.openai.com 的 API keys 頁產生"},
    {"key": "gemini", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "default_model": "", "note": "Gemini 的 OpenAI 相容端點；金鑰在 Google AI Studio 產生"},
    {"key": "anthropic", "name": "Anthropic Claude", "base_url": "https://api.anthropic.com/v1", "default_model": "",
     "note": "Anthropic 的 OpenAI 相容端點；金鑰在 console.anthropic.com 產生"},
    {"key": "mistral", "name": "Mistral", "base_url": "https://api.mistral.ai/v1", "default_model": "",
     "note": "金鑰在 console.mistral.ai 產生"},
    {"key": "custom", "name": "自訂（OpenAI 相容）", "base_url": "", "default_model": "",
     "note": "任何講 OpenAI Chat Completions 的服務（vLLM、llama.cpp、Ollama、LiteLLM…）；網址填到 /v1 為止"},
]
SECRET_TEXT_RX = re.compile(r"(sk-ant-[\w-]{8,}|sk-[\w-]{12,}|AIza[\w-]{20,}|xc-[\w-]{20,}|Bearer\s+[\w.~+/-]{12,})")


def mask_secret(text, *keys):
    """錯誤訊息、記錄、稽核裡的金鑰一律遮掉：對方服務有時會把收到的金鑰原樣回在錯誤裡。"""
    text = str(text or "")
    for k in keys:
        if k and len(k) >= 4:
            text = text.replace(k, "[已遮罩]")
    return SECRET_TEXT_RX.sub("[已遮罩]", text)


def cn_origin_host(*words):
    low = " ".join(str(w or "").lower() for w in words)
    return any(h in low for h in CN_HOSTS)


def _private_ok(ip):
    a = ipaddress.ip_address(ip)
    return (a.is_private or a.is_loopback) and not a.is_link_local and not a.is_multicast and not a.is_unspecified


def check_provider_url(url):
    """回 (正規化後的網址, 錯誤)。"""
    url = str(url or "").strip().rstrip("/")
    if not url or len(url) > 300 or re.search(r"[\s\x00-\x1f]", url):
        return None, "網址要填，例：https://api.openai.com/v1"
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.query or u.fragment:
        return None, "網址只收 https://主機/路徑（不能帶帳密、查詢字串）"
    try:
        u.port
    except ValueError:
        return None, "埠號不對"
    if u.scheme == "http":
        try:
            ips = {i[4][0] for i in socket.getaddrinfo(u.hostname, None)}
        except OSError:
            return None, f"解析不到 {u.hostname}"
        if not ips or not all(_private_ok(i) for i in ips):
            return None, "http（不加密）只准連區網私有位址或本機；網際網路上的服務請用 https"
    return url, None


class ProviderError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def provider_key(prov):
    k = prov.get("api_key") or ""
    return tls.unseal(CERTS.p, k).decode() if k.startswith("enc:v1:") else ""


def provider_open(prov, method, path, body=None, timeout=60):
    """連第三方服務：http 時先解析、確認是私有位址才直接連那個 IP（不讓 DNS 在檢查和連線之間換位址）；
    https 一律驗憑證（可加對方的 CA）、TLS 1.2 以上。回 (連線, 回應, 金鑰)。"""
    u = urlparse(prov["base_url"])
    key = provider_key(prov)
    port = u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "http":
        try:
            ips = sorted({i[4][0] for i in socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)})
        except OSError:
            raise ProviderError(502, f"解析不到 {u.hostname}")
        if not ips or not all(_private_ok(i) for i in ips):
            raise ProviderError(400, "http 只准連區網私有位址或本機，這個網址現在解析到外部位址，已拒絕")
        conn = http.client.HTTPConnection(ips[0], port, timeout=timeout)
    else:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if prov.get("ca_pem"):
            ctx.load_verify_locations(cadata=prov["ca_pem"])        # 加上對方的 CA，不是關掉驗證
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=ctx)
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "User-Agent": "xconsole", "Host": u.netloc}
    if key:
        h["Authorization"] = f"Bearer {key}"
        if (u.hostname or "") == "api.anthropic.com" or (u.hostname or "").endswith(".anthropic.com"):
            h.update({"x-api-key": key, "anthropic-version": "2023-06-01"})   # 模型清單走它原生的 API
    try:
        conn.request(method, (u.path or "").rstrip("/") + path,
                     body=json.dumps(body).encode() if body is not None else None, headers=h)
        resp = conn.getresponse()
    except ssl.SSLError as exc:
        conn.close()
        raise ProviderError(502, f"{u.hostname} 的憑證驗證不過（{exc.reason or exc}）。自建 CA 的服務請在這筆設定匯入對方的 CA 憑證")
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        raise ProviderError(502, f"連不到 {u.hostname}：{mask_secret(exc, key)[:160]}")
    if 300 <= resp.status < 400:
        conn.close()
        raise ProviderError(502, f"{u.hostname} 回 {resp.status} 轉址；為了不把金鑰送到別處，不跟轉址。請填實際提供 API 的網址")
    return conn, resp, key


def _provider_err(resp, key):
    try:
        raw = resp.read(65536)
        d = json.loads(raw or b"{}")
        if isinstance(d, list) and d:
            d = d[0]
        e = d.get("error") if isinstance(d, dict) else None
        msg = (e.get("message") if isinstance(e, dict) else e) or (d.get("message") if isinstance(d, dict) else None) or raw[:300].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        msg = ""
    hint = {401: "金鑰不對或沒有權限", 403: "沒有權限（金鑰的權限或帳號方案）", 404: "網址或模型名稱不對",
            429: "被對方限流或額度用完"}.get(resp.status, "")
    return mask_secret(f"對方回 {resp.status}" + (f"（{hint}）" if hint else "") + (f"：{msg}" if msg else ""), key)[:400]


def provider_json(prov, method, path, body=None, timeout=120):
    """非串流的一次呼叫。回 (狀態碼, JSON)。錯誤一律 {"error": 遮罩過的訊息}。"""
    try:
        conn, resp, key = provider_open(prov, method, path, body, timeout)
    except ProviderError as exc:
        return exc.code, {"error": exc.msg}
    try:
        if resp.status >= 400:
            return (400 if resp.status < 500 else 502), {"error": _provider_err(resp, key), "status": resp.status}
        raw = resp.read(NODE_REPLY_MAX + 1)
        if len(raw) > NODE_REPLY_MAX:
            return 502, {"error": "對方回應太大"}
        try:
            return 200, json.loads(raw or b"{}")
        except ValueError:
            return 502, {"error": "對方回的不是 JSON（網址可能不是 OpenAI 相容的 API）"}
    finally:
        conn.close()


HF_TOKEN_PATHS = re.compile(r"/api/node/[\w.-]+/(?:library/(?:hf|hf/tree|hf/search|download)|sd/download)")


def hf_token():
    t = (fleet().get("hf") or {}).get("token") or ""
    try:
        return tls.unseal(CERTS.p, t).decode() if t.startswith("enc:v1:") else ""
    except Exception:  # noqa: BLE001  # 主金鑰換過或密文壞了：當作沒設，不擋下載公開模型
        return ""


def open_with_system(path):
    """用這台的系統打開檔案（桌面版更新時打開下載好的安裝檔）。"""
    import subprocess
    if os.name == "nt":
        os.startfile(path)  # noqa: S606  路徑是自己下載、核對過 sha256 的安裝檔
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif path.endswith(".AppImage") and os.environ.get("APPIMAGE"):
        # AppImage：直接換掉正在用的那個檔（執行中的不受影響），下次打開就是新版
        import shutil
        tmp = os.environ["APPIMAGE"] + ".new"
        shutil.copyfile(path, tmp)
        os.chmod(tmp, 0o700)
        os.replace(tmp, os.environ["APPIMAGE"])
        updater._job("installer_ready", message="新版已換上，從系統匣結束 xConsole 再打開就是新版")
    else:
        subprocess.Popen(["xdg-open", path])     # .deb：交給系統的軟體安裝程式


def restart_console():
    """換好新版之後結束行程，由服務管理員（systemd、launchd、Windows 排程）重新帶起。結束碼不是 0：Windows 排程要失敗才會重啟。"""
    os._exit(3)


def update_overview():
    job, cur = updater.status()
    _, sha = updater.bundled_agent()
    fl = fleet()["nodes"]

    def probe(n):
        code, out = call_node(n["id"], "/self", timeout=4)
        return {"id": n["id"], "name": n["name"], "online": code == 200, "sha256": out.get("sha256") if code == 200 else None,
                "os": out.get("os"), "current": code == 200 and out.get("sha256") == sha,
                "old_agent": code == 404}                 # 太舊的代理沒有 /self：要用安裝程式或部署腳本更新一次
    # 各機同時問、每台最多等 4 秒：幾台離線也不會讓這頁卡上好幾分鐘
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(16, len(fl)))) as ex:
        nodes = list(ex.map(probe, fl))
    return {"current": cur, "job": job, "backups": [] if DESKTOP else updater.backups(STATE_DIR), "agent_sha256": sha,
            "nodes": nodes, "repo": updater.REPO, "desktop": DESKTOP}


def chat_seal(data: bytes) -> str:
    return tls.seal(CERTS.p, data)


def chat_unseal(token: str) -> bytes:
    return tls.unseal(CERTS.p, token)


def provider_public(p):
    return {"id": p["id"], "name": p["name"], "base_url": p["base_url"], "default_model": p.get("default_model") or "",
            "key_set": bool(p.get("api_key")), "ca_set": bool(p.get("ca_pem")),
            "cn_origin": cn_origin_host(urlparse(p["base_url"]).hostname, p.get("default_model")),
            "created": p.get("created"), "updated": p.get("updated")}


def provider_save(body):
    """回 (狀態碼, 回應, 稽核用的摘要)。編輯時金鑰欄空白＝保留原金鑰；clear_key＝清掉。"""
    pid = str(body.get("id") or "")
    cur = STORE.provider_get(pid) if pid else None
    if pid and not cur:
        return 404, {"error": "沒有這筆第三方 API"}, None
    if not cur and len(STORE.provider_list()) >= PROVIDER_MAX:
        return 400, {"error": f"最多 {PROVIDER_MAX} 筆"}, None
    name = str(body.get("name") or "").strip()
    if not PROVIDER_NAME_RX.fullmatch(name):
        return 400, {"error": "名稱要 1～40 個字，不能有 < > 或控制字元"}, None
    url, err = check_provider_url(body.get("base_url"))
    if err:
        return 400, {"error": err}, None
    # 沒帶 default_model 欄位就沿用原本的（帶了空字串才是清掉）
    model = str((body.get("default_model") if "default_model" in body else (cur or {}).get("default_model")) or "").strip()
    if not PROVIDER_MODEL_RX.fullmatch(model):
        return 400, {"error": "預設模型名稱只能用英數與 . _ : / @ + -，最多 120 字"}, None
    key_in = body.get("api_key")
    key_in = "" if key_in is None else str(key_in).strip()
    if key_in and (len(key_in) > 512 or re.search(r"[\s\x00-\x1f]", key_in)):
        return 400, {"error": "金鑰格式不對（不能有空白，最多 512 字）"}, None
    if key_in:
        sealed, key_change = tls.seal(CERTS.p, key_in.encode()), "換新"
    elif body.get("clear_key"):
        sealed, key_change = "", "清除"
    else:
        sealed, key_change = (cur or {}).get("api_key") or "", "保留" if cur else "無"
    ca = (cur or {}).get("ca_pem") or ""
    if body.get("clear_ca"):
        ca = ""
    elif str(body.get("ca_pem") or "").strip():
        r, pem = tls.inspect_trusted_ca(str(body["ca_pem"]).encode())
        if not r.get("ok"):
            return 400, {"error": "CA 憑證讀不出來：" + "；".join(r.get("errors") or [])}, None
        ca = pem.decode()
    row = {"id": pid or secrets.token_hex(6), "name": name, "base_url": url, "api_key": sealed,
           "default_model": model, "ca_pem": ca}
    STORE.provider_put(row)
    saved = STORE.provider_get(row["id"])
    return 200, {"ok": True, "provider": provider_public(saved)}, {
        "name": name, "base_url": url, "default_model": model, "credential": key_change, "new": not cur,
        "ca": bool(ca), "cn_origin": provider_public(saved)["cn_origin"]}


CHAT_ROLES = ("system", "user", "assistant", "tool")


def check_messages(body):
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not 1 <= len(msgs) <= 200:
        return None, "對話內容不對"
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in CHAT_ROLES:
            return None, "對話內容不對"
    return msgs, None


def provider_chat_request(prov, body):
    """跟本機對話（代理 chat_request）同一組參數規則。回 (請求本文, 錯誤)。"""
    msgs, err = check_messages(body)
    if err:
        return None, err
    model = str(body.get("model") or prov.get("default_model") or "").strip()
    if not model or not PROVIDER_MODEL_RX.fullmatch(model):
        return None, "請先選模型（這筆第三方 API 沒有設預設模型）"
    req = {"model": model, "messages": msgs, "stream": bool(body.get("stream", True))}
    if req["stream"]:
        req["stream_options"] = {"include_usage": True}
    # 第三方只送 OpenAI 標準的取樣參數（Top K、Min P、重複懲罰是 llama.cpp 的擴充，有的服務會整筆拒收）
    try:
        req.update(chats.sampling(body, only=chats.OPENAI_STD))
    except chats.Bad as exc:
        return None, str(exc)
    if req.get("seed") == -1:
        req.pop("seed")
    if isinstance(body.get("tools"), list) and len(body["tools"]) <= 16:
        req["tools"] = body["tools"]
    return req, None


# ---------------------------------------------------------------- Agent 任務（主控台負責對話迴圈）
# 模型用 OpenAI 的 tools 格式呼叫六個工具；檔案與指令實際在那台機器的代理上做（/agent/*，只能動工作資料夾）。
# run_command 一定先停在 waiting_approval，有人按允許才送出去；每一條實際執行的指令都寫稽核。
AGENT_MAX_STEPS = 40
AGENT_MAX_SEC = 1800
AGENT_KEEP = 20
AGENT_TASKS = {}
_AGENT_LOCK = threading.Lock()
AGENT_TOOLS = [
    {"type": "function", "function": {"name": "list_files", "description": "列出工作資料夾（或其中一個子資料夾）裡的檔案",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "子資料夾的相對路徑，空白＝整個工作資料夾"}}}}},
    {"type": "function", "function": {"name": "read_file", "description": "讀一個文字檔（最多 1 MB）",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "建立或覆寫一個檔案（整份內容，最多 2 MB）；上層資料夾會自動建立",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                                                     "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "在工作資料夾裡執行一條指令（要使用者按允許才會執行）",
                                      "parameters": {"type": "object", "properties": {"command": {"type": "string"},
                                                                                      "timeout": {"type": "integer", "description": "秒，預設 120、最多 600"}},
                                                     "required": ["command"]}}},
    {"type": "function", "function": {"name": "update_plan", "description": "更新工作計畫（整份），每一步標 pending、in_progress 或 done",
                                      "parameters": {"type": "object", "properties": {"steps": {"type": "array", "items": {
                                          "type": "object", "properties": {"text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
                                          "required": ["text"]}}}, "required": ["steps"]}}},
    {"type": "function", "function": {"name": "finish", "description": "工作完成時呼叫，附上給使用者的總結",
                                      "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]
TOOLS_UNSUPPORTED_RX = re.compile(r"tool|function|jinja", re.I)


def agent_system_prompt(folder, osname):
    shell = {"windows": "Windows PowerShell", "darwin": "macOS 的 zsh", "linux": "Linux 的 bash"}.get(osname, "shell")
    return ("你是在使用者自己機器上工作的程式設計助理，用繁體中文（台灣用語）跟使用者說明。"
            f"你只能用提供的工具做事，所有檔案路徑都是相對於工作資料夾「{folder}」的路徑；指令在這個資料夾裡用 {shell} 執行。"
            "做法：先用 update_plan 列出步驟，再一步一步做，每做完一步更新計畫；需要時用 list_files、read_file 了解現況，"
            "用 write_file 寫整份檔案內容，用 run_command 執行指令（每一條都要使用者按允許，被拒絕就換別的做法或說明原因）。"
            "不要執行會刪除工作資料夾以外檔案、改系統設定或要互動輸入的指令。全部做完呼叫 finish 並寫總結。")


class AgentTask:
    def __init__(self, user, ip, node, folder, model, prompt, label):
        self.id = secrets.token_hex(6)
        self.user, self.ip, self.node, self.folder, self.model, self.prompt, self.label = user, ip, node, folder, model, prompt, label
        self.state, self.error, self.summary = "running", None, None
        self.plan, self.files, self.log, self.tree = [], {}, [], []
        self.pending, self.decision, self.always, self.stop = None, None, False, False
        self.steps, self.created, self.updated = 0, time.time(), time.time()
        self.deadline = self.created + AGENT_MAX_SEC
        self.event = threading.Event()
        self.lock = threading.Lock()

    def add(self, kind, **kw):
        with self.lock:
            self.log.append({"type": kind, "ts": time.time(), **kw})
            del self.log[:-400]
            self.updated = time.time()

    def public(self, full=True):
        with self.lock:
            out = {"id": self.id, "node": self.node, "folder": self.folder, "model": self.label, "state": self.state,
                   "prompt": self.prompt if full else self.prompt[:160], "steps": self.steps, "max_steps": AGENT_MAX_STEPS,
                   "created": self.created, "updated": self.updated, "deadline": self.deadline, "error": self.error,
                   "summary": self.summary, "always": self.always, "pending": self.pending}
            if full:
                out.update(plan=list(self.plan), log=list(self.log), tree=list(self.tree),
                           files=[{"path": k, "action": v} for k, v in self.files.items()])
            return out


def agent_model_call(t, req):
    """呼叫 Agent 用的模型（非串流）。本機欄位經代理；第三方經 provider_json。"""
    m = t.model
    if m["kind"] == "local":
        return call_node(m["node"], f"/chat/{m['slot']}", "POST", req, timeout=600)
    prov = STORE.provider_get(m["id"])
    if not prov:
        return 404, {"error": "這筆第三方 API 已經被刪掉了"}
    body = {**req, "model": m.get("model") or prov.get("default_model")}
    if not body["model"]:
        return 400, {"error": "這筆第三方 API 沒有設預設模型"}
    return provider_json(prov, "POST", "/chat/completions", body, timeout=600)


def _tree_snapshot(t):
    code, out = call_node(t.node, "/agent/fs/list", "POST", {"folder": t.folder}, timeout=60)
    if code != 200:
        return None
    rows = out.get("files") or []
    with t.lock:
        t.tree = [{"path": r["path"], "dir": r["dir"], "size": r["size"]} for r in rows]
    return {r["path"]: (r["size"], r["mtime"]) for r in rows if not r["dir"]}


def _mark_files(t, before, after):
    if before is None or after is None:
        return
    with t.lock:
        for p, v in after.items():
            if p not in before:
                t.files.setdefault(p, "created")
            elif before[p] != v and t.files.get(p) != "created":
                t.files[p] = "modified"


def _wait_approval(t, cmd, timeout):
    """停在 waiting_approval，等有人按允許／拒絕；按停止或超過 30 分鐘就不等了。回 True＝允許。"""
    with t.lock:
        t.pending = {"id": secrets.token_hex(4), "tool": "run_command", "command": cmd, "timeout": timeout,
                     "asked_at": time.time()}
        t.state, t.decision = "waiting_approval", None
        t.event.clear()
        t.updated = time.time()
    while not t.event.wait(1):
        if t.stop or time.time() > t.deadline:
            break
    with t.lock:
        d, t.pending = t.decision, None
        if t.state == "waiting_approval":
            t.state = "running"
    if not d:
        return False
    if d.get("always"):
        t.always = True
    return bool(d.get("allow"))


def agent_tool(t, name, args):
    """執行一個工具，回給模型的結果（dict）。"""
    if name == "list_files":
        code, out = call_node(t.node, "/agent/fs/list", "POST", {"folder": t.folder, "path": str(args.get("path") or "")}, timeout=60)
        if code != 200:
            return {"error": out.get("error")}
        rows = out.get("files") or []
        return {"files": [r["path"] + ("/" if r["dir"] else "") for r in rows[:400]], "truncated": len(rows) > 400 or out.get("truncated")}
    if name == "read_file":
        code, out = call_node(t.node, "/agent/fs/read", "POST", {"folder": t.folder, "path": str(args.get("path") or "")}, timeout=60)
        if code != 200:
            return {"error": out.get("error")}
        if out.get("binary"):
            return {"path": out["path"], "size": out["size"], "note": "這是二進位檔，不顯示內容"}
        c = out.get("content") or ""
        return {"path": out["path"], "size": out["size"], "content": c[:24000],
                "truncated": out.get("truncated") or len(c) > 24000}
    if name == "write_file":
        path, content = str(args.get("path") or ""), args.get("content")
        code, out = call_node(t.node, "/agent/fs/write", "POST", {"folder": t.folder, "path": path, "content": content}, timeout=60)
        if code != 200:
            return {"error": out.get("error")}
        with t.lock:
            if out.get("created"):
                t.files.setdefault(out["path"], "created")
            elif t.files.get(out["path"]) != "created":
                t.files[out["path"]] = "modified"
        _tree_snapshot(t)
        return {"ok": True, "path": out["path"], "size": out["size"], "created": out.get("created")}
    if name == "run_command":
        cmd = str(args.get("command") or "").strip()
        try:
            tmo = max(1, min(600, int(args.get("timeout") or 120)))
        except (TypeError, ValueError):
            tmo = 120
        if not cmd:
            return {"error": "指令是空的"}
        if not t.always and not _wait_approval(t, cmd, tmo):
            if t.stop or time.time() > t.deadline:
                return {"error": "任務已停止"}
            t.add("result", tool="run_command", ok=False, text="使用者拒絕執行這條指令")
            return {"denied": True, "message": "使用者拒絕執行這條指令。請換別的做法，或說明為什麼需要它"}
        before = _tree_snapshot(t)
        code, out = call_node(t.node, "/agent/exec", "POST", {"folder": t.folder, "cmd": cmd, "timeout": tmo}, timeout=tmo + 60)
        STORE.audit(t.user, t.ip, "agent.exec", f"{t.node}/{t.folder}", code == 200 and out.get("code") == 0,
                    {"task": t.id, "cmd": mask_secret(cmd)[:300], "exit": out.get("code") if code == 200 else None,
                     "timed_out": out.get("timed_out"), "message": out.get("error")})
        if code != 200:
            return {"error": out.get("error")}
        _mark_files(t, before, _tree_snapshot(t))
        return {"exit_code": out.get("code"), "timed_out": out.get("timed_out"),
                "stdout": out.get("stdout"), "stderr": out.get("stderr"), "truncated": out.get("truncated")}
    if name == "update_plan":
        steps = args.get("steps") if isinstance(args.get("steps"), list) else args.get("plan")
        if not isinstance(steps, list):
            return {"error": "steps 要是清單"}
        plan = []
        for x in steps[:30]:
            if isinstance(x, str):
                x = {"text": x}
            if isinstance(x, dict) and str(x.get("text") or x.get("step") or "").strip():
                st = x.get("status") if x.get("status") in ("pending", "in_progress", "done") else "pending"
                plan.append({"text": str(x.get("text") or x.get("step")).strip()[:200], "status": st})
        with t.lock:
            t.plan = plan
        return {"ok": True, "steps": len(plan)}
    if name == "finish":
        return {"ok": True}
    return {"error": f"沒有 {name} 這個工具"}


def agent_run(t):
    """背景執行緒：一步＝呼叫模型一次。上限 40 步、30 分鐘。"""
    convo = [{"role": "system", "content": agent_system_prompt(t.folder, t.model.get("os"))},
             {"role": "user", "content": t.prompt}]
    used_tools, bare = False, 0

    def end(state, error=None):
        with t.lock:
            if t.stop:                       # 按過停止就維持「已停止」，背景晚回來的結果不改狀態
                state, error = "stopped", None
            t.state, t.error, t.pending = state, error, None
            t.updated = time.time()
        if error:
            t.add("error", text=error)
        STORE.audit(t.user, t.ip, "agent.task_end", f"{t.node}/{t.folder}", state == "done",
                    {"task": t.id, "state": state, "steps": t.steps, "message": error})
    try:
        _tree_snapshot(t)
        while True:
            if t.stop:
                return end("stopped")
            if t.steps >= AGENT_MAX_STEPS:
                return end("failed", f"已達 {AGENT_MAX_STEPS} 步上限，任務停止；可以看已完成的部分，再交代下一件事")
            if time.time() > t.deadline:
                return end("failed", "已超過 30 分鐘上限，任務停止")
            t.steps += 1
            code, out = agent_model_call(t, {"messages": convo, "tools": AGENT_TOOLS, "stream": False,
                                             "max_tokens": 4096, "temperature": 0.2})
            if t.stop:
                return end("stopped")
            if code != 200:
                err = mask_secret((out or {}).get("error") or f"模型回 {code}")
                if not used_tools and TOOLS_UNSUPPORTED_RX.search(err):
                    return end("failed", "這個模型（或它的推理引擎）不支援工具呼叫（tools），Agent 用不了。"
                                         f"請換一個支援工具呼叫的模型。對方的訊息：{err[:200]}")
                return end("failed", f"模型回 {code}：{err[:300]}")
            msg = ((out.get("choices") or [{}])[0]).get("message") or {}
            text = (msg.get("content") or "").strip()
            calls = [c for c in (msg.get("tool_calls") or []) if isinstance(c, dict)][:3]
            if text:
                t.add("text", text=text[:8000])
            if not calls:
                if used_tools:
                    with t.lock:
                        t.summary = text or t.summary
                    return end("done")
                bare += 1
                if bare >= 2:
                    return end("failed", "模型一直沒有呼叫工具，可能不支援工具呼叫（tools）；請換一個支援的模型")
                convo += [{"role": "assistant", "content": text},
                          {"role": "user", "content": "請用提供的工具動手做（先 update_plan），全部做完呼叫 finish。"}]
                continue
            used_tools = True
            for c in calls:
                c.setdefault("id", "call_" + secrets.token_hex(4))
                c.setdefault("type", "function")
            convo.append({"role": "assistant", "content": text, "tool_calls": calls})
            finished = False
            for c in calls:
                fn = c.get("function") or {}
                name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}") if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
                except ValueError:
                    args = {}
                args = args if isinstance(args, dict) else {}
                shown = {k: (v if k != "content" else f"（{len(str(v))} 字）") for k, v in args.items()}
                t.add("tool", tool=name, args=shown)
                result = agent_tool(t, name, args)
                if t.stop:
                    return end("stopped")
                if name == "finish":
                    with t.lock:
                        t.summary = str(args.get("summary") or text or "")[:8000]
                    finished = True
                if not (name == "run_command" and result.get("denied")):
                    t.add("result", tool=name, ok="error" not in result,
                          text=json.dumps(result, ensure_ascii=False)[:4000])
                convo.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result, ensure_ascii=False)[:12000]})
            if finished:
                return end("done")
    except Exception as exc:  # noqa: BLE001
        import traceback
        print("Agent 任務出錯：\n" + traceback.format_exc(), flush=True)
        return end("failed", f"任務出錯：{type(exc).__name__}")


def agent_stop(t, audit):
    """按停止：馬上標成已停止並放開「一次一個任務」的鎖，正在跑的指令砍掉。
    ponytail: 進行中的模型請求不硬切，背景執行緒等它回來就收工、不再做下一步；要秒停得替 call_node 加可中斷的連線。"""
    with t.lock:
        was = t.state
        t.stop = True
        if was in ("running", "waiting_approval"):
            t.state, t.pending, t.updated = "stopped", None, time.time()
    t.event.set()
    if was in ("running", "waiting_approval"):
        threading.Thread(target=call_node, args=(t.node, "/agent/exec/cancel", "POST", {}), kwargs={"timeout": 20},
                         daemon=True).start()
    audit("agent.task_stop", t.id, True)
    return {"ok": True, "state": t.state}


def resolve_model(node, m):
    """Agent 與操作電腦共用：模型要是本機欄位（kind=local, node, slot）或第三方 API（kind=provider, id, model）。回 (模型, 標籤, 錯誤)。"""
    if isinstance(m, str):
        kind, _, rest = m.partition(":")
        m = {"kind": kind, "slot": rest} if kind == "local" else {"kind": kind, "id": rest.split(":", 1)[0],
                                                                    "model": rest.split(":", 1)[1] if ":" in rest else ""}
    if not isinstance(m, dict) or m.get("kind") not in ("local", "provider"):
        return None, None, "模型要是本機欄位或第三方 API"
    if m["kind"] == "local":
        slot, mnode = str(m.get("slot") or ""), str(m.get("node") or node)
        if not re.fullmatch(r"[\w.-]{1,64}", slot) or not any(n["id"] == mnode for n in fleet()["nodes"]):
            return None, None, "本機模型要指定欄位"
        return {"kind": "local", "node": mnode, "slot": slot}, f"本機 {mnode}/{slot}", None
    prov = STORE.provider_get(str(m.get("id") or ""))
    if not prov:
        return None, None, "沒有這筆第三方 API"
    name = str(m.get("model") or prov.get("default_model") or "")
    if not name or not PROVIDER_MODEL_RX.fullmatch(name):
        return None, None, "這筆第三方 API 沒有設預設模型，請指定模型"
    return {"kind": "provider", "id": prov["id"], "model": name}, f"{prov['name']}／{name}", None


def computer_start(user, ip, body, audit):
    node = str(body.get("node") or "")
    if not any(n["id"] == node for n in fleet()["nodes"]):
        return 404, {"error": "沒有這台機器"}
    model, label, err = resolve_model(node, body.get("model"))
    if err:
        return 400, {"error": err}

    def cn(nid, path, method, b, timeout):
        return call_node(nid, path, method, b, timeout=timeout)

    class _T:      # agent_model_call 只用到 .model
        pass

    def mc(t, req):
        holder = _T()
        holder.model = t.model
        return agent_model_call(holder, req)
    return computer.start(user, ip, node, model, body.get("goal"), label, cn, mc, audit)


def agent_start(user, ip, body):
    """回 (狀態碼, 回應)。"""
    node = str(body.get("node") or "")
    folder = str(body.get("folder") or "")
    prompt = str(body.get("prompt") or "").strip()
    if not any(n["id"] == node for n in fleet()["nodes"]):
        return 404, {"error": "沒有這台機器"}
    if not 1 <= len(prompt) <= 8000:
        return 400, {"error": "請寫要交代的事（最多 8000 字）"}
    m = body.get("model")
    if isinstance(m, str):                    # 也收字串：local:<欄位>、provider:<id>[:<模型>]
        kind, _, rest = m.partition(":")
        m = {"kind": kind, "slot": rest} if kind == "local" else {"kind": kind, "id": rest.split(":", 1)[0],
                                                                    "model": rest.split(":", 1)[1] if ":" in rest else ""}
    if not isinstance(m, dict) or m.get("kind") not in ("local", "provider"):
        return 400, {"error": "模型要是本機欄位（kind=local, slot）或第三方 API（kind=provider, id）"}
    if m["kind"] == "local":
        slot = str(m.get("slot") or "")
        mnode = str(m.get("node") or node)
        if not re.fullmatch(r"[\w.-]{1,64}", slot) or not any(n["id"] == mnode for n in fleet()["nodes"]):
            return 400, {"error": "本機模型要指定欄位"}
        model, label = {"kind": "local", "node": mnode, "slot": slot}, f"本機 {mnode}/{slot}"
    else:
        prov = STORE.provider_get(str(m.get("id") or ""))
        if not prov:
            return 404, {"error": "沒有這筆第三方 API"}
        name = str(m.get("model") or prov.get("default_model") or "")
        if not name or not PROVIDER_MODEL_RX.fullmatch(name):
            return 400, {"error": "這筆第三方 API 沒有設預設模型，請指定模型"}
        model, label = {"kind": "provider", "id": prov["id"], "model": name}, f"{prov['name']}／{name}"
    code, out = call_node(node, "/agent/folders", timeout=30)
    if code != 200:
        return code, {"error": out.get("error"), "disabled": out.get("disabled")}
    if folder not in [f["name"] for f in out.get("folders") or []]:
        return 400, {"error": f"沒有「{folder}」這個工作資料夾，請先建立"}
    model["os"] = out.get("os")
    with _AGENT_LOCK:
        busy = next((x for x in AGENT_TASKS.values() if x.state in ("running", "waiting_approval")), None)
        if busy:
            return 409, {"error": "已經有一個 Agent 任務在進行，請等它做完或按停止", "task": busy.id}
        t = AgentTask(user, ip, node, folder, model, prompt, label)
        AGENT_TASKS[t.id] = t
        for k in sorted(AGENT_TASKS, key=lambda k: AGENT_TASKS[k].created)[:-AGENT_KEEP]:
            if AGENT_TASKS[k].state not in ("running", "waiting_approval"):
                AGENT_TASKS.pop(k)
    threading.Thread(target=agent_run, args=(t,), daemon=True, name=f"agent-{t.id}").start()
    return 200, t.public()


# ---------------------------------------------------------------- HTTP

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}


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

    def _upload(self, node_id, ctype, agent_path="/library/upload", keys=("folder", "name", "offset", "total"),
                audit="library.upload"):
        """上傳 GGUF 的一塊：本文是檔案的位元組。不是 JSON，所以另外擋跨站：要登入、要 octet-stream、
        要帶自訂標頭 X-XConsole-Upload（跨站的表單送不出自訂標頭，fetch 送的話會先被 CORS 預檢擋下）。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not self.user or ctype != "application/octet-stream" or self.headers.get("X-XConsole-Upload") != "1" \
                or not 0 < n <= 64 * 1024 * 1024:
            # 小的本文照樣讀掉再回錯誤（沒讀完就關連線，對方收到的是連線重置、看不到原因）；大的不讀直接關：
            # 沒登入的人不能讓主控台收 64 MB
            if 0 <= n <= MAX_ANON_BODY:
                self.rfile.read(n)
            else:
                self.close_connection = True
            self._headers_done()
            return self._json(401 if not self.user else 400, {"error": "請先登入" if not self.user else "上傳的請求格式不對"})
        data = self.rfile.read(n)
        self._headers_done()
        q = parse_qs(urlparse(self.path).query)
        g = lambda k: (q.get(k) or [""])[0]  # noqa: E731
        if not re.fullmatch(r"\d{1,15}", g("offset")) or not re.fullmatch(r"\d{1,15}", g("total")):
            return self._json(400, {"error": "分塊的位置或大小不對"})
        qs = urllib.parse.urlencode({k: g(k) for k in keys})
        req, ctx = node_request(node_id, agent_path + "?" + qs, data, "application/octet-stream")
        if req is None:
            return self._json(404, {"error": ctx})
        try:
            with agent_open(req, ctx, 300) as resp:
                code, out = resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            code, out = exc.code, json.load(exc)
        except Exception as exc:  # noqa: BLE001
            return self._json(502, {"error": f"連不上那台的代理：{str(exc)[:120]}"})
        if g("offset") == "0" or out.get("done"):
            self._audit(audit, f"{node_id}/{g('folder') + '/' if g('folder') else ''}{g('name')}", code == 200,
                        {"total": int(g("total")), "done": bool(out.get("done")), "message": out.get("error")})
        return self._json(code, out)

    def _chat(self, node_id, slot_id, body):
        """網頁對話：經代理轉給模型服務，串流原樣轉給瀏覽器。對話內容不寫進稽核（只記用了哪個欄位、幾則訊息）。"""
        n_msg = len(body.get("messages") or [])

        def done(code, out=None):
            # 稽核在知道結果之後才寫，記真實的成敗（以前一進來就記成功，連不存在的機器都算用過）
            self._audit("chat", f"{node_id}/{slot_id}", code == 200, {"messages": n_msg, "status": code,
                                                                        "message": (out or {}).get("error")})
            return None if out is None else self._json(code, out)
        req, ctx = node_request(node_id, f"/chat/{slot_id}", json.dumps(body).encode())
        if req is None:
            return done(404, {"error": ctx})
        try:
            resp = agent_open(req, ctx, 600)
        except urllib.error.HTTPError as exc:
            try:
                return done(exc.code, json.load(exc))
            except Exception:  # noqa: BLE001
                return done(exc.code, {"error": f"代理回 {exc.code}"})
        except Exception as exc:  # noqa: BLE001
            return done(502, {"error": f"連不上那台的代理：{str(exc)[:120]}"})
        done(200)
        with resp:
            if "event-stream" not in (resp.headers.get("Content-Type") or ""):
                return self._json(resp.status, json.load(resp))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._sec_headers()
            self.end_headers()
            self.close_connection = True
            try:
                for line in resp:
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        return None

    def _update_upload(self, ctype):
        """手動上傳更新包（不能連外的機器）：本文就是 .tar.gz，sha256 放在網址查詢字串（管理者從 GitHub 版本頁抄來）。"""
        if not self.user:
            self._body(MAX_ANON_BODY)
            self._headers_done()
            return self._json(401, {"error": "請先登入"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        # 跟上傳模型一樣擋跨站：要 octet-stream 加自訂標頭（跨站表單送不出自訂標頭）
        if ctype != "application/octet-stream" or self.headers.get("X-XConsole-Upload") != "1":
            if 0 <= n <= MAX_ANON_BODY:
                self.rfile.read(n)
            self.close_connection = True
            return self._json(415, {"error": "只接受 application/octet-stream，並要帶 X-XConsole-Upload"})
        if not 0 < n <= updater.MAX_PKG:
            self.close_connection = True
            return self._json(413, {"error": "更新包要在 64 MB 以內"})
        data = self.rfile.read(n)
        self._headers_done()
        sha = (parse_qs(urlparse(self.path).query).get("sha256") or [""])[0]
        code, out = updater.start_update(STATE_DIR, restart_console, data=data, sha256=sha, version="手動上傳")
        self._audit("update.upload", hashlib.sha256(data).hexdigest()[:16], code == 200, {"bytes": n, "message": out.get("error")})
        return self._json(code, out)

    def _preview(self, pid):
        """執行預覽：只回給建立者那一個登入；標頭把它關進 sandbox（不透明來源），只准被主控台自己的頁面嵌入。"""
        html = preview_get(pid, self._cookie()) if self.user else None
        if html is None:
            return self._send(404, "找不到這個預覽，或已經過期（預覽保留 30 分鐘）。\n".encode(), "text/plain; charset=utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")                 # 全站其他頁仍是 DENY
        self.send_header("Content-Security-Policy", PREVIEW_CSP)
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=(), payment=()")
        # 主控台頁面帶 COEP require-corp：被嵌入的文件也要宣告 COEP。用 credentialless，CDN 的腳本不必有 CORP 標頭也載得進來
        self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if CERTS.hsts_header():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.end_headers()
        self.wfile.write(html)
        return None

    def _provider_chat(self, pid, body):
        """第三方 API 的對話：串流原樣轉給瀏覽器，格式跟本機對話（_chat）一樣（data: 一行一個 JSON、data: [DONE]）。"""
        prov = STORE.provider_get(pid)
        n_msg = len(body.get("messages") or []) if isinstance(body.get("messages"), list) else 0

        def done(code, out=None, model=None):
            self._audit("provider.chat", pid, code == 200, {"messages": n_msg, "status": code, "model": model,
                                                            "message": (out or {}).get("error")})
            return None if out is None else self._json(code, out)
        if not prov:
            return done(404, {"error": "沒有這筆第三方 API"})
        req, err = provider_chat_request(prov, body)
        if err:
            return done(400, {"error": err})
        try:
            conn, resp, key = provider_open(prov, "POST", "/chat/completions", req, timeout=600)
            if resp.status == 400 and req.get("stream_options"):
                # 有的相容端點不認 stream_options：拿掉再送一次（只是少了用量統計）
                peek = _provider_err(resp, key)
                conn.close()
                if "stream_options" in peek or "include_usage" in peek:
                    req.pop("stream_options")
                    conn, resp, key = provider_open(prov, "POST", "/chat/completions", req, timeout=600)
                else:
                    return done(400, {"error": peek}, req["model"])
        except ProviderError as exc:
            return done(exc.code, {"error": exc.msg}, req["model"])
        try:
            if resp.status >= 400:
                return done(400 if resp.status < 500 else 502, {"error": _provider_err(resp, key)}, req["model"])
            done(200, None, req["model"])
            if "event-stream" not in (resp.getheader("Content-Type") or "") or not req["stream"]:
                raw = resp.read(NODE_REPLY_MAX)
                try:
                    return self._json(200, json.loads(raw or b"{}"))
                except ValueError:
                    return self._json(502, {"error": "對方回的不是 JSON"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._sec_headers()
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    line = resp.readline(1 << 20)
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return None
        finally:
            conn.close()

    def _assist(self, body):
        """助理：模型要查資料就呼叫唯讀工具，最多來回 6 次。每一步（查了什麼）也回給畫面看。"""
        node_id, slot_id = str(body.get("node") or ""), str(body.get("slot") or "")
        msgs = [m for m in (body.get("messages") or []) if isinstance(m, dict) and m.get("role") in ("user", "assistant")][-20:]
        if not msgs:
            return self._json(400, {"error": "沒有問題"})
        extra = str(body.get("system") or "")[:8000].strip()
        try:
            samp = chats.sampling(body.get("sampling") or {})
        except chats.Bad as exc:
            return self._json(400, {"error": str(exc)})
        samp.pop("seed", None) if samp.get("seed") == -1 else None
        convo = [{"role": "system", "content": ASSIST_SYSTEM + (f"\n\n使用者另外的指示：{extra}" if extra else "")}] \
            + [{"role": m["role"], "content": str(m.get("content") or "")[:8000]} for m in msgs]
        steps, seen = [], {}

        def done(code, out):
            self._audit("assist", f"{node_id}/{slot_id}", code == 200,
                        {"messages": len(msgs), "steps": len(steps), "status": code, "message": out.get("error")})
            return self._json(code, out)
        for rnd in range(6):
            last = rnd == 5
            req = {"messages": convo, "stream": False, "max_tokens": 2048, "temperature": 0.2, **samp}
            if not last:
                req["tools"] = ASSIST_TOOLS
            else:
                # 最後一輪不給工具：小模型會一直重複查同一個工具、不肯作答，這一輪逼它用查到的資料回答
                convo.append({"role": "user", "content": "資料已經查好了，請直接根據上面查到的內容回答，不要再查。"})
            code, out = call_node(node_id, f"/chat/{slot_id}", "POST", req, timeout=600)
            if code != 200 and "tools" in req:
                # 帶工具時 llama.cpp 會用文法限制輸出格式，小模型偶爾產出不合格式的東西就整個 500：
                # 這一輪改成不帶工具、請它用已經查到的資料回答
                last = True
                req.pop("tools")
                convo.append({"role": "user", "content": "資料已經查好了，請直接根據上面查到的內容回答，不要再查。"})
                code, out = call_node(node_id, f"/chat/{slot_id}", "POST", req, timeout=600)
            if code != 200:
                return done(code, {"error": out.get("error") or f"模型回 {code}", "steps": steps})
            msg = ((out.get("choices") or [{}])[0]).get("message") or {}
            # 一次最多處理 4 個呼叫；回給模型的那則也只留這 4 個，否則下一輪會帶著沒有回覆的呼叫、被伺服器拒絕
            calls = [] if last else (msg.get("tool_calls") or [])[:4]
            if not calls:
                return done(200, {"content": msg.get("content") or "（模型沒有給出回答）", "reasoning": msg.get("reasoning_content"),
                                  "steps": steps, "usage": out.get("usage"), "timings": out.get("timings")})
            convo.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                fn = (c.get("function") or {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                args = args if isinstance(args, dict) else {}
                key = json.dumps([fn.get("name"), args], sort_keys=True, ensure_ascii=False)
                if key in seen:
                    result = {"note": "這個剛剛已經查過了，結果在前面；請直接回答"}
                else:
                    result = seen[key] = assist_tool(fn.get("name"), args)
                    steps.append({"tool": fn.get("name"), "args": args})
                convo.append({"role": "tool", "tool_call_id": c.get("id"), "content": json.dumps(result, ensure_ascii=False)[:12000]})
        return done(200, {"content": "（沒有結論）", "steps": steps})

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

    def _agent_link(self, path):
        """代理主動連回（工作站模式）：先驗節點權杖再讀本文。驗過的連線改依節點計連線數（tls.TLSServer.promote），
        幾百台工作站在 NAT 後面共用一個 IP 也不會被「同一來源 64 條」擋下。"""
        nid = str(self.headers.get("X-XConsole-Node") or "")[:40]
        got = self.headers.get("Authorization") or ""
        with _AGENT_FAIL_LOCK:
            fails = [t for t in _AGENT_FAILS.get(self.ip, []) if t > time.time() - 900]
            _AGENT_FAILS[self.ip] = fails
        node = next((n for n in fleet()["nodes"] if n["id"] == nid and n.get("link") == "push"), None)
        # 先驗權杖，限速只管驗不過的：幾百台工作站共用一個 NAT 位址時，其中一台亂送錯的權杖
        # 不能把同一個位址後面的所有工作站一起擋在外面
        ok = bool(node) and got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), node_token(node).encode())
        if not ok:
            if self._body(MAX_ANON_BODY) is None:
                return None
            self._headers_done()
            if len(fails) >= AGENT_FAIL_MAX:
                return self._json(429, {"error": "權杖錯太多次，請 15 分鐘後再試"})
            with _AGENT_FAIL_LOCK:
                _AGENT_FAILS.setdefault(self.ip, []).append(time.time())
            if not fails:                          # 每個來源每一輪只記一筆，免得灌爆稽核表
                STORE.audit("", self.ip, "agent.auth", nid, False)
            time.sleep(0.5)
            return self._json(401, {"error": "節點權杖不對，或這台不是主動連回的機器"})
        promote = getattr(self.server, "promote", None)
        if promote is not None and not promote(nid):
            self._body(MAX_ANON_BODY)
            self._headers_done()
            self.close_connection = True
            return self._json(429, {"error": "這台同時開的連線太多"})
        body = self._body(MAX_BODY)
        self._headers_done()
        if body is None:
            return self._json(413, {"error": "請求內容太大"})
        if path == "/api/agent/poll":
            lv = str(body.get("link") or "")
            link_tick(force=RING.ack(nid, int(lv.split("-")[0]) if re.fullmatch(r"\d{1,9}-[0-9a-f]{8}", lv) else 0))
            out = {"cmds": LINK.poll(nid, body.get("seen"), stop=lambda: lv != link_ver())}
            if lv != link_ver():
                out["link"] = link_payload()        # 換了通道憑證、對外網址或代理伺服器：這台下一輪就換上
            return self._json(200, out)
        return self._json(200, {"ok": LINK.result(nid, body)})

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
            j = JOINS.usable(m.group(1), self.ip)
            if not j:
                return self._send(404, "加入碼不對、已經用完、過期、被作廢，或這台不在限定的網段裡；請在主控台重新產生加入碼。\n".encode(),
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
        m = re.fullmatch(r"/preview/([A-Za-z0-9_-]{16,40})", path)
        if m:
            return self._preview(m.group(1))
        if not path.startswith("/api/"):
            return self._static(path)
        if self._need_auth():
            return None

        if path == "/api/me":
            cur = CERTS.current() or {}
            return self._json(200, {"user": self.user, "app_window": bool(getattr(self, "loopback", False)),
                                    "lan_access": bool(fleet().get("lan_access")) if DESKTOP else True, "tls": {
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
        if path == "/api/nodes/join-codes":
            return self._json(200, {"codes": JOINS.listing()})
        m = re.fullmatch(r"/api/nodes/join-code/([0-9a-f]{12})", path)
        if m:
            _, j = JOINS.by_id(m.group(1))
            if not j:
                return self._json(404, {"error": "沒有這個加入碼"})
            return self._json(200, joincodes.public(j))
        m = re.fullmatch(r"/api/node/([\w.-]+)/machine", path)
        if m:
            return self._json(*call_node(m.group(1), "/machine", timeout=60))
        m = re.fullmatch(r"/api/node/([\w.-]+)/(llama|sd)/build(/progress)?", path)
        if m:
            return self._json(*call_node(m.group(1), f"/{m.group(2)}/build{m.group(3) or ''}", timeout=60))
        m = re.fullmatch(r"/api/node/([\w.-]+)/apiinfo/([\w.-]+)", path)
        if m:
            code, out = call_node(m.group(1), f"/apiinfo/{m.group(2)}", timeout=60)
            run = out.get("run") if code == 200 else None
            if run:
                # 預估速度與推薦清單同一條公式（recommend.decode_speed），畫面不自己算
                run["tok_s"] = recommend.decode_speed(run.get("size_gib"), run.get("profile") or {}, run.get("bandwidth_gbs"))
                run["load_est_sec"] = recommend.load_seconds(run.get("size_gib"), run.get("hdd"))
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/library", path)
        if m:
            code, lib = call_node(m.group(1), "/library", timeout=90)
            if code == 200:
                # 每顆 GGUF 附等級、速度與分層（跟推薦清單同一條公式，畫面不自己算）
                sc, st = call_node(m.group(1), "/state", timeout=30)
                if sc == 200 and st.get("pools"):
                    recommend.plan_installed(st, lib)
            return self._json(code, lib)
        m = re.fullmatch(r"/api/node/([\w.-]+)/slot/([\w.-]+)/progress", path)
        if m:
            return self._json(*call_node(m.group(1), f"/slot/{m.group(2)}/progress", timeout=15))
        m = re.fullmatch(r"/api/node/([\w.-]+)/llama/args", path)
        if m:
            q = (parse_qs(urlparse(self.path).query).get("model") or [""])[0]
            return self._json(*call_node(m.group(1), "/llama/args?model=" + urllib.parse.quote(q[:400]), timeout=30))
        m = re.fullmatch(r"/api/node/([\w.-]+)/(agent|library)/browse", path)
        if m:
            q = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            return self._json(*call_node(m.group(1), f"/{m.group(2)}/browse?path=" + urllib.parse.quote(q[:1000]), timeout=30))
        m = re.fullmatch(r"/api/node/([\w.-]+)/agent/folders", path)
        if m:
            return self._json(*call_node(m.group(1), "/agent/folders", timeout=30))
        if path == "/api/agent/tasks":
            with _AGENT_LOCK:
                rows = sorted(AGENT_TASKS.values(), key=lambda x: -x.created)
            return self._json(200, {"items": [t.public(full=False) for t in rows],
                                    "limits": {"steps": AGENT_MAX_STEPS, "seconds": AGENT_MAX_SEC}})
        if path == "/api/update":
            return self._json(200, update_overview())
        m = re.fullmatch(r"/api/computer/tasks/([0-9a-f]{12})", path)
        if m:
            t = computer.TASKS.get(m.group(1))
            if not t or t.user != self.user:
                return self._json(404, {"error": "沒有這個任務"})
            after = (parse_qs(urlparse(self.path).query).get("after") or [None])[0]
            return self._json(200, t.public(int(after) if after and after.isdigit() else None))
        m = re.fullmatch(r"/api/node/([\w.-]+)/desktop/status", path)
        if m:
            return self._json(*call_node(m.group(1), "/desktop/status", timeout=20))
        m = re.fullmatch(r"/api/agent/tasks/([0-9a-f]{12})", path)
        if m:
            t = AGENT_TASKS.get(m.group(1))
            return self._json(200, t.public()) if t else self._json(404, {"error": "沒有這個任務"})
        # ---- 對話紀錄（存在主控台、加密落地；每個人只看得到自己的）
        if path == "/api/chats":
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
            return self._json(200, chats.listing(STORE, chat_unseal, self.user, q))
        m = re.fullmatch(r"/api/chats/blob/([0-9a-f]{16})", path)
        if m:
            got = chats.blob(STORE, chat_unseal, self.user, m.group(1))
            if not got:
                return self._json(404, {"error": "找不到這張圖"})
            return self._send(200, got[1], got[0], {"Cache-Control": "private, max-age=86400"})
        m = re.fullmatch(r"/api/chats/([0-9a-f]{12})", path)
        if m:
            code, out = chats.load(STORE, chat_unseal, self.user, m.group(1))
            return self._json(code, out)
        if path == "/api/providers":
            return self._json(200, {"items": [provider_public(p) for p in STORE.provider_list()],
                                    "presets": PROVIDER_PRESETS})
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
        m = re.fullmatch(r"/api/node/([\w.-]+)/sd/jobs(/[0-9a-f]{8}(?:/output(?:/[0-3])?|/init)?)?", path)
        if m:
            return self._json(*call_node(m.group(1), "/sd/jobs" + (m.group(2) or ""), timeout=60))
        m = re.fullmatch(r"/api/node/([\w.-]+)/(state|counters)", path)
        if m:
            return self._json(*call_node(m.group(1), "/" + m.group(2)))
        m = re.fullmatch(r"/api/node/([\w.-]+)/(logs|catalog)/([\w.-]+)", path)
        if m:
            return self._json(*call_node(m.group(1), f"/{m.group(2)}/{m.group(3)}"))

        if path == "/api/groups":
            cfg = fleet()
            count = {}
            for n in cfg["nodes"]:
                count[n.get("group") or groups.DEFAULT] = count.get(n.get("group") or groups.DEFAULT, 0) + 1
            out = []
            for g in cfg.get("groups", []):
                ms = group_members(g, cfg["nodes"])
                out.append(dict(g, members=count.get(g["name"], 0), nodes=ms,
                                ready=sum(1 for m in ms if m["prepared"] >= g["version"]),
                                done=sum(1 for m in ms if m["applied"] >= g["version"])))
            return self._json(200, {"groups": out,
                                    "kinds": groups.KINDS, "lockable": groups.LOCKABLE, "sources": groups.SOURCES,
                                    "defaults": groups.defaults()})
        if path == "/api/link":
            c = link_cfg()
            return self._json(200, dict(c, port=LINK_PORT, listen=LINK_LISTEN, nodes=len(push_ids()),
                                        ring=RING.summary(push_ids(), LINK.online)))
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
                                    "username": cfg["auth"].get("username", "admin"),
                                    "hf_token_set": bool((cfg.get("hf") or {}).get("token")),
                                    "desktop": DESKTOP, "app_window": bool(getattr(self, "loopback", False)),
                                    "lan_access": bool(cfg.get("lan_access")) if DESKTOP else True,
                                    "password_unset": bool(cfg.get("desktop_pw_unset")), "https_port": PORT})
        return self._json(404, {"error": "找不到"})

    # ---- POST
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        m = re.fullmatch(r"/api/node/([\w.-]+)/library/upload", path)
        if m:
            return self._upload(m.group(1), ctype)
        m = re.fullmatch(r"/api/node/([\w.-]+)/(llama|sd)/import", path)
        if m:
            return self._upload(m.group(1), ctype, f"/{m.group(2)}/import", ("name", "offset", "total"), f"{m.group(2)}.import.upload")
        if path == "/api/update/upload":
            if DESKTOP:
                # 桌面版換的是整個應用程式（含內附的 Python 與外殼），不能只換原始碼包
                self._body(MAX_ANON_BODY)
                self._headers_done()
                return self._json(400, {"error": "桌面版用安裝檔更新；請到「版本與更新」按更新，或到 GitHub 的版本頁下載安裝檔"})
            return self._update_upload(ctype)
        if path.startswith("/api/") and ctype != "application/json":
            # 跨站的 <form> 只能送 form-urlencoded／multipart／text/plain，送不出 application/json
            return self._json(415, {"error": "只接受 application/json"})
        anon = path in ("/api/login", "/api/join", "/api/logout", "/api/desktop/enter")
        if not anon and not self.user:
            # 先驗登入再讀本文：沒登入的人不能讓主控台讀 6 MB。小的本文照樣讀掉再回 401（沒讀完就關連線，
            # TCP 會送重置，對方連 401 都收不到）；超過 64 KB 的直接關
            if self._body(MAX_ANON_BODY) is None:
                return None
            self._headers_done()
            return self._json(401, {"error": "請先登入"})
        body = self._body(MAX_ANON_BODY if anon else EXTRACT_BODY if path == "/api/extract"
                          else CHAT_BODY if path == "/api/chats/save" else MAX_BODY)
        self._headers_done()             # 本文讀完才停計時：後面像換模型這種要等好幾分鐘的處理不受影響
        if body is None:
            return self._json(413, {"error": "請求內容太大"})
        if HF_TOKEN_PATHS.fullmatch(path) and not str(body.get("token") or "").strip():
            body["token"] = hf_token()          # 畫面沒另外填令牌：用設定裡存的（受限模型如 Llama、部分 Gemma 才下載得到）

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
                    left = max(0, LOGIN_MAX_PER_IP - STORE.failed_logins(since, self.ip))
                    return self._json(401, {"error": "帳號或密碼不對", "remaining": left})
            STORE.audit(user, self.ip, "auth.login", user, True)
            sid = STORE.session_new(user, self.ip, SESSION_HOURS)
            cookie = (f"xconsole={sid}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age={SESSION_HOURS * 3600}; Secure")
            return self._json(200, {"ok": True, "user": user}, cookie=cookie)
        if path == "/api/desktop/enter":
            # 應用程式開視窗時把鑰匙放在網址的 # 後面（不會送到伺服器、不進記錄），頁面讀到再送來這裡換登入
            key = str(body.get("key") or "")
            if not (getattr(self, "loopback", False) and DESKTOP_KEY and secrets.compare_digest(key, DESKTOP_KEY)):
                time.sleep(1.0)
                return self._json(403, {"error": "請從 xConsole 應用程式打開"})
            user = fleet()["auth"].get("username", "admin")
            sid = STORE.session_new(user, self.ip, 24 * 30)
            STORE.audit(user, self.ip, "auth.desktop", user, True)
            return self._json(200, {"ok": True, "user": user},
                              cookie=f"xconsole={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={24 * 30 * 3600}")
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
            if group:
                try:
                    group = groups.check_name(group)
                except ValueError as ex:
                    return self._json(400, {"error": str(ex)})
            code = secrets.token_urlsafe(24)
            try:
                j = JOINS.add(code, minutes=body.get("minutes", 30), uses=body.get("uses", 1), name=name, group=group,
                              replace=replace, cidrs=body.get("cidrs") or [], console=host, pin=console_pin(),
                              fp=console_fp(), by=self.user)
            except ValueError as ex:
                return self._json(400, {"error": str(ex)})
            self._audit("node.join_code", host, True, {"id": j["id"], "name": name, "group": group, "uses": j["uses"],
                                                        "minutes": j["minutes"], "cidrs": j["cidrs"]})
            url = f"https://{host}/install/{code}/agent.sh"
            ps = windows_command(f"https://{host}/install/{code}/agent.ps1").replace("{fp}", j["fp"])
            return self._json(200, {"id": j["id"], "expires_at": j["exp"], "uses": j["uses"], "names": j["names"][:j["uses"]],
                                    "command": f"curl -fsSk --pinnedpubkey '{j['pin']}' '{url}' | bash",
                                    "command_windows": "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
                                                       + base64.b64encode(ps.encode("utf-16-le")).decode(),
                                    "command_windows_text": ps})
        m = re.fullmatch(r"/api/nodes/join-code/([0-9a-f]{12})/revoke", path)
        if m:
            j = JOINS.revoke(m.group(1))
            if not j:
                return self._json(404, {"error": "沒有這個加入碼"})
            self._audit("node.join_code_revoke", j["id"], True, {"group": j.get("group"), "used": j["used"], "uses": j["uses"]})
            return self._json(200, {"ok": True, "code": joincodes.public(j)})
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
                LINK.forget(nid)
                RING.forget(nid)
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
            try:
                group = groups.check_name(group)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
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
            update_fleet(groups.ensure)             # 打了新的群組名稱：確定改成功才建群組物件，失敗改回時不會留下空群組
            self._audit("node.update", nid, True, {"from": old, "to": {"name": name, "group": group, "host": host,
                                                                       "agent_port": port}})
            return self._json(200, {"ok": True})
        m = re.fullmatch(r"/api/node/([\w.-]+)/(llama|sd)/import/(status|cancel|install)", path)
        if m:
            nid, kind, act = m.groups()
            want = {"name": str(body.get("name") or "")[:200]} | ({"sha256": str(body.get("sha256") or "")[:80]} if act == "install" else {})
            code, out = call_node(nid, f"/{kind}/import/{act}", "POST", want, timeout=60)
            if act != "status":
                self._audit(f"{kind}.import.{act}", f"{nid}/{want['name']}", code == 200,
                            {"sha256": want.get("sha256") or None} | ({"message": out.get("error")} if code != 200 else {}))
            return self._json(code, out)
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
                                                  "llama_server", "sd_cli", "agent_enabled", "type", "unit", "role", "tier", "port", "key", "name") if k in body}
                        | ({"message": out.get("error")} if code != 200 else {}))
            return self._json(code, out)

        # ---- 對話附件：抽出文件的文字（不存檔、不寫稽核內容，只記檔名大小）
        if path == "/api/extract":
            code, out = extract_doc(body.get("name"), body.get("b64"))
            self._audit("chat.extract", str(body.get("name") or "")[:120], code == 200,
                        {"chars": out.get("chars"), "message": out.get("error")})
            return self._json(code, out)

        # ---- 對話紀錄
        if path == "/api/chats/save":
            code, out = chats.save(STORE, chat_seal, self.user, body)
            if code != 200:
                self._audit("chat.save", str(body.get("id") or ""), False, {"message": out.get("error")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/chats/([0-9a-f]{12})/(rename|delete)", path)
        if m:
            cid, act = m.groups()
            if act == "delete":
                ok = STORE.chat_delete(self.user, cid)
                self._audit("chat.delete", cid, ok)
                return self._json(200 if ok else 404, {"ok": True} if ok else {"error": "沒有這則對話"})
            title = str(body.get("title") or "").strip()[:80]
            if not title:
                return self._json(400, {"error": "標題不能空白"})
            ok = STORE.chat_rename(self.user, cid, chat_seal(title.encode()))
            return self._json(200 if ok else 404, {"ok": True, "title": title} if ok else {"error": "沒有這則對話"})
        if path == "/api/chats/delete-all":
            n = 0
            for r in STORE.chat_list(self.user):
                n += STORE.chat_delete(self.user, r["id"])
            self._audit("chat.delete_all", "", True, {"count": n})
            return self._json(200, {"ok": True, "deleted": n})

        # ---- 執行預覽
        if path == "/api/preview":
            pid, err = preview_put(self._cookie(), self.user, body.get("html"))
            self._audit("preview.create", pid or "", not err, {"bytes": len(str(body.get("html") or "").encode()),
                                                                "message": err})
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"url": f"/preview/{pid}", "id": pid, "expires_at": time.time() + PREVIEW_TTL})
        # ---- 第三方 API
        if path == "/api/providers/save":
            code, out, det = provider_save(body)
            self._audit("provider.save", (out.get("provider") or {}).get("id") or str(body.get("id") or ""), code == 200,
                        det or {"message": out.get("error")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/providers/([0-9a-f]{12})/(delete|models|chat)", path)
        if m:
            pid, act = m.groups()
            if act == "chat":
                return self._provider_chat(pid, body)
            prov = STORE.provider_get(pid)
            if not prov:
                return self._json(404, {"error": "沒有這筆第三方 API"})
            if act == "delete":
                STORE.provider_delete(pid)
                self._audit("provider.delete", pid, True, {"name": prov["name"], "base_url": prov["base_url"]})
                return self._json(200, {"ok": True})
            t0 = time.time()
            code, out = provider_json(prov, "GET", "/models", timeout=30)
            ids = [str(x.get("id")) for x in (out.get("data") or []) if isinstance(x, dict) and x.get("id")] if code == 200 else []
            self._audit("provider.models", pid, code == 200, {"count": len(ids), "message": out.get("error")})
            if code != 200:
                return self._json(code, out)
            return self._json(200, {"ok": True, "ms": int((time.time() - t0) * 1000),
                                    "models": [{"id": i, "cn_origin": any(c in i.lower() for c in recommend.CN_ORIGIN)}
                                               for i in sorted(ids)][:500]})
        # ---- Agent：工作資料夾與任務
        m = re.fullmatch(r"/api/node/([\w.-]+)/llama/args", path)
        if m:
            code, out = call_node(m.group(1), "/llama/args", "POST", {"model": body.get("model"), "args": body.get("args")}, timeout=30)
            self._audit("llama.custom_args", f"{m.group(1)}/{body.get('model')}", code == 200,
                        {"args": out.get("args"), "message": out.get("error")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/agent/(folders/create|folders/link|folders/unlink|fs/list|fs/read)", path)
        if m:
            node_id, act = m.groups()
            want = {k: body.get(k) for k in ("name", "folder", "path") if k in body}
            code, out = call_node(node_id, f"/agent/{act}", "POST", want, timeout=60)
            if act.startswith("folders/"):
                self._audit("agent." + act.replace("folders/", "folder_"), f"{node_id}/{body.get('name') or body.get('path')}",
                            code == 200, {"message": out.get("error"), "path": out.get("path")})
            return self._json(code, out)
        if path == "/api/agent/tasks":
            code, out = agent_start(self.user, self.ip, body)
            self._audit("agent.task_start", f"{body.get('node')}/{body.get('folder')}", code == 200,
                        {"task": out.get("id"), "model": out.get("model"), "prompt_chars": len(str(body.get("prompt") or "")),
                         "message": out.get("error")})
            return self._json(code, out)
        if path == "/api/update/check":
            code, out = updater.check()
            self._audit("update.check", out.get("latest") or "", code == 200, {"message": out.get("error")})
            return self._json(code, out)
        if path == "/api/update/apply" and DESKTOP:
            # 桌面版：換的是整個應用程式（含內附的 Python），所以下載新版安裝檔、核對 sha256、打開它
            code, info = updater.check()
            inst = info.get("installer") if code == 200 else None
            if not inst or not inst.get("sha256"):
                return self._json(code if code != 200 else 400, {"error": info.get("error") or "這一版沒有這個系統的安裝檔"})
            code, out = updater.start_installer(STATE_DIR, inst["url"], inst["sha256"], info["latest"], open_with_system)
            self._audit("update.installer", info["latest"], code == 200, {"from": updater.current_version(), "message": out.get("error")})
            return self._json(code, out)
        if path == "/api/update/rollback" and DESKTOP:
            return self._json(400, {"error": "桌面版用安裝檔更新；要回舊版，請到 GitHub 的版本頁下載那一版的安裝檔"})
        if path == "/api/update/apply":
            # 網址與 sha256 一律由主控台自己問 GitHub 拿，不收畫面送來的（避免被指到別的檔）
            code, info = updater.check()
            if code != 200 or not info.get("asset") or not info["asset"].get("sha256"):
                return self._json(code if code != 200 else 400, {"error": info.get("error") or "這一版沒有可以核對的安裝包"})
            code, out = updater.start_update(STATE_DIR, restart_console, url=info["asset"]["url"], sha256=info["asset"]["sha256"],
                                             version=info["latest"])
            self._audit("update.apply", info["latest"], code == 200, {"from": updater.current_version(), "message": out.get("error")})
            return self._json(code, out)
        if path == "/api/update/rollback":
            code, out = updater.rollback(STATE_DIR, restart_console)
            self._audit("update.rollback", out.get("version") or "", code == 200, {"message": out.get("error")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/update/agent/([\w.-]+)", path)
        if m:
            data, sha = updater.bundled_agent()
            if not data:
                return self._json(404, {"error": "主控台這裡沒有隨附的代理程式"})
            code, out = call_node(m.group(1), "/self/update", "POST", {"code": base64.b64encode(data).decode(), "sha256": sha}, timeout=60)
            self._audit("update.agent", m.group(1), code == 200, {"sha256": sha[:12], "message": out.get("error")})
            return self._json(code, out)
        if path == "/api/computer/tasks":
            who, where = self.user or "", self.ip      # 背景執行緒寫稽核：先記下來，不能事後再讀 self（連線會被下一個請求重用）

            def aud(action, target, ok, detail=None):
                STORE.audit(who, where, action, target, ok, detail)
            code, out = computer_start(self.user, self.ip, body, aud)
            if code != 200:
                self._audit("computer.start", str(body.get("node") or ""), False, {"message": out.get("error")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/computer/tasks/([0-9a-f]{12})/(decide|stop)", path)
        if m:
            t = computer.TASKS.get(m.group(1))
            if not t or t.user != self.user:
                return self._json(404, {"error": "沒有這個任務"})
            if m.group(2) == "stop":
                self._audit("computer.stop", t.node, True, {"task": t.id})
                return self._json(*computer.stop(t))
            code, out = computer.decide(t, body)
            self._audit("computer.decide", t.node, code == 200, {"task": t.id, "allow": body.get("allow") is True,
                                                                  "always": body.get("always") is True})
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/desktop/open", path)
        if m:
            code, out = call_node(m.group(1), "/desktop/open", "POST", {"path": body.get("path")}, timeout=20)
            self._audit("desktop.open", f"{m.group(1)}:{str(body.get('path') or '')[:200]}", code == 200, {"message": out.get("error")})
            return self._json(code, out)
        m = re.fullmatch(r"/api/agent/tasks/([0-9a-f]{12})/(approve|stop)", path)
        if m:
            t = AGENT_TASKS.get(m.group(1))
            if not t:
                return self._json(404, {"error": "沒有這個任務"})
            if m.group(2) == "stop":
                return self._json(200, agent_stop(t, self._audit))
            with t.lock:
                waiting = t.state == "waiting_approval" and t.pending
                cmd = (t.pending or {}).get("command", "")
                if waiting:
                    t.decision = {"allow": bool(body.get("allow")), "always": bool(body.get("allow") and body.get("always"))}
            if not waiting:
                return self._json(409, {"error": "這個任務現在沒有在等核准"})
            t.event.set()
            self._audit("agent.approve", t.id, True, {"allow": bool(body.get("allow")), "always": bool(body.get("always")),
                                                      "cmd": mask_secret(cmd)[:300]})
            return self._json(200, {"ok": True})
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
            want = {k: body.get(k) for k in ("pack", "native", "engine", "model", "ctx", "gpus", "force", "allow_disk",
                                             "thinking", "lan") if k in body}
            # 載幾十 GB 的模型加上失敗時的還原，最久可能各要十幾分鐘
            code, out = call_node(m.group(1), f"/slot/{m.group(2)}/model", "POST", want, timeout=2000)
            self._audit("model.swap", f"{m.group(1)}/{m.group(2)}", code == 200,
                        {"to": out.get("model") or body.get("model") or os.path.basename(str(body.get("pack", ""))),
                         "engine": body.get("engine") or "strata", "switched_engine": out.get("switched_engine"),
                         "gpus": out.get("gpus") if code == 200 else body.get("gpus"),
                         "force": bool(body.get("force")), "blocked": code == 409 and "admit" in out,
                         "allow_disk": body.get("allow_disk") is True, "disk_mode": out.get("disk_mode"),
                         "thinking": body.get("thinking"), "lan": body.get("lan"),
                         "reverted": out.get("reverted"), "message": out.get("error")})
            return self._json(code, out)
        # ---- API 串接：網址、模型名稱、上下文、金鑰（改完重啟並驗證，失敗自動換回）
        m = re.fullmatch(r"/api/node/([\w.-]+)/apiinfo/([\w.-]+)/settings", path)
        if m:
            want = {k: body.get(k) for k in ("public_url", "model_name", "max_context", "max_tokens", "key", "thinking", "lan")
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
        m = re.fullmatch(r"/api/node/([\w.-]+)/library/(hf|hf/tree|download|delete|dirs)", path)
        if m:
            node_id, act = m.groups()
            if act == "dirs":
                want = {k: str(body[k]) for k in ("add", "remove") if body.get(k)}
                code, out = call_node(node_id, "/library/dirs", "POST", want, timeout=60)
                self._audit("library.dirs", node_id, code == 200, {**want, "message": out.get("error")})
                return self._json(code, out)
            if act == "hf/tree":
                return self._json(*call_node(node_id, "/library/hf/tree", "POST",
                                             {"repo": body.get("repo"), "token": body.get("token")}, timeout=60))
            if act == "hf":
                return self._json(*hf_with_plans(node_id, body))
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
                    "generate": ("model", "prompt", "negative", "width", "height", "steps", "cfg", "sampler", "seed", "frames", "fps",
                                 "count", "strength", "init_image")}[act]
            code, out = call_node(node_id, f"/sd/{act}", "POST", {k: body.get(k) for k in keys}, timeout=120)
            if act != "admit":
                self._audit(f"sd.{act}", f"{node_id}/{body.get('id') or body.get('model')}", code == 200,
                            {"message": out.get("error")} | ({"job": out.get("id"), "size": f"{body.get('width')}x{body.get('height')}",
                                                             "count": body.get("count") or 1, "init_image": bool(body.get("init_image"))}
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
        m = re.fullmatch(r"/api/node/([\w.-]+)/library/(hf/search|upload/status|upload/cancel)", path)
        if m:
            want = {k: body.get(k) for k in ("q", "token", "folder", "name") if k in body}
            code, out = call_node(m.group(1), f"/library/{m.group(2)}", "POST", want, timeout=60)
            if m.group(2) == "upload/cancel":
                self._audit("library.upload_cancel", f"{m.group(1)}/{want.get('folder')}/{want.get('name')}", code == 200)
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/chat/([\w.-]+)", path)
        if m:
            return self._chat(m.group(1), m.group(2), body)
        if path == "/api/assistant":
            return self._assist(body)
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
        if path == "/api/groups":
            try:
                g = groups.new(body.get("name"), str(body.get("kind") or "一般"))
                if body.get("settings") is not None:
                    groups.update(g, settings=body["settings"])
                    g["version"] = 1                 # 新建的群組從 1 起算
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            err = []

            def add(cfg):
                if any(x["name"] == g["name"] for x in cfg.setdefault("groups", [])):
                    err.append("已經有同名的群組")
                    return
                cfg["groups"].append(g)
            update_fleet(add)
            if err:
                return self._json(409, {"error": err[0]})
            self._audit("group.create", g["name"], True, {"kind": g["kind"]})
            return self._json(200, {"ok": True, "group": g})
        m = re.fullmatch(r"/api/groups/([0-9a-f]{8})/(update|delete)", path)
        if m:
            gid, act = m.groups()
            out = {}

            def edit(cfg):
                g = next((x for x in cfg.get("groups", []) if x["id"] == gid), None)
                if g is None:
                    out.update(code=404, error="沒有這個群組")
                    return
                old = g["name"]
                if act == "delete":
                    if old == groups.DEFAULT:
                        out.update(code=400, error="「未分類」是預設群組，不能刪除")
                        return
                    cfg["groups"] = [x for x in cfg["groups"] if x["id"] != gid]
                    moved = [n["id"] for n in cfg["nodes"] if n.get("group") == old]
                    for n in cfg["nodes"]:
                        if n.get("group") == old:
                            n["group"] = groups.DEFAULT     # 機器不刪，移到「未分類」
                    groups.ensure(cfg)
                    out.update(code=200, name=old, moved=moved)
                    return
                try:
                    name, changed = groups.update(g, body.get("name"), body.get("kind"), body.get("settings"),
                                                  taken={x["name"] for x in cfg["groups"] if x["id"] != gid})
                except ValueError as exc:
                    out.update(code=409 if "同名" in str(exc) else 400, error=str(exc))
                    return
                for n in cfg["nodes"]:
                    if n.get("group") == old:
                        n["group"] = name               # 改名時成員一起改，不會掉出群組
                out.update(code=200, group=dict(g), changed=changed, old=old)
            update_fleet(edit)
            if out.get("code") != 200:
                return self._json(out.get("code", 400), {"error": out.get("error")})
            if act == "delete":
                self._audit("group.delete", out["name"], True, {"moved": len(out["moved"])})
                return self._json(200, {"ok": True, "moved": len(out["moved"])})
            g = out["group"]
            self._audit("group.update", g["name"], True, {"from": out["old"], "version": g["version"], "settings_changed": out["changed"]})
            if out["changed"]:
                ALIGN_WAKE.set()                    # 改了設定：馬上送給各台，不必等下一分鐘
            return self._json(200, {"ok": True, "group": g, "changed": out["changed"]})
        if path == "/api/link/settings":
            pub = [str(a).strip() for a in body.get("public") or [] if str(a).strip()]
            proxy = str(body.get("proxy") or "").strip()
            bad = [a for a in pub if not valid_link_addr(a)]
            if bad or len(pub) > 8:
                return self._json(400, {"error": f"對外網址要寫成「主機:埠」，最多 8 個：{', '.join(bad)[:120]}"})
            if proxy and not valid_proxy(proxy):
                return self._json(400, {"error": "代理伺服器要寫成 http://主機:埠，不收帳密（要帳密請用那台的系統設定）"})
            update_fleet(lambda c: c.__setitem__("link", {"public": pub, "proxy": proxy}))
            self._audit("link.settings", "", True, {"public": pub, "proxy": proxy})
            LINK.wake()                             # 掛著的輪詢馬上回，各台立刻拿到新設定
            return self._json(200, dict(link_cfg(), ok=True))
        if path == "/api/link/rotate":
            ok, msg = RING.rotate()
            self._audit("link.rotate", "", ok, {"message": msg})
            if ok:
                LINK.wake()
                link_tick(force=True)               # 沒有在線的機器就直接換上
            return self._json(200 if ok else 409, {"ok": ok, "message": msg, "ring": RING.summary(push_ids(), LINK.online)})
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
            first = getattr(self, "loopback", False) and fleet().get("desktop_pw_unset")
            if not first and not check_credentials(user, old):
                time.sleep(1.0)
                self._audit("account.password", user, False, {"reason": "舊密碼不對"})
                return self._json(403, {"error": "目前的密碼不對"})
            if len(new) < 8:
                return self._json(400, {"error": "新密碼至少 8 碼"})
            set_credentials(user, new)             # 內含作廢所有登入
            update_fleet(lambda c: c.pop("desktop_pw_unset", None))
            self._audit("account.password", user, True, {"first": bool(first)})
            if getattr(self, "loopback", False):
                sid = STORE.session_new(user, self.ip, 24 * 30)
                return self._json(200, {"ok": True, "message": "已設定；從別台電腦登入時用這組密碼"},
                                  cookie=f"xconsole={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={24 * 30 * 3600}")
            return self._json(200, {"ok": True, "message": "已變更，請用新密碼重新登入"},
                              cookie="xconsole=; Path=/; HttpOnly; Max-Age=0; Secure")
        if path == "/api/settings/lan":
            # 桌面版的「區網存取」：打開才讓別台電腦連進來（HTTPS＋密碼），要先設好密碼。改了要重新聽，所以重啟主控台
            if not DESKTOP:
                return self._json(400, {"error": "只有桌面版有這個開關（伺服器版本來就讓區網連）"})
            on = body.get("on") is True
            if on and fleet().get("desktop_pw_unset"):
                return self._json(400, {"error": "先在下面「設定密碼」設一組密碼，別台電腦登入要用", "need_password": True})
            update_fleet(lambda c: c.__setitem__("lan_access", on))
            self._audit("settings.lan", "", True, {"on": on})
            threading.Timer(0.8, restart_console).start()
            return self._json(200, {"ok": True, "on": on, "restarting": True})
        if path == "/api/settings/hf":
            # Hugging Face 令牌：加密落地，畫面只看得到「已設定」；送 Hugging Face 時才解開（走鏡像時代理不會送）
            if body.get("clear") is True:
                update_fleet(lambda c: c.setdefault("hf", {}).pop("token", None))
                self._audit("settings.hf_token", "", True, {"cleared": True})
                return self._json(200, {"ok": True, "token_set": False})
            tok = str(body.get("token") or "").strip()
            if not re.fullmatch(r"hf_[A-Za-z0-9]{20,200}", tok):
                return self._json(400, {"error": "Hugging Face 令牌的格式是 hf_ 開頭的一串英數字（在 huggingface.co 的 Settings › Access Tokens 建立，選 Read 權限就夠）"})
            sealed = tls.seal(CERTS.p, tok.encode())
            update_fleet(lambda c: c.setdefault("hf", {}).__setitem__("token", sealed))
            self._audit("settings.hf_token", "", True, {"set": True})
            return self._json(200, {"ok": True, "token_set": True})
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


class LinkHandler(Handler):
    """通道埠（LINK_PORT）：只回代理連回的兩條路徑。對外只開這個埠時，管理網頁不會跟著露出去。"""

    def do_GET(self):  # noqa: N802
        self._headers_done()
        if urlparse(self.path).path == "/healthz":
            return self._json(200, {"ok": True, "service": "xconsole-link"})
        return self._json(404, {"error": "這個埠只給代理連回；管理網頁在另一個埠"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/api/agent/poll", "/api/agent/result"):
            return self._agent_link(path)
        self._body(MAX_ANON_BODY)
        self._headers_done()
        return self._json(404, {"error": "這個埠只給代理連回；管理網頁在另一個埠"})


class LoopHandler(Handler):
    """桌面版視窗專用：只聽 127.0.0.1。流量不出這台，所以不包 TLS；但要擋別的網頁借瀏覽器連進來：
    Host 與 Origin 一定要是 127.0.0.1:這個埠（DNS rebinding 也過不了），登入只收應用程式給的鑰匙。"""
    loopback = True

    def parse_request(self):
        if not super().parse_request():
            return False
        want = f"127.0.0.1:{APP_PORT}"
        origin = self.headers.get("Origin")
        if self.headers.get("Host") != want or (origin and origin != "http://" + want):
            self.send_error(421, "Misdirected Request")
            return False
        return True

    def send_header(self, k, v):
        if k == "Strict-Transport-Security":
            return
        if k == "Set-Cookie":
            v = v.replace("; Secure", "")         # 本機 http 收不到 Secure cookie
        super().send_header(k, v)



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


def _watch_parent(pid):
    """應用程式（父行程）不在了就跟著結束：應用程式當掉或被強制關掉時，不留下沒人管的主控台。"""
    while True:
        time.sleep(3)
        if not _pid_alive(pid):
            os._exit(0)


def _pid_alive(pid):
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259                                          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    for st in (sys.stdout, sys.stderr):
        # Windows 的主控台或管線預設 cp1252／cp950，印中文會直接例外結束：一律改 UTF-8，印不出的字換掉
        if st is not None and hasattr(st, "reconfigure"):
            st.reconfigure(encoding="utf-8", errors="replace")
    if sys.stdout is None:
        # Windows 用 pythonw 在背景跑時沒有主控台：訊息寫進記錄檔，不然出錯時什麼都看不到
        sys.stdout = sys.stderr = open(os.path.join(STATE_DIR, "console.log"), "a", encoding="utf-8", buffering=1)
    global DESKTOP_KEY
    cfg = fleet()
    if DESKTOP:
        DESKTOP_KEY = (sys.stdin.readline() if sys.stdin else "").strip()
        if len(DESKTOP_KEY) < 32 or not APP_PORT:
            raise SystemExit("桌面版要由 xConsole 應用程式帶起來（缺鑰匙或本機通道的埠）")
        if not (cfg.get("auth") or {}).get("hash"):
            # 第一次開：先放一組沒有人知道的隨機密碼（本機視窗用鑰匙進入）；要開區網存取時才請使用者自己設
            set_credentials("admin", secrets.token_urlsafe(32))
            update_fleet(lambda c: c.__setitem__("desktop_pw_unset", True))
        cfg = fleet()
        if PARENT_PID:
            threading.Thread(target=_watch_parent, args=(PARENT_PID,), daemon=True).start()
    elif not (cfg.get("auth") or {}).get("hash"):
        print("還沒設帳密。先跑：python3 xconsole_server.py --set-credentials", flush=True)
    moved = migrate_tokens()
    if groups.ensure(fleet()):
        update_fleet(groups.ensure)          # 舊版只有群組名稱：補上群組物件（版本號從 1 起）
    if moved:
        print(f"已把 {moved} 個明文代理權杖轉成密文（fleet.json）", flush=True)
    if not DESKTOP or cfg.get("lan_access"):
        wait_for_address()           # 桌面版只給這台用時不等網路位址：離線的筆電也要馬上開得起來
    fresh = CERTS.ensure(hostnames())
    bind = "0.0.0.0" if not DESKTOP or cfg.get("lan_access") else "127.0.0.1"
    srv = tls.TLSServer((bind, PORT), Handler)
    srv.certs = CERTS
    if bind == "0.0.0.0":
        try:
            lsrv = tls.TLSServer((bind, LINK_PORT), LinkHandler)
            lsrv.certs = RING
            threading.Thread(target=lsrv.serve_forever, daemon=True).start()
            LINK_LISTEN.update(ok=True, error=None)
        except OSError as exc:
            # 不擋主控台啟動：只有主動連回的工作站連不上；設定頁會顯示原因
            LINK_LISTEN.update(ok=False, error=f"通道埠 {LINK_PORT} 聽不起來：{exc}")
            print(LINK_LISTEN["error"], flush=True)
    if DESKTOP:
        loop = ThreadingHTTPServer(("127.0.0.1", APP_PORT), LoopHandler)
        loop.daemon_threads = True
        threading.Thread(target=loop.serve_forever, daemon=True).start()
        print(f"桌面版：視窗走 http://127.0.0.1:{APP_PORT}（只有這台）；區網存取{'開' if bind == '0.0.0.0' else '關'}", flush=True)
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=aligner, daemon=True).start()
    ALIGN_WAKE.set()
    cur = CERTS.current()
    kind = "暫用自簽（請到「設定 → HTTPS 與憑證」換成正式憑證）" if cur["selfSigned"] else cur["issuer"]
    print(f"xConsole 聽在 https://{bind}:{PORT}；憑證：{kind}"
          + ("（剛產生）" if fresh else "") + f"；涵蓋 {', '.join(cur['dns'] + cur['ips'])}"
          + (f"；代理通道 {LINK_PORT}" if LINK_LISTEN["ok"] else ""), flush=True)
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
