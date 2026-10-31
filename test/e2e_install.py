#!/usr/bin/env python3
"""安裝完之後的端對端驗收（CI 在 Linux／macOS／Windows 各跑一次，也可以在客戶機器上跑）：
經過網頁 API 走一遍管理者的流程——登入 → 看到這台 → 推薦清單 → 一鍵裝推理引擎 → 下載小模型 → 建欄位並換上 →
真的問它一句話 → 停掉。全部走主控台，不直接碰代理。

    python test/e2e_install.py --home <安裝時的家目錄> --password-file <帳密檔> [--skip-engine]

連主控台時用它自己的憑證驗證（不關驗證）。小模型 SmolLM2-135M（約 100 MB），跑完留在存放區。
"""
import argparse
import http.cookiejar
import json
import os
import ssl
import sys
import time
import urllib.request

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}{'（' + str(detail)[:200] + '）' if detail != '' else ''}", flush=True)
    return good


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--password-file", required=True)
    ap.add_argument("--skip-engine", action="store_true", help="Linux 是在這台編譯 llama.cpp，CI 上太久就略過")
    a = ap.parse_args()
    user, pw = open(a.password_file, encoding="utf-8").read().splitlines()[:2]
    ctx = ssl.create_default_context(cafile=os.path.join(a.home, ".xconsole", "tls", "cert.pem"))
    ctx.check_hostname = False          # 憑證本身照樣驗：只接受主控台那一張，主機名稱交給下一行比對
    jar = http.cookiejar.CookieJar()
    # 連本機不走代理（macOS 的 urllib 會讀系統代理設定）
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx),
                                     urllib.request.HTTPCookieProcessor(jar))
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, body=None, timeout=60):
        req = urllib.request.Request("https://127.0.0.1:9400" + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with op.open(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def poll(fn, secs, every=3):
        end = time.time() + secs
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(every)
        return None

    c, r = call("/api/login", {"username": user, "password": pw})
    case("登入", c == 200, "" if c == 200 else (c, r))
    fleet = poll(lambda: (lambda c, f: f if c == 200 and f["nodes"] and f["nodes"][0]["online"] else None)(*call("/api/fleet")), 60)
    if not case("本機的節點代理已納管而且連得上", fleet, fleet and [n["id"] for n in fleet["nodes"]]):
        return 1
    n = fleet["nodes"][0]
    nid, st = n["id"], n["state"]
    case("讀得到記憶體架構與晶片", st["pools"]["memory_model"] and st.get("chip"), (st["pools"]["memory_model"], (st.get("chip") or {}).get("gpu")))
    case("算得出這台能跑多大", n.get("capacity") is not None, (n.get("capacity") or {}).get("max_dense_b"))
    sug = st["chip"]["suggest"]
    # CI 主機記憶體很小（macOS 約 7 GB），預設底線（統一記憶體 12 GiB）會擋下任何模型；
    # 小機器的管理者也是在網頁「這台的設定」把底線調低，這裡照做
    # CI 的 Mac 實測只剩約 3 GiB 可用，底線 1 GiB 都還會擋下 0.1 GiB 的小模型（載完剩 0.4）：CI 上直接設 0
    small = {"ram_floor_gib": 0, "vram_floor_gib": 0} if os.environ.get("CI") else {}
    case("照偵測建議確認記憶體架構", call(f"/api/node/{nid}/machine", {"memory_model": sug, **small})[0] == 200, sug)
    c, rec = call(f"/api/node/{nid}/recommend")
    case("推薦清單", c == 200 and rec["items"], c == 200 and len(rec["items"]))

    if not a.skip_engine:
        c, r = call(f"/api/node/{nid}/llama/build", {})
        case("一鍵安裝推理引擎", c == 200, r)
        done = poll(lambda: (lambda c, p: p if c == 200 and not p["running"] else None)(*call(f"/api/node/{nid}/llama/build/progress")), 900, 5)
        j = (done or {}).get("job") or {}
        case("推理引擎裝好、跑得起來", j.get("state") == "done", j.get("exe") or j.get("error") or (done or {}).get("log_tail", [])[-3:])

    c, r = call(f"/api/node/{nid}/library/download", {"repo": "unsloth/SmolLM2-135M-Instruct-GGUF", "stem": "SmolLM2-135M-Instruct-Q4_K_M"})
    case("下載小模型", c == 200, r)
    lib = poll(lambda: (lambda c, l: l if c == 200 and l["gguf"] and not any(x["state"] in ("running", "queued") for x in l["downloads"]) else None)(
        *call(f"/api/node/{nid}/library", timeout=90)), 300)
    case("存放區掃得到模型", lib and lib["gguf"], lib and [m["id"] for m in lib["gguf"]])
    if a.skip_engine or not lib or not lib["gguf"]:
        print("\n全部通過" if OK else "\n有項目失敗")
        return 0 if OK else 1

    c, r = call(f"/api/node/{nid}/slots/add", {"type": "empty", "role": "對話", "tier": "驗收"})
    case("新增欄位", c == 200, r)
    sid = r["slot"]["id"]
    c, r = call(f"/api/node/{nid}/slot/{sid}/model", {"engine": "llama", "model": lib["gguf"][0]["id"], "ctx": 4096}, timeout=1200)
    case("換上模型（llama.cpp 起來並通過健康檢查）", c == 200, r)
    if c == 200:
        req = urllib.request.Request(f"http://127.0.0.1:{r['port']}/v1/chat/completions", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"messages": [{"role": "user", "content": "Say hi."}], "max_tokens": 12}).encode())
        try:
            with local.open(req, timeout=120) as resp:
                text = json.load(resp)["choices"][0]["message"]["content"]
            case("模型真的回答了", bool(text.strip()), text)
        except Exception as ex:  # noqa: BLE001
            case("模型真的回答了", False, ex)
        c, f = call("/api/fleet")
        slot = next(s for s in f["nodes"][0]["state"]["slots"] if s["id"] == sid)
        case("欄位顯示使用中", slot["active"], slot.get("state"))
        case("停掉欄位", call(f"/api/node/{nid}/slot/{sid}/stop", {}, timeout=120)[0] == 200)
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
