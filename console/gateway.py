"""加密入口：主控台 9400 埠的 /v1，讓 IDE 用個別金鑰呼叫模型（HTTPS，沿用主控台的憑證）。

每把金鑰各自能撤銷、各自記用量。資料庫只存金鑰的 SHA-256 與前 8 個字元，明文只在產生當下回一次。
金鑰本身有 256 位元亂數，用一般雜湊就夠；每個請求都要驗一次，PBKDF2 這類慢雜湊會讓每次呼叫多出上百毫秒。
這個模組只放不碰網路的邏輯（格式、驗證、用量解析），轉送在 xconsole_server.Handler._gateway。
"""
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

TPE = dt.timezone(dt.timedelta(hours=8))
PREFIX = "xck_"
# 對 IDE 開放的路徑與方法；Strata 另外講 Anthropic 與 Responses 兩種格式
PATHS = {"models": "GET", "chat/completions": "POST", "completions": "POST", "embeddings": "POST"}
STRATA_PATHS = {"messages": "POST", "responses": "POST"}
SLOT_RX = re.compile(r"[\w.-]{1,40}/[\w.-]{1,40}")
TOKEN_RX = re.compile(r"[\x21-\x7e]{16,200}")
NAME_MAX, SLOTS_MAX, INFLIGHT_MAX, EXPIRE_DAYS_MAX = 60, 32, 16, 3650
DEFAULT_INFLIGHT = 2


def new_key():
    return PREFIX + secrets.token_urlsafe(32)


# 雜湊加一段固定的用途標記（HMAC）：同一串字拿去別處算的 SHA-256 對不上這裡，彩虹表也得專門為 xConsole 重算。
# 代理那邊匯入舊金鑰時算的是同一個（agent/xconsole_agent.py 的 KEY_HASH_TAG），兩邊要一致。
HASH_TAG = b"xconsole-api-key-v1"


def key_hash(key):
    return hmac.new(HASH_TAG, key.encode(), hashlib.sha256).hexdigest()


def presented_key(headers):
    """IDE 帶來的金鑰：OpenAI 格式放 Authorization: Bearer，Anthropic 格式放 x-api-key。網址參數一律不收。"""
    auth = (headers.get("Authorization") or "").strip()
    if auth[:7].lower() == "bearer ":
        k = auth[7:].strip()
    else:
        k = (headers.get("x-api-key") or "").strip()
    return k if TOKEN_RX.fullmatch(k) else ""


def day(ts=None):
    """用量依台北時間的日期分天。"""
    return dt.datetime.fromtimestamp(ts if ts is not None else time.time(), TPE).strftime("%Y-%m-%d")


def month_start(ts=None):
    return day(ts)[:8] + "01"


def check_fields(body, partial=False):
    """新增或修改金鑰的欄位。回 (欄位, 錯誤訊息)。partial：修改時沒送的欄位不管。"""
    out = {}
    if not partial or "name" in body:
        name = str(body.get("name") or "").strip()
        if not 1 <= len(name) <= NAME_MAX or any(ord(c) < 32 for c in name):
            return None, f"名稱要 1～{NAME_MAX} 個字，例如「資工系 王老師」或「機器學習（週三）」"
        out["name"] = name
    if not partial or "slots" in body:
        s = body.get("slots", "*")
        if s == "*":
            out["slots"] = "*"
        elif isinstance(s, list) and 1 <= len(s) <= SLOTS_MAX and all(isinstance(x, str) and SLOT_RX.fullmatch(x) for x in s):
            out["slots"] = sorted(set(s))
        else:
            return None, "能用的模型要選「全部」或至少選一格"
    if not partial or "max_inflight" in body:
        v = body.get("max_inflight", DEFAULT_INFLIGHT)
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= INFLIGHT_MAX:
            return None, f"同時進行的請求上限要是 1～{INFLIGHT_MAX}"
        out["max_inflight"] = v
    if not partial or "expires_days" in body:
        v = body.get("expires_days", 0)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= EXPIRE_DAYS_MAX:
            return None, f"有效天數要是 0（不到期）～{EXPIRE_DAYS_MAX}"
        out["expires"] = time.time() + v * 86400 if v else None
    return out, None


def state(row, now=None):
    now = now or time.time()
    if row.get("revoked"):
        return "revoked"
    if row.get("expires") and row["expires"] <= now:
        return "expired"
    return "active"


def allows(row, slot_key):
    s = row.get("slots")
    return s == "*" or slot_key in (s or [])


def public(row, usage=None):
    """給畫面的樣子：不含雜湊。"""
    out = {k: row.get(k) for k in ("id", "name", "owner", "prefix", "slots", "max_inflight", "created", "created_by",
                                   "expires", "revoked", "revoked_by", "last_used", "last_ip", "source")}
    out["state"] = state(row)
    if usage is not None:
        out["usage"] = usage
    return out


