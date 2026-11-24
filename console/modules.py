"""企業版模組的載入器與擴充合約（核心API第1版，docs/WORKSTATION_FLEET.md第五節、清單#11）。

載入：主控台啟動時找<安裝目錄>/enterprise/。依序
  1. 讀manifest.json（模組名稱、版本、core_api、每個檔的sha256）
  2. 用核心內建的云碩公鑰，以Ed25519驗manifest.sig
  3. 逐檔核對sha256；清單外的檔（含__pycache__）一律不收
  4. 目錄與檔案只有系統管理員能寫（Linux、macOS看擁有者與權限；Windows看ACL）
  5. core_api相容
  6. 全部通過才import，呼叫register(api)
任一步失敗都以開源核心啟動（等同沒有企業版），設定頁顯示原因與修法，不默默失效。授權由模組自己驗，核心不碰。
簽章的作用是防止被竄改的模組被載入，不是防盜版：公開核心可以被人改掉驗證步驟。

擴充合約：模組只拿得到api這個物件。
  - api.route(method, pattern, handler, role, scope)：只能掛在/api/ent/底下；scope必填。
    登入、角色、群組範圍、本文大小、統一錯誤處理、稽核都由核心在進handler之前做，不交給模組判斷。
  - api.scope.path(名稱)／api.scope.body(欄位)／api.scope.obj(資料表, 名稱)／api.scope.GLOBAL
  - api.store(namespace)：模組自己的資料，每筆都有group_id（全域資料填None）；整筆AES-256-GCM加密落地（enc:v1:）；
    在請求裡查詢時，核心自動套目前帳號的群組範圍，範圍外的讀不到、寫不進去。
  - api.static(資料夾)：前端檔案掛在/ent/static/
  - api.audit(action, target, ok, detail)：寫稽核，寫入前遮罩
其餘第五節列的擴充點（單一登入、角色對應、群組欄位、排列方式、套用排程、事件、報告章節）這一版還沒開放，
模組呼叫會明確失敗，不會默默沒作用。
"""
import base64
import contextvars
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import zipfile

CORE_API = 1
ROLE_RANK = {"user": 0, "viewer": 1, "operator": 2, "admin": 3}
# 云碩的模組簽章公鑰（Ed25519，32位元組hex）。私鑰離線保管，不進repo、不進CI、不放環境變數。
# 換金鑰：在保管私鑰的機器跑tools/module_sign.py keygen，把新公鑰加進來發新版核心；舊的那把等客戶都換到新簽的模組再拿掉
TRUSTED_KEYS = {
    "xcloudinfo-2026-11": "ed21ccdd49b8f3ccce2b77be216a4cd54d11ae5f631ae23bbc72ca0eaccf7b71",
}
PKG = "xconsole_ent"
ENTRY = "__init__.py"
RESERVED = ("manifest.json", "manifest.sig")
NAME_RX = re.compile(r"[a-z][a-z0-9_]{0,31}")
ID_RX = re.compile(r"[\w.-]{1,64}")
MAX_ZIP = 32 * 1024 * 1024
MAX_FILES = 2000
MAX_UNPACKED = 128 * 1024 * 1024
NOT_YET = ("auth_provider", "role_mapper", "group_fields", "group_layout", "apply_plan", "on", "report_section")
# 目前請求的群組範圍：UNSCOPED＝不在請求裡（背景工作），None＝不限，frozenset＝只能這些群組
UNSCOPED = object()
CURRENT = contextvars.ContextVar("xc_ent_scope", default=UNSCOPED)


class ModuleError(Exception):
    pass


# ---------------------------------------------------------------- 範圍
class Scope:
    def __init__(self, kind, a=None, b=None):
        self.kind, self.a, self.b = kind, a, b

    def __repr__(self):
        return f"scope.{self.kind}({self.a or ''}{', ' + self.b if self.b else ''})"


