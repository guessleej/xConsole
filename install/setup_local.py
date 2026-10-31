#!/usr/bin/env python3
"""單機安裝的最後一步：把同一台的節點代理登記進主控台（fleet.json＋信任憑證）。
install.sh（Linux／macOS）與 install.ps1（Windows）共用，三個系統同一套寫法。

    python setup_local.py --home <代理與主控台共用的家目錄> [--name 名稱] [--group 群組]

已經登記過就只更新權杖與憑證（代理重裝之後兩樣都會換），名稱與群組不動。
權杖以明文寫進 fleet.json（0600），主控台下次啟動時會轉成 AES-256-GCM 密文（migrate_tokens）。
"""
import argparse
import json
import os
import shutil
import socket


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--name", default=socket.gethostname())
    ap.add_argument("--group", default="本機")
    a = ap.parse_args()
    st = os.path.join(a.home, ".xconsole")
    token = open(os.path.join(st, "token"), encoding="utf-8").read().strip()
    cert = os.path.join(st, "agent-cert.pem")
    fleet_p = os.path.join(st, "fleet.json")
    cfg = json.load(open(fleet_p, encoding="utf-8")) if os.path.exists(fleet_p) else {}
    cfg.setdefault("auth", {})
    nodes = cfg.setdefault("nodes", [])
    me = next((n for n in nodes if n.get("host") in ("127.0.0.1", "localhost") and n.get("agent_port", 9401) == 9401), None)
    if me is None:
        me = {"id": "local", "name": a.name, "group": a.group, "host": "127.0.0.1", "agent_port": 9401}
        if any(n["id"] == "local" for n in nodes):
            me["id"] = "local-" + socket.gethostname().lower()[:20]
        nodes.append(me)
    me["token"] = token
    os.makedirs(os.path.join(st, "trust"), mode=0o700, exist_ok=True)
    dst = os.path.join(st, "trust", me["id"] + ".pem")
    shutil.copyfile(cert, dst)
    os.chmod(dst, 0o600)
    tmp = fleet_p + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, fleet_p)
    print(f"已把這台的節點代理登記進主控台：{me['name']}（{me['id']}）")


if __name__ == "__main__":
    main()
