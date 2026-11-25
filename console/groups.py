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
HISTORY_MAX = 20
FIELD_NAMES = ("models", "allowed_models", "profile", "reserve_user_gib", "gpu_power_limit", "storage_root",
               "sources", "locked", "apply_at")


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


def _same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def diff(a, b):
    """兩份設定哪幾項不同，回欄位名稱的清單（依 FIELD_NAMES 的順序）。"""
    a, b = a or defaults(), b or defaults()
    return [k for k in FIELD_NAMES if not _same(a.get(k), b.get(k))]


def history(g):
    """這個群組留著的每一版設定，新的在前；每筆標出跟目前設定差在哪幾項。"""
    cur = g.get("settings") or defaults()
    return [dict(h, current=h["version"] == g["version"], diff=diff(h["settings"], cur))
            for h in reversed(g.get("history") or [])]


def rollback(g, version, by=""):
    """把某一版的設定拿回來，當成新的一版（版本號照樣往上加，代理才會重新對齊）。回 (目標那一筆, 設定有沒有變)。"""
    h = next((x for x in g.get("history") or [] if x["version"] == version), None)
    if h is None:
        raise LookupError(f"沒有 v{version} 這一版（只保留最近 {HISTORY_MAX} 版）")
    old = {k: v for k, v in h["settings"].items() if k in defaults()}   # 舊版沒有的新欄位由 check 補預設值
    _, changed = update(g, settings=old, by=by, note=f"退回 v{version}")
    return h, changed


def _record(g, by, note):
    hs = g.setdefault("history", [])
    if not hs:                                       # 第一次留紀錄：把改之前那一版補進來，才退得回去
        hs.append({"version": int(g.get("version") or 1), "settings": g.get("settings") or defaults(),
                   "at": int(g.get("updated") or time.time()), "by": "", "note": ""})
    return hs, {"by": str(by or "")[:64], "note": note}


def update(g, name=None, kind=None, settings=None, taken=(), by="", note=""):
    """改名、改種類、改設定，全部檢查過才動。設定內容真的不同才把版本號加一，並留一筆歷史（最多 HISTORY_MAX 版）。
    taken：別的群組已用的名稱；by：誰改的；note：這一版的說明（例：退回 v3）。回 (新名稱, 設定有沒有變)。"""
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
    changed = new_s is not None and not _same(new_s, g.get("settings") or defaults())
    now = int(time.time())
    if changed:
        hs, meta = _record(g, by, note)
        g["settings"] = new_s
        g["version"] = int(g.get("version") or 0) + 1
        hs.append(dict(meta, version=g["version"], settings=new_s, at=now))
        del hs[:-HISTORY_MAX]
    g["updated"] = now
    return g["name"], changed


# ---------------------------------------------------------------- 群組總覽（清單 #9）
# 每台的就緒狀態與群組的設定順序軸，都由主控台手上的真實資料算：機器在不在線、代理回報的對齊進度、身分。畫面不自己判斷。
APPLY_WAIT = {"waiting_boot": "已準備好，等下次開機套用", "waiting_time": "已準備好，等指定時間套用"}


