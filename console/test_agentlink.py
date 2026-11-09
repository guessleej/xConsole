#!/usr/bin/env python3
"""代理主動連回（工作站模式）主控台這一側的檢查。不碰正式環境，全部在隔離目錄與隨機埠上跑。

一、指令佇列（agentlink.Link，不開網路）：離線立刻失敗、配對、逾時回 504 並送取消、
    沒收到的重送、收到的不重送、長的不擋短的、串流逐行讀、缺片段當失敗、逾時後才到的結果丟掉。
二、端對端：另起一個主控台與一支真的代理，代理只靠 agent-link.json 連回；經網頁 API 看得到那台、
    查得到狀態、改位址時的連線測試走連回通道；權杖錯的輪詢被擋。
    通道另外聽一個埠、用專用的通道憑證：換管理網頁的憑證不影響；通道憑證輪替、對外網址在網頁上改，代理幾秒內收到。
三、連線上限：同一個來源 200 支已驗證的代理同時掛著長輪詢全部連得上；
    同一個來源沒登入的連線仍在第 65 條被擋。
四、通道憑證（linkcert.Ring）：私鑰不落明文、輪替要在線的都收到才換、SNI 選憑證、退役 30 天後刪掉。
跑法：python3 test_agentlink.py
"""
import base64
import http.client
import http.cookiejar
import json
import os
import re
import secrets
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agentlink  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


def frame(cid, seq, data=b"", more=False, status=None, ctype="application/json"):
    fr = {"id": cid, "seq": seq, "data": base64.b64encode(data).decode(), "more": more}
    if seq == 0:
        fr.update(status=status, ctype=ctype)
    return fr


