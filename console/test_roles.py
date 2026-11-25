#!/usr/bin/env python3
"""多帳號與角色（管理員／維運／檢視／使用者）與群組範圍（清單 #10）：
- 守衛：從原始碼抓出每一條 /api 路由，用一般使用者的身分各打一次；沒列在 USER_ROUTES 的一律 403，
  而且使用者能用的路由要剛好等於這裡寫死的清單（放寬權限要改測試，不能悄悄放寬）
- 一次性密碼：新帳號第一次登入只能改密碼；新密碼至少 12 碼
- 每個人只看得到自己的：Agent 任務、Agent 資料夾、畫圖影片產出、金鑰；別人的用 id 直接查也是 404
- 使用者的 Agent 不能執行指令；第三方 API 看不到
- 停用、降級、重設密碼：現有登入馬上失效；最後一位管理員不能被停用、降級或刪除；不能刪自己
- 刪帳號：對話、附圖、登入一起刪，金鑰撤銷，Agent 資料夾改歸管理員
- 登入節流：同一來源、不同帳號各打錯 4 次誰都不擋；同一帳號錯 5 次只擋這個帳號
- 舊版只有 fleet.json 的單一帳號：升級後原密碼登得進去、角色是管理員
- 維運、檢視：逐條路由各打一次，能用的要剛好等於寫死的清單（檢視只能看、維運能換模型與套用群組設定）
- 群組範圍：範圍外的機器與群組在清單裡看不到，用id直接打是404；金鑰只能選範圍內的，範圍改了舊金鑰跟著縮；
  Agent的資料夾與模型都要在範圍內；維運只能改群組設定不能改名；群組刪了範圍不會變成不限；改角色或範圍登入失效
跑法：python3 test_roles.py（隔離 HOME，不連外）
"""
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
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
    ("GET", "/api/sd/catalog"), ("GET", "/api/agent/tasks"), ("GET", "/api/providers"), ("GET", "/api/assistant/tools"),
    ("POST", "/api/logout"), ("POST", "/api/account/password"), ("POST", "/api/chats/save"), ("POST", "/api/chats/delete-all"),
    ("POST", "/api/extract"), ("POST", "/api/preview"), ("POST", "/api/agent/tasks"), ("POST", "/api/keys/create"),
    ("POST", "/api/assistant"), ("POST", "/api/assistant/continue"), ("GET", "/api/issues/mine"), ("GET", "/api/issues/checking"),
    ("POST", "/api/issues"),
}
EXPECT_USER_RX = [r"GET /api/chats/(blob/[0-9a]{16}|[0-9a]{12})", r"GET /api/agent/tasks/[0-9a]{12}",
                  r"GET /api/node/a/(agent/folders|sd/recommend)", r"GET /api/node/a/sd/jobs(/[0-9a]{8}(/output(/0)?|/init)?)?",
                  r"POST /api/chats/[0-9a]{12}/(rename|delete|meta)", r"POST /api/agent/tasks/[0-9a]{12}/(approve|stop|delete)",
                  r"POST /api/node/a/chat/a", r"POST /api/node/a/agent/(folders/create|fs/list|fs/read)",
                  r"POST /api/node/a/sd/(generate|admit)", r"POST /api/node/a/sd/jobs/[0-9a]{8}/(cancel|delete)",
                  r"POST /api/keys/[0-9a]{8}/(update|revoke)"]


EXPECT_VIEWER_RX = [r"GET /api/(groups|catalog)", r"GET /api/groups/a{8}/(history|plan)",
                    r"GET /api/node/a/(state|counters|live|machine|library|recommend|llama/args|(llama|sd)/build(/progress)?|desktop/status)",
                    r"GET /api/node/a/(logs|catalog)/a", r"GET /api/node/a/slot/a/progress"]
EXPECT_OPERATOR_RX = [r"POST /api/node/a/slot/a/(start|stop|restart|model)", r"POST /api/node/a/(all/(start|stop|restart)|admit/a|profile/a)",
                      r"POST /api/groups/a{8}/(update|rollback)", r"POST /api/groups/a{8}/rejoin/a/(approve|reject)",
                      r"POST /api/node/a/slots/add"]


def expected(method, path, role="user"):
    rx = EXPECT_USER_RX + (EXPECT_VIEWER_RX if role in ("viewer", "operator") else []) + (EXPECT_OPERATOR_RX if role == "operator" else [])
    return (method, path) in EXPECT_USER or any(re.fullmatch(r, f"{method} {path}") for r in rx)


