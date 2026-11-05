#!/usr/bin/env python3
"""交付前實測（新功能）：另起一個代理與一個主控台（各自的家目錄與埠、走 HTTPS 並驗證憑證），
經網頁實際呼叫的 API 走一遍：
  Hugging Face 量化清單附等級（假的 Hugging Face，讀遠端 GGUF 檔頭；讀不到檔頭退回稠密假設 guess）
  → 已安裝清單附等級與載入估計 → 載入進度 → 執行預覽 → 第三方 API → 打開 Agent 模式 → 建資料夾
  → Agent 任務（等核准、允許、完成）→ 稽核。
不碰正式環境（埠 19400／19401 那一套不動）。跑法：python3 test_e2e_features.py
"""
import http.cookiejar
import io
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
W = tempfile.mkdtemp(prefix="xc-e2e-feat-")
AH, CH = os.path.join(W, "agent"), os.path.join(W, "console")
OK = True
GIB = 1024 ** 3


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


def gguf_header(layers=32, ctx=32768):
    b = io.BytesIO()
    s = lambda t: struct.pack("<Q", len(t)) + t.encode()  # noqa: E731
    kv = {"general.architecture": "t", "general.name": "E2E-Dense", "t.block_count": layers, "t.context_length": ctx,
          "t.attention.head_count": 32, "t.attention.head_count_kv": 8, "t.embedding_length": 4096}
    tensors = [("token_embd.weight", 0), ("output.weight", 4096), ("blk.0.attn_q.weight", 8192)]
    b.write(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensors), len(kv)))
    for k, v in kv.items():
        b.write(s(k) + (struct.pack("<I", 8) + s(v) if isinstance(v, str) else struct.pack("<I", 4) + struct.pack("<I", v)))
    for name, off in tensors:
        b.write(s(name) + struct.pack("<I", 1) + struct.pack("<Q", 1) + struct.pack("<I", 0) + struct.pack("<Q", off))
    return b.getvalue()


HF_FILES = {"good/E2E-GGUF": {"E2E-Q4_K_M.gguf": int(4.6 * GIB), "E2E-Q8_0.gguf": int(8.5 * GIB), "E2E-F16.gguf": 16 * GIB},
            "nohead/E2E-GGUF": {"E2E-Q4_K_M.gguf": int(4.6 * GIB)}}


