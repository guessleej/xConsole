#!/usr/bin/env python3
"""準備桌面版要打包的內容（desktop/build/payload/）：
- python/：python-build-standalone 的獨立 Python（不靠這台有沒有裝 Python），核對 GitHub 公布的 sha256 才用，
  再把 requirements.txt 的相依（版本釘死）裝進去
- app/：主控台、畫面、節點代理、登記代理用的 setup_local.py、VERSION、requirements.txt（不含測試）

跑法：python3 desktop/scripts/prepare_payload.py [--target x86_64-pc-windows-msvc]
目標沒給就照這台（建置機）。GH_TOKEN 有的話帶上（CI 共用機器不帶權杖很容易被 GitHub 限流）。
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "desktop", "build", "payload")
PY_MINOR = "3.12"
REPO = "astral-sh/python-build-standalone"


def host_target():
    m = platform.machine().lower()
    arch = "aarch64" if m in ("arm64", "aarch64") else "x86_64"
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if os.name == "nt":
        return f"{arch}-pc-windows-msvc"
    return f"{arch}-unknown-linux-gnu"


def gh(url):
    h = {"User-Agent": "xconsole-build", "Accept": "application/vnd.github+json"}
    if os.environ.get("GH_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
        return json.load(r)


def find_python(target):
    """最新一版裡 3.12 的 install_only_stripped 包：回 (網址, sha256, 檔名)。"""
    rx = re.compile(rf"cpython-{re.escape(PY_MINOR)}\.\d+\+\d+-{re.escape(target)}-install_only_stripped\.tar\.gz")
    for rel in gh(f"https://api.github.com/repos/{REPO}/releases?per_page=5"):
        for a in rel.get("assets") or []:
            if rx.fullmatch(a["name"]):
                dg = str(a.get("digest") or "")
                if not dg.startswith("sha256:"):
                    raise SystemExit(f"{a['name']} 沒有 GitHub 公布的 sha256，不用")
                return a["browser_download_url"], dg.split(":", 1)[1], a["name"]
    raise SystemExit(f"找不到 {target} 的 Python {PY_MINOR} 獨立版")


def download(url, sha, dest):
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "xconsole-build"}), timeout=600) as r, \
            open(dest, "wb") as fh:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            h.update(b)
            fh.write(b)
    if h.hexdigest() != sha:
        raise SystemExit(f"Python 包的 sha256 對不上（{h.hexdigest()} ≠ {sha}），不用")


def copy_app(dst):
    os.makedirs(os.path.join(dst, "console", "static"), exist_ok=True)
    os.makedirs(os.path.join(dst, "agent"), exist_ok=True)
    os.makedirs(os.path.join(dst, "install"), exist_ok=True)
    for f in os.listdir(os.path.join(ROOT, "console")):
        p = os.path.join(ROOT, "console", f)
        if os.path.isfile(p) and re.fullmatch(r"[\w.-]+\.(py|json)", f) and not f.startswith("test_"):
            shutil.copy2(p, os.path.join(dst, "console", f))
    for f in os.listdir(os.path.join(ROOT, "console", "static")):
        p = os.path.join(ROOT, "console", "static", f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dst, "console", "static", f))
    shutil.copy2(os.path.join(ROOT, "agent", "xconsole_agent.py"), os.path.join(dst, "agent", "xconsole_agent.py"))
    # 主控台的線上更新會把代理推給各機，從 console/agent/ 拿
    os.makedirs(os.path.join(dst, "console", "agent"), exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "agent", "xconsole_agent.py"), os.path.join(dst, "console", "agent", "xconsole_agent.py"))
    shutil.copy2(os.path.join(ROOT, "install", "setup_local.py"), os.path.join(dst, "install", "setup_local.py"))
    for f in ("VERSION", "requirements.txt"):
        shutil.copy2(os.path.join(ROOT, f), os.path.join(dst, f))
        shutil.copy2(os.path.join(ROOT, f), os.path.join(dst, "console", f))


def slim(py_root):
    """拿掉執行用不到的：測試、IDLE、tkinter、ensurepip 的安裝包，省幾十 MB。"""
    lib = os.path.join(py_root, "Lib") if os.name == "nt" or os.path.isdir(os.path.join(py_root, "Lib")) else None
    if not lib:
        cands = [os.path.join(py_root, "lib", d) for d in os.listdir(os.path.join(py_root, "lib")) if d.startswith("python3")]
        lib = cands[0] if cands else None
    if not lib:
        return
    for d in ("test", "idlelib", "tkinter", "turtledemo", "lib2to3", "ensurepip/_bundled"):
        shutil.rmtree(os.path.join(lib, d), ignore_errors=True)
    for dp, dn, _ in os.walk(lib):
        for d in list(dn):
            if d in ("__pycache__", "tests") and "site-packages" not in dp:
                shutil.rmtree(os.path.join(dp, d), ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=host_target())
    a = ap.parse_args()
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    url, sha, name = find_python(a.target)
    tgz = os.path.join(os.path.dirname(OUT), name)
    if not (os.path.exists(tgz) and hashlib.sha256(open(tgz, "rb").read()).hexdigest() == sha):
        print("下載", name, flush=True)
        download(url, sha, tgz)
    with tarfile.open(tgz) as tf:
        tf.extractall(OUT, filter="data")
    py_root = os.path.join(OUT, "python")
    exe = os.path.join(py_root, "python.exe") if "windows" in a.target else os.path.join(py_root, "bin", "python3")
    if a.target == host_target():
        subprocess.run([exe, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "-q",
                        "-r", os.path.join(ROOT, "requirements.txt")], check=True)
        subprocess.run([exe, "-c", "import cryptography, pypdf; print('相依 OK', cryptography.__version__)"], check=True)
    else:
        raise SystemExit("跨平台打包要在目標平台上跑（相依有編譯過的套件）；CI 每個平台各跑一次")
    slim(py_root)
    copy_app(os.path.join(OUT, "app"))
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(OUT) for f in fs)
    print(f"payload 準備好了：{OUT}（{size / 1e6:.0f} MB，{a.target}）")


if __name__ == "__main__":
    main()
