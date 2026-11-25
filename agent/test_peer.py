#!/usr/bin/env python3
"""工作站互傳（清單 #12）：送方另起一支行程開互傳埠，收方在這支行程裡照主控台發的票證拿檔。
驗：票證（主控台簽、送方驗、60 秒、只能用一次、只限那個檔）、只送核對過的檔、路徑跳不出存放區、只收同網段、
同時最多送 2 份、續傳、收方核對 sha256、試連、主控台的排程（種子、等待、連不到改走網路、送方忙）。
跑法：python3 agent/test_peer.py（只用標準函式庫＋代理與主控台本身，Linux／macOS／Windows 都能跑）
"""
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-peer-recv-")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "console"))
import xconsole_agent as A  # noqa: E402

OK = True
SPEC = "test/Peer-Model-GGUF:Q4_K_M"
REL = "test--peer-model/Peer-Model-Q4_K_M.gguf"
REL2 = "test--peer-model/Peer-Model-Q8_0.gguf"
BIG = "test--peer-model/Peer-Model-F16.gguf"
LOOSE = "test--peer-model/Not-Verified.gguf"


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def content(n, seed):
    out, h = bytearray(), seed.encode()
    while len(out) < n:
        h = hashlib.sha256(h).digest()
        out += h * 1024
    return bytes(out[:n])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
SENDER_HOME = tempfile.mkdtemp(prefix="xc-peer-send-")
SENDER = r'''
import os, sys, time, json, hashlib
sys.path.insert(0, sys.argv[1])
import xconsole_agent as A
A.ensure_cert()
root = A.gguf_root()
files = json.loads(sys.argv[2])
for rel, (size, seed, verify) in files.items():
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    out, h = bytearray(), seed.encode()
    while len(out) < size:
        h = hashlib.sha256(h).digest()
        out += h * 1024
    with open(full, "wb") as fh:
        fh.write(bytes(out[:size]))
    if verify:
        A.verified_mark(full, hashlib.sha256(bytes(out[:size])).hexdigest())
with open(os.path.join(root, "..", "..", ".xconsole", "token-secret.txt"), "w") as fh:
    fh.write("密文不該被送出去")
A.peer_serve(True)
print(json.dumps({"token": A.TOKEN, "cert": open(A.CERT_FILE).read()}), flush=True)
time.sleep(600)
'''
FILES = {REL: (3_000_000, "a", True), REL2: (2_500_000, "b", True), BIG: (96 << 20, "c", True), LOOSE: (100_000, "d", False)}
proc = subprocess.Popen([sys.executable, "-c", SENDER, HERE, json.dumps(FILES)], stdout=subprocess.PIPE, text=True,
                        env={**os.environ, "XCONSOLE_HOME": SENDER_HOME, "XCONSOLE_PEER_PORT": str(PORT)})
