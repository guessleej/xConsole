"""群組：一級物件（id、名稱、種類、設定、版本號、更新時間）與群組設定的檢查。

機器以名稱掛在群組底下（fleet.json 每台的 "group"）；改群組名稱時，主控台在同一次寫入把成員一起改名。
設定一改（內容真的不同）版本號就加一，只增不減；代理對齊（清單 #5）靠這個版本號判斷要不要重拿設定。
種類只是顯示用的標籤，核心不依種類做任何事。
"""
import json
import re
import secrets
import time

DEFAULT = "未分類"
KINDS = ("一般", "教室", "部門")
SOURCES = ("group", "mirror", "huggingface")         # 同群組互傳、內部鏡像、Hugging Face
LOCKABLE = ("models", "profile", "reserve_user_gib", "allowed_models", "gpu_power_limit")
APPLY = ("now", "boot", "at")
NAME_RX = re.compile(r"[^\x00-\x1f<>\"'`\\]{1,30}")
MODEL_RX = re.compile(r"[A-Za-z0-9][\w./:+-]{0,159}")


def defaults():
    return {"models": [], "profile": {}, "locked": [], "reserve_user_gib": None, "storage_root": "",
            "sources": ["huggingface"], "allowed_models": [], "apply_at": {"mode": "now"}, "gpu_power_limit": None}


def _num(v, lo, hi, name, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} 要是數字")
    if integer and int(v) != v:
        raise ValueError(f"{name} 要是整數")
    if not lo <= v <= hi:
        raise ValueError(f"{name} 要在 {lo}～{hi} 之間")
    return int(v) if integer else float(v)


def _models(v, name):
    if not isinstance(v, list) or len(v) > 32:
        raise ValueError(f"{name} 要是清單，最多 32 個")
    out = []
    for m in v:
        if not isinstance(m, str):
            raise ValueError(f"{name} 每一項都要是文字（模型名稱）")
        m = m.strip()
        if m != "auto" and not MODEL_RX.fullmatch(m):
            raise ValueError(f"{name} 的「{m[:40]}」不是模型名稱（例：Org/Model-GGUF:Q4_K_M，或 auto 由主控台依硬體挑）")
        if m not in out:
            out.append(m)
    return out


def check(s):
    """檢查並整理群組設定；不合法就丟 ValueError（訊息直接給管理者看）。沒給的欄位用預設值。"""
    if not isinstance(s, dict):
        raise ValueError("設定要是一個物件")
    unknown = set(s) - set(defaults())
    if unknown:
        raise ValueError(f"不認得的設定欄位：{', '.join(sorted(unknown))}")
    out = defaults()
    if "models" in s:
        out["models"] = _models(s["models"], "要哪些模型")
    if "allowed_models" in s:
        out["allowed_models"] = [m for m in _models(s["allowed_models"], "只准用的模型") if m != "auto"]
    p = s.get("profile") or {}
    if not isinstance(p, dict):
        raise ValueError("執行參數要是一個物件")
    bad = set(p) - {"ctx", "parallel", "thinking", "temperature", "top_p"}
    if bad:
        raise ValueError(f"不認得的執行參數：{', '.join(sorted(bad))}")
    prof = {}
    if p.get("ctx") is not None:
        prof["ctx"] = _num(p["ctx"], 512, 1048576, "上下文長度", True)
    if p.get("parallel") is not None:
        prof["parallel"] = _num(p["parallel"], 1, 64, "並行數", True)
    if p.get("thinking") is not None:
        if not isinstance(p["thinking"], bool):
            raise ValueError("思考開關要是開或關")
        prof["thinking"] = p["thinking"]
    if p.get("temperature") is not None:
        prof["temperature"] = _num(p["temperature"], 0, 2, "溫度")
    if p.get("top_p") is not None:
        prof["top_p"] = _num(p["top_p"], 0, 1, "top_p")
    out["profile"] = prof
    lk = s.get("locked") or []
    if not isinstance(lk, list) or set(lk) - set(LOCKABLE):
        raise ValueError(f"鎖定項目只能是：{', '.join(LOCKABLE)}")
    out["locked"] = [k for k in LOCKABLE if k in lk]
    if s.get("reserve_user_gib") is not None:
        out["reserve_user_gib"] = _num(s["reserve_user_gib"], 0, 1024, "保留給使用者的記憶體（GiB）")
    root = str(s.get("storage_root") or "").strip()
    if root:
        parts = re.split(r"[\\/]+", root)
        absolute = root.startswith("/") or re.match(r"[A-Za-z]:[\\/]", root)
        if len(root) > 255 or not absolute or ".." in parts or any(ord(c) < 32 for c in root):
            raise ValueError("模型存放位置要是完整路徑（例：/data/xconsole 或 D:\\xconsole），不能有 ..")
    out["storage_root"] = root
    src = s.get("sources", out["sources"])
    if not isinstance(src, list) or not src or set(src) - set(SOURCES):
        raise ValueError("模型來源至少選一個：同群組、內部鏡像、Hugging Face")
    out["sources"] = [x for x in SOURCES if x in src]
    a = s.get("apply_at") or {"mode": "now"}
    if not isinstance(a, dict) or a.get("mode") not in APPLY:
        raise ValueError("套用時機要是：立即、下次開機或指定時間")
    if a["mode"] == "at":
        out["apply_at"] = {"mode": "at", "at": _num(a.get("at"), 946684800, 4102444800, "指定時間", True)}
    else:
        out["apply_at"] = {"mode": a["mode"]}
    if s.get("gpu_power_limit") is not None:
        out["gpu_power_limit"] = _num(s["gpu_power_limit"], 30, 3000, "顯卡功耗上限（瓦）", True)
    return out


