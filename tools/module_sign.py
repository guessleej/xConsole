#!/usr/bin/env python3
"""企業版模組的打包與簽章（云碩在離線機器上用；格式的唯一來源，核心的合約測試也用這一支）。

  python3 tools/module_sign.py keygen <私鑰檔>
      產一把Ed25519金鑰。私鑰檔權限600、不加密存在你指定的離線位置；只印出公鑰（hex）。
      把公鑰加進console/modules.py的TRUSTED_KEYS後發新版核心。私鑰不進repo、不進CI、不放環境變數。
  python3 tools/module_sign.py manifest <模組資料夾> --name xconsole-enterprise --version 1.0.0
      寫出manifest.json：每個檔的sha256與core_api。不收__pycache__與連結檔。
  python3 tools/module_sign.py sign <模組資料夾> <私鑰檔>
      簽manifest.json，寫出manifest.sig（base64）。
  python3 tools/module_sign.py pack <模組資料夾> <輸出.zip>
      打成上傳用的zip（主控台「設定 → 企業版模組」上傳）。
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "console"))


def keygen(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if os.path.exists(path):
        raise SystemExit(f"{path} 已經存在，不覆蓋")
    k = Ed25519PrivateKey.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def build_manifest(d, name, version, core_api=None):
    import modules
    files = {}
    for dp, dn, fs in os.walk(d):
        dn[:] = sorted(x for x in dn if x != "__pycache__")
        for f in sorted(fs):
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, d).replace(os.sep, "/")
            if rel in modules.RESERVED:
                continue
            if os.path.islink(full):
                raise SystemExit(f"{rel} 是連結，不收")
            files[rel] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    if modules.ENTRY not in files:
        raise SystemExit(f"模組資料夾要有{modules.ENTRY}（register(api)）")
    man = {"name": name, "version": version, "core_api": core_api or [modules.CORE_API], "files": files}
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1, sort_keys=True)
    return man


def sign(d, key_path=None, key=None):
    from cryptography.hazmat.primitives import serialization
    if key is None:
        key = serialization.load_pem_private_key(open(key_path, "rb").read(), password=None)
    raw = open(os.path.join(d, "manifest.json"), "rb").read()
    with open(os.path.join(d, "manifest.sig"), "wb") as fh:
        fh.write(base64.b64encode(key.sign(raw)))


def pack(d, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dn, fs in os.walk(d):
            dn[:] = sorted(x for x in dn if x != "__pycache__")
            for f in sorted(fs):
                full = os.path.join(dp, f)
                z.write(full, os.path.relpath(full, d).replace(os.sep, "/"))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("key")
    m = sub.add_parser("manifest")
    m.add_argument("dir")
    m.add_argument("--name", required=True)
    m.add_argument("--version", required=True)
    s = sub.add_parser("sign")
    s.add_argument("dir")
    s.add_argument("key")
    p = sub.add_parser("pack")
    p.add_argument("dir")
    p.add_argument("out")
    a = ap.parse_args()
    if a.cmd == "keygen":
        print("公鑰（加進console/modules.py的TRUSTED_KEYS）：", keygen(a.key))
    elif a.cmd == "manifest":
        man = build_manifest(a.dir, a.name, a.version)
        print(f"manifest.json：{len(man['files'])} 個檔")
    elif a.cmd == "sign":
        sign(a.dir, a.key)
        print("manifest.sig已寫好")
    else:
        pack(a.dir, a.out)
        print("已打包：", a.out)


if __name__ == "__main__":
    main()
