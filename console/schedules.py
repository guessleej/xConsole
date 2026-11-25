"""定時任務：到時間叫模型做一件事（查新聞、彙整、提醒），結果存成一則對話，也可以推送到手機或自家系統。

排程在主控台跑（常駐服務），不靠瀏覽器開著。這個檔只放規則與檢查（純邏輯，好測）；
實際叫模型、存對話、推送在 xconsole_server.py 的 sched_* 函式。

推送管道的令牌、Webhook網址屬於機密：跟任務一起以 AES-256-GCM 加密後存進主控台資料庫，畫面只回「已設定」。
"""
import re
import secrets
import time

CATCHUP = 12 * 3600          # 關機或主控台沒開時錯過的，12小時內補跑一次；更久就跳到下一次
MIN_EVERY = 5                # 「每隔一段時間」最短5分鐘
KEEP_RUNS = 20               # 每個任務留最近20筆執行紀錄
TASKS_MAX = 50               # 每個人最多幾個任務
CHANNELS_MAX = 20            # 每個人最多幾個推送管道
PROMPT_MAX = 4000
NAME_MAX = 60
RESULT_MAX = 20000           # 存進紀錄與推送的結果字數上限
TOOL_ROUNDS = 10             # 無人值守：最多10輪工具呼叫
WEEKDAYS = "一二三四五六日"
CATEGORY = "定時任務"         # 結果存成對話時放的分類
ID_RX = re.compile(r"[0-9a-f]{12}")
TIME_RX = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
CHAN_KINDS = ("telegram", "webhook")
TELEGRAM_TOKEN_RX = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,64}")
TELEGRAM_CHAT_RX = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}")


class Bad(Exception):
    pass


def _text(v, limit, name, required=False):
    if v is None or v == "":
        if required:
            raise Bad(f"請填{name}")
        return ""
    if not isinstance(v, str):
        raise Bad(f"{name}格式不對")
    v = v.strip()
    if len(v) > limit:
        raise Bad(f"{name}最多{limit}字")
    if required and not v:
        raise Bad(f"請填{name}")
    return v


def clean_rule(r, now=None):
    """{"kind": "daily", "time": "08:30"}／{"kind": "weekly", "days": [0..6], "time"}（0＝週一）／
    {"kind": "every", "minutes": N}／{"kind": "once", "at": 時間戳}。"""
    if not isinstance(r, dict) or r.get("kind") not in ("daily", "weekly", "every", "once"):
        raise Bad("排程方式不對")
    k = r["kind"]
    if k in ("daily", "weekly"):
        t = r.get("time")
        if not isinstance(t, str) or not TIME_RX.fullmatch(t):
            raise Bad("時間要寫成「時:分」，例如08:30")
        if k == "daily":
            return {"kind": k, "time": t}
        days = r.get("days")
        if not isinstance(days, list) or not days or any(not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6 for d in days):
            raise Bad("每週要至少選一天")
        return {"kind": k, "days": sorted(set(days)), "time": t}
    if k == "every":
        m = r.get("minutes")
        if not isinstance(m, int) or isinstance(m, bool) or not MIN_EVERY <= m <= 7 * 24 * 60:
            raise Bad(f"間隔要在{MIN_EVERY}分鐘到7天之間")
        return {"kind": k, "minutes": m}
    at = r.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        raise Bad("請選日期與時間")
    if at <= (now or time.time()):
        raise Bad("只跑一次的時間要在未來")
    return {"kind": k, "at": int(at)}


def next_after(rule, t):
    """這條規則在時間t之後（不含t）的下一次執行時間；沒有下一次（只跑一次而且已過）回None。照主控台這台的當地時間。"""
    k = rule["kind"]
    if k == "every":
        return t + rule["minutes"] * 60
    if k == "once":
        return rule["at"] if rule["at"] > t else None
    hh, mm = (int(x) for x in rule["time"].split(":"))
    lt = time.localtime(t)
    for add in range(0, 8):
        day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + add, hh, mm, 0, 0, 0, -1))
        if day <= t:
            continue
        if k == "daily" or time.localtime(day).tm_wday in rule["days"]:
            return day
    return None


