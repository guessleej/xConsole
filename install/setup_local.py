#!/usr/bin/env python3
"""單機安裝的最後一步：把同一台的節點代理登記進主控台（fleet.json＋信任憑證）。
install.sh（Linux／macOS）與 install.ps1（Windows）共用，三個系統同一套寫法。

    python setup_local.py --home <代理與主控台共用的家目錄> [--name 名稱] [--group 群組]
    python setup_local.py --home <桌面版的資料夾> --managed [--name 名稱]

已經登記過就只更新權杖與憑證（代理重裝之後兩樣都會換），名稱與群組不動。
--managed（桌面版每次啟動先跑）：這台有學校或公司統一裝的系統代理在聽本機使用者通道，就把這台登記成經通道連、
回 0，桌面版不再帶起自己的代理；沒有就回 3，什麼都不改。
權杖以明文寫進 fleet.json（0600），主控台下次啟動時會轉成 AES-256-GCM 密文（migrate_tokens）。
"""
import argparse
import json
import os
import shutil
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "console"))
import userchan  # noqa: E402


def save(fleet_p, cfg):
    os.makedirs(os.path.dirname(fleet_p), mode=0o700, exist_ok=True)
    tmp = fleet_p + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, fleet_p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--name", default=socket.gethostname())
    ap.add_argument("--group", default="本機")
    ap.add_argument("--agent-port", type=int, default=9401)   # 桌面版每次開的埠可能不同（9401 被占就換一個）
    ap.add_argument("--managed", action="store_true")
    a = ap.parse_args()
    st = os.path.join(a.home, ".xconsole")
    fleet_p = os.path.join(st, "fleet.json")
    cfg = json.load(open(fleet_p, encoding="utf-8")) if os.path.exists(fleet_p) else {}
    cfg.setdefault("auth", {})
    nodes = cfg.setdefault("nodes", [])
    if a.managed:
        if not userchan.managed():
            sys.exit(3)
        me = next((n for n in nodes if n.get("id") == "local"), None)
        if me is None:
            me = {"id": "local", "name": a.name, "group": "本機"}
            nodes.append(me)
        for k in ("token", "agent_port"):
            me.pop(k, None)
        me.update(host="127.0.0.1", link="user")
        trust = os.path.join(st, "trust", "local.pem")
        if os.path.exists(trust):
            os.remove(trust)
        save(fleet_p, cfg)
        print(f"這台由學校或公司統一管理：桌面版改經本機通道連系統代理（{userchan.DEFAULT}）")
        return
    token = open(os.path.join(st, "token"), encoding="utf-8").read().strip()
    cert = os.path.join(st, "agent-cert.pem")
    me = next((n for n in nodes if n.get("host") in ("127.0.0.1", "localhost") and n.get("agent_port", 9401) == a.agent_port), None) \
        or next((n for n in nodes if n.get("id") == "local" and n.get("host") in ("127.0.0.1", "localhost")), None)
    if me is None:
        me = {"id": "local", "name": a.name, "group": a.group, "host": "127.0.0.1", "agent_port": a.agent_port}
        if any(n["id"] == "local" for n in nodes):
            me["id"] = "local-" + socket.gethostname().lower()[:20]
        nodes.append(me)
    me["token"] = token
    me["agent_port"] = a.agent_port
    for k in ("link", "channel", "channel_owners"):       # 以前是受管的機器、系統代理移除了：改回自己的代理
        me.pop(k, None)
    os.makedirs(os.path.join(st, "trust"), mode=0o700, exist_ok=True)
    dst = os.path.join(st, "trust", me["id"] + ".pem")
    shutil.copyfile(cert, dst)
    os.chmod(dst, 0o600)
    save(fleet_p, cfg)
    print(f"已把這台的節點代理登記進主控台：{me['name']}（{me['id']}）")


if __name__ == "__main__":
    main()
