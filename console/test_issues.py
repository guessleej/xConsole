#!/usr/bin/env python3
"""即時問題回報（清單 #19）。不連外：寄信用本機的假郵件伺服器。
一、模組：回條、主管自動列緊急、勾「完全不能用」列緊急、說明長度、附件看檔頭與大小與個數、只有回報人能附、
    緊急件逾時沒人接手催辦且之後每小時再寄、有人接手就停、一般件逾期每天提醒、處理完通知回報人與學校窗口、
    查核中、管理頁警示（沒收件人、沒郵件伺服器、最近寄信失敗）、設定檢查。
二、主控台：設定存檔（SMTP 密碼加密落地）、主管送的回報立刻寄信且信裡只有摘要、附件原始檔上傳、測試信、
    工單清單與更新、使用者看得到自己的回條。
跑法：python3 test_issues.py
"""
import http.cookiejar
import json
import os
import secrets
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import issues  # noqa: E402

OK = True
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


SENT = []
I = issues.Issues(os.path.join(tempfile.mkdtemp(prefix="xc-issues-"), "i.db"), tempfile.mkdtemp(prefix="xc-ifiles-"),
                  lambda to, subj, text: (SENT.append((to, subj, text)) or (True, "")), background=False)
CFG = issues.check_cfg({"recipients": ["ops@cloudinfo.com.tw"], "school": ["ir@school.edu.tw"],
                        "seniors": ["president@school.edu.tw"], "urgent_reply_hours": 1, "normal_days": 3})
T0 = 1_800_000_000.0

r = I.submit("teacher@school.edu.tw", "user", {"text": "這台的記憶體數字不對", "page": "home", "target": "lab-1・記憶體可用",
                                               "context": {"機器": "lab-1"}}, CFG, now=T0)
check("回條：工單編號、一般件、預計完成", r["id"] == 1 and r["level"] == "一般" and r["due"] == T0 + 3 * 86400 and not SENT, r)
r2 = I.submit("president@school.edu.tw", "user", {"text": "首頁的使用人數少了一半"}, CFG, now=T0)
check("主管帳號送的：自動列緊急，立刻寄信給云碩收件人", r2["level"] == "緊急" and r2["senior"]
      and SENT[-1][0] == ["ops@cloudinfo.com.tw"] and "緊急回報" in SENT[-1][1])
check("  信裡只有摘要（回報人、頁面、說明），附件不寄", "首頁的使用人數少了一半" in SENT[-1][2] and "附件" not in SENT[-1][2])
r3 = I.submit("student@school.edu.tw", "user", {"text": "對話完全沒反應", "blocking": True}, CFG, now=T0)
check("  勾「現在完全不能用」也列緊急", r3["level"] == "緊急")
for bad, word in (({"text": ""}, "一句話"), ({"text": "長" * 600}, "最多")):
    try:
        I.submit("a", "user", bad, CFG)
        ok = False
    except ValueError as exc:
        ok = word in str(exc)
    check(f"說明檢查：{word}", ok)

f = I.attach(1, "teacher@school.edu.tw", PNG, "../../截圖.png")
check("附件：看檔頭是圖片才收，檔名不帶路徑", f["kind"] == "image" and "/" not in f["name"] and ".." not in f["name"])
path, _ = I.file_path(1, f["id"])
check("  附件只存在主控台這台、只有自己讀得到", path and os.path.exists(path) and (os.name == "nt" or (os.stat(path).st_mode & 0o077) == 0))
for data, who, word in ((b"#!/bin/sh\nrm -rf /", "teacher@school.edu.tw", "只收"), (PNG, "someone-else", "沒有這張"),
                        (b"\x89PNG" + b"0" * (11 << 20), "teacher@school.edu.tw", "太大")):
    try:
        I.attach(1, who, data, "x")
        ok = False
    except (ValueError, LookupError) as exc:
        ok = word in str(exc)
    check(f"  附件擋下：{word}", ok)
for k in range(4):
    I.attach(1, "teacher@school.edu.tw", PNG + bytes([k]), f"{k}.png")
try:
    I.attach(1, "teacher@school.edu.tw", PNG + b"x", "6.png")
    ok = False
except ValueError:
    ok = True
check("  每張工單最多 5 個附件", ok)

