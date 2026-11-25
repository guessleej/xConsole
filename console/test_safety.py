#!/usr/bin/env python3
"""安全指令（清單 #25）：停用、恢復、清除、撤銷身分。不碰正式環境，全部在隔離目錄與隨機埠上跑。

一、主控台的佇列（store，不開網路）：後下的覆蓋先下的（同一種、停用與恢復互相）、排了撤銷身分就不收別的、
    撤銷身分一定最後送、送出中的不能取消、主控台重開把送到一半的改回待送、機器移除時一起取消。
二、代理這一側（直接呼叫）：同一個編號再送來回上次的結果、停用後本機不能啟動或換模型（停止與看狀態照樣可以）、
    停用寫檔重開照樣停用、清除只刪 xConsole 自己的東西（外部資料夾只取消連結）、勾了才刪模型、撤銷身分丟掉權杖。
三、端對端：主控台加一支真的代理。代理離線時排指令（名稱打錯擋下、可取消），連上後依序送達；
    停用期間經主控台換模型回 423、群組總覽標「已停用」；恢復；撤銷身分之後主控台不再對它下指令、代理要重新加入。
跑法：python3 test_safety.py
"""
import http.cookiejar
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from store import Store  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


def queue():
    W = tempfile.mkdtemp(prefix="xc-safety-q-")
    try:
        s = Store(os.path.join(W, "t.db"))
        d1, _ = s.safety_add("pc-1", "disable", {}, "admin")
        e1, gone = s.safety_add("pc-1", "enable", {}, "admin")
        check("恢復取代還沒送出的停用", gone == [d1["id"]] and s.safety_get(d1["id"])["state"] == "superseded")
        w1, gone = s.safety_add("pc-1", "wipe", {"models": True}, "admin")
        check("  清除不會取代停用或恢復", gone == [] and len(s.safety_pending("pc-1")) == 2)
        w2, gone = s.safety_add("pc-1", "wipe", {}, "it@cloudinfo.com.tw")
        check("  同一種再下一次：取代前一筆", gone == [w1["id"]])
        r1, _ = s.safety_add("pc-1", "revoke", {}, "admin")
        try:
            s.safety_add("pc-1", "disable", {}, "admin")
            blocked = False
        except ValueError:
            blocked = True
        check("排了撤銷身分：不再收別的指令", blocked)
        order = [c["kind"] for c in s.safety_pending("pc-1")]
        check("送的順序照先後、撤銷身分一定最後", order == ["enable", "wipe", "revoke"], order)
        check("有待送的機器清單", s.safety_pending_nodes() == {"pc-1"})
        check("取消待送的那筆", s.safety_cancel("pc-1", r1["id"], "admin") and s.safety_get(r1["id"])["state"] == "canceled")
        check("  別台的編號取消不了", not s.safety_cancel("pc-2", w2["id"], "admin"))
        check("送出中的不能取消", s.safety_claim(e1["id"]) and not s.safety_cancel("pc-1", e1["id"], "admin"))
        check("  也不會被重複認領", not s.safety_claim(e1["id"]))
        s.safety_tried(e1["id"], "連不上")
        c = s.safety_get(e1["id"])
        check("送不成：改回待送，記下原因", c["state"] == "pending" and c["error"] == "連不上" and c["tried"])
        s.safety_claim(e1["id"])
        s.safety_unstick()
        check("主控台重開：送到一半的改回待送", s.safety_get(e1["id"])["state"] == "pending")
        s.safety_claim(e1["id"])
        check("送達：記下結果", s.safety_done(e1["id"], {"started": []}) and s.safety_get(e1["id"])["state"] == "done"
              and not s.safety_done(e1["id"], {}))
        check("機器移除：還沒送的一起取消", s.safety_cancel_node("pc-1", "admin") == 1 and not s.safety_pending_nodes())
        check("清單新的在前", s.safety_list("pc-1")[0]["id"] == r1["id"] and len(s.safety_list("pc-1")) == 5)
    finally:
        shutil.rmtree(W, ignore_errors=True)


