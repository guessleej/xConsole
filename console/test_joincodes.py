#!/usr/bin/env python3
"""群組加入碼（清單 #6）：可用次數、有效時間、名稱樣板、限定網段、作廢、主控台重啟後仍有效。

前半是 joincodes 模組本身；後半在隔離 HOME 起一支真的代理與主控台，照安裝腳本最後一步的格式呼叫 /api/join。
跑法：python3 console/test_joincodes.py
"""
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import joincodes as J  # noqa: E402

W = tempfile.mkdtemp(prefix="xc-jc-")
PW = "join-codes-test-2026"
OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail and not cond else ""))
    OK = OK and bool(cond)


def raises(fn, word):
    try:
        fn()
    except ValueError as ex:
        return word in str(ex)
    return False


def unit():
    check("樣板 A{01..03} 補零展開", J.expand("A{01..03}") == ["A01", "A02", "A03"])
    check("  前後綴都保留、位數照起頭", J.expand("PC-{8..10}-x") == ["PC-8-x", "PC-9-x", "PC-10-x"])
    check("  沒有大括號就是一般名稱", J.expand("三樓教室") is None)
    for bad in ("A{3..1}", "A{1..2}{3..4}", "A{x}", "A{0..1000}", "A{01..03}{bad}", "A{{1..3}}"):
        check(f"  不合法的樣板 {bad} 拒絕", raises(lambda b=bad: J.expand(b), "樣板"))
    check("網段可用逗號或空白分隔", J.parse_cidrs("192.168.10.0/24, 10.0.0.5") == ["192.168.10.0/24", "10.0.0.5/32"])
    check("  不是網段就拒絕", raises(lambda: J.parse_cidrs(["abc"]), "不是網段"))
    rec = {"cidrs": ["192.168.10.0/24"]}
    check("限定網段：網段內放行、網段外擋、IPv4 對映的 IPv6 照樣認",
          J.ip_allowed(rec, "192.168.10.7") and not J.ip_allowed(rec, "10.0.0.1") and J.ip_allowed(rec, "::ffff:192.168.10.7"))

    p = os.path.join(W, "codes.json")
    c = J.Codes(p)
    check("可用次數不能是 0", raises(lambda: c.add("x", uses=0), "可用次數"))
    check("有效時間不能超過 7 天", raises(lambda: c.add("x", minutes=7 * 24 * 60 + 1), "有效時間"))
    check("多台卻給同一個名稱 → 要用樣板", raises(lambda: c.add("x", uses=3, name="教室"), "樣板"))
    check("樣板名稱比次數少 → 拒絕", raises(lambda: c.add("x", uses=5, name="A{1..3}"), "比可用次數"))
    check("重新安裝只能一次", raises(lambda: c.add("x", uses=2, replace="n1"), "只能是 1"))
    check("樣板扣掉已有機器在用的名稱後不夠次數 → 拒絕並點名", raises(lambda: c.add("x", uses=3, name="A{01..03}", taken={"A01"}), "A01"))
    r = c.add("code-1", uses=2, name="A{01..03}", group="教室", group_id="g1", console="h:1", taken={"A01"})
    check("檔案權限 0600、只存雜湊不存碼", oct(os.stat(p).st_mode & 0o777) == "0o600" and "code-1" not in open(p).read())
    out = {}

    def take(tag):
        out[tag] = c.reserve("code-1", "127.0.0.1", {"A01"})
        time.sleep(0.3)
        if tag == "t1":
            c.finish(out[tag][0], "A02", out[tag][1], "127.0.0.1")
        else:
            c.release()
    ts = [threading.Thread(target=take, args=(t,)) for t in ("t1", "t2")]
    [t.start() for t in ts]
    time.sleep(0.1)
    third = c.reserve("code-1", "127.0.0.1")
    [t.join() for t in ts]
    names = sorted(v[1] for v in out.values())
    check("兩個同時加入各拿到不同名稱，略過已被別台用掉的 A01", names == ["A02", "A03"], out)
    check("  名額都被預留時第三個擋下", third[0] is None and "名額已滿" in third[2], third)
    check("  一個完成、一個放回：已用 1／2，還是等待中", r["used"] == 1 and J.state(r) == "waiting")
    c2 = J.Codes(p)
    check("重新載入（＝主控台重啟）加入碼照樣有效", c2.usable("code-1") is not None)
    rr = c2.reserve("code-1", "127.0.0.1")
    check("  重啟後沒有殘留預留，下一個名稱是 A01", rr[1] == "A01", rr)
    c2.finish(rr[0], "A01", rr[1], "127.0.0.1")
    check("  用完 → 已加入，不能再用", J.state(c2.get("code-1")) == "joined" and c2.reserve("code-1", "127.0.0.1")[0] is None)
    c2.add("code-g", uses=2, group="教室", group_id="g1")
    c2.rename_group("g1", "三樓教室")
    check("群組改名：還能用的加入碼跟著改", c2.get("code-g")["group"] == "三樓教室")
    check("群組刪除：它還能用的加入碼一起作廢（已用完的不動）", c2.revoke_group("g1") == 1
          and J.state(c2.get("code-g")) == "revoked" and J.state(c2.get("code-1")) == "joined")
    c2.add("code-2", minutes=5, cidrs=["10.0.0.0/8"])
    check("限定網段：網段外下載不到腳本、也加入不了",
          c2.usable("code-2", "127.0.0.1") is None and "網段" in c2.reserve("code-2", "127.0.0.1")[2])
    rid = c2.get("code-2")["id"]
    c2.revoke(rid)
    check("作廢之後立刻不能用", c2.usable("code-2", "10.1.1.1") is None and J.state(c2.get("code-2")) == "revoked")
    c2.get("code-2")["exp"] = c2.get("code-2")["closed"] = time.time() - J.KEEP - 10
    c2.add("code-3")
    check("過期超過 7 天的紀錄自動清掉", c2.get("code-2") is None and c2.get("code-3") is not None)
    pub = J.public(c2.get("code-3"))
    check("給網頁的欄位不含憑證公鑰與雜湊", "pin" not in pub and "fp" not in pub and len(pub["id"]) == 12)


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
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json", **(headers or {})})
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


