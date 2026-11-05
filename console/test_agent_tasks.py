#!/usr/bin/env python3
"""Agent 任務（主控台的對話迴圈）的自我檢查：用假的 OpenAI 相容服務照劇本回 tool_calls，
檔案與指令實際交給代理的 /agent/* 函式在暫存資料夾裡做。
涵蓋：等待核准 → 允許、拒絕（把「使用者拒絕」當工具結果回給模型）、always（之後不再問）、步數上限、
模型不支援 tools 講清楚、只有一個任務、停止、本機欄位當模型、每條實際執行的指令都寫稽核。
跑法：python3 test_agent_tasks.py（不連外、不碰正式環境）
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-agenttask-")
os.environ["XCONSOLE_HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "agent")]
import xconsole_server as x  # noqa: E402
import xconsole_agent as A  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

OK = True
SCRIPT = []          # 假模型依序回的訊息
REQS = []            # 假模型收到的請求


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def call(name, **args):
    return {"id": "c" + os.urandom(3).hex(), "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def say(text="", *calls):
    return {"choices": [{"message": {"role": "assistant", "content": text, "tool_calls": list(calls) or None}}]}


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        REQS.append(body)
        nxt = SCRIPT.pop(0) if SCRIPT else say("", call("list_files"))
        code, obj = nxt if isinstance(nxt, tuple) else (200, nxt)
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
threading.Thread(target=fake.serve_forever, daemon=True).start()


def fake_call_node(node_id, path, method="GET", body=None, timeout=40):
    """不走 TLS：直接呼叫代理的函式（代理端的檢查由 agent/test_agent_mode.py 另外測）。"""
    b = body or {}
    if node_id != "n1":
        return 404, {"error": "沒有這台機器"}
    if path == "/agent/folders":
        return A.agent_folders()
    if path == "/agent/fs/list":
        return A.agent_list(b.get("folder"), b.get("path") or "")
    if path == "/agent/fs/read":
        return A.agent_read(b.get("folder"), b.get("path"))
    if path == "/agent/fs/write":
        return A.agent_write(b.get("folder"), b.get("path"), b.get("content"))
    if path == "/agent/exec":
        return A.agent_exec(b.get("folder"), b.get("cmd"), b.get("timeout"))
    if path == "/agent/folders/create":
        return A.agent_folder_create(b.get("name"))
    if path.startswith("/chat/"):
        REQS.append(b)
        nxt = SCRIPT.pop(0) if SCRIPT else say("", call("list_files"))
        return nxt if isinstance(nxt, tuple) else (200, nxt)
    return 404, {"error": "找不到"}


x.call_node = fake_call_node
x.update_fleet(lambda c: c.__setitem__("nodes", [{"id": "n1", "name": "測試機", "host": "127.0.0.1", "agent_port": 1}]))
x.set_credentials("admin", "agent-test-pw")
srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
COOKIE = {}


def req(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if COOKIE.get("v"):
        r.add_header("Cookie", "xconsole=" + COOKIE["v"])
    try:
        resp = urllib.request.urlopen(r, timeout=30)
    except urllib.error.HTTPError as e:
        resp = e
    payload = resp.read()
    sc = resp.headers.get("Set-Cookie") or ""
    if sc.startswith("xconsole="):
        COOKIE["v"] = sc.split(";")[0].split("=", 1)[1]
    return resp.status, json.loads(payload or b"{}")


def wait(tid, states, sec=30):
    end = time.time() + sec
    while time.time() < end:
        st, t = req("GET", f"/api/agent/tasks/{tid}")
        if t.get("state") in states:
            return t
        time.sleep(0.1)
    return t


PY = f'"{sys.executable}"' if os.name != "nt" else f"& '{sys.executable}'"


def main():
    req("POST", "/api/login", {"username": "admin", "password": "agent-test-pw"})
    st, out = req("POST", "/api/providers/save", {"name": "假模型", "base_url": f"http://127.0.0.1:{fake.server_address[1]}/v1",
                                                  "api_key": "sk" + "-fake-agent-key-0000", "default_model": "fake-tools"})
    pid = out["provider"]["id"]
    model = {"kind": "provider", "id": pid}

    # ---- 1. 這台沒開 Agent 模式
    st, out = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "p", "model": model, "prompt": "做一個網頁"})
    check("這台沒開 Agent 模式：建立任務回 403 並講清楚", st == 403 and out.get("disabled"), out)
    st, out = req("GET", "/api/node/n1/agent/folders")
    check("  列工作資料夾也是 403", st == 403)
    A.save_machine({"memory_model": "discrete", "agent_enabled": True})
    st, out = req("POST", "/api/node/n1/agent/folders/create", {"name": "網頁"})
    check("建立工作資料夾（經主控台）", st == 200, out)
    st, out = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "沒有這個", "model": model, "prompt": "x"})
    check("工作資料夾不存在 → 400", st == 400, out)

    # ---- 2. 正常流程：計畫、寫檔、執行指令（等核准 → 允許）、完成
    SCRIPT[:] = [
        say("我先列計畫。", call("update_plan", steps=[{"text": "寫首頁", "status": "in_progress"}, {"text": "產生說明檔", "status": "pending"}]),
            call("write_file", path="index.html", content="<h1>你好</h1>")),
        say("", call("run_command", command=f"{PY} -c \"open('out.txt','w').write('done')\"")),
        say("", call("finish", summary="首頁做好了，也產生了 out.txt")),
    ]
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": model, "prompt": "做一個首頁"})
    check("建立任務（第三方模型）", st == 200 and t["state"] in ("running", "waiting_approval"), t)
    tid = t["id"]
    t = wait(tid, ("waiting_approval", "failed", "done"))
    check("執行指令前停在 waiting_approval，pending 帶指令", t["state"] == "waiting_approval" and "out.txt" in (t["pending"] or {}).get("command", ""), t.get("error"))
    check("  還沒按允許之前指令沒有執行", not os.path.exists(os.path.join(A.AGENT_ROOT, "網頁", "out.txt")))
    st2, other = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": model, "prompt": "另一件事"})
    check("同時只能有一個任務：第二個回 409", st2 == 409, other)
    st, _ = req("POST", f"/api/agent/tasks/{tid}/approve", {"allow": True})
    t = wait(tid, ("done", "failed"))
    check("按允許之後做完（done）", t["state"] == "done", t.get("error"))
    files = {f["path"]: f["action"] for f in t["files"]}
    check("  改動的檔案：write_file 建的與指令產生的都列為新建", files.get("index.html") == "created" and files.get("out.txt") == "created", files)
    check("  計畫照 update_plan", [p["text"] for p in t["plan"]] == ["寫首頁", "產生說明檔"] and t["plan"][0]["status"] == "in_progress", t["plan"])
    check("  log 有 text、tool、result 三種", {e["type"] for e in t["log"]} >= {"text", "tool", "result"}, {e["type"] for e in t["log"]})
    check("  tree 是資料夾的檔案清單", {r["path"] for r in t["tree"]} >= {"index.html", "out.txt"}, t["tree"])
    check("  總結取自 finish", t["summary"] == "首頁做好了，也產生了 out.txt")
    check("  送給模型的是 OpenAI tools 格式（六個工具）", sorted(f["function"]["name"] for f in REQS[0]["tools"])
          == sorted(["list_files", "read_file", "write_file", "run_command", "update_plan", "finish"]))
    tool_msgs = [m for m in REQS[-1]["messages"] if m["role"] == "tool"]
    check("  指令結果（結束碼、輸出）回給模型", any('"exit_code": 0' in m["content"] for m in tool_msgs), tool_msgs[-1:])
    st, au = req("GET", "/api/admin/audit?limit=200")
    ex = [a for a in au["items"] if a["action"] == "agent.exec"]
    check("每條實際執行的指令都寫稽核（指令、結束碼）", len(ex) == 1 and "out.txt" in ex[0]["detail"]["cmd"] and ex[0]["detail"]["exit"] == 0, ex)
    check("  稽核沒有第三方金鑰", "sk" + "-fake-agent-key" not in json.dumps(au, ensure_ascii=False))

    # ---- 3. 拒絕：把「使用者拒絕」當工具結果回給模型
    SCRIPT[:] = [say("", call("run_command", command="echo 不該執行 > nope.txt")),
                 say("好，換個做法。", call("finish", summary="使用者不讓我執行，改用說明"))]
    REQS.clear()
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": model, "prompt": "試試看"})
    tid = t["id"]
    wait(tid, ("waiting_approval",))
    req("POST", f"/api/agent/tasks/{tid}/approve", {"allow": False})
    t = wait(tid, ("done", "failed"))
    check("拒絕之後任務繼續、指令沒有執行", t["state"] == "done" and not os.path.exists(os.path.join(A.AGENT_ROOT, "網頁", "nope.txt")), t.get("error"))
    last_tool = [m for m in REQS[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    check("  模型收到的工具結果是「使用者拒絕」", "使用者拒絕" in last_tool, last_tool)
    st, out = req("POST", f"/api/agent/tasks/{tid}/approve", {"allow": True})
    check("沒有在等核准時按允許 → 409", st == 409)

    # ---- 4. always：這個任務之後不再問
    SCRIPT[:] = [say("", call("run_command", command="echo one")),
                 say("", call("run_command", command="echo two")),
                 say("", call("finish", summary="兩條都跑了"))]
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": model, "prompt": "跑兩條"})
    tid = t["id"]
    wait(tid, ("waiting_approval",))
    req("POST", f"/api/agent/tasks/{tid}/approve", {"allow": True, "always": True})
    t = wait(tid, ("done", "failed", "waiting_approval"))
    check("always：第二條指令不再停下來問", t["state"] == "done" and t["always"], t["state"])
    st, au = req("GET", "/api/admin/audit?limit=200")
    cmds = [a["detail"]["cmd"] for a in au["items"] if a["action"] == "agent.exec"]
    check("  兩條都寫了稽核", "echo one" in cmds and "echo two" in cmds, cmds)

    # ---- 5. 步數上限
    x.AGENT_MAX_STEPS = 4
    SCRIPT[:] = []                               # 假模型一直回 list_files
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": model, "prompt": "一直列"})
    t = wait(t["id"], ("failed", "done"))
    check("步數上限：到了就停、講清楚", t["state"] == "failed" and "4 步" in (t["error"] or "") and t["steps"] == 4, (t["state"], t.get("error")))
    x.AGENT_MAX_STEPS = 40

    # ---- 6. 模型不支援 tools
    SCRIPT[:] = [(500, {"error": {"message": "tools param requires --jinja flag"}})]
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": model, "prompt": "x"})
    t = wait(t["id"], ("failed", "done"))
    check("推理引擎拒收 tools：回「不支援工具呼叫」", t["state"] == "failed" and "不支援工具呼叫" in t["error"], t.get("error"))
    SCRIPT[:] = [say("我直接回答：好的。"), say("還是直接回答。")]
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": model, "prompt": "x"})
    t = wait(t["id"], ("failed", "done"))
    check("模型一直不呼叫工具：回「可能不支援工具呼叫」", t["state"] == "failed" and "工具呼叫" in t["error"], t.get("error"))

    # ---- 7. 停止
    SCRIPT[:] = [say("", call("run_command", command="echo wait"))]
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": model, "prompt": "等我"})
    tid = t["id"]
    wait(tid, ("waiting_approval",))
    req("POST", f"/api/agent/tasks/{tid}/stop", {})
    t = wait(tid, ("stopped", "failed", "done"))
    check("等核准時按停止 → stopped", t["state"] == "stopped" and t["pending"] is None, t["state"])

    # ---- 8. 本機欄位當模型
    SCRIPT[:] = [say("", call("write_file", path="local.txt", content="本機模型寫的")), say("", call("finish", summary="好了"))]
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": {"kind": "local", "slot": "s1"}, "prompt": "寫一個檔"})
    t = wait(t["id"], ("done", "failed"))
    check("本機欄位當模型（經代理的 /chat/{欄位}）", t["state"] == "done" and t["model"] == "本機 n1/s1"
          and open(os.path.join(A.AGENT_ROOT, "網頁", "local.txt"), encoding="utf-8").read() == "本機模型寫的", t.get("error"))
    st, lst = req("GET", "/api/agent/tasks")
    check("任務清單（新的在前、不含完整 log）", st == 200 and lst["items"][0]["id"] == t["id"] and "log" not in lst["items"][0]
          and lst["limits"] == {"steps": 40, "seconds": 1800}, lst["items"][:1])
    st, out = req("POST", "/api/node/n1/agent/fs/read", {"folder": "網頁", "path": "local.txt"})
    check("右側面板可以經主控台讀檔（唯讀）", st == 200 and out["content"] == "本機模型寫的", out)
    st, out = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "網頁", "model": {"kind": "x"}, "prompt": "x"})
    check("模型格式不對 → 400", st == 400)

    srv.shutdown()
    fake.shutdown()
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
