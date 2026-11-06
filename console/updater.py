"""線上更新：主控台自己與各機的代理。

來源是 GitHub 上 xConsole 的正式版（releases）：每一版附一個 xconsole-<版本>.tar.gz，GitHub 會替附件算 sha256（digest）。
- 檢查：問 GitHub API 最新一版的版本、說明、附件與 sha256。
- 更新主控台：下載 → 整個檔核對 GitHub 公布的 sha256（不能連外的機器改成手動上傳，核對管理者貼上的 sha256）
  → 解到暫存資料夾（擋 .. 與絕對路徑、檔數與大小上限）→ 每個 .py 先編譯一次確認沒有語法錯 → 備份目前的檔
  → 換上（requirements.txt 變了就先用同一個 Python 裝相依套件，裝不起來就整組還原）→ 結束行程，由服務管理員重新帶起。
- 退回上一版：把最近一次的備份換回去（相依清單不同就裝回舊的）再重啟；更新進行中不准退回。備份只留最近 10 份。
- 代理：主控台把新版的 xconsole_agent.py 送給各機（代理核對 sha256、編譯過才換，舊的留備份），代理結束後由服務管理員重新帶起。
"""
import hashlib
import io
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request

REPO = os.environ.get("XCONSOLE_UPDATE_REPO", "guessleej/xConsole")
GH_API = os.environ.get("XCONSOLE_GH_API", "https://api.github.com")
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PKG = 64 * 1024 * 1024
MAX_INSTALLER = 400 * 1024 * 1024
MAX_FILES = 2000
MAX_UNPACKED = 256 * 1024 * 1024
KEEP_BACKUPS = 10
BUSY = ("downloading", "checking", "installing", "applying", "restarting", "rolling_back")   # installer_ready 不算：裝到一半取消可以再按一次
VER_RX = re.compile(r"v?\d{1,4}(\.\d{1,4}){1,3}([-.][\w.]{1,20})?")
STATE = {"job": None}
LOCK = threading.Lock()


def current_version():
    for p in (os.path.join(HERE, "VERSION"), os.path.join(os.path.dirname(HERE), "VERSION")):
        try:
            v = open(p, encoding="utf-8").read().strip()
            if VER_RX.fullmatch(v):
                return v.lstrip("v")
        except OSError:
            continue
    return "未知"


