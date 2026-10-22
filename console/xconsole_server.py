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
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report  # noqa: E402
import tls  # noqa: E402
from store import Store  # noqa: E402

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".xconsole")
FLEET = os.path.join(STATE_DIR, "fleet.json")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = int(os.environ.get("XCONSOLE_PORT", "9400"))
SESSION_HOURS = 12
SAMPLE_EVERY = 300
MAX_BODY = 6 * 1024 * 1024
PBKDF2_ROUNDS = 200_000
# 登入節流：伺服器是多執行緒的，只在每次失敗後睡一秒擋不住並行猜測。
# 同一來源 15 分鐘內錯 5 次就先擋；全部來源加起來錯 50 次也擋（分散猜測）。
# 不鎖帳號：鎖帳號等於讓內網任何人都能把管理者鎖在門外。
LOGIN_WINDOW = 900
LOGIN_MAX_PER_IP = 5
LOGIN_MAX_GLOBAL = 50

os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
STORE = Store(os.path.join(STATE_DIR, "xconsole.db"))
CERTS = tls.CertManager(STATE_DIR, PORT)


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
    base = info["public_url"].rstrip("/")
    ctx = ssl.create_default_context() if base.startswith("https://") else None   # 驗證開著，不准略過
    t0 = time.time()

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        # 不跟轉址：對外網址被改成會轉到別處的位址時，金鑰不能跟著送過去
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx)) if ctx \
        else urllib.request.build_opener(NoRedirect)

    def call(method, path, with_key):
        h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if with_key else {})}
        req = urllib.request.Request(base + path, method=method, headers=h,
                                     data=b"{}" if method == "POST" else None)
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc)[:160]

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
            "detail": f"回 {st}" + ("，金鑰不對" if st == 401 else f"，模型清單 {ids}")}


# ---------------------------------------------------------------- 代理呼叫

def trust_file(node_id):
    # 路徑一律確認落在 trust 目錄內：node_id 雖然已比對過機器名單，這裡再擋一次 ../ 之類的寫法
    base = os.path.realpath(CERTS.p.trust)
    full = os.path.realpath(os.path.join(base, f"{node_id}.pem"))
    if not full.startswith(base + os.sep):
        raise ValueError("機器 id 不合法")
    return full


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
    url = f"https://{node['host']}:{node.get('agent_port', 9401)}{path}"
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {node_token(node)}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
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
                      "state": body if code == 200 else None})
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
                         "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
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

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        n = max(n, 0)            # 負數交給 read() 會一直讀到連線結束，執行緒就卡住
        if n > MAX_BODY:
            # 不讀完就回 413 並關連線：照宣稱的長度去讀，對方宣稱 10 GB 卻不送，執行緒就被占住
            self.close_connection = True
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    def _audit(self, action, target="", ok=True, detail=None):
        STORE.audit(self.user or "", self.ip, action, target, ok, detail)

    # ---- 統一錯誤處理：任何未預期的例外都不把堆疊或內部訊息回給前端，原因寫進服務記錄
    def handle_one_request(self):
        # 同一條 keep-alive 連線上的每個請求共用同一個 Handler 物件：上一個請求查到的使用者
        # 一定要清掉，否則登入前查到的「沒人」會跟到登入後，登出後查到的「有人」也會留著
        # （實際踩到：登入成功後 /api/me 仍回 401，畫面卡在登入頁）
        self.__dict__.pop("_user_cache", None)
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

    def version_string(self):
        return "xconsole"

    # ---- GET
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/healthz":
            return self._json(200, {"ok": True, "service": "xconsole"})
        if path == "/api/health":
            cur = CERTS.current() or {}
            return self._json(200, {"ok": True, "service": "xconsole", "tls": {
                "daysLeft": cur.get("daysLeft"), "validToTaipei": cur.get("validToTaipei"),
                "source": cur.get("source"), "selfSigned": cur.get("selfSigned")}})
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
        m = re.fullmatch(r"/api/node/([\w.-]+)/apiinfo/([\w.-]+)", path)
        if m:
            return self._json(*call_node(m.group(1), f"/apiinfo/{m.group(2)}", timeout=20))
        m = re.fullmatch(r"/api/node/([\w.-]+)/library", path)
        if m:
            return self._json(*call_node(m.group(1), "/library", timeout=90))
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
        body = self._body()
        if body is None:
            return self._json(413, {"error": "上傳超過 6 MB"})

        if path == "/api/login":
            user = str(body.get("username") or "")[:64]
            since = time.time() - LOGIN_WINDOW
            if (STORE.failed_logins(since, self.ip) >= LOGIN_MAX_PER_IP
                    or STORE.failed_logins(since) >= LOGIN_MAX_GLOBAL):
                STORE.audit(user, self.ip, "auth.throttled", user, False)
                return self._json(429, {"error": "登入失敗次數過多，請 15 分鐘後再試"})
            if not check_credentials(user, str(body.get("password") or "")):
                time.sleep(1.0)
                STORE.audit(user, self.ip, "auth.login", user, False)
                return self._json(401, {"error": "帳號或密碼不對"})
            STORE.audit(user, self.ip, "auth.login", user, True)
            sid = STORE.session_new(user, self.ip, SESSION_HOURS)
            cookie = (f"xconsole={sid}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age={SESSION_HOURS * 3600}; Secure")
            return self._json(200, {"ok": True, "user": user}, cookie=cookie)
        if path == "/api/logout":
            who = self.user                                   # 先記下是誰，作廢之後就查不到了
            STORE.session_end(self._cookie())                 # 伺服器端作廢，不只是清 cookie
            if who:
                STORE.audit(who, self.ip, "auth.logout", who, True)
            return self._json(200, {"ok": True}, cookie="xconsole=; Path=/; HttpOnly; Max-Age=0; Secure")
        if self._need_auth():
            return None

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
            want = {k: body.get(k) for k in ("pack", "native", "engine", "model", "ctx", "force") if k in body}
            # 載幾十 GB 的模型加上失敗時的還原，最久可能各要十幾分鐘
            code, out = call_node(m.group(1), f"/slot/{m.group(2)}/model", "POST", want, timeout=2000)
            self._audit("model.swap", f"{m.group(1)}/{m.group(2)}", code == 200,
                        {"to": out.get("model") or body.get("model") or os.path.basename(str(body.get("pack", ""))),
                         "engine": body.get("engine") or "strata", "switched_engine": out.get("switched_engine"),
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
        m = re.fullmatch(r"/api/node/([\w.-]+)/library/(hf|download|delete)", path)
        if m:
            node_id, act = m.groups()
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
    for _ in range(seconds):
        if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", tls.default_hostnames()[0]):
            return
        time.sleep(1)


def main():
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
            lines = sys.stdin.read().splitlines()
            user = (lines[0].strip() if lines else "") or "admin"
            pw = lines[1] if len(lines) > 1 else ""
        if len(pw) < 8:
            raise SystemExit("密碼至少 8 碼")
        set_credentials(user, pw)
        STORE.audit(user, "本機", "account.password", user, True, {"via": "cli"})
        print(f"已設定帳號 {user}")
        raise SystemExit(0)
    main()
