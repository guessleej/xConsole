#!/usr/bin/env python3
"""群組設定對齊（清單 #5）代理這一側的檢查：準備（下載、核對 SHA-256、只准用的模型）與套用分兩段、
套用時機（立即／指定時間以主控台時鐘為準／下次開機）、鎖定強制與被改掉時拉回、換群組重來。
不碰正式環境：隔離目錄、本機假的 Hugging Face、換模型那一步用假的（只記下呼叫）。
跑法：python3 test_group.py（只用標準函式庫＋代理本身）
"""
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-group-")

GOOD = b"GGUF" + os.urandom(300_000)
FILES = {"acme/tiny-GGUF": ("tiny-Q4_K_M.gguf", GOOD, hashlib.sha256(GOOD).hexdigest()),
         "acme/bad-GGUF": ("bad-Q4_K_M.gguf", GOOD, "0" * 64)}          # 公布的 SHA-256 跟內容對不上


class FakeHF(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        for repo, (name, data, oid) in FILES.items():
            if self.path.startswith(f"/api/models/{repo}/tree/main"):
                body = json.dumps([{"type": "file", "path": name, "size": len(data),
                                    "lfs": {"oid": oid, "size": len(data)}}]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == f"/{repo}/resolve/main/{name}":
                start = int((self.headers.get("Range") or "bytes=0-")[6:].rstrip("-") or 0)
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:])
                return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


hf = ThreadingHTTPServer(("127.0.0.1", 0), FakeHF)
threading.Thread(target=hf.serve_forever, daemon=True).start()
os.environ["XCONSOLE_HF_BASE"] = f"http://127.0.0.1:{hf.server_address[1]}"
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


# ---- 換模型那一步用假的：記下呼叫，並把欄位設定檔改成指向那顆（拉回檢查看的是這個）
CALLS = []
CFG = {sid: os.path.join(A.STATE_DIR, f"{sid}.json") for sid in ("s1", "s2")}


def fake_set(sid, model_id, ctx=None, force=False, thinking=None, **_):
    m = next(x for x in A.scan_gguf()[1] if x["id"] == model_id)
    CALLS.append((sid, model_id, ctx, thinking))
    A.save_json(CFG[sid], {"model": m["path"], "ctx": ctx})
    return 200, {"ok": True}


def fake_scan(reg=None):
    """存放區裡的 .gguf（真的掃會讀檔頭，假檔讀不出來）。"""
    root, out = A.gguf_root(), []
    for d, _, fs in os.walk(root):
        for f in fs:
            if f.endswith(".gguf"):
                rel = os.path.relpath(d, root)
                name, quant = A.pretty_model(f)
                out.append({"id": f"{rel}/{f[:-5]}", "path": os.path.join(d, f), "name": name, "quant": quant})
    return None, out, []


A.set_llama_model = fake_set
A.scan_gguf = fake_scan
A.llama_server_exe = lambda reg=None: "/bin/true"
reg = A.registry()
reg["slots"] = [{"id": sid, "kind": "llama", "config": CFG[sid]} for sid in ("s1", "s2")]
A.save_json(A.REGISTRY, reg)
os.makedirs(A.gguf_root(), exist_ok=True)

TINY = "acme/tiny-GGUF:Q4_K_M"
GID = "a1b2c3d4"


def sync(v, settings, now=None, gid=GID):
    return A.group_sync({"group_id": gid, "version": v, "settings": settings, "now": now or time.time()})


def tick():
    A.group_tick()
    return A.group_state()