def _ver_key(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def check(timeout=20):
    """問 GitHub 最新一版。回 (碼, 結果)。"""
    req = urllib.request.Request(f"{GH_API}/repos/{REPO}/releases/latest",
                                 headers={"User-Agent": "xconsole-updater", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            rel = json.load(r)
    except Exception as ex:  # noqa: BLE001
        return 502, {"error": f"連不上 GitHub（{str(ex)[:80]}）。這台不能連外的話，請在別台下載安裝包，用「手動上傳更新包」", "offline": True,
                     "current": current_version()}
    tag = str(rel.get("tag_name") or "")
    asset = next((a for a in rel.get("assets") or [] if re.fullmatch(r"xconsole-.+\.tar\.gz", a.get("name") or "")), None)
    inst = next((a for a in rel.get("assets") or [] if re.fullmatch(installer_pattern(), a.get("name") or "")), None)
    digest = str((asset or {}).get("digest") or "")
    cur = current_version()
    return 200, {"current": cur, "latest": tag.lstrip("v"), "newer": _ver_key(tag) > _ver_key(cur) if cur != "未知" else True,
                 "notes": str(rel.get("body") or "")[:20000], "published": rel.get("published_at"), "url": rel.get("html_url"),
                 "asset": {"name": asset["name"], "size": asset.get("size"), "url": asset.get("browser_download_url"),
                           "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None} if asset else None,
                 "installer": _asset_info(inst)}


def _asset_info(a):
    if not a:
        return None
    dg = str(a.get("digest") or "")
    return {"name": a["name"], "size": a.get("size"), "url": a.get("browser_download_url"),
            "sha256": dg.split(":", 1)[1] if dg.startswith("sha256:") else None}


def installer_pattern():
    """這台該用哪一個桌面版安裝檔（GitHub 正式版的附件）。"""
    import platform
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if os.name == "nt":
        return r"xConsole_[\d.]+_(x64|arm64)-setup\.exe" if not arm else r"xConsole_[\d.]+_arm64-setup\.exe"
    if sys.platform == "darwin":
        return r"xConsole_[\d.]+_" + ("aarch64" if arm else "x64") + r"\.dmg"
    return r"xConsole_[\d.]+_" + ("aarch64" if arm else "amd64") + r"\.AppImage"


def start_installer(state_dir, url, sha256, version, opener):
    """桌面版的更新：下載新版安裝檔、核對 GitHub 公布的 sha256，再打開它（Windows 的安裝程式會請使用者關掉 xConsole 再換上）。
    opener(路徑)＝用系統打開檔案。"""
    sha256 = str(sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return 400, {"error": "這一版的安裝檔沒有可以核對的 sha256，不下載"}
    with LOCK:
        if STATE.get("job") and STATE["job"].get("state") in BUSY:
            return 409, {"error": "已經在更新了"}
        STATE["job"] = {"state": "downloading", "at": time.time(), "version": version}

    def run():
        try:
            d = os.path.join(state_dir, "updates")
            os.makedirs(d, exist_ok=True)
            name = os.path.basename(urllib.parse.urlparse(url).path)
            if not re.fullmatch(installer_pattern(), name):
                raise ValueError("安裝檔名稱不對，不下載")
            data = _download(url, sha256, max_bytes=MAX_INSTALLER)
            path = os.path.join(d, name)
            # 建立的當下就只有自己能讀寫執行（不是先用預設權限建好再 chmod，中間會有一段別的帳號讀得到）
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700), "wb") as fh:
                fh.write(data)
            os.chmod(path, 0o700)                     # 檔案原本就在時 os.open 不會改權限，再設一次
            _job("installer_ready", message="安裝檔已下載並核對過，請照安裝程式的畫面完成更新", path=path)
            opener(path)
        except Exception as ex:  # noqa: BLE001
            _job("failed", error=str(ex)[:500])
    threading.Thread(target=run, daemon=True).start()
    return 200, {"ok": True}


def _job(state, **kw):
    with LOCK:
        STATE["job"] = {**(STATE.get("job") or {}), "state": state, "at": time.time(), **kw}


def status():
    with LOCK:
        return dict(STATE.get("job") or {}), current_version()


def _download(url, want_sha, timeout=600, max_bytes=None):
    req = urllib.request.Request(url, headers={"User-Agent": "xconsole-updater"})
    h, buf = hashlib.sha256(), io.BytesIO()
    lim = max_bytes or MAX_PKG
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            if buf.tell() + len(chunk) > lim:
                raise ValueError(f"更新包太大（上限 {lim >> 20} MB）")
            h.update(chunk)
            buf.write(chunk)
            _job("downloading", got=buf.tell())
    if h.hexdigest() != want_sha:
        raise ValueError(f"下載的檔 sha256 對不上 GitHub 公布的（{h.hexdigest()[:16]}… ≠ {want_sha[:16]}…），不更新")
    return buf.getvalue()


def _extract(data, dest):
    """解開更新包：只收一般檔案、不准 .. 與絕對路徑、不准連結；要有主控台與代理的主程式。回 (根目錄, 檔案清單)。"""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = tf.getmembers()
        if len(members) > MAX_FILES:
            raise ValueError("更新包的檔案太多")
        if sum(m.size for m in members if m.isfile()) > MAX_UNPACKED:
            raise ValueError("更新包解開後太大（上限 256 MB）")   # tarfile 照標頭的大小讀，總和擋住就不會解出超量的檔
        for m in members:
            name = m.name.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/") or re.match(r"^[A-Za-z]:", name):
                raise ValueError(f"更新包裡有不安全的路徑：{name[:80]}")
            if not (m.isfile() or m.isdir()):
                raise ValueError(f"更新包裡有連結或特殊檔：{name[:80]}")
        for m in members:
            if m.isfile():
                out = os.path.join(dest, m.name)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with tf.extractfile(m) as src, open(out, "wb") as fh:
                    shutil.copyfileobj(src, fh)
    tops = [d for d in os.listdir(dest) if os.path.isdir(os.path.join(dest, d))]
    root = os.path.join(dest, tops[0]) if len(tops) == 1 and not os.path.exists(os.path.join(dest, "console")) else dest
    for need in ("console/xconsole_server.py", "console/static/index.html", "agent/xconsole_agent.py"):
        if not os.path.isfile(os.path.join(root, need)):
            raise ValueError(f"這不是 xConsole 的更新包（少了 {need}）")
    return root


def _compile_all(root):
    for d in ("console", "agent"):
        for f in os.listdir(os.path.join(root, d)):
            if f.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(root, d, f), doraise=True, cfile=os.path.join(tempfile.gettempdir(), "xc-upd.pyc"))
                except py_compile.PyCompileError as ex:
                    raise ValueError(f"新版的 {d}/{f} 編譯不過，不更新：{str(ex)[:200]}") from ex


def _plan(root):
    """(來源, 目的) 清單：主控台的 .py／.json、static 整個、隨附給別台的代理、相依清單、版本檔。"""
    pairs = []
    for f in sorted(os.listdir(os.path.join(root, "console"))):
        p = os.path.join(root, "console", f)
        if os.path.isfile(p) and re.fullmatch(r"[\w.-]+\.(py|json)", f) and not f.startswith("test_"):
            pairs.append((p, os.path.join(HERE, f)))
    for f in sorted(os.listdir(os.path.join(root, "console", "static"))):
        p = os.path.join(root, "console", "static", f)
        if os.path.isfile(p) and re.fullmatch(r"[\w.-]+", f):
            pairs.append((p, os.path.join(HERE, "static", f)))
    pairs.append((os.path.join(root, "agent", "xconsole_agent.py"), os.path.join(HERE, "agent", "xconsole_agent.py")))
    for f in ("requirements.txt", "VERSION"):
        if os.path.isfile(os.path.join(root, f)):
            pairs.append((os.path.join(root, f), os.path.join(HERE, f)))
    return pairs


def _backup_dir(state_dir):
    return os.path.join(state_dir, "update-backups")


def _apply(root, state_dir, restart):
    pairs = _plan(root)
    bdir = os.path.join(_backup_dir(state_dir), time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir, exist_ok=True)
    manifest = []
    for _, dst in pairs:
        rel = os.path.relpath(dst, HERE)
        if os.path.exists(dst):
            os.makedirs(os.path.dirname(os.path.join(bdir, rel)), exist_ok=True)
            shutil.copy2(dst, os.path.join(bdir, rel))
            manifest.append({"path": rel, "existed": True})
        else:
            manifest.append({"path": rel, "existed": False})
    json.dump({"version": current_version(), "files": manifest, "at": time.time()}, open(os.path.join(bdir, "manifest.json"), "w"))
    old_req = open(os.path.join(HERE, "requirements.txt"), encoding="utf-8").read() if os.path.exists(os.path.join(HERE, "requirements.txt")) else ""
    new_req_p = os.path.join(root, "requirements.txt")
    new_req = open(new_req_p, encoding="utf-8").read() if os.path.exists(new_req_p) else old_req
    if new_req != old_req:
        _job("installing", message="相依套件有變，安裝中")
        ok, msg = _pip(new_req_p)
        if not ok:
            raise ValueError("新版的相依套件裝不起來（這台要能連 pypi.org）：" + msg)
    try:
        for src, dst in pairs:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".upd"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
    except OSError as ex:
        _restore(bdir)
        raise ValueError(f"換檔失敗，已還原：{ex}") from ex
    _prune(state_dir, keep=os.path.basename(bdir))
    return bdir


def _prune(state_dir, keep):
    """只留最近 KEEP_BACKUPS 份備份（畫面上也只列這些），更舊的刪掉，免得每次更新多一份、磁碟越吃越多。"""
    d = _backup_dir(state_dir)
    names = sorted((n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))), reverse=True)
    for n in names[KEEP_BACKUPS:]:
        if n != keep:
            shutil.rmtree(os.path.join(d, n), ignore_errors=True)


