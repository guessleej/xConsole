#!/usr/bin/env python3
"""真機冒煙測試：用真的 Hugging Face 與真的 llama-server 走一次「下載 → 放入空欄位 → 開金鑰 → 實測 → 刪除」。

只用一顆約 100 MB 的小模型（SmolLM2-135M），放在隔離的存放區與隔離的欄位，跑完全部刪掉，
不碰機器上任何正式欄位。假引擎測不出來的事情靠這支：組出來的 llama-server 參數真的 llama-server 認不認。
跑法（在有 llama.cpp 的機器上）：python3 test/smoke_real_llama.py
"""
import http.client
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = tempfile.mkdtemp(prefix="xc-smoke-")
SLOT = f"smoke{secrets.token_hex(3)}"
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
REPO, STEM = "bartowski/SmolLM2-135M-Instruct-GGUF", "SmolLM2-135M-Instruct-Q4_K_M"
OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail and not cond else ""), flush=True)
    OK = OK and bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    port = free_port()
    home = os.path.join(W, "home")
    os.makedirs(os.path.join(home, ".xconsole"))
    with open(os.path.join(home, ".xconsole", "registry.json"), "w") as fh:
        json.dump({"machine": {"name": "smoke", "ip": "127.0.0.1", "memory_model": "discrete",
                               "gguf_root": os.path.join(W, "gguf")},
                   "slots": [{"id": SLOT, "role": "測試", "tier": "冒煙", "caps": []}]}, fh)
    env = {**os.environ, "HOME": home, "XCONSOLE_AGENT_PORT": str(port), "XCONSOLE_UNIT_DIR": UNIT_DIR}
    # 隔離的 HOME 底下找不到 llama.cpp：把登入帳號家目錄下的那幾支交給代理挑
    real = os.path.expanduser("~")
    for d in os.listdir(real):
        if d.startswith("llama.cpp"):
            os.symlink(os.path.join(real, d), os.path.join(home, d))
    agent = subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    unit = f"xc-{SLOT}-llama.service"
    try:
        for _ in range(80):
            try:
                ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        token = open(os.path.join(home, ".xconsole", "token")).read().strip()
        ctx = ssl.create_default_context(cafile=os.path.join(home, ".xconsole", "agent-cert.pem"))
        ctx.check_hostname = False

        def ag(method, path, body=None):
            c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=1200)
            c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            r = c.getresponse()
            d = json.loads(r.read() or b"{}")
            c.close()
            return r.status, d

        st, lib = ag("GET", "/library")
        check("自動找到 llama-server", st == 200 and lib.get("llama_server"), lib.get("llama_server"))
        st, files = ag("POST", "/library/hf", {"repo": REPO})
        check("真的 Hugging Face：列出量化", st == 200 and any(c["stem"] == STEM for c in files.get("choices", [])), files.get("error"))
        st, dl = ag("POST", "/library/download", {"repo": REPO, "stem": STEM})
        check("開始下載", st == 200, dl)
        t0 = time.time()
        while time.time() - t0 < 600:
            _, lib = ag("GET", "/library")
            job = next((j for j in lib["downloads"] if j["id"] == dl.get("id")), {})
            if job.get("state") in ("done", "failed", "cancelled"):
                break
            time.sleep(1)
        check(f"下載完成（{job.get('total', 0) / 1e6:.0f} MB，{time.time() - t0:.0f} 秒）", job.get("state") == "done", job)
        m = next((x for x in lib["gguf"] if x["id"].endswith("/" + STEM)), {})
        check("檔頭讀得出架構、上下文與 KV", m.get("arch") and m.get("ctx_train") and m.get("kv_full_bpt"), m)

        st, d = ag("POST", f"/slot/{SLOT}/model", {"engine": "llama", "model": m.get("id"), "ctx": 4096})
        check("放入空欄位：真的 llama-server 起得來，用新名稱打得通", st == 200, d)
        p = d.get("port")
        st, d2 = ag("POST", f"/apiinfo/{SLOT}/settings", {"key": "new", "model_name": "smoke-model"})
        check("開金鑰並改名稱：llama-server 吃得到 --api-key-file 與 --alias", st == 200 and d2.get("api_key"), d2)
        st, t = ag("POST", f"/apiinfo/{SLOT}/test")
        names = {c["name"]: (c["ok"], c["detail"]) for c in t.get("checks", [])}
        for k, v in names.items():
            # 小模型不一定會呼叫工具，那一項只列出來，不算失敗
            if "工具" in k:
                print(f"[參考] {k}：{v[1]}")
            else:
                check(f"實測：{k}", v[0], v[1])
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        check("金鑰不在任何程序的指令列", d2.get("api_key", "x") not in ps)
        check("--no-mmap 有帶上（統一記憶體機不多吃一份頁快取）", any("--no-mmap" in ln and f"--port {p}" in ln for ln in ps.splitlines()))
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", unit], capture_output=True)
        try:
            os.remove(os.path.join(UNIT_DIR, unit))
        except OSError:
            pass
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)
        agent.terminate()
        agent.wait(timeout=10)
        shutil.rmtree(W, ignore_errors=True)
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
