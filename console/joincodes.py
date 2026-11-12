"""群組加入碼：可用 N 次、有效 T 分鐘、名稱樣板、限定網段、可作廢。

只存加入碼的 SHA-256（碼本身 24 位元組隨機，雜湊不必加鹽或拉長），存在 join_codes.json（0600），
主控台重啟後照樣有效：教室一次發 20 台，不能因為主控台重開就全部重來。
同時加入的請求各自先「預留」一個名額（與名稱），驗證失敗就放回去；名額算法：已用＋預留中 < 可用次數。
"""
import hashlib
import ipaddress
import json
import os
import re
import threading
import time

MAX_USES = 1000
MIN_MIN, MAX_MIN = 5, 7 * 24 * 60
KEEP = 7 * 86400                  # 過期、用完或作廢的碼再留 7 天，管理者查得到它加入了哪些機器
RANGE_RX = re.compile(r"\{(\d{1,4})\.\.(\d{1,4})\}")


def key(code):
    return hashlib.sha256(str(code or "").encode()).hexdigest()


def expand(tpl):
    """名稱樣板：`A{01..20}` → A01～A20（位數照起頭那個數字補零）。沒有 {a..b} 回 None（＝一般名稱）。"""
    ms = RANGE_RX.findall(tpl)
    if not ms:
        if "{" in tpl or "}" in tpl:
            raise ValueError("名稱樣板寫法是 前綴{起..迄}，例：A{01..20}")
        return None
    if len(ms) > 1:
        raise ValueError("名稱樣板只能有一段 {起..迄}")
    a, b = ms[0]
    lo, hi = int(a), int(b)
    if hi < lo or hi - lo >= MAX_USES:
        raise ValueError(f"名稱樣板的範圍要由小到大，最多 {MAX_USES} 個")
    w = len(a)
    pre, post = RANGE_RX.split(tpl, 1)[0], RANGE_RX.split(tpl, 1)[-1]
    return [f"{pre}{str(i).zfill(w)}{post}" for i in range(lo, hi + 1)]


def parse_cidrs(v):
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s，、]+", v) if x]
    if not isinstance(v, list) or len(v) > 8:
        raise ValueError("限定網段最多 8 個，例：198.51.100.0/24")
    out = []
    for x in v:
        try:
            out.append(str(ipaddress.ip_network(str(x).strip(), strict=False)))
        except ValueError:
            raise ValueError(f"「{str(x)[:40]}」不是網段，例：198.51.100.0/24") from None
    return out


def ip_allowed(rec, ip):
    if not rec.get("cidrs"):
        return True
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in ipaddress.ip_network(c) for c in rec["cidrs"])


def state(rec, now=None):
    now = now or time.time()
    if rec.get("revoked"):
        return "revoked"
    if rec["used"] >= rec["uses"]:
        return "joined"
    if rec["exp"] < now:
        return "expired"
    return "waiting"


