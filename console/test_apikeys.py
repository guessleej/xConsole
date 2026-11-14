#!/usr/bin/env python3
"""加密入口金鑰的管理：
- 產生：完整金鑰只回一次；資料庫（含 WAL）、稽核、清單都找不到明文，只有雜湊與前 8 個字元
- 欄位檢查：名稱、能用的模型（機器要在納管清單裡）、同時上限、有效天數
- 改名稱與權限、撤銷；撤銷後不能再改、不能再撤銷一次
- 用量依台北時間分天累加，清單附本月用量
- 加密入口網址：沒填用憑證上的名稱；填了要 https、/v1 結尾
跑法：python3 test_apikeys.py（隔離 HOME，不連外）
"""
import glob
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-keys-")
os.environ["HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent"))
import gateway  # noqa: E402
import xconsole_server as x  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


x.set_credentials("admin", "keys-test-pw")
x.update_fleet(lambda c: c.__setitem__("nodes", [{"id": "n1", "name": "測試機", "host": "127.0.0.1", "token": ""}]))
srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
COOKIE = {}


def req(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if COOKIE.get("v"):
        r.add_header("Cookie", "xconsole=" + COOKIE["v"])
    try:
        resp = urllib.request.urlopen(r, timeout=20)
    except urllib.error.HTTPError as e:
        resp = e
    payload = resp.read()
    sc = resp.headers.get("Set-Cookie") or ""
    if sc.startswith("xconsole="):
        COOKIE["v"] = sc.split(";")[0].split("=", 1)[1]
    return resp.status, json.loads(payload or b"{}")


def main():
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(HERE, "gateway.py")], capture_output=True, text=True)
    check("gateway.py 自我檢查（金鑰格式、標頭、串流用量）", r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-300:])
    st, _ = req("GET", "/api/keys")
    check("沒登入看不到金鑰清單", st == 401)
    st, _ = req("POST", "/api/keys/create", {"name": "x"})
    check("沒登入不能產生金鑰", st == 401)
    req("POST", "/api/login", {"username": "admin", "password": "keys-test-pw"})

    st, out = req("POST", "/api/keys/create", {"name": "資工系 王老師", "slots": ["n1/brain"], "max_inflight": 3})
    key = out.get("key", "")
    check("產生金鑰：回完整金鑰一次、xck_ 開頭 47 字", st == 200 and key.startswith("xck_") and len(key) == 47, out)
    item = out.get("item") or {}
    check("  清單項目不含雜湊、有前 8 個字元與狀態", "hash" not in item and item.get("prefix") == key[:8]
          and item.get("state") == "active" and item.get("slots") == ["n1/brain"] and item.get("max_inflight") == 3, item)
    kid = item.get("id", "")
    st, lst = req("GET", "/api/keys")
    check("  清單看得到這把、不含完整金鑰", st == 200 and any(i["id"] == kid for i in lst["items"]) and key not in json.dumps(lst), lst)
    row = x.STORE.key_by_hash(gateway.key_hash(key))
    check("  用雜湊查得到這把", row and row["id"] == kid)
    files = glob.glob(os.path.join(TMP, ".xconsole", "xconsole.db*"))
    check("  資料庫檔案（含 WAL）找不到完整金鑰", files and not any(key.encode() in open(f, "rb").read() for f in files), files)
    aud = json.dumps(x.STORE.audit_list(50), ensure_ascii=False)
    check("  稽核記了 apikey.create，沒有完整金鑰", "apikey.create" in aud and key not in aud)
    try:
        x.STORE.key_insert({**row, "id": "deadbeef", "hash": key})
        check("  儲存層拒收不是雜湊的值（防把明文當雜湊存）", False)
    except ValueError:
        check("  儲存層拒收不是雜湊的值（防把明文當雜湊存）", True)

    st, out2 = req("POST", "/api/keys/create", {"name": "全部模型"})
    check("預設：全部模型、同時上限 2、不到期", st == 200 and out2["item"]["slots"] == "*" and out2["item"]["max_inflight"] == 2
          and out2["item"]["expires"] is None, out2)
    check("  兩把金鑰不一樣", out2.get("key") != key)

    for body, why in (({"name": ""}, "名稱空白"), ({"name": "a" * 61}, "名稱太長"), ({"name": "a\nb"}, "名稱有控制字元"),
                      ({"name": "a", "slots": []}, "沒選模型"), ({"name": "a", "slots": ["沒有斜線"]}, "模型格式不對"),
                      ({"name": "a", "slots": ["n9/brain"]}, "機器不在清單"), ({"name": "a", "max_inflight": 0}, "上限 0"),
                      ({"name": "a", "max_inflight": 17}, "上限太大"), ({"name": "a", "max_inflight": True}, "上限是布林"),
                      ({"name": "a", "expires_days": -1}, "天數負數"), ({"name": "a", "expires_days": 4000}, "天數太大")):
        st, o = req("POST", "/api/keys/create", body)
        check(f"欄位檢查：{why} → 400", st == 400 and o.get("error"), (st, o))

    st, o = req("POST", f"/api/keys/{kid}/update", {"name": "資工系 王老師（改）", "slots": "*", "max_inflight": 1, "expires_days": 30})
    check("改名稱、權限、上限、有效天數", st == 200 and o["item"]["name"] == "資工系 王老師（改）" and o["item"]["slots"] == "*"
          and o["item"]["max_inflight"] == 1 and 29 * 86400 < o["item"]["expires"] - time.time() <= 30 * 86400, o)
    st, o = req("POST", f"/api/keys/{kid}/update", {"max_inflight": 2})
    check("  只送一個欄位，其他不變", st == 200 and o["item"]["name"] == "資工系 王老師（改）" and o["item"]["max_inflight"] == 2, o)
    st, o = req("POST", "/api/keys/0000beef/update", {"name": "x"})
    check("  改不存在的 → 404", st == 404)

    real = x.STORE.key_update
    x.STORE.key_update = lambda k, f: False          # 模擬：讀完之後、更新之前，別人把它撤銷了
    st, o = req("POST", f"/api/keys/{kid}/update", {"name": "搶在撤銷之後"})
    x.STORE.key_update = real
    check("  改的同時被撤銷（條件式更新沒改到）→ 409，不回成功", st == 409, (st, o))
    st, o = req("POST", f"/api/keys/{kid}/revoke", {})
    check("撤銷", st == 200 and o["item"]["state"] == "revoked" and o["item"]["revoked_by"] == "admin", o)
    st, o = req("POST", f"/api/keys/{kid}/revoke", {})
    check("  再撤銷一次 → 409", st == 409)
    st, o = req("POST", f"/api/keys/{kid}/update", {"name": "復活"})
    check("  撤銷後不能改 → 409", st == 409)
    check("  另一把不受影響", x.STORE.key_get(out2["item"]["id"])["revoked"] is None)
    exp = dict(row, expires=time.time() - 1, revoked=None)
    check("  過期的狀態是 expired", gateway.state(exp) == "expired")

    k2 = out2["item"]["id"]
    today = gateway.hour()
    x.STORE.usage_add(k2, today, "n1", "brain", True, 100, 20)
    x.STORE.usage_add(k2, today, "n1", "brain", False, 5, 0)
    x.STORE.usage_add(k2, "2000-01-01 08", "n1", "brain", True, 999, 999)
    st, lst = req("GET", "/api/keys")
    u = next(i for i in lst["items"] if i["id"] == k2)["usage"]
    check("用量：同一天累加、上個月的不算進本月", u == {"requests": 2, "errors": 1, "prompt_tokens": 105, "completion_tokens": 20}, u)
    bs = x.STORE.usage_by_slot(today)
    check("  依欄位加總（算直連量用）", bs.get(("n1", "brain"), {}).get("requests") == 2, bs)
    check("  以台北時間分小時", gateway.hour(0) == "1970-01-01 08" and gateway.hour(16 * 3600) == "1970-01-02 00")
    check("  起點可以給到幾點：前一小時的不算", x.STORE.usage_by_key(gateway.hour(time.time() + 3600)).get(k2) is None
          and x.STORE.usage_by_key(today)[k2]["requests"] == 2)

    st, o = req("GET", "/api/keys")
    check("加密入口網址：沒填就用主控台憑證上的名稱與埠", o["url"].startswith("https://") and o["url"].endswith(f":{x.PORT}/v1")
          and not o["url_custom"], o["url"])
    for bad in ("http://ai.example.edu.tw/v1", "https://ai.example.edu.tw/api", "javascript:alert(1)",
                "https://example.com:notaport/v1", "https://:/v1", "https://example.com:70000/v1", "https://u:p@example.com/v1"):
        st, o = req("POST", "/api/keys/settings", {"public_url": bad})
        check(f"  網址不收 {bad}", st == 400)
    st, o = req("POST", "/api/keys/settings", {"public_url": "https://ai.example.edu.tw/v1"})
    st2, o2 = req("GET", "/api/keys")
    check("  填了就用填的", st == 200 and o2["url"] == "https://ai.example.edu.tw/v1" and o2["url_custom"], o2)
    req("POST", "/api/keys/settings", {"public_url": ""})
    check("  清空就回到預設", not req("GET", "/api/keys")[1]["url_custom"])

    # ---- 主控台先升級、代理還是舊版（不認得 /relay）：講清楚要更新代理，不要回看不懂的 404
    import io
    x.GW_ROUTES.list_slots = lambda: [{"node": "n1", "slot": "brain", "kind": "llama", "name": "m1", "online": True, "ctx": 4096},
                                      {"node": "n1", "slot": "b2", "kind": "llama", "name": "dup", "online": True},
                                      {"node": "n1", "slot": "b3", "kind": "llama", "name": "dup", "online": True}]
    x.GW_ROUTES.invalidate()
    x.node_request = lambda nid, path, data, ctype="application/json": (urllib.request.Request("https://127.0.0.1:1" + path, data=data), None)

    def old_agent(req, ctx, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {"Content-Type": "application/json"},
                                     io.BytesIO(json.dumps({"error": "找不到"}).encode()))
    x.agent_open = old_agent
    k = out2["key"]

    def v1(path, body):
        r = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {k}"})
        try:
            resp = urllib.request.urlopen(r, timeout=20)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, json.loads(resp.read() or b"{}")
    st, o = v1("/v1/chat/completions", {"model": "m1", "messages": [{"role": "user", "content": "hi"}]})
    check("代理是舊版 → 502 並說要更新代理", st == 502 and "更新代理" in o["error"]["message"], (st, o))
    u = x.STORE.usage_by_key(gateway.day())[out2["item"]["id"]]
    check("  記成一次錯誤", u["errors"] == 2 and u["requests"] == 3, u)
    st, o = v1("/v1/chat/completions", {"model": "dup", "messages": [{"role": "user", "content": "hi"}]})
    check("兩格同名 → 409，請管理員改名", st == 409 and o["error"]["type"] == "model_name_conflict", (st, o))
    r = urllib.request.Request(BASE + "/v1/models", headers={"Authorization": f"Bearer {k}"})
    names = [m["id"] for m in json.loads(urllib.request.urlopen(r, timeout=20).read())["data"]]
    check("  /v1/models 不列重名的模型", names == ["m1"], names)

    # ---- 只在這把金鑰看得到的格子裡找名稱：別格剛好同名不影響（跟 /v1/models 一致）
    _, mine = req("POST", "/api/keys/create", {"name": "只能用 b2", "slots": ["n1/b2"]})
    k2 = mine["key"]
    r = urllib.request.Request(BASE + "/v1/models", headers={"Authorization": f"Bearer {k2}"})
    names = [m["id"] for m in json.loads(urllib.request.urlopen(r, timeout=20).read())["data"]]
    check("只授權 b2 的金鑰：/v1/models 列得出 dup（看不到同名的 b3）", names == ["dup"], names)
    k, k_saved = k2, k
    st, o = v1("/v1/chat/completions", {"model": "dup", "messages": [{"role": "user", "content": "hi"}]})
    check("  用 dup 會送到 b2，不回 409", st == 502 and "更新代理" in o["error"]["message"], (st, o))
    k = k_saved

    # ---- stream_options 不是物件：先擋下，不佔同時名額
    kid2 = out2["item"]["id"]
    for _ in range(3):
        st, o = v1("/v1/chat/completions", {"model": "m1", "stream": True, "stream_options": "x",
                                            "messages": [{"role": "user", "content": "hi"}]})
    check("stream_options 不是物件 → 400，同時名額沒有被卡住", st == 400 and x.GW_INFLIGHT.count(kid2) == 0, (st, o))

    # ---- 回應超過上限：回 502 明講，不送截斷的 JSON
    class Big:
        status, headers = 200, {"Content-Type": "application/json"}

        def read(self, n=-1):
            return b"{" + b" " * 50 + b"}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    x.agent_open, real_open, x.GW_MAX = (lambda req, ctx, timeout: Big()), x.agent_open, 10
    st, o = v1("/v1/chat/completions", {"model": "m1", "messages": [{"role": "user", "content": "hi"}]})
    x.agent_open, x.GW_MAX = real_open, 64 * 1024 * 1024
    check("回應超過上限 → 502，不送截斷的內容", st == 502 and "64 MB" in o["error"]["message"], (st, o))

    # ---- 金鑰放網址：參數名解碼後才比對；GET 宣稱有本文卻不送也不會卡住
    for q in ("api_key=abc", "api%5fkey=abc", "Access_Token=abc"):
        r = urllib.request.Request(BASE + "/v1/models?" + q, headers={"Authorization": f"Bearer {k}"})
        try:
            urllib.request.urlopen(r, timeout=10)
            st = 200
        except urllib.error.HTTPError as e:
            st = e.code
        check(f"網址參數 {q} → 400", st == 400, st)
    import socket
    host, port = srv.server_address
    sk = socket.create_connection((host, port), timeout=5)
    sk.sendall(b"GET /v1/models?api_key=x HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n")
    t0 = time.time()
    first = sk.recv(64)
    sk.close()
    check("  GET 宣稱有本文但不送：馬上回 400，不等本文", first.startswith(b"HTTP/1.1 400") and time.time() - t0 < 3, first)

    print("全部通過" if OK else "有失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
