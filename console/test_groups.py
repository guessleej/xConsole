#!/usr/bin/env python3
"""群組（清單 #4）的檢查。不碰正式環境，全部在隔離目錄與隨機埠上跑。

一、群組設定的檢查（groups.check／update／ensure，不開網路）：合法的照收、不合法的講人話擋下、
    設定真的變了版本號才加一、改名撞名不動任何東西、「未分類」不能改名。
三、群組設定對齊：主控台送給一支真的代理，代理準備並套用後回報，鎖定經主控台也改不了。
二、端對端：舊版只有群組名稱的機器，主控台啟動時補上群組物件；經網頁 API 新增、改設定、改名（成員跟著改）、
    刪除（成員移到未分類）；機器改到新群組名稱時自動建出群組。
跑法：python3 test_groups.py
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
sys.path.insert(0, HERE)
import groups  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


def bad(s):
    try:
        groups.check(s)
        return ""
    except ValueError as exc:
        return str(exc)


def unit():
    full = {"models": ["auto", "unsloth/gemma-3-4b-it-GGUF:Q4_K_M", "auto"], "allowed_models": ["auto", "a/b"],
            "profile": {"ctx": 32768, "parallel": 2, "thinking": False, "temperature": 0.7, "top_p": 0.9},
            "locked": ["profile", "models"], "reserve_user_gib": 8, "storage_root": "D:\\xconsole",
            "sources": ["huggingface", "group"], "apply_at": {"mode": "at", "at": 1800000000}, "gpu_power_limit": 300}
    s = groups.check(full)
    check("合法的設定照收：模型去重、只准用的清單不收 auto、鎖定與來源照固定順序",
          s["models"] == ["auto", "unsloth/gemma-3-4b-it-GGUF:Q4_K_M"] and s["allowed_models"] == ["a/b"]
          and s["locked"] == ["models", "profile"] and s["sources"] == ["group", "huggingface"]
          and s["apply_at"] == {"mode": "at", "at": 1800000000} and s["profile"]["ctx"] == 32768, s)
    check("什麼都沒給：用預設值（來源 Hugging Face、立即套用）", groups.check({}) == groups.defaults())
    for s_, want in (({"x": 1}, "不認得"), ({"models": ["../etc/passwd"]}, "不是模型名稱"),
                     ({"profile": {"ctx": 100}}, "上下文"), ({"profile": {"parallel": 1.5}}, "整數"),
                     ({"profile": {"thinking": "yes"}}, "思考"), ({"profile": {"seed": 1}}, "不認得"),
                     ({"locked": ["storage_root"]}, "鎖定"), ({"storage_root": "models"}, "完整路徑"),
                     ({"storage_root": "/data/../etc"}, "完整路徑"), ({"sources": []}, "至少選一個"),
                     ({"apply_at": {"mode": "later"}}, "套用時機"), ({"apply_at": {"mode": "at"}}, "指定時間"),
                     ({"gpu_power_limit": 5}, "功耗"), ({"reserve_user_gib": True}, "數字"),
                     ({"models": [None]}, "文字"), ({"allowed_models": [123]}, "文字")):
        msg = bad(s_)
        check(f"不合法的擋下並講原因：{json.dumps(s_, ensure_ascii=False)}", want in msg, msg)

    g = groups.new("電腦教室 A", "教室")
    v0 = g["version"]
    _, ch = groups.update(g, settings={"models": ["auto"]})
    _, ch2 = groups.update(g, settings={"models": ["auto"]})
    check("設定真的變了版本號加一；同樣內容再存一次不加", ch and not ch2 and g["version"] == v0 + 1, g["version"])
    before = json.dumps(g, sort_keys=True)
    try:
        groups.update(g, name="教室 B", settings={"sources": []})
        ok = False
    except ValueError:
        ok = True
    check("設定不合法：名稱也不會改（全部檢查過才動）", ok and json.dumps(g, sort_keys=True) == before)
    try:
        groups.update(g, name="教室 B", taken={"教室 B"})
        ok = False
    except ValueError as exc:
        ok = "同名" in str(exc)
    check("改成別的群組已用的名稱：擋下", ok and g["name"] == "電腦教室 A")
    u = groups.new(groups.DEFAULT)
    try:
        groups.update(u, name="別的名字")
        ok = False
    except ValueError:
        ok = True
    check("「未分類」不能改名", ok)
    for nm in ("", "x" * 31, "a<b", 'a"b'):
        try:
            groups.check_name(nm)
            ok = False
        except ValueError:
            ok = True
        check(f"群組名稱不合法：{nm[:12]!r}", ok)
    cfg = {"nodes": [{"id": "a", "group": "教室 A"}, {"id": "b"}, {"id": "c", "group": "教室 A"}, {"id": "d", "group": "Bob's lab"}]}
    check("舊資料補群組物件：每個用到的名稱一個，加上未分類；舊版收過、現在不收的名稱照樣保留",
          groups.ensure(cfg) and sorted(g["name"] for g in cfg["groups"]) == sorted(["未分類", "教室 A", "Bob's lab"])
          and not groups.ensure(cfg))
    old = next(g for g in cfg["groups"] if g["name"] == "Bob's lab")
    _, ch = groups.update(old, name="Bob's lab", settings={"models": ["auto"]})
    check("  這種舊名稱的群組照樣能改設定（名稱沒改就不重驗），也能改成合法的名稱",
          ch and groups.update(old, name="Bob lab")[0] == "Bob lab")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def e2e():
    W = tempfile.mkdtemp(prefix="xc-groups-")
    sd = os.path.join(W, ".xconsole")
    os.makedirs(sd, mode=0o700)
    port, lport = free_port(), free_port()
    nodes = [{"id": f"pc-{i}", "name": f"PC {i}", "host": f"10.0.0.{i}", "group": "電腦教室 A" if i < 3 else "未分類",
              "link": "push", "token": "t" * 40} for i in range(5)]
    json.dump({"nodes": nodes, "auth": {}}, open(os.path.join(sd, "fleet.json"), "w"))
    env = {**os.environ, "HOME": W, "XCONSOLE_HOME": W, "XCONSOLE_PORT": str(port), "XCONSOLE_LINK_PORT": str(lport)}
    srv = os.path.join(HERE, "xconsole_server.py")
    subprocess.run([sys.executable, srv, "--set-credentials"], input=b"admin\ngroup-pass-2026\n", env=env, check=True, capture_output=True)
    cons = subprocess.Popen([sys.executable, srv], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        ctx = ssl.create_default_context(cafile=os.path.join(sd, "tls", "cert.pem"))
        ctx.check_hostname = False
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                         urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def call(path, body=None):
            req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            try:
                with op.open(req, timeout=30) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)

        def by_name():
            return {g["name"]: g for g in call("/api/groups")[1]["groups"]}

        check("登入", call("/api/login", {"username": "admin", "password": "group-pass-2026"})[0] == 200)
        gs = by_name()
        check("啟動時替舊版的群組名稱補上群組物件（版本 1），機器數對得上",
              set(gs) == {"未分類", "電腦教室 A"} and gs["電腦教室 A"]["members"] == 3 and gs["未分類"]["members"] == 2
              and gs["電腦教室 A"]["version"] == 1, {k: (v["members"], v["version"]) for k, v in gs.items()})
        st, out = call("/api/groups", {"name": "業務部", "kind": "部門"})
        check("新增群組", st == 200 and out["group"]["kind"] == "部門" and out["group"]["version"] == 1, out)
        st, out = call("/api/groups", {"name": "業務部"})
        check("  同名的再建一次：409", st == 409, out)
        a = gs["電腦教室 A"]
        st, out = call(f"/api/groups/{a['id']}/update", {"settings": {"models": ["auto"], "locked": ["models"]}})
        check("改設定：版本升到 2", st == 200 and out["changed"] and out["group"]["version"] == 2, out)
        st, out = call(f"/api/groups/{a['id']}/update", {"settings": {"models": ["auto"], "locked": ["models"]}})
        check("  同樣內容再存一次：版本不動", st == 200 and not out["changed"] and out["group"]["version"] == 2, out)
        st, out = call(f"/api/groups/{a['id']}/update", {"settings": {"storage_root": "relative/path"}})
        check("  不合法的設定：400，版本不動", st == 400 and by_name()["電腦教室 A"]["version"] == 2, out)
        st, out = call(f"/api/groups/{a['id']}/update", {"name": "業務部"})
        check("  改成已有的名稱：409", st == 409, out)
        st, out = call(f"/api/groups/{a['id']}/update", {"name": "電腦教室 101"})
        fl = call("/api/nodes")[1]["nodes"]
        check("改名：成員一起改，不會掉出群組", st == 200 and sum(1 for n in fl if n["group"] == "電腦教室 101") == 3
              and by_name()["電腦教室 101"]["version"] == 2, [n["group"] for n in fl])
        st, out = call("/api/nodes/pc-4/update", {"name": "PC 4", "group": "失敗組", "host": "10.0.0.99", "agent_port": 9401})
        check("換位址連不到而改回：不會留下空的新群組", st == 400 and "失敗組" not in by_name(), (st, out))
        st, out = call("/api/nodes/pc-3/update", {"name": "PC 3", "group": "研發部", "host": "10.0.0.3", "agent_port": 9401})
        gs = by_name()
        check("機器改到新的群組名稱：自動建出群組", st == 200 and gs.get("研發部", {}).get("members") == 1, (st, out))
        st, out = call(f"/api/groups/{gs['未分類']['id']}/delete", {})
        check("「未分類」不能刪", st == 400, out)
        st, out = call(f"/api/groups/{gs['電腦教室 101']['id']}/delete", {})
        gs = by_name()
        check("刪除群組：成員移到「未分類」，機器不刪", st == 200 and out["moved"] == 3 and "電腦教室 101" not in gs
              and gs["未分類"]["members"] == 4 and len(call("/api/nodes")[1]["nodes"]) == 5, out)
        st, out = call("/api/groups/zzzzzzzz/update", {})
        check("編號不對的路徑：不受理", st in (400, 404), st)
    finally:
        cons.terminate()
        try:
            cons.wait(10)
        except subprocess.TimeoutExpired:
            cons.kill()
        if not OK:
            print(cons.stdout.read().decode(errors="replace")[-2000:])
        shutil.rmtree(W, ignore_errors=True)


def e2e_align():
    """主控台把群組設定送給一支真的代理（主控台連代理的伺服器模式），代理套用後回報；鎖定經主控台也改不了。"""
    import secrets
    W = tempfile.mkdtemp(prefix="xc-align-")
    chome, ahome = os.path.join(W, "console"), os.path.join(W, "agent")
    for d in (os.path.join(chome, ".xconsole", "trust"), os.path.join(ahome, ".xconsole")):
        os.makedirs(d, mode=0o700)
    port, lport, aport = free_port(), free_port(), free_port()
    token = secrets.token_urlsafe(32)
    open(os.path.join(ahome, ".xconsole", "token"), "w").write(token)
    agent = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "agent", "xconsole_agent.py"), "--home", ahome,
                              "--bind", "127.0.0.1"], env={**os.environ, "HOME": ahome, "XCONSOLE_HOME": ahome,
                                                           "XCONSOLE_AGENT_PORT": str(aport)},
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    cons = None
    try:
        acert = os.path.join(ahome, ".xconsole", "agent-cert.pem")
        for _ in range(100):
            if os.path.exists(acert):
                try:
                    ssl.get_server_certificate(("127.0.0.1", aport), timeout=1)
                    break
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.3)
        shutil.copy(acert, os.path.join(chome, ".xconsole", "trust", "ws-1.pem"))
        json.dump({"auth": {}, "nodes": [{"id": "ws-1", "name": "教室 01", "host": "127.0.0.1", "agent_port": aport,
                                          "group": "教室", "link": "pull", "token": token}]},
                  open(os.path.join(chome, ".xconsole", "fleet.json"), "w"))
        env = {**os.environ, "HOME": chome, "XCONSOLE_HOME": chome, "XCONSOLE_PORT": str(port), "XCONSOLE_LINK_PORT": str(lport)}
        srv = os.path.join(HERE, "xconsole_server.py")
        subprocess.run([sys.executable, srv, "--set-credentials"], input=b"admin\nalign-pass-2026\n", env=env, check=True, capture_output=True)
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
                with op.open(req, timeout=60) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)
        call("/api/login", {"username": "admin", "password": "align-pass-2026"})
        for _ in range(40):
            g = next(x for x in call("/api/groups")[1]["groups"] if x["name"] == "教室")
            if g["done"] == 1:
                break
            time.sleep(0.5)
        check("預設設定的群組也送（機器移到這種群組時，代理才會放掉舊群組的鎖定）", g["done"] == 1
              and g["nodes"][0]["applied"] == 1, g["nodes"][0])
        t0 = time.time()
        st, out = call(f"/api/groups/{g['id']}/update", {"settings": {"locked": ["models"], "apply_at": {"mode": "now"}}})
        g = None
        for _ in range(60):
            g = next(x for x in call("/api/groups")[1]["groups"] if x["name"] == "教室")
            if g["done"] == 1:
                break
            time.sleep(0.5)
        m = g["nodes"][0]
        check(f"改了群組設定：幾秒內送到代理、準備並套用，主控台看得到（{time.time() - t0:.1f} 秒）",
              st == 200 and g["ready"] == 1 and g["done"] == 1 and m["phase"] == "done" and m["applied"] == g["version"]
              and time.time() - t0 < 20, m)
        check("  每一步的紀錄回到主控台", any("已套用" in x["step"] for x in m["steps"]), m["steps"])
        st, out = call("/api/node/ws-1/slot/main/model", {"engine": "llama", "model": "x/y"})
        check("群組鎖定了模型：經主控台的機器頁也換不了（要改群組設定）", st == 423 and out.get("group_locked"), (st, out))
    finally:
        for p in (cons, agent):
            if p is not None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        if not OK:
            for p in (cons, agent):
                if p is not None:
                    print(p.stdout.read().decode(errors="replace")[-1500:])
        shutil.rmtree(W, ignore_errors=True)


if __name__ == "__main__":
    unit()
    e2e()
    e2e_align()
    print("全部通過" if OK else "有項目失敗")
    sys.exit(0 if OK else 1)