ANON = ("/api/login", "/api/logout", "/api/join", "/api/desktop/enter", "/api/account/password", "/api/health")


def member(admin, username, role, scope=None):
    """建一個帳號、改掉一次性密碼、登入。回 (Client, 密碼)。"""
    st, o = admin.req("POST", "/api/users/create", {"username": username, "display": username.split("@")[0], "role": role,
                                                    "scope": scope or []})
    assert st == 200, o
    c, pw = Client(), f"{role}-pass-{secrets_hex()}"
    c.login(username, o["password"])
    c.req("POST", "/api/account/password", {"old": o["password"], "new": pw})
    c.login(username, pw)
    return c, pw


def secrets_hex():
    return os.urandom(4).hex()


def roles_and_scope(admin):
    real_call = x.call_node
    x.call_node = lambda nid, path, method="GET", body=None, timeout=40: (200, {"ok": True})
    try:
        _roles_and_scope(admin)
    finally:
        x.call_node = real_call


def _roles_and_scope(admin):
    # ---- 守衛：維運與檢視逐條路由（不限群組，只看角色）
    rs = routes()
    for role in ("viewer", "operator"):
        c, _ = member(admin, f"{role}-all@cloudinfo.com.tw", role)
        wrong, opened = [], []
        for method, path in sorted(rs):
            if path in ANON:
                continue
            st, out = c.req(method, path, {} if method == "POST" else None)
            blocked = st == 403 and out.get("admin_only")
            if expected(method, path, role) and blocked:
                wrong.append(f"{method} {path}")
            elif not expected(method, path, role) and not blocked:
                opened.append(f"{method} {path}（{st}）")
        name = x.ROLE_NAMES[role]
        check(f"{name}：能用的路由都沒被擋", not wrong, wrong[:5])
        check(f"  {name}：其餘每一條都回403", not opened, opened[:8])
        extra = [f"{m} {p}" for m, p in rs if x.user_may(m, p, role) and not expected(m, p, role)]
        check(f"  {name}：路由清單沒有比測試寫死的更寬", not extra, extra[:5])
    check("不認得的角色當成使用者（最少權限）", not x.user_may("GET", "/api/groups", "superuser") and x.user_may("GET", "/api/chats", "superuser"))

    # ---- 群組範圍
    ga = admin.req("POST", "/api/groups", {"name": "電腦教室A"})[1]["group"]["id"]
    gb = admin.req("POST", "/api/groups", {"name": "電腦教室B"})[1]["group"]["id"]
    x.update_fleet(lambda c: c.__setitem__("nodes", [
        {"id": "n1", "name": "A01", "host": "127.0.0.1", "token": "", "group": "電腦教室A"},
        {"id": "n2", "name": "B01", "host": "127.0.0.1", "token": "", "group": "電腦教室B"}]))
    st, o = admin.req("POST", "/api/users/create", {"username": "bad-scope@cloudinfo.com.tw", "role": "operator", "scope": ["ffffffff"]})
    check("範圍填不存在的群組 → 400", st == 400, (st, o))
    op, op_pw = member(admin, "teacher-a@cloudinfo.com.tw", "operator", [ga])
    vw, _ = member(admin, "viewer-a@cloudinfo.com.tw", "viewer", [ga])
    me = op.req("GET", "/api/me")[1]
    check("/api/me帶角色與範圍", me.get("role") == "operator" and me.get("scope") == [ga], me)
    users = {u["username"]: u for u in admin.req("GET", "/api/users")[1]["items"]}
    check("  帳號清單帶範圍；沒限的是空的", users["teacher-a@cloudinfo.com.tw"]["scope"] == [ga]
          and users["viewer-all@cloudinfo.com.tw"]["scope"] == [], users.get("teacher-a@cloudinfo.com.tw"))

    fl = op.req("GET", "/api/fleet")[1]
    check("機器清單：只有範圍內的機器與群組", [n["id"] for n in fl["nodes"]] == ["n1"] and [g["name"] for g in fl["groups"]] == ["電腦教室A"], fl)
    gl = op.req("GET", "/api/groups")[1]
    check("  群組清單：只有範圍內的", [g["name"] for g in gl["groups"]] == ["電腦教室A"], gl.get("groups"))
    check("  管理員看全部", len(admin.req("GET", "/api/fleet")[1]["nodes"]) == 2)
    st, o = op.req("GET", "/api/node/n2/state")
    check("範圍外的機器用id直接查 → 404（不洩漏有沒有）", st == 404 and "沒有這台" in o.get("error", ""), (st, o))
    check("  範圍內的查得到", op.req("GET", "/api/node/n1/state")[0] == 200)
    check("  維運在範圍內換模型、啟動：可以", op.req("POST", "/api/node/n1/slot/s/start", {})[0] == 200
          and op.req("POST", "/api/node/n1/slot/s/model", {"model": "m"})[0] == 200)
    check("  範圍外的不能動 → 404", op.req("POST", "/api/node/n2/slot/s/start", {})[0] == 404)
    st, o = op.req("POST", "/api/node/n2/chat/s", {"messages": []})
    check("  範圍外的機器也不能對話（使用者功能一樣套範圍）", st == 404 and o.get("error") == "沒有這台機器", (st, o))
    st, o = op.req("POST", "/api/assistant", {"node": "n2", "slot": "s", "messages": [{"role": "user", "content": "x"}]})
    check("  範圍外的機器也不能用對話工具 → 404", st == 404 and "沒有這台" in o.get("error", ""), (st, o))
    check("  範圍外的機器查不到可用的工具 → 404", op.req("GET", "/api/assistant/tools?node=n2")[0] == 404)
    st, o = vw.req("POST", "/api/node/n1/slot/s/start", {})
    check("檢視：範圍內也不能操作 → 403並講清楚", st == 403 and "檢視" in o.get("error", ""), (st, o))
    check("  檢視看得到狀態", vw.req("GET", "/api/node/n1/state")[0] == 200)
    # 這台還沒有欄位時，「啟動模型」會先建一格空欄位：維運要能建，但只能是空欄位、只帶那幾個欄位
    sent = []
    keep_call = x.call_node
    x.call_node = lambda nid, path, method="GET", body=None, timeout=40: (sent.append((path, body)), (200, {"ok": True}))[1]
    st, _ = op.req("POST", "/api/node/n1/slots/add", {"type": "empty", "role": "主模型", "tier": "", "caps": ["文字"], "port": 22})
    check("維運在沒有欄位的機器按啟動：能建空欄位，多帶的欄位丟掉", st == 200 and sent[-1] == ("/slots/add", {"type": "empty", "role": "主模型", "tier": "", "caps": ["文字"]}), sent[-1:])
    st, o = op.req("POST", "/api/node/n1/slots/add", {"type": "systemd", "unit": "ssh.service"})
    check("  登記既有服務 → 403", st == 403 and len(sent) == 1, (st, o))
    check("  範圍外的機器 → 404", op.req("POST", "/api/node/n2/slots/add", {"type": "empty"})[0] == 404)
    x.call_node = keep_call
    for path in ("/api/nodes/n1/update", "/api/users/create", "/api/node/n1/library/download", "/api/groups"):
        st, o = op.req("POST", path, {})
        check(f"維運不能：{path} → 403", st == 403 and o.get("admin_only"), (st, o))

    # 群組設定：維運只能改設定，不能改名、改種類；範圍外的群組回404
    cur = next(g for g in admin.req("GET", "/api/groups")[1]["groups"] if g["id"] == ga)
    st, o = op.req("POST", f"/api/groups/{ga}/update", {"name": cur["name"], "kind": cur["kind"], "settings": {**cur["settings"], "models": ["auto"]}})
    check("維運改範圍內群組的設定 → 200，版本加一", st == 200 and o["group"]["version"] == cur["version"] + 1, (st, o))
    st, o = op.req("POST", f"/api/groups/{ga}/update", {"name": "改掉的名字"})
    check("  改名 → 403", st == 403, (st, o))
    check("  範圍外的群組 → 404", op.req("POST", f"/api/groups/{gb}/update", {"settings": cur["settings"]})[0] == 404)
    check("  檢視不能改群組設定 → 403", vw.req("POST", f"/api/groups/{ga}/update", {"settings": cur["settings"]})[0] == 403)
    check("  檢視看得到設定歷史、不能退回 → 403", vw.req("GET", f"/api/groups/{ga}/history")[0] == 200
          and vw.req("POST", f"/api/groups/{ga}/rollback", {"version": 1})[0] == 403)
    st, o = op.req("POST", f"/api/groups/{ga}/rollback", {"version": 1})
    check("  維運能退回設定；歷史記下是誰退的", st == 200 and o.get("changed")
          and op.req("GET", f"/api/groups/{ga}/history")[1]["history"][0]["by"] == "teacher-a@cloudinfo.com.tw", o)
    check("  範圍外的群組看歷史、退回 → 404", op.req("GET", f"/api/groups/{gb}/history")[0] == 404
          and op.req("POST", f"/api/groups/{gb}/rollback", {"version": 1})[0] == 404)
    check("  每種機型會拿到哪一版：檢視看得到自己範圍的，範圍外的 → 404", vw.req("GET", f"/api/groups/{ga}/plan")[0] == 200
          and op.req("GET", f"/api/groups/{gb}/plan")[0] == 404)

    # 金鑰：只能選範圍內的；範圍改了舊金鑰馬上跟著縮
    check("限定群組的帳號產生金鑰：不能選全部", op.req("POST", "/api/keys/create", {"name": "全部"})[0] == 400)
    check("  不能選範圍外的機器", op.req("POST", "/api/keys/create", {"name": "B", "slots": ["n2/s"]})[0] == 400)
    st, o = op.req("POST", "/api/keys/create", {"name": "A", "slots": ["n1/s"]})
    check("  範圍內的可以", st == 200, (st, o))
    kid = o["item"]["id"]
    check("  改金鑰也不能改成範圍外", op.req("POST", f"/api/keys/{kid}/update", {"slots": ["n2/s"]})[0] == 400)
    keep_rows = x.GW_ROUTES.rows
    x.GW_ROUTES.rows = lambda: [{"name": "a-model", "node": "n1", "slot": "s", "online": True},
                                {"name": "b-model", "node": "n2", "slot": "s", "online": True}]
    st, o = op.req("GET", "/api/keys")
    check("  金鑰頁的「可用模型」只列範圍內的（範圍外的不露）", st == 200 and o["models"] == ["a-model"], o.get("models"))
    check("  管理員看全部", admin.req("GET", "/api/keys")[1]["models"] == ["a-model", "b-model"])
    x.GW_ROUTES.rows = keep_rows
    row = x.STORE.key_get(kid)
    check("  加密入口：範圍內的格子用得到", x.key_allowed(row)({"node": "n1", "slot": "s"}))
    t = x.AgentTask("teacher-a@cloudinfo.com.tw", "", "n1", "f", {"kind": "local", "node": "n1", "slot": "s"}, "p", "l")
    t.state = "running"
    x.AGENT_TASKS[t.id] = t
    admin.req("POST", "/api/users/teacher-a@cloudinfo.com.tw/update", {"scope": [gb]})
    check("  管理員把他改到別的群組：舊金鑰馬上用不到原本那台", not x.key_allowed(row)({"node": "n1", "slot": "s"}))
    check("  他在原本那台進行中的 Agent 任務停掉", t.state == "stopped", t.state)
    check("改了範圍：他現在的登入馬上失效", op.req("GET", "/api/me")[0] == 401)
    op.login("teacher-a@cloudinfo.com.tw", op_pw)
    check("  重新登入後看到的是新範圍", [n["id"] for n in op.req("GET", "/api/fleet")[1]["nodes"]] == ["n2"])
    admin.req("POST", "/api/users/teacher-a@cloudinfo.com.tw/update", {"role": "viewer"})
    check("改了角色：他現在的登入馬上失效", op.req("GET", "/api/me")[0] == 401)

    # Agent：資料夾所在的機器與模型所在的機器都要在範圍內
    may = lambda n: n == "n1"  # noqa: E731
    code, _ = x.agent_start("viewer-a@cloudinfo.com.tw", "", {"node": "n2", "folder": "f", "prompt": "做事",
                                                               "model": {"kind": "local", "slot": "s"}}, can_exec=False, may_node=may)
    check("Agent：資料夾在範圍外的機器 → 404", code == 404, code)
    code, _ = x.agent_start("viewer-a@cloudinfo.com.tw", "", {"node": "n1", "folder": "f", "prompt": "做事",
                                                               "model": {"kind": "local", "node": "n2", "slot": "s"}}, can_exec=False, may_node=may)
    check("  模型在範圍外的機器 → 擋下", code == 400, code)

    # 群組刪了：範圍不會變成不限
    admin.req("POST", f"/api/groups/{ga}/delete", {})
    fl = vw.req("GET", "/api/fleet")[1]
    check("負責的群組被刪了：什麼都看不到（不會變成看全部）", fl["nodes"] == [] and fl["groups"] == [], fl)
    check("  帳號清單照實留著那個群組（畫面標「已刪除的群組」）",
          next(u for u in admin.req("GET", "/api/users")[1]["items"] if u["username"] == "viewer-a@cloudinfo.com.tw")["scope"] == [ga])
    st, o = admin.req("POST", "/api/users/viewer-a@cloudinfo.com.tw/update", {"display": "改名字", "scope": [ga]})
    check("  只改顯示名稱（表單把已刪除的群組照樣送回）：照收，範圍不變、不會變成不限", st == 200 and o["user"]["scope"] == [ga], (st, o))
    st, o = admin.req("POST", "/api/users/teacher-a@cloudinfo.com.tw/update", {"scope": [ga]})
    check("  別人不能被加進已刪除的群組（只有原本就有的才照收）", st == 400, (st, o))

    # 批次匯入收新角色；最後一位管理員不能被降成維運
    st, o = admin.req("POST", "/api/users/import", {"rows": [{"username": "op-imp@cloudinfo.com.tw", "role": "operator"},
                                                             {"username": "vw-imp@cloudinfo.com.tw", "role": "viewer"}]})
    check("批次匯入：維運、檢視照建", st == 200 and [c["role"] for c in o["created"]] == ["operator", "viewer"], o)
    last = [u for u in x.STORE.user_list() if u["role"] == "admin" and not u.get("disabled")]
    check("最後一位管理員不能被降成維運或檢視", len(last) == 1 and x.user_change(last[0]["username"], {"role": "operator"}, "x")[0] == 409
          and x.user_change(last[0]["username"], {"role": "viewer"}, "x")[0] == 409, [u["username"] for u in last])

    # 舊版的帳號表沒有範圍欄位：開起來自動補，原本的帳號範圍是空的（不限）
    import sqlite3
    from store import Store
    p = os.path.join(TMP, "old.db")
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE users(username TEXT PRIMARY KEY, display TEXT, role TEXT, source TEXT, salt TEXT, hash TEXT, algo TEXT,"
               " must_change INTEGER, disabled INTEGER, created REAL, pw_changed REAL, last_login REAL)")
    db.execute("INSERT INTO users(username, role) VALUES('old', 'user')")
    db.commit()
    db.close()
    old = Store(p).user_get("old")
    check("舊版資料庫升級：補上範圍欄位，原本的帳號不限群組", old and "scope" in old and x.scope_of(old) is None, old)


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
        if path in ANON:  # 不用登入的
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

    # ---- 審查補強
    st, o = admin.req("POST", "/api/users/create", {"username": "a0002@cloudinfo.com.tw", "display": "副管理員", "role": "admin"})
    a2 = Client()
    a2.login("a0002@cloudinfo.com.tw", o["password"])
    st, _ = a2.req("POST", "/api/branding", {"org": "被改掉的單位"})
    check("還沒改一次性密碼：連品牌設定也不能改（只放行看自己、改密碼、登出）", st == 403)
    a2.req("POST", "/api/account/password", {"old": o["password"], "new": "second-admin-pass-1"})
    a2.login("a0002@cloudinfo.com.tw", "second-admin-pass-1")

    t = x.AgentTask("a0002@cloudinfo.com.tw", "", "n1", "f", {"kind": "local"}, "p", "l")
    t.state, t.always = "waiting_approval", True
    x.AGENT_TASKS[t.id] = t
    admin.req("POST", "/api/users/a0002@cloudinfo.com.tw/update", {"role": "user"})
    check("管理員被降級：他進行中的 Agent 任務停掉，也不能再執行指令", t.state == "stopped" and not t.can_exec and not t.always)
    admin.req("POST", "/api/users/a0002@cloudinfo.com.tw/update", {"role": "admin"})
    code, out = x.agent_start("s9", "", {"node": "n1", "folder": "f", "prompt": "做事", "model": {"kind": "provider", "id": "p00000000001"}},
                              can_exec=False)
    check("使用者的 Agent 不能用第三方 API（單位付費的金鑰）", code == 403, (code, out))

    # 兩位管理員同時互相降級：至少留一位
    res = []
    th = [threading.Thread(target=lambda a, b: res.append(x.user_change(a, {"role": "user"}, b)[0]), args=p)
          for p in (("a0002@cloudinfo.com.tw", "admin"), ("admin", "a0002@cloudinfo.com.tw"))]
    [z.start() for z in th]
    [z.join() for z in th]
    check("兩位管理員同時互相降級：只成功一個，至少留一位管理員", sorted(res) == [200, 409] and x.STORE.admin_count() >= 1, res)
    keep = "admin" if x.STORE.user_get("admin")["role"] == "admin" else "a0002@cloudinfo.com.tw"
    gone = "a0002@cloudinfo.com.tw" if keep == "admin" else "admin"
    x.user_change(gone, {"role": "admin"}, keep)

    # 停用帳號：他自己產生的金鑰跟著不能用
    st, o = admin.req("POST", "/api/users/create", {"username": "s3001@cloudinfo.com.tw", "display": "丙"})
    u3 = Client()
    u3.login("s3001@cloudinfo.com.tw", o["password"])
    u3.req("POST", "/api/account/password", {"old": o["password"], "new": "student-three-pw-1"})
    u3.login("s3001@cloudinfo.com.tw", "student-three-pw-1")
    res = []
    th = [threading.Thread(target=lambda: res.append(u3.req("POST", "/api/keys/create", {"name": "並行"}))) for _ in range(6)]
    [z.start() for z in th]
    [z.join() for z in th]
    ok_keys = [r[1]["key"] for r in res if r[0] == 200]
    check("同時送 6 個產生金鑰的請求：只成功 3 把", len(ok_keys) == 3, [r[0] for r in res])

    def v1(key):
        r = urllib.request.Request(BASE + "/v1/models", headers={"Authorization": f"Bearer {key}"})
        try:
            resp = urllib.request.urlopen(r, timeout=20)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, json.loads(resp.read() or b"{}")
    admin.req("POST", "/api/users/s3001@cloudinfo.com.tw/update", {"disabled": True})
    st, o = v1(ok_keys[0])
    check("帳號停用：他自己產生的金鑰在加密入口也不能用", st == 401 and "停用" in o["error"]["message"], (st, o))

    # 刪帳號：Agent 任務與畫圖影片產出一起清掉，同名的新帳號看不到前一個人的東西
    t = x.AgentTask("s3001@cloudinfo.com.tw", "", "n1", "f", {"kind": "local"}, "p", "l")
    t.state = "done"
    x.AGENT_TASKS[t.id] = t
    deleted = []
    real_call = x.call_node
    x.call_node = lambda nid, path, method="GET", body=None, timeout=40: (
        (200, {"jobs": [{"id": "eeee0001", "by": "s3001@cloudinfo.com.tw"}, {"id": "ffff0002", "by": "admin"}]}) if path == "/sd/jobs"
        else (deleted.append(path), (200, {"ok": True}))[1] if path.endswith("/delete") else (404, {"error": "找不到"}))
    st, o = admin.req("POST", "/api/users/s3001@cloudinfo.com.tw/delete", {})
    x.call_node = real_call
    check("刪帳號：他的畫圖影片產出一起刪、別人的不動", deleted == ["/sd/jobs/eeee0001/delete"] and o.get("deleted_jobs") == 1, (deleted, o))
    check("  他的 Agent 任務紀錄一起丟掉", t.id not in x.AGENT_TASKS)

    # 刪掉從 fleet.json 搬進來的那位管理員：退回舊版用的那份帳密改成做這件事的管理員
    check("退回舊版用的帳密原本是 admin", x.fleet()["auth"]["username"] == "admin")
    a2.login("a0002@cloudinfo.com.tw", "second-admin-pass-1")       # 互相降級那一步可能把他登出了
    st, o = a2.req("POST", "/api/users/admin/delete", {})
    check("刪掉原本那位管理員：fleet.json 的帳密改成做這件事的管理員", st == 200 and x.fleet()["auth"]["username"] == "a0002@cloudinfo.com.tw"
          and x.fleet()["auth"]["hash"] == x.STORE.user_get("a0002@cloudinfo.com.tw")["hash"], (st, o))

    # 舊版搬進來的帳號可能是中文：管理動作照樣找得到
    x.STORE.user_insert({"username": "管理員", "display": "管理員", "role": "user", "disabled": 0})
    st, o = a2.req("POST", "/api/users/" + urllib.parse.quote("管理員") + "/update", {"display": "舊帳號"})
    check("帳號是中文（舊版搬進來的）：改得到", st == 200 and o["user"]["display"] == "舊帳號", (st, o))

    roles_and_scope(a2)

    print("全部通過" if OK else "有失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
