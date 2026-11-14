#!/usr/bin/env python3
"""加密入口（主控台 9400 的 /v1）端到端：假引擎 → 代理 → 主控台，全程 HTTPS 並驗證憑證。
- 金鑰：沒帶、帶錯、只帶網頁 cookie、放網址參數都擋；Bearer 與 x-api-key 兩種都收
- 撤銷其中一把，其他照常可用（問題清單 FEAT-2 的驗收條件）；撤銷後下一個請求就 401，不必重啟
- 權限：金鑰只授權別格時看不到、用不了這個模型；/v1/models 只列這把能用的
- 同一把金鑰同時進行中的請求超過上限回 429
- 用量：非串流、串流、Anthropic 格式的 token 都記到對的金鑰；入口自己加的用量段落不轉給客戶端
- 路徑白名單、方法、Content-Type、找不到模型都回 OpenAI 格式的錯誤
- 同一來源送錯金鑰 30 次之後回 429；稽核只記一筆；資料庫找不到完整金鑰
不需要 systemd。跑法：python3 test/test_gateway.py
"""
import glob
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = tempfile.mkdtemp(prefix="xc-gw-")
OK = True
PW = "gateway-test-pass-2026"
ENGINE_KEY = "xc-engine-internal-key-gateway-01"


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail and not cond else ""))
    OK = OK and bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_tls(port):
    for _ in range(120):
        try:
            ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def https(port, cafile, method, path, body=None, headers=None, raw=False, timeout=60):
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.check_hostname = False
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=timeout)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    sc = r.getheader("Set-Cookie") or ""
    hdrs = dict(r.getheaders())
    c.close()
    if raw:
        return r.status, data, hdrs
    try:
        return r.status, json.loads(data or b"{}"), sc
    except ValueError:
        return r.status, {}, sc