class Inflight:
    """每把金鑰同時進行中的請求數：一個人不能把整台佔滿。"""

    def __init__(self):
        self._n, self._lock = {}, threading.Lock()

    def acquire(self, kid, limit):
        with self._lock:
            if self._n.get(kid, 0) >= limit:
                return False
            self._n[kid] = self._n.get(kid, 0) + 1
            return True

    def release(self, kid):
        with self._lock:
            self._n[kid] = self._n.get(kid, 1) - 1
            if self._n[kid] <= 0:
                self._n.pop(kid, None)

    def count(self, kid):
        with self._lock:
            return self._n.get(kid, 0)


def usage_of(u):
    """各種格式的用量欄位統一成 (輸入, 輸出)：OpenAI 是 prompt／completion_tokens，Anthropic 與 Responses 是 input／output_tokens。"""
    if not isinstance(u, dict):
        return 0, 0

    def num(*ks):
        for k in ks:
            v = u.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                return int(v)
        return 0
    return num("prompt_tokens", "input_tokens"), num("completion_tokens", "output_tokens")


class StreamUsage:
    """一行一行看串流（SSE），記下用量；入口自己替客戶端加的用量段落不轉給客戶端。

    OpenAI 格式要 stream_options.include_usage 才會在最後多送一段 choices 為空陣列的用量，
    客戶端沒要的話入口自己加上，那一段就吞掉（有的客戶端收到空的 choices 會出錯）。
    Anthropic 格式的用量分在 message_start 與 message_delta 兩處，Responses 在 response.completed：
    各欄位取看過的最大值。"""

    def __init__(self, injected):
        self.injected, self.prompt, self.completion = injected, 0, 0

    def feed(self, line):
        """回 True＝這一行要轉給客戶端。"""
        if b'"usage"' not in line:
            return True
        s = line.decode("utf-8", "replace").strip()
        if not s.startswith("data:"):
            return True
        try:
            obj = json.loads(s[5:].strip())
        except ValueError:
            return True
        if not isinstance(obj, dict):
            return True
        for u in (obj.get("usage"), (obj.get("message") or {}).get("usage") if isinstance(obj.get("message"), dict) else None,
                  (obj.get("response") or {}).get("usage") if isinstance(obj.get("response"), dict) else None):
            p, c = usage_of(u)
            self.prompt, self.completion = max(self.prompt, p), max(self.completion, c)
        return not (self.injected and obj.get("choices") == [] and isinstance(obj.get("usage"), dict))


class FailLimit:
    """同一來源送錯金鑰的次數（記憶體裡，15 分鐘一輪）。金鑰有 256 位元，猜不到；擋的是有人拿錯的金鑰一直重送灌爆稽核。"""

    def __init__(self, window=900, limit=30):
        self.window, self.limit, self._f, self._lock = window, limit, {}, threading.Lock()

    MAX_SOURCES = 10000

    def _purge(self):
        """過期的來源整筆刪掉：掃描器或一直換 IPv6 位址的客戶端不能讓這張表無限長大。"""
        cut = time.time() - self.window
        for ip in [k for k, v in self._f.items() if not v or v[-1] <= cut]:
            del self._f[ip]

    def blocked(self, ip):
        with self._lock:
            lst = [t for t in self._f.get(ip, []) if t > time.time() - self.window]
            if lst:
                self._f[ip] = lst
            else:
                self._f.pop(ip, None)
            return len(lst) >= self.limit

    def add(self, ip):
        """回 True＝這一輪第一次失敗（要寫一筆稽核）。"""
        with self._lock:
            if len(self._f) >= self.MAX_SOURCES:
                self._purge()
                if len(self._f) >= self.MAX_SOURCES:      # 全部都還沒過期：丟掉最舊的那一半
                    for ip in sorted(self._f, key=lambda k: self._f[k][-1])[:len(self._f) // 2]:
                        del self._f[ip]
            lst = self._f.setdefault(ip, [])
            lst.append(time.time())
            return len(lst) == 1

    def size(self):
        with self._lock:
            return len(self._f)


if __name__ == "__main__":
    # 自我檢查
    k = new_key()
    assert k.startswith(PREFIX) and len(k) == 47 and key_hash(k) != key_hash(new_key())
    assert presented_key({"Authorization": "Bearer " + k}) == k
    assert presented_key({"x-api-key": k}) == k
    assert presented_key({"Authorization": "Basic abc"}) == ""
    fl = FailLimit(window=900, limit=3)
    fl.MAX_SOURCES = 4
    for i in range(10):
        fl.add(f"10.0.0.{i}")
    assert fl.size() <= 4                      # 來源表有上限，不會無限長大
    assert not fl.blocked("10.9.9.9") and fl.size() <= 4
    su = StreamUsage(True)
    assert su.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n')
    assert not su.feed(b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n')
    assert (su.prompt, su.completion) == (5, 2)
    assert StreamUsage(False).feed(b'data: {"choices":[],"usage":{"prompt_tokens":5}}\n')
    print("ok")
