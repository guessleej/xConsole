"""xConsole 的儲存層：稽核紀錄與定時取樣（SQLite，標準函式庫）。

稽核：誰在什麼時候、從哪裡、做了什麼、結果如何。寫入前一律經過 redact()——
審計最常見的洞就是把整包 request body 寫進去，密碼與私鑰跟著落地。
取樣：每 5 分鐘一筆各機狀態，服務使用報告的數字全部從這裡算，不靠手抄。
"""
import json
import os
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
