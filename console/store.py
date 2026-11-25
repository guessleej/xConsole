"""xConsole 的儲存層：稽核紀錄與定時取樣（SQLite，標準函式庫）。

稽核：誰在什麼時候、從哪裡、做了什麼、結果如何。寫入前一律經過 redact()——
審計最常見的洞就是把整包 request body 寫進去，密碼與私鑰跟著落地。
取樣：每 5 分鐘一筆各機狀態，服務使用報告的數字全部從這裡算，不靠手抄。
"""
import hashlib
import json
import os
import re
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
        CREATE TABLE IF NOT EXISTS api_keys(
          id TEXT PRIMARY KEY, name TEXT, owner TEXT, prefix TEXT, hash TEXT UNIQUE, slots TEXT,
          max_inflight INTEGER, created REAL, created_by TEXT, expires REAL, revoked REAL, revoked_by TEXT,
          last_used REAL, last_ip TEXT, source TEXT);
        CREATE TABLE IF NOT EXISTS api_usage(
          key_id TEXT, hour TEXT, node TEXT, slot TEXT, requests INTEGER, errors INTEGER,
          prompt_tokens INTEGER, completion_tokens INTEGER, PRIMARY KEY(key_id, hour, node, slot));
        CREATE INDEX IF NOT EXISTS api_usage_hour ON api_usage(hour);
        CREATE TABLE IF NOT EXISTS users(
          username TEXT PRIMARY KEY, display TEXT, role TEXT, source TEXT, salt TEXT, hash TEXT, algo TEXT,
          must_change INTEGER, disabled INTEGER, created REAL, pw_changed REAL, last_login REAL);
        CREATE TABLE IF NOT EXISTS agent_tasks(
          id TEXT PRIMARY KEY, user TEXT, node TEXT, state TEXT, created REAL, updated REAL, body TEXT);
        CREATE INDEX IF NOT EXISTS agent_tasks_user ON agent_tasks(user, created);
        CREATE TABLE IF NOT EXISTS agent_folders(
          node TEXT, folder TEXT, owner TEXT, created REAL, PRIMARY KEY(node, folder));
        CREATE TABLE IF NOT EXISTS schedules(
          id TEXT PRIMARY KEY, user TEXT, created REAL, updated REAL, body TEXT);
        CREATE TABLE IF NOT EXISTS push_channels(
          id TEXT PRIMARY KEY, user TEXT, created REAL, updated REAL, body TEXT);
        CREATE TABLE IF NOT EXISTS safety_cmds(
          id TEXT PRIMARY KEY, node TEXT, kind TEXT, opts TEXT, state TEXT, created REAL, by TEXT,
          tried REAL, error TEXT, done REAL, result TEXT, closed_by TEXT);
        CREATE INDEX IF NOT EXISTS safety_node ON safety_cmds(node, state);
        """)
        # 舊版資料庫補欄位：對話的收藏（明文0／1）與分類標籤（使用者寫的字，跟標題一樣加密）
        have = {r[1] for r in self.db.execute("PRAGMA table_info(chats)")}
        for col, typ in (("fav", "INTEGER DEFAULT 0"), ("labels", "TEXT")):
            if col not in have:
                self.db.execute(f"ALTER TABLE chats ADD COLUMN {col} {typ}")
        # 舊版的帳號表沒有群組範圍：補欄位，空的＝不限群組（升級後每個人看到的跟以前一樣）
        if "scope" not in {r[1] for r in self.db.execute("PRAGMA table_info(users)")}:
            self.db.execute("ALTER TABLE users ADD COLUMN scope TEXT")

    # ---- 安全指令（清單 #25）：停用、恢復、清除、撤銷身分。不設期限，機器下次連上時先送

    SAFETY_PAIR = {"disable": "enable", "enable": "disable"}

    def _safety_row(self, r):
        return {"id": r[0], "node": r[1], "kind": r[2], "opts": json.loads(r[3] or "{}"), "state": r[4], "created": r[5],
                "by": r[6], "tried": r[7], "error": r[8], "done": r[9], "result": json.loads(r[10] or "null"), "closed_by": r[11]}

    def safety_add(self, node, kind, opts, by):
        """排一筆。後下的覆蓋先下的：同一種、或停用與恢復互相，還沒送出的那筆標「被取代」。
        已排了撤銷身分就不再收新的（撤銷之後這台要重新加入，之後的指令送不到）。回 (這一筆, 被取代的編號)。"""
        with self.lock:
            pend = [self._safety_row(r) for r in self.db.execute(
                "SELECT * FROM safety_cmds WHERE node=? AND state IN ('pending','sending')", (node,))]
            if any(c["kind"] == "revoke" for c in pend) and kind != "revoke":
                raise ValueError("這台已經排了「撤銷身分」，要先取消那一筆才能下別的指令")
            gone = [c["id"] for c in pend if c["state"] == "pending" and c["kind"] in (kind, self.SAFETY_PAIR.get(kind))]
            now = time.time()
            for cid in gone:
                self.db.execute("UPDATE safety_cmds SET state='superseded', done=?, closed_by=? WHERE id=?", (now, by, cid))
            cid = secrets.token_hex(12)
            self.db.execute("INSERT INTO safety_cmds(id, node, kind, opts, state, created, by) VALUES(?,?,?,?,?,?,?)",
                            (cid, node, kind, json.dumps(opts or {}), "pending", now, by))
        return self.safety_get(cid), gone

    def safety_get(self, cid):
        r = self.db.execute("SELECT * FROM safety_cmds WHERE id=?", (cid,)).fetchone()
        return self._safety_row(r) if r else None

    def safety_pending(self, node):
        """要送的順序：照下的先後，撤銷身分一定最後（送完這台就認不得主控台了）。"""
        rows = [self._safety_row(r) for r in self.db.execute(
            "SELECT * FROM safety_cmds WHERE node=? AND state IN ('pending','sending') ORDER BY created", (node,))]
        return sorted(rows, key=lambda c: c["kind"] == "revoke")

    def safety_pending_nodes(self):
        return {r[0] for r in self.db.execute("SELECT DISTINCT node FROM safety_cmds WHERE state IN ('pending','sending')")}

    def safety_claim(self, cid):
        """送出前標「送出中」：送出中的不能取消（代理可能已經在做了）。回 False 表示已被取消或別人在送。"""
        with self.lock:
            return self.db.execute("UPDATE safety_cmds SET state='sending' WHERE id=? AND state='pending'", (cid,)).rowcount == 1

    def safety_unstick(self):
        """主控台重開：上次送到一半的改回待送（代理靠編號去重，再送一次不會做兩次）。"""
        with self.lock:
            self.db.execute("UPDATE safety_cmds SET state='pending' WHERE state='sending'")

    def safety_list(self, node, limit=50):
        return [self._safety_row(r) for r in self.db.execute(
            "SELECT * FROM safety_cmds WHERE node=? ORDER BY created DESC LIMIT ?", (node, limit))]

    def safety_cancel(self, node, cid, by):
        with self.lock:
            cur = self.db.execute("UPDATE safety_cmds SET state='canceled', done=?, closed_by=? "
                                  "WHERE id=? AND node=? AND state='pending'", (time.time(), by, cid, node))
        return cur.rowcount == 1

    def safety_cancel_node(self, node, by):
        """機器從主控台移除：還沒送到的一起取消（含送到一半的，那台已經不歸這個主控台管）。"""
        with self.lock:
            return self.db.execute("UPDATE safety_cmds SET state='canceled', done=?, closed_by=? "
                                   "WHERE node=? AND state IN ('pending','sending')", (time.time(), by, node)).rowcount

    def safety_tried(self, cid, error):
        with self.lock:
            self.db.execute("UPDATE safety_cmds SET state='pending', tried=?, error=? WHERE id=? AND state='sending'",
                            (time.time(), str(error or "")[:300], cid))

    def safety_done(self, cid, result):
        """回 True 表示這次才完成。"""
        with self.lock:
            cur = self.db.execute("UPDATE safety_cmds SET state='done', done=?, result=?, error=NULL "
                                  "WHERE id=? AND state='sending'", (time.time(), json.dumps(result), cid))
        return cur.rowcount == 1

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

    def failed_logins(self, since, ip=None, user=None):
        q, a = "SELECT COUNT(*) FROM audit WHERE action='auth.login' AND ok=0 AND ts>=?", [since]
        if ip is not None:
            q += " AND ip=?"
            a.append(ip)
        if user is not None:
            q += " AND target=?"
            a.append(user)
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
    CHAT_COLS = ("id", "title", "target", "n_msgs", "created", "updated", "fav", "labels")

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
            # 收藏與分類標籤另外改（chat_meta），存對話內容時不能把它們洗掉：用upsert不用REPLACE
            self.db.execute("INSERT INTO chats(id,user,title,target,n_msgs,created,updated,body) VALUES(?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, target=excluded.target, "
                            "n_msgs=excluded.n_msgs, updated=excluded.updated, body=excluded.body",
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

    def chat_meta(self, user, cid, fav=None, labels=None):
        """改收藏（布林）或分類標籤（enc:v1: 密文）；不動updated，列表順序不會因為按收藏而跳。"""
        if labels is not None and labels and not labels.startswith("enc:v1:"):
            raise ValueError("分類標籤沒有加密，拒絕寫入")
        sets, args = [], []
        if fav is not None:
            sets.append("fav=?")
            args.append(1 if fav else 0)
        if labels is not None:
            sets.append("labels=?")
            args.append(labels)
        if not sets:
            return self.chat_get(user, cid) is not None
        with self.lock:
            return self.db.execute("UPDATE chats SET " + ",".join(sets) + " WHERE id=? AND user=?", (*args, cid, user)).rowcount > 0

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

    # ---- 帳號：密碼只存 PBKDF2 雜湊；角色每個請求重新查，降級或停用馬上生效
    USER_COLS = ("username", "display", "role", "source", "salt", "hash", "algo", "must_change", "disabled",
                 "created", "pw_changed", "last_login", "scope")

    def user_get(self, username):
        with self.lock:
            r = self.db.execute("SELECT " + ",".join(self.USER_COLS) + " FROM users WHERE username=?", (username,)).fetchone()
        return dict(zip(self.USER_COLS, r)) if r else None

    def user_list(self):
        with self.lock:
            rows = self.db.execute("SELECT " + ",".join(self.USER_COLS) + " FROM users ORDER BY role, username").fetchall()
        return [dict(zip(self.USER_COLS, r)) for r in rows]

    def user_count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def admin_count(self):
        """還能登入的管理員有幾位：最後一位不能被刪、停用或降級。"""
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND COALESCE(disabled,0)=0").fetchone()[0]

    def user_insert(self, row):
        with self.lock:
            self.db.execute("INSERT INTO users(" + ",".join(self.USER_COLS) + ") VALUES(" + ",".join("?" * len(self.USER_COLS)) + ")",
                            tuple(row.get(k) for k in self.USER_COLS))

    def user_update(self, username, fields):
        cols = [k for k in fields if k in self.USER_COLS and k != "username"]
        if not cols:
            return False
        with self.lock:
            return self.db.execute("UPDATE users SET " + ",".join(f"{k}=?" for k in cols) + " WHERE username=?",
                                   (*[fields[k] for k in cols], username)).rowcount > 0

    def user_delete(self, username):
        """刪帳號連同他的對話、附圖與登入票證；金鑰與 Agent 資料夾由呼叫端處理（撤銷、改歸管理員）。"""
        with self.lock:
            n = self.db.execute("DELETE FROM users WHERE username=?", (username,)).rowcount
            self.db.execute("DELETE FROM chats WHERE user=?", (username,))
            self.db.execute("DELETE FROM chat_blobs WHERE user=?", (username,))
            self.db.execute("DELETE FROM sessions WHERE user=?", (username,))
            self.db.execute("DELETE FROM agent_tasks WHERE user=?", (username,))
            self.db.execute("DELETE FROM schedules WHERE user=?", (username,))
            self.db.execute("DELETE FROM push_channels WHERE user=?", (username,))
        return n > 0

    # ---- Agent任務：body是呼叫端tls.seal加密過的enc:v1: 密文（交代的事、計畫、步驟紀錄都是使用者內容），這裡只存不解
    def agent_task_put(self, tid, user, node, state, created, body, keep=100):
        """新增或整筆更新。超過keep則刪掉這個人最舊、已經結束的任務。"""
        if not body.startswith("enc:v1:"):
            raise ValueError("Agent任務沒有加密，拒絕寫入")
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO agent_tasks(id,user,node,state,created,updated,body) VALUES(?,?,?,?,?,?,?)",
                            (tid, user, node, state, created, time.time(), body))
            gone = [r[0] for r in self.db.execute(
                "SELECT id FROM agent_tasks WHERE user=? AND state NOT IN ('running','waiting_approval') "
                "ORDER BY created DESC LIMIT -1 OFFSET ?", (user, keep)).fetchall()]
            for g in gone:
                self.db.execute("DELETE FROM agent_tasks WHERE id=?", (g,))
        return gone

    def agent_task_rows(self):
        with self.lock:
            return [dict(zip(("id", "user", "node", "state", "created", "updated", "body"), r)) for r in
                    self.db.execute("SELECT id,user,node,state,created,updated,body FROM agent_tasks ORDER BY created").fetchall()]

    def agent_task_delete(self, tid, user):
        with self.lock:
            return self.db.execute("DELETE FROM agent_tasks WHERE id=? AND user=?", (tid, user)).rowcount > 0

    # ---- 定時任務與推送管道：body是呼叫端tls.seal加密過的enc:v1: 密文（提示詞、結果、令牌、Webhook網址），這裡只存不解
    def sealed_put(self, table, rid, user, body):
        if table not in ("schedules", "push_channels"):
            raise ValueError(table)
        if not body.startswith("enc:v1:"):
            raise ValueError("沒有加密，拒絕寫入")
        now = time.time()
        with self.lock:
            self.db.execute(f"INSERT INTO {table}(id,user,created,updated,body) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(id) DO UPDATE SET body=excluded.body, updated=excluded.updated WHERE user=excluded.user",
                            (rid, user, now, now, body))

    def sealed_rows(self, table, user=None):
        if table not in ("schedules", "push_channels"):
            raise ValueError(table)
        q, args = f"SELECT id,user,created,updated,body FROM {table}", ()
        if user is not None:
            q, args = q + " WHERE user=?", (user,)
        with self.lock:
            return [dict(zip(("id", "user", "created", "updated", "body"), r)) for r in self.db.execute(q + " ORDER BY created", args).fetchall()]

    def sealed_delete(self, table, rid, user):
        if table not in ("schedules", "push_channels"):
            raise ValueError(table)
        with self.lock:
            return self.db.execute(f"DELETE FROM {table} WHERE id=? AND user=?", (rid, user)).rowcount > 0

    # ---- Agent 資料夾的擁有者（資料夾本身在代理那台；這裡只記是誰建的）
    def folder_owner(self, node, folder):
        with self.lock:
            r = self.db.execute("SELECT owner FROM agent_folders WHERE node=? AND folder=?", (node, folder)).fetchone()
        return r[0] if r else None

    def folder_set(self, node, folder, owner):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO agent_folders(node,folder,owner,created) VALUES(?,?,?,?)",
                            (node, folder, owner, time.time()))

    def folders_of(self, owner):
        with self.lock:
            return {(r[0], r[1]) for r in self.db.execute("SELECT node, folder FROM agent_folders WHERE owner=?", (owner,))}

    def folders_reassign(self, owner, to):
        with self.lock:
            self.db.execute("UPDATE agent_folders SET owner=? WHERE owner=?", (to, owner))

    # ---- 加密入口的金鑰：只存 SHA-256 與前 8 個字元（明文只在產生當下回一次，這裡碰不到）
    KEY_COLS = ("id", "name", "owner", "prefix", "hash", "slots", "max_inflight", "created", "created_by",
                "expires", "revoked", "revoked_by", "last_used", "last_ip", "source")

    def _key_row(self, r):
        d = dict(zip(self.KEY_COLS, r))
        d["slots"] = d["slots"] if d["slots"] == "*" else json.loads(d["slots"] or "[]")
        return d

    def key_insert(self, row):
        if not re.fullmatch(r"[0-9a-f]{64}", row.get("hash") or ""):
            raise ValueError("金鑰雜湊格式不對，拒絕寫入")       # 防呼叫端把明文當雜湊傳進來
        with self.lock:
            self.db.execute("INSERT INTO api_keys(" + ",".join(self.KEY_COLS) + ") VALUES(" + ",".join("?" * len(self.KEY_COLS)) + ")",
                            tuple(json.dumps(row[k]) if k == "slots" and row[k] != "*" else row.get(k) for k in self.KEY_COLS))

    def key_by_hash(self, h):
        with self.lock:
            r = self.db.execute("SELECT " + ",".join(self.KEY_COLS) + " FROM api_keys WHERE hash=?", (h,)).fetchone()
        return self._key_row(r) if r else None

    def key_get(self, kid):
        with self.lock:
            r = self.db.execute("SELECT " + ",".join(self.KEY_COLS) + " FROM api_keys WHERE id=?", (kid,)).fetchone()
        return self._key_row(r) if r else None

    def key_list(self, owner=None):
        q, a = "SELECT " + ",".join(self.KEY_COLS) + " FROM api_keys", []
        if owner is not None:
            q, a = q + " WHERE owner=?", [owner]
        with self.lock:
            rows = self.db.execute(q + " ORDER BY created DESC", a).fetchall()
        return [self._key_row(r) for r in rows]

    def key_update(self, kid, fields):
        cols = [k for k in ("name", "slots", "max_inflight", "expires") if k in fields]
        if not cols:
            return self.key_get(kid) is not None
        vals = [json.dumps(fields[k]) if k == "slots" and fields[k] != "*" else fields[k] for k in cols]
        with self.lock:
            return self.db.execute("UPDATE api_keys SET " + ",".join(f"{k}=?" for k in cols) + " WHERE id=? AND revoked IS NULL",
                                   (*vals, kid)).rowcount > 0

    def key_revoke(self, kid, by):
        with self.lock:
            return self.db.execute("UPDATE api_keys SET revoked=?, revoked_by=? WHERE id=? AND revoked IS NULL",
                                   (time.time(), by or "", kid)).rowcount > 0

    def key_touch(self, kid, ip):
        with self.lock:
            self.db.execute("UPDATE api_keys SET last_used=?, last_ip=? WHERE id=?", (time.time(), ip or "", kid))

    def usage_add(self, kid, hour, node, slot, ok, prompt, completion):
        """hour 是台北時間的「YYYY-MM-DD HH」：以小時分桶，報表的起點才能對到幾點，不會多算整天。"""
        with self.lock:
            self.db.execute(
                "INSERT INTO api_usage(key_id,hour,node,slot,requests,errors,prompt_tokens,completion_tokens) VALUES(?,?,?,?,1,?,?,?) "
                "ON CONFLICT(key_id,hour,node,slot) DO UPDATE SET requests=requests+1, errors=errors+excluded.errors, "
                "prompt_tokens=prompt_tokens+excluded.prompt_tokens, completion_tokens=completion_tokens+excluded.completion_tokens",
                (kid, hour, node, slot, 0 if ok else 1, int(prompt), int(completion)))

    def usage_by_key(self, since, until=None):
        """{金鑰 id: {requests, errors, prompt_tokens, completion_tokens}}。since／until 是「YYYY-MM-DD」或「YYYY-MM-DD HH」
        （台北時間），含 since、不含 until；字串照字典序比，日期當成那天 00 時。"""
        q, a = ("SELECT key_id, SUM(requests), SUM(errors), SUM(prompt_tokens), SUM(completion_tokens) FROM api_usage "
                "WHERE hour>=?"), [since]
        if until:
            q, a = q + " AND hour<?", a + [until]
        with self.lock:
            rows = self.db.execute(q + " GROUP BY key_id", a).fetchall()
        return {r[0]: {"requests": r[1], "errors": r[2], "prompt_tokens": r[3], "completion_tokens": r[4]} for r in rows}

    def usage_by_slot(self, since, until=None):
        """{(node, slot): {...}}：跟引擎記錄檔算到的總量相減，就是沒經過加密入口（直連）的量。"""
        q, a = ("SELECT node, slot, SUM(requests), SUM(errors), SUM(prompt_tokens), SUM(completion_tokens) FROM api_usage "
                "WHERE hour>=?"), [since]
        if until:
            q, a = q + " AND hour<?", a + [until]
        with self.lock:
            rows = self.db.execute(q + " GROUP BY node, slot", a).fetchall()
        return {(r[0], r[1]): {"requests": r[2], "errors": r[3], "prompt_tokens": r[4], "completion_tokens": r[5]} for r in rows}

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

    def last_online(self, node):
        """最後一次取樣到這台在線的時間（群組總覽的「離線（上次連線時間）」）。"""
        with self.lock:
            r = self.db.execute("SELECT MAX(ts) FROM samples WHERE node=? AND online=1", (node,)).fetchone()
        return r[0] if r else None

    def first_sample_ts(self):
        with self.lock:
            r = self.db.execute("SELECT MIN(ts) FROM samples").fetchone()
        return r[0] if r else None

    def prune(self, keep_days=400):
        cut = time.time() - keep_days * 86400
        with self.lock:
            self.db.execute("DELETE FROM samples WHERE ts<?", (cut,))
            self.db.execute("DELETE FROM audit WHERE ts<?", (cut,))
            self.db.execute("DELETE FROM api_usage WHERE hour<?",
                            (time.strftime("%Y-%m-%d", time.gmtime(cut + 8 * 3600)),))