def _pip(req_path):
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", req_path],
                       capture_output=True, text=True, timeout=900)
    return r.returncode == 0, (r.stderr or r.stdout)[-300:]


def _restore(bdir):
    man = json.load(open(os.path.join(bdir, "manifest.json")))
    for it in man["files"]:
        dst = os.path.join(HERE, it["path"])
        if it["existed"]:
            shutil.copy2(os.path.join(bdir, it["path"]), dst)
        elif os.path.exists(dst):
            os.remove(dst)
    return man.get("version")


def _restart_soon(restart):
    def go():
        time.sleep(1.5)
        restart()
    threading.Thread(target=go, daemon=True).start()


def start_update(state_dir, restart, data=None, url=None, sha256=None, version=None):
    """背景更新。data＝手動上傳的位元組（要帶 sha256），url＝GitHub 附件網址（sha256 取自 GitHub API）。"""
    sha256 = str(sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return 400, {"error": "要有 64 碼的 sha256 才能核對更新包（GitHub 的版本頁上每個附件都有）"}
    with LOCK:
        if STATE.get("job") and STATE["job"].get("state") in BUSY:
            return 409, {"error": "已經在更新或退回了"}
        STATE["job"] = {"state": "checking", "at": time.time(), "version": version}

    def run():
        tmp = tempfile.mkdtemp(prefix="xc-update-")
        try:
            pkg = data
            if pkg is None:
                pkg = _download(url, sha256)
            elif hashlib.sha256(pkg).hexdigest() != sha256:
                raise ValueError(f"上傳的檔 sha256 對不上（算出來是 {hashlib.sha256(pkg).hexdigest()}），不更新")
            _job("checking", message="檢查更新包")
            root = _extract(pkg, tmp)
            _compile_all(root)
            _job("applying", message="備份並換上新版")
            bdir = _apply(root, state_dir, restart)
            _job("restarting", message="重新啟動主控台", backup=os.path.basename(bdir), new_version=current_version())
            _restart_soon(restart)
        except Exception as ex:  # noqa: BLE001
            _job("failed", error=str(ex)[:500])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    threading.Thread(target=run, daemon=True).start()
    return 200, {"ok": True}


def backups(state_dir):
    d = _backup_dir(state_dir)
    out = []
    for name in sorted(os.listdir(d), reverse=True) if os.path.isdir(d) else []:
        try:
            man = json.load(open(os.path.join(d, name, "manifest.json")))
        except (OSError, ValueError):
            continue
        out.append({"name": name, "version": man.get("version"), "at": man.get("at")})
    return out[:10]


def rollback(state_dir, restart):
    """退回最近一次的備份。跟更新共用同一把鎖：更新進行中不准退回；相依清單不同就把舊的裝回去再重啟。"""
    bs = backups(state_dir)
    if not bs:
        return 404, {"error": "沒有可以退回的備份"}
    with LOCK:
        if STATE.get("job") and STATE["job"].get("state") in BUSY:
            return 409, {"error": "更新正在進行，等它做完再退回"}
        STATE["job"] = {"state": "rolling_back", "at": time.time(), "message": f"退回 {bs[0]['version']}"}
    bdir = os.path.join(_backup_dir(state_dir), bs[0]["name"])
    req = os.path.join(HERE, "requirements.txt")
    before = open(req, encoding="utf-8").read() if os.path.exists(req) else ""

    def run():
        try:
            ver = _restore(bdir)
            after = open(req, encoding="utf-8").read() if os.path.exists(req) else ""
            if after and after != before:
                _job("rolling_back", message="相依套件換回舊版中")
                ok, msg = _pip(req)
                if not ok:
                    raise ValueError("舊版的相依套件裝不回去（這台要能連 pypi.org）：" + msg)
            _job("restarting", message=f"退回 {ver}，重新啟動主控台", new_version=ver)
            _restart_soon(restart)
        except Exception as ex:  # noqa: BLE001
            _job("failed", error=f"退回失敗：{str(ex)[:500]}")
    threading.Thread(target=run, daemon=True).start()
    return 200, {"ok": True, "version": bs[0]["version"]}


def bundled_agent():
    """隨主控台附的代理程式（要推給各機的那一份）：(內容, sha256)。"""
    p = os.path.join(HERE, "agent", "xconsole_agent.py")
    if not os.path.isfile(p):
        return None, None
    data = open(p, "rb").read()
    return data, hashlib.sha256(data).hexdigest()