def main():
    lp, ap, cp = free_port(), free_port(), free_port()
    cfg = os.path.join(W, "engine.json")
    json.dump({"port": lp, "model_name": "mock-a", "api_key": ENGINE_KEY, "args": ["--max-context", "8192"]}, open(cfg, "w"))
    procs = [subprocess.Popen([sys.executable, os.path.join(ROOT, "test", "fixtures", "mock_llm.py"), cfg],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    ahome = os.path.join(W, "agent-home")
    os.makedirs(os.path.join(ahome, ".xconsole"))
    json.dump({"machine": {"name": "gw-test", "ip": "127.0.0.1", "memory_model": "discrete"},
               "slots": [{"id": "brain", "role": "主腦", "unit": "xc-gwtest-fake.service", "kind": "strata", "config": cfg}]},
              open(os.path.join(ahome, ".xconsole", "registry.json"), "w"))
    procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py")],
                                  env={**os.environ, "HOME": ahome, "XCONSOLE_HOME": ahome, "XCONSOLE_AGENT_PORT": str(ap)},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        check("代理啟動", wait_tls(ap))
        token = open(os.path.join(ahome, ".xconsole", "token")).read().strip()
        chome = os.path.join(W, "console-home")
        os.makedirs(os.path.join(chome, ".xconsole", "trust"), mode=0o700)
        cenv = {**os.environ, "HOME": chome, "XCONSOLE_PORT": str(cp)}
        subprocess.run([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py"), "--set-credentials"],
                       input=f"admin\n{PW}\n".encode(), env=cenv, check=True, capture_output=True)
        fp = os.path.join(chome, ".xconsole", "fleet.json")
        c = json.load(open(fp))
        c["nodes"] = [{"id": "t1", "name": "t1", "host": "127.0.0.1", "group": "測試", "agent_port": ap, "token": token}]
        json.dump(c, open(fp, "w"))
        shutil.copy(os.path.join(ahome, ".xconsole", "agent-cert.pem"), os.path.join(chome, ".xconsole", "trust", "t1.pem"))
        procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py")], env=cenv,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("主控台啟動", wait_tls(cp))
        ca = os.path.join(chome, ".xconsole", "tls", "cert.pem")
        _, _, sc = https(cp, ca, "POST", "/api/login", {"username": "admin", "password": PW})
        ck = {"Cookie": "xconsole=" + sc.split("xconsole=", 1)[1].split(";", 1)[0]}

        def admin(method, path, body=None):
            st, d, _ = https(cp, ca, method, path, body, ck)
            return st, d

        _, a = admin("POST", "/api/keys/create", {"name": "王老師", "slots": ["t1/brain"]})
        _, b = admin("POST", "/api/keys/create", {"name": "機器學習（週三）"})
        _, other = admin("POST", "/api/keys/create", {"name": "只能用別格", "slots": ["t1/coder"]})
        _, one = admin("POST", "/api/keys/create", {"name": "同時一個", "max_inflight": 1})
        ka, kb, ko, k1 = a["key"], b["key"], other["key"], one["key"]
        bearer = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731

        def v1(method, sub, body=None, headers=None, raw=False):
            st, d, h = https(cp, ca, method, "/v1/" + sub, body, headers, raw=raw)
            return st, d, h

        chat = {"model": "mock-a", "messages": [{"role": "user", "content": "hi"}]}
        st, d, _ = v1("GET", "models", headers=bearer(ka))
        check("/v1/models：列出這把能用的模型", st == 200 and [m["id"] for m in d.get("data", [])] == ["mock-a"]
              and d["data"][0].get("context_length") == 8192, d)
        st, d, _ = v1("GET", "models")
        check("沒帶金鑰 → 401（OpenAI 錯誤格式）", st == 401 and d.get("error", {}).get("type") == "invalid_api_key", d)
        st, d, _ = v1("POST", "chat/completions", chat, ck)
        check("只帶網頁登入的 cookie 不算數 → 401", st == 401, d)
        st, d, _ = https(cp, ca, "GET", f"/v1/models?api_key={ka}")
        check("金鑰放網址參數 → 400", st == 400 and "網址" in d["error"]["message"], d)
        st, d, _ = v1("POST", "chat/completions", chat, {"Authorization": "Bearer xck_" + "A" * 43})
        check("金鑰不對 → 401", st == 401 and d["error"]["message"] == "金鑰不對", d)
        st, d, _ = v1("POST", "chat/completions", chat, bearer(ka))
        check("非串流對話：經代理送到模型、回應原樣回來", st == 200 and d["choices"][0]["message"]["content"] == "連線成功", d)
        st, d, _ = v1("POST", "chat/completions", chat, {"x-api-key": kb})
        check("x-api-key 標頭也收", st == 200, d)

        st, raw, h = v1("POST", "chat/completions", dict(chat, stream=True), bearer(ka), raw=True)
        lines = [ln for ln in raw.decode().split("\n") if ln.startswith("data:")]
        check("串流（客戶端沒要用量）：內容照轉，入口自己加的用量段落沒轉出去", st == 200 and "event-stream" in h.get("Content-Type", "")
              and len(lines) == 3 and lines[-1] == "data: [DONE]" and not any('"usage"' in ln for ln in lines), lines)
        st, raw, _ = v1("POST", "chat/completions", dict(chat, stream=True, stream_options={"include_usage": True}), bearer(ka), raw=True)
        check("串流（客戶端自己要了用量）：用量段落照轉", st == 200 and '"usage"' in raw.decode(), raw[-200:])
        st, d, _ = v1("POST", "messages", chat, bearer(ka))
        check("Strata 欄位的 Anthropic 格式", st == 200 and d["usage"]["input_tokens"] == 7, d)
        st, d, _ = v1("POST", "embeddings", {"model": "mock-a", "input": "x"}, bearer(ka))
        check("embeddings", st == 200 and d["data"][0]["embedding"] == [0.1, 0.2], d)

        st, d, _ = v1("POST", "chat/completions", dict(chat, model="nope"), bearer(ka))
        check("找不到模型 → 404 model_not_found", st == 404 and d["error"]["type"] == "model_not_found", d)
        st, d, _ = v1("GET", "models", headers=bearer(ko))
        check("只授權別格的金鑰：/v1/models 是空的", st == 200 and d["data"] == [], d)
        st, d, _ = v1("POST", "chat/completions", chat, bearer(ko))
        check("  用這格的模型 → 403", st == 403 and d["error"]["type"] == "permission_denied", d)
        st, d, _ = v1("POST", "admin", {}, bearer(ka))
        check("不在白名單的路徑 → 404", st == 404, d)
        st, d, _ = v1("GET", "chat/completions", headers=bearer(ka))
        check("方法不對 → 405", st == 405, d)
        st, d, _ = https(cp, ca, "POST", "/v1/chat/completions", None, {**bearer(ka), "Content-Type": "text/plain"})
        check("Content-Type 不是 JSON → 415", st == 415, d)
        st, d, _ = v1("POST", "chat/completions", {"messages": []}, bearer(ka))
        check("沒帶 model → 400", st == 400, d)

        res = []
        slow = dict(chat, messages=[{"role": "user", "content": "slow"}])
        ts = [threading.Thread(target=lambda: res.append(v1("POST", "chat/completions", slow, bearer(k1))[0])) for _ in range(2)]
        for t in ts:
            t.start()
            time.sleep(0.2)
        for t in ts:
            t.join()
        check("同時上限 1：第二個請求 → 429", sorted(res) == [200, 429], res)

        _, big = admin("POST", "/api/keys/create", {"name": "同時十二個", "max_inflight": 12})
        res2 = []
        ts = [threading.Thread(target=lambda: res2.append(v1("POST", "chat/completions", slow, bearer(big["key"]))[0])) for _ in range(10)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check("同時上限 12：10 個並行請求都通過（連線數上限跟著金鑰的同時上限走，不卡在 8 條）", res2 == [200] * 10, res2)

        st, d = admin("POST", f"/api/keys/{a['item']['id']}/revoke", {})
        st, d, _ = v1("POST", "chat/completions", chat, bearer(ka))
        check("撤銷王老師的金鑰 → 下一個請求就 401，不必重啟", st == 401 and "撤銷" in d["error"]["message"], d)
        st, d, _ = v1("POST", "chat/completions", chat, bearer(kb))
        check("  另一把金鑰照常可用", st == 200, d)

        _, lst = admin("GET", "/api/keys")
        u = {i["name"]: i["usage"] for i in lst["items"]}
        # 王老師：非串流 1、串流 2、messages 1、embeddings 1；找不到模型、撤銷後那次都在入口就擋下，沒送到模型不算
        ua = u["王老師"]
        check("用量記到對的金鑰：請求數、錯誤數", ua["requests"] == 5 and ua["errors"] == 0, ua)
        check("  token：非串流 7／2、兩次串流各 7／2、messages 7／2、embeddings 3／0", ua["prompt_tokens"] == 31
              and ua["completion_tokens"] == 8, ua)
        check("  另一把各算各的", u["機器學習（週三）"]["requests"] == 2, u["機器學習（週三）"])
        check("  被 403 擋下的不算用量", u["只能用別格"]["requests"] == 0, u["只能用別格"])
        check("  最後使用時間有記", next(i for i in lst["items"] if i["name"] == "機器學習（週三）")["last_used"])

        # ---- 舊共用金鑰：自動匯入，原金鑰經加密入口照樣能用；清單只看得到前幾個字元
        st, lst = admin("GET", "/api/keys")
        leg = [i for i in lst["items"] if i["source"] == "legacy"]
        check("舊共用金鑰自動匯入：名稱、只能用原本那一格", len(leg) == 1 and leg[0]["name"] == "舊共用金鑰（主腦）"
              and leg[0]["slots"] == ["t1/brain"] and leg[0]["prefix"] == ENGINE_KEY[:7] and leg[0]["created_by"] == "自動匯入", leg)
        check("  清單不含完整的舊金鑰", ENGINE_KEY not in json.dumps(lst))
        st, d, _ = v1("POST", "chat/completions", chat, bearer(ENGINE_KEY))
        check("  老師只改網址：原本的金鑰經加密入口打得通", st == 200, d)
        admin("GET", "/api/keys")
        check("  再讀一次不會重複匯入", len([i for i in admin("GET", "/api/keys")[1]["items"] if i["source"] == "legacy"]) == 1)
        st, t = admin("POST", "/api/node/t1/apiinfo/brain/test", {})
        g = t.get("gateway") or {}
        check("API 實測多一項加密入口：https 驗過憑證、沒帶金鑰被擋、帶臨時金鑰對話通", st == 200 and g.get("ok")
              and g.get("url", "").startswith("https://"), g)
        check("  臨時金鑰不寫資料庫、不記用量", not any(i["name"] == "實測" for i in admin("GET", "/api/keys")[1]["items"]))

        # 前面已經錯了 4 次（沒帶、只帶 cookie、金鑰不對、撤銷的）；API 實測故意不帶金鑰的那次不算
        n401 = 0
        for _ in range(26):
            st, _, _ = v1("GET", "models", headers={"Authorization": "Bearer xck_" + "B" * 43})
            n401 += st == 401
        st, d, _ = v1("GET", "models", headers={"Authorization": "Bearer xck_" + "B" * 43})
        check("同一來源送錯金鑰第 31 次 → 429；API 實測的那次不算進去", st == 429 and n401 == 26, (st, n401))
        _, aud = admin("GET", "/api/admin/audit?limit=200")
        rows = [r for r in aud.get("items", aud if isinstance(aud, list) else []) if r.get("action") == "gateway.auth"]
        check("  稽核只記一筆（每個來源每一輪）", len(rows) == 1, len(rows))
        blob = b"".join(open(f, "rb").read() for f in glob.glob(os.path.join(chome, ".xconsole", "xconsole.db*")))
        check("資料庫檔（含 WAL）找不到任何完整金鑰（含舊共用金鑰）", not any(k.encode() in blob for k in (ka, kb, ko, k1, ENGINE_KEY)))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(W, ignore_errors=True)
    print("全部通過" if OK else "有失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
