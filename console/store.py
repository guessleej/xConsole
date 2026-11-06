"""xConsole 的儲存層：稽核紀錄與定時取樣（SQLite，標準函式庫）。

稽核：誰在什麼時候、從哪裡、做了什麼、結果如何。寫入前一律經過 redact()——
審計最常見的洞就是把整包 request body 寫進去，密碼與私鑰跟著落地。
取樣：每 5 分鐘一筆各機狀態，服務使用報告的數字全部從這裡算，不靠手抄。
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

SENSITIVE = ("password", "passwd", "pw", "secret", "token", "key", "pfx", "p12", "pem", "files",
             "cookie", "authorization", "csr_key", "key_enc", "private")


def redact(obj, depth=0):
    """集中遮罩：所有寫稽核與記錄的地方都經過這裡。鍵名像機密的一律換掉，長字串截斷。"""
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(s in lk for s in SENSITIVE) and lk not in ("keytype", "key_type", "fingerprint256"):
                out[k] = "[已遮罩]"
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, depth + 1) for v in obj[:50]]
    if isinstance(obj, str):
        if "PRIVATE KEY" in obj or obj.startswith("enc:v1:"):
            return "[已遮罩]"
        return obj if len(obj) <= 300 else obj[:300] + "…"
    return obj


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        new = not os.path.exists(path)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if new:
            os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, ts REAL, user TEXT, ip TEXT,
          action TEXT, target TEXT, ok INTEGER, detail TEXT);
        CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
        CREATE TABLE IF NOT EXISTS samples(
          id INTEGER PRIMARY KEY, ts REAL, node TEXT, online INTEGER, mm TEXT,
          vram_used REAL, vram_total REAL, ram_free REAL, ram_total REAL, ram_floor REAL,
          gpu_power REAL, gpu_use REAL, busy INTEGER, slots INTEGER,
          states TEXT, counters TEXT);
        CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
        CREATE INDEX IF NOT EXISTS samples_node ON samples(node, ts);
        CREATE TABLE IF NOT EXISTS sessions(
          sid_hash TEXT PRIMARY KEY, user TEXT, ip TEXT, created REAL, expires REAL);
        CREATE TABLE IF NOT EXISTS providers(
          id TEXT PRIMARY KEY, name TEXT, base_url TEXT, api_key TEXT, default_model TEXT,
          ca_pem TEXT, created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS chats(
          id TEXT PRIMARY KEY, user TEXT, title TEXT, target TEXT, n_msgs INTEGER,
          created REAL, updated REAL, body TEXT);
        CREATE INDEX IF NOT EXISTS chats_user ON chats(user, updated);
        CREATE TABLE IF NOT EXISTS chat_blobs(
          id TEXT PRIMARY KEY, chat TEXT, user TEXT, mime TEXT, data TEXT, created REAL);
        CREATE INDEX IF NOT EXISTS chat_blobs_chat ON chat_blobs(chat);
        """)

    # ---- 稽核
    def audit(self, user, ip, action, target="", ok=True, detail=None):
        with self.lock:
            self.db.execute(
                "INSERT INTO audit(ts,user,ip,action,target,ok,detail) VALUES(?,?,?,?,?,?,?)",
                (time.time(), user or "", ip or "", action, target or "", 1 if ok else 0,
                 json.dumps(redact(detail or {}), ensure_ascii=False)))

    def audit_list(self, limit=200, since=None):
        q = "SELECT ts,user,ip,action,target,ok,detail FROM audit"
        args = []
        if since:
            q += " WHERE ts>=?"
            args.append(since)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        return [{"ts": r[0], "user": r[1], "ip": r[2], "action": r[3], "target": r[4],
                 "ok": bool(r[5]), "detail": json.loads(r[6] or "{}")} for r in rows]

    # ---- 登入票證（伺服器端記錄：登出就作廢，資料庫只存雜湊）
    @staticmethod
    def _h(sid):
        return hashlib.sha256(sid.encode()).hexdigest()

    def session_new(self, user, ip, hours):
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            self.db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                            (self._h(sid), user, ip, now, now + hours * 3600))
        return sid

    def session_user(self, sid):
        if not sid or len(sid) > 200:
            return None
        with self.lock:
            r = self.db.execute("SELECT user, expires FROM sessions WHERE sid_hash=?", (self._h(sid),)).fetchone()
        if not r or r[1] <= time.time():
            return None
        return r[0]

    def session_end(self, sid):
        if sid:
            with self.lock:
                self.db.execute("DELETE FROM sessions WHERE sid_hash=?", (self._h(sid),))

    def session_end_all(self, user=None):
        with self.lock:
            if user is None:
                self.db.execute("DELETE FROM sessions")
            else:
                self.db.execute("DELETE FROM sessions WHERE user=?", (user,))

    def failed_logins(self, since, ip=None):
        q, a = "SELECT COUNT(*) FROM audit WHERE action='auth.login' AND ok=0 AND ts>=?", [since]
        if ip is not None:
            q += " AND ip=?"
            a.append(ip)
        with self.lock:
            return self.db.execute(q, a).fetchone()[0]

    # ---- 第三方 API（OpenAI 相容）：金鑰欄位只收 enc:v1: 密文（AES-256-GCM，由呼叫端 tls.seal 加密），這裡不碰明文
    PROVIDER_COLS = ("id", "name", "base_url", "api_key", "default_model", "ca_pem", "created", "updated")

    def provider_list(self):
        with self.lock:
            rows = self.db.execute("SELECT " + ",".join(self.PROVIDER_COLS) + " FROM providers ORDER BY created").fetchall()
        return [dict(zip(self.PROVIDER_COLS, r)) for r in rows]

    def provider_get(self, pid):
        with self.lock:
            r = self.db.execute("SELECT " + ",".join(self.PROVIDER_COLS) + " FROM providers WHERE id=?", (pid,)).fetchone()
        return dict(zip(self.PROVIDER_COLS, r)) if r else None

    def provider_put(self, row):
        """新增或整筆更新。api_key 必須是 enc:v1: 密文或空字串，明文一律拒收（防呼叫端忘了加密）。"""
        key = row.get("api_key") or ""
        if key and not key.startswith("enc:v1:"):
            raise ValueError("金鑰沒有加密，拒絕寫入")
        now = time.time()
        with self.lock:
            cur = self.db.execute("SELECT created FROM providers WHERE id=?", (row["id"],)).fetchone()
            self.db.execute(
                "INSERT OR REPLACE INTO providers(id,name,base_url,api_key,default_model,ca_pem,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (row["id"], row["name"], row["base_url"], key, row.get("default_model") or "", row.get("ca_pem") or "",
                 cur[0] if cur else now, now))

    def provider_delete(self, pid):
        with self.lock:
            return self.db.execute("DELETE FROM providers WHERE id=?", (pid,)).rowcount > 0

    # ---- 對話紀錄：body 與附圖都是呼叫端用 tls.seal 加密過的 enc:v1: 密文，這裡只存不解；
    # 每一個查詢都帶 user，別人的對話一律當作不存在
    CHAT_COLS = ("id", "title", "target", "n_msgs", "created", "updated")

    def chat_list(self, user, with_body=False):
        cols = self.CHAT_COLS + (("body",) if with_body else ())
        with self.lock:
            rows = self.db.execute("SELECT " + ",".join(cols) + " FROM chats WHERE user=? ORDER BY updated DESC",
                                   (user,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def chat_get(self, user, cid):
        cols = self.CHAT_COLS + ("body",)
        with self.lock:
            r = self.db.execute("SELECT " + ",".join(cols) + " FROM chats WHERE id=? AND user=?", (cid, user)).fetchone()
        return dict(zip(cols, r)) if r else None

    def chat_owner(self, cid):
        with self.lock:
            r = self.db.execute("SELECT user FROM chats WHERE id=?", (cid,)).fetchone()
        return r[0] if r else None

    def chat_put(self, user, row, keep=500):
        """新增或整筆更新。別人的 id 回 False（不覆寫）。超過 keep 則刪掉這個人最舊的對話與附圖。"""
        body = row.get("body") or ""
        if body and not body.startswith("enc:v1:"):
            raise ValueError("對話內容沒有加密，拒絕寫入")
        if row.get("title") and not str(row["title"]).startswith("enc:v1:"):
            raise ValueError("對話標題沒有加密，拒絕寫入")       # 標題取自第一句話，一樣是使用者內容
        now = time.time()
        with self.lock:
            old = self.db.execute("SELECT user, created FROM chats WHERE id=?", (row["id"],)).fetchone()
            if old and old[0] != user:
                return False
            self.db.execute("INSERT OR REPLACE INTO chats(id,user,title,target,n_msgs,created,updated,body) VALUES(?,?,?,?,?,?,?,?)",
                            (row["id"], user, row.get("title") or "", row.get("target") or "", int(row.get("n_msgs") or 0),
                             old[1] if old else now, now, body))
            gone = [r[0] for r in self.db.execute("SELECT id FROM chats WHERE user=? ORDER BY updated DESC LIMIT -1 OFFSET ?",
                                                  (user, keep)).fetchall()]
            for g in gone:
                self.db.execute("DELETE FROM chats WHERE id=?", (g,))
                self.db.execute("DELETE FROM chat_blobs WHERE chat=?", (g,))
        return True

    def chat_rename(self, user, cid, title):
        if not str(title).startswith("enc:v1:"):
            raise ValueError("對話標題沒有加密，拒絕寫入")
        with self.lock:
            return self.db.execute("UPDATE chats SET title=? WHERE id=? AND user=?", (title, cid, user)).rowcount > 0

    def chat_delete(self, user, cid):
        with self.lock:
            ok = self.db.execute("DELETE FROM chats WHERE id=? AND user=?", (cid, user)).rowcount > 0
            if ok:
                self.db.execute("DELETE FROM chat_blobs WHERE chat=? AND user=?", (cid, user))
        return ok

    def blob_put(self, user, chat, bid, mime, data):
        if not data.startswith("enc:v1:"):
            raise ValueError("附圖沒有加密，拒絕寫入")
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO chat_blobs(id,chat,user,mime,data,created) VALUES(?,?,?,?,?,?)",
                            (bid, chat, user, mime, data, time.time()))

    def blob_bytes(self, user, chat=None):
        """這個人（或這則對話）的附圖總共占多少位元組（密文長度）。"""
        q, a = "SELECT COALESCE(SUM(LENGTH(data)),0) FROM chat_blobs WHERE user=?", [user]
        if chat:
            q += " AND chat=?"
            a.append(chat)
        with self.lock:
            return int(self.db.execute(q, a).fetchone()[0])

    def blob_get(self, user, bid):
        with self.lock:
            r = self.db.execute("SELECT chat, mime, data FROM chat_blobs WHERE id=? AND user=?", (bid, user)).fetchone()
        return {"chat": r[0], "mime": r[1], "data": r[2]} if r else None

    def blob_ids(self, user, chat):
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT id FROM chat_blobs WHERE chat=? AND user=?", (chat, user)).fetchall()}

    def blob_gc(self, user, chat, keep):
        """這則對話裡已經沒有被引用的附圖（訊息刪了、改了）一併刪掉。"""
        with self.lock:
            for (bid,) in self.db.execute("SELECT id FROM chat_blobs WHERE chat=? AND user=?", (chat, user)).fetchall():
                if bid not in keep:
                    self.db.execute("DELETE FROM chat_blobs WHERE id=?", (bid,))

    # ---- 取樣
    def sample(self, node, online, st=None, counters=None):
        p = (st or {}).get("pools") or {}
        mm = p.get("memory_model")
        pool = p.get("unified") if mm == "unified_single" else p.get("vram")
        ram = p.get("ram") or {}
        slots = (st or {}).get("slots") or []
        gpu = (st or {}).get("gpu") or {}
        with self.lock:
            self.db.execute(
                "INSERT INTO samples(ts,node,online,mm,vram_used,vram_total,ram_free,ram_total,ram_floor,"
                "gpu_power,gpu_use,busy,slots,states,counters) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), node, 1 if online else 0, mm,
                 (pool or {}).get("used_gib"), (pool or {}).get("total_gib"),
                 ram.get("free_gib"), ram.get("total_gib"), ram.get("floor_gib"),
                 gpu.get("power"), gpu.get("use"),
                 sum(1 for s in slots if s.get("busy")), sum(1 for s in slots if s.get("unit")),
                 json.dumps({s["id"]: s.get("state") for s in slots if s.get("unit")}),
                 json.dumps(counters or {})))

    def samples(self, since, node=None):
        q = ("SELECT ts,node,online,mm,vram_used,vram_total,ram_free,ram_total,ram_floor,"
             "gpu_power,gpu_use,busy,slots,states,counters FROM samples WHERE ts>=?")
        args = [since]
        if node:
            q += " AND node=?"
            args.append(node)
        q += " ORDER BY node, ts"
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        keys = ("ts", "node", "online", "mm", "vram_used", "vram_total", "ram_free", "ram_total",
                "ram_floor", "gpu_power", "gpu_use", "busy", "slots", "states", "counters")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["states"] = json.loads(d["states"] or "{}")
            d["counters"] = json.loads(d["counters"] or "{}")
            out.append(d)
        return out

    def first_sample_ts(self):
        with self.lock:
            r = self.db.execute("SELECT MIN(ts) FROM samples").fetchone()
        return r[0] if r else None

    def prune(self, keep_days=400):
        cut = time.time() - keep_days * 86400
        with self.lock:
            self.db.execute("DELETE FROM samples WHERE ts<?", (cut,))
            self.db.execute("DELETE FROM audit WHERE ts<?", (cut,))
