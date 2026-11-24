#!/usr/bin/env python3
"""企業版模組的擴充合約（核心API第1版，docs/WORKSTATION_FLEET.md第五節）。核心任何改動只要破壞合約，這支就會失敗。

用test/fake_module當模組，執行時才產一把臨時Ed25519金鑰簽章（repo裡不放私鑰），簽章與打包用tools/module_sign.py（格式的唯一來源）。

一、載入：簽章、逐檔sha256、清單外的檔（含__pycache__）、資料夾權限、core_api，任一不過就以開源核心啟動並寫原因與修法；
    沒宣告scope、掛在/api/ent/以外、呼叫還沒開放的擴充點，註冊就失敗；個人版不載入。
二、路由：登入、角色、群組範圍由核心在進handler之前檢查（網址、本文、模組資料三種範圍與全域）；範圍外一律404；
    handler出錯回500且不洩漏堆疊；POST寫稽核。
三、資料：整筆加密落地；在請求裡自動套目前帳號的群組範圍，讀不到也寫不進範圍外；全域資料只有不限群組的帳號碰得到。
四、網頁上傳：驗過才換上並熱載入；壞的套件不動原本那一份；只有管理員能上傳；移除後回到一般版。
跑法：python3 test/test_module_contract.py（隔離HOME，不連外）
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-modc-")
os.environ["HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "console"))
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import xconsole_server as x  # noqa: E402
import modules  # noqa: E402
import module_sign  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

OK = True
FAKE = os.path.join(HERE, "fake_module")
KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
INSTALL = os.path.join(TMP, "install")
os.makedirs(INSTALL, mode=0o755)


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def build(dest, src_patch=None, key=KEY, core_api=None, sign=True, after=None):
    """把假模組複製到dest、（可選）改寫 __init__.py、產清單、簽章。after(dest) 在簽章後再動手腳。"""
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(FAKE, dest, ignore=shutil.ignore_patterns("__pycache__"))
    if src_patch:
        p = os.path.join(dest, "__init__.py")
        s = open(p, encoding="utf-8").read()
        for a, b in src_patch:
            assert a in s, a
            s = s.replace(a, b)
        open(p, "w", encoding="utf-8").write(s)
    module_sign.build_manifest(dest, "xconsole-enterprise-test", "0.0.1", core_api)
    if sign:
        module_sign.sign(dest, key=key)
    if os.name != "nt":
        for dp, dn, fs in os.walk(dest):
            for d in dn:
                os.chmod(os.path.join(dp, d), 0o755)
            for f in fs:
                os.chmod(os.path.join(dp, f), 0o644)
        os.chmod(dest, 0o755)
    if after:
        after(dest)
    return dest


def load_case(name, want, **kw):
    build(x.MODULES.dir, **kw)
    ok = x.MODULES.load()
    st = x.MODULES.status()
    check(name, not ok and want in (st.get("reason") or "") and st.get("fix") is not None and not x.MODULES.routes, st)


def loading():
    x.MODULES.root = INSTALL
    x.MODULES.keys = {}
    build(x.MODULES.dir)
    x.MODULES.load()
    st = x.MODULES.status()
    check("核心沒有內建公鑰：不載入，說明要更新核心", not st["loaded"] and "公鑰" in st["reason"], st)
    x.MODULES.keys = {"test": PUB}
    load_case("沒有簽章：不載入", "manifest.sig", sign=False)
    load_case("別人的金鑰簽的：不載入", "簽章驗不過", key=Ed25519PrivateKey.generate())
    load_case("簽章後改了檔案：不載入，點名哪一個檔", "__init__.py",
              after=lambda d: open(os.path.join(d, "__init__.py"), "a").write("\n# 被改過\n"))
    load_case("清單外多了一個檔：不載入", "清單外", after=lambda d: open(os.path.join(d, "evil.py"), "w").write("x=1"))
    load_case("有__pycache__（可能被換成別的位元碼）：不載入", "清單外",
              after=lambda d: (os.makedirs(os.path.join(d, "__pycache__")), open(os.path.join(d, "__pycache__", "x.pyc"), "wb").write(b"x")))
    load_case("核心API不相容：不載入", "核心API", core_api=[99])
    if os.name != "nt":
        load_case("別的帳號也能寫：不載入", "別的帳號也能寫", after=lambda d: os.chmod(os.path.join(d, "__init__.py"), 0o666))
        load_case("上一層資料夾別人能寫（能把整個模組換掉）：不載入", "別的帳號也能寫",
                  after=lambda d: os.chmod(os.path.dirname(d), 0o777))
        os.chmod(INSTALL, 0o755)
    else:
        load_case("別的帳號也能寫（Everyone）：不載入", "能寫",
                  after=lambda d: subprocess.run(["icacls", d, "/grant", "*S-1-1-0:(OI)(CI)M"], capture_output=True))
    load_case("有路由沒宣告scope：註冊就失敗", "沒有宣告scope",
              src_patch=[('license_info, role="admin", scope=api.scope.GLOBAL)', 'license_info, role="admin")')])
    load_case("路由掛在/api/ent/以外：註冊就失敗", "/api/ent/",
              src_patch=[('"/api/ent/license"', '"/api/users/license"')])
    load_case("呼叫還沒開放的擴充點：明確失敗，不會默默沒作用", "還沒開放",
              src_patch=[('    api.static("static")', '    api.static("static")\n    api.on("online", lambda e: None)')])
    load_case("scope要的名稱不在網址裡：註冊就失敗", "命名群組",
              src_patch=[('scope=api.scope.path("room_id"))\n    api.route("POST", "/api/ent/notes"', 'scope=api.scope.path("nope"))\n    api.route("POST", "/api/ent/notes"')])
    x._ModCore.desktop = True
    build(x.MODULES.dir)
    x.MODULES.load()
    check("個人版（桌面版）：不載入企業版模組", not x.MODULES.state["loaded"] and "個人版" in x.MODULES.state["reason"])
    x._ModCore.desktop = False
    build(x.MODULES.dir)
    check("正確簽章的模組：載入，路由與前端都掛上", x.MODULES.load() and x.MODULES.state["routes"] == 8 and x.MODULES.state["static"],
          x.MODULES.status())

    api = modules.API(x.MODULES, x.MODULES.dir)
    public = {k for k in dir(api) if not k.startswith("_")}
    check("合約：api開放的就是這幾個", public == {"CORE_API", "scope", "route", "static", "store", "audit", "routes", "static_dir", "namespaces"},
          sorted(public))
    check("  第五節其他擴充點這一版明確回「還沒開放」", all(callable(getattr(api, n)) for n in modules.NOT_YET))


class Client:
    def __init__(self, base):
        self.base, self.cookie = base, ""

    def req(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if raw is None and data is not None:
            r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        if self.cookie:
            r.add_header("Cookie", "xconsole=" + self.cookie)
        try:
            resp = urllib.request.urlopen(r, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        sc = resp.headers.get("Set-Cookie") or ""
        if sc.startswith("xconsole="):
            self.cookie = sc.split(";")[0].split("=", 1)[1]
        b = resp.read()
        try:
            return resp.status, json.loads(b or b"{}")
        except ValueError:
            return resp.status, b


def member(admin, base, name, role, scope):
    st, o = admin.req("POST", "/api/users/create", {"username": name, "display": name, "role": role, "scope": scope})
    assert st == 200, o
    c = Client(base)
    c.req("POST", "/api/login", {"username": name, "password": o["password"]})
    c.req("POST", "/api/account/password", {"old": o["password"], "new": "contract-member-pw-1"})
    c.req("POST", "/api/login", {"username": name, "password": "contract-member-pw-1"})
    return c


def http():
    x.set_credentials("admin", "contract-admin-pass-1")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    admin = Client(base)
    admin.req("POST", "/api/login", {"username": "admin", "password": "contract-admin-pass-1"})
    ga = admin.req("POST", "/api/groups", {"name": "電腦教室A"})[1]["group"]["id"]
    gb = admin.req("POST", "/api/groups", {"name": "電腦教室B"})[1]["group"]["id"]
    vw = member(admin, base, "viewer-a@cloudinfo.com.tw", "viewer", [ga])
    op = member(admin, base, "teacher-a@cloudinfo.com.tw", "operator", [ga])
    us = member(admin, base, "student@cloudinfo.com.tw", "user", [])
    anon = Client(base)

    st, o = anon.req("GET", f"/api/ent/rooms/{ga}")
    check("沒登入：401", st == 401, (st, o))
    st, o = vw.req("GET", f"/api/ent/rooms/{ga}")
    check("網址裡的群組：範圍內 → 進handler，帶群組與身分", st == 200 and o == {"room": ga, "user": "viewer-a@cloudinfo.com.tw", "role": "viewer"}, (st, o))
    check("  範圍外 → 404（不洩漏有沒有）", vw.req("GET", f"/api/ent/rooms/{gb}")[0] == 404)
    check("  不存在的群組 → 404", vw.req("GET", "/api/ent/rooms/ffffffff")[0] == 404)
    check("  角色不夠（使用者）→ 403", us.req("GET", f"/api/ent/rooms/{ga}")[0] == 403)
    check("沒有這條路由 → 404", admin.req("GET", "/api/ent/nope")[0] == 404)

    st, o = op.req("POST", "/api/ent/notes", {"group_id": ga, "id": "n1", "text": "A教室的筆記"})
    check("本文裡的群組：範圍內 → 200", st == 200, (st, o))
    check("  範圍外 → 404", op.req("POST", "/api/ent/notes", {"group_id": gb, "id": "n9"})[0] == 404)
    check("  沒帶群組 → 400", op.req("POST", "/api/ent/notes", {"id": "n9"})[0] == 400)
    check("  檢視不能寫（角色不夠）→ 403", vw.req("POST", "/api/ent/notes", {"group_id": ga, "id": "n9"})[0] == 403)
    admin.req("POST", "/api/ent/notes", {"group_id": gb, "id": "n2", "text": "B教室的筆記"})
    st, o = op.req("GET", f"/api/ent/rooms/{ga}/notes")
    check("資料清單自動套範圍：只看得到A教室的", st == 200 and [i["id"] for i in o["items"]] == ["n1"], o)
    check("  管理員看得到全部", [i["id"] for i in admin.req("GET", f"/api/ent/rooms/{ga}/notes")[1]["items"]] == ["n1", "n2"])
    st, o = op.req("POST", "/api/ent/sneak", {"group_id": ga, "other": gb})
    check("模組自己想寫到範圍外的群組：核心擋下（404），沒寫進去", st == 404 and x.MODULES.row("ent", "notes", "sneak") is None, (st, o))

    db = sqlite3.connect(os.path.join(x.STATE_DIR, "xconsole.db"))
    blobs = [r[0] for r in db.execute("SELECT data FROM ent_rows")]
    db.close()
    raw = b"".join(open(os.path.join(x.STATE_DIR, f), "rb").read() for f in os.listdir(x.STATE_DIR) if f.startswith("xconsole.db"))
    check("模組資料整筆加密落地（enc:v1:），資料庫檔找不到明文", blobs and all(b.startswith("enc:v1:") for b in blobs)
          and "A教室的筆記".encode() not in raw, blobs[:1])

    admin.req("POST", "/api/ent/seed", {"templates": {"t-a": ga, "t-b": gb, "t-g": None}})
    st, o = vw.req("GET", "/api/ent/templates/t-a")
    check("模組資料的範圍（scope.obj）：那筆屬於範圍內的群組 → 200", st == 200 and o["group"] == ga and o["tpl"] == {"name": "t-a"}, (st, o))
    check("  屬於範圍外的群組 → 404", vw.req("GET", "/api/ent/templates/t-b")[0] == 404)
    check("  全域資料：限定群組的帳號 → 404", vw.req("GET", "/api/ent/templates/t-g")[0] == 404)
    check("  查不到那筆 → 404", vw.req("GET", "/api/ent/templates/none")[0] == 404)
    check("  管理員碰得到全域資料", admin.req("GET", "/api/ent/templates/t-g")[0] == 200)
    check("全域（scope.GLOBAL）：管理員 → 200", admin.req("GET", "/api/ent/license")[0] == 200)
    st, o = vw.req("GET", "/api/ent/boom")
    check("  限定群組的帳號 → 403", st == 403, (st, o))
    st, o = admin.req("GET", "/api/ent/boom")
    check("handler出錯：500，不洩漏堆疊與內部路徑", st == 500 and o == {"error": "模組發生錯誤，已記錄"}, (st, o))

    aud = json.dumps(x.STORE.audit_list(200), ensure_ascii=False)
    check("POST寫稽核（核心記一筆，模組自己也能記）", "ent.notes" in aud and "ent.note.add" in aud)
    check("  模組寫的稽核先過遮罩", "不該進稽核" not in aud)

    st, js = admin.req("GET", "/ent/static/main.js")
    check("模組的前端檔：登入後拿得到", st == 200 and b"registerPage" in js, st)
    check("  沒登入 → 401", anon.req("GET", "/ent/static/main.js")[0] == 401)
    check("  跳出前端資料夾 → 404", admin.req("GET", "/ent/static/../__init__.py")[0] in (400, 404))
    me = admin.req("GET", "/api/me")[1]
    check("/api/me告訴畫面模組已載入、有前端檔", me.get("ent") == {"loaded": True, "static": True}, me.get("ent"))

    # ---- 網頁上傳與移除
    def upload(client, data):
        return client.req("POST", "/api/modules/upload", raw=data,
                          headers={"Content-Type": "application/octet-stream", "X-XConsole-Upload": "1"})
    good_dir = build(os.path.join(TMP, "pkg-good"))
    module_sign.pack(good_dir, os.path.join(TMP, "good.zip"))
    bad_dir = build(os.path.join(TMP, "pkg-bad"), after=lambda d: open(os.path.join(d, "__init__.py"), "a").write("\n#x\n"))
    module_sign.pack(bad_dir, os.path.join(TMP, "bad.zip"))
    check("非管理員不能上傳模組 → 403", upload(op, open(os.path.join(TMP, "good.zip"), "rb").read())[0] == 403)
    st, o = upload(admin, open(os.path.join(TMP, "bad.zip"), "rb").read())
    check("上傳被改過的套件：400，講原因與修法，原本那一份照常", st == 400 and "sha256" in o["error"] and o.get("fix")
          and x.MODULES.state["loaded"] and admin.req("GET", "/api/ent/license")[0] == 200, (st, o))
    import io
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("../evil.py", "x")
    st, o = upload(admin, b.getvalue())
    check("上傳路徑往上跳的zip：400", st == 400 and "不安全" in o["error"], (st, o))
    st, o = upload(admin, open(os.path.join(TMP, "good.zip"), "rb").read())
    check("上傳正確的套件：驗過、換上並熱載入", st == 200 and o["loaded"] and o["name"] == "xconsole-enterprise-test", (st, o))
    st, o = admin.req("POST", "/api/modules/remove", {})
    check("移除：回到一般版，模組路由消失", st == 200 and not o["loaded"] and admin.req("GET", "/api/ent/license")[0] == 404, (st, o))
    st, o = admin.req("GET", "/api/modules")
    check("  狀態頁：沒有模組不是錯誤（沒有原因）", st == 200 and not o["loaded"] and not o["reason"], o)
    srv.shutdown()


if __name__ == "__main__":
    loading()
    http()
    print("全部通過" if OK else "有失敗")
    sys.exit(0 if OK else 1)
