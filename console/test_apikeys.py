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
    today = gateway.day()
    x.STORE.usage_add(k2, today, "n1", "brain", True, 100, 20)
    x.STORE.usage_add(k2, today, "n1", "brain", False, 5, 0)
    x.STORE.usage_add(k2, "2000-01-01", "n1", "brain", True, 999, 999)
    st, lst = req("GET", "/api/keys")
    u = next(i for i in lst["items"] if i["id"] == k2)["usage"]
    check("用量：同一天累加、上個月的不算進本月", u == {"requests": 2, "errors": 1, "prompt_tokens": 105, "completion_tokens": 20}, u)
    bs = x.STORE.usage_by_slot(today)
    check("  依欄位加總（算直連量用）", bs.get(("n1", "brain"), {}).get("requests") == 2, bs)
    check("  日期用台北時間", gateway.day(0) == "1970-01-01" and gateway.day(16 * 3600) == "1970-01-02")

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

    print("全部通過" if OK else "有失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