SENT.clear()
check("緊急件還沒到初步回覆時限：不催", I.escalate_scan(CFG, now=T0 + 1800) == [])
check("  過了時限還沒人接手：寄給所有云碩收件人", I.escalate_scan(CFG, now=T0 + 3700) == [2, 3] and len(SENT) == 2
      and all(s[0] == ["ops@cloudinfo.com.tw"] for s in SENT))
check("  一小時內不重寄", I.escalate_scan(CFG, now=T0 + 5000) == [])
check("  之後每小時再寄（夜間假日照寄）", I.escalate_scan(CFG, now=T0 + 3700 + 3600) == [2, 3])
I.update(2, "ops", CFG, reply="收到，正在查", now=T0 + 8000)
check("  有人接手（回覆了）就不再催", I.escalate_scan(CFG, now=T0 + 3700 + 7200) == [3])
SENT.clear()
check("一般件還沒過預計完成：不提醒", I.overdue_scan(CFG, now=T0 + 86400) == [])
check("  逾期：提醒云碩收件人與主管", I.overdue_scan(CFG, now=T0 + 3 * 86400 + 60) == [1]
      and set(SENT[-1][0]) == {"ops@cloudinfo.com.tw", "president@school.edu.tw"})
check("  一天只提醒一次", I.overdue_scan(CFG, now=T0 + 3 * 86400 + 3600) == [])
SENT.clear()
I.update(1, "ops", CFG, state="已完成", reply="已修正，是取樣時間差造成", now=T0 + 4 * 86400)
check("處理完：通知回報人與學校窗口", SENT and set(SENT[-1][0]) == {"teacher@school.edu.tw", "ir@school.edu.tw"}
      and "已處理完" in SENT[-1][1])
check("  處理完的逾期件不再提醒", I.overdue_scan(CFG, now=T0 + 9 * 86400) == [])
I.update(3, "ops", CFG, checking=True)
I.submit("x", "user", {"text": "y", "target": "lab-2・溫度"}, CFG)
I.update(4, "ops", CFG, checking=True)
check("查核中：列出用到這個數字的對象", I.checking() == ["lab-2・溫度"], I.checking())

st = I.status({"recipients": []}, False)
check("管理頁警示：沒有云碩收件人、郵件伺服器沒設", len(st["warnings"]) == 2 and st["open"] >= 2, st)
J = issues.Issues(os.path.join(tempfile.mkdtemp(), "j.db"), tempfile.mkdtemp(), lambda *a: (False, "連不到 smtp"), background=False)
J.submit("president@school.edu.tw", "user", {"text": "x"}, CFG)
check("  最近一次寄信失敗也要看得到（工單照建）", any("寄信失敗" in w for w in J.status(CFG, True)["warnings"]) and J.get(1))
for bad, word in (({"recipients": ["不是email"]}, "email"), ({"urgent_reply_hours": 0}, "初步回覆"), ({"normal_days": 99}, "預計完成")):
    try:
        issues.check_cfg(bad)
        ok = False
    except ValueError as exc:
        ok = word in str(exc)
    check(f"設定檢查：{word}", ok)
ok, why = issues.send_mail({"host": "127.0.0.1", "port": 1, "security": "none", "user": "u"}, "pw", ["a@b.tw"], "s", "t")
check("不加密的郵件伺服器不送帳號密碼", not ok and "STARTTLS" in why)


# ---- 主控台：用本機的假郵件伺服器收信
INBOX = []


class FakeSMTP(socketserver.StreamRequestHandler):
    def handle(self):
        w = self.wfile.write
        w(b"220 fake ESMTP\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.strip().upper()
            if cmd.startswith((b"EHLO", b"HELO")):
                w(b"250 fake\r\n")
            elif cmd.startswith(b"DATA"):
                w(b"354 go\r\n")
                buf = b""
                while not buf.endswith(b"\r\n.\r\n"):
                    chunk = self.rfile.readline()
                    if not chunk:
                        return
                    buf += chunk
                INBOX.append(buf.decode("utf-8", "replace"))
                w(b"250 ok\r\n")
            elif cmd.startswith(b"QUIT"):
                w(b"221 bye\r\n")
                return
            else:
                w(b"250 ok\r\n")


smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSMTP)
threading.Thread(target=smtp.serve_forever, daemon=True).start()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