def main():
    case("格式不對的同步：400", sync(0, {})[0] == 400 and A.group_sync({"version": 1})[0] == 400)

    # ---- 1. 立即套用：下載、核對 SHA-256、換上、參數
    s1 = {"models": [TINY], "profile": {"ctx": 8192, "parallel": 2, "thinking": False, "temperature": 0.6},
          "locked": [], "allowed_models": [], "sources": ["huggingface"], "apply_at": {"mode": "now"}}
    code, out = sync(1, s1)
    st = tick()
    dl = next(iter(A._DL.values()))
    case("立即套用：下載完、換上，已準備＝已套用＝v1", code == 200 and st["prepared"] == 1 and st["applied"] == 1
         and st["phase"] == "done", (st["phase"], st["error"]))
    case("  下載後核對 Hugging Face 公布的 SHA-256", dl.get("verified") == ["tiny-Q4_K_M.gguf"], dl)
    case("  換到第一個欄位，帶上下文與思考關", CALLS and CALLS[-1][0] == "s1" and CALLS[-1][2] == 8192 and CALLS[-1][3] == "off", CALLS)
    m = A.fake = fake_scan()[1][0]
    args = A.llama_custom_args({"model": m["path"]})
    case("  並行數寫進欄位、溫度寫進這顆的啟動引數", next(x for x in A.registry()["slots"] if x["id"] == "s1")["parallel"] == 2
         and args[-2:] == ["--temp", "0.6"], args)
    case("  每一步都有紀錄", [x["state"] for x in st["steps"]].count("fail") == 0
         and any("SHA-256" in x["step"] for x in st["steps"]), [x["step"] for x in st["steps"]])
    n = len(CALLS)
    sync(1, s1)
    tick()
    case("同一版再送一次：不重做", len(CALLS) == n)

    # ---- 2. SHA-256 對不上：不留檔、停在準備，正在跑的不動
    code, _ = sync(2, dict(s1, models=["acme/bad-GGUF:Q4_K_M"]))
    st = tick()
    case("SHA-256 對不上：準備失敗，已套用仍是 v1，沒有去換模型", st["phase"] == "failed" and "SHA-256" in (st["error"] or "")
         and st["prepared"] == 1 and st["applied"] == 1 and len(CALLS) == n, (st["phase"], st["error"]))
    case("  對不上的檔案刪掉，不留在存放區", not any("bad" in x["path"] for x in fake_scan()[1]))

    # ---- 3. 指定時間：以主控台時鐘為準
    later = time.time() + 3600
    sync(3, dict(s1, apply_at={"mode": "at", "at": int(later)}))
    st = tick()
    case("指定時間還沒到：先準備好，不套用", st["prepared"] == 3 and st["applied"] == 1 and st["phase"] == "waiting_time"
         and len(CALLS) == n, (st["phase"], st["prepared"], st["applied"]))
    sync(3, dict(s1, apply_at={"mode": "at", "at": int(later)}), now=later + 5)     # 主控台的時鐘已經過了，本機還沒
    st = tick()
    case("  主控台時鐘到了就套用（不看本機時鐘）", st["applied"] == 3 and st["phase"] == "done", (st["phase"], st["error"]))

    # ---- 4. 下次開機
    sync(4, dict(s1, apply_at={"mode": "boot"}))
    st = tick()
    case("下次開機：這次只準備", st["prepared"] == 4 and st["applied"] == 3 and st["phase"] == "waiting_boot", st["phase"])
    A.GROUP_BOOT = "restarted"
    st = tick()
    case("  代理重新啟動後套用", st["applied"] == 4 and st["phase"] == "done", (st["phase"], st["error"]))

    # ---- 5. 鎖定：本機 API 拒絕；被改掉就拉回
    sync(5, dict(s1, locked=["models", "profile"], allowed_models=[TINY]))
    st = tick()
    case("鎖定與只准用的模型：套用", st["applied"] == 5 and A.group_lock("models") and A.group_lock("profile"), st["error"])
    A.ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(A.CERT_FILE, A.KEY_FILE)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    A.PORT = s.getsockname()[1]
    s.close()
    srv = A.AgentTLSServer(("127.0.0.1", A.PORT), A.Handler)
    srv.ctx = ctx
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cctx = ssl.create_default_context(cafile=A.CERT_FILE)
    cctx.check_hostname = False

    def post(path, body):
        c = http.client.HTTPSConnection("127.0.0.1", A.PORT, context=cctx, timeout=20)
        c.request("POST", path, body=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {A.TOKEN}", "Content-Type": "application/json"})
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out
    for path, body in (("/slot/s1/model", {"engine": "llama", "model": m["id"]}), ("/llama/args", {"model": m["id"], "args": ""}),
                       ("/profile/teaching", {}), ("/slot/s1/edit", {"name": "x"})):
        code, out = post(path, body)
        case(f"鎖定時本機改不了：{path} → 423", code == 423 and out.get("group_locked"), (code, out))
    c = http.client.HTTPSConnection("127.0.0.1", A.PORT, context=cctx, timeout=20)
    c.request("GET", "/group/state", headers={"Authorization": f"Bearer {A.TOKEN}"})
    r = c.getresponse()
    gs = json.loads(r.read())
    case("GET /group/state 回報兩個版本與鎖定項目", r.status == 200 and gs["applied"] == 5 and gs["locked"] == ["models", "profile"], gs)
    A.save_json(CFG["s1"], {"model": "/elsewhere/other.gguf"})            # 有人繞過去把模型換掉了
    n = len(CALLS)
    st = tick()
    case("鎖定的模型被改掉：下一次對齊拉回群組設定的值", len(CALLS) == n + 1 and CALLS[-1][0] == "s1"
         and any("拉回" in x["step"] for x in st["steps"]), [x["step"] for x in st["steps"]][-3:])
    case("沒被改的就不動", (tick(), len(CALLS))[1] == n + 1)
    code, out = post("/group/sync", {"group_id": GID, "version": 6, "settings": dict(s1, models=[TINY, "acme/other-GGUF:Q8_0"],
                                                                                     allowed_models=[TINY]), "now": time.time()})
    st = tick()
    case("要的模型不在只准用的清單：準備就擋下", code == 200 and st["phase"] == "failed" and "只准用" in (st["error"] or ""), st["error"])

    # ---- 6. 換群組：版本號各算各的，重新準備與套用
    sync(1, dict(s1, profile={}), gid="ffff0000")
    st = tick()
    case("換到別的群組：從那個群組的 v1 重新來", st["group_id"] == "ffff0000" and st["prepared"] == 1 and st["applied"] == 1
         and not A.group_lock("models"), (st["phase"], st["error"]))
    srv.shutdown()
    hf.shutdown()
    print("全部通過" if OK else "有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
