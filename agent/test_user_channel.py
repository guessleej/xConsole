#!/usr/bin/env python3
"""本機使用者通道（清單 #28）：系統代理開的 Unix socket／具名管道，給同一台的桌面版用。
驗：只放行使用者層的操作、不是坐在電腦前的人擋下（Unix）、每筆寫稽核且不記內容、
主控台端先確認通道是系統代理開的（擁有者不對就不連）、主控台經這條通道呼叫代理（call_node）。
跑法：python3 agent/test_user_channel.py（只用標準函式庫＋代理與主控台本身，Linux／macOS／Windows 都能跑）
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-uch-")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "console"))
import userchan  # noqa: E402
import xconsole_agent as A  # noqa: E402

OK = True
WIN = os.name == "nt"


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def call(method, path, body=None, owners=None):
    r = userchan.request(method, path, json.dumps(body or {}).encode() if method == "POST" else None,
                         where=WHERE, owners=owners or ME, timeout=10)
    with r:
        raw = r.read()
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        return r.status, {"raw": raw[:200]}


def audit():
    with open(A.USER_LOG, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh]


if WIN:
    WHERE = f"xconsole-test-{os.getpid()}"
    sid = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True).stdout.strip().split(",")[-1].strip('"')
    ME = userchan.OWNERS + [sid]
else:
    import pwd
    d = tempfile.mkdtemp(prefix="xc-uch-run-")
    os.chmod(d, 0o755)
    WHERE = os.path.join(d, "agent.sock")
    ME = [pwd.getpwuid(os.getuid()).pw_name]
    A.console_uids = lambda: {os.getuid()}

case("代理開起本機通道", A.start_user_channel(WHERE))
case("主控台端認得出這台有受管的系統代理", userchan.managed(WHERE, ME))
if not WIN:
    case("  socket 開 0666，誰能用由連線者身分決定", oct(os.stat(WHERE).st_mode & 0o777) == "0o666", oct(os.stat(WHERE).st_mode))
    bad = tempfile.mkdtemp(prefix="xc-uch-bad-")
    os.chmod(bad, 0o777)
    case("  資料夾別人寫得進去就不開（可能被先放了假通道），代理照常跑", A.start_user_channel(os.path.join(bad, "agent.sock")) is False)

st, out = call("GET", "/group/state")
case("使用者層的操作放行：看群組狀態與鎖定", st == 200 and "locked" in out, (st, out))
st, out = call("POST", "/chat/nope", {"messages": [{"role": "user", "content": "祕密內容"}]})
case("  對話會轉給模型（沒有這個欄位就照代理原本的回法，不是被通道擋）", st != 403, (st, out))
for method, path in (("POST", "/slot/main/model"), ("POST", "/library/delete"), ("GET", "/registry"),
                     ("GET", "/apikey/main"), ("POST", "/group/sync"), ("POST", "/agent/exec"), ("GET", "/logs/main")):
    st, out = call(method, path)
    case(f"  {method} {path}：拒絕，講明要請管理者", st == 403 and out.get("managed") and "管理者" in out.get("error", ""), (st, out))

log = audit()
case("每筆寫稽核：誰、做什麼、放行或拒絕", log and all(x["who"] and x["path"] and x["verdict"] for x in log)
     and any(x["verdict"] == "放行" for x in log) and any(x["verdict"].startswith("拒絕") for x in log), log[-3:])
with open(A.USER_LOG, encoding="utf-8") as fh:
    case("  稽核不記對話內容", "祕密內容" not in fh.read())
me = ME[-1] if WIN else ME[0]
case("  連線者身分是作業系統給的（就是跑測試的這個帳號）", any(me.split("\\")[-1].lower() in x["who"].lower() for x in log)
     if not WIN else all(x["who"] != "?" for x in log), [x["who"] for x in log[-2:]])

if not WIN and os.getuid() != 0:
    A.console_uids = lambda: set()
    st, out = call("GET", "/group/state")
    case("不是坐在這台電腦前的人：連得上也不給用", st == 403 and "坐在這台電腦前" in out.get("error", ""), (st, out))
    case("  拒絕也寫稽核", audit()[-1]["verdict"].startswith("拒絕：不是坐在"), audit()[-1])
    A.console_uids = lambda: {os.getuid()}

fake = ["S-1-5-21-0-0-0-1001"] if WIN else ["root"]
try:
    userchan.request("GET", "/healthz", where=WHERE, owners=fake, timeout=5)
    case("通道的擁有者不是系統代理的帳號：主控台不連", False)
except userchan.NotTrusted:
    case("通道的擁有者不是系統代理的帳號：主控台不連", True)

# 主控台經這條通道呼叫代理：fleet.json 裡 link=user 的節點走 call_node，不帶權杖、不走 TLS
import xconsole_server as X  # noqa: E402

os.makedirs(X.STATE_DIR, exist_ok=True)
with open(os.path.join(X.STATE_DIR, "fleet.json"), "w", encoding="utf-8") as fh:
    json.dump({"auth": {}, "nodes": [{"id": "local", "name": "本機", "group": "本機", "host": "127.0.0.1", "link": "user",
                                      "channel": WHERE, "channel_owners": ME}]}, fh)
st, out = X.call_node("local", "/group/state")
case("主控台 call_node 經本機通道拿得到狀態", st == 200 and "locked" in out, (st, out))
st, out = X.call_node("local", "/slot/main/model", "POST", {"engine": "llama", "model": "x/y"})
case("  經主控台換模型：代理拒絕，主控台把原因帶回畫面", st == 403 and out.get("managed"), (st, out))

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