def plan(task, now):
    """這一刻該做什麼：("run", 本來排定的時間)／("skip", 下一次)（錯過太久，跳過不補）／(None, 下一次)。
    anchor＝從哪個時間點往後算：建立、改排程、重新啟用、上次執行都會把它設成當下，所以不會因為改設定立刻補跑。"""
    if not task.get("enabled"):
        return None, None
    nxt = next_after(task["rule"], task["anchor"])
    if nxt is None or nxt > now:
        return None, nxt
    if now - nxt > CATCHUP:
        return "skip", next_after(task["rule"], now)
    return "run", nxt


def rule_text(rule):
    k = rule["kind"]
    if k == "daily":
        return f"每天{rule['time']}"
    if k == "weekly":
        days = rule["days"]
        label = "週一到週五" if days == [0, 1, 2, 3, 4] else "週末" if days == [5, 6] else "每天" if len(days) == 7 \
            else "每週" + "、".join(WEEKDAYS[d] for d in days)
        return f"{label}{rule['time']}"
    if k == "every":
        m = rule["minutes"]
        return f"每{m // 60}小時" if m % 60 == 0 else f"每{m}分鐘"
    return "只跑一次：" + time.strftime("%Y-%m-%d %H:%M", time.localtime(rule["at"]))


def clean_target(t):
    """{"kind": "local", "node", "slot"}／{"kind": "provider", "id", "model"?}"""
    if not isinstance(t, dict):
        raise Bad("請選要用哪個模型")
    if t.get("kind") == "local":
        node, slot = t.get("node"), t.get("slot")
        if not isinstance(node, str) or not re.fullmatch(r"[\w.-]{1,64}", node) or not isinstance(slot, str) \
                or not re.fullmatch(r"[\w.-]{1,64}", slot):
            raise Bad("模型欄位不對")
        return {"kind": "local", "node": node, "slot": slot}
    if t.get("kind") == "provider":
        pid = t.get("id")
        if not isinstance(pid, str) or not ID_RX.fullmatch(pid):
            raise Bad("第三方API不對")
        model = _text(t.get("model"), 200, "模型名稱")
        return {"kind": "provider", "id": pid, **({"model": model} if model else {})}
    raise Bad("請選要用哪個模型")


def clean_task(body, channel_ids, may_operate, now=None, old=None):
    """管理畫面送來的任務 → 乾淨的任務。old＝改之前的那筆（排程或啟用狀態有變才從現在重算）。"""
    now = now or time.time()
    if not isinstance(body, dict):
        raise Bad("格式不對")
    prompt = _text(body.get("prompt"), PROMPT_MAX, "要模型做的事", required=True)
    name = _text(body.get("name"), NAME_MAX, "名稱") or prompt.replace("\n", " ")[:16]
    rule = clean_rule(body.get("rule"), now)
    target = clean_target(body.get("target"))
    chans = body.get("channels") or []
    if not isinstance(chans, list) or any(c not in channel_ids for c in chans):
        raise Bad("推送管道不對（可能已被刪掉）")
    flags = {k: body.get(k) is True for k in ("web", "auto_start", "auto_stop")}
    flags["notify_fail"] = body.get("notify_fail", True) is not False
    if (flags["auto_start"] or flags["auto_stop"]) and not may_operate:
        raise Bad("自動啟動與關閉模型要維運或管理員帳號")
    if target["kind"] != "local":
        flags["auto_start"] = flags["auto_stop"] = False
    enabled = body.get("enabled", True) is not False
    out = {"name": name, "prompt": prompt, "rule": rule, "target": target, "channels": list(dict.fromkeys(chans)),
           "save_chat": body.get("save_chat", True) is not False, "enabled": enabled, **flags}
    if old is None or old.get("rule") != rule or (enabled and not old.get("enabled")):
        out["anchor"] = now
    else:
        out["anchor"] = old.get("anchor") or now
    return out


