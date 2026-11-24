"""Windows工作站執行環境（xconsole-runtime-<版本>-win-x64.zip）：內附Python與相依套件、Visual C++執行階段。

「加一台機器」的Windows腳本從主控台拿這一包，那台不必先裝Python、不必連pypi.org，也不必連GitHub。
主控台這一包的來源：
- 從GitHub正式版下載：只拿跟主控台同一版的那個附件，核對GitHub公布的sha256
- 管理者在網頁上傳（主控台不能連外時）：檢查結構（一定要有python.exe、pythonw.exe與執行階段）、路徑安全、大小上限

腳本裡寫死這一包的sha256，那台下載後先核對才解開。
"""
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
import zipfile

import updater

NAME = "xconsole-runtime-win-x64.zip"
MAX_ZIP = 200 * 1024 * 1024
MAX_UNPACKED = 600 * 1024 * 1024
MAX_FILES = 20000
MUST = ("python/python.exe", "python/pythonw.exe", "runtime/msvcp140.dll", "runtime/vcruntime140.dll", "runtime/vcruntime140_1.dll",
        "runtime/vcomp140.dll")
_LOCK = threading.Lock()
_SAVE_LOCK = threading.Lock()
_JOB = {"state": None}


def _dir(state_dir):
    return os.path.join(state_dir, "winrt")


def path(state_dir):
    return os.path.join(_dir(state_dir), NAME)


def meta(state_dir):
    """備妥的那一包：{version, sha256, size, source, at}；沒有或檔案對不上就回None。"""
    try:
        m = json.load(open(os.path.join(_dir(state_dir), "winrt.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return None
    p = path(state_dir)
    if not os.path.isfile(p) or os.path.getsize(p) != m.get("size"):
        return None
    if m.get("for_version") != updater.current_version():
        return None                    # 主控台升級過：舊版的Python與相依可能對不上新版，要重新準備
    return m


def status(state_dir):
    with _LOCK:
        job = dict(_JOB)
    m = meta(state_dir)
    return {"ready": bool(m), **({k: m[k] for k in ("version", "sha256", "size", "source", "at")} if m else {}),
            "job": job if job.get("state") else None, "current": updater.current_version()}


def check(data):
    """檢查上傳或下載的這一包：是zip、路徑安全、有必要的檔、解開不會太大。回錯誤訊息或None。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return "不是zip檔"
    names, total = set(), 0
    infos = z.infolist()
    if len(infos) > MAX_FILES:
        return "檔案數太多"
    for i in infos:
        n = i.filename
        if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/") or re.match(r"^[A-Za-z]:", n):
            return f"壓縮檔裡有不安全的路徑：{n[:80]}"
        if not re.match(r"^(python|runtime)/", n):
            return f"壓縮檔裡有python／runtime以外的東西：{n[:80]}"
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            return f"壓縮檔裡有連結檔：{n[:80]}"
        total += i.file_size
        names.add(n)
    if total > MAX_UNPACKED:
        return "解開後太大"
    miss = [m for m in MUST if m not in names]
    if miss:
        return f"這不是xConsole的Windows執行環境包（少了 {', '.join(miss)}）"
    try:
        bad = z.testzip()                # 逐檔解壓核對CRC：傳輸中損壞的包在這裡就擋下，不會讓每台工作站解到一半才失敗
    except Exception:  # noqa: BLE001
        bad = "?"
    if bad:
        return f"壓縮檔內容損壞：{str(bad)[:80]}"
    return None


def _save(state_dir, data, version, source):
    err = check(data)
    if err:
        raise ValueError(err)
    d = _dir(state_dir)
    os.makedirs(d, mode=0o700, exist_ok=True)
    m = {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "source": source, "at": int(time.time()),
         "for_version": updater.current_version()}
    # 上傳與背景下載可能同時存：各寫各的暫存檔，換上包與說明檔這一步一次只讓一個做，兩個檔永遠描述同一包
    with _SAVE_LOCK:
        fd, tmp = tempfile.mkstemp(prefix=".winrt-", dir=d)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path(state_dir))
        fd, tmpm = tempfile.mkstemp(prefix=".winrt-", suffix=".json", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(m, fh)
        os.replace(tmpm, os.path.join(d, "winrt.json"))
    return m


def upload(state_dir, data):
    """管理者上傳。回 (碼, 結果)。"""
    if not data or len(data) > MAX_ZIP:
        return 413, {"error": f"執行環境包要在 {MAX_ZIP >> 20} MB以內"}
    try:
        m = _save(state_dir, data, "上傳", "上傳")
    except ValueError as ex:
        return 400, {"error": str(ex)}
    return 200, {"ok": True, **m}


def _release_asset(version, timeout=20):
    """GitHub正式版v<版本>裡的Windows執行環境包：(網址, sha256)。"""
    req = urllib.request.Request(f"{updater.GH_API}/repos/{updater.REPO}/releases/tags/v{version}",
                                 headers={"User-Agent": "xconsole-winrt", "Accept": "application/vnd.github+json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
        rel = json.load(r)
    want = f"xconsole-runtime-{version}-win-x64.zip"
    a = next((x for x in rel.get("assets") or [] if x.get("name") == want), None)
    if not a:
        raise ValueError(f"GitHub的v{version} 沒有 {want}（這一版還沒有Windows執行環境包）")
    dg = str(a.get("digest") or "")
    if not dg.startswith("sha256:"):
        raise ValueError(f"{want} 沒有GitHub公布的sha256，不下載")
    return a["browser_download_url"], dg.split(":", 1)[1]


def fetch(state_dir, quiet=False):
    """背景從GitHub下載跟主控台同一版的執行環境包。quiet＝啟動時自動試一次，失敗不留錯誤（畫面上有按鈕可以再試）。"""
    version = updater.current_version()
    with _LOCK:
        if _JOB.get("state") == "downloading":
            return 409, {"error": "已經在下載了"}
        _JOB.clear()
        _JOB.update(state="downloading", at=time.time(), version=version)

    def run():
        try:
            if not updater.VER_RX.fullmatch(version):
                raise ValueError("不知道主控台的版本，無法對應GitHub正式版；請改用上傳")
            url, sha = _release_asset(version)
            req = urllib.request.Request(url, headers={"User-Agent": "xconsole-winrt"})
            h, buf = hashlib.sha256(), io.BytesIO()
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=600) as r:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    if buf.tell() + len(b) > MAX_ZIP:
                        raise ValueError("執行環境包太大")
                    h.update(b)
                    buf.write(b)
                    with _LOCK:
                        _JOB["got"] = buf.tell()
            if h.hexdigest() != sha:
                raise ValueError("下載的檔sha256跟GitHub公布的不一樣，已丟掉")
            _save(state_dir, buf.getvalue(), version, "GitHub")
            with _LOCK:
                _JOB.clear()
        except Exception as ex:  # noqa: BLE001
            with _LOCK:
                _JOB.clear()
                if not quiet:
                    _JOB.update(state="failed", error=str(ex)[:300], at=time.time())
    threading.Thread(target=run, daemon=True).start()
    return 200, {"ok": True}
