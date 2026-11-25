"""即時問題回報（清單 #19）：長官或使用者看到數字不對、模型答錯、畫面壞掉，當下就能讓云碩知道。

一張工單從哪裡來、誰回報、哪一頁哪個數字，都由畫面自動帶入，回報人只寫一句話（可附截圖、錄音、錄影）。
送出當下回條（工單編號、等級、預計完成）；主管帳號送的自動列緊急，立刻寄信給通知名單上的云碩收件人；
緊急件過了初步回覆時限還沒人接手，寄給所有云碩收件人，之後每小時再寄，直到有人接手（夜間假日照寄）；
一般件逾期每天提醒一次；處理完通知回報人與學校窗口。信裡只放摘要，附件留在主控台這台。
通知名單或郵件伺服器沒設好，工單照建，管理頁顯示警示，不默默不寄。
"""
import email.utils
import hashlib
import json
import os
import re
import smtplib
import sqlite3
import ssl
import threading
import time
from email.message import EmailMessage

STATES = ("已受理", "處理中", "已完成", "已結案")
OPEN = ("已受理", "處理中")
LEVELS = ("緊急", "一般")
_EMAIL_BAD = set(" \t\r\n,;<>")


def is_email(s):
    """一個 @、網域至少兩段、不含空白與分隔符號。不用正規表示式：輸入是使用者給的，避免回溯。"""
    s = str(s or "")
    if not 3 <= len(s) <= 254 or s.count("@") != 1 or _EMAIL_BAD & set(s):
        return False
    local, domain = s.split("@")
    labels = domain.split(".")
    return bool(local) and len(labels) >= 2 and all(labels)
DEFAULT_CFG = {"recipients": [], "school": [], "seniors": [], "urgent_reply_hours": 1, "normal_days": 3}
ESCALATE_EVERY = 3600
OVERDUE_EVERY = 86400
TEXT_MAX = 500
ATTACH_MAX = 5
ATTACH_LIMIT = {"image": 10 << 20, "audio": 15 << 20, "video": 40 << 20}
MAGIC = ((b"\x89PNG", "image", "png"), (b"\xff\xd8\xff", "image", "jpg"), (b"GIF8", "image", "gif"),
         (b"ID3", "audio", "mp3"), (b"\xff\xfb", "audio", "mp3"), (b"OggS", "audio", "ogg"))


def sniff(data):
    """看檔頭決定是什麼，不信副檔名與瀏覽器給的類型。回 (類別, 副檔名) 或 None。"""
    for sig, kind, ext in MAGIC:
        if data.startswith(sig):
            return kind, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio", "wav"
    if data[4:8] == b"ftyp":
        return ("audio", "m4a") if data[8:12] in (b"M4A ", b"M4B ") else ("video", "mp4")
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video", "webm"
    return None


def check_cfg(body):
    """管理頁存的設定：收件人與學校窗口要是 email、主管帳號是登入帳號、時限有範圍。不合法丟 ValueError。"""
    out = dict(DEFAULT_CFG)
    for k, name in (("recipients", "云碩收件人"), ("school", "學校窗口")):
        rows = [str(x).strip() for x in body.get(k) or [] if str(x).strip()]
        bad = [x for x in rows if not is_email(x)]
        if bad or len(rows) > 30:
            raise ValueError(f"{name}要是 email（一行一個，最多 30 個）：{'、'.join(bad[:3])}")
        out[k] = list(dict.fromkeys(rows))
    seniors = [str(x).strip() for x in body.get("seniors") or [] if str(x).strip()]
    if len(seniors) > 50 or any(len(x) > 120 for x in seniors):
        raise ValueError("主管帳號最多 50 個")
    out["seniors"] = list(dict.fromkeys(seniors))
    for k, lo, hi, name in (("urgent_reply_hours", 0.25, 24, "緊急件初步回覆時限（小時）"), ("normal_days", 1, 30, "一般件預計完成（天）")):
        v = body.get(k, DEFAULT_CFG[k])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{name}要在 {lo}～{hi} 之間")
        out[k] = v
    return out