def unit():
    logs = []
    L = agentlink.Link(log=logs.append)
    try:
        L.open("ws", "GET", "/state")
        check("離線：立刻失敗，不排進佇列", False)
    except agentlink.Offline:
        check("離線：立刻失敗，不排進佇列", not L._nodes.get("ws", {}).get("q"))

    # 代理先來輪詢一次（掛著），主控台這時送指令
    got = {}

    def agent_poll(seen=(), hold=5):
        got["cmds"] = L.poll("ws", list(seen), hold=hold)

    t = threading.Thread(target=agent_poll)
    t.start()
    time.sleep(0.2)
    res = {}

    def caller():
        try:
            r = L.open("ws", "POST", "/x", b'{"a":1}', timeout=5)
            res["r"] = (r.status, json.loads(r.read()))
        except Exception as exc:  # noqa: BLE001
            res["r"] = exc
    c = threading.Thread(target=caller)
    c.start()
    t.join(3)
    cmds = got.get("cmds") or []
    check("掛著的輪詢一有指令就立刻回", len(cmds) == 1 and cmds[0]["path"] == "/x"
          and base64.b64decode(cmds[0]["body"]) == b'{"a":1}', cmds)
    cid = cmds[0]["id"]
    check("  指令編號是主控台產的隨機值", len(cid) == 24)
    again = L.poll("ws", [], hold=0)
    check("代理說沒收到（seen 裡沒有）：再送一次", [x["id"] for x in again] == [cid], again)
    again = L.poll("ws", [cid], hold=0)
    check("代理說收到了：不再送", again == [])
    check("  回送的結果配對到那個呼叫", L.result("ws", frame(cid, 0, b'{"ok":true}', status=201)))
    c.join(3)
    check("  呼叫拿到狀態碼與內容", res.get("r") == (201, {"ok": True}), res.get("r"))
    check("別台送來同一個編號：不認", not L.result("other", frame(cid, 0, b"{}", status=200)))
    check("逾時之後才到的結果：丟掉並記一筆", not L.result("ws", frame(cid, 0, b"{}", status=200)) and logs, logs)

    # 逾時：回 504（Deadline），已送出的要另送一筆取消
    res.clear()

    def slow():
        try:
            L.open("ws", "GET", "/slow", timeout=1)
        except Exception as exc:  # noqa: BLE001
            res["e"] = exc
    s = threading.Thread(target=slow)
    s.start()
    time.sleep(0.1)
    sc = L.poll("ws", [], hold=1)
    s.join(3)
    check("過了期限沒回：Deadline（call_node 回 504）", isinstance(res.get("e"), agentlink.Deadline), res.get("e"))
    cancel = L.poll("ws", [sc[0]["id"]], hold=0)
    check("  另送一筆取消，帶要取消的那個編號", len(cancel) == 1 and cancel[0].get("cancel") == sc[0]["id"], cancel)
    check("  取消送出去就好，不會一直重送", L.poll("ws", [], hold=0) == [])
    res.clear()

    def never():
        try:
            L.open("ws", "GET", "/never", timeout=0.5)
        except Exception as exc:  # noqa: BLE001
            res["e"] = exc
    s = threading.Thread(target=never)
    s.start()
    s.join(3)
    check("還沒送出就逾時：直接從佇列拿掉，不送取消", L.poll("ws", [], hold=0) == [] and isinstance(res.get("e"), agentlink.Deadline))

    # 長的不擋短的：同時送一個長的與三個短的，短的先回
    done = []

    def call(path, to):
        r = L.open("ws", "GET", path, timeout=to)
        done.append((path, r.read()))
    ths = [threading.Thread(target=call, args=("/load", 10))] + \
          [threading.Thread(target=call, args=(f"/state{i}", 5)) for i in range(3)]
    for th in ths:
        th.start()
    time.sleep(0.2)
    batch = L.poll("ws", [], hold=1)
    check("一次輪詢回好幾筆", len(batch) == 4, len(batch))
    ids = {x["path"]: x["id"] for x in batch}
    for i in range(3):
        L.result("ws", frame(ids[f"/state{i}"], 0, b"{}", status=200))
    time.sleep(0.3)
    check("長的還在跑，短的都先回來了", sorted(p for p, _ in done) == ["/state0", "/state1", "/state2"], done)
    L.result("ws", frame(ids["/load"], 0, b'{"loaded":1}', status=200))
    for th in ths:
        th.join(3)
    check("  長的後來也回來", len(done) == 4)

    # 串流：一片一片來，呼叫端逐行讀
    out = {}

    def stream():
        r = L.open("ws", "POST", "/chat/a", b"{}", timeout=5)
        out["ctype"] = r.headers.get("Content-Type")
        out["lines"] = [ln for ln in r]
    st = threading.Thread(target=stream)
    st.start()
    time.sleep(0.1)
    sid = L.poll("ws", [], hold=1)[0]["id"]
    L.result("ws", frame(sid, 0, b"", more=True, status=200, ctype="text/event-stream"))
    L.result("ws", frame(sid, 1, b"data: 1\n\nda", more=True))
    L.result("ws", frame(sid, 1, b"data: 1\n\nda", more=True))          # 網路重送的同一片：不重複
    L.result("ws", frame(sid, 2, b"ta: 2\n\n", more=False))
    st.join(3)
    check("串流：型別照帶、逐行讀、重送的片段不重複", "event-stream" in (out.get("ctype") or "")
          and out.get("lines") == [b"data: 1\n", b"\n", b"data: 2\n", b"\n"], out)

    out.clear()

    def gap():
        try:
            r = L.open("ws", "GET", "/g", timeout=3)
            out["r"] = r.read()
        except Exception as exc:  # noqa: BLE001
            out["r"] = exc
    g = threading.Thread(target=gap)
    g.start()
    time.sleep(0.1)
    gid = L.poll("ws", [], hold=1)[0]["id"]
    L.result("ws", frame(gid, 0, b"a", more=True, status=200))
    L.result("ws", frame(gid, 2, b"c", more=False))
    g.join(4)
    check("中間掉了一片：結果不完整，不當成成功", out.get("r") == b"a", out.get("r"))
    check("壞掉的片段（不是 base64）：不收", not L.result("ws", {"id": gid, "seq": 0, "data": "@@"}))

    L2 = agentlink.Link(log=lambda m: None)
    threading.Thread(target=lambda: L2.poll("gone", [], hold=0)).start()
    time.sleep(0.1)
    res.clear()

    def removed():
        try:
            L2.open("gone", "GET", "/x", timeout=5)
        except Exception as exc:  # noqa: BLE001
            res["e"] = exc
    r2 = threading.Thread(target=removed)
    r2.start()
    time.sleep(0.2)
    t0 = time.time()
    L2.forget("gone")
    r2.join(3)
    check("機器被移除：還在等的呼叫立刻失敗", isinstance(res.get("e"), agentlink.Offline) and time.time() - t0 < 1, res.get("e"))


