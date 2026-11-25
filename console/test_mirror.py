#!/usr/bin/env python3
"""內部鏡像與下載限速（清單 #13）。用本機的假 Hugging Face，不連外、不碰正式環境。
一、鏡像本身：照轉檔案清單並存一份（上游斷了回存的那份）、還沒存的檔先回「準備中」、核對 sha256、續傳、
    檔名跳不出存放區、先下載只挑那個量化（連同 mmproj）、下載中不能刪。
二、主控台：機器要帶自己的代理權杖才拿得到、還沒存的回 503、續傳回 206、管理頁看得到與刪得掉。
三、代理：連主控台的鏡像一定驗主控台憑證、走鏡像時 Hugging Face 權杖不送；限速與時段（含跨午夜）。
跑法：python3 test_mirror.py
"""
import hashlib
import http.cookiejar
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-mirror-agent-")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent"))
import mirror  # noqa: E402

OK = True
REPO = "test/Mirror-GGUF"


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


def blob(n, seed):
    out, h = bytearray(), seed.encode()
    while len(out) < n:
        h = hashlib.sha256(h).digest()
        out += h * 1024
    return bytes(out[:n])


FILES = {"m-Q4_K_M.gguf": blob(3_000_000, "q4"), "m-Q8_0.gguf": blob(2_000_000, "q8"),
         "mmproj-F16.gguf": blob(1_000_000, "mm"), "bad-Q2_K.gguf": blob(500_000, "bad"), "README.md": b"hello"}
SEEN = {"auth": set(), "ranges": [], "down": False}