def agent_side():
    """另開一個 Python 匯入代理（隔離的家目錄），直接呼叫安全指令的函式。"""
    W = tempfile.mkdtemp(prefix="xc-safety-a-")
    code = r'''
import json, os, sys, re
sys.path.insert(0, sys.argv[1])
import xconsole_agent as A
out = {}
A.ensure_cert = lambda: None
os.makedirs(os.path.join(A.AGENT_ROOT, "作業一"), exist_ok=True)
open(os.path.join(A.AGENT_ROOT, "作業一", "a.txt"), "w").write("x")
ext = os.path.join(A.HOME, "外部資料夾"); os.makedirs(ext, exist_ok=True); open(os.path.join(ext, "keep.txt"), "w").write("k")
reg = A.registry(); reg["machine"]["agent_links"] = {"外部": ext}; A.save_json(A.REGISTRY, reg)
os.makedirs(A.GEN_DIR, exist_ok=True); open(os.path.join(A.GEN_DIR, "img.png"), "w").write("p")
root = A.gguf_root(A.registry()); os.makedirs(os.path.join(root, "m"), exist_ok=True)
open(os.path.join(root, "m", "m.gguf"), "w").write("g")
cid = lambda n: ("%024x" % n)
out["bad"] = A.safety_run({"id": "xyz", "kind": "disable"})[0]
out["d1"] = A.safety_run({"id": cid(1), "kind": "disable", "by": "admin"})
out["disabled"] = bool(A.safety_disabled())
out["d1_again"] = A.safety_run({"id": cid(1), "kind": "disable"})
ok = A.DISABLED_OK.fullmatch
out["allow"] = [bool(ok(p)) for p in ("/slot/main/stop", "/all/stop", "/group/sync", "/safety", "/self/update")]
out["deny"] = [bool(ok(p)) for p in ("/slot/main/start", "/slot/main/model", "/all/start", "/chat/main", "/relay/main",
                                     "/profile/x", "/sd/generate", "/agent/exec", "/library/download", "/slots/add", "/desktop/act")]
out["state_flag"] = A.safety_public() is not None
out["w1"] = A.safety_run({"id": cid(2), "kind": "wipe"})[1]
out["agent_left"] = os.listdir(A.AGENT_ROOT)
out["ext_kept"] = os.path.exists(os.path.join(ext, "keep.txt"))
out["links"] = A.registry()["machine"].get("agent_links")
out["gen_left"] = os.listdir(A.GEN_DIR)
out["model_kept"] = os.path.exists(os.path.join(root, "m", "m.gguf"))
out["w2"] = A.safety_run({"id": cid(3), "kind": "wipe", "opts": {"models": True}})[1]
out["model_gone"] = not os.path.exists(os.path.join(root, "m"))
out["e1"] = A.safety_run({"id": cid(4), "kind": "enable"})[1]
out["enabled"] = A.safety_disabled() is None
old = A.TOKEN
open(A.LINK_FILE, "w").write("{}")
A._revoke()
out["revoke"] = [A.TOKEN != old, open(A.TOKEN_FILE).read().strip() == A.TOKEN, not os.path.exists(A.LINK_FILE),
                 bool(A.IDENTITY.get("rejoin"))]
print(json.dumps(out, ensure_ascii=False))
'''
    try:
        r = subprocess.run([sys.executable, "-c", code, os.path.join(ROOT, "agent")], capture_output=True, text=True, timeout=120,
                           env={**os.environ, "HOME": W, "XCONSOLE_HOME": W})
        out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
        if not out:
            print(r.stdout[-1500:], r.stderr[-1500:])
        check("代理：格式不對的指令擋下", out.get("bad") == 400)
        check("代理：停用", out.get("d1", [0])[0] == 200 and out.get("disabled"), out.get("d1"))
        check("  同一個編號再送來：回上次的結果，不重做", (out.get("d1_again") or [0, {}])[1].get("repeat") is True)
        check("  停用中仍可以停止、收群組設定、收安全指令、更新代理", all(out.get("allow") or [False]), out.get("allow"))
        check("  停用中不能啟動、換模型、對話、畫圖、執行Agent指令、下載模型、加欄位、操作桌面",
              not any(out.get("deny") or [True]), out.get("deny"))
        check("  狀態裡帶停用（主控台與本機畫面看得到）", out.get("state_flag"))
        w1 = out.get("w1") or {}
        check("清除：Agent工作資料夾與畫圖產出刪掉，外部資料夾只取消連結、裡面的檔案不動",
              out.get("agent_left") == [] and out.get("ext_kept") and out.get("links") == {} and out.get("gen_left") == []
              and w1.get("agent_files") == 1 and w1.get("agent_links") == 1, (w1, out.get("agent_left"), out.get("links")))
        check("  沒勾就不刪模型；勾了才刪 xConsole 存放區裡的", out.get("model_kept") and out.get("model_gone")
              and (out.get("w2") or {}).get("models", 0) >= 1, out.get("w2"))
        check("恢復：停用解除", out.get("enabled") and "started" in (out.get("e1") or {}))
        check("撤銷身分：換掉記憶體與檔案裡的權杖、刪連回設定、顯示需要重新加入", out.get("revoke") == [True, True, True, True],
              out.get("revoke"))
    finally:
        shutil.rmtree(W, ignore_errors=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_agent(ahome, aport):
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py"), "--home", ahome, "--bind", "127.0.0.1"],
                         env={**os.environ, "HOME": ahome, "XCONSOLE_HOME": ahome, "XCONSOLE_AGENT_PORT": str(aport)},
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            ssl.get_server_certificate(("127.0.0.1", aport), timeout=1)
            return p
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return p


def stop(p):
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def e2e():
    import secrets
    W = tempfile.mkdtemp(prefix="xc-safety-e2e-")
    chome, ahome = os.path.join(W, "console"), os.path.join(W, "agent")
    for d in (os.path.join(chome, ".xconsole", "trust"), os.path.join(ahome, ".xconsole")):
        os.makedirs(d, mode=0o700)
    port, lport, aport = free_port(), free_port(), free_port()
    token = secrets.token_urlsafe(32)
    tfile = os.path.join(ahome, ".xconsole", "token")
    open(tfile, "w").write(token)
    agent = start_agent(ahome, aport)
    cons = None
    try:
        shutil.copy(os.path.join(ahome, ".xconsole", "agent-cert.pem"), os.path.join(chome, ".xconsole", "trust", "ws-1.pem"))
        stop(agent)                               # 先讓它離線：指令要能排著等
        json.dump({"auth": {}, "nodes": [{"id": "ws-1", "name": "教室 01", "host": "127.0.0.1", "agent_port": aport,
                                          "group": "教室", "link": "pull", "token": token}]},
                  open(os.path.join(chome, ".xconsole", "fleet.json"), "w"))
        env = {**os.environ, "HOME": chome, "XCONSOLE_HOME": chome, "XCONSOLE_PORT": str(port), "XCONSOLE_LINK_PORT": str(lport)}
        srv = os.path.join(HERE, "xconsole_server.py")
        subprocess.run([sys.executable, srv, "--set-credentials"], input=b"admin\nsafety-pass-2026\n", env=env, check=True,
                       capture_output=True)
        cons = subprocess.Popen([sys.executable, srv], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        ctx = ssl.create_default_context(cafile=os.path.join(chome, ".xconsole", "tls", "cert.pem"))
        ctx.check_hostname = False
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                         urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def call(path, body=None):
            req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            try:
                with op.open(req, timeout=90) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)

        def wait(cond, secs=30):
            for _ in range(secs * 2):
                d = call("/api/node/ws-1/safety")[1]
                if cond(d):
                    return d
                time.sleep(0.5)
            return d

        call("/api/login", {"username": "admin", "password": "safety-pass-2026"})
        st, out = call("/api/node/ws-1/safety", {"kind": "disable", "confirm": "教室 02"})
        check("名稱打錯：400，不排", st == 400 and "教室 01" in out.get("error", ""), out)
        check("  不認得的指令：400", call("/api/node/ws-1/safety", {"kind": "format", "confirm": "教室 01"})[0] == 400)
        st, out = call("/api/node/ws-1/safety", {"kind": "wipe", "confirm": "教室 01"})
        wid = out["cmd"]["id"]
        check("離線時排清除：存起來等它連上", st == 200 and out["cmd"]["state"] == "pending", out)
        # 排進去的當下背景就會試送一次：正在送的那幾秒取消會回 409，等它送不成、改回待送再取消
        for _ in range(60):
            st, out = call(f"/api/node/ws-1/safety/{wid}/cancel", {})
            if st != 409:
                break
            time.sleep(0.5)
        check("  送出前可以取消", st == 200 and call(f"/api/node/ws-1/safety/{wid}/cancel", {})[0] == 409)
        st, out = call("/api/node/ws-1/safety", {"kind": "disable", "confirm": "教室 01"})
        did = out["cmd"]["id"]
        d = wait(lambda d: any(c["id"] == did and c.get("error") for c in d["cmds"]), 90)
        c = next(c for c in d["cmds"] if c["id"] == did)
        check("連不上：留著待送、記下原因（不設期限）", c["state"] == "pending" and c["error"], c)

        agent = start_agent(ahome, aport)
        st, out = call("/api/node/ws-1/safety", {"kind": "wipe", "confirm": "教室 01"})       # 新排一筆會馬上重試
        d = wait(lambda d: all(c["state"] in ("done", "canceled") for c in d["cmds"]))
        done = [c for c in d["cmds"] if c["state"] == "done"]
        check("連上之後依序送達：停用、清除", [c["kind"] for c in sorted(done, key=lambda c: c["done"])] == ["disable", "wipe"]
              and d["disabled"], d["cmds"])
        st, out = call("/api/node/ws-1/all/start", {})
        check("停用中經主控台啟動：423，講明被停用", st == 423 and out.get("disabled_by_admin"), (st, out))
        fl = call("/api/fleet")[1]
        me = next(n for n in fl["nodes"] if n["id"] == "ws-1")
        check("  機器狀態帶停用、群組總覽標「已停用」", me["state"]["disabled_by_admin"] and me["ready"]["code"] == "disabled",
              me.get("ready"))
        stop(agent)
        agent = start_agent(ahome, aport)
        fl = call("/api/fleet")[1]
        me = next(n for n in fl["nodes"] if n["id"] == "ws-1")
        check("  代理重開：照樣停用", me["online"] and me["state"]["disabled_by_admin"], me.get("error"))
        call("/api/node/ws-1/safety", {"kind": "enable"})
        d = wait(lambda d: not d["disabled"])
        check("恢復：不用輸入名稱，送達後解除停用", not d["disabled"] and d["cmds"][0]["state"] == "done", d["cmds"][:1])
        call("/api/node/ws-1/safety", {"kind": "revoke", "confirm": "教室 01"})
        st, out = call("/api/node/ws-1/safety", {"kind": "disable", "confirm": "教室 01"})
        d = wait(lambda d: d["revoked"])
        check("撤銷身分：送達後主控台標成已撤銷", d["revoked"] and d["cmds"][0]["kind"] == "revoke", d["cmds"][:2])
        st, out = call("/api/node/ws-1/state")
        check("  之後主控台不再對它下指令", st == 409 and out.get("revoked"), (st, out))
        actx = ssl.create_default_context(cafile=os.path.join(ahome, ".xconsole", "agent-cert.pem"))
        actx.check_hostname = False
        for _ in range(40):                       # 換權杖檔與標「需要重新加入」是兩步，兩樣都到了才算
            hz = json.loads(urllib.request.urlopen(f"https://127.0.0.1:{aport}/healthz", context=actx, timeout=10).read())
            if open(tfile).read().strip() != token and hz.get("needs_rejoin"):
                break
            time.sleep(0.5)
        check("  代理丟掉舊權杖、顯示需要重新加入", open(tfile).read().strip() != token and hz.get("needs_rejoin"), hz)
        check("  身分撤銷後不收新指令", call("/api/node/ws-1/safety", {"kind": "disable", "confirm": "教室 01"})[0] == 409)
        aud = [a["action"] for a in call("/api/admin/audit")[1]["items"]]
        check("每一筆排入、取消、送達都寫稽核", {"node.safety", "node.safety_cancel", "node.safety_done"} <= set(aud), aud[:10])
    finally:
        stop(cons)
        stop(agent)
        if not OK and cons:
            print(cons.stdout.read().decode(errors="replace")[-2500:])
        shutil.rmtree(W, ignore_errors=True)


if __name__ == "__main__":
    queue()
    agent_side()
    e2e()
    print("全部通過" if OK else "有項目失敗")
    sys.exit(0 if OK else 1)
