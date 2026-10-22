#!/usr/bin/env python3
"""API 串接整合測試：網頁改設定 → 重啟 → 用新設定打一次 → 打不通自動換回，全程走真的 systemd。

不碰正式的模型：用 test/fixtures/mock_llm.py 當假引擎（讀同格式設定檔、講 OpenAI 介面、支援金鑰與工具呼叫），
掛成臨時的 systemd 使用者服務 xc-apitest-<亂數>.service，測完刪掉。
主控台與節點代理都在隔離的 HOME 起，各用隨機埠。
跑法（Linux、有 systemd --user）：python3 test/test_api_integration.py
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
W = tempfile.mkdtemp(prefix="xc-api-")
UNIT = f"xc-apitest-{secrets.token_hex(3)}.service"
UNIT_FILE = os.path.expanduser(f"~/.config/systemd/user/{UNIT}")
OK = True
PW = "api-test-pass-2026"


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


def wait_tls(port):
    for _ in range(80):
        try:
            ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def https(port, cafile, method, path, body=None, headers=None, timeout=600):
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.check_hostname = False
    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=timeout)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
    r = c.getresponse()
    raw = r.read()
    sc = r.getheader("Set-Cookie") or ""
    c.close()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    return r.status, data, sc


def plain(port, path, key=""):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"Authorization": f"Bearer {key}"} if key else {})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def main():
    if not shutil.which("systemctl") or subprocess.run(["systemctl", "--user", "is-system-running"],
                                                       capture_output=True).returncode not in (0, 1):
        print("略過：這台沒有 systemd --user")
        return 0
    llm_port, agent_port, console_port = free_port(), free_port(), free_port()
    cfg_path = os.path.join(W, "engine.json")
    with open(cfg_path, "w") as fh:
        json.dump({"port": llm_port, "model_name": "mock-a", "host": "0.0.0.0",
                   "args": ["--max-context", "32768"]}, fh)
    os.makedirs(os.path.dirname(UNIT_FILE), exist_ok=True)
    with open(UNIT_FILE, "w") as fh:
        fh.write(f"[Unit]\nDescription=xConsole API 測試用假引擎\n[Service]\n"
                 f"ExecStart={sys.executable} {ROOT}/test/fixtures/mock_llm.py {cfg_path}\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "start", UNIT], check=True)

    ahome = os.path.join(W, "agent-home")
    os.makedirs(os.path.join(ahome, ".xconsole"))
    with open(os.path.join(ahome, ".xconsole", "registry.json"), "w") as fh:
        json.dump({"machine": {"name": "api-test", "ip": "127.0.0.1", "memory_model": "discrete"},
                   "slots": [{"id": "brain", "role": "主腦", "tier": "高", "unit": UNIT, "kind": "strata",
                              "config": cfg_path, "caps": ["文字"], "est_vram_gib": 0, "est_ram_gib": 0}]}, fh)
    aenv = {**os.environ, "HOME": ahome, "XCONSOLE_AGENT_PORT": str(agent_port)}
    agent = subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py")], env=aenv,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    console = None
    try:
        check("節點代理啟動", wait_tls(agent_port))
        token = open(os.path.join(ahome, ".xconsole", "token")).read().strip()
        acert = os.path.join(ahome, ".xconsole", "agent-cert.pem")
        auth = {"Authorization": f"Bearer {token}"}

        def ag(method, path, body=None):
            st, d, _ = https(agent_port, acert, method, path, body, auth)
            return st, d

        for _ in range(40):
            if plain(llm_port, "/v1/models") == 200:
                break
            time.sleep(0.25)
        st, info = ag("GET", "/apiinfo/brain")
        check("讀連線資訊：網址、模型名稱、上下文", st == 200 and info.get("online")
              and info["public_url"] == f"http://127.0.0.1:{llm_port}/v1" and info["model_name"] == "mock-a"
              and info["max_context"] == 32768 and info["editable"] and not info["api_key_set"], info)

        # ---- 輸入驗證
        st, d = ag("POST", "/apiinfo/brain/settings", {"model_name": "壞 名稱; rm -rf"})
        check("模型名稱有空白或分號 → 400", st == 400, d)
        st, d = ag("POST", "/apiinfo/brain/settings", {"max_tokens": 40000})
        check("輸出上限不小於上下文 → 400", st == 400, d)
        st, d = ag("POST", "/apiinfo/brain/settings", {"public_url": "javascript:alert(1)"})
        check("對外網址不是 http(s) → 400", st == 400, d)
        st, d = ag("POST", "/apiinfo/brain/settings", {"public_url": "http://ai.example.edu.tw:8081/v1"})
        st2, info = ag("GET", "/apiinfo/brain")
        check("改對外網址：不重啟、立刻生效", st == 200 and not d.get("restarted")
              and info["public_url"] == "http://ai.example.edu.tw:8081/v1", d)
        ag("POST", "/apiinfo/brain/settings", {"public_url": ""})

        # ---- 改名稱＋產生金鑰：重啟後用新設定打得通、沒帶金鑰被擋
        st, d = ag("POST", "/apiinfo/brain/settings", {"model_name": "xvita-coder", "key": "new", "max_tokens": 8192})
        key = d.get("api_key", "")
        check("改模型名稱、輸出上限並產生金鑰 → 重啟並驗證通過", st == 200 and d.get("restarted") and key.startswith("xc-"), d)
        check("  新金鑰只在這次回傳", key and "api_key" not in json.dumps(ag("GET", "/apiinfo/brain")[1]).replace('"api_key_set"', ""))
        check("  沒帶金鑰打不進去、帶對金鑰打得進去",
              plain(llm_port, "/v1/models") == 401 and plain(llm_port, "/v1/models", key) == 200)
        mode = oct(os.stat(cfg_path).st_mode & 0o777)
        check("  設定檔（含金鑰）權限 0600", mode == "0o600", mode)
        st, info = ag("GET", "/apiinfo/brain")
        check("  連線資訊顯示新名稱、已設金鑰，不顯示金鑰本身",
              info["model_name"] == "xvita-coder" and info["api_key_set"] and key not in json.dumps(info), info)

        st, t = ag("POST", "/apiinfo/brain/test")
        names = {c["name"]: c["ok"] for c in t.get("checks", [])}
        check("本機實測四項：模型清單、沒帶金鑰被擋、一般對話、工具呼叫", st == 200 and t["ok"] and len(names) == 4, t)

        # ---- 調上下文到引擎起不來：自動換回
        st, d = ag("POST", "/apiinfo/brain/settings", {"max_context": 300000})
        check("上下文調到引擎起不來 → 500 並自動換回", st == 500 and d.get("reverted") and d.get("restored_ok"), d)
        st, info = ag("GET", "/apiinfo/brain")
        check("  換回之後原本的設定還在、服務照常",
              info["online"] and info["max_context"] == 32768 and info["model_name"] == "xvita-coder", info)
        check("  換回之後金鑰還是有效", plain(llm_port, "/v1/models", key) == 200)

        # ---- 主控台：經網路實測與轉送
        chome = os.path.join(W, "console-home")
        os.makedirs(os.path.join(chome, ".xconsole", "trust"), mode=0o700)
        cenv = {**os.environ, "HOME": chome, "XCONSOLE_PORT": str(console_port)}
        subprocess.run([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py"), "--set-credentials"],
                       input=f"admin\n{PW}\n".encode(), env=cenv, check=True, capture_output=True)
        fp = os.path.join(chome, ".xconsole", "fleet.json")
        cfg = json.load(open(fp))
        cfg["nodes"] = [{"id": "t1", "name": ".t1", "host": "127.0.0.1", "group": "測試",
                         "agent_port": agent_port, "token": token}]
        json.dump(cfg, open(fp, "w"))
        shutil.copy(acert, os.path.join(chome, ".xconsole", "trust", "t1.pem"))
        console = subprocess.Popen([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py")], env=cenv,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("隔離主控台啟動", wait_tls(console_port))
        ccert = os.path.join(chome, ".xconsole", "tls", "cert.pem")
        _, _, sc = https(console_port, ccert, "POST", "/api/login", {"username": "admin", "password": PW})
        ck = {"Cookie": "xconsole=" + sc.split("xconsole=", 1)[1].split(";", 1)[0]}

        def co(method, path, body=None):
            st, d, _ = https(console_port, ccert, method, path, body, ck)
            return st, d

        st, info = co("GET", "/api/node/t1/apiinfo/brain")
        check("主控台轉送連線資訊", st == 200 and info.get("model_name") == "xvita-coder", (st, info))
        st, t = co("POST", "/api/node/t1/apiinfo/brain/test", {})
        check("經網路實測（沒給金鑰）：連得到、沒帶金鑰回 401 算通過", st == 200 and t["network"]["ok"]
              and t["network"].get("partial"), t.get("network"))
        st, t = co("POST", "/api/node/t1/apiinfo/brain/test", {"key": "wrong-key"})
        check("經網路實測（金鑰錯）→ 不通過", st == 200 and not t["network"]["ok"], t.get("network"))
        st, t = co("POST", "/api/node/t1/apiinfo/brain/test", {"key": key})
        check("經網路實測（金鑰對）→ 全部通過", st == 200 and t["ok"] and t["network"]["ok"], t.get("network"))

        co("POST", "/api/node/t1/apiinfo/brain/settings", {"public_url": f"http://127.0.0.1:{llm_port}/redir/v1"})
        st, t = co("POST", "/api/node/t1/apiinfo/brain/test", {"key": key})
        check("對外網址會轉址 → 實測不跟過去（金鑰不送到別處）", st == 200 and not t["network"]["ok"]
              and "轉址" in t["network"]["detail"], t.get("network"))
        co("POST", "/api/node/t1/apiinfo/brain/settings", {"public_url": ""})

        st, d = co("POST", "/api/node/t1/apiinfo/brain/settings", {"key": "none"})
        check("主控台改設定：拿掉金鑰 → 重啟後不帶金鑰打得通", st == 200 and plain(llm_port, "/v1/models") == 200, d)
        st, au = co("GET", "/api/admin/audit?limit=50")
        dump = json.dumps(au, ensure_ascii=False)
        check("稽核記了 api.settings 與 api.test，沒有金鑰內容與錯的金鑰",
              "api.settings" in dump and "api.test" in dump and key not in dump and "wrong-key" not in dump)
    finally:
        for p in (console, agent):
            if p:
                p.terminate()
                p.wait(timeout=10)
        subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
        os.remove(UNIT_FILE)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", UNIT], capture_output=True)
        shutil.rmtree(W, ignore_errors=True)
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
