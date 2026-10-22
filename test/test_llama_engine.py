#!/usr/bin/env python3
"""模型庫與 llama.cpp 引擎整合測試：下載 → 換成 GGUF → 改 API 設定 → 壞模型自動還原 → 切回 Strata → 刪除。

不碰正式的模型與服務：
  - 假的 Hugging Face 站（本機執行緒，有檔案清單與可續傳的下載）
  - 假的 GGUF（只有檔頭，KV 位元組可以手算核對）
  - 假的 llama-server 與假的 Strata（都是 test/fixtures/mock_llm.py），掛成臨時的 systemd 使用者服務
欄位名稱帶亂數，systemd 服務名稱不會撞到真的；測完全部刪掉。
跑法（Linux、有 systemd --user）：python3 test/test_llama_engine.py
"""
import http.client
import http.server
import json
import os
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK = os.path.join(ROOT, "test", "fixtures", "mock_llm.py")
W = tempfile.mkdtemp(prefix="xc-llama-")
TAG = secrets.token_hex(3)
SLOT, SLOT2 = f"t{TAG}a", f"t{TAG}b"
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
STRATA_UNIT = f"xc-test-strata-{TAG}.service"
OK = True


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


def gguf_bytes(arch, name, layers, heads, kv_heads, head_dim, ctx, pad=1024 * 1024):
    """做一個只有檔頭的 GGUF（v3）。tokenizer 放 200 個字串，驗證解析器跳得過大陣列。"""
    out = bytearray(b"GGUF" + struct.pack("<I", 3))
    kvs = []

    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b

    def add(k, t, v):
        kvs.append(s(k) + struct.pack("<I", t) + v)

    add("general.architecture", 8, s(arch))
    add("general.name", 8, s(name))
    add(f"{arch}.context_length", 4, struct.pack("<I", ctx))
    add(f"{arch}.block_count", 4, struct.pack("<I", layers))
    add(f"{arch}.embedding_length", 4, struct.pack("<I", heads * head_dim))
    add(f"{arch}.attention.head_count", 4, struct.pack("<I", heads))
    add(f"{arch}.attention.head_count_kv", 4, struct.pack("<I", kv_heads))
    add(f"{arch}.attention.key_length", 4, struct.pack("<I", head_dim))
    toks = b"".join(s(f"t{i}") for i in range(200))
    add("tokenizer.ggml.tokens", 9, struct.pack("<IQ", 8, 200) + toks)
    out += struct.pack("<QQ", 0, len(kvs)) + b"".join(kvs)
    return bytes(out) + b"\0" * pad