def member_status(g, online, align, clone=None, rejoin=None, last_seen=None, admin=None):
    """一台機器在這個群組裡的狀態。依序判斷，越前面越要先處理：
    疑似複製 → 管理者撤銷身分、停用 → 離線 → 需要重新加入 → 放不下 → 失敗 → 就緒 → 準備中 → 設定不同。
    admin：管理者下的安全指令造成的狀態（{"code": "revoked"|"disabled", "at"}），離線也照樣標。
    align：代理回報的對齊狀態（group_public），沒有就是主控台還沒送到。回 {"code", "label", "detail", "pct"}。"""
    st = (align or {}).get("state") or {}
    err = (align or {}).get("error")
    same = st.get("group_id") == g["id"]
    v = g["version"]

    def out(code, label, detail="", pct=None):
        return {"code": code, "label": label, "detail": detail, "pct": pct,
                "prepared": bool(same and st.get("prepared", 0) >= v)}       # 套用中、套用失敗也已經準備好了
    if clone:
        return out("clone", "疑似複製", "同一個身分同時從兩個地方連回，已停止下指令")
    if admin:
        when = time.strftime("%m/%d %H:%M", time.gmtime(admin["at"] + 8 * 3600)) + "（台北時間）"
        if admin["code"] == "revoked":
            return out("revoked", "身分已撤銷", f"{when} 由管理者撤銷，要重新加入才能再管理")
        return out("disabled", "已停用", f"{when} 由管理者停用，恢復後才會套用群組設定" + ("" if online else "；目前離線"))
    if not online:
        return out("offline", "離線", f"上次連線 {last_seen}" if last_seen else "還沒連上過")
    if rejoin:
        return out("rejoin", "需要重新加入", rejoin)
    if err and not st:
        return out("failed", "設定送不過去", err)
    if not align:
        return out("preparing", "準備中", "主控台一分鐘內會把群組設定送過去")
    if same and st.get("phase") == "failed":
        nf = st.get("nofit")
        if nf:
            return out("nofit", "放不下", f"建議改用 {nf['suggest']}" if nf.get("suggest") else f"{nf.get('spec')} 放不下，請改小上下文或換小一點的量化")
        return out("failed", "失敗", st.get("error") or "")
    if same and st.get("applied", 0) >= v:
        return out("ready", "就緒", f"已套用 v{v}")
    if same and st.get("phase") in ("queued", "preparing", "applying"):
        run = next((s for s in reversed(st.get("steps") or []) if s.get("state") in ("run", "wait")), None)  # wait：等同群組互傳
        pct = run.get("pct") if run else None
        return out("preparing", "準備中", (run or {}).get("step", "") if pct is None else f"下載 {pct}%", pct)
    if same and st.get("prepared", 0) >= v:
        return out("differs", "設定不同", APPLY_WAIT.get(st.get("phase"), f"已準備 v{v}，還沒套用"))
    return out("differs", "設定不同", f"這台在 v{st.get('applied') or 0}，群組是 v{v}" if same else "還在用別的群組的設定")


def setup_steps(g, statuses):
    """這個群組從零到能用的五步，狀態由真實資料算：done／now／blocked／skip。"""
    n = len(statuses)
    ready = sum(1 for s in statuses if s["code"] == "ready")
    prepared = sum(1 for s in statuses if s["prepared"])
    edited = g.get("version", 1) > 1
    steps = [
        {"key": "create", "t": "建立群組", "why": "一間教室或一個部門一個群組，裡面的機器用同一份設定", "state": "done"},
        {"key": "join", "t": "加入機器", "why": f"已加入 {n} 台" if n else "產生群組加入碼，在每台執行安裝（大量部署用回應檔）",
         "state": "done" if n else "now"},
        {"key": "settings", "t": "填群組設定", "why": "選要放的模型、上下文與鎖定；不填就是每台維持原本的模型" if not edited
         else f"設定在 v{g['version']}", "state": "done" if edited else ("skip" if n else "blocked")},
        {"key": "prepare", "t": "每台下載並核對模型", "why": f"{prepared}／{n} 台已準備好", "state":
         "blocked" if not n else ("done" if prepared == n else "now")},
        {"key": "ready", "t": "套用並確認就緒", "why": f"{ready}／{n} 台就緒", "state":
         "blocked" if not n or prepared < n else ("done" if ready == n else "now")},
    ]
    for i, s in enumerate(steps):
        if s["state"] == "blocked":
            first = next((j for j, x in enumerate(steps[:i]) if x["state"] in ("now", "blocked")), None)
            s["need"] = (first + 1) if first is not None else None
    nxt = next((i for i, s in enumerate(steps) if s["state"] == "now"), None)
    return {"steps": steps, "done": sum(1 for s in steps if s["state"] in ("done", "skip")), "total": len(steps),
            "next": None if nxt is None else {"n": nxt + 1, "t": steps[nxt]["t"]}, "ready": ready, "members": n}
