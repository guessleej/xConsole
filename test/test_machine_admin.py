#!/usr/bin/env python3
"""機器的納管與設定整合測試：加一台機器、改名與位址、移除、機器設定、欄位增改刪，全部走網頁會打的 API。

主控台與節點代理都在隔離的 HOME 起、各用隨機埠；登記服務用的假服務 xc-matest-<亂數>.service 測完刪掉。
安裝腳本本身不實跑（會改到這台真正的 ~/xconsole_agent.py 與使用者服務），改成：
檢查腳本語法與內容、用 curl 驗證腳本釘的公鑰真的是主控台這張、再照腳本最後一步的格式呼叫 /api/join。
跑法（Linux、有 systemd --user）：python3 test/test_machine_admin.py
"""
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = tempfile.mkdtemp(prefix="xc-ma-")
UNIT = f"xc-matest-{secrets.token_hex(3)}.service"
UNIT_FILE = os.path.expanduser(f"~/.config/systemd/user/{UNIT}")
PW = "machine-admin-test-2026"
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


def wait_tls(port):
    for _ in range(80):
        try:
            ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def https(port, cafile, method, path, body=None, headers=None, raw=False):
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=120)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    sc = r.getheader("Set-Cookie") or ""
    c.close()
    if raw:
        return r.status, data.decode("utf-8", "replace"), sc
    try:
        return r.status, json.loads(data), sc
    except ValueError:
        return r.status, {}, sc