class FakeHF(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        p = urllib.parse.unquote(self.path.split("?")[0])
        for repo, files in HF_FILES.items():
            if p == f"/api/models/{repo}/tree/main":
                return self._json([{"type": "file", "path": k, "size": v} for k, v in files.items()])
            if p == f"/api/models/{repo}":
                return self._json({"cardData": {"license": "apache-2.0"}, "createdAt": "2026-10-01"})
            if p.startswith(f"/{repo}/resolve/main/"):
                if repo.startswith("nohead"):
                    return self._json({"error": "no"}, 404)
                name = p.rsplit("/", 1)[1]
                body = gguf_header()
                self.send_response(206)
                self.send_header("Content-Range", f"bytes 0-{len(body) - 1}/{files[name]}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
        return self._json({"error": "not found"}, 404)


SCRIPT = []


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        msg = SCRIPT.pop(0) if SCRIPT else {"role": "assistant", "content": "做完了"}
        b = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def tc(name, **args):
    return {"id": "c" + os.urandom(3).hex(), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def main():
    hf, fm = serve(FakeHF), serve(FakeModel)
    ap, cp = free_port(), free_port()
    os.makedirs(AH)
    os.makedirs(CH)
    gg = os.path.join(AH, "models", "gguf", "local--e2e")
    os.makedirs(gg)
    with open(os.path.join(gg, "E2E-Q4_K_M.gguf"), "wb") as fh:
        fh.write(gguf_header())
        fh.truncate(int(1.2 * GIB))                  # 稀疏檔，不真的占磁碟
    aenv = {**os.environ, "XCONSOLE_HOME": AH, "XCONSOLE_AGENT_PORT": str(ap), "XCONSOLE_SUPERVISOR": "1",
            "XCONSOLE_HF_BASE": f"http://127.0.0.1:{hf.server_address[1]}"}
    agent = subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py"), "--home", AH], env=aenv,
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    console = None
    try:
        cert = os.path.join(AH, ".xconsole", "agent-cert.pem")
        for _ in range(100):
            if os.path.exists(cert) and os.path.exists(os.path.join(AH, ".xconsole", "token")):
                try:
                    cx = ssl.create_default_context(cafile=cert)
                    cx.minimum_version = ssl.TLSVersion.TLSv1_2
                    cx.wrap_socket(socket.create_connection(("127.0.0.1", ap), 1), server_hostname="127.0.0.1").close()
                    break
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.2)
        token = open(os.path.join(AH, ".xconsole", "token")).read().strip()
        cenv = {**os.environ, "XCONSOLE_PORT": str(cp)}
        srv = os.path.join(HERE, "xconsole_server.py")
        subprocess.run([sys.executable, srv, "--home", CH, "--set-credentials"], input=b"admin\ne2e-feat-2026\n", env=cenv,
                       check=True, capture_output=True)
        fleet_path = os.path.join(CH, ".xconsole", "fleet.json")
        cfg = json.load(open(fleet_path))
        cfg["nodes"] = [{"id": "local", "name": "本機", "host": "127.0.0.1", "agent_port": ap, "token": token}]
        cfg.setdefault("tls", {})["hostnames"] = ["127.0.0.1"]
        json.dump(cfg, open(fleet_path, "w"))
        os.makedirs(os.path.join(CH, ".xconsole", "trust"), exist_ok=True)
        shutil.copy(cert, os.path.join(CH, ".xconsole", "trust", "local.pem"))
        console = subprocess.Popen([sys.executable, srv, "--home", CH], env=cenv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        live = os.path.join(CH, ".xconsole", "tls", "cert.pem")
        for _ in range(100):
            try:
                ssl.get_server_certificate(("127.0.0.1", cp), timeout=1)
                if os.path.exists(live):
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
        ctx = ssl.create_default_context(cafile=live)        # 驗證開著
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        base = f"https://127.0.0.1:{cp}"

        def call(path, body=None, raw=False):
            r = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                       data=json.dumps(body).encode() if body is not None else None,
                                       headers={"Content-Type": "application/json"})
            try:
                resp = op.open(r, timeout=120)
            except urllib.error.HTTPError as e:
                resp = e
            data = resp.read()
            return resp.status, (data if raw else json.loads(data or b"{}")), resp.headers

        st, _, _ = call("/api/login", {"username": "admin", "password": "e2e-feat-2026"})
        check("隔離的主控台登入（HTTPS、驗證憑證）", st == 200, st)
        st, fl, _ = call("/api/fleet")
        check("主控台經 HTTPS 連得到隔離的代理", st == 200 and fl["nodes"][0]["online"], fl["nodes"][0].get("error"))

        # ---- 1. Hugging Face 量化清單附等級
        st, out, _ = call("/api/node/local/library/hf", {"repo": "good/E2E-GGUF"})
        plans = {c["stem"]: c.get("plan") for c in out.get("choices") or []}
        check("HF 清單：每個量化都有 plan（等級、速度、放哪、需要多少、分層、guess）",
              st == 200 and plans and all(p and {"level", "tok_s", "place", "need_gib", "split", "guess"} <= set(p) for p in plans.values()), out)
        check("  讀得到遠端檔頭：guess=false、整體附 pick", not any(p["guess"] for p in plans.values()) and out.get("pick") in plans, out.get("pick"))
        st, out, _ = call("/api/node/local/library/hf", {"repo": "nohead/E2E-GGUF"})
        check("  讀不到檔頭：退回稠密假設、標 guess=true", st == 200 and out["choices"][0]["plan"]["guess"] is True and out.get("profile_error"), out)

        # ---- 2. 已安裝清單附等級
        st, lib, _ = call("/api/node/local/library")
        g = (lib.get("gguf") or [{}])[0]
        check("已安裝清單：每顆 GGUF 有 plan 與第一次載入估計秒數", st == 200 and g.get("plan") and g["plan"].get("load_est_sec") and "load_sec" in g["plan"], g.get("plan"))

        # ---- 3. 載入進度
        st, pg, _ = call("/api/node/local/slot/none/progress")
        check("載入進度：沒有這個欄位 → 404", st == 404)

        # ---- 4. 執行預覽
        st, pv, _ = call("/api/preview", {"html": "<p>e2e</p>"})
        st2, body, h = call(pv["url"], raw=True)
        check("執行預覽：建立者拿得到、CSP sandbox、X-Frame-Options SAMEORIGIN",
              st == 200 and st2 == 200 and body == b"<p>e2e</p>" and (h.get("Content-Security-Policy") or "").startswith("sandbox ")
              and h.get("X-Frame-Options") == "SAMEORIGIN", (st, st2, h.get("X-Frame-Options")))

        # ---- 5. 第三方 API ＋ 6. Agent 模式
        st, pr, _ = call("/api/providers/save", {"name": "假模型", "base_url": f"http://127.0.0.1:{fm.server_address[1]}/v1",
                                                 "api_key": "sk" + "-e2e-0000000000001", "default_model": "fake"})
        check("第三方 API 新增", st == 200, pr)
        st, out, _ = call("/api/node/local/agent/folders")
        check("Agent 模式預設關閉（403）", st == 403 and out.get("disabled"), out)
        st, out, _ = call("/api/node/local/machine", {"memory_model": "discrete", "agent_enabled": True})
        check("從「這台的設定」打開 Agent 模式", st == 200 and out["settings"]["agent_enabled"], out)
        st, mi, _ = call("/api/node/local/machine")
        check("  machine_info 回傳 agent_enabled", st == 200 and mi["settings"]["agent_enabled"] is True)
        st, out, _ = call("/api/node/local/agent/folders/create", {"name": "e2e"})
        check("建立工作資料夾", st == 200, out)
        SCRIPT[:] = [{"role": "assistant", "content": "", "tool_calls": [tc("write_file", path="a.txt", content="嗨")]},
                     {"role": "assistant", "content": "", "tool_calls": [tc("run_command", command="echo ok > b.txt")]},
                     {"role": "assistant", "content": "", "tool_calls": [tc("finish", summary="完成")]}]
        st, t, _ = call("/api/agent/tasks", {"node": "local", "folder": "e2e", "model": {"kind": "provider", "id": pr["provider"]["id"]},
                                             "prompt": "寫兩個檔"})
        check("建立 Agent 任務", st == 200, t)
        for _ in range(150):
            st, t, _ = call(f"/api/agent/tasks/{t['id']}")
            if t["state"] != "running":
                break
            time.sleep(0.2)
        check("  執行指令前停下來等核准", t["state"] == "waiting_approval", t.get("error"))
        call(f"/api/agent/tasks/{t['id']}/approve", {"allow": True})
        for _ in range(150):
            st, t, _ = call(f"/api/agent/tasks/{t['id']}")
            if t["state"] not in ("running", "waiting_approval"):
                break
            time.sleep(0.2)
        work = os.path.join(AH, "xconsole-agent", "e2e")
        check("  允許之後完成，檔案真的寫在代理那台的工作資料夾", t["state"] == "done" and os.path.exists(os.path.join(work, "a.txt"))
              and os.path.exists(os.path.join(work, "b.txt")), t.get("error"))
        st, au, _ = call("/api/admin/audit?limit=300")
        acts = {a["action"] for a in au["items"]}
        check("稽核：預覽、第三方 API、機器設定、Agent 任務與指令都有記", {"preview.create", "provider.save", "machine.settings",
                                                                        "agent.folder_create", "agent.task_start", "agent.exec"} <= acts, acts)
        dump = json.dumps(au, ensure_ascii=False)
        check("  稽核沒有代理權杖、沒有第三方金鑰", token not in dump and "sk" + "-e2e-0000000000001" not in dump)
    finally:
        for p in (console, agent):
            if p:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        hf.shutdown()
        fm.shutdown()
        shutil.rmtree(W, ignore_errors=True)
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