W = tempfile.mkdtemp(prefix="xc-issues-console-")
chome = os.path.join(W, "console")
os.makedirs(os.path.join(chome, ".xconsole"), mode=0o700)
port, lport = free_port(), free_port()
env = {**os.environ, "HOME": chome, "XCONSOLE_HOME": chome, "XCONSOLE_PORT": str(port), "XCONSOLE_LINK_PORT": str(lport)}
srv = os.path.join(HERE, "xconsole_server.py")
subprocess.run([sys.executable, srv, "--set-credentials"], input=b"admin\nissues-pass-2026\n", env=env, check=True, capture_output=True)
cons = subprocess.Popen([sys.executable, srv], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(100):
        try:
            ssl.get_server_certificate(("127.0.0.1", port), timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    ctx = ssl.create_default_context(cafile=os.path.join(chome, ".xconsole", "tls", "cert.pem"))
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx),
                                     urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = headers or {"Content-Type": "application/json"}
        req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", data=data, method="POST" if data is not None else "GET", headers=h)
        try:
            with op.open(req, timeout=30) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    call("/api/login", {"username": "admin", "password": "issues-pass-2026"})
    secret = "smtp-" + secrets.token_hex(6)
    st, out = call("/api/issues/settings", {"cfg": {"recipients": ["ops@cloudinfo.com.tw"], "seniors": ["admin"]},
                                            "smtp": {"host": "127.0.0.1", "port": smtp.server_address[1], "security": "none",
                                                     "user": "", "password": secret, "from": "xconsole@cloudinfo.com.tw"}})
    check("主控台：存通知名單與郵件伺服器", st == 200, out)
    with open(os.path.join(chome, ".xconsole", "fleet.json"), encoding="utf-8") as fh:
        check("  郵件伺服器密碼加密落地（設定檔找不到明文）", secret not in fh.read())
    st, out = call("/api/issues")
    check("  管理頁不回密碼，只說有沒有設", st == 200 and out["smtp"]["password_set"] and secret not in json.dumps(out))
    st, r = call("/api/issues", {"text": "總覽的 GPU 溫度一直是 0 度，金鑰 sk-abcdefghijklmnop1234", "page": "home", "target": "lab-1・GPU 溫度"})
    check("  主管帳號送的回報：回條列緊急", st == 200 and r["level"] == "緊急" and r["id"] == 1, r)
    for _ in range(50):
        if INBOX:
            break
        time.sleep(0.2)
    check("  立刻寄信給云碩收件人", INBOX and "ops@cloudinfo.com.tw" in INBOX[0])
    check("  信裡的金鑰已遮罩", INBOX and "sk-abcdefghijklmnop1234" not in INBOX[0] and "已遮罩" in INBOX[0])
    up = {"Content-Type": "application/octet-stream", "X-XConsole-Upload": "1", "X-Filename": "screen.png"}
    st, out = call("/api/issues/1/attach", raw=PNG, headers=up)
    check("  附截圖（原始檔直接送）", st == 200 and out["file"]["kind"] == "image", out)
    check("  不是圖片、錄音、錄影的檔：400", call("/api/issues/1/attach", raw=b"MZ\x90\x00exe", headers=up)[0] == 400)
    check("  沒帶上傳標頭：415", call("/api/issues/1/attach", raw=PNG, headers={"Content-Type": "application/octet-stream"})[0] == 415)
    st, out = call("/api/issues")
    it = out["items"][0]
    check("  工單清單：說明、對象、附件、寄信紀錄", it["target"] == "lab-1・GPU 溫度" and len(it["files"]) == 1 and it["mails"][0]["ok"], it)
    st, out = call("/api/issues/1/update", {"state": "處理中", "checking": True, "reply": "正在查感測器"})
    check("  改進度、標查核中、回覆", st == 200 and out["item"]["checking"] and out["item"]["state"] == "處理中")
    check("  查核中的對象大家都看得到", call("/api/issues/checking")[1]["targets"] == ["lab-1・GPU 溫度"])
    check("  回報人看得到自己的回條與回覆", call("/api/issues/mine")[1]["items"][0]["replies"][0]["text"] == "正在查感測器")
    INBOX.clear()
    st, out = call("/api/issues/test-mail", {})
    check("測試信：寄給云碩收件人", st == 200 and out["ok"] and INBOX, out)
    check("設定檢查：收件人不是 email → 400", call("/api/issues/settings", {"cfg": {"recipients": ["abc"]}, "smtp": {}})[0] == 400)
finally:
    cons.terminate()
    smtp.shutdown()

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