def ring():
    """通道憑證（linkcert.Ring，不開主控台）：私鑰不落明文、重開沿用、輪替要在線的都收到才換、SNI 選憑證、30 天後刪掉。"""
    import hashlib
    import linkcert
    d = tempfile.mkdtemp(prefix="xc-ring-")
    r = linkcert.Ring(d)
    cur = r.current()["fp"]
    raw = open(r.file).read()
    keyf = open(r._path(cur)).read()
    check("通道憑證：第一次就產一張；ring.json 沒有私鑰，私鑰檔是加密的",
          len(r.d["keys"]) == 1 and "PRIVATE KEY" not in raw and "ENCRYPTED PRIVATE KEY" in keyf
          and (os.name == "nt" or os.stat(r._path(cur)).st_mode & 0o077 == 0))
    check("  重開沿用同一張", linkcert.Ring(d).current()["fp"] == cur)
    ok, _ = r.rotate()
    nx = r.pending()["fp"]
    ok2, msg2 = r.rotate()
    check("輪替：產下一張；上一次還沒完成時不能再換", ok and nx != cur and not ok2 and "還沒完成" in msg2)
    pem, c = r.trusted()
    check("  發給代理的是目前這張＋下一張", c == cur and pem.count("BEGIN CERTIFICATE") == 2)
    online = {"a", "b"}.__contains__
    r.ack("a", r.version())
    check("  在線的還有一台沒收到：不換", r.check(["a", "b", "off"], online) is False and r.current()["fp"] == cur
          and r.summary(["a", "b", "off"], online)["waiting"] == ["b"])
    r.ack("b", r.version())
    r.ack("x", 10 ** 6)                                   # 比主控台的版本還大：不收
    check("  在線的都收到了：換上新的；沒上線的不用等", r.check(["a", "b", "off"], online) is True and r.current()["fp"] == nx
          and "x" not in r.acked)
    old = next(k for k in r.d["keys"] if k["fp"] == cur)
    check("  舊的改成退役，保留 30 天；不再發給代理", old["state"] == "retired"
          and abs(old["retire_at"] - time.time() - 30 * 86400) < 60 and r.trusted()[0].count("BEGIN CERTIFICATE") == 1)

    # SNI：要哪張給哪張，不認得的給目前這張
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(8)

    def serve():
        while True:
            try:
                c, _ = ls.accept()
            except OSError:
                return
            try:
                t = r.ctx.wrap_socket(c, server_side=True)
                t.close()
            except OSError:
                c.close()
    threading.Thread(target=serve, daemon=True).start()

    def got(name):
        cx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cx.check_hostname, cx.verify_mode = False, ssl.CERT_NONE   # 測試只看主控台送哪一張
        with socket.create_connection(ls.getsockname()) as raw_s:
            with cx.wrap_socket(raw_s, server_hostname=name) as t:
                return hashlib.sha256(t.getpeercert(binary_form=True)).hexdigest()
    check("  SNI 要退役那張：還拿得到（沒上線的機器 30 天內連得回來）", got(linkcert.tag(cur)) == cur)
    check("  SNI 要新的、或不認得：給目前這張", got(linkcert.tag(nx)) == nx and got("x.example") == nx)
    old["retire_at"] = int(time.time()) - 1
    v0 = r.version()
    check("  過了 30 天：退役的刪掉，私鑰檔一起刪，版本加一", r.check([], online) is True and len(r.d["keys"]) == 1
          and not os.path.exists(r._path(cur)) and r.version() == v0 + 1)
    check("  刪掉之後要舊的那張：拿到的是目前這張（舊機器要重新加入）", got(linkcert.tag(cur)) == nx)
    ls.close()
    shutil.rmtree(d, ignore_errors=True)


def agent_fail_max():
    import ast
    tree = ast.parse(open(os.path.join(HERE, "xconsole_server.py"), encoding="utf-8").read())
    return next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "AGENT_FAIL_MAX") + 1


