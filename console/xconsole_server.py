#!/usr/bin/env python3
"""xConsole 主控台 — 彙整各機節點代理，對人提供一個畫面。

機密處理（照 secret-handling）：
  各機代理的權杖存在 ~/.xconsole/fleet.json（0600），不走環境變數、不進網址；
  瀏覽器只拿一個 HMAC 簽章的 session cookie，看不到任何代理權杖。
憑證（照 https-certificate）：
  第一次啟動沒有正式憑證時自動產生暫用自簽，絕不只開 http。
  換正式憑證把 cert.pem／key.pem 放進 ~/.xconsole/tls/ 再重啟即可。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".xconsole")
FLEET = os.path.join(STATE_DIR, "fleet.json")
TLS_DIR = os.path.join(STATE_DIR, "tls")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = int(os.environ.get("XCONSOLE_PORT", "9400"))
SESSION_HOURS = 12


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

def fleet():
    cfg = load_json(FLEET)
    if cfg is None:
        cfg = {"nodes": [], "auth": {}, "session_secret": secrets.token_urlsafe(32)}
        save_json(FLEET, cfg)
    cfg.setdefault("session_secret", secrets.token_urlsafe(32))
    cfg.setdefault("auth", {})
    cfg.setdefault("nodes", [])
    return cfg


def set_password(password):
    cfg = fleet()
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    cfg["auth"] = {"salt": base64.b64encode(salt).decode(),
                   "hash": base64.b64encode(digest).decode(), "algo": "pbkdf2-sha256-200k"}
    save_json(FLEET, cfg)


def check_password(password):
    cfg = fleet()
    a = cfg.get("auth") or {}
    if not a.get("hash"):
        return False
    salt = base64.b64decode(a["salt"])
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return hmac.compare_digest(base64.b64encode(digest).decode(), a["hash"])


def make_session():
    cfg = fleet()
    exp = int(time.time()) + SESSION_HOURS * 3600
    payload = f"{exp}"
    sig = hmac.new(cfg["session_secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_session(cookie):
    if not cookie or "." not in cookie:
        return False
    payload, sig = cookie.rsplit(".", 1)
    cfg = fleet()
    want = hmac.new(cfg["session_secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


# ---------------------------------------------------------------- 代理呼叫

def call_node(node_id, path, method="GET", body=None, timeout=40):
    cfg = fleet()
    node = next((n for n in cfg["nodes"] if n["id"] == node_id), None)
    if not node:
        return 404, {"error": f"沒有這台機器：{node_id}"}
    url = f"http://{node['host']}:{node.get('agent_port', 9401)}{path}"
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {node['token']}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except Exception:  # noqa: BLE001
            return exc.code, {"error": f"代理回 {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"連不上 {node['name']} 的代理：{exc}", "offline": True}


# ---------------------------------------------------------------- 憑證

def ensure_tls():
    """沒有憑證就自己產一張暫用自簽，絕不退回只開 http。"""
    os.makedirs(TLS_DIR, mode=0o700, exist_ok=True)
    cert, key = os.path.join(TLS_DIR, "cert.pem"), os.path.join(TLS_DIR, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key, False
    rc = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", cert, "-days", "825",
         "-subj", "/C=TW/O=xCloudinfo/CN=xconsole",
         "-addext", "subjectAltName=DNS:xconsole,DNS:localhost,IP:127.0.0.1"],
        capture_output=True)
    if rc.returncode != 0:
        return None, None, False
    os.chmod(key, 0o600)
    return cert, key, True


# ---------------------------------------------------------------- HTTP

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "xconsole/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -------------------------------------------------- 回應

    def _json(self, code, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._sec_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _sec_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'self'")

    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        if "/" in name or ".." in name:          # 只服務 static 根目錄下的檔案
            name = os.path.basename(name)
        full = os.path.join(STATIC, name)
        if not os.path.isfile(full):
            return self._json(404, {"error": "找不到"})
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._sec_headers()
        self.end_headers()
        self.wfile.write(body)
        return None

    # -------------------------------------------------- 工具

    def _cookie(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "xconsole":
                return v
        return None

    def _authed(self):
        return valid_session(self._cookie())

    def _need_auth(self):
        if self._authed():
            return False
        self._json(401, {"error": "請先登入"})
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    # -------------------------------------------------- 路由

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {"ok": True, "service": "xconsole"})
        if not path.startswith("/api/"):
            return self._static(path)
        if self._need_auth():
            return None

        if path == "/api/me":
            return self._json(200, {"ok": True})
        if path == "/api/nodes":
            cfg = fleet()
            return self._json(200, {"nodes": [
                {k: v for k, v in n.items() if k != "token"} for n in cfg["nodes"]]})
        if path == "/api/fleet":
            return self._json(200, self._fleet_state())
        m = re.fullmatch(r"/api/node/([\w.-]+)/state", path)
        if m:
            code, body = call_node(m.group(1), "/state")
            return self._json(code, body)
        m = re.fullmatch(r"/api/node/([\w.-]+)/logs/([\w.-]+)", path)
        if m:
            code, body = call_node(m.group(1), f"/logs/{m.group(2)}")
            return self._json(code, body)
        m = re.fullmatch(r"/api/node/([\w.-]+)/catalog/([\w.-]+)", path)
        if m:
            code, body = call_node(m.group(1), f"/catalog/{m.group(2)}")
            return self._json(code, body)
        return self._json(404, {"error": "找不到"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/login":
            body = self._body()
            if not check_password(str(body.get("password") or "")):
                time.sleep(1.0)           # 慢一點，讓暴力猜測不划算
                return self._json(401, {"error": "密碼不對"})
            cookie = (f"xconsole={make_session()}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age={SESSION_HOURS * 3600}; Secure")
            return self._json(200, {"ok": True}, cookie=cookie)
        if path == "/api/logout":
            return self._json(200, {"ok": True},
                              cookie="xconsole=; Path=/; HttpOnly; Max-Age=0; Secure")
        if self._need_auth():
            return None

        body = self._body()
        m = re.fullmatch(r"/api/node/([\w.-]+)/slot/([\w.-]+)/(start|stop|restart)", path)
        if m:
            code, out = call_node(m.group(1), f"/slot/{m.group(2)}/{m.group(3)}",
                                  "POST", body, timeout=240)
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/slot/([\w.-]+)/model", path)
        if m:
            code, out = call_node(m.group(1), f"/slot/{m.group(2)}/model", "POST", body, timeout=300)
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/admit/([\w.-]+)", path)
        if m:
            code, out = call_node(m.group(1), f"/admit/{m.group(2)}", "POST", body)
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/profile/([\w.-]+)", path)
        if m:
            code, out = call_node(m.group(1), f"/profile/{m.group(2)}", "POST", body, timeout=300)
            return self._json(code, out)
        m = re.fullmatch(r"/api/node/([\w.-]+)/all/(start|stop|restart)", path)
        if m:
            code, out = call_node(m.group(1), f"/all/{m.group(2)}", "POST", body, timeout=300)
            return self._json(code, out)
        return self._json(404, {"error": "找不到"})

    # -------------------------------------------------- 彙整

    def _fleet_state(self):
        cfg = fleet()
        nodes = []
        for n in cfg["nodes"]:
            code, body = call_node(n["id"], "/state", timeout=30)
            nodes.append({
                "id": n["id"], "name": n["name"], "host": n["host"],
                "group": n.get("group", "未分類"),
                "online": code == 200,
                "error": None if code == 200 else (body.get("error") or f"代理回 {code}"),
                "state": body if code == 200 else None,
            })
        return {"ts": time.time(), "nodes": nodes}


def main():
    cfg = fleet()
    if not (cfg.get("auth") or {}).get("hash"):
        print("還沒設密碼。先跑：python3 xconsole_server.py --set-password", flush=True)
    cert, key, fresh = ensure_tls()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        kind = "暫用自簽憑證（請於設定頁換成正式憑證）" if fresh else "憑證"
        print(f"xConsole 聽在 https://0.0.0.0:{PORT}（{kind}）", flush=True)
    else:
        print("產不出憑證（缺 openssl），停止啟動，不退回只開 http", flush=True)
        raise SystemExit(2)
    srv.serve_forever()


if __name__ == "__main__":
    import sys
    if "--set-password" in sys.argv:
        import getpass
        pw = os.environ.get("XCONSOLE_INIT_PASSWORD") or getpass.getpass("設定 xConsole 密碼：")
        if len(pw) < 8:
            raise SystemExit("密碼至少 8 碼")
        set_password(pw)
        print("已設定")
        raise SystemExit(0)
    main()