def e2e():
    procs = []
    try:
        ahome = os.path.join(W, "agent")
        os.makedirs(os.path.join(ahome, ".xconsole"))
        json.dump({"machine": {"name": "a", "memory_model": "discrete"}, "slots": []},
                  open(os.path.join(ahome, ".xconsole", "registry.json"), "w"))
        aport = free_port()
        procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py")],
                                      env={**os.environ, "HOME": ahome, "XCONSOLE_AGENT_PORT": str(aport)},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("節點代理啟動", wait_tls(aport))
        token = open(os.path.join(ahome, ".xconsole", "token")).read().strip()
        acert = open(os.path.join(ahome, ".xconsole", "agent-cert.pem")).read()

        cport = free_port()
        chome = os.path.join(W, "console")
        os.makedirs(os.path.join(chome, ".xconsole"), mode=0o700)
        cenv = {**os.environ, "HOME": chome, "XCONSOLE_PORT": str(cport), "XCONSOLE_LINK_PORT": str(free_port())}
        subprocess.run([sys.executable, os.path.join(HERE, "xconsole_server.py"), "--set-credentials"],
                       input=f"admin\n{PW}\n".encode(), env=cenv, check=True, capture_output=True)

        def start():
            p = subprocess.Popen([sys.executable, os.path.join(HERE, "xconsole_server.py")], env=cenv,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(p)
            return p, wait_tls(cport)
        cp, up = start()
        check("隔離主控台啟動", up)
        ccert = os.path.join(chome, ".xconsole", "tls", "cert.pem")

        def login():
            _, _, sc = https(cport, ccert, "POST", "/api/login", {"username": "admin", "password": PW})
            return {"Cookie": "xconsole=" + sc.split("xconsole=", 1)[1].split(";", 1)[0]}
        ck = login()

        def co(m, p, b=None, raw=False):
            st, d, _ = https(cport, ccert, m, p, b, ck, raw=raw)
            return st, d

        def anon(m, p, b=None, raw=False):
            st, d, _ = https(cport, ccert, m, p, b, raw=raw)
            return st, d

        def join(code, **kw):
            return anon("POST", "/api/join", {"code": code, "token": token, "cert": acert, "hostname": "same-image",
                                              "port": aport, **kw})

        st, d = co("POST", "/api/nodes/join-code", {"name": "A{01..03}", "uses": 4, "console": f"127.0.0.1:{cport}"})
        check("產碼：樣板名稱比次數少 → 400 並說明", st == 400 and "比可用次數" in d.get("error", ""), d)
        st, d = co("POST", "/api/nodes/join-code", {"name": "x", "uses": "3", "console": f"127.0.0.1:{cport}"})
        check("產碼：次數不是整數 → 400", st == 400 and "可用次數" in d.get("error", ""), d)
        st, jc = co("POST", "/api/nodes/join-code", {"name": "A{01..03}", "uses": 3, "minutes": 120, "group": "三樓教室",
                                                     "cidrs": "127.0.0.0/8", "console": f"127.0.0.1:{cport}"})
        check("產碼：3 次、120 分鐘、樣板、限定網段", st == 200 and jc.get("uses") == 3 and jc.get("names") == ["A01", "A02", "A03"]
              and 7100 < jc["expires_at"] - time.time() <= 7200, jc)
        code = jc["command"].split("/install/")[1].split("/")[0]
        st, s1 = anon("GET", f"/install/{code}/agent.sh", raw=True)
        check("  安裝腳本下載得到", st == 200 and code in s1)

        # 主控台重啟：加入碼照樣有效（教室一次發 20 台，中途重開不能全部重來）
        cp.terminate()
        cp.wait(10)
        _, up = start()
        ck.update(login())
        check("主控台重啟後", up)
        st, d = join(code)
        check("  第 1 台加入：名稱與代號是 A01、放進三樓教室", st == 200 and d.get("id") == "A01", d)
        st, d = join(code)
        check("  第 2 台（同一個映像、主機名稱一樣）是 A02", st == 200 and d.get("id") == "A02", d)
        st, js = co("GET", f"/api/nodes/join-code/{jc['id']}")
        check("  網頁上看得到已用 2／3 與加入了哪些機器", js.get("used") == 2 and js.get("state") == "waiting"
              and [n["name"] for n in js.get("nodes", [])] == ["A01", "A02"], js)
        _, gs = co("GET", "/api/groups")
        gid = next(g["id"] for g in gs["groups"] if g["name"] == "三樓教室")
        st, _ = co("POST", f"/api/groups/{gid}/update", {"name": "301 教室"})
        st, d = join(code)
        check("  群組中途改名：第 3 台 A03 進改名後的群組，不會把舊名稱建回來", st == 200 and d.get("id") == "A03", d)
        st, d = join(code)
        check("  用完之後第 4 台擋下", st == 400 and "用完" in d.get("error", ""), d)
        st, _ = anon("GET", f"/install/{code}/agent.sh", raw=True)
        check("  用完之後腳本也下載不到", st == 404)
        _, nodes = co("GET", "/api/nodes")
        mine = {n["id"]: n for n in nodes["nodes"]}
        check("清單上三台都在 301 教室", all(mine.get(k, {}).get("group") == "301 教室" for k in ("A01", "A02", "A03")), mine)
        _, gs = co("GET", "/api/groups")
        check("  舊名稱「三樓教室」沒有被建回來", not any(g["name"] == "三樓教室" for g in gs["groups"]), [g["name"] for g in gs["groups"]])
        st, d = co("POST", "/api/nodes/join-code", {"name": "A{01..05}", "uses": 3, "console": f"127.0.0.1:{cport}"})
        check("產碼：樣板裡 A01～A03 已有機器在用，剩下的不夠 → 400", st == 400 and "已經有機器在用" in d.get("error", ""), d)
        st, d = co("POST", "/api/nodes/join-code", {"name": "B{1..2}", "uses": 2, "group": "暫時群組", "console": f"127.0.0.1:{cport}"})
        _, gs = co("GET", "/api/groups")
        tg = next((g for g in gs["groups"] if g["name"] == "暫時群組"), None)
        check("產碼時指定新群組：群組跟著建好", tg is not None, gs)
        st, dd = co("POST", f"/api/groups/{tg['id']}/delete")
        check("  刪掉群組 → 它還能用的加入碼一起作廢", st == 200 and dd.get("join_codes_revoked") == 1, dd)
        st, dj = join(d["command"].split("/install/")[1].split("/")[0])
        check("  作廢後加入不了", st == 400, dj)

        st, jc2 = co("POST", "/api/nodes/join-code", {"uses": 5, "cidrs": ["10.9.0.0/16"], "console": f"127.0.0.1:{cport}"})
        code2 = jc2["command"].split("/install/")[1].split("/")[0]
        st, _ = anon("GET", f"/install/{code2}/agent.sh", raw=True)
        check("限定網段外：腳本下載不到", st == 404)
        st, d = join(code2)
        check("  也加入不了", st == 400 and "網段" in d.get("error", ""), d)
        st, jc3 = co("POST", "/api/nodes/join-code", {"uses": 5, "console": f"127.0.0.1:{cport}"})
        code3 = jc3["command"].split("/install/")[1].split("/")[0]
        st, d = co("POST", f"/api/nodes/join-code/{jc3['id']}/revoke")
        check("作廢加入碼", st == 200 and d["code"]["state"] == "revoked", d)
        st, d = join(code3)
        check("  作廢後加入不了", st == 400, d)
        st, lst = co("GET", "/api/nodes/join-codes")
        ids = [x["id"] for x in lst.get("codes", [])]
        check("加入碼清單列出全部，新的在前，不含公鑰", ids[:2] == [jc3["id"], jc2["id"]] and jc["id"] in ids
              and all("pin" not in x for x in lst["codes"]), ids)
        st, _ = anon("GET", "/api/nodes/join-codes")
        check("  沒登入看不到清單", st == 401)
        st, _ = anon("POST", f"/api/nodes/join-code/{jc2['id']}/revoke")
        check("  沒登入不能作廢", st == 401)
        raw = open(os.path.join(chome, ".xconsole", "join_codes.json")).read()
        check("磁碟上只有雜湊，沒有加入碼本身", code not in raw and code2 not in raw and code3 not in raw)
    finally:
        for p in procs:
            p.terminate()


def main():
    unit()
    e2e()
    print("全部通過" if OK else "有失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