class FakeHF(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        SEEN["auth"].add(self.headers.get("Authorization", ""))
        if SEEN["down"]:
            self.send_response(500)
            self.end_headers()
            return
        if self.path == f"/api/models/{REPO}/tree/main?recursive=true":
            tree = [{"path": p, "size": len(b), **({"lfs": {"oid": "0" * 64 if p.startswith("bad") else hashlib.sha256(b).hexdigest(),
                                                            "size": len(b)}} if p.endswith(".gguf") else {})} for p, b in FILES.items()]
            body = json.dumps(tree).encode()
        elif self.path == f"/api/models/{REPO}":
            body = json.dumps({"id": REPO, "gated": False}).encode()
        elif self.path.startswith(f"/{REPO}/resolve/main/"):
            data = FILES.get(self.path.rsplit("/", 1)[1])
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            rng = self.headers.get("Range", "")
            SEEN["ranges"].append(rng)
            start = int(rng[6:-1]) if rng.startswith("bytes=") else 0
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


hf = ThreadingHTTPServer(("127.0.0.1", 0), FakeHF)
threading.Thread(target=hf.serve_forever, daemon=True).start()
UP = f"http://127.0.0.1:{hf.server_address[1]}"

# ---- 一、鏡像本身
M = mirror.Mirror(tempfile.mkdtemp(prefix="xc-mirror-"), UP, lambda: "hf_testtoken")
code, raw = M.tree(REPO)
check("檔案清單照轉 Hugging Face，帶主控台的 Hugging Face 權杖", code == 200 and len(json.loads(raw)) == 5
      and "Bearer hf_testtoken" in SEEN["auth"])
SEEN["down"] = True
code, raw = M.tree(REPO)
check("  Hugging Face 連不到：回存的那份", code == 200 and len(json.loads(raw)) == 5)
SEEN["down"] = False
try:
    M.file(REPO, "m-Q4_K_M.gguf")
    pend = False
except mirror.Pending as exc:
    pend = "準備中" in str(exc) or "下載" in str(exc)
check("還沒存的檔：排進佇列、回準備中（機器等一下再來）", pend and (REPO, "m-Q4_K_M.gguf") in M.queue)
M.queue.clear()
M._fetch(REPO, "m-Q4_K_M.gguf")
full = M.file(REPO, "m-Q4_K_M.gguf")
with open(full, "rb") as fh:
    got = fh.read()
check("  下載完核對 sha256，之後直接給", got == FILES["m-Q4_K_M.gguf"]
      and M._index(REPO)["m-Q4_K_M.gguf"]["sha256"] == hashlib.sha256(got).hexdigest())

part = M._file(REPO, "m-Q8_0.gguf") + ".part"
os.makedirs(os.path.dirname(part), exist_ok=True)
with open(part, "wb") as fh:
    fh.write(FILES["m-Q8_0.gguf"][:700_000])
SEEN["ranges"].clear()
M._fetch(REPO, "m-Q8_0.gguf")
check("續傳：從斷掉的地方接著下載", SEEN["ranges"] == ["bytes=700000-"] and M.jobs[f"{REPO}/m-Q8_0.gguf"]["state"] == "done")

M._fetch(REPO, "bad-Q2_K.gguf")
j = M.jobs[f"{REPO}/bad-Q2_K.gguf"]
check("sha256 跟 Hugging Face 公布的不同：刪掉、不給機器", j["state"] == "failed" and "SHA-256" in j["error"]
      and "bad-Q2_K.gguf" not in M._index(REPO) and not os.path.exists(M._file(REPO, "bad-Q2_K.gguf") + ".part"))

bad = 0
for r, p in ((REPO, "../../../etc/passwd"), ("../x", "a.gguf"), (REPO, "a/../../b.gguf"), ("a/b/c", "x.gguf")):
    try:
        M.file(r, p)
    except ValueError:
        bad += 1
    except mirror.Pending:
        pass
check("檔名與模型名稱跳不出存放區", bad == 4, bad)

M.queue.clear()
got = M.prefetch(REPO, "Q4_K_M")
check("先下載：只挑那個量化，連同看圖用的 mmproj", sorted(got) == ["m-Q4_K_M.gguf", "mmproj-F16.gguf"], got)
try:
    M.delete(REPO)
    blocked = False
except ValueError:
    blocked = True
check("  還在下載（排隊中）不能刪", blocked)
M.queue.clear()
st = M.status()
check("管理頁：列出存了哪些、各多大", [r["repo"] for r in st["repos"]] == [REPO] and st["repos"][0]["size"] == 5_000_000, st["repos"])
M.delete(REPO)
check("  刪掉", M.status()["repos"] == [])

# ---- 二、主控台
W = tempfile.mkdtemp(prefix="xc-mirror-console-")
chome = os.path.join(W, "console")
os.makedirs(os.path.join(chome, ".xconsole"), mode=0o700)
port, lport = free_port(), free_port()
TOK = secrets.token_urlsafe(32)
with open(os.path.join(chome, ".xconsole", "fleet.json"), "w", encoding="utf-8") as fh:
    json.dump({"auth": {}, "nodes": [{"id": "ws-1", "name": "教室 01", "host": "127.0.0.1", "agent_port": free_port(), "group": "教室",
                                      "link": "pull", "token": TOK}]}, fh)
env = {**os.environ, "HOME": chome, "XCONSOLE_HOME": chome, "XCONSOLE_PORT": str(port), "XCONSOLE_LINK_PORT": str(lport),
       "XCONSOLE_HF_BASE": UP}
srv = os.path.join(HERE, "xconsole_server.py")
subprocess.run([sys.executable, srv, "--set-credentials"], input=b"admin\nmirror-pass-2026\n", env=env, check=True, capture_output=True)
cons = subprocess.Popen([sys.executable, srv], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(100):
        try:
            ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    with open(os.path.join(chome, ".xconsole", "tls", "cert.pem"), encoding="utf-8") as fh:
        CERT = fh.read()
    ctx = ssl.create_default_context(cadata=CERT)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx),
                                     urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    BASE = f"https://127.0.0.1:{port}"

    def get(path, tok=None, rng=None):
        h = {**({"Authorization": f"Bearer {tok}"} if tok else {}), **({"Range": rng} if rng else {})}
        try:
            with op.open(urllib.request.Request(BASE + path, headers=h), timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def call(path, body=None):
        req = urllib.request.Request(BASE + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
        try:
            with op.open(req, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    check("主控台鏡像：沒帶權杖不給", get("/mirror/ping")[0] == 401)
    check("  權杖不對不給", get("/mirror/ping", "guessed-token")[0] == 401)
    check("  帶加入的機器的代理權杖才給", get("/mirror/ping", TOK)[0] == 200)
    st, body = get(f"/mirror/api/models/{REPO}/tree/main", TOK)
    check("  檔案清單", st == 200 and len(json.loads(body)) == 5, st)
    st, body = get(f"/mirror/{REPO}/resolve/main/m-Q4_K_M.gguf", TOK)
    check("  還沒存的檔：503 準備中", st == 503 and json.loads(body).get("mirror_pending"), (st, body[:120]))
    for _ in range(60):
        st, body = get(f"/mirror/{REPO}/resolve/main/m-Q4_K_M.gguf", TOK)
        if st == 200:
            break
        time.sleep(0.5)
    check("  背景下載好之後給檔", st == 200 and body == FILES["m-Q4_K_M.gguf"], st)
    st, body = get(f"/mirror/{REPO}/resolve/main/m-Q4_K_M.gguf", TOK, "bytes=1000-")
    check("  續傳：206 從指定位置給", st == 206 and body == FILES["m-Q4_K_M.gguf"][1000:], st)
    check("  檔名跳不出存放區", get(f"/mirror/{REPO}/resolve/main/..%2F..%2F..%2Ffleet.json", TOK)[0] in (400, 404))

    call("/api/login", {"username": "admin", "password": "mirror-pass-2026"})
    st, out = call("/api/mirror")
    check("管理頁看得到存了哪些", st == 200 and [r["repo"] for r in out.get("repos", [])] == [REPO], out)
    st, out = call("/api/mirror/prefetch", {"repo": REPO, "quant": "Q8_0"})
    check("  先下載", st == 200 and out.get("files") == ["m-Q8_0.gguf", "mmproj-F16.gguf"], out)
    for _ in range(60):
        if not call("/api/mirror")[1].get("jobs"):
            break
        time.sleep(0.5)
    st, out = call("/api/mirror/delete", {"repo": REPO})
    check("  刪掉", st == 200 and call("/api/mirror")[1].get("repos") == [], out)
    check("  模型名稱不合法：400", call("/api/mirror/prefetch", {"repo": "../x"})[0] == 400)

    # ---- 三、代理
    import xconsole_agent as A  # noqa: E402
    A.TOKEN = TOK
    settings = {"sources": ["mirror"], "rate_limit": None}
    A.group_state = lambda: {"settings": settings}
    A.GROUP_MIRROR.update(urls=[f"{BASE}/mirror"], cert=CERT, base="", at=0.0)
    check("代理：群組開了內部鏡像就走主控台", A.hf_base() == f"{BASE}/mirror", A.hf_base())
    get(f"/mirror/{REPO}/resolve/main/m-Q4_K_M.gguf", TOK)
    for _ in range(60):
        try:
            with A._hf_get(f"{A.hf_base()}/{REPO}/resolve/main/m-Q4_K_M.gguf", token="hf_must_not_be_sent") as r:
                data = r.read()
            break
        except A.PeerWait:
            time.sleep(0.5)
    check("  經鏡像拿到檔；Hugging Face 權杖不送給主控台", data == FILES["m-Q4_K_M.gguf"]
          and not any("hf_must_not_be_sent" in a for a in SEEN["auth"]))
    A.GROUP_MIRROR.update(cert="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", base="", at=0.0)
    try:
        A.mirror_base()
        bad_cert = A.GROUP_MIRROR["base"] == ""
    except ssl.SSLError:
        bad_cert = True
    check("  主控台憑證對不上：不連（不退回不驗證）", bad_cert)
    settings["sources"] = ["huggingface"]
    A.GROUP_MIRROR.update(cert=CERT, base="", at=0.0)
    check("  群組沒勾內部鏡像：照舊走 Hugging Face", A.hf_base() != f"{BASE}/mirror")

    settings["rate_limit"] = {"mbps": 8, "hours": []}
    th, t0, n = A.Throttle(), time.monotonic(), 0
    while n < 2_000_000:
        k = min(th.chunk(), 2_000_000 - n)
        th.tick(k)
        n += k
    took = time.monotonic() - t0
    check("限速 8 Mbps：2 MB 至少花 1.6 秒", took >= 1.6, round(took, 2))
    real = time.localtime

    def at(wday, hh, mm):
        return lambda *_: time.struct_time((2026, 11, 25, hh, mm, 0, wday, 329, 0))
    settings["rate_limit"] = {"mbps": 8, "hours": [{"days": [1, 2, 3, 4, 5], "from": "08:00", "to": "17:00"}]}
    time.localtime = at(2, 10, 0)
    on = A.rate_cap()
    time.localtime = at(2, 18, 0)
    off = A.rate_cap()
    time.localtime = at(5, 10, 0)
    weekend = A.rate_cap()
    settings["rate_limit"] = {"mbps": 8, "hours": [{"days": [5], "from": "22:00", "to": "06:00"}]}
    time.localtime = at(5, 3, 0)
    midnight = A.rate_cap()
    time.localtime = real
    check("限速時段：上班時段限、下班與週末不限、跨午夜的時段算到隔天早上", on == 1_000_000 and off is None and weekend is None
          and midnight == 1_000_000, (on, off, weekend, midnight))
finally:
    cons.terminate()
    hf.shutdown()

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