def start_agent(name):
    home = os.path.join(W, name)
    os.makedirs(os.path.join(home, ".xconsole"))
    with open(os.path.join(home, ".xconsole", "registry.json"), "w") as fh:
        json.dump({"machine": {"name": name, "memory_model": "discrete"}, "slots": []}, fh)
    port = free_port()
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py")],
                            env={**os.environ, "HOME": home, "XCONSOLE_AGENT_PORT": str(port)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return home, port, proc


def main():
    if not shutil.which("systemctl") or subprocess.run(["systemctl", "--user", "is-system-running"],
                                                       capture_output=True).returncode not in (0, 1):
        print("略過：這台沒有 systemd --user")
        return 0
    if subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True).returncode != 0:
        print("略過：連不到 systemd 使用者 bus（daemon-reload 失敗）")
        return 0
    os.makedirs(os.path.dirname(UNIT_FILE), exist_ok=True)
    with open(UNIT_FILE, "w") as fh:
        fh.write("[Unit]\nDescription=xConsole 機器管理測試用假服務\n[Service]\nExecStart=/bin/sleep 600\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    procs = []
    try:
        ahome, aport, ap = start_agent("agent-a")
        procs.append(ap)
        check("節點代理啟動", wait_tls(aport))
        token = open(os.path.join(ahome, ".xconsole", "token")).read().strip()
        acert = open(os.path.join(ahome, ".xconsole", "agent-cert.pem")).read()

        cport = free_port()
        chome = os.path.join(W, "console-home")
        os.makedirs(os.path.join(chome, ".xconsole"), mode=0o700)
        cenv = {**os.environ, "HOME": chome, "XCONSOLE_PORT": str(cport)}
        subprocess.run([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py"), "--set-credentials"],
                       input=f"admin\n{PW}\n".encode(), env=cenv, check=True, capture_output=True)
        cp = subprocess.Popen([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py")], env=cenv,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(cp)
        check("隔離主控台啟動（還沒有任何機器）", wait_tls(cport))
        ccert = os.path.join(chome, ".xconsole", "tls", "cert.pem")

        def anon(method, path, body=None, raw=False):
            st, d, _ = https(cport, ccert, method, path, body, raw=raw)
            return st, d

        st, _ = anon("POST", "/api/nodes/join-code", {})
        check("沒登入不能產加入碼", st == 401, st)
        _, _, sc = https(cport, ccert, "POST", "/api/login", {"username": "admin", "password": PW})
        ck = {"Cookie": "xconsole=" + sc.split("xconsole=", 1)[1].split(";", 1)[0]}

        def co(method, path, body=None, raw=False):
            st, d, _ = https(cport, ccert, method, path, body, ck, raw=raw)
            return st, d

        # ---- 1. 加一台機器：加入碼、安裝腳本、釘公鑰、加入
        st, jc = co("POST", "/api/nodes/join-code", {"name": "測試機 A", "group": "測試群組", "console": f"127.0.0.1:{cport}"})
        check("產加入碼與安裝指令", st == 200 and "--pinnedpubkey 'sha256//" in jc.get("command", ""), jc)
        url = re.search(r"'(https://[^']+/agent\.sh)'", jc["command"]).group(1)
        code = url.split("/install/")[1].split("/")[0]
        pin = re.search(r"'(sha256//[^']+)'", jc["command"]).group(1)
        r = subprocess.run(["curl", "-sSk", "--pinnedpubkey", pin, f"https://127.0.0.1:{cport}/healthz"], capture_output=True)
        check("安裝指令釘的公鑰就是主控台這張（curl 釘公鑰連得上）", r.returncode == 0, r.stderr[-200:])
        r = subprocess.run(["curl", "-sSk", "--pinnedpubkey", "sha256//" + "A" * 43 + "=", f"https://127.0.0.1:{cport}/healthz"],
                           capture_output=True)
        check("  公鑰不對就連不上（不是關掉驗證）", r.returncode != 0)
        st, script = anon("GET", f"/install/{code}/agent.sh", raw=True)
        check("不登入也拿得到安裝腳本（要帶加入碼）", st == 200 and code in script and pin in script)
        sp = os.path.join(W, "install.sh")
        open(sp, "w").write(script)
        r = subprocess.run(["bash", "-n", sp], capture_output=True, text=True)
        check("  安裝腳本語法正確（bash -n）", r.returncode == 0, r.stderr)
        check("  腳本拒絕用 root 執行、會檢查 python3／openssl／curl",
              'id -u)" = 0' in script and "python3 openssl curl" in script)
        check("  重新安裝時等健康檢查通過才往下（不是只等權杖檔出現）",
              re.search(r"for i in .*\n.*\n.*healthz.*UP=yes; break", script) is not None)
        st, src = anon("GET", f"/install/{code}/xconsole_agent.py", raw=True)
        check("  代理程式下載得到，跟 repo 裡的一樣", st == 200 and src == open(os.path.join(ROOT, "agent", "xconsole_agent.py")).read())
        st, _ = anon("GET", f"/install/{'x' * 32}/agent.sh", raw=True)
        check("加入碼不對 → 404", st == 404, st)

        # 資安審查 H1：送「別台代理的憑證＋自己的 CA」整串，想搶走已納管的那台 → 只收剛好一張
        ccert_pem = open(ccert).read()
        st, d = anon("POST", "/api/join", {"code": code, "token": token, "cert": acert + ccert_pem, "hostname": "agent-a", "port": aport})
        check("加入時送兩張憑證串在一起 → 拒絕（不能用來搶走別台）", st == 400 and "剛好一張" in d.get("error", ""), d)
        st, d = anon("POST", "/api/join", {"code": code, "token": "wrong" * 8, "cert": acert, "hostname": "agent-a", "port": aport, "ips": 5})
        check("ips 不是清單也不會讓加入碼卡住（回 400 之後還能用）", st == 400, d)
        st, d = anon("POST", "/api/join", {"code": code, "token": "wrong" * 8, "cert": acert, "hostname": "agent-a", "port": aport})
        check("權杖不對 → 不能加入，並說明連不到", st == 400 and "連不到" in d.get("error", ""), d)
        check("  失敗原因不回連線細節（不能拿加入端點分辨埠的狀態）", "refused" not in d.get("error", "").lower()
              and "Errno" not in d.get("error", ""), d)
        st, d = anon("POST", "/api/join", {"code": code, "token": token, "cert": acert, "hostname": "agent-a", "port": free_port()})
        check("埠連不到 → 不能加入，提示開防火牆", st == 400 and "防火牆" in d.get("error", ""), d)
        st, js = co("GET", f"/api/nodes/join-code/{jc['id']}")
        check("  網頁上看得到這次失敗的原因", st == 200 and js["state"] == "waiting" and "防火牆" in (js.get("error") or ""), js)
        # 兩個請求同時拿同一個加入碼：第一個驗證期間（這裡故意讓它去試一個連不到的位址）第二個要被擋
        import threading
        slow = {}
        th = threading.Thread(target=lambda: slow.update(r=anon("POST", "/api/join", {
            "code": code, "token": "wrong" * 8, "cert": acert, "hostname": "agent-a", "port": aport, "ips": ["10.255.255.1"]})))
        th.start()
        time.sleep(2)
        st, d = anon("POST", "/api/join", {"code": code, "token": token, "cert": acert, "hostname": "agent-a", "port": aport})
        check("同一個加入碼另一個請求還在驗證時 → 擋下（不會兩台都加入）", st == 400 and "正在用" in d.get("error", ""), d)
        th.join()
        check("  第一個驗證失敗後放回去，還可以重試", slow["r"][0] == 400, slow)
        st, d = anon("POST", "/api/join", {"code": code, "token": token, "cert": acert, "hostname": "agent-a",
                                            "port": aport, "linger": True, "ips": ["10.255.255.1"]})
        check("權杖與憑證都對 → 加入成功", st == 200 and d.get("id") == "agent-a", d)
        st, js = co("GET", f"/api/nodes/join-code/{jc['id']}")
        check("  網頁上的狀態變成已加入", js.get("state") == "joined" and js.get("node_id") == "agent-a", js)
        st, d = anon("POST", "/api/join", {"code": code, "token": token, "cert": acert, "hostname": "agent-a", "port": aport})
        check("同一個加入碼不能再用", st == 400, d)
        st, nodes = co("GET", "/api/nodes")
        n = next((x for x in nodes["nodes"] if x["id"] == "agent-a"), {})
        check("清單出現這台：名稱、群組照產碼時填的、已信任憑證",
              n.get("name") == "測試機 A" and n.get("group") == "測試群組" and n.get("trusted") and n.get("host") == "127.0.0.1", n)
        check("  fleet.json 裡的權杖是密文", json.load(open(os.path.join(chome, ".xconsole", "fleet.json")))["nodes"][0]["token"].startswith("enc:v1:"))
        st, fl = co("GET", "/api/fleet")
        check("  主控台連得上它（在線）", any(x["id"] == "agent-a" and x["online"] for x in fl["nodes"]), fl)

        # ---- 2. 改名、群組、位址；換到連不到的位址要自動改回
        st, d = co("POST", "/api/nodes/agent-a/update", {"name": "A 機", "group": "實驗室"})
        n = next(x for x in co("GET", "/api/nodes")[1]["nodes"] if x["id"] == "agent-a")
        check("改名稱與群組", st == 200 and n["name"] == "A 機" and n["group"] == "實驗室", (st, d))
        st, d = co("POST", "/api/nodes/agent-a/update", {"agent_port": free_port()})
        n = next(x for x in co("GET", "/api/nodes")[1]["nodes"] if x["id"] == "agent-a")
        check("改到連不到的埠 → 400，並自動改回原本的", st == 400 and n["agent_port"] == aport, (st, d, n))
        st, d = co("POST", "/api/nodes/agent-a/update", {"host": "bad host;rm"})
        check("位址格式不對 → 400", st == 400, d)

        # ---- 3. 機器設定（白名單欄位、路徑逐項檢查）
        st, mi = co("GET", "/api/node/agent-a/machine")
        check("讀機器設定：含偵測建議、存放區候選、可登記的服務", st == 200 and mi["detect"]["memory_model"] in
              ("discrete", "unified_split", "unified_single") and isinstance(mi["storage"], list) and UNIT in mi["units"], mi.get("units"))
        for bad, why in (("/etc/xc-test", "系統目錄"), (os.path.join(ahome, ".ssh", "m"), "隱藏資料夾"), (ahome, "家目錄本身"), ("models", "相對路徑"),
                         ("/tmp/xc-models", "允許位置以外（/tmp）")):
            st, d = co("POST", "/api/node/agent-a/machine", {"memory_model": "discrete", "gguf_root": bad})
            check(f"存放區指到{why} → 400", st == 400, d)
        st, d = co("POST", "/api/node/agent-a/machine", {"memory_model": "discrete", "llama_server": "/bin/sh"})
        check("llama-server 指到別的執行檔 → 400", st == 400, d)
        st, d = co("POST", "/api/node/agent-a/machine", {"memory_model": "abc"})
        check("記憶體架構不在三種之內 → 400", st == 400, d)
        good_root = os.path.join(ahome, "models", "gguf2")
        st, d = co("POST", "/api/node/agent-a/machine", {"memory_model": "unified_single", "ram_floor_gib": "10",
                                                         "vram_floor_gib": "", "gguf_root": good_root, "note": "測試備註"})
        reg = json.load(open(os.path.join(ahome, ".xconsole", "registry.json")))
        check("儲存機器設定：架構、底線、存放區、備註寫進登錄檔，並標成已確認",
              st == 200 and reg["machine"]["memory_model"] == "unified_single" and reg["machine"]["ram_floor_gib"] == 10
              and reg["machine"]["gguf_root"] == good_root and reg["machine"]["note"] == "測試備註" and reg["machine"]["confirmed"]
              and "vram_floor_gib" not in reg["machine"], reg["machine"])
        st, lib = co("GET", "/api/node/agent-a/library")
        check("  模型庫跟著用新的存放區", lib.get("root") == good_root, lib.get("root"))

        # ---- 4. 欄位：新增空欄位、登記服務、修改、移除
        st, d = co("POST", "/api/node/agent-a/slots/add", {"type": "empty", "role": "主腦", "tier": "高", "caps": ["文字"]})
        check("新增空欄位", st == 200 and d["slot"]["id"] == "slot1" and "unit" not in d["slot"], d)
        st, d = co("POST", "/api/node/agent-a/slots/add", {"type": "service", "unit": "ssh.service", "role": "測"})
        check("登記不在清單內的服務 → 400", st == 400, d)
        st, d = co("POST", "/api/node/agent-a/slots/add", {"type": "service", "unit": UNIT, "role": "瀏覽器", "tier": "眼睛",
                                                           "port": 9377, "est_ram_gib": 3, "critical": True})
        check("登記這台已經有的服務", st == 200 and d["slot"]["unit"] == UNIT and d["slot"]["port"] == 9377 and d["slot"]["critical"], d)
        sid = d["slot"]["id"]
        st, mi = co("GET", "/api/node/agent-a/machine")
        check("  登記過的服務不再列在候選裡", UNIT not in mi["units"])
        st, d = co("POST", f"/api/node/agent-a/slot/{sid}/edit", {"role": "抓料", "log": "/etc/shadow",
                                                                   "config": os.path.join(ahome, ".ssh/id_rsa"), "unit": "ssh.service"})
        reg = json.load(open(os.path.join(ahome, ".xconsole", "registry.json")))
        sl = next(x for x in reg["slots"] if x["id"] == sid)
        check("修改欄位：改得到角色，改不到記錄檔、設定檔與服務名稱（防讀任意檔案）",
              st == 200 and sl["role"] == "抓料" and "log" not in sl and "config" not in sl and sl["unit"] == UNIT, sl)
        st, d = co("POST", f"/api/node/agent-a/slot/{sid}/edit", {"role": "x" * 30})
        check("角色名稱太長 → 400", st == 400, d)
        subprocess.run(["systemctl", "--user", "start", UNIT], check=True)
        st, d = co("POST", f"/api/node/agent-a/slot/{sid}/delete", {})
        check("服務還在跑時不能移除欄位 → 409", st == 409, d)
        subprocess.run(["systemctl", "--user", "stop", UNIT], check=True)
        st, d = co("POST", f"/api/node/agent-a/slot/{sid}/delete", {})
        check("停止後移除：只取消登記，服務本身還在", st == 200 and d.get("kept_service") == UNIT and os.path.exists(UNIT_FILE), d)
        st, d = co("POST", "/api/node/agent-a/slot/slot1/delete", {})
        reg = json.load(open(os.path.join(ahome, ".xconsole", "registry.json")))
        check("移除空欄位", st == 200 and reg["slots"] == [], reg["slots"])

        # ---- 4b. 情境：只開放上下文、並行數、自動縮輸出上限；原本的其他參數保留
        scfg = os.path.join(ahome, "strata.json")
        json.dump({"args": ["--max-context", "32768"], "parallel": 1}, open(scfg, "w"))
        reg = json.load(open(os.path.join(ahome, ".xconsole", "registry.json")))
        reg["slots"].append({"id": "brain", "role": "主腦", "unit": UNIT, "kind": "strata", "config": scfg})
        reg["slots"].append({"id": "other", "role": "其他", "unit": UNIT})
        reg["profiles"] = {"coding": {"name": "寫程式", "why": "舊的", "changes": [
            {"slot": "brain", "args": {"--max-context": "131072", "--kv": "int8"}, "config": {"parallel": 1}}]}}
        reg["machine"]["profile"] = "coding"
        json.dump(reg, open(os.path.join(ahome, ".xconsole", "registry.json"), "w"))
        st, d = co("POST", "/api/node/agent-a/profiles/save", {"name": "抓料", "why": "多並行",
                                                              "changes": [{"slot": "brain", "max_context": 32768, "parallel": 2, "fit_max_tokens": True}]})
        check("新增情境", st == 200 and d["profile"]["changes"][0] == {"slot": "brain", "args": {"--max-context": "32768"},
                                                                     "config": {"parallel": 2, "fit_max_tokens": True}}, d)
        st, d = co("POST", "/api/node/agent-a/profiles/save", {"key": "coding", "name": "寫程式後端", "why": "Copilot",
                                                              "changes": [{"slot": "brain", "max_context": 65536, "parallel": "", "fit_max_tokens": None}]})
        ch = d.get("profile", {}).get("changes", [{}])[0]
        check("修改情境：改得到上下文，原本的其他參數（--kv）保留、清空的欄位拿掉",
              st == 200 and ch.get("args") == {"--max-context": "65536", "--kv": "int8"} and "config" not in ch, d)
        for bad, why in (({"slot": "other", "parallel": 2}, "非 Strata 欄位"), ({"slot": "brain", "max_context": 100}, "上下文太小"),
                         ({"slot": "brain", "parallel": 99}, "並行數太大"), ({"slot": "brain"}, "什麼都沒調")):
            st, d = co("POST", "/api/node/agent-a/profiles/save", {"name": "壞", "changes": [bad]})
            check(f"情境：{why} → 400", st == 400, d)
        st, d = co("POST", "/api/node/agent-a/profiles/save", {"name": "x", "changes": [{"slot": "brain", "parallel": 2, "args": {"--log-file": "/etc/x"}}]})
        reg = json.load(open(os.path.join(ahome, ".xconsole", "registry.json")))
        check("情境裡塞其他引擎參數（例：記錄檔路徑）會被忽略", st == 200 and "--log-file" not in json.dumps(reg["profiles"]), reg["profiles"])
        st, d = co("POST", "/api/node/agent-a/profiles/coding/delete", {})
        reg = json.load(open(os.path.join(ahome, ".xconsole", "registry.json")))
        check("刪除正在用的情境：情境不見、機器不再標示目前情境", st == 200 and "coding" not in reg["profiles"] and "profile" not in reg["machine"], reg["machine"])
        reg["slots"] = [x for x in reg["slots"] if x["id"] not in ("brain", "other")]
        json.dump(reg, open(os.path.join(ahome, ".xconsole", "registry.json"), "w"))

        # ---- 4c. 編譯 llama.cpp：狀態查得到、沒在編時取消回 409（實際編譯另在有 CUDA 的機器實測）
        st, b = co("GET", "/api/node/agent-a/llama/build")
        check("編譯視窗讀得到工具檢查與加速方式", st == 200 and set(b["prereqs"]["tools"]) == {"git", "cmake", "C++ 編譯器"}
              and b["prereqs"]["backend"] in ("cuda", "hip", "cpu") and "env" not in b["prereqs"], b.get("prereqs"))
        st, d = co("GET", "/api/node/agent-a/llama/build/progress")
        check("編譯進度查得到（還沒編過）", st == 200 and d["running"] is False, d)
        st, d = co("POST", "/api/node/agent-a/llama/build/cancel", {})
        check("沒在編譯時按取消 → 409", st == 409, d)

        # ---- 5. 移除機器
        st, d = co("POST", "/api/nodes/agent-a/remove", {})
        st2, nodes = co("GET", "/api/nodes")
        check("移除機器：清單裡不見、信任的憑證一起刪掉", st == 200 and not nodes["nodes"]
              and not os.path.exists(os.path.join(chome, ".xconsole", "trust", "agent-a.pem")), nodes)
        check("  那台的代理沒被動到（還在跑）", ap.poll() is None)

        # ---- 6. 移除後重新加入：當成新機器（沒填名稱就用主機名稱）
        st, jc2 = co("POST", "/api/nodes/join-code", {"console": f"127.0.0.1:{cport}"})
        code2 = re.search(r"/install/([^/]+)/", jc2["command"]).group(1)
        st, d = anon("POST", "/api/join", {"code": code2, "token": token, "cert": acert, "hostname": "agent-a", "port": aport})
        n = next((x for x in co("GET", "/api/nodes")[1]["nodes"] if x["id"] == "agent-a"), {})
        check("移除後可以重新加入；沒填名稱就用主機名稱、群組是未分類",
              st == 200 and n.get("name") == "agent-a" and n.get("group") == "未分類", (d, n))
        co("POST", "/api/nodes/agent-a/update", {"name": "改過的名字"})
        st, jc3 = co("POST", "/api/nodes/join-code", {"console": f"127.0.0.1:{cport}", "replace": "agent-a"})
        code3 = re.search(r"/install/([^/]+)/", jc3["command"]).group(1)
        st, d = anon("POST", "/api/join", {"code": code3, "token": token, "cert": acert, "hostname": "agent-a", "port": aport})
        ns = co("GET", "/api/nodes")[1]["nodes"]
        check("管理者指定「重新安裝哪一台」：更新原本那筆，不會多一台，名稱不變",
              st == 200 and len(ns) == 1 and ns[0]["name"] == "改過的名字", ns)
        st, jc4 = co("POST", "/api/nodes/join-code", {"console": f"127.0.0.1:{cport}"})
        code4 = re.search(r"/install/([^/]+)/", jc4["command"]).group(1)
        st, d = anon("POST", "/api/join", {"code": code4, "token": token, "cert": acert, "hostname": "agent-a", "port": aport})
        ns = co("GET", "/api/nodes")[1]["nodes"]
        check("沒指定重新安裝：一律新增一台，不會自動覆寫既有那筆（防搶機器）",
              st == 200 and len(ns) == 2 and any(x["name"] == "改過的名字" for x in ns), ns)
        co("POST", f"/api/nodes/{d.get('id')}/remove", {})
        st, d = co("POST", "/api/nodes/join-code", {"console": f"127.0.0.1:{cport}", "replace": "nope"})
        check("指定重新安裝不存在的那台 → 400", st == 400, d)
        st, d = co("POST", "/api/nodes/join-code", {"console": "127.0.0.1:99999"})
        check("主控台位址的埠超過 65535 → 400", st == 400, d)

        # ---- 7. 加入失敗太多次要擋
        for _ in range(12):
            st, d = anon("POST", "/api/join", {"code": "x" * 32, "token": token, "cert": acert})
        check("同一來源加入失敗太多次 → 429", st == 429, (st, d))

        # ---- 7b. 資安審查 L1：沒登入的請求不讀大本文；其他端點先驗登入
        def anon_or_closed(path, body):
            try:
                return anon("POST", path, body)[0]
            except (ConnectionError, http.client.HTTPException, ssl.SSLError, OSError):
                return "closed"            # 超過上限不讀就關連線：對方多半收不到回應，這正是要的
        st = anon_or_closed("/api/nodes/agent-a/update", {"name": "x" * 200000})
        check("沒登入打需要登入的端點、本文 200 KB → 不讀就擋（401 或直接關連線）", st in (401, "closed"), st)
        st, d = anon("POST", "/api/nodes/agent-a/update", {"name": "x"})
        check("沒登入、本文很小 → 401", st == 401, st)
        st = anon_or_closed("/api/login", {"username": "a", "password": "b" * 100000})
        check("登入的本文超過 64 KB → 擋下（413 或直接關連線）", st in (413, "closed"), st)
        st, fl = co("GET", "/api/fleet")
        check("  擋完之後主控台照常服務", st == 200, st)

        # ---- 7c. 資安審查 M4：代理回應有大小上限與總時限
        r = subprocess.run([sys.executable, "-c", f"""
import io, os, sys, time
os.environ["HOME"] = {repr(os.path.join(W, "cap-home"))}
sys.path.insert(0, {repr(os.path.join(ROOT, "console"))})
import xconsole_server as X
class Big(io.RawIOBase):
    def readinto(self, b):
        b[:len(b)] = b"x" * len(b); return len(b)
import socket, threading, urllib.request
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
def drip():
    c, _ = srv.accept(); c.recv(4096)
    c.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: 1000\\r\\n\\r\\n")
    for _ in range(30):
        try: c.sendall(b" "); time.sleep(1)
        except OSError: break
threading.Thread(target=drip, daemon=True).start()
try:
    X._read_capped(Big(), time.time() + 30); print("big-ok")
except ValueError: print("big-blocked")
t0 = time.time()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{{srv.getsockname()[1]}}/", timeout=2) as r:
        X._read_capped(r, time.time() + 3); print("drip-ok")
except TimeoutError: print("drip-blocked", round(time.time() - t0, 1))
"""], capture_output=True, text=True, timeout=120)
        secs = float(re.search(r"drip-blocked ([\d.]+)", r.stdout).group(1)) if "drip-blocked" in r.stdout else 99
        check("代理回超大內容 → 擋下；每秒吐一個位元組拖時間（真的 socket）→ 3 秒總時限內就斷",
              "big-blocked" in r.stdout and secs < 5, r.stdout + r.stderr[-300:])

        # ---- 7c2. 慢速佔線：請求列與標頭 15 秒內沒送完就關（以前每 100 秒送一個位元組就能無限期佔住）
        ctx0 = ssl.create_default_context(cafile=ccert)
        ctx0.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx0.check_hostname = False
        sk = ctx0.wrap_socket(socket.create_connection(("127.0.0.1", cport), timeout=40))
        sk.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")
        t0 = time.time()
        try:
            got = sk.recv(10)
        except (ConnectionError, ssl.SSLError, OSError):
            got = b""
        check("標頭送一半就停：15 秒左右主控台主動斷線", got == b"" and 13 <= time.time() - t0 <= 25, round(time.time() - t0, 1))
        sk.close()
        st, _ = co("GET", "/api/fleet")
        check("  一般請求不受影響", st == 200, st)

        # ---- 7d. 資安審查 M2：同一來源同時送很多次錯的密碼，也只會驗前 5 次
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(20) as ex:
            codes = list(ex.map(lambda _: anon("POST", "/api/login", {"username": "admin", "password": "wrong"})[0], range(20)))
        check("同時送 20 次錯的密碼：最多 5 次被驗、其餘直接擋", codes.count(401) <= 5 and codes.count(429) >= 15, codes)

        # ---- 8. 操作紀錄
        st, au = co("GET", "/api/admin/audit?limit=200")
        acts = {x["action"] for x in au.get("items", [])}
        need = {"node.join_code", "node.join", "node.update", "node.remove", "machine.settings", "slot.add", "slot.edit", "slot.delete",
                "profile.save", "profile.delete", "llama.build_cancel"}
        check("每個動作都寫進操作紀錄", need <= acts, need - acts)
        check("  紀錄裡沒有代理的權杖", token not in json.dumps(au, ensure_ascii=False))
    finally:
        for p in procs:
            p.terminate()
        subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
        if os.path.exists(UNIT_FILE):
            os.remove(UNIT_FILE)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        shutil.rmtree(W, ignore_errors=True)
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
