#!/usr/bin/env python3
"""定時任務與推送（LocModel 3.6.2比對5.2第二批）。驗：
- 排程規則：每天、每週、每隔、只跑一次的檢查與下次時間；錯過12小時內補跑、更久跳過；改排程或重新啟用從現在算
- 執行：模型叫工具（查時間、計算機）、濾掉<think>、存成「定時任務」分類的對話、推送到Telegram與Webhook、寫紀錄
- 安全：令牌與網址加密落地（資料庫裡找不到明文）、Bearer走標頭、一般帳號的Webhook只能https且只能連網際網路、
  每個人只看得到自己的任務與管道、一般帳號不能自動啟動模型也不能用第三方API、群組範圍外的機器不能排
- 計算機只算數學，丟不出程式碼或算到當機的式子
跑法：python3 test_schedules.py（隔離 HOME，不連外）
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-sched-")
os.environ["XCONSOLE_HOME"] = TMP
os.environ["HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "agent")]
import chattools  # noqa: E402
import schedules as SC  # noqa: E402
import xconsole_server as x  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OK = True
ST = os.path.join(HERE, "static")


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


def bad(fn, *a, **k):
    try:
        fn(*a, **k)
    except SC.Bad as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------- 規則（純邏輯）
NOW = time.mktime((2026, 11, 26, 10, 0, 0, 0, 0, -1))          # 2026-11-26（週四）10:00 當地時間
check("每天：時間要寫成時:分", bad(SC.clean_rule, {"kind": "daily", "time": "8:30"}) and not bad(SC.clean_rule, {"kind": "daily", "time": "08:30"}))
check("每週：至少選一天", bad(SC.clean_rule, {"kind": "weekly", "days": [], "time": "08:30"}) is not None)
check("每隔：最短5分鐘", bad(SC.clean_rule, {"kind": "every", "minutes": 4}) and not bad(SC.clean_rule, {"kind": "every", "minutes": 5}))
check("只跑一次：要在未來", bad(SC.clean_rule, {"kind": "once", "at": NOW - 60}, NOW) and not bad(SC.clean_rule, {"kind": "once", "at": NOW + 60}, NOW))
lt = lambda t: time.localtime(t)  # noqa: E731
n = SC.next_after({"kind": "daily", "time": "08:30"}, NOW)
check("每天08:30：今天已過就是明天08:30", (lt(n).tm_mday, lt(n).tm_hour, lt(n).tm_min) == (27, 8, 30), time.ctime(n))
n = SC.next_after({"kind": "daily", "time": "10:30"}, NOW)
check("  今天還沒到就是今天", (lt(n).tm_mday, lt(n).tm_hour) == (26, 10))
n = SC.next_after({"kind": "weekly", "days": [0], "time": "09:00"}, NOW)
check("每週一09:00：下週一", lt(n).tm_wday == 0 and lt(n).tm_mday == 30, time.ctime(n))
check("每隔2小時：從起算點加2小時", SC.next_after({"kind": "every", "minutes": 120}, NOW) == NOW + 7200)
check("只跑一次：過了就沒有下一次", SC.next_after({"kind": "once", "at": NOW + 10}, NOW + 20) is None)
task = {"enabled": True, "rule": {"kind": "daily", "time": "08:30"}, "anchor": NOW - 86400}
check("到期就跑（排定的是今天08:30）", SC.plan(task, NOW)[0] == "run")
check("  錯過超過12小時就跳過，改排下一次", SC.plan(task, NOW + 13 * 3600)[0] == "skip")
check("  暫停的不跑", SC.plan({**task, "enabled": False}, NOW)[0] is None)
check("  起算點是現在就不會立刻跑", SC.plan({**task, "anchor": NOW}, NOW)[0] is None)
body = {"prompt": "查天氣", "rule": {"kind": "daily", "time": "08:30"}, "target": {"kind": "local", "node": "n1", "slot": "main"}}
old = {**SC.clean_task(body, set(), False, NOW - 9999), "anchor": NOW - 9999}
check("改了名稱、排程沒變：起算點不變（不會因為改設定立刻補跑或漏跑）", SC.clean_task({**body, "name": "新名"}, set(), False, NOW, old)["anchor"] == NOW - 9999)
check("  改了排程：從現在重算", SC.clean_task({**body, "rule": {"kind": "daily", "time": "09:00"}}, set(), False, NOW, old)["anchor"] == NOW)
check("  名稱空白就用要做的事的前幾個字", SC.clean_task(body, set(), False, NOW)["name"] == "查天氣")
check("  一般帳號不能勾自動啟動模型", bad(SC.clean_task, {**body, "auto_start": True}, set(), False, NOW) is not None)
check("  推送管道要是自己的", bad(SC.clean_task, {**body, "channels": ["ffffffffffff"]}, set(), False, NOW) is not None)
check("排程文字", (SC.rule_text({"kind": "weekly", "days": [0, 1, 2, 3, 4], "time": "08:00"}), SC.rule_text({"kind": "every", "minutes": 120}),
                SC.rule_text({"kind": "every", "minutes": 30})) == ("週一到週五08:00", "每2小時", "每30分鐘"))
TOKEN = "123456789:AAH" + "x" * 32
check("Telegram：Token與Chat ID格式", bad(SC.clean_channel, {"kind": "telegram", "token": "abc", "chat_id": "1"})
      and bad(SC.clean_channel, {"kind": "telegram", "token": TOKEN, "chat_id": "abc"})
      and not bad(SC.clean_channel, {"kind": "telegram", "token": TOKEN, "chat_id": "-100123"}))
pub, sec = SC.clean_channel({"kind": "telegram", "chat_id": "42", "name": "手機"}, {"token": TOKEN})
check("  沒重填Token就沿用舊的（畫面不回顯）", sec == {"token": TOKEN} and "token" not in pub)
check("Webhook：一般帳號只能https", bad(SC.clean_channel, {"kind": "webhook", "url": "http://example.com/x"}) is not None
      and not bad(SC.clean_channel, {"kind": "webhook", "url": "http://n8n.lan/x"}, None, True))
check("Telegram查找：最近傳過訊息的對象、去重", SC.telegram_chats({"result": [
    {"message": {"chat": {"id": 1, "first_name": "小明", "type": "private"}}},
    {"message": {"chat": {"id": -100, "title": "家人群組", "type": "group"}}},
    {"message": {"chat": {"id": 1, "first_name": "小明", "type": "private"}}}]}) == [
    {"id": "1", "name": "小明", "type": "private"}, {"id": "-100", "name": "家人群組", "type": "group"}])
check("濾掉<think>，只留正文", SC.strip_think("<think>想很久</think>\n結果") == "結果" and SC.strip_think("<think>只有想") == "")
check("Telegram一則不超過4096字", len(SC.push_payload("telegram", "t", "字" * 9000, "1")["text"]) <= 4096)

# ---------------------------------------------------------------- 計算機
check("計算機：四則、次方、函式", (chattools.calculate("1+2*3"), chattools.calculate("2^10"), chattools.calculate("sqrt(16)")) == (7, 1024, 4.0))
for e in ('__import__("os").system("x")', "().__class__", "9**9**9", "a+1", "[1]*10**9"):
    try:
        chattools.calculate(e)
        check(f"  擋下：{e}", False)
    except chattools.CalcError:
        check(f"  擋下：{e}", True)

# ---------------------------------------------------------------- 主控台：API、權限、執行
x.update_fleet(lambda c: c.update(nodes=[{"id": "n1", "name": "本機", "host": "127.0.0.1", "group": "g-a"},
                                          {"id": "n2", "name": "別間教室", "host": "127.0.0.2", "group": "g-b"}]))
x.set_credentials("admin", "sched-test-pw-1")
srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

SLOT = {"id": "main", "busy": True, "state": "active", "model": "gemma", "kind": "llama", "unit": "u"}
CALLS = []


def fake_node(node_id, path, method="GET", body=None, timeout=40):
    CALLS.append((node_id, path))
    if path == "/state":
        return 200, {"slots": [dict(SLOT)]}
    if path.startswith("/slot/"):
        SLOT["busy"] = path.endswith("start")
        return 200, {"ok": True}
    return 404, {"error": "沒有"}


MODEL = []


def fake_model(t, req):
    MODEL.append(req)
    if len(MODEL) % 2 == 1:
        return 200, {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "calculator", "arguments": json.dumps({"expression": "6*7"})}},
            {"id": "c2", "type": "function", "function": {"name": "run_command", "arguments": "{}"}}]}}]}
    return 200, {"choices": [{"message": {"content": "<think>算一下</think>答案是42。"}}]}


PUSH = []


def fake_push(method, url, payload=None, headers=None, timeout=20, allow=chattools.ip_ok):
    PUSH.append({"method": method, "url": url, "payload": payload, "headers": headers or {}, "allow": allow})
    if "telegram" in url:
        return 200, {"ok": True}, ""
    return 204, None, ""


x.call_node = fake_node
x.agent_model_call = fake_model
x.chattools.request_json = fake_push
x.fleet_state = lambda: {"nodes": [{"id": "n1", "name": "本機", "online": True, "state": {"slots": [dict(SLOT)]}}]}


class Client:
    def __init__(self):
        self.cookie = ""

    def req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if self.cookie:
            r.add_header("Cookie", "xconsole=" + self.cookie)
        try:
            resp = urllib.request.urlopen(r, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        raw = resp.read()
        sc = resp.headers.get("Set-Cookie") or ""
        if sc.startswith("xconsole="):
            self.cookie = sc.split(";")[0].split("=", 1)[1]
        return resp.status, json.loads(raw or b"{}")


def login(name, role, scope=None):
    st, out = admin.req("POST", "/api/users/create", {"username": name, "display": name, "role": role, **({"scope": scope} if scope else {})})
    c = Client()
    c.req("POST", "/api/login", {"username": name, "password": out["password"]})
    c.req("POST", "/api/account/password", {"old": out["password"], "new": "sched-pass-0001"})
    c.req("POST", "/api/login", {"username": name, "password": "sched-pass-0001"})
    return c


admin = Client()
admin.req("POST", "/api/login", {"username": "admin", "password": "sched-test-pw-1"})
user = login("s1@cloudinfo.com.tw", "user")
other = login("s2@cloudinfo.com.tw", "user")

st, ch = user.req("POST", "/api/push/save", {"kind": "telegram", "name": "我的手機", "token": TOKEN, "chat_id": "42"})
check("一般帳號建得出Telegram管道，回應不含Token", st == 200 and ch.get("secret_set") and TOKEN not in json.dumps(ch), (st, ch))
st, wh = user.req("POST", "/api/push/save", {"kind": "webhook", "name": "自家系統", "url": "https://hooks.example.com/abc?secret=zzz", "bearer": "bearer-xyz-123"})
check("  Webhook：回應只露出主機名稱", st == 200 and wh.get("host") == "hooks.example.com" and "secret=zzz" not in json.dumps(wh), wh)
st, out = user.req("POST", "/api/push/save", {"kind": "webhook", "url": "http://hooks.example.com/x"})
check("  一般帳號不能用http://的Webhook", st == 400)
st, out = other.req("POST", f"/api/push/{ch['id']}/test", {})
check("  別人的管道測不到也刪不到", st == 404 and other.req("POST", f"/api/push/{ch['id']}/delete", {})[0] == 404)

job = {"name": "算數", "prompt": "幫我算6*7", "rule": {"kind": "daily", "time": "08:30"}, "target": {"kind": "local", "node": "n1", "slot": "main"},
       "channels": [ch["id"], wh["id"]]}
st, out = user.req("POST", "/api/schedules/save", {**job, "auto_start": True})
check("一般帳號不能勾自動啟動模型", st == 400 and "維運或管理員" in out.get("error", ""), out)
st, out = user.req("POST", "/api/schedules/save", {**job, "target": {"kind": "provider", "id": "a" * 12}})
check("  一般帳號不能用第三方API排任務", st == 400, out)
st, saved = user.req("POST", "/api/schedules/save", job)
tid = saved.get("id")
check("一般帳號建得出任務", st == 200 and tid, saved)
st, lst = other.req("GET", "/api/schedules")
check("  別人看不到這個任務，也動不了", not lst["tasks"] and other.req("POST", f"/api/schedules/{tid}/run", {})[0] == 404)
raw = b"".join(open(f, "rb").read() for f in (x.STORE.path, x.STORE.path + "-wal") if os.path.exists(f))
check("資料庫裡找不到Token、Webhook網址、Bearer、提示詞的明文",
      all(s.encode() not in raw for s in (TOKEN, "hooks.example.com/abc", "bearer-xyz-123", "幫我算6*7")))

x.sched_execute("s1@cloudinfo.com.tw", tid, True)
st, lst = user.req("GET", "/api/schedules")
run = (lst["tasks"][0]["runs"] or [{}])[0]
check("執行：模型用計算機算出結果、濾掉思考", run.get("ok") and run.get("result") == "答案是42。", run)
check("  模型叫了任務沒開放的工具（執行指令）就回拒絕，不會執行", any(s["tool"] == "run_command" and s.get("error") for s in run.get("steps") or [])
      and not any(p for _, p in CALLS if "agent" in p), run.get("steps"))
check("  系統提示帶了現在時間、不准反問", "現在是" in MODEL[0]["messages"][0]["content"] and "不要反問" in MODEL[0]["messages"][0]["content"])
check("  工具只給查時間與計算機（沒勾聯網）", {t["function"]["name"] for t in MODEL[0]["tools"]} == {"current_time", "calculator"})
tg = next(p for p in PUSH if "telegram" in p["url"])
hk = next(p for p in PUSH if "hooks.example.com" in p["url"])
check("  推送到Telegram：送到設定的Chat ID、內容是結果", tg["payload"]["chat_id"] == "42" and "答案是42" in tg["payload"]["text"])
check("  推送到Webhook：JSON格式、Bearer放在標頭不在網址", hk["payload"] == {"title": "算數", "content": "答案是42。", "source": "xConsole"}
      and hk["headers"].get("Authorization") == "Bearer bearer-xyz-123" and "bearer" not in hk["url"])
check("  一般帳號的Webhook只能連網際網路（內網位址擋掉）", hk["allow"] is chattools.ip_ok and not hk["allow"]("192.168.1.5"))
check("  兩個管道都記成功", [p["ok"] for p in run.get("pushes") or []] == [True, True], run.get("pushes"))
st, cl = user.req("GET", "/api/chats")
it = next((c for c in cl.get("items") or [] if c["id"] == run.get("chat")), None)
check("  結果存成「定時任務」分類的對話，可以接著問", it is not None and it.get("category") == SC.CATEGORY and it["title"].startswith("算數・"), it)
audit = json.dumps(x.STORE.audit_list(500), ensure_ascii=False)
check("  稽核記了執行，但不含Token與Webhook網址", "schedule.run" in audit and TOKEN not in audit and "secret=zzz" not in audit)

SLOT["busy"] = False
st, out = admin.req("POST", "/api/schedules/save", {**job, "channels": [], "auto_start": True, "auto_stop": True})
aid = out.get("id")
MODEL.clear()
x.sched_execute("admin", aid, True)
run = admin.req("GET", "/api/schedules")[1]["tasks"][0]["runs"][0]
check("管理員的任務：模型沒在跑就自動啟動、跑完關掉", run.get("ok") and ("n1", "/slot/main/start") in CALLS and ("n1", "/slot/main/stop") in CALLS, run)
SLOT["busy"] = False
st, out = user.req("POST", "/api/schedules/save", {**job, "name": "沒在跑"})
x.sched_execute("s1@cloudinfo.com.tw", out["id"], True)
run = next(t for t in user.req("GET", "/api/schedules")[1]["tasks"] if t["name"] == "沒在跑")["runs"][0]
check("  一般帳號的任務遇到模型沒在跑：不啟動，記失敗並推送失敗通知", not run["ok"] and "沒有在跑" in run.get("error", "")
      and ("n1", "/slot/main/start") not in CALLS[-3:] and len(run.get("pushes") or []) == 2
      and "沒有完成" in PUSH[-1]["payload"]["title"], run)
SLOT["busy"] = True

# 排程：到期的排進佇列並把起算點設成現在；錯過太久的直接跳過
t = x.sched_get("s1@cloudinfo.com.tw", tid)
t.update(rule={"kind": "every", "minutes": 120}, anchor=time.time() - 3 * 3600)
x.sched_put("s1@cloudinfo.com.tw", tid, t)
with x._SCHED_LOCK:
    x.SCHED["queue"].clear()
x.sched_tick()
q = [j[:2] for j in x.SCHED["queue"]]
check("排程：到期的排進佇列、起算點設成現在（失敗也不會每20秒重試）",
      ("s1@cloudinfo.com.tw", tid) in q and time.time() - x.sched_get("s1@cloudinfo.com.tw", tid)["anchor"] < 30, q)
check("  同一件不會重複排", not x.sched_enqueue("s1@cloudinfo.com.tw", tid))
with x._SCHED_LOCK:
    x.SCHED["queue"].clear()
t = x.sched_get("s1@cloudinfo.com.tw", tid)
t["anchor"] = time.time() - 86400 * 3
x.sched_put("s1@cloudinfo.com.tw", tid, t)
x.sched_tick()
check("  錯過超過12小時：不補跑，跳到下一次", not x.SCHED["queue"])

st, out = user.req("POST", f"/api/schedules/{tid}/toggle", {"enabled": False})
check("暫停之後不會排進去", st == 200 and x.sched_get("s1@cloudinfo.com.tw", tid)["enabled"] is False)
st, out = user.req("POST", f"/api/push/{ch['id']}/delete", {})
check("刪掉管道：從用到它的任務拿掉", st == 200 and ch["id"] not in x.sched_get("s1@cloudinfo.com.tw", tid)["channels"])
st, out = user.req("POST", f"/api/schedules/{tid}/delete", {})
check("刪掉任務", st == 200 and x.sched_get("s1@cloudinfo.com.tw", tid) is None)

# ---------------------------------------------------------------- 畫面守衛
js = open(os.path.join(ST, "schedule.js"), encoding="utf-8").read()
dlg = js[js.index("function schedDialog"):js.index("async function viewSchedule")]
check("編輯與推送管道的視窗不能點背景關（有輸入欄位），Esc或取消才關",
      "addEventListener(\"click\"" not in dlg and "e.target === overlay" not in dlg and '"Escape"' in dlg
      and "lastElementChild === overlay" in dlg)
check("令牌與網址欄位用密碼框、存好只顯示已設定", js.count('type = "password"') >= 3 and "已設定" in js)
check("範本用台灣在地（台北、新台幣），不用中國城市與人民幣", "台北" in js and "新台幣" in js and not re.search(r"北京|上海|人民幣|微信|釘釘|飛書", js))
check("自動更新只在有任務在跑時輪詢，內容沒變不重畫", "s2 === sig" in js and "t.running || t.queued" in js)
app = open(os.path.join(ST, "app.js"), encoding="utf-8").read()
check("定時任務在Agent的第二列（頂層維持八格）", 'subBtn("定時任務", "schedule"' in app and 'else if (v === "schedule") viewSchedule();' in app)
check("對話工具也有查時間與計算機", "current_time" in open(os.path.join(ST, "chat.js"), encoding="utf-8").read()
      and {"current_time", "calculator"} <= {t["function"]["name"] for t in x.assist_toolset("u", False, "n1")[0]})
idx = open(os.path.join(ST, "index.html"), encoding="utf-8").read()
check("index.html 載入 schedule.js", "schedule.js" in idx)

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
