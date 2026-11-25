#!/usr/bin/env python3
"""還原卡接回（清單 #18）。在隔離 HOME 起一支真的代理與主控台，照安裝腳本最後一步的格式呼叫 /api/join。
驗：加入時發接回憑證、拿它接回原來那一格並當場換新、舊的用過就作廢、拿不出憑證但主機板識別碼一樣的列進待核准、
核准接回那一格／當成新機器、群組沒開「允許接回」時憑證與識別碼都不算數；join.py 存憑證的位置與權限、識別碼雜湊。
跑法：python3 console/test_rejoin.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "install"))
import join as JP  # noqa: E402
import test_joincodes as T  # noqa: E402

W = tempfile.mkdtemp(prefix="xc-rejoin-")
PW = "rejoin-test-2026"
OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


# ---- join.py
sd = os.path.join(W, "sd")
os.makedirs(sd)
check("join.py：有不還原的位置就先找那裡，代理資料夾也存一份",
      JP.rejoin_paths(sd, "/data/x") == ["/data/x/.xconsole/rejoin.key", os.path.join(sd, "rejoin.key")]
      and JP.rejoin_paths(sd, "") == [os.path.join(sd, "rejoin.key")])
JP.save_rejoin([os.path.join(W, "keep", ".xconsole", "rejoin.key")], "k" * 40)
mode = stat.S_IMODE(os.stat(os.path.join(W, "keep", ".xconsole", "rejoin.key")).st_mode)
check("  接回憑證只有自己讀得到", os.name == "nt" or mode == 0o600, oct(mode))
with open(os.path.join(sd, "identity.json"), "w", encoding="utf-8") as fh:
    json.dump({"ids": {"smbios": "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"}}, fh)
check("  主機板識別碼是無效值（全 F）：不送", JP.hwid(sd) == "")
with open(os.path.join(sd, "identity.json"), "w", encoding="utf-8") as fh:
    json.dump({"ids": {"smbios": "4C4C4544-0042-3510-8051-B4C04F4B3732"}}, fh)
check("  送的是雜湊，不是識別碼本身", len(JP.hwid(sd)) == 64 and "4C4C4544" not in JP.hwid(sd))

# ---- 代理：群組的存放位置
os.environ["XCONSOLE_HOME"] = os.path.join(W, "agent-store")
sys.path.insert(0, os.path.join(ROOT, "agent"))
import xconsole_agent as A  # noqa: E402

os.environ.pop("XCONSOLE_HOME")                 # 後面另起的代理與主控台各用自己的 HOME
A.group_state = lambda: {"group_id": "abcdef12", "version": 1}
A._gsave = lambda st: None
os.makedirs(A.STATE_DIR, exist_ok=True)
with open(os.path.join(A.STATE_DIR, "rejoin.key"), "w", encoding="utf-8") as fh:
    fh.write("r" * 40)
keep = os.path.join(W, "D-drive", "xconsole")
st = {"steps": []}
A.group_storage(st, keep)
check("代理：模型改放存放位置底下的 models", os.path.abspath(A.gguf_root()) == os.path.abspath(os.path.join(keep, "models")))
kp = os.path.join(keep, ".xconsole", "rejoin.key")
check("  接回憑證另存一份到存放位置（系統碟被還原也還在）", os.path.exists(kp) and open(kp).read() == "r" * 40
      and (os.name == "nt" or stat.S_IMODE(os.stat(kp).st_mode) == 0o600))
blocker = os.path.join(W, "not-a-dir")
open(blocker, "w").close()
try:
    A.group_storage(st, os.path.join(blocker, "x"))
    bad = False
except RuntimeError as exc:
    bad = "寫不進去" in str(exc)
check("  存放位置寫不進去：講清楚，不默默改放別處", bad)

# ---- 主控台
procs = []
try:
    ahome = os.path.join(W, "agent")
    os.makedirs(os.path.join(ahome, ".xconsole"))
    json.dump({"machine": {"name": "a", "memory_model": "discrete"}, "slots": []},
              open(os.path.join(ahome, ".xconsole", "registry.json"), "w"))
    aport = T.free_port()
    procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py")],
                                  env={**os.environ, "HOME": ahome, "XCONSOLE_AGENT_PORT": str(aport)},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    check("節點代理啟動", T.wait_tls(aport))
    token = open(os.path.join(ahome, ".xconsole", "token")).read().strip()
    acert = open(os.path.join(ahome, ".xconsole", "agent-cert.pem")).read()
    cport = T.free_port()
    chome = os.path.join(W, "console")
    os.makedirs(os.path.join(chome, ".xconsole"), mode=0o700)
    cenv = {**os.environ, "HOME": chome, "XCONSOLE_PORT": str(cport), "XCONSOLE_LINK_PORT": str(T.free_port())}
    subprocess.run([sys.executable, os.path.join(HERE, "xconsole_server.py"), "--set-credentials"],
                   input=f"admin\n{PW}\n".encode(), env=cenv, check=True, capture_output=True)
    procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, "xconsole_server.py")], env=cenv,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    check("隔離主控台啟動", T.wait_tls(cport))
    ccert = os.path.join(chome, ".xconsole", "tls", "cert.pem")
    _, _, sc = T.https(cport, ccert, "POST", "/api/login", {"username": "admin", "password": PW})
    ck = {"Cookie": "xconsole=" + sc.split("xconsole=", 1)[1].split(";", 1)[0]}

    def co(m, p, b=None):
        st, d, _ = T.https(cport, ccert, m, p, b, ck)
        return st, d

    def nodes():
        return {n["id"]: n for n in co("GET", "/api/nodes")[1]["nodes"]}

    st, g = co("POST", "/api/groups", {"name": "還原卡教室"})
    gid = g["group"]["id"]
    co("POST", f"/api/groups/{gid}/update", {"settings": {"allow_rejoin": True}})
    st, jc = co("POST", "/api/nodes/join-code", {"name": "", "uses": 10, "minutes": 60, "group": "還原卡教室",
                                                 "console": f"127.0.0.1:{cport}"})
    code = jc["command"].split("/install/")[1].split("/")[0]
    H = "a" * 64

    def join(**kw):
        st, d, _ = T.https(cport, ccert, "POST", "/api/join", {"code": code, "token": token, "cert": acert, "hostname": "lab-pc",
                                                               "port": aport, **kw})
        return st, d

    st, d1 = join(hwid=H)
    n1, k1 = d1.get("id"), d1.get("rejoin")
    check("加入：發一張接回憑證（只回這一次；機器清單不給雜湊與識別碼）", st == 200 and k1 and len(k1) >= 32
          and "rejoin_hash" not in nodes()[n1] and "hwid" not in nodes()[n1] and k1 not in json.dumps(nodes()), d1)
    with open(os.path.join(chome, ".xconsole", "fleet.json"), encoding="utf-8") as fh:
        check("  主控台只存雜湊，不存接回憑證本身", k1 not in fh.read())
    st, d2 = join(rejoin=k1, hwid=H)
    check("拿接回憑證接回：還是原來那一格，沒有多出一台", st == 200 and d2.get("id") == n1 and d2.get("via") == "rejoin"
          and len(nodes()) == 1, d2)
    check("  接回時當場換一張新的", d2.get("rejoin") and d2["rejoin"] != k1)
    st, d3 = join(rejoin=k1, hwid=H)
    n3 = d3.get("id")
    check("舊的接回憑證用過就作廢：當成新機器", st == 200 and n3 != n1 and d3.get("via") != "rejoin", d3)
    st, gs = co("GET", "/api/groups")
    pend = next(x for x in gs["groups"] if x["id"] == gid).get("rejoin_pending") or []
    check("  主機板識別碼跟原來那一格一樣：列進待核准（識別碼不是秘密，要人確認）", d3.get("via") == "pending"
          and [p["node"] for p in pend] == [n3] and pend[0]["claims"] == n1, pend)
    name1 = nodes()[n1]["name"]
    st, out = co("POST", f"/api/groups/{gid}/rejoin/{n3}/approve", {})
    ns = nodes()
    check("核准接回：新加入的這台接手原來那一格的名稱，舊的那一格拿掉", st == 200 and n1 not in ns and ns[n3]["name"] == name1, out)
    st, gs = co("GET", "/api/groups")
    check("  核准完就不在待核准清單", not next(x for x in gs["groups"] if x["id"] == gid).get("rejoin_pending"))
    check("  同一筆不能核准兩次", co("POST", f"/api/groups/{gid}/rejoin/{n3}/approve", {})[0] == 404)

    st, d4 = join(hwid=H)
    st, gs = co("GET", "/api/groups")
    pend = next(x for x in gs["groups"] if x["id"] == gid).get("rejoin_pending") or []
    target = next((p for p in pend if p["node"] == d4.get("id")), None)
    if target:
        co("POST", f"/api/groups/{gid}/rejoin/{target['node']}/reject", {})
    ns = nodes()
    check("當成新機器：兩台都留著，不接回", target is not None and d4["id"] in ns and target["claims"] in ns)
    st, d5 = join(hwid=H)
    st, gs = co("GET", "/api/groups")
    pend = next(x for x in gs["groups"] if x["id"] == gid).get("rejoin_pending") or []
    check("  識別碼同時跟兩格一樣（分不出是哪一台）：不列待核准，當成新機器", st == 200 and not pend, pend)

    co("POST", f"/api/groups/{gid}/update", {"settings": {"allow_rejoin": False}})
    st, d6 = join(rejoin=d4.get("rejoin"), hwid=H)
    st, gs = co("GET", "/api/groups")
    pend = next(x for x in gs["groups"] if x["id"] == gid).get("rejoin_pending") or []
    check("群組沒開「允許接回」：接回憑證不算數、也不列待核准", st == 200 and d6.get("id") != d4.get("id")
          and not d6.get("via") and not pend, (d6, pend))
    st, d7 = join(rejoin="x" * 40)
    check("  亂猜的接回憑證：當成新機器", st == 200 and not d7.get("via"))
finally:
    for p in procs:
        p.terminate()

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
