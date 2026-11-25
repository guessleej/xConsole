"""內部鏡像（清單 #13）：主控台先從 Hugging Face 取得模型檔、核對 sha256 存起來，群組裡的機器一律跟主控台拿。
適用於企業代理伺服器擋外網、學校外網頻寬小的情況。只提供代理下載模型用到的那幾支 Hugging Face API：
檔案清單（tree）、模型資訊、下載檔案（resolve，支援續傳）。檔案清單與模型資訊照轉 Hugging Face 的回應並存一份，
連不到 Hugging Face 時回存的那份；還沒存的檔排進佇列下載，下載期間回「準備中」，機器等一下再來拿。
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

REPO_RX = re.compile(r"[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}")
ENGINE_RX = re.compile(r"[A-Za-z0-9][\w.+-]{0,150}\.tar\.gz")
PART_RX = re.compile(r"[\w.+-]{1,200}")         # 檔名逐段比：一段一段比是線性的，不會被刻意的長字串拖慢
CHUNK = 8 << 20


class Pending(Exception):
    pass


def _safe(repo, path=None):
    parts = (path or "").split("/")
    if not REPO_RX.fullmatch(repo or "") or (path is not None and (len(path) > 400 or len(parts) > 4 or ".." in parts
                                                                    or not all(PART_RX.fullmatch(x) for x in parts))):
        raise ValueError("模型名稱或檔名不合法")


class Mirror:
    def __init__(self, root, upstream="https://huggingface.co", token=lambda: "", opener=None):
        self.root, self.upstream, self.token = root, upstream.rstrip("/"), token
        self.opener = opener or urllib.request.build_opener()
        self.lock = threading.Lock()
        self.queue, self.jobs = [], {}            # 佇列：(repo, path)；進度：「repo/path」→ {state, done, total, error}
        self.wake = threading.Event()
        os.makedirs(root, mode=0o700, exist_ok=True)

    # ---- 存放位置
    def _dir(self, repo):
        return os.path.join(self.root, *repo.split("/"))

    def _file(self, repo, path):
        full = os.path.realpath(os.path.join(self._dir(repo), "files", *path.split("/")))
        if not full.startswith(os.path.realpath(self.root) + os.sep):
            raise ValueError("檔名不合法")
        return full

    def _index(self, repo):
        try:
            with open(os.path.join(self._dir(repo), "index.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_index(self, repo, idx):
        p = os.path.join(self._dir(repo), "index.json")
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(idx, fh, ensure_ascii=False)
        os.replace(p + ".tmp", p)

    # ---- 上游
    def _get(self, url, rng=None, timeout=30):
        h = {"User-Agent": "xconsole-mirror"}
        if self.token():
            h["Authorization"] = f"Bearer {self.token()}"
        if rng:
            h["Range"] = f"bytes={rng}-"
        return self.opener.open(urllib.request.Request(url, headers=h), timeout=timeout)

    def _cached_json(self, repo, name, url):
        """照轉上游的 JSON 並存一份；上游連不到時回存的那份。回 (狀態碼, 內容)。"""
        p = os.path.join(self._dir(repo), name)
        try:
            with self._get(url) as r:
                raw = r.read(16 << 20)
            json.loads(raw)
            os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
            with open(p + ".tmp", "wb") as fh:
                fh.write(raw)
            os.replace(p + ".tmp", p)
            return 200, raw
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                return exc.code, json.dumps({"error": f"Hugging Face 回 {exc.code}"}).encode()
        except (OSError, ValueError):
            pass
        try:
            with open(p, "rb") as fh:
                return 200, fh.read()
        except OSError:
            return 502, json.dumps({"error": "內部鏡像連不到 Hugging Face，也還沒有這個模型的資料"}, ensure_ascii=False).encode()

    def tree(self, repo):
        _safe(repo)
        return self._cached_json(repo, "tree.json", f"{self.upstream}/api/models/{urllib.parse.quote(repo, safe='/')}/tree/main?recursive=true")

    def info(self, repo):
        _safe(repo)
        return self._cached_json(repo, "info.json", f"{self.upstream}/api/models/{urllib.parse.quote(repo, safe='/')}")

    def _expect(self, repo, path):
        """Hugging Face 公布的 (sha256, 大小)；只有 LFS 的大檔有 sha256。"""
        code, raw = self.tree(repo)
        if code != 200:
            raise OSError(json.loads(raw).get("error") or f"讀不到 {repo} 的檔案清單")
        item = next((x for x in json.loads(raw) if isinstance(x, dict) and x.get("path") == path), None)
        if not item:
            raise OSError(f"{repo} 沒有 {path}")
        lfs = item.get("lfs") or {}
        sha = lfs.get("oid") if re.fullmatch(r"[0-9a-f]{64}", str(lfs.get("oid") or "")) else None
        return sha, int(lfs.get("size") or item.get("size") or 0)

    # ---- 機器來拿
    def file(self, repo, path):
        """存好的回完整路徑；還沒存的排進佇列並丟 Pending（機器等一下再來）。"""
        _safe(repo, path)
        if path in self._index(repo):
            full = self._file(repo, path)
            if os.path.exists(full):
                return full
        self.enqueue(repo, path)
        j = self.jobs.get(f"{repo}/{path}") or {}
        if j.get("state") == "failed":
            raise Pending(f"內部鏡像下載 {path} 失敗（{j.get('error')}），已重新排隊")
        pct = int(100 * j["done"] / j["total"]) if j.get("total") else 0
        raise Pending(f"內部鏡像正在從 Hugging Face 下載 {path}（{pct}%），稍後再試")

    def enqueue(self, repo, path):
        _safe(repo, path)
        key = f"{repo}/{path}"
        with self.lock:
            if (repo, path) not in self.queue and (self.jobs.get(key) or {}).get("state") != "running":
                self.queue.append((repo, path))
                self.jobs.setdefault(key, {"state": "queued", "done": 0, "total": 0})
                if self.jobs[key].get("state") == "failed":
                    self.jobs[key]["state"] = "queued"
        self.wake.set()

    def prefetch(self, repo, quant=""):
        """管理者在網頁上先下載：這個 repo 裡檔名含這個量化的 GGUF（不給量化就整個 repo 的 GGUF），連同看圖用的 mmproj。"""
        code, raw = self.tree(repo)
        if code != 200:
            raise OSError(json.loads(raw).get("error") or "讀不到檔案清單")
        q = quant.lower()
        files = [x["path"] for x in json.loads(raw) if isinstance(x, dict) and str(x.get("path", "")).endswith(".gguf")
                 and (not q or q in x["path"].lower() or "mmproj" in x["path"].lower())]
        for p in files:
            self.enqueue(repo, p)
        return files

    def worker(self):
        while True:
            self.wake.wait(30)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.queue:
                        break
                    repo, path = self.queue.pop(0)
                self._fetch(repo, path)

    def _fetch(self, repo, path):
        key = f"{repo}/{path}"
        j = self.jobs.setdefault(key, {})
        j.update(state="running", error=None, started=int(time.time()))
        try:
            sha, size = self._expect(repo, path)
            full = self._file(repo, path)
            os.makedirs(os.path.dirname(full), mode=0o700, exist_ok=True)
            part = full + ".part"
            have = os.path.getsize(part) if os.path.exists(part) else 0
            j.update(total=size, done=have)
            url = f"{self.upstream}/{urllib.parse.quote(repo, safe='/')}/resolve/main/{urllib.parse.quote(path, safe='/')}"
            with self._get(url, rng=have or None, timeout=60) as r, open(part, "ab" if have and r.status == 206 else "wb") as fh:
                if r.status != 206:
                    j["done"] = 0
                while chunk := r.read(CHUNK):
                    fh.write(chunk)
                    j["done"] += len(chunk)
            if size and os.path.getsize(part) != size:
                raise OSError(f"大小不對（{os.path.getsize(part)}／{size}），下次會接著下載")
            got = None
            if sha:
                h = hashlib.sha256()
                with open(part, "rb") as fh:
                    while b := fh.read(CHUNK):
                        h.update(b)
                got = h.hexdigest()
                if got != sha:
                    os.remove(part)
                    raise OSError("SHA-256 跟 Hugging Face 公布的不同，已刪除，會重新下載")
            os.replace(part, full)
            with self.lock:
                idx = self._index(repo)
                idx[path] = {"sha256": got, "size": os.path.getsize(full), "at": int(time.time())}
                self._save_index(repo, idx)
            j.update(state="done", done=os.path.getsize(full))
        except Exception as exc:  # noqa: BLE001
            j.update(state="failed", error=str(exc)[:200])

    # ---- 管理頁
    def status(self):
        repos = []
        for owner in sorted(os.listdir(self.root)) if os.path.isdir(self.root) else []:
            for name in sorted(os.listdir(os.path.join(self.root, owner))) if os.path.isdir(os.path.join(self.root, owner)) else []:
                repo = f"{owner}/{name}"
                if not REPO_RX.fullmatch(repo):
                    continue
                idx = self._index(repo)
                repos.append({"repo": repo, "files": [{"path": p, **v} for p, v in sorted(idx.items())],
                              "size": sum(v.get("size") or 0 for v in idx.values())})
        jobs = [{"key": k, **{x: v.get(x) for x in ("state", "done", "total", "error")}}
                for k, v in self.jobs.items() if v.get("state") in ("queued", "running", "failed")]
        return {"repos": repos, "jobs": jobs}

    # ---- 推理引擎包（清單 #15）：同機型挑一台編好，主控台收進來給同機型的其他台
    def _edir(self):
        return os.path.join(self.root, "_engines")

    def engines(self):
        try:
            with open(os.path.join(self._edir(), "index.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_engines(self, idx):
        os.makedirs(self._edir(), mode=0o700, exist_ok=True)
        p = os.path.join(self._edir(), "index.json")
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(idx, fh, ensure_ascii=False)
        os.replace(p + ".tmp", p)

    def engine_for(self, platform):
        rows = [dict(v, name=k) for k, v in self.engines().items() if v.get("platform") == platform]
        return max(rows, key=lambda r: r.get("at", 0)) if rows else None

    def engine_path(self, name):
        if not ENGINE_RX.fullmatch(name or "") or name not in self.engines():
            return None
        full = os.path.join(self._edir(), name)
        return full if os.path.exists(full) else None

    def engine_put(self, meta, src):
        """收一包：sha256 對得上才收，同平台舊的那包換掉。"""
        name = str(meta.get("name") or "")
        if not ENGINE_RX.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", str(meta.get("sha256") or "")):
            raise ValueError("推理引擎包的名稱或 sha256 不對")
        h = hashlib.sha256()
        with open(src, "rb") as fh:
            while b := fh.read(CHUNK):
                h.update(b)
        if h.hexdigest() != meta["sha256"]:
            os.remove(src)
            raise OSError("推理引擎包的 SHA-256 跟那台回報的不同，不收")
        os.makedirs(self._edir(), mode=0o700, exist_ok=True)
        with self.lock:
            idx = self.engines()
            for old in [k for k, v in idx.items() if v.get("platform") == meta.get("platform") and k != name]:
                idx.pop(old)
                try:
                    os.remove(os.path.join(self._edir(), old))
                except OSError:
                    pass
            os.replace(src, os.path.join(self._edir(), name))
            idx[name] = {k: meta.get(k) for k in ("platform", "tag", "sha256", "size", "from")} | {"at": int(time.time())}
            self._save_engines(idx)

    def engine_delete(self, name):
        if not ENGINE_RX.fullmatch(name or ""):
            raise ValueError("推理引擎包名稱不合法")
        with self.lock:
            idx = self.engines()
            idx.pop(name, None)
            self._save_engines(idx)
            try:
                os.remove(os.path.join(self._edir(), name))
            except OSError:
                pass

    def delete(self, repo):
        _safe(repo)
        with self.lock:
            if any(r == repo for r, _ in self.queue) or any(k.startswith(repo + "/") and v.get("state") == "running"
                                                             for k, v in self.jobs.items()):
                raise ValueError("這個模型還在下載，等下載完再刪")
            shutil.rmtree(self._dir(repo), ignore_errors=True)
            for k in [k for k in self.jobs if k.startswith(repo + "/")]:
                self.jobs.pop(k, None)