def new_id():
    return secrets.token_hex(6)


# ---------------------------------------------------------------- 推送管道

def clean_channel(body, old_secret=None, is_admin=False):
    """回 (公開欄位, 機密欄位)。機密沒重填就沿用舊的（畫面不回顯，只顯示「已設定」）。"""
    if not isinstance(body, dict) or body.get("kind") not in CHAN_KINDS:
        raise Bad("推送方式不對")
    kind = body["kind"]
    name = _text(body.get("name"), 40, "名稱") or {"telegram": "Telegram", "webhook": "Webhook"}[kind]
    old = old_secret or {}
    if kind == "telegram":
        token = _text(body.get("token"), 120, "Bot Token") or old.get("token")
        if not token:
            raise Bad("請填Bot Token（跟@BotFather要）")
        if not TELEGRAM_TOKEN_RX.fullmatch(token):
            raise Bad("Bot Token格式不對，應該像123456789:AA…")
        chat = _text(body.get("chat_id"), 40, "Chat ID", required=True)
        if not TELEGRAM_CHAT_RX.fullmatch(chat):
            raise Bad("Chat ID格式不對：個人與群組是數字（群組是負數），頻道可以寫@名稱")
        return {"kind": kind, "name": name, "chat_id": chat}, {"token": token}
    url = _text(body.get("url"), 500, "網址") or old.get("url")
    if not url:
        raise Bad("請填Webhook網址")
    if not re.fullmatch(r"https?://[^\s/?#]+[^\s]*", url):
        raise Bad("網址要是http://或https://開頭")
    if url.startswith("http://") and not is_admin:
        raise Bad("一般帳號的Webhook只能用https://")
    bearer = _text(body.get("bearer"), 500, "Bearer Token")
    if body.get("clear_bearer") is True:
        bearer = ""
    elif not bearer:
        bearer = old.get("bearer") or ""
    host = re.sub(r"^https?://", "", url).split("/", 1)[0].split("@")[-1]
    return {"kind": kind, "name": name, "host": host}, {"url": url, **({"bearer": bearer} if bearer else {})}


def push_payload(kind, title, content, chat_id=None):
    """各管道要送的內容（Telegram一則最多4096字，留一點空間給標題）。"""
    if kind == "telegram":
        text = f"{title}\n\n{content}"
        return {"chat_id": chat_id, "text": text[:4000] + ("…" if len(text) > 4000 else ""), "disable_web_page_preview": True}
    return {"title": title, "content": content, "source": "xConsole"}


def telegram_chats(updates):
    """getUpdates 的回應 → 最近傳過訊息給機器人的對象 [{id, name, type}]（使用者按「查找」挑一個）。"""
    out, seen = [], set()
    for u in reversed((updates or {}).get("result") or []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = msg.get("chat") or {}
        cid = c.get("id")
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        name = c.get("title") or " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or c.get("username") or str(cid)
        out.append({"id": str(cid), "name": name, "type": c.get("type") or ""})
    return out[:20]


def unattended_system(now, extra=""):
    """沒有人在電腦前時的系統提示：帶現在時間、不寒暄不反問、適合手機看。"""
    lt = time.localtime(now)
    head = (f"現在是{time.strftime('%Y-%m-%d %H:%M', lt)}（星期{WEEKDAYS[lt.tm_wday]}）。這是排定時間自動執行的任務，"
            "旁邊沒有人可以回答問題：不要寒暄、不要反問、不要說「我無法」，直接把事情做完並交出結果。"
            "結果會推送到手機，用條列、段落短一點；引用網路資料要附來源網址。用任務描述的語言回答，中文一律用繁體中文（台灣用語）。")
    return head + (f"\n\n使用者另外的指示：{extra}" if extra else "")


def strip_think(text):
    """有的模型把思考寫在<think>裡：推送與存檔只留正文。"""
    return re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S).strip()