def put_gguf(path, **kw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(gguf_bytes(**kw))


# ---------------------------------------------------------------- 假的 Hugging Face
HF_FILES = {
    "tiny-Q4_K_M.gguf": gguf_bytes("llama", "Tiny", 2, 4, 2, 64, 8192, pad=3 * 1024 * 1024),
    "tiny-Q8_0.gguf": gguf_bytes("llama", "Tiny", 2, 4, 2, 64, 8192, pad=5 * 1024 * 1024),
    "mmproj-tiny-F16.gguf": gguf_bytes("clip", "Tiny vision", 1, 1, 1, 8, 512, pad=1024),
    "MTP/mtp-tiny-Q8_0.gguf": gguf_bytes("llama", "Tiny MTP", 1, 4, 2, 64, 8192, pad=1024),
}


class HF(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/models/acme/tiny-GGUF/tree/main"):
            body = json.dumps([{"type": "file", "path": k, "size": len(v)} for k, v in HF_FILES.items()]
                              + [{"type": "file", "path": "README.md", "size": 10}]).encode()
            self.send_response(200)
        elif self.path.startswith("/api/models/"):
            body, _ = b"{}", self.send_response(404)
        elif self.path.startswith("/acme/tiny-GGUF/resolve/main/"):
            data = HF_FILES.get(self.path.rsplit("/", 1)[1])
            if data is None:
                self.send_response(404)
                body = b""
            else:
                rng = self.headers.get("Range")
                start = int(rng.split("=")[1].rstrip("-")) if rng else 0
                body = data[start:]
                self.send_response(206 if rng else 200)
        else:
            body, _ = b"", self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                           # 代理只讀標頭查大小就關連線，屬正常


def main():
    if subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True).returncode not in (0, 1):
        print("略過：這台沒有 systemd --user")
        return 0
    hf_port, llm_port, agent_port = free_port(), free_port(), free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", hf_port), HF)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ---- 假的 Strata：兩顆可以換（fake-a 現用、fake-b 備用）
    sd, conf = os.path.join(W, "strata-data"), os.path.join(W, "strata")
    for k, mname in (("fake-a", "mock-a"), ("fake-b", "mock-b")):
        os.makedirs(os.path.join(sd, "packs", k, "tokenizer"))
        put_gguf(os.path.join(sd, "models", k.upper(), f"Qwen3.8-Flash-Next-{k}-00001-of-00001.gguf"),
                 arch="qwen3next", name="Qwen3.8", layers=2, heads=2, kv_heads=1, head_dim=8, ctx=4096)
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, f"strata-{k}.json"), "w") as fh:
            json.dump({"port": llm_port, "model_name": mname, "args": [
                "--pack", os.path.join(sd, "packs", k),
                "--native", os.path.join(sd, "models", k.upper(), f"Qwen3.8-Flash-Next-{k}-00001-of-00001.gguf"),
                "--max-context", "32768"]}, fh)
    prod = os.path.join(conf, "strata-prod.json")
    shutil.copy(os.path.join(conf, "strata-fake-a.json"), prod)
    with open(os.path.join(UNIT_DIR, STRATA_UNIT), "w") as fh:
        fh.write(f"[Unit]\nDescription=xConsole 測試用假 Strata\n[Service]\nExecStart={sys.executable} {MOCK} {prod}\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "start", STRATA_UNIT], check=True)

    # ---- GGUF 存放區：Gemma 一顆、壞掉的一顆、Qwen 一顆（要被標成中國來源）
    groot = os.path.join(W, "gguf")
    put_gguf(os.path.join(groot, "gemma-test", "gemma-test-Q4_K_M.gguf"),
             arch="gemma3", name="Gemma Test", layers=4, heads=8, kv_heads=4, head_dim=128, ctx=131072)
    put_gguf(os.path.join(groot, "broken", "broken-Q4_K_M.gguf"),
             arch="gemma3", name="Broken", layers=4, heads=8, kv_heads=4, head_dim=128, ctx=131072)
    put_gguf(os.path.join(groot, "qwen-x", "qwen-x-Q4_K_M.gguf"),
             arch="qwen3", name="Qwen X", layers=2, heads=4, kv_heads=2, head_dim=64, ctx=32768)
    os.makedirs(os.path.join(groot, "half"))
    put_gguf(os.path.join(groot, "half", "half-Q4_K_M-00001-of-00002.gguf"),
             arch="llama", name="Half", layers=2, heads=4, kv_heads=2, head_dim=64, ctx=4096)

    ahome = os.path.join(W, "agent-home")
    os.makedirs(os.path.join(ahome, ".xconsole"))
    with open(os.path.join(ahome, ".xconsole", "registry.json"), "w") as fh:
        json.dump({"machine": {"name": "llama-test", "ip": "127.0.0.1", "memory_model": "discrete",
                               "gguf_root": groot, "llama_server": MOCK},
                   "slots": [{"id": SLOT, "role": "主腦", "tier": "高", "unit": STRATA_UNIT, "kind": "strata",
                              "config": prod, "caps": ["文字"], "est_vram_gib": 0, "est_ram_gib": 0},
                             {"id": SLOT2, "role": "副腦", "tier": "低", "caps": []}]}, fh)
    env = {**os.environ, "HOME": ahome, "XCONSOLE_AGENT_PORT": str(agent_port),
           "XCONSOLE_HF_BASE": f"http://127.0.0.1:{hf_port}", "XCONSOLE_UNIT_DIR": UNIT_DIR}
    agent = subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                ssl.get_server_certificate(("127.0.0.1", agent_port), timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        token = open(os.path.join(ahome, ".xconsole", "token")).read().strip()
        ctx = ssl.create_default_context(cafile=os.path.join(ahome, ".xconsole", "agent-cert.pem"))
        ctx.check_hostname = False

        def ag(method, path, body=None, timeout=900):
            c = http.client.HTTPSConnection("127.0.0.1", agent_port, context=ctx, timeout=timeout)
            c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            r = c.getresponse()
            d = json.loads(r.read() or b"{}")
            c.close()
            return r.status, d

        def plain(method, path, key="", body=None):
            c = http.client.HTTPConnection("127.0.0.1", llm_port, timeout=10)
            h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
            c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
            r = c.getresponse()
            d = r.read()
            c.close()
            return r.status, d

        def active(unit):
            return subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True).stdout.strip()

        def enabled(unit):
            return subprocess.run(["systemctl", "--user", "is-enabled", unit], capture_output=True, text=True).stdout.strip()

        for _ in range(40):
            if plain("GET", "/v1/models")[0] == 200:
                break
            time.sleep(0.25)

        # ---- 1. 掃存放區
        st, lib = ag("GET", "/library")
        g = {m["id"]: m for m in lib.get("gguf", [])}
        gm = g.get("gemma-test/gemma-test-Q4_K_M", {})
        check("模型庫掃到 GGUF，名稱取自檔頭、量化取自檔名", st == 200 and gm.get("name") == "Gemma Test" and gm.get("quant") == "Q4_K_M", gm)
        check("  從檔頭讀出架構與訓練上下文", gm.get("arch") == "gemma3" and gm.get("ctx_train") == 131072, gm)
        check("  每 token 的 KV 位元組照層數 × KV 頭 × (K+V) × 2 算：4×4×256×2＝8,192", gm.get("kv_full_bpt") == 8192, gm.get("kv_full_bpt"))
        check("  Qwen 標示為中國來源、Gemma 不標", g.get("qwen-x/qwen-x-Q4_K_M", {}).get("cn_origin") is True and gm.get("cn_origin") is False)
        check("  分片不齊的不列入，並說明原因", "half/half-Q4_K_M" not in g
              and any("分片不齊" in p["missing"] for p in lib.get("problems", [])), lib.get("problems"))
        check("  Strata 家族也列在同一個模型庫", len(lib.get("strata", [])) == 2, lib.get("strata"))

        st, cat = ag("GET", f"/catalog/{SLOT}")
        check("換模型清單同時有 Strata 與 GGUF", st == 200 and len(cat.get("models", [])) == 2 and len(cat.get("gguf", [])) == 3
              and cat.get("can_llama"), (len(cat.get("models", [])), len(cat.get("gguf", []))))

        # ---- 2. Strata → llama.cpp（同一個欄位、同一個埠）
        st, d = ag("POST", f"/slot/{SLOT}/model", {"engine": "llama", "model": "gemma-test/gemma-test-Q4_K_M",
                                                  "ctx": 8192, "force": True})
        lunit = f"xc-{SLOT}-llama.service"
        check("從 Strata 換成 GGUF（跨引擎）", st == 200 and d.get("switched_engine") and d.get("port") == llm_port, d)
        check("  舊引擎停掉、新引擎開著並設成開機自啟", active(STRATA_UNIT) != "active" and active(lunit) == "active"
              and enabled(lunit) == "enabled", (active(STRATA_UNIT), active(lunit), enabled(lunit)))
        sc, body = plain("GET", "/v1/models")
        check("  同一個埠回的是新模型的名稱（IDE 網址不用改）", sc == 200 and d.get("api_model_name", "x") in body.decode(), body[:120])
        cfgp = os.path.join(ahome, ".xconsole", "engines", f"{SLOT}-llama.json")
        raw = json.load(open(cfgp))
        check("  上下文照指定的 8,192、權限 0600", raw["ctx"] == 8192 and oct(os.stat(cfgp).st_mode & 0o777) == "0o600", raw)
        st, info = ag("GET", f"/apiinfo/{SLOT}")
        check("  API 串接頁：引擎 llama、可在網頁改設定", info.get("engine") == "llama" and info.get("editable")
              and info.get("model_name") == d.get("api_model_name"), info)

        # ---- 3. llama.cpp 的 API 設定：金鑰、名稱
        st, d2 = ag("POST", f"/apiinfo/{SLOT}/settings", {"key": "new", "model_name": "gemma-coder", "force": True})
        key = d2.get("api_key", "")
        check("llama.cpp 欄位產生金鑰並改名稱 → 重啟驗證通過", st == 200 and key.startswith("xc-"), d2)
        check("  金鑰寫進檔案、不在指令列", key not in subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout)
        check("  沒帶金鑰打對話被擋、帶金鑰打得通（/v1/models 本來就公開，不能拿它判斷）",
              plain("POST", "/v1/chat/completions", body={})[0] == 401
              and plain("POST", "/v1/chat/completions", key, {"model": "gemma-coder", "messages": [{"role": "user", "content": "hi"}]})[0] == 200)
        st, t = ag("POST", f"/apiinfo/{SLOT}/test")
        check("  本機實測（模型清單、沒帶金鑰被擋、對話、工具呼叫）全過", st == 200 and t.get("ok"), t.get("checks"))

        # ---- 4. 換到壞掉的 GGUF：整組還原
        subprocess.run(["systemctl", "--user", "disable", lunit], capture_output=True)   # 原本沒設開機自啟
        st, d3 = ag("POST", f"/slot/{SLOT}/model", {"engine": "llama", "model": "broken/broken-Q4_K_M", "force": True})
        check("換到起不來的模型 → 500 並自動換回", st == 500 and d3.get("reverted") and d3.get("restored_ok"), d3)
        check("  同一個服務還原後，開機自啟照原本的狀態（沒被試的時候打開）", enabled(lunit) != "enabled" and active(lunit) == "active",
              (enabled(lunit), active(lunit)))
        subprocess.run(["systemctl", "--user", "enable", lunit], capture_output=True)
        sc, body = plain("GET", "/v1/models")
        check("  換回後名稱、金鑰都還是原本的", b"gemma-coder" in body
              and plain("POST", "/v1/chat/completions", key, {"model": "gemma-coder", "messages": [{"role": "user", "content": "hi"}]})[0] == 200, body[:120])

        # ---- 5. 切回 Strata 家族的另一顆
        pack_b = os.path.join(sd, "packs", "fake-b")
        nat_b = os.path.join(sd, "models", "FAKE-B", "Qwen3.8-Flash-Next-fake-b-00001-of-00001.gguf")
        st, d4 = ag("POST", f"/slot/{SLOT}/model", {"pack": pack_b, "native": nat_b, "force": True})
        check("從 GGUF 切回 Strata 的另一顆", st == 200 and d4.get("switched_engine"), d4)
        check("  llama.cpp 停掉並取消開機自啟、Strata 開著", active(lunit) != "active" and enabled(lunit) != "enabled"
              and active(STRATA_UNIT) == "active", (active(lunit), enabled(lunit), active(STRATA_UNIT)))
        sc, body = plain("GET", "/v1/models", key)          # Strata 的 /v1/models 要金鑰
        check("  欄位自己取的名稱跟著走（IDE 不用改）", sc == 200 and b"gemma-coder" in body, body[:120])
        check("  金鑰也跟著走", plain("POST", "/v1/chat/completions", body={})[0] == 401
              and plain("POST", "/v1/chat/completions", key, {"model": "gemma-coder", "messages": [{"role": "user", "content": "hi"}]})[0] == 200)

        # ---- 6. 下載（假的 Hugging Face）
        st, e = ag("POST", "/library/hf", {"repo": "acme/../../etc"})
        check("Hugging Face 名稱格式不對 → 400", st == 400, e)
        st, files = ag("POST", "/library/hf", {"repo": "acme/tiny-GGUF"})
        check("列出 repo 的量化選項與 mmproj；MTP 輔助檔標出並排到最後",
              st == 200 and [c["stem"] for c in files["choices"]] == ["tiny-Q4_K_M", "tiny-Q8_0", "mtp-tiny-Q8_0"]
              and [c["aux"] for c in files["choices"]] == [False, False, True]
              and files["mmproj"] == ["mmproj-tiny-F16.gguf"], files)
        st, e = ag("POST", "/library/download", {"repo": "acme/tiny-GGUF", "stem": "../../evil"})
        check("下載不在清單內的檔名 → 400", st == 400, e)
        st, dl = ag("POST", "/library/download", {"repo": "acme/tiny-GGUF", "stem": "tiny-Q4_K_M",
                                                 "mmproj": "mmproj-tiny-F16.gguf", "token": "hf_secret_for_test"})
        check("背景下載開始", st == 200 and dl.get("id"), dl)
        for _ in range(100):
            _, lib = ag("GET", "/library")
            job = next((j for j in lib["downloads"] if j["id"] == dl.get("id")), {})
            if job.get("state") in ("done", "failed", "cancelled"):
                break
            time.sleep(0.2)
        check("  下載完成、大小正確", job.get("state") == "done" and job.get("done") == job.get("total"), job)
        tm = next((m for m in lib["gguf"] if m["id"] == "acme--tiny/tiny-Q4_K_M"), {})
        check("  下載完自動出現在模型庫，帶 mmproj（可看圖）", tm.get("vision") is True, tm)
        dump = json.dumps(lib) + open(os.path.join(ahome, ".xconsole", "downloads.json")).read()
        check("  Hugging Face 權杖不留在任何紀錄", "hf_secret_for_test" not in dump)

        # ---- 7. 空欄位直接放一顆 GGUF（自動配埠）
        st, d5 = ag("POST", f"/slot/{SLOT2}/model", {"engine": "llama", "model": "acme--tiny/tiny-Q4_K_M", "force": True})
        check("空欄位放入 GGUF：自動配一個沒人用的埠並啟動", st == 200 and d5.get("port") and d5["port"] != llm_port
              and active(f"xc-{SLOT2}-llama.service") == "active", d5)

        # ---- 8. 刪除
        st, e = ag("POST", "/library/delete", {"id": "acme--tiny/tiny-Q4_K_M"})
        check("有欄位正在用的模型不能刪", st == 409, e)
        st, e = ag("POST", "/library/delete", {"id": "../../etc/passwd"})
        check("刪除路徑穿越 → 擋下", st in (400, 404), e)
        st, e = ag("POST", "/library/delete", {"id": "qwen-x/qwen-x-Q4_K_M"})
        check("刪除沒在用的模型：檔案與空資料夾一起移除", st == 200 and not os.path.exists(os.path.join(groot, "qwen-x")), e)

        # ---- 9. 能力標籤跟著模型走
        def caps(sid):
            return next(s for s in ag("GET", "/state")[1]["slots"] if s["id"] == sid)["caps"]
        had = "看圖" in caps(SLOT2)
        st, _ = ag("POST", f"/slot/{SLOT2}/model", {"engine": "llama", "model": "gemma-test/gemma-test-Q4_K_M", "force": True})
        check("換到沒有 mmproj 的模型：「看圖」標籤拿掉", had and st == 200 and "看圖" not in caps(SLOT2), caps(SLOT2))
        check("下載資料夾帶作者名稱（不同作者的同名 repo 不會互相蓋掉）", os.path.isdir(os.path.join(groot, "acme--tiny")))
    finally:
        agent.terminate()
        agent.wait(timeout=10)
        for u in (STRATA_UNIT, f"xc-{SLOT}-llama.service", f"xc-{SLOT2}-llama.service"):
            subprocess.run(["systemctl", "--user", "stop", u], capture_output=True)
            subprocess.run(["systemctl", "--user", "disable", u], capture_output=True)
            try:
                os.remove(os.path.join(UNIT_DIR, u))
            except OSError:
                pass
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed"], capture_output=True)
        srv.shutdown()
        shutil.rmtree(W, ignore_errors=True)
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
