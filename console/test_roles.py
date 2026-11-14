#!/usr/bin/env python3
"""多帳號與角色（管理員／使用者）：
- 守衛：從原始碼抓出每一條 /api 路由，用一般使用者的身分各打一次；沒列在 USER_ROUTES 的一律 403，
  而且使用者能用的路由要剛好等於這裡寫死的清單（放寬權限要改測試，不能悄悄放寬）
- 一次性密碼：新帳號第一次登入只能改密碼；新密碼至少 12 碼
- 每個人只看得到自己的：Agent 任務、Agent 資料夾、畫圖影片產出、金鑰；別人的用 id 直接查也是 404
- 使用者的 Agent 不能執行指令；第三方 API 看不到
- 停用、降級、重設密碼：現有登入馬上失效；最後一位管理員不能被停用、降級或刪除；不能刪自己
- 刪帳號：對話、附圖、登入一起刪，金鑰撤銷，Agent 資料夾改歸管理員
- 登入節流：同一來源、不同帳號各打錯 4 次誰都不擋；同一帳號錯 5 次只擋這個帳號
- 舊版只有 fleet.json 的單一帳號：升級後原密碼登得進去、角色是管理員
跑法：python3 test_roles.py（隔離 HOME，不連外）
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-roles-")
os.environ["HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent"))
try:
    import re._parser as sre  # noqa: E402
except ImportError:             # Python 3.10 以前
    import sre_parse as sre  # noqa: E402
import xconsole_server as x  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OK = True
ADMIN_PW = "roles-admin-pass-2026"


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


# 舊版升上來：只有 fleet.json 的帳密、帳號表是空的
x.set_credentials("admin", ADMIN_PW)
with x.STORE.lock:
    x.STORE.db.execute("DELETE FROM users")
x.update_fleet(lambda c: c.__setitem__("nodes", [{"id": "n1", "name": "測試機", "host": "127.0.0.1", "token": ""}]))
srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


class Client:
    def __init__(self):
        self.cookie = ""

    def req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if self.cookie:
            r.add_header("Cookie", "xconsole=" + self.cookie)
        try:
            resp = urllib.request.urlopen(r, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        raw = resp.read()
        sc = resp.headers.get("Set-Cookie") or ""
        if sc.startswith("xconsole="):
            self.cookie = sc.split(";")[0].split("=", 1)[1]
        try:
            return resp.status, json.loads(raw or b"{}")
        except ValueError:
            return resp.status, {}

    def login(self, user, pw):
        return self.req("POST", "/api/login", {"username": user, "password": pw})


# ---------------------------------------------------------------- 從原始碼抓路由

def expand(rx):
    """把路由的正規式展開成幾個具體的路徑（每個分支各一個、可有可無的兩種都要）。"""
    def walk(items):
        outs = [""]
        for op, av in items:
            if op is sre.LITERAL:
                alts = [chr(av)]
            elif op is sre.SUBPATTERN:
                alts = walk(av[3])
            elif op is sre.BRANCH:
                alts = [s for b in av[1] for s in walk(b)]
            elif op is sre.IN:
                alts = ["a" if any(o is sre.CATEGORY or (o is sre.RANGE and a[0] <= 97 <= a[1]) for o, a in av) else "0"]
            elif op in (sre.MAX_REPEAT, sre.MIN_REPEAT):
                lo, hi, sub = av
                inner = walk(sub)
                alts = ([""] if lo == 0 else []) + [s * max(lo, 1) for s in inner]
            else:
                alts = [""]
            outs = [a + b for a in outs for b in alts][:64]
        return outs
    return walk(sre.parse(rx))


def routes():
    src = open(os.path.join(HERE, "xconsole_server.py"), encoding="utf-8").read().splitlines()
    start = next(i for i, ln in enumerate(src) if ln.startswith("class Handler("))
    end = next(i for i, ln in enumerate(src) if ln.startswith("class LinkHandler("))
    out, method = set(), None
    for ln in src[start:end]:
        if ln.startswith("    def do_GET"):
            method = "GET"
        elif ln.startswith("    def do_POST"):
            method = "POST"
        elif ln.startswith("    def "):
            method = None
        if not method:
            continue
        for lit in re.findall(r'path == "(/api/[^"]+)"', ln):
            out.add((method, lit))
        for tup in re.findall(r'path in \(([^)]*)\)', ln):
            for lit in re.findall(r'"(/api/[^"]+)"', tup):
                out.add((method, lit))
        for rx in re.findall(r're\.fullmatch\(r"(/api/[^"]+)", path\)', ln):
            for p in expand(rx):
                out.add((method, p))
    return out


# 使用者能用的：寫死在這裡。放寬權限要連這裡一起改
EXPECT_USER = {
    ("GET", "/api/me"), ("GET", "/api/fleet"), ("GET", "/api/branding"), ("GET", "/api/chats"), ("GET", "/api/keys"),
    ("GET", "/api/sd/catalog"), ("GET", "/api/agent/tasks"), ("GET", "/api/providers"),
    ("POST", "/api/logout"), ("POST", "/api/account/password"), ("POST", "/api/chats/save"), ("POST", "/api/chats/delete-all"),
    ("POST", "/api/extract"), ("POST", "/api/preview"), ("POST", "/api/agent/tasks"), ("POST", "/api/keys/create"),
}
EXPECT_USER_RX = [r"GET /api/chats/(blob/[0-9a]{16}|[0-9a]{12})", r"GET /api/agent/tasks/[0-9a]{12}",
                  r"GET /api/node/a/(agent/folders|sd/recommend)", r"GET /api/node/a/sd/jobs(/[0-9a]{8}(/output(/0)?|/init)?)?",
                  r"POST /api/chats/[0-9a]{12}/(rename|delete)", r"POST /api/agent/tasks/[0-9a]{12}/(approve|stop)",
                  r"POST /api/node/a/chat/a", r"POST /api/node/a/agent/(folders/create|fs/list|fs/read)",
                  r"POST /api/node/a/sd/(generate|admit)", r"POST /api/node/a/sd/jobs/[0-9a]{8}/(cancel|delete)",
                  r"POST /api/keys/[0-9a]{8}/(update|revoke)"]


def expected(method, path):
    return (method, path) in EXPECT_USER or any(re.fullmatch(r, f"{method} {path}") for r in EXPECT_USER_RX)


def main():
    admin = Client()
    st, out = admin.login("admin", ADMIN_PW)
    check("舊版升上來：fleet.json 的原密碼登得進去，角色是管理員", st == 200 and out.get("role") == "admin", (st, out))
    check("  帳號表有了這位管理員", (x.STORE.user_get("admin") or {}).get("role") == "admin")

    # ---- 建帳號：一次性密碼、第一次登入只能改密碼
    st, out = admin.req("POST", "/api/users/create", {"username": "s1101@cloudinfo.com.tw", "display": "王小明", "role": "user"})
    pw1 = out.get("password", "")
    check("新增使用者：回一次性密碼（16 碼）", st == 200 and len(pw1) == 16 and out["user"]["must_change"], out)
    st, lst = admin.req("GET", "/api/users")
    check("  帳號清單不含密碼雜湊", st == 200 and "hash" not in json.dumps(lst) and "salt" not in json.dumps(lst))
    st, _ = admin.req("POST", "/api/users/create", {"username": "s1101@cloudinfo.com.tw"})
    check("  同一個帳號再建一次 → 409", st == 409)
    st, _ = admin.req("POST", "/api/users/create", {"username": "a b"})
    check("  帳號格式不對 → 400", st == 400)
    u1 = Client()
    st, out = u1.login("s1101@cloudinfo.com.tw", pw1)
    check("一次性密碼登入：成功並標示要先改密碼", st == 200 and out.get("must_change") and out.get("role") == "user", out)
    st, out = u1.req("GET", "/api/chats")
    check("  改密碼之前什麼都不能用 → 403 must_change", st == 403 and out.get("must_change"), (st, out))
    st, out = u1.req("POST", "/api/account/password", {"old": pw1, "new": "short-pw-11"})
    check("  新密碼少於 12 碼 → 400", st == 400 and "12" in out.get("error", ""), out)
    U1PW = "student-new-pass-01"
    st, out = u1.req("POST", "/api/account/password", {"old": pw1, "new": U1PW})
    check("  改密碼成功", st == 200, out)
    st, _ = u1.login("s1101@cloudinfo.com.tw", U1PW)
    st, me = u1.req("GET", "/api/me")
    check("  用新密碼登入：/api/me 回角色與顯示名稱", st == 200 and me["role"] == "user" and me["display"] == "王小明"
          and not me["must_change"], me)

    # ---- 守衛：逐條路由
    rs = routes()
    check(f"從原始碼抓到 {len(rs)} 個路由樣本（每條路由、每個分支）", len(rs) > 150, len(rs))
    wrong, opened = [], []
    for method, path in sorted(rs):
        if path in ("/api/login", "/api/logout", "/api/join", "/api/desktop/enter", "/api/account/password", "/api/health"):  # 不用登入的
            continue
        st, out = u1.req(method, path, {} if method == "POST" else None)
        blocked = st == 403 and out.get("admin_only")
        if expected(method, path):
            if blocked:
                wrong.append(f"{method} {path}")
        elif not blocked:
            opened.append(f"{method} {path}（{st}）")
    check("  使用者能用的路由都沒被擋", not wrong, wrong[:5])
    check("  其餘每一條路由對使用者都回 403（只有管理員能用）", not opened, opened[:8])
    extra = [f"{m} {p}" for m, p in rs if x.user_may(m, p) and not expected(m, p)]
    check("  USER_ROUTES 沒有比測試寫死的清單更寬", not extra, extra[:5])

    # ---- 第三方 API 看不到
    x.STORE.provider_put({"id": "p00000000001", "name": "外部模型", "base_url": "https://api.example.com/v1", "api_key": ""})
    st, out = u1.req("GET", "/api/providers")
    check("使用者看不到第三方 API（單位付費的金鑰）", st == 200 and out["items"] == [], out)
    check("  管理員看得到", len(admin.req("GET", "/api/providers")[1]["items"]) == 1)

    # ---- 金鑰：只看得到、只改得到自己的；最多 3 把
    st, ak = admin.req("POST", "/api/keys/create", {"name": "管理員發的"})
    keys = [u1.req("POST", "/api/keys/create", {"name": f"自己的 {i}"}) for i in range(4)]
    check("使用者自己產生金鑰：最多 3 把有效的", [k[0] for k in keys] == [200, 200, 200, 409], [k[0] for k in keys])
    check("  記在本人名下", x.STORE.key_get(keys[0][1]["item"]["id"])["owner"] == "s1101@cloudinfo.com.tw")
    st, lst = u1.req("GET", "/api/keys")
    check("  清單只有自己的 3 把，附可用的模型名稱", st == 200 and len(lst["items"]) == 3 and "models" in lst
          and all(i["owner"] == "s1101@cloudinfo.com.tw" for i in lst["items"]), lst.get("items"))
    st, _ = u1.req("POST", f"/api/keys/{ak['item']['id']}/revoke", {})
    check("  撤銷別人的金鑰 → 404（當作不存在）", st == 404)
    st, _ = u1.req("POST", f"/api/keys/{keys[0][1]['item']['id']}/revoke", {})
    check("  撤銷自己的 → 200，之後又能再產生一把", st == 200 and u1.req("POST", "/api/keys/create", {"name": "再一把"})[0] == 200)

    # ---- Agent 任務：只看得到自己的
    def fake_task(user):
        t = x.AgentTask(user, "127.0.0.1", "n1", "f", {"kind": "local", "node": "n1", "slot": "s"}, "做一件事", "本機")
        t.state = "done"
        x.AGENT_TASKS[t.id] = t
        return t
    ta, tb = fake_task("admin"), fake_task("s1101@cloudinfo.com.tw")
    st, out = u1.req("GET", "/api/agent/tasks")
    check("Agent 任務清單：使用者只看得到自己的", [i["id"] for i in out["items"]] == [tb.id], out["items"])
    check("  別人的任務用 id 直接查 → 404", u1.req("GET", f"/api/agent/tasks/{ta.id}")[0] == 404)
    check("  別人的任務不能停 → 404", u1.req("POST", f"/api/agent/tasks/{ta.id}/stop", {})[0] == 404)
    check("  管理員看得到全部", len(admin.req("GET", "/api/agent/tasks")[1]["items"]) == 2)
    tt = x.AgentTask("u", "", "n1", "f", {"kind": "local"}, "p", "l")
    tt.can_exec = False
    check("  使用者的 Agent 不能執行指令（就算模型硬叫 run_command 也擋）",
          "不能執行指令" in x.agent_tool(tt, "run_command", {"command": "cat ~/.xconsole/master.key"}).get("error", ""))
    check("  也不會把 run_command 這個工具給模型", "run_command" not in x.agent_system_prompt("f", "linux", False))

    # ---- Agent 資料夾與畫圖影片產出：只看得到自己的
    real_call = x.call_node
    jobs = [{"id": "aaaa0001", "by": "s1101@cloudinfo.com.tw", "state": "done"}, {"id": "bbbb0002", "by": "admin", "state": "done"},
            {"id": "cccc0003", "by": "", "state": "done"}]

    def fake_call(nid, path, method="GET", body=None, timeout=40):
        if path == "/agent/folders":
            return 200, {"folders": [{"name": "mine"}, {"name": "theirs"}, {"name": "old"}], "os": "linux"}
        if path == "/agent/folders/create":
            return 200, {"ok": True, "name": body["name"]}
        if path.startswith("/agent/fs/"):
            return 200, {"ok": True}
        if path == "/sd/jobs":
            return 200, {"jobs": jobs}
        if path.startswith("/sd/jobs/"):
            return 200, {"ok": True}
        if path == "/sd/generate":
            SEEN.append(body)
            return 200, {"ok": True, "id": "dddd0004"}
        return 404, {"error": "找不到"}
    SEEN = []
    x.call_node = fake_call
    u1.req("POST", "/api/node/n1/agent/folders/create", {"name": "mine"})
    x.STORE.folder_set("n1", "theirs", "admin")
    st, out = u1.req("GET", "/api/node/n1/agent/folders")
    check("Agent 資料夾：使用者只看得到自己建的（升級前沒有擁有者的也看不到）", [f["name"] for f in out["folders"]] == ["mine"], out)
    st, out = admin.req("GET", "/api/node/n1/agent/folders")
    check("  管理員看得到全部並附擁有者", {f["name"]: f["owner"] for f in out["folders"]}
          == {"mine": "s1101@cloudinfo.com.tw", "theirs": "admin", "old": None}, out)
    check("  讀別人資料夾的檔案 → 404", u1.req("POST", "/api/node/n1/agent/fs/list", {"folder": "theirs"})[0] == 404)
    check("  在別人的資料夾開 Agent 任務 → 404", u1.req("POST", "/api/agent/tasks", {"node": "n1", "folder": "theirs", "prompt": "x"})[0] == 404)
    st, out = u1.req("GET", "/api/node/n1/sd/jobs")
    check("畫圖影片產出：使用者只看得到自己的", [j["id"] for j in out["jobs"]] == ["aaaa0001"], out)
    check("  別人的產出直接查 → 404", u1.req("GET", "/api/node/n1/sd/jobs/bbbb0002")[0] == 404)
    check("  別人的產出不能刪 → 404", u1.req("POST", "/api/node/n1/sd/jobs/bbbb0002/delete", {})[0] == 404)
    check("  自己的查得到", u1.req("GET", "/api/node/n1/sd/jobs/aaaa0001")[0] == 200)
    u1.req("POST", "/api/node/n1/sd/generate", {"model": "m", "prompt": "貓", "by": "admin"})
    check("  產生時記在本人名下（本文帶別人的名字也沒用）", SEEN and SEEN[-1]["by"] == "s1101@cloudinfo.com.tw", SEEN[-1:])
    check("  管理員看得到全部", len(admin.req("GET", "/api/node/n1/sd/jobs")[1]["jobs"]) == 3)
    x.call_node = real_call

    # ---- 停用、降級、重設密碼：現有登入馬上失效
    st, out = admin.req("POST", "/api/users/create", {"username": "t0001@cloudinfo.com.tw", "display": "李老師", "role": "admin"})
    t1 = Client()
    t1.login("t0001@cloudinfo.com.tw", out["password"])
    t1.req("POST", "/api/account/password", {"old": out["password"], "new": "teacher-pass-2026"})
    t1.login("t0001@cloudinfo.com.tw", "teacher-pass-2026")
    check("第二位管理員能管理帳號", t1.req("GET", "/api/users")[0] == 200)
    admin.req("POST", "/api/users/t0001@cloudinfo.com.tw/update", {"role": "user"})
    check("  降成使用者：現有登入馬上失效", t1.req("GET", "/api/me")[0] == 401)
    t1.login("t0001@cloudinfo.com.tw", "teacher-pass-2026")
    check("  重新登入後只剩使用者的權限", t1.req("GET", "/api/users")[0] == 403)
    admin.req("POST", "/api/users/t0001@cloudinfo.com.tw/update", {"disabled": True})
    check("  停用：現有登入失效、也登不進來", t1.req("GET", "/api/me")[0] == 401
          and t1.login("t0001@cloudinfo.com.tw", "teacher-pass-2026")[0] == 401)
    st, out = admin.req("POST", "/api/users/s1101@cloudinfo.com.tw/reset", {})
    check("  重設密碼：給新的一次性密碼，舊登入失效", st == 200 and len(out["password"]) == 16 and u1.req("GET", "/api/me")[0] == 401)
    st, out = admin.req("POST", "/api/users/admin/update", {"role": "user"})
    check("不能降級自己", st == 409, out)
    st, out = admin.req("POST", "/api/users/admin/delete", {})
    check("不能刪自己", st == 409, out)
    x.STORE.user_insert({"username": "only-admin-2", "role": "admin", "disabled": 1})
    check("  最後一位（還能登入的）管理員不能被降級", x.user_change("admin", {"role": "user"}, "someone-else")[0] == 409)

    # ---- 刪帳號：對話、登入一起刪，金鑰撤銷，資料夾改歸管理員
    x.STORE.chat_put("s1101@cloudinfo.com.tw", {"id": "c" * 12, "body": "enc:v1:xx", "title": "enc:v1:yy"})
    st, out = admin.req("POST", "/api/users/s1101@cloudinfo.com.tw/delete", {})
    check("刪帳號：撤銷他的金鑰", st == 200 and out["revoked_keys"] == 3, out)
    check("  對話一起刪", not x.STORE.chat_list("s1101@cloudinfo.com.tw"))
    check("  Agent 資料夾改歸做這件事的管理員", x.STORE.folder_owner("n1", "mine") == "admin")
    check("  金鑰都撤銷了", all(k["revoked"] for k in x.STORE.key_list(owner="s1101@cloudinfo.com.tw")))

    # ---- 批次匯入
    st, out = admin.req("POST", "/api/users/import", {"rows": [{"username": "s2001@cloudinfo.com.tw", "display": "甲"},
                                                               {"username": "s2002@cloudinfo.com.tw", "display": "乙"},
                                                               {"username": "壞 帳號"}, {"username": "s2001@cloudinfo.com.tw"}]})
    check("批次匯入：成功的回一次性密碼，失敗的標出第幾列", st == 200 and len(out["created"]) == 2
          and [e["row"] for e in out["errors"]] == [3, 4] and all(len(c["password"]) == 16 for c in out["created"]), out)
    aud = json.dumps(x.STORE.audit_list(200), ensure_ascii=False)
    check("  稽核沒有任何一次性密碼", not any(c["password"] in aud for c in out["created"]))

    # ---- 登入節流：一間教室在同一個 NAT 後面
    for u in ("s2001@cloudinfo.com.tw", "s2002@cloudinfo.com.tw"):
        for _ in range(4):
            Client().login(u, "wrong-password-x")
    pw2 = out["created"][0]["password"]
    check("同一來源、兩個帳號各錯 4 次：誰都沒被擋", Client().login("s2001@cloudinfo.com.tw", pw2)[0] == 200)
    for _ in range(5):
        Client().login("s2002@cloudinfo.com.tw", "wrong-password-x")
    check("  同一帳號錯到 5 次以上：只擋這個帳號", Client().login("s2002@cloudinfo.com.tw", out["created"][1]["password"])[0] == 429
          and Client().login("admin", ADMIN_PW)[0] == 200)

    print("全部通過" if OK else "有失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
