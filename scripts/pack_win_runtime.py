#!/usr/bin/env python3
"""Windows執行環境包：讓伺服器版安裝與「加一台機器」在Windows上不必先裝Python、不必連pypi.org。

內容來自桌面版已經準備好（有憑證時也已簽章）的desktop/build/payload/：
- python/：python-build-standalone＋requirements.txt的相依（版本釘死）
- runtime/：Visual C++執行階段（推理引擎要的）

產出兩個檔：
- xconsole-runtime-<版本>-win-x64.zip：只有python/ 與runtime/。主控台下載這個（核對GitHub公布的sha256），
  「加一台機器」的Windows腳本從主控台拿
- xconsole-<版本>-windows-x64.zip：整個xConsole（同git archive）再加winrt\\python與winrt\\runtime。
  在Windows主控台機器解開後執行install\\install.ps1，就用內附的這一份

跑法（CI的Windows工作，在prepare_payload.py與簽章之後）：python scripts/pack_win_runtime.py dist
"""
import io
import os
import subprocess
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(ROOT, "desktop", "build", "payload")
MUST = ("python/python.exe", "python/pythonw.exe", "runtime/msvcp140.dll", "runtime/vcruntime140.dll", "runtime/vcomp140.dll")


def runtime_files():
    """(壓縮檔內路徑, 本機路徑)，只收python/ 與runtime/。排序固定，同樣內容產出同樣的檔。"""
    out = []
    for top in ("python", "runtime"):
        base = os.path.join(PAYLOAD, top)
        for dp, dn, fs in os.walk(base):
            dn[:] = sorted(d for d in dn if d != "__pycache__")
            for f in sorted(fs):
                full = os.path.join(dp, f)
                out.append((os.path.relpath(full, PAYLOAD).replace(os.sep, "/"), full))
    have = {a for a, _ in out}
    miss = [m for m in MUST if m not in have]
    if miss:
        raise SystemExit(f"桌面版的payload少了 {', '.join(miss)}：先跑desktop/scripts/prepare_payload.py（要在Windows上）")
    return out


def add(z, arc, full):
    info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with open(full, "rb") as fh:
        z.writestr(info, fh.read())


def source_tree(version):
    """git archive的內容（跟正式版的tar.gz同一份）：(壓縮檔內路徑, 位元組)。"""
    raw = subprocess.run(["git", "-C", ROOT, "archive", "--format=tar", f"--prefix=xconsole-{version}/", "HEAD"],
                         check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        for m in tf.getmembers():
            if m.isfile():
                yield m.name, tf.extractfile(m).read()


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "dist"
    os.makedirs(out_dir, exist_ok=True)
    version = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
    files = runtime_files()
    rt = os.path.join(out_dir, f"xconsole-runtime-{version}-win-x64.zip")
    with zipfile.ZipFile(rt, "w") as z:
        for arc, full in files:
            add(z, arc, full)
    full_pkg = os.path.join(out_dir, f"xconsole-{version}-windows-x64.zip")
    with zipfile.ZipFile(full_pkg, "w") as z:
        for arc, data in source_tree(version):
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
        for arc, full in files:
            add(z, f"xconsole-{version}/winrt/{arc}", full)
    for p in (rt, full_pkg):
        print(f"{os.path.basename(p)}：{os.path.getsize(p) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