class _ScopeNS:
    GLOBAL = Scope("global")

    @staticmethod
    def path(name):
        return Scope("path", str(name))

    @staticmethod
    def body(field):
        return Scope("body", str(field))

    @staticmethod
    def obj(table, name):
        if not NAME_RX.fullmatch(str(table)):
            raise ModuleError(f"資料表名稱不合法：{table}")
        return Scope("obj", str(table), str(name))


class Route:
    def __init__(self, method, pattern, handler, role, scope):
        self.method, self.pattern, self.handler, self.role, self.scope = method, pattern, handler, role, scope
        self.rx = re.compile(pattern)
        self.name = re.sub(r"[^\w]+", ".", pattern.replace("/api/ent/", "")).strip(".")[:60] or "route"


class Request:
    """交給模組handler的請求：核心已經驗過登入、角色與群組範圍。"""

    def __init__(self, method, path, params, query, body, user, role, group_id, scope):
        self.method, self.path, self.params, self.query, self.body = method, path, params, query, body
        self.user, self.role, self.group_id = user, role, group_id
        self.scope = None if scope is None else sorted(scope)


# ---------------------------------------------------------------- 模組自己的資料
class Table:
    def __init__(self, loader, ns, name):
        self.loader, self.ns, self.name = loader, ns, name

    def _scope(self):
        return CURRENT.get()

    def _visible(self, gid):
        sc = self._scope()
        if sc is UNSCOPED or sc is None:
            return True
        return gid is not None and gid in sc                  # 限定群組的帳號看不到全域資料

    def get(self, rid):
        row = self.loader.row(self.ns, self.name, str(rid))
        if not row or not self._visible(row[0]):
            return None
        return self.loader.unseal(row[1])

    def list(self):
        out = []
        for rid, gid, blob in self.loader.rows(self.ns, self.name):
            if self._visible(gid):
                out.append({"id": rid, "group_id": gid, "data": self.loader.unseal(blob)})
        return out

    def put(self, rid, data, group_id=None):
        rid = str(rid)
        if not ID_RX.fullmatch(rid):
            raise ModuleError("資料編號只能用英數字與 . _ -，最多64字")
        if group_id is not None and group_id not in self.loader.core.group_ids():
            raise ModuleError("沒有這個群組")
        cur = self.loader.row(self.ns, self.name, rid)
        if not self._visible(group_id) or (cur and not self._visible(cur[0])):
            raise PermissionError("範圍外的群組")              # 核心轉成404
        self.loader.write(self.ns, self.name, rid, group_id, self.loader.seal(data))

    def delete(self, rid):
        cur = self.loader.row(self.ns, self.name, str(rid))
        if not cur:
            return False
        if not self._visible(cur[0]):
            raise PermissionError("範圍外的群組")
        return self.loader.remove_row(self.ns, self.name, str(rid))


class Store:
    def __init__(self, loader, ns):
        self.loader, self.ns = loader, ns

    def table(self, name):
        if not NAME_RX.fullmatch(str(name)):
            raise ModuleError(f"資料表名稱不合法：{name}")
        return Table(self.loader, self.ns, name)