def check_name(name):
    name = str(name or "").strip()
    if not NAME_RX.fullmatch(name):
        raise ValueError("群組名稱 1～30 個字，不能有引號、角括號或反斜線")
    return name


def new(name, kind="一般", legacy=False):
    """legacy：舊版留下的群組名稱，可能有現在不收的字元（引號、反斜線）。照原樣保留，
    群組頁才管得到它；管理者之後可以改成合法的名稱。"""
    return {"id": secrets.token_hex(4), "name": str(name)[:60] if legacy else check_name(name),
            "kind": kind if kind in KINDS else "一般", "settings": defaults(), "version": 1, "updated": int(time.time())}


def ensure(cfg):
    """每台機器的群組都要有群組物件（舊版只有名稱）；「未分類」一定存在。回 True 表示有補。"""
    gs = cfg.setdefault("groups", [])
    have = {g["name"] for g in gs}
    added = False
    for name in [DEFAULT] + [n.get("group") or DEFAULT for n in cfg.get("nodes", [])]:
        if name not in have:
            gs.append(new(name, legacy=True))
            have.add(name)
            added = True
    return added


def update(g, name=None, kind=None, settings=None, taken=()):
    """改名、改種類、改設定，全部檢查過才動。設定內容真的不同才把版本號加一。
    taken：別的群組已用的名稱。回 (新名稱, 設定有沒有變)。"""
    new_name = g["name"] if name is None or name == g["name"] else check_name(name)   # 沒改名就不重驗（舊名稱可能有現在不收的字元）
    if g["name"] == DEFAULT and new_name != DEFAULT:
        raise ValueError("「未分類」是預設群組，不能改名")
    if new_name != g["name"] and new_name in taken:
        raise ValueError("已經有同名的群組")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"種類要是：{'、'.join(KINDS)}")
    new_s = check(settings) if settings is not None else None
    g["name"] = new_name
    if kind is not None:
        g["kind"] = kind
    changed = new_s is not None and json.dumps(new_s, sort_keys=True) != json.dumps(g.get("settings") or defaults(), sort_keys=True)
    if changed:
        g["settings"] = new_s
        g["version"] = int(g.get("version") or 0) + 1
    g["updated"] = int(time.time())
    return g["name"], changed