try:
    info = json.loads(proc.stdout.readline())
    import xconsole_server as X  # noqa: E402

    SND = {"id": "snd", "name": "送方", "token": info["token"]}
    SHA = {rel: hashlib.sha256(content(size, seed)).hexdigest() for rel, (size, seed, _) in FILES.items()}

    def offer(rel, ticket=None, sha=None):
        return {"name": "送方", "addr": "127.0.0.1", "port": PORT, "cert": info["cert"]}, \
            {"rel": rel, "sha256": sha or SHA[rel], "size": FILES[rel][0], "ticket": ticket or X.peer_ticket(SND, rel, "recv")}

    def wait_for(fn):
        try:
            fn()
            return None
        except A.PeerWait as exc:
            return str(exc)

    code, out = A.peer_probe({"addrs": ["127.0.0.1"], "port": PORT, "cert": info["cert"]})
    case("試連：收方連得上送方的互傳埠（驗送方憑證）", code == 200 and out.get("ok") and out.get("addr") == "127.0.0.1", out)
    code, out = A.peer_probe({"addrs": ["127.0.0.1"], "port": PORT, "cert": ""})
    case("  試連：沒有送方憑證就不連（不退回不驗證）", code == 400 or not out.get("ok"), out)
    code, out = A.peer_probe({"addrs": ["8.8.8.8"], "port": PORT, "cert": info["cert"]})
    case("  試連：只試私有位址", code == 200 and not out.get("ok"), out)

    off, f = offer(REL)
    A._peer_get(off, f)
    dest = A.peer_rel_ok(REL)
    case("拿到檔、核對 sha256、記成核對過（之後可以再送給別台）",
         dest and os.path.exists(dest) and A.verified_files().get(REL, {}).get("sha256") == SHA[REL])
    os.remove(dest)
    case("  同一張票證再用一次：送方拒絕", "用過" in (wait_for(lambda: A._peer_get(off, f)) or ""))

    X.PEER_TICKET_TTL = -1
    off, f = offer(REL)
    X.PEER_TICKET_TTL = 60
    case("過期的票證：送方拒絕", "過期" in (wait_for(lambda: A._peer_get(off, f)) or ""))
    off, f = offer(REL, ticket=X.peer_ticket(SND, REL2, "recv"))
    case("給別的檔的票證：送方拒絕", "不是給這個檔" in (wait_for(lambda: A._peer_get(off, f)) or ""))
    off, f = offer(REL, ticket=X.peer_ticket({"token": "猜的權杖"}, REL, "recv"))
    case("不是用送方權杖簽的票證：送方拒絕", "票證不對" in (wait_for(lambda: A._peer_get(off, f)) or ""))

    def raw(rel):
        ctx = ssl.create_default_context(cadata=info["cert"])
        req = urllib.request.Request(f"https://127.0.0.1:{PORT}/peer/file/{urllib.parse.quote(rel)}",
                                     headers={"X-Peer-Ticket": X.peer_ticket(SND, rel, "recv")})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx)).open(req, timeout=10) as r:
                return r.status, r.read(200)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(200)

    for bad in ("../../.xconsole/token-secret.txt", "../../.xconsole/token", "/etc/passwd", "a/../../x.gguf"):
        st, body = raw(bad)
        case(f"  路徑跳出存放區（{bad}）：不給", st in (403, 404) and "密文".encode() not in body, (st, body))
    st, _ = raw(LOOSE)
    case("存放區裡但沒核對過 sha256 的檔：不送", st == 404, st)

    part = A.peer_rel_ok(REL2) + ".part"
    os.makedirs(os.path.dirname(part), exist_ok=True)
    with open(part, "wb") as fh:
        fh.write(content(FILES[REL2][0], "b")[:1_000_000])
    off, f = offer(REL2)
    A._peer_get(off, f)
    case("續傳：已經有的部分接著拿，拿完核對 sha256", A.verified_files().get(REL2, {}).get("sha256") == SHA[REL2]
         and not os.path.exists(part))

    off, f = offer(REL, sha="0" * 64)
    try:
        A._peer_get(off, f)
        bad = False
    except OSError as exc:
        bad = "SHA-256" in str(exc)
    case("收到的檔跟主控台給的 sha256 對不上：丟掉、不記成核對過", bad and not os.path.exists(A.peer_rel_ok(REL) + ".part")
         and REL not in A.verified_files())

    holds = []
    ctx = ssl.create_default_context(cadata=info["cert"])
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    for _ in range(2):
        holds.append(op.open(urllib.request.Request(f"https://127.0.0.1:{PORT}/peer/file/{urllib.parse.quote(BIG)}",
                                                    headers={"X-Peer-Ticket": X.peer_ticket(SND, BIG, "r")}), timeout=10))
    time.sleep(0.5)
    off, f = offer(BIG)
    msg = wait_for(lambda: A._peer_get(off, f))
    case("每台同時最多送 2 份：第 3 個請求請它稍後再試", msg and "上限" in msg, msg)
    for h in holds:
        h.close()

    A._PEER["nets"] = (time.time(), [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("192.168.11.0/24")])
    case("互傳埠只收同網段：同網段放行、外面的擋下",
         A.peer_client_ok("192.168.11.27") and not A.peer_client_ok("8.8.8.8") and not A.peer_client_ok("192.168.12.1"))

    # 主控台的排程
    X.node_token = lambda n: n.get("token") or ""
    X._peer_reach = lambda recv, sender, peer: "10.0.0.2" if sender["id"] != "far" else None
    now = time.time()
    peer_has = {"port": PORT, "cert": "PEM", "listening": True, "addrs": ["10.0.0.2"],
                "specs": {SPEC: [{"rel": REL, "sha256": SHA[REL], "size": 1}]}}
    g = {"id": "g1", "name": "教室", "settings": {"models": [SPEC], "sources": ["group", "huggingface"]}}
    nodes = [{"id": i, "name": i, "group": "教室", "token": f"tok-{i}"} for i in ("a", "b", "c", "d", "far")]

    def state(nid, peer):
        X.ALIGN_STATE[nid] = {"at": now, "state": {"group_id": "g1", "peer": peer}}

    for i in ("a", "b", "c", "d"):
        state(i, {"specs": {}, "listening": True})
    case("排程：群組裡還沒有人有，第一台（種子）自己從網路拿", X.peer_plan(g, nodes[0], nodes) == {})
    case("  其他台等種子拿到再互傳", X.peer_plan(g, nodes[1], nodes) == {SPEC: {"wait": True}})
    state("a", peer_has)
    p = X.peer_plan(g, nodes[1], nodes)
    o = (p.get(SPEC) or {}).get("offers") or [{}]
    tk = o[0].get("files", [{}])[0].get("ticket", "")
    body, sig = tk.rsplit(".", 1) if "." in tk else ("", "")
    case("  種子拿到後：發給下一台票證，用送方的權杖簽", o[0].get("node") == "a" and o[0].get("addr") == "10.0.0.2"
         and hmac.compare_digest(sig, hmac.new(b"tok-a", body.encode(), hashlib.sha256).hexdigest()), p)
    X.peer_plan(g, nodes[2], nodes)
    case("  送方同時送 2 台已經滿了：第 3 台等下一輪，不改走網路", X.peer_plan(g, nodes[3], nodes) == {SPEC: {"wait": True}})
    state("b", peer_has)
    p = X.peer_plan(g, nodes[3], nodes)
    case("  拿到的那台接著送（樹狀）：先排手上最空的那台", [x["node"] for x in (p.get(SPEC) or {}).get("offers", [])][:1] == ["b"], p)
    X.PEER_LOAD.clear()
    for i in ("a", "b"):
        state(i, {"specs": {}, "listening": True})
    state("far", peer_has)
    case("  只有連不到的機器有這顆：照舊從網路（或這台設定的鏡像）拿", X.peer_plan(g, nodes[2], nodes) == {})
    state("far", {**peer_has, "listening": False})
    case("  有這顆的機器沒開互傳埠：不讓其他台空等", X.peer_plan(g, nodes[2], nodes) == {})
    case("  群組沒開同群組互傳：不排", X.peer_plan({**g, "settings": {**g["settings"], "sources": ["huggingface"]}},
                                                   nodes[2], nodes) is None)
finally:
    proc.kill()

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