# ---------------------------------------------------------------- 給模組的api
class API:
    CORE_API = CORE_API
    scope = _ScopeNS

    def __init__(self, loader, root):
        self._loader, self._root, self._frozen = loader, root, False
        self.routes, self.static_dir, self.namespaces = [], None, set()

    def __getattr__(self, name):
        if name in NOT_YET:
            def nope(*a, **k):
                raise ModuleError(f"核心API第{CORE_API}版還沒開放api.{name}")
            return nope
        raise AttributeError(name)

    def _check_open(self):
        if self._frozen:
            raise ModuleError("只能在register(api)裡註冊")

    def route(self, method, pattern, handler, role=None, scope=None):
        self._check_open()
        method = str(method).upper()
        if method not in ("GET", "POST"):
            raise ModuleError(f"只收GET與POST：{method}")
        if not isinstance(pattern, str) or not pattern.startswith("/api/ent/"):
            raise ModuleError(f"路由只能掛在/api/ent/底下：{pattern}")
        if role not in ROLE_RANK:
            raise ModuleError(f"{pattern} 要宣告角色（{'、'.join(ROLE_RANK)}）")
        if not isinstance(scope, Scope):
            raise ModuleError(f"{pattern} 沒有宣告scope：每條路由都要說明碰的是哪個群組，由核心檢查")
        if not callable(handler):
            raise ModuleError(f"{pattern} 的handler不是函式")
        r = Route(method, pattern, handler, role, scope)
        need = scope.a if scope.kind == "path" else scope.b if scope.kind == "obj" else None
        if need and need not in r.rx.groupindex:
            raise ModuleError(f"{pattern} 的scope要的「{need}」不在網址的命名群組裡")
        if scope.kind == "body" and method != "POST":
            raise ModuleError(f"{pattern}：scope.body只能用在POST")
        if any(x.method == method and x.pattern == pattern for x in self.routes):
            raise ModuleError(f"重複註冊：{method} {pattern}")
        self.routes.append(r)

    def static(self, sub="static"):
        self._check_open()
        d = os.path.realpath(os.path.join(self._root, str(sub)))
        if not d.startswith(os.path.realpath(self._root) + os.sep) or not os.path.isdir(d):
            raise ModuleError(f"找不到前端資料夾：{sub}")
        self.static_dir = d

    def store(self, namespace):
        if not NAME_RX.fullmatch(str(namespace)):
            raise ModuleError(f"namespace不合法：{namespace}")
        self.namespaces.add(namespace)
        return Store(self._loader, namespace)

    def audit(self, action, target="", ok=True, detail=None):
        self._loader.core.audit("ent." + str(action)[:60], str(target)[:120], bool(ok), detail)


# ---------------------------------------------------------------- 檢查
def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _perm_problems(d):
    """只有系統管理員（Linux、macOS：root或主控台自己的帳號；Windows：SYSTEM、Administrators、主控台自己的帳號）能寫。
    回問題清單。也看上一層資料夾：別人能改名上一層，就能把整個模組換掉。"""
    if os.name == "nt":
        return _win_acl_problems(d)
    bad = []
    me = os.geteuid()
    paths = [os.path.dirname(os.path.abspath(d)), d]
    for dp, dn, fs in os.walk(d):
        paths += [os.path.join(dp, x) for x in dn + fs]
    for p in paths:
        st = os.lstat(p)
        if os.path.islink(p):
            bad.append(f"{p} 是連結")
        elif st.st_uid not in (0, me):
            bad.append(f"{p} 的擁有者不是root或主控台的帳號")
        elif st.st_mode & 0o022:
            bad.append(f"{p} 別的帳號也能寫（權限{oct(st.st_mode & 0o777)}）")
    return bad