def send_mail(smtp, password, to, subject, text):
    """寄一封純文字信。smtp：{host, port, security（starttls／ssl／none）, user, from}。回 (成功, 原因)。"""
    if not smtp.get("host") or not to:
        return False, "郵件伺服器或收件人沒設"
    if smtp.get("security") == "none" and smtp.get("user"):
        return False, "不加密的郵件伺服器不送帳號密碼：要登入請改用 STARTTLS 或 SSL"
    msg = EmailMessage()
    msg["From"] = smtp.get("from") or smtp.get("user") or "xconsole@localhost"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(text)
    try:
        port = int(smtp.get("port") or (465 if smtp.get("security") == "ssl" else 587))
        ctx = ssl.create_default_context()
        if smtp.get("security") == "ssl":
            s = smtplib.SMTP_SSL(smtp["host"], port, timeout=20, context=ctx)
        else:
            s = smtplib.SMTP(smtp["host"], port, timeout=20)
            if smtp.get("security", "starttls") == "starttls":
                s.starttls(context=ctx)
        with s:
            if smtp.get("user"):
                s.login(smtp["user"], password)
            s.send_message(msg)
        return True, ""
    except (OSError, smtplib.SMTPException) as exc:
        return False, str(exc)[:200]


class Issues:
    def __init__(self, path, files_dir, mailer=None, background=True):
        self.path, self.files_dir = path, files_dir
        self.lock = threading.Lock()
        new = not os.path.exists(path)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if new:
            os.chmod(path, 0o600)
        self.db.execute("""CREATE TABLE IF NOT EXISTS issues(
            id INTEGER PRIMARY KEY, at REAL, by TEXT, role TEXT, level TEXT, text TEXT, page TEXT, target TEXT,
            context TEXT, state TEXT, checking INTEGER DEFAULT 0, due REAL, reply_due REAL, picked REAL,
            done REAL, replies TEXT, files TEXT, mails TEXT, last_escalate REAL, last_overdue REAL)""")
        self.mailer = mailer or (lambda *a, **k: (False, "沒有寄信程式"))
        self.background = background          # 寄信在背景做：回條當場回，不等郵件伺服器
        os.makedirs(files_dir, mode=0o700, exist_ok=True)

    # ---- 讀寫
    def _row(self, r):
        keys = ("id", "at", "by", "role", "level", "text", "page", "target", "context", "state", "checking", "due",
                "reply_due", "picked", "done", "replies", "files", "mails", "last_escalate", "last_overdue")
        d = dict(zip(keys, r))
        for k in ("context", "replies", "files", "mails"):
            d[k] = json.loads(d[k] or ("{}" if k == "context" else "[]"))
        d["checking"] = bool(d["checking"])
        return d

    def get(self, iid):
        r = self.db.execute("SELECT * FROM issues WHERE id=?", (int(iid),)).fetchone()
        return self._row(r) if r else None

    def _set(self, iid, **kv):
        for k in ("context", "replies", "files", "mails"):
            if k in kv:
                kv[k] = json.dumps(kv[k], ensure_ascii=False)
        if "checking" in kv:
            kv["checking"] = int(bool(kv["checking"]))
        cols = ", ".join(f"{k}=?" for k in kv)
        self.db.execute(f"UPDATE issues SET {cols} WHERE id=?", (*kv.values(), int(iid)))

    def rows(self, state=None, by=None, limit=200):
        q, args = "SELECT * FROM issues", []
        conds = []
        if state:
            conds.append("state=?")
            args.append(state)
        if by:
            conds.append("by=?")
            args.append(by)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id DESC LIMIT ?"
        return [self._row(r) for r in self.db.execute(q, (*args, int(limit))).fetchall()]

    # ---- 回報
    def submit(self, user, role, body, cfg, now=None):
        """建一張工單，當場回回條。主管帳號送的、或回報人勾「現在完全不能用」的列緊急。"""
        now = now or time.time()
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("寫一句話說明哪裡不對")
        if len(text) > TEXT_MAX:
            raise ValueError(f"說明最多 {TEXT_MAX} 字；細節可以附截圖或錄音")
        ctx = body.get("context") if isinstance(body.get("context"), dict) else {}
        ctx = {str(k)[:40]: str(v)[:300] for k, v in list(ctx.items())[:20]}
        senior = user in (cfg.get("seniors") or [])
        level = "緊急" if senior or body.get("blocking") is True else "一般"
        reply_due = now + float(cfg.get("urgent_reply_hours") or 1) * 3600 if level == "緊急" else now + 86400
        due = now + (86400 if level == "緊急" else float(cfg.get("normal_days") or 3) * 86400)
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO issues(at, by, role, level, text, page, target, context, state, due, reply_due, replies, files, mails)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, user, role, level, text, str(body.get("page") or "")[:120], str(body.get("target") or "")[:200],
                 json.dumps(ctx, ensure_ascii=False), "已受理", due, reply_due, "[]", "[]", "[]"))
            iid = cur.lastrowid
        if level == "緊急":
            self._later(iid, "urgent", cfg)
        it = self.get(iid)
        return {"id": iid, "level": level, "senior": senior, "due": due, "reply_due": reply_due, "state": it["state"]}

    def attach(self, iid, user, data, name=""):
        """附一個檔（截圖、錄音、錄影）：看檔頭決定類型，大小與個數有上限，檔案只存在這台。"""
        it = self.get(iid)
        if not it or it["by"] != user:
            raise LookupError("沒有這張工單")
        if len(it["files"]) >= ATTACH_MAX:
            raise ValueError(f"每張工單最多附 {ATTACH_MAX} 個檔")
        kind = sniff(data[:64])
        if not kind:
            raise ValueError("只收圖片（PNG、JPG、GIF、WebP）、錄音（MP3、M4A、WAV、OGG）與錄影（MP4、WebM）")
        if len(data) > ATTACH_LIMIT[kind[0]]:
            raise ValueError(f"這個檔太大：{kind[0]} 最多 {ATTACH_LIMIT[kind[0]] >> 20} MB")
        fid = hashlib.sha256(data).hexdigest()[:16]
        d = os.path.join(self.files_dir, str(int(iid)))
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = os.path.join(d, f"{fid}.{kind[1]}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        safe = re.sub(r"[^\w.\-]", "_", os.path.basename(str(name or "")))[:80] or f"{fid}.{kind[1]}"
        files = [f for f in it["files"] if f["id"] != fid] + [{"id": fid, "name": safe, "kind": kind[0], "ext": kind[1],
                                                                 "size": len(data)}]
        self._set(iid, files=files)
        return files[-1]

    def file_path(self, iid, fid):
        it = self.get(iid)
        f = next((x for x in (it or {}).get("files") or [] if x["id"] == fid), None)
        return (os.path.join(self.files_dir, str(int(iid)), f"{f['id']}.{f['ext']}"), f) if f else (None, None)

    # ---- 處理
    def update(self, iid, actor, cfg, state=None, reply=None, checking=None, now=None):
        now = now or time.time()
        it = self.get(iid)
        if not it:
            raise LookupError("沒有這張工單")
        kv = {}
        if reply:
            reply = str(reply).strip()[:2000]
            kv["replies"] = it["replies"] + [{"at": now, "by": actor, "text": reply}]
        if state:
            if state not in STATES:
                raise ValueError("進度只能是：" + "、".join(STATES))
            kv["state"] = state
            if state in ("已完成", "已結案") and not it["done"]:
                kv["done"] = now
        if (reply or (state and state != "已受理")) and not it["picked"]:
            kv["picked"] = now                    # 有人接手了：緊急件的催辦到這裡停
        if checking is not None:
            kv["checking"] = bool(checking)
        if kv:
            self._set(iid, **kv)
        if state == "已完成" and it["state"] != "已完成":
            self._later(iid, "resolved", cfg)
        elif reply:
            self._later(iid, "replied", cfg)
        return self.get(iid)

    # ---- 寄信
    def _summary(self, it, event):
        head = {"urgent": "緊急回報", "escalate": "緊急回報逾時沒人接手", "overdue": "回報逾期", "resolved": "回報已處理完",
                "replied": "回報有新回覆"}[event]
        lines = [f"[xConsole] {head}：#{it['id']}（{it['level']}）", "",
                 f"回報人：{it['by']}", f"時間：{time.strftime('%Y-%m-%d %H:%M', time.localtime(it['at']))}",
                 f"頁面：{it['page'] or '—'}", f"對象：{it['target'] or '—'}", f"說明：{it['text'][:200]}",
                 f"進度：{it['state']}{'（查核中）' if it['checking'] else ''}"]
        if it["replies"]:
            lines.append(f"最新回覆：{it['replies'][-1]['text'][:200]}")
        if it["files"]:
            lines.append(f"附件 {len(it['files'])} 個：留在主控台，請登入後到「設定 › 問題回報」查看")
        return lines[0], "\n".join(lines)

    def _to(self, it, event, cfg):
        if event in ("urgent", "escalate"):
            return list(cfg.get("recipients") or [])
        if event == "overdue":
            return list(dict.fromkeys((cfg.get("recipients") or []) + [s for s in cfg.get("seniors") or [] if is_email(s)]))
        reporter = [it["by"]] if is_email(it["by"] or "") else []
        return list(dict.fromkeys(reporter + (cfg.get("school") or [] if event == "resolved" else [])))

    def _later(self, iid, event, cfg):
        if self.background:
            threading.Thread(target=self.notify, args=(iid, event, cfg), daemon=True).start()
        else:
            self.notify(iid, event, cfg)

    def notify(self, iid, event, cfg):
        it = self.get(iid)
        to = self._to(it, event, cfg)
        subject, text = self._summary(it, event)
        ok, why = self.mailer(to, subject, text) if to else (False, "沒有收件人")
        mails = it["mails"] + [{"at": time.time(), "event": event, "to": to, "ok": ok, "error": why}]
        self._set(iid, mails=mails[-50:])
        return ok

    def escalate_scan(self, cfg, now=None):
        """緊急件過了初步回覆時限還沒人接手：寄給所有云碩收件人，之後每小時再寄，直到有人接手。用實際時間，夜間假日照寄。"""
        now = now or time.time()
        sent = []
        for it in self.rows(state="已受理", limit=500):
            if it["level"] != "緊急" or it["picked"] or now < it["reply_due"]:
                continue
            if it["last_escalate"] and now - it["last_escalate"] < ESCALATE_EVERY:
                continue
            self._set(it["id"], last_escalate=now)
            self.notify(it["id"], "escalate", cfg)
            sent.append(it["id"])
        return sorted(sent)

    def overdue_scan(self, cfg, now=None):
        """一般件過了預計完成還沒處理完：每天提醒一次云碩收件人與主管。"""
        now = now or time.time()
        sent = []
        for it in self.rows(limit=500):
            if it["level"] != "一般" or it["state"] not in OPEN or now < it["due"]:
                continue
            if it["last_overdue"] and now - it["last_overdue"] < OVERDUE_EVERY:
                continue
            self._set(it["id"], last_overdue=now)
            self.notify(it["id"], "overdue", cfg)
            sent.append(it["id"])
        return sorted(sent)

    def checking(self):
        """查核中的對象：用到這些數字的畫面要提醒。"""
        return sorted({it["target"] for it in self.rows(limit=500) if it["checking"] and it["state"] in OPEN and it["target"]})

    def status(self, cfg, smtp_ok):
        """管理頁的警示：通知名單或郵件伺服器沒設、最近一次寄信失敗，都要看得到。"""
        warn = []
        if not cfg.get("recipients"):
            warn.append("通知名單上沒有云碩收件人：緊急回報寄不出去")
        if not smtp_ok:
            warn.append("郵件伺服器還沒設好：工單照建，但不會寄信")
        last = next((m for it in self.rows(limit=50) for m in reversed(it["mails"]) if m.get("to")), None)
        if last and not last["ok"]:
            warn.append(f"最近一次寄信失敗（{time.strftime('%m/%d %H:%M', time.localtime(last['at']))}）：{last['error']}")
        opened = self.rows(limit=500)
        return {"warnings": warn, "open": sum(1 for x in opened if x["state"] in OPEN),
                "urgent_open": sum(1 for x in opened if x["state"] in OPEN and x["level"] == "緊急")}