def join_save():
    """安裝腳本讀加入回應的那段（bash 與 PowerShell 共用）：推送模式寫連回設定、改回主控台連這台時清掉。"""
    import ast
    tree = ast.parse(open(os.path.join(HERE, "xconsole_server.py"), encoding="utf-8").read())
    code = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "JOIN_SAVE_PY")
    home = tempfile.mkdtemp(prefix="xc-joinsave-")
    sd = os.path.join(home, ".xconsole")
    os.makedirs(sd)

    def run(resp):
        return subprocess.run([sys.executable, "-c", code], input=json.dumps(resp), text=True, capture_output=True,
                              env={**os.environ, "XCONSOLE_HOME": home})
    pem = "-----BEGIN CERTIFICATE-----\nAA\n-----END CERTIFICATE-----\n"
    r = run({"ok": True, "link": "push", "message": "m", "console": {"url": "https://a:1", "node": "w", "cert": pem,
                                                                   "current": "ab", "public": ["b:2"], "proxy": "", "v": "3-x"}})
    files = sorted(os.listdir(sd))
    meta = json.load(open(os.path.join(sd, "agent-link.json"))) if "agent-link.json" in files else {}
    check("  連回設定帶通道憑證版本與對外網址，不帶憑證內容", meta == {"url": "https://a:1", "node": "w", "current": "ab",
                                                            "public": ["b:2"], "proxy": "", "v": "3-x"}, meta)
    check("加入成主動連回：寫下連回設定與通道憑證（權限 600）", r.returncode == 0 and files == ["agent-link.json", "agent-link.pem"]
          and all(os.stat(os.path.join(sd, f)).st_mode & 0o077 == 0 for f in files) or os.name == "nt", (r.stderr, files))
    run({"ok": False, "error": "加入碼不對"})
    check("  加入失敗：不動既有的連回設定", sorted(os.listdir(sd)) == files)
    r = run({"ok": True, "id": "w", "message": "已加入"})
    check("  重裝後改由主控台連這台：舊的連回設定清掉，代理不會再連回舊主控台", r.returncode == 0 and os.listdir(sd) == [], os.listdir(sd))
    shutil.rmtree(home, ignore_errors=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def e2e():
    W = tempfile.mkdtemp(prefix="xc-link-e2e-")
    chome, ahome = os.path.join(W, "console"), os.path.join(W, "agent")
    for d in (chome, ahome):
        os.makedirs(os.path.join(d, ".xconsole"), mode=0o700)
    port, aport, lport = free_port(), free_port(), free_port()
    token = secrets.token_urlsafe(32)
    with open(os.path.join(ahome, ".xconsole", "token"), "w") as fh:
        fh.write(token)
    # 主控台的機器名單：一台真的工作站（主動連回）＋ 200 台假的（只用來測連線上限）
    fakes = [{"id": f"fake-{i:03d}", "name": f"fake-{i:03d}", "host": "10.9.0.1", "link": "push",
              "token": secrets.token_urlsafe(24)} for i in range(200)]
    nodes = [{"id": "ws-01", "name": "教室 01", "host": "127.0.0.1", "group": "教室", "link": "push", "token": token}] + fakes
    with open(os.path.join(chome, ".xconsole", "fleet.json"), "w") as fh:
        json.dump({"nodes": nodes, "auth": {}}, fh)
    srv = os.path.join(HERE, "xconsole_server.py")
    cenv = {**os.environ, "HOME": chome, "XCONSOLE_HOME": chome, "XCONSOLE_PORT": str(port), "XCONSOLE_LINK_PORT": str(lport)}
    subprocess.run([sys.executable, srv, "--set-credentials"], input=b"admin\nlink-pass-2026\n",
                   env=cenv, check=True, capture_output=True)
    cons = subprocess.Popen([sys.executable, srv], env=cenv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    agent = None
    try:
        for _ in range(100):
            try:
                ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
                ssl.get_server_certificate(("127.0.0.1", lport), timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        cert = os.path.join(chome, ".xconsole", "tls", "cert.pem")
        ringf = os.path.join(chome, ".xconsole", "link", "ring.json")

        def ring_now():
            return json.load(open(ringf))
        lcert = os.path.join(W, "link.pem")
        open(lcert, "w").write(next(k["cert"] for k in ring_now()["keys"] if k["state"] == "current"))
        check("通道憑證跟管理網頁的不同張", open(lcert).read() != open(cert).read())
        fleet = json.load(open(os.path.join(chome, ".xconsole", "fleet.json")))
        check("主控台啟動時把明文權杖轉成密文", all(n["token"].startswith("enc:v1:") for n in fleet["nodes"]))

        # 代理：只靠 agent-link.json＋通道憑證連回通道埠（安裝腳本寫的就是這兩個檔）
        alink = os.path.join(ahome, ".xconsole", "agent-link.json")
        apem = os.path.join(ahome, ".xconsole", "agent-link.pem")
        shutil.copy(lcert, apem)
        with open(alink, "w") as fh:
            json.dump({"url": f"https://127.0.0.1:{lport}", "node": "ws-01"}, fh)
        aenv = {**os.environ, "HOME": ahome, "XCONSOLE_HOME": ahome, "XCONSOLE_AGENT_PORT": str(aport)}
        agent = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "agent", "xconsole_agent.py"),
                                  "--home", ahome, "--bind", "127.0.0.1"],
                                 env=aenv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        jar = http.cookiejar.CookieJar()

        def opener():
            c = ssl.create_default_context(cafile=cert)
            c.check_hostname = False
            return urllib.request.build_opener(urllib.request.HTTPSHandler(context=c), urllib.request.HTTPCookieProcessor(jar))
        op = opener()
        ctx = ssl.create_default_context(cafile=lcert)      # 通道埠用通道憑證
        ctx.check_hostname = False
        base = f"https://127.0.0.1:{port}"

        def call(path, body=None, timeout=60, at=None):
            req = urllib.request.Request((at or base) + path, method="POST" if body is not None else "GET",
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            try:
                with op.open(req, timeout=timeout) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)

        code, _ = call("/api/login", {"username": "admin", "password": "link-pass-2026"})
        check("隔離主控台登入", code == 200, code)
        ws = None
        for _ in range(60):
            code, fl = call("/api/fleet")
            ws = next((n for n in fl.get("nodes", []) if n["id"] == "ws-01"), None)
            if ws and ws["online"]:
                break
            time.sleep(1)
        check("工作站經連回通道上線，狀態查得到（記憶體池）", ws and ws["online"] and "pools" in (ws["state"] or {}),
              ws and ws.get("error"))
        fake = next((n for n in fl.get("nodes", []) if n["id"] == "fake-000"), None)
        check("  沒有連回的機器：顯示連不上（不排舊指令）", fake and not fake["online"] and "沒有連回" in (fake["error"] or ""),
              fake and fake.get("error"))
        code, nl = call("/api/nodes")
        me = next(n for n in nl["nodes"] if n["id"] == "ws-01")
        check("機器清單帶 link=push、不帶權杖", me.get("link") == "push" and "token" not in me, me)
        code, out = call("/api/nodes/ws-01/update", {"name": "教室 01", "group": "教室", "host": "127.0.0.2"})
        check("改位址時的連線測試走連回通道（不是去連 127.0.0.2:9401）", code == 200, out)
        code, lib = call("/api/node/ws-01/library")
        check("模型庫這種一般頁面不用改就能用", code == 200, (code, lib))
        al = json.load(open(alink))
        check("代理第一次輪詢就拿到通道設定版本，存進 agent-link.json", re.fullmatch(r"\d+-[0-9a-f]{8}", al.get("v") or ""), al)

        # 分埠：通道埠只回代理的路徑，管理網頁埠不收代理連回
        lop = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        for pth, body in (("/api/fleet", None), ("/api/login", {"username": "admin", "password": "link-pass-2026"})):
            req = urllib.request.Request(f"https://127.0.0.1:{lport}{pth}", data=json.dumps(body).encode() if body else None,
                                         headers={"Content-Type": "application/json"})
            try:
                st = lop.open(req, timeout=10).status
            except urllib.error.HTTPError as e:
                st = e.code
            check(f"通道埠不回管理網頁的路徑：{pth} → 404", st == 404, st)

        def mgmt_poll():
            c = http.client.HTTPSConnection("127.0.0.1", port, context=opener_ctx(), timeout=10)
            c.request("POST", "/api/agent/poll", body=b'{"seen":[]}', headers={
                "Authorization": f"Bearer {token}", "X-XConsole-Node": "ws-01", "Content-Type": "application/json"})
            r = c.getresponse()
            out = r.status, r.read()
            c.close()
            return out

        def opener_ctx():
            c = ssl.create_default_context(cafile=cert)
            c.check_hostname = False
            return c
        st, b = mgmt_poll()
        check("管理網頁埠不收代理連回（對外只開通道埠時兩邊分得開）", st != 200 and b"cmds" not in b, (st, b[:80]))
        code, li = call("/api/link")
        check("設定頁看得到通道：埠、有在聽、一張目前的通道憑證", code == 200 and li["port"] == lport and li["listen"]["ok"]
              and [k["state"] for k in li["ring"]["keys"]] == ["current"] and li["nodes"] == 201, li)

        # 換管理網頁的憑證：通道憑證不動，代理不斷線
        code, out = call("/api/tls/bootstrap", {})
        op = opener()
        code2, st = call("/api/node/ws-01/state")
        check("換管理網頁的憑證：通道憑證不變，工作站照樣查得到", code == 200 and code2 == 200 and "pools" in st
              and next(k["cert"] for k in ring_now()["keys"] if k["state"] == "current") == open(lcert).read(), (code, out, code2))

        # 通道憑證輪替：產下一張 → 代理收到並回報 → 在線的都收到就換上；舊的退役保留 30 天
        old_fp = next(k["fp"] for k in ring_now()["keys"] if k["state"] == "current")
        t0 = time.time()
        code, out = call("/api/link/rotate", {})
        rot = None
        for _ in range(40):
            rot = call("/api/link")[1]["ring"]
            if not rot["rotating"] and len(rot["keys"]) == 2:
                break
            time.sleep(0.25)
        states = {k["fingerprint256"]: k["state"] for k in rot["keys"]}
        new_fp = next((f for f, st_ in states.items() if st_ == "current"), "")
        check(f"輪替：在線的工作站幾秒內收到並回報，換上新的（{time.time() - t0:.1f} 秒）",
              code == 200 and states.get(old_fp) == "retired" and new_fp and new_fp != old_fp and time.time() - t0 < 10, (out, rot))
        al = json.load(open(alink))
        for _ in range(20):
            al = json.load(open(alink))
            if al.get("current") == new_fp:
                break
            time.sleep(0.25)
        check("  代理只認新的那張（退役的不留）", al.get("current") == new_fp and open(apem).read().count("BEGIN CERTIFICATE") == 1, al)
        code, st = call("/api/node/ws-01/state")
        check("  換完照樣下得了指令", code == 200 and "pools" in st, code)
        ctx = ssl.create_default_context(cafile=apem)    # 之後測試直接打通道埠的請求改信新的那張
        ctx.check_hostname = False

        # 對外網址與代理伺服器：在網頁上改，代理馬上收到；不合法的擋下
        for bad in ({"public": ["no-port"]}, {"proxy": "http://u:p@10.0.0.1:3128"}, {"proxy": "socks5://10.0.0.1:1080"}):
            code, _ = call("/api/link/settings", bad)
            check(f"通道設定不合法：400（{bad}）", code == 400, code)
        code, _ = call("/api/link/settings", {"public": ["vpn.example.com:9402"], "proxy": ""})
        for _ in range(20):
            al = json.load(open(alink))
            if al.get("public") == ["vpn.example.com:9402"]:
                break
            time.sleep(0.25)
        check("改對外網址：工作站幾秒內就收到", code == 200 and al.get("public") == ["vpn.example.com:9402"], al)

        # 權杖不對：擋下；同一來源錯太多次被限速
        def raw_poll(node, tok, hold_body=None):
            c = http.client.HTTPSConnection("127.0.0.1", lport, context=ctx, timeout=40)
            c.request("POST", "/api/agent/poll", body=json.dumps(hold_body or {"seen": []}).encode(),
                      headers={"Authorization": f"Bearer {tok}", "X-XConsole-Node": node,
                               "Content-Type": "application/json"})
            r = c.getresponse()
            st = r.status
            r.read()
            c.close()
            return st
        check("權杖不對的輪詢：401", raw_poll("ws-01", "x" * 40) == 401)
        check("不是主動連回的機器（不存在）：401", raw_poll("nope", token) == 401)
        sts = [raw_poll("ws-01", "y" * 40) for _ in range(agent_fail_max())]
        check("同一來源權杖錯太多次：之後錯的回 429", sts[-1] == 429 and 401 in sts, sts[-3:])
        check("  同一個位址後面權杖正確的工作站不受影響（NAT 後面有人亂送也擋不掉整群）",
              raw_poll("fake-000", fakes[0]["token"], {"seen": []}) == 200)

        # ---- 三、連線上限
        held, results, lock = [], [], threading.Lock()
        lv = json.load(open(alink))["v"]      # 跟真的代理一樣回報手上的通道設定版本：版本對了才會掛著等

        def hold(n):
            try:
                c = http.client.HTTPSConnection("127.0.0.1", lport, context=ctx, timeout=60)
                c.connect()
                with lock:
                    held.append(c)
                c.request("POST", "/api/agent/poll", body=json.dumps({"seen": [], "link": lv}).encode(),
                          headers={"Authorization": f"Bearer {n['token']}", "X-XConsole-Node": n["id"],
                                   "Content-Type": "application/json"})
                r = c.getresponse()
                body = json.loads(r.read())
                with lock:
                    results.append((r.status, body.get("cmds")))
            except Exception as exc:  # noqa: BLE001
                with lock:
                    results.append((0, str(exc)))
        ths = [threading.Thread(target=hold, args=(n,)) for n in fakes]
        t0 = time.time()
        for th in ths:
            th.start()
            time.sleep(0.01)
        time.sleep(4)                         # 全部都在主控台那邊掛著
        early = len(results)
        # 這時同一個來源再開沒登入的連線：前 64 條收、第 65 條被關
        anon = []
        refused = 0
        for _ in range(70):
            anon.append(socket.create_connection(("127.0.0.1", lport), timeout=5))
        time.sleep(1)
        # 被擋的那幾條主控台一接受就關掉：一起看哪些已經被關（逐條等會等到握手逾時 15 秒，沒被擋的也會被關）
        for s in anon:
            try:
                s.send(b"\x16")             # 像 TLS 握手的第一個位元組
            except OSError:
                pass
        rd, _, _ = select.select(anon, [], [], 1)
        for s in rd:
            try:
                if s.recv(1) == b"":
                    refused += 1
            except OSError:
                refused += 1
        for s in anon:
            s.close()
        for th in ths:
            th.join(40)
        ok200 = sum(1 for st, cmds in results if st == 200 and cmds == [])
        check(f"同一來源 200 支已驗證的代理同時掛著長輪詢：全部連得上（{ok200}/200）",
              ok200 == 200 and early == 0 and time.time() - t0 >= 20, (ok200, early, [r for r in results if r[0] != 200][:3]))
        check(f"同一來源沒登入的連線：仍在第 65 條起被擋（70 條被關 {refused} 條）", refused == 6, refused)
        code, out = call("/api/link/rotate", {})
        code2, out2 = call("/api/link/rotate", {})
        w = call("/api/link")[1]["ring"]
        check("在線的機器還沒收到新的通道憑證：不換上，設定頁列出還在等哪幾台；不能再按一次",
              code == 200 and w["rotating"] and len(w["waiting"]) >= 190 and "ws-01" not in w["waiting"]
              and code2 == 409, (out, out2, len(w["waiting"])))
        for c in held:
            c.close()
    finally:
        for p in (agent, cons):
            if p is not None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        if agent is not None and not OK:
            print("---- 代理記錄\n" + agent.stdout.read().decode(errors="replace")[-3000:])
            print("---- 主控台記錄\n" + cons.stdout.read().decode(errors="replace")[-3000:])
        shutil.rmtree(W, ignore_errors=True)


if __name__ == "__main__":
    unit()
    ring()
    join_save()
    e2e()
    print("全部通過" if OK else "有項目失敗")
    sys.exit(0 if OK else 1)