_PS_ACL = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$ok = @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464', $me)
$mask = 0xD0156 -bor 0x40000000 -bor 0x10000000
$root = $env:XC_MODULE_DIR
$items = @((Get-Item -LiteralPath (Split-Path -Parent $root)), (Get-Item -LiteralPath $root)) + @(Get-ChildItem -LiteralPath $root -Recurse -Force)
foreach ($i in $items) {
  if ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) { "$($i.FullName) 是連結"; continue }
  # 用.NET讀ACL，不靠Get-Acl（Microsoft.PowerShell.Security模組在某些環境載不起來）
  $acl = if ($i.PSIsContainer) { (New-Object IO.DirectoryInfo $i.FullName).GetAccessControl() } else { (New-Object IO.FileInfo $i.FullName).GetAccessControl() }
  # 擁有者本身要是系統管理員或主控台的帳號：OWNER RIGHTS（S-1-3-4）與CREATOR OWNER（S-1-3-0）給的權限落在擁有者身上
  $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
  if ($ok -notcontains $owner) { "$($i.FullName) 的擁有者 $owner 不是系統管理員或主控台的帳號" }
  foreach ($a in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
    if ($a.AccessControlType -ne 'Allow') { continue }
    if ($a.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }   # 只給之後新建的子項，不作用在這一項
    if (@('S-1-3-4', 'S-1-3-0') -contains $a.IdentityReference.Value) { continue }
    $sid = $a.IdentityReference.Value
    if ($ok -contains $sid) { continue }
    if (([int64]$a.FileSystemRights -band $mask) -ne 0) { "$($i.FullName) $($a.IdentityReference) 能寫" }
  }
}
"""


def _win_acl_problems(d):
    try:
        # 路徑走環境變數：-Command模式不會把後面的參數放進$args
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_ACL],
                           capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
                           # 拿掉PSModulePath：從PowerShell 7帶起的行程會傳下7版的模組路徑，Windows PowerShell 5.1吃到就載不了自己的模組
                           env={**{k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}, "XC_MODULE_DIR": os.path.abspath(d)})
    except (OSError, subprocess.TimeoutExpired) as ex:
        return [f"查不了權限：{ex}"]
    if r.returncode != 0:
        return [f"查不了權限：{(r.stderr or r.stdout).strip()[:200]}"]
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def verify(d, keys):
    """回 (manifest, None) 或 (None, (原因, 修法))。順序照規格：清單、簽章、sha256、權限、core_api。"""
    mp, sp = os.path.join(d, "manifest.json"), os.path.join(d, "manifest.sig")
    try:
        raw = open(mp, "rb").read()
        man = json.loads(raw)
        if not isinstance(man, dict) or not isinstance(man.get("files"), dict):
            raise ValueError
    except (OSError, ValueError):
        return None, ("讀不到manifest.json，或格式不對", "請重新上傳云碩提供的完整模組套件")
    try:
        sig = base64.b64decode(open(sp, "rb").read().strip(), validate=True)
    except (OSError, ValueError):
        return None, ("讀不到manifest.sig（模組沒有簽章）", "請重新上傳云碩提供的完整模組套件")
    if not keys:
        return None, ("這一版核心還沒有內建云碩的模組公鑰", "請把主控台更新到有內建公鑰的版本")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    signer = None
    for kid, hexkey in keys.items():
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(hexkey)).verify(sig, raw)
            signer = kid
            break
        except (InvalidSignature, ValueError):
            continue
    if not signer:
        return None, ("模組的簽章驗不過：不是云碩簽的，或檔案被改過", "請向云碩重新取得模組套件，不要自行修改裡面的檔案")
    files = man["files"]
    if ENTRY not in files:
        return None, (f"模組缺少{ENTRY}", "請向云碩重新取得模組套件")
    for rel, want in files.items():
        if not isinstance(rel, str) or rel.startswith(("/", "\\")) or ".." in re.split(r"[\\/]", rel) or not re.fullmatch(r"[0-9a-f]{64}", str(want)):
            return None, (f"清單裡有不合法的項目：{str(rel)[:80]}", "請向云碩重新取得模組套件")
        p = os.path.join(d, *rel.split("/"))
        if not os.path.isfile(p):
            return None, (f"少了檔案：{rel}", "請重新上傳完整的模組套件")
        if _sha(p) != want:
            return None, (f"{rel} 跟清單的sha256不一樣（被改過）", "請重新上傳云碩提供的模組套件，不要自行修改裡面的檔案")
    listed = set(files) | set(RESERVED)
    for dp, dn, fs in os.walk(d):
        for f in dn + fs:
            rel = os.path.relpath(os.path.join(dp, f), d).replace(os.sep, "/")
            if os.path.isdir(os.path.join(dp, f)):
                if not any(x.startswith(rel + "/") for x in listed):
                    return None, (f"有清單外的資料夾：{rel}", "請刪掉它，或重新上傳完整的模組套件")
            elif rel not in listed:
                return None, (f"有清單外的檔案：{rel}", "請刪掉它，或重新上傳完整的模組套件")
    bad = _perm_problems(d)
    if bad:
        return None, ("模組資料夾別的帳號也能寫：" + "；".join(bad[:3]), "只留給系統管理員（Windows：SYSTEM與Administrators；Linux、macOS：root或主控台的帳號，權限不能讓群組或其他人寫）")
    api = man.get("core_api")
    ok = api == CORE_API or (isinstance(api, list) and CORE_API in api)
    if not ok:
        return None, (f"模組要的核心API是{api}，這一版核心是{CORE_API}", "請把主控台與模組更新到相容的版本")
    man["_signer"] = signer
    return man, None


def safe_extract(data, dest):
    """解開上傳的模組套件：不收連結、往上跳的路徑、絕對路徑；檔案數與大小有上限。"""
    import io
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("不是zip檔")
    infos = z.infolist()
    if len(infos) > MAX_FILES:
        raise ValueError("檔案數太多")
    if sum(i.file_size for i in infos) > MAX_UNPACKED:
        raise ValueError("解開後太大")
    names = [i.filename for i in infos]
    for i in infos:                                     # 先用原始路徑判斷，再決定要不要拿掉外層資料夾
        parts = re.split(r"[\\/]", i.filename)
        if i.filename.startswith(("/", "\\")) or ".." in parts or re.match(r"^[A-Za-z]:", i.filename):
            raise ValueError(f"壓縮檔裡有不安全的路徑：{i.filename[:80]}")
    # 套件可以多包一層資料夾（例xconsole-enterprise-1.0/）：拿掉
    tops = {n.split("/", 1)[0] for n in names if n}
    strip = len(tops) == 1 and all("/" in n or n.endswith("/") for n in names) and not any(n == next(iter(tops)) for n in names)
    for i in infos:
        n = i.filename
        if strip:
            n = n.split("/", 1)[1] if "/" in n else ""
        if not n or n.endswith("/"):
            continue
        parts = re.split(r"[\\/]", n)
        if n.startswith(("/", "\\")) or ".." in parts or re.match(r"^[A-Za-z]:", n):
            raise ValueError(f"壓縮檔裡有不安全的路徑：{n[:80]}")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"壓縮檔裡有連結檔：{n[:80]}")
        p = os.path.join(dest, *parts)
        os.makedirs(os.path.dirname(p), mode=0o755, exist_ok=True)
        with z.open(i) as src, open(p, "wb") as out:
            shutil.copyfileobj(src, out)
        if os.name != "nt":
            os.chmod(p, 0o644)
    if os.name != "nt":
        for dp, dn, _ in os.walk(dest):
            for x in dn:
                os.chmod(os.path.join(dp, x), 0o755)


# ---------------------------------------------------------------- 載入器
class Modules:
    """core：主控台交給載入器的東西——db、lock、seal(bytes)->str、unseal(str)->bytes、audit(action, target, ok, detail)、
    group_ids()->set、desktop（個人版不載入）。"""

    def __init__(self, core, root, keys=None):
        self.core, self.root = core, root
        self.keys = TRUSTED_KEYS if keys is None else keys
        self.lock = threading.RLock()
        self.routes, self.static_dir, self.namespaces = [], None, set()
        self.state = {"loaded": False, "reason": None, "fix": None}
        with core.lock:
            core.db.execute("CREATE TABLE IF NOT EXISTS ent_rows(ns TEXT, tbl TEXT, id TEXT, group_id TEXT, data TEXT, updated REAL,"
                            " PRIMARY KEY(ns, tbl, id))")

    @property
    def dir(self):
        return os.path.join(self.root, "enterprise")

    def status(self):
        with self.lock:
            return {**self.state, "core_api": CORE_API, "dir": self.dir, "keys": len(self.keys)}

    def _fail(self, reason, fix):
        self.state = {"loaded": False, "reason": reason, "fix": fix}
        return False

    def load(self):
        """載入或重新載入。失敗時以開源核心繼續（不留任何模組路由），原因寫在狀態裡。"""
        with self.lock:
            self.routes, self.static_dir, self.namespaces = [], None, set()
            if self.core.desktop:
                return self._fail("個人版（桌面版）不載入企業版模組", None)
            if not os.path.isdir(self.dir):
                self.state = {"loaded": False, "reason": None, "fix": None}      # 沒有模組：一般版，不是錯誤
                return False
            man, err = verify(self.dir, self.keys)
            if err:
                return self._fail(*err)
            for k in [k for k in sys.modules if k == PKG or k.startswith(PKG + ".")]:
                del sys.modules[k]
            api = API(self, self.dir)
            old = sys.dont_write_bytecode
            sys.dont_write_bytecode = True                       # 不寫__pycache__：下次啟動時它會是清單外的檔
            try:
                spec = importlib.util.spec_from_file_location(PKG, os.path.join(self.dir, ENTRY), submodule_search_locations=[self.dir])
                mod = importlib.util.module_from_spec(spec)
                sys.modules[PKG] = mod
                spec.loader.exec_module(mod)
                if not callable(getattr(mod, "register", None)):
                    raise ModuleError("模組沒有register(api)")
                mod.register(api)
            except Exception as ex:  # noqa: BLE001
                print("企業版模組載入失敗：\n" + traceback.format_exc(), flush=True)
                for k in [k for k in sys.modules if k == PKG or k.startswith(PKG + ".")]:
                    del sys.modules[k]
                return self._fail(f"模組載入時出錯：{str(ex)[:200]}", "請向云碩回報，並附上主控台的記錄")
            finally:
                sys.dont_write_bytecode = old
            api._frozen = True
            self.routes, self.static_dir, self.namespaces = api.routes, api.static_dir, api.namespaces
            self.state = {"loaded": True, "reason": None, "fix": None, "name": str(man.get("name") or "")[:80],
                          "version": str(man.get("version") or "")[:40], "signer": man["_signer"], "files": len(man["files"]),
                          "routes": len(self.routes), "static": bool(self.static_dir), "at": int(time.time())}
            return True

    def install(self, data):
        """網頁上傳的模組套件：解到暫存資料夾、驗過才換上並熱載入；換上後載入失敗就換回原本那一份。回 (碼, 結果)。"""
        if not data or len(data) > MAX_ZIP:
            return 413, {"error": f"模組套件要在{MAX_ZIP >> 20} MB以內"}
        with self.lock:
            stage, prev = self.dir + ".staging", self.dir + ".prev"
            shutil.rmtree(stage, ignore_errors=True)
            os.makedirs(stage, mode=0o755)
            try:
                safe_extract(data, stage)
            except ValueError as ex:
                shutil.rmtree(stage, ignore_errors=True)
                return 400, {"error": str(ex)}
            man, err = verify(stage, self.keys)
            if err:
                shutil.rmtree(stage, ignore_errors=True)
                return 400, {"error": err[0], "fix": err[1]}
            shutil.rmtree(prev, ignore_errors=True)
            had = os.path.isdir(self.dir)
            if had:
                os.replace(self.dir, prev)
            os.replace(stage, self.dir)
            if self.load():
                shutil.rmtree(prev, ignore_errors=True)
                return 200, {"ok": True, **self.status()}
            st = self.status()
            shutil.rmtree(self.dir, ignore_errors=True)
            if had:
                os.replace(prev, self.dir)
            self.load()
            return 400, {"error": st["reason"], "fix": st["fix"], "kept_previous": had}

    def remove(self):
        with self.lock:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.routes, self.static_dir, self.namespaces = [], None, set()
            self.state = {"loaded": False, "reason": None, "fix": None}
            return True

    # ---- 路由
    def match(self, method, path):
        for r in self.routes:
            if r.method == method:
                m = r.rx.fullmatch(path)
                if m:
                    return r, m
        return None

    def group_of(self, table, rid):
        """scope.obj：那筆資料屬於哪個群組。回 (找到了嗎, group_id)。只查這個模組註冊過的namespace。"""
        with self.core.lock:
            for ns in sorted(self.namespaces):
                row = self.core.db.execute("SELECT group_id FROM ent_rows WHERE ns=? AND tbl=? AND id=?", (ns, table, rid)).fetchone()
                if row:
                    return True, row[0]
        return False, None

    def resolve(self, r, m, body, scope, is_admin):
        """核心在進handler之前檢查群組範圍。回 (group_id, None) 或 (None, (碼, 回應))。
        scope：目前帳號能管的群組（None＝不限）。範圍外一律404，跟查別人的東西一樣，不洩漏有沒有。"""
        k = r.scope.kind
        if k == "global":
            if is_admin and scope is None:
                return None, None
            return None, (403, {"error": "這個功能要能管全部群組的管理員", "admin_only": True})
        if k == "path":
            gid = m.group(r.scope.a)
        elif k == "body":
            gid = (body or {}).get(r.scope.a) if isinstance(body, dict) else None
            if not isinstance(gid, str) or not gid:
                return None, (400, {"error": f"本文要帶{r.scope.a}（群組編號）"})
        else:
            found, gid = self.group_of(r.scope.a, m.group(r.scope.b))
            if not found:
                return None, (404, {"error": "找不到"})
            if gid is None:                                       # 全域資料：只有不限群組的帳號碰得到
                return (None, None) if scope is None else (None, (404, {"error": "找不到"}))
        if gid not in self.core.group_ids() or (scope is not None and gid not in scope):
            return None, (404, {"error": "沒有這個群組"})
        return gid, None

    def call(self, r, req, scope):
        """在目前帳號的範圍裡跑handler。handler丟例外：回500，堆疊只寫記錄不回給前端。"""
        tok = CURRENT.set(None if scope is None else frozenset(scope))
        try:
            out = r.handler(req)
            code, obj = out if isinstance(out, tuple) else (200, out)
            json.dumps(obj)
            return int(code), obj
        except PermissionError:
            return 404, {"error": "沒有這個群組"}
        except Exception:  # noqa: BLE001
            print(f"企業版模組 {r.method} {r.pattern} 出錯：\n" + traceback.format_exc(), flush=True)
            return 500, {"error": "模組發生錯誤，已記錄"}
        finally:
            CURRENT.reset(tok)

    # ---- 資料表（Table用）
    def seal(self, data):
        return self.core.seal(json.dumps(data, ensure_ascii=False).encode())

    def unseal(self, blob):
        return json.loads(self.core.unseal(blob))

    def row(self, ns, tbl, rid):
        with self.core.lock:
            r = self.core.db.execute("SELECT group_id, data FROM ent_rows WHERE ns=? AND tbl=? AND id=?", (ns, tbl, rid)).fetchone()
        return (r[0], r[1]) if r else None

    def rows(self, ns, tbl):
        with self.core.lock:
            return self.core.db.execute("SELECT id, group_id, data FROM ent_rows WHERE ns=? AND tbl=? ORDER BY id", (ns, tbl)).fetchall()

    def write(self, ns, tbl, rid, gid, blob):
        if not str(blob).startswith("enc:v1:"):
            raise ModuleError("模組資料一定要加密才寫入")
        with self.core.lock:
            self.core.db.execute("INSERT OR REPLACE INTO ent_rows(ns, tbl, id, group_id, data, updated) VALUES(?,?,?,?,?,?)",
                                 (ns, tbl, rid, gid, blob, time.time()))

    def remove_row(self, ns, tbl, rid):
        with self.core.lock:
            return self.core.db.execute("DELETE FROM ent_rows WHERE ns=? AND tbl=? AND id=?", (ns, tbl, rid)).rowcount > 0

    def static_file(self, rel):
        """/ent/static/<rel>：只給模組宣告的前端資料夾裡的檔。"""
        if not self.static_dir:
            return None
        full = os.path.realpath(os.path.join(self.static_dir, *re.split(r"[\\/]", rel)))
        if not full.startswith(self.static_dir + os.sep) or not os.path.isfile(full):
            return None
        return full
