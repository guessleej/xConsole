#!/usr/bin/env python3
"""推理引擎一次編譯分發（清單 #15）。不連外、不碰正式環境、不真的編譯。
一、主控台收包：sha256 對得上才收、同平台的舊包換掉、名稱檢查、刪除。
二、排程：同機型只讓一台編、其他等；主控台有包就發給同機型的；不同機型各自排；官方預編譯版與已經有引擎的不排。
三、代理（Linux／macOS）：打包編好的那一層、跟主控台的內部鏡像拿包、經手動匯入那一套核對並裝上；sha256 不對不裝；
    主控台叫它等就等（不算失敗）。
跑法：python3 test_engine_dist.py
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-engdist-")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent"))
import mirror  # noqa: E402

OK = True
WIN = os.name == "nt"


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


def blob(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return hashlib.sha256(data).hexdigest()


# ---- 一、主控台收包
M = mirror.Mirror(tempfile.mkdtemp(prefix="xc-engmirror-"))
src = os.path.join(M.root, "in.part")
sha = blob(src, b"pack-one")
meta = {"name": "b6800-linux-ubuntu24.04-aarch64-cuda13-sm121.tar.gz", "platform": "linux-ubuntu24.04-aarch64-cuda13-sm121",
        "tag": "b6800", "sha256": "0" * 64, "size": 8, "from": "dgx-1"}
try:
    M.engine_put(meta, src)
    bad = False
except OSError:
    bad = True
check("包的 sha256 跟那台回報的不同：不收", bad and not M.engines())
sha = blob(src, b"pack-one")
M.engine_put({**meta, "sha256": sha}, src)
check("  對得上才收，記下平台、版本、從哪台來", M.engine_for(meta["platform"])["from"] == "dgx-1"
      and M.engine_path(meta["name"]) is not None)
sha2 = blob(src, b"pack-two")
M.engine_put({**meta, "name": "b6900-linux-ubuntu24.04-aarch64-cuda13-sm121.tar.gz", "tag": "b6900", "sha256": sha2}, src)
check("  同平台的新包換掉舊包", list(M.engines()) == ["b6900-linux-ubuntu24.04-aarch64-cuda13-sm121.tar.gz"]
      and M.engine_path(meta["name"]) is None)
check("  名稱跳不出資料夾", M.engine_path("../index.json") is None and M.engine_path("x/../../a.tar.gz") is None)
M.engine_delete("b6900-linux-ubuntu24.04-aarch64-cuda13-sm121.tar.gz")
check("  刪掉", M.engines() == {})

# ---- 二、排程
import xconsole_server as X  # noqa: E402

X.MIRROR = mirror.Mirror(tempfile.mkdtemp(prefix="xc-engmirror2-"))
X.mirror_urls = lambda: {"urls": ["https://console:9400/mirror"], "cert": "PEM"}
P = "linux-ubuntu24.04-x86_64-cuda13-sm89"
now = time.time()
nodes = [{"id": i, "name": i, "group": "教室"} for i in ("a", "b", "c", "mac")]
g = {"id": "g1", "name": "教室"}


def eng(nid, **kw):
    X.ALIGN_STATE[nid] = {"at": now, "state": {"group_id": "g1", "engine": {"platform": P, "have": False, "official": False,
                                                                           "building": False, "pack": None, **kw}}}


for i in ("a", "b", "c"):
    eng(i)
eng("mac", platform="darwin-arm64-cpu", official=True)
check("同機型都還沒有引擎：只讓第一台編", X.engine_plan(g, nodes[0], nodes) == {"build": True})
check("  其他台等它", X.engine_plan(g, nodes[1], nodes) == {"wait": True} and X.engine_plan(g, nodes[2], nodes) == {"wait": True})
check("  macOS／Windows 用官方預編譯版，不排", X.engine_plan(g, nodes[3], nodes) == {})
eng("a", building=True)
check("  那台正在編：其他台繼續等", X.engine_plan(g, nodes[1], nodes) == {"wait": True})
eng("a", have=True, pack={"name": "b1-x.tar.gz", "platform": P, "sha256": "1" * 64})
check("  編好了、主控台還在收：等", X.engine_plan(g, nodes[1], nodes) == {"wait": True})
src = os.path.join(X.MIRROR.root, "p.part")
psha = blob(src, b"real-pack")
X.MIRROR.engine_put({"name": "b1-x.tar.gz", "platform": P, "tag": "b1", "sha256": psha, "size": 9, "from": "a"}, src)
p = X.engine_plan(g, nodes[1], nodes)
check("  主控台收好了：同機型的直接拿（附主控台網址與憑證）", (p.get("offer") or {}).get("sha256") == psha
      and p["offer"].get("urls") and p["offer"].get("cert"), p)
check("  已經有引擎的不排", X.engine_plan(g, nodes[0], nodes) == {})
eng("c", platform="linux-ubuntu24.04-aarch64-cuda13-sm121")
check("不同機型各自排：那種機型的第一台編", X.engine_plan(g, nodes[2], nodes) == {"build": True})
X.ALIGN_STATE["b"]["state"]["error"] = "前一次編譯失敗"
eng("a")
X.ALIGN_STATE["a"]["state"]["error"] = "缺編譯工具"
X.MIRROR.engine_delete("b1-x.tar.gz")
check("  第一台出錯了：換下一台編，不讓整組卡住", X.engine_plan(g, nodes[1], nodes) == {"build": True}
      or X.engine_plan(g, nodes[2], nodes) == {"build": True})

# ---- 三、代理
import xconsole_agent as A  # noqa: E402

check("代理的平台代號：作業系統、架構、GPU 後端", re.fullmatch(r"(linux|darwin|windows)[\w.+-]*-(cuda\d*-sm[\d_]+|rocm-[\w_]+|cpu)",
                                                         A.engine_platform()), A.engine_platform())
A.GROUP_ENGINE.update(plan={"wait": True})
real_os, A.OS = A.OS, "linux"                  # 「等同機型的那台」只有 Linux 會走到；macOS／Windows 裝官方版
try:
    A.group_engine({"steps": []})
    waited = False
except A.PeerWait:
    waited = True
A.OS = real_os
check("  Linux 上主控台叫它等：等（不算失敗）", waited)

if WIN:
    print("Windows：打包與從主控台安裝的實測略過（測試用的假引擎是 shell 腳本）")
else:
    bindir = os.path.join(A.HOME, "llama.cpp-b9999", "build", "bin")
    os.makedirs(bindir)
    exe = os.path.join(bindir, "llama-server")
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho 'version: 9999 (testbuild)'\n")
    os.chmod(exe, 0o755)
    blob(os.path.join(bindir, "libggml-base.so"), b"fake lib")
    pk = A.engine_pack_make(exe, "b9999")
    with tarfile.open(pk["path"]) as tf:
        names = sorted(tf.getnames())
    check("打包：編好的那一層（執行檔與函式庫），記下 sha256 與平台", names == ["llama-bin/libggml-base.so", "llama-bin/llama-server"]
          and pk["sha256"] == A._sha256_file(pk["path"]) and pk["platform"] == A.engine_platform(), names)
    check("  回給主控台的資訊帶著包", (A.engine_info().get("pack") or {}).get("name") == pk["name"])

    W = tempfile.mkdtemp(prefix="xc-engdist-console-")
    chome = os.path.join(W, "console")
    os.makedirs(os.path.join(chome, ".xconsole"), mode=0o700)
    CM = mirror.Mirror(os.path.join(chome, ".xconsole", "mirror"))
    tmp = os.path.join(W, "pack.part")
    shutil.copy(pk["path"], tmp)
    CM.engine_put({**pk, "from": "builder"}, tmp)

    def free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p
    port, lport, TOK = free_port(), free_port(), secrets.token_urlsafe(32)
    with open(os.path.join(chome, ".xconsole", "fleet.json"), "w", encoding="utf-8") as fh:
        json.dump({"auth": {}, "nodes": [{"id": "ws-1", "name": "教室 01", "host": "127.0.0.1", "agent_port": free_port(),
                                          "group": "教室", "link": "pull", "token": TOK}]}, fh)
    env = {**os.environ, "HOME": chome, "XCONSOLE_HOME": chome, "XCONSOLE_PORT": str(port), "XCONSOLE_LINK_PORT": str(lport)}
    srv = os.path.join(HERE, "xconsole_server.py")
    subprocess.run([sys.executable, srv, "--set-credentials"], input=b"admin\nengine-pass-2026\n", env=env, check=True, capture_output=True)
    cons = subprocess.Popen([sys.executable, srv], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        with open(os.path.join(chome, ".xconsole", "tls", "cert.pem"), encoding="utf-8") as fh:
            cert = fh.read()
        shutil.rmtree(os.path.join(A.HOME, "llama.cpp-b9999"))
        os.remove(A.ENGINE_PACK)
        A.TOKEN = TOK
        offer = {"name": pk["name"], "sha256": pk["sha256"], "urls": [f"https://127.0.0.1:{port}/mirror"], "cert": cert}
        try:
            A._engine_fetch({**offer, "sha256": "0" * 64})
            bad = False
        except RuntimeError as exc:
            bad = "sha256" in str(exc).lower()
        check("跟主控台拿的包 sha256 跟主控台說的不同：不裝", bad and not A.llama_globs())
        A._engine_fetch(offer)
        got = A.llama_globs()
        check("  對得上：經手動匯入那一套核對、試跑過才裝上", got and "import-" in got[0] and A.llama_server_exe() == got[0], got)
        A.GROUP_ENGINE.update(plan={})
        A.group_engine({"steps": []})
        check("  已經有引擎：群組準備時直接往下走", True)
    finally:
        cons.terminate()

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