class Codes:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.pending = {}                 # 執行緒 → (雜湊, 預留的名稱)；只放記憶體
        try:
            with open(path, encoding="utf-8") as fh:
                self.recs = json.load(fh)
        except (OSError, ValueError):
            self.recs = {}

    def _save(self):
        now = time.time()
        for k in [k for k, r in self.recs.items() if max(r["exp"], r.get("closed") or 0) < now - KEEP]:
            self.recs.pop(k)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.recs, fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    def add(self, code, *, minutes=30, uses=1, name="", group="", replace="", cidrs=(), **extra):
        """建一個加入碼。參數不合法丟 ValueError（訊息給管理者看）。回紀錄（不含碼本身）。"""
        if isinstance(uses, bool) or not isinstance(uses, int) or not 1 <= uses <= MAX_USES:
            raise ValueError(f"可用次數要是 1～{MAX_USES} 的整數")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not MIN_MIN <= minutes <= MAX_MIN:
            raise ValueError(f"有效時間要是 {MIN_MIN}～{MAX_MIN} 分鐘（最長 7 天）")
        names = expand(name) if name else None
        if names is not None and len(names) < uses:
            raise ValueError(f"名稱樣板只有 {len(names)} 個名稱，比可用次數 {uses} 少")
        if names is None and name and uses > 1:
            raise ValueError("一個加入碼要加入多台時，名稱請用樣板（例：A{01..20}），或留空用各台的主機名稱")
        if replace and uses != 1:
            raise ValueError("重新安裝某一台時，可用次數只能是 1")
        now = time.time()
        rec = dict(extra, id=key(code)[:12], created=now, exp=now + minutes * 60, minutes=minutes, uses=uses, used=0,
                   name="" if names else name, template=name if names else "", names=names or [], group=group,
                   replace=replace, cidrs=parse_cidrs(cidrs if isinstance(cidrs, str) else list(cidrs)), nodes=[], revoked=False, error=None)
        with self.lock:
            self.recs[key(code)] = rec
            self._save()
        return rec

    def get(self, code):
        with self.lock:
            return self.recs.get(key(code))

    def by_id(self, cid):
        with self.lock:
            return next(((k, r) for k, r in self.recs.items() if r["id"] == cid), (None, None))

    def usable(self, code, ip=None):
        """安裝腳本下載用：碼有效、還有名額、來源在限定網段內才回紀錄。"""
        with self.lock:
            r = self.recs.get(key(code))
            if not r or state(r) != "waiting" or (ip is not None and not ip_allowed(r, ip)):
                return None
            return r

    def reserve(self, code, ip, taken_names=()):
        """預留一個名額給這個執行緒。回 (紀錄, 名稱, 錯誤)。名稱：樣板的下一個沒人用的；不是樣板就是產碼時填的。"""
        with self.lock:
            h = key(code)
            r = self.recs.get(h)
            if not r or state(r) != "waiting":
                return None, None, "加入碼不對、已經用完、過期或被作廢了；請在主控台重新產生加入碼"
            if not ip_allowed(r, ip):
                return None, None, "這個加入碼只收指定網段來的機器"
            mine = [v for v in self.pending.values() if v[0] == h]
            if r["used"] + len(mine) >= r["uses"]:
                return None, None, "另一台正在用這個加入碼加入，名額已滿；等它完成或請在主控台重新產生"
            name = r["name"]
            if r["names"]:
                busy = {n["name"] for n in r["nodes"]} | {v[1] for v in mine} | set(taken_names)
                name = next((x for x in r["names"] if x not in busy), None)
                if name is None:
                    return None, None, "名稱樣板的名稱都被用掉了，請在主控台重新產生加入碼"
            self.pending[threading.get_ident()] = (h, name)
            return r, name, None

    def release(self):
        with self.lock:
            self.pending.pop(threading.get_ident(), None)

    def fail(self, r, msg):
        with self.lock:
            r["error"] = msg
            self.pending.pop(threading.get_ident(), None)
            self._save()

    def finish(self, r, node_id, name, ip, linger=None):
        with self.lock:
            self.pending.pop(threading.get_ident(), None)
            r["used"] += 1
            r["nodes"].append({"id": node_id, "name": name, "ip": ip, "at": int(time.time())})
            r["node_id"], r["linger"], r["error"] = node_id, linger, None
            if r["used"] >= r["uses"]:
                r["closed"] = time.time()
            self._save()

    def revoke(self, cid):
        with self.lock:
            k, r = self.by_id(cid)
            if not r:
                return None
            r["revoked"], r["closed"] = True, time.time()
            self._save()
            return r

    def listing(self):
        with self.lock:
            return sorted((public(r) for r in self.recs.values()), key=lambda x: -x["created"])


def public(r):
    """給網頁看的欄位：不含雜湊、憑證公鑰這些安裝腳本才要的東西。"""
    return {k: r.get(k) for k in ("id", "created", "minutes", "uses", "used", "name", "template", "group", "replace",
                                  "cidrs", "nodes", "error", "by", "node_id", "linger")} | {
        "expires_at": r["exp"], "state": state(r)}

