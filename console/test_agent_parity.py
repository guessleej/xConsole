#!/usr/bin/env python3
"""Agent頁每天會碰到的四項（對齊LocModel 3.6.2）的自我檢查：
  1. 同一件任務接著交代：模型看得到前幾輪的交代、做過的操作與結論；重啟主控台後也接得上；前情太長只留最近幾輪
  2. edit_file：原文出現0次、1次、多次（replace_all）、路徑在工作資料夾外、CRLF、非UTF-8、舊版代理
  3. read_file：帶行號分段讀、總行數與下文、PDF／Word／Excel走獨立行程抽字、二進位照舊拒絕
  4. 串流：片段組回完整訊息（內容、思考、工具呼叫）、即時狀態（正在寫哪個檔、產生多少）、不支援串流就退回一般呼叫、按停止就斷
檔案與指令交給代理的/agent/*函式在暫存資料夾裡做，模型是照劇本回的假服務。不連外、不碰正式環境。
跑法：python3 test_agent_parity.py
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

TMP = tempfile.mkdtemp(prefix="xc-agentparity-")
os.environ["XCONSOLE_HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "agent")]
import xconsole_server as x  # noqa: E402
import xconsole_agent as A  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

OK = True
SCRIPT = []          # 假模型依序回的訊息：dict＝一則回應；("slow", dict, 秒)＝慢慢串流；(狀態碼, dict)＝錯誤
REQS = []
STREAM_CUT = []      # 慢速串流被主控台中途關掉時記一筆
OLD_AGENT = set()    # 模擬舊版代理：列在這裡的路徑回404「找不到」、不認新參數


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def call(name, **args):
    return {"id": "c" + os.urandom(3).hex(), "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def say(text="", *calls, reasoning=None):
    m = {"role": "assistant", "content": text, "tool_calls": list(calls) or None}
    if reasoning:
        m["reasoning_content"] = reasoning
    return {"choices": [{"message": m}]}


def sse(obj, piece=7):
    msg = obj["choices"][0]["message"]
    out = [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
    r = msg.get("reasoning_content") or ""
    out += [{"choices": [{"index": 0, "delta": {"reasoning_content": r[i:i + 4]}}]} for i in range(0, len(r), 4)]
    text = msg.get("content") or ""
    out += [{"choices": [{"index": 0, "delta": {"content": text[i:i + 3]}}]} for i in range(0, len(text), 3)]
    for i, c in enumerate(msg.get("tool_calls") or []):
        a = c["function"]["arguments"]
        out.append({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                                                          "function": {"name": c["function"]["name"], "arguments": ""}}]}}]})
        out += [{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, "function": {"arguments": a[j:j + piece]}}]}}]}
                for j in range(0, len(a), piece)]
    out.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    return [f"data: {json.dumps(x, ensure_ascii=False)}\n\n".encode() for x in out] + [b"data: [DONE]\n\n"]


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        REQS.append(body)
        nxt = SCRIPT.pop(0) if SCRIPT else say("", call("finish", summary="好"))
        delay = 0
        if isinstance(nxt, tuple) and nxt[0] == "slow":
            _, nxt, delay = nxt
        code, obj = nxt if isinstance(nxt, tuple) else (200, nxt)
        if code == 200 and body.get("stream") and not obj.get("_json"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            lines = sse(obj, piece=40 if delay else 7)
            try:
                for line in lines:
                    self.wfile.write(line)
                    self.wfile.flush()
                    if delay:
                        time.sleep(delay / len(lines))
            except (BrokenPipeError, ConnectionResetError):
                STREAM_CUT.append(time.time())
            return
        obj = {k: v for k, v in obj.items() if k != "_json"}
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
threading.Thread(target=fake.serve_forever, daemon=True).start()


def fake_call_node(node_id, path, method="GET", body=None, timeout=40):
    b = body or {}
    if node_id != "n1":
        return 404, {"error": "沒有這台機器"}
    if path in OLD_AGENT:
        return 404, {"error": "找不到"}
    if path == "/agent/folders":
        return A.agent_folders()
    if path == "/agent/folders/create":
        return A.agent_folder_create(b.get("name"))
    if path == "/agent/fs/list":
        return A.agent_list(b.get("folder"), b.get("path") or "")
    if path == "/agent/fs/read":
        if "/agent/fs/read:old" in OLD_AGENT:
            return A.agent_read(b.get("folder"), b.get("path"))
        return A.agent_read(b.get("folder"), b.get("path"), b.get("start"), b.get("lines"), b.get("raw") is True)
    if path == "/agent/fs/write":
        return A.agent_write(b.get("folder"), b.get("path"), b.get("content"))
    if path == "/agent/fs/edit":
        return A.agent_edit(b.get("folder"), b.get("path"), b.get("old"), b.get("new"), b.get("replace_all") is True)
    if path == "/agent/exec":
        return A.agent_exec(b.get("folder"), b.get("cmd"), b.get("timeout"))
    if path == "/agent/exec/cancel":
        return A.agent_exec_cancel()
    return 404, {"error": "找不到"}


x.call_node = fake_call_node
x.update_fleet(lambda c: c.__setitem__("nodes", [{"id": "n1", "name": "測試機", "host": "127.0.0.1", "agent_port": 1}]))
x.set_credentials("admin", "agent-parity-pw")
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
    t = {}
    while time.time() < end:
        st, t = req("GET", f"/api/agent/tasks/{tid}")
        if t.get("state") in states:
            return t
        time.sleep(0.05)
    return t


def user_msgs(r):
    return [m["content"] for m in r["messages"] if m["role"] == "user"]


def docx(paras):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document><w:body>" + "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
                   + "</w:body></w:document>")
    return b.getvalue()


def xlsx(rows):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="月報" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        body = "".join(f'<row r="{i}">' + "".join(f'<c r="{chr(65 + j)}{i}" t="inlineStr"><is><t>{v}</t></is></c>' for j, v in enumerate(r))
                       + "</row>" for i, r in enumerate(rows, 1))
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{body}</sheetData></worksheet>")
    return b.getvalue()


def pdf(text):
    """一頁的最小PDF（xref照實算）。"""
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def main():
    req("POST", "/api/login", {"username": "admin", "password": "agent-parity-pw"})
    st, out = req("POST", "/api/providers/save", {"name": "假模型", "base_url": f"http://127.0.0.1:{fake.server_address[1]}/v1",
                                                  "api_key": "sk" + "-fake-parity-key-0000", "default_model": "fake-tools"})
    model = {"kind": "provider", "id": out["provider"]["id"]}
    A.save_machine({"memory_model": "discrete", "agent_enabled": True})
    req("POST", "/api/node/n1/agent/folders/create", {"name": "proj"})
    proj = os.path.join(A.AGENT_ROOT, "proj")

    # ================================================================ 2. edit_file（代理端）
    with open(os.path.join(proj, "app.py"), "w", encoding="utf-8") as fh:
        fh.write("def hi():\n    return '你好'\n\nprint(hi())\nprint(hi())\n")
    code, out = A.agent_edit("proj", "app.py", "return '你好'", "return '哈囉'")
    body = open(os.path.join(proj, "app.py"), encoding="utf-8").read()
    check("edit_file：原文只出現一次 → 換掉那一處、其他不動", code == 200 and out["replaced"] == 1 and "return '哈囉'" in body
          and body.count("print(hi())") == 2, (code, out))
    code, out = A.agent_edit("proj", "app.py", "return '不存在'", "x")
    check("  原文出現0次 → 回錯誤、講清楚找不到、檔案不動", code == 400 and out.get("count") == 0 and "找不到" in out["error"]
          and open(os.path.join(proj, "app.py"), encoding="utf-8").read() == body, out)
    code, out = A.agent_edit("proj", "app.py", "print(hi())", "print(hi() * 2)")
    check("  原文出現2次、沒給replace_all → 回錯誤、說出現幾次、檔案不動", code == 400 and out.get("count") == 2 and "2次" in out["error"]
          and "replace_all" in out["error"] and open(os.path.join(proj, "app.py"), encoding="utf-8").read() == body, out)
    code, out = A.agent_edit("proj", "app.py", "print(hi())", "print(hi() * 2)", replace_all=True)
    check("  replace_all：兩處都換", code == 200 and out["replaced"] == 2
          and open(os.path.join(proj, "app.py"), encoding="utf-8").read().count("print(hi() * 2)") == 2, out)
    for bad in ("../outside.txt", "/etc/hosts", "C:/Windows/win.ini", "a/../../x"):
        code, out = A.agent_edit("proj", bad, "a", "b")
        check(f"  工作資料夾外的路徑一律拒絕：{bad}", code == 400, (code, out))
    if os.name != "nt":
        secret = os.path.join(TMP, "secret.txt")
        with open(secret, "w") as fh:
            fh.write("token=abc")
        os.symlink(secret, os.path.join(proj, "link.txt"))
        code, out = A.agent_edit("proj", "link.txt", "abc", "pwned")
        check("  符號連結指到外面：拒絕、外面的檔案不動", code == 400 and open(secret).read() == "token=abc", (code, out))
    with open(os.path.join(proj, "win.txt"), "wb") as fh:
        fh.write(b"\xef\xbb\xbfline1\r\nline2\r\nline3\r\n")
    code, out = A.agent_edit("proj", "win.txt", "line1\nline2", "第一行\n第二行")
    raw = open(os.path.join(proj, "win.txt"), "rb").read()
    check("  CRLF的檔案用LF寫原文也認得，寫回保留CRLF與BOM", code == 200 and raw == "\ufeff第一行\r\n第二行\r\nline3\r\n".encode(), raw)
    with open(os.path.join(proj, "big5.txt"), "wb") as fh:
        fh.write("中文".encode("cp950"))
    code, out = A.agent_edit("proj", "big5.txt", "中", "英")
    check("  不是UTF-8 → 回錯誤、建議用write_file", code == 400 and "write_file" in out["error"], out)
    check("  原文是空的 → 拒絕", A.agent_edit("proj", "app.py", "", "x")[0] == 400)
    check("  檔案不存在 → 404、建議用write_file", A.agent_edit("proj", "nope.py", "a", "b")[0] == 404)
    A.save_machine({"agent_enabled": False})
    check("  這台沒開Agent模式 → 403", A.agent_edit("proj", "app.py", "hi", "ho")[0] == 403)
    A.save_machine({"agent_enabled": True})

    # ---- 經任務：模型叫edit_file
    with open(os.path.join(proj, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<h1>你好</h1>\n<p>說明</p>\n")
    SCRIPT[:] = [say("", call("edit_file", path="index.html", old_text="<h1>你好</h1>", new_text="<h1 style=\"color:blue\">你好</h1>")),
                 say("", call("edit_file", path="index.html", old_text="<p>不存在</p>", new_text="x")),
                 say("", call("finish", summary="標題改成藍色了"))]
    REQS.clear()
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "proj", "model": model, "prompt": "標題改成藍色"})
    t = wait(t["id"], ("done", "failed"))
    check("任務裡的edit_file：檔案改好、改動清單標修改", t["state"] == "done" and 'color:blue' in open(os.path.join(proj, "index.html"), encoding="utf-8").read()
          and {f["path"]: f["action"] for f in t["files"]}.get("index.html") == "modified", (t.get("state"), t.get("error"), t.get("files")))
    tool_msgs = [m["content"] for m in REQS[-1]["messages"] if m["role"] == "tool"]
    check("  模型收到：第一次換了1處、第二次找不到的錯誤", '"replaced": 1' in tool_msgs[0] and "找不到" in tool_msgs[1], tool_msgs)
    check("  edit_file在工具清單裡", "edit_file" in [f["function"]["name"] for f in REQS[0]["tools"]])
    shown = [e for e in t["log"] if e["type"] == "tool" and e["tool"] == "edit_file"]
    check("  紀錄裡原文與新內容只記字數，不整段存", shown and shown[0]["args"]["old_text"].endswith("字）"), shown[:1])
    OLD_AGENT.add("/agent/fs/edit")
    tt = x.AGENT_TASKS[t["id"]]
    out = x.agent_tool(tt, "edit_file", {"path": "index.html", "old_text": "說明", "new_text": "介紹"})
    check("  舊版代理沒有edit_file：講清楚要改用write_file、請管理者更新", "write_file" in out.get("error", "") and "更新" in out["error"], out)
    OLD_AGENT.discard("/agent/fs/edit")

    # ================================================================ 3. read_file分段與文件
    with open(os.path.join(proj, "long.txt"), "w", encoding="utf-8") as fh:
        fh.write("".join(f"第{i}行\n" for i in range(1, 5001)))
    code, out = A.agent_read("proj", "long.txt", 1, 2000)
    check("代理分段讀：第1行起2000行、回總行數5000、還有下文", code == 200 and out["lines"] == 2000 and out["total_lines"] == 5000
          and out["more"] and out["content"].startswith("第1行\n") and out["content"].endswith("第2000行\n"), {k: out.get(k) for k in ("lines", "total_lines", "more")})
    code, out = A.agent_read("proj", "long.txt", 4990, 2000)
    check("  從第4990行起：只剩11行、沒有下文", out["lines"] == 11 and not out["more"] and out["content"].startswith("第4990行"), out.get("lines"))
    code, out = A.agent_read("proj", "long.txt")
    check("  沒給分段參數照舊回整份（右側面板用）", code == 200 and "total_lines" not in out and out["content"].count("\n") == 5000)
    check("  分段參數不是整數 → 400", A.agent_read("proj", "long.txt", "abc", 5)[0] == 400)
    text = x.agent_read_tool(tt, "long.txt", None, None)
    first_line = text.split("\n", 1)[0]
    check("read_file回帶行號的內容，開頭寫總行數與從哪裡接著讀", isinstance(text, str) and "共5000行" in first_line and "start_line=" in first_line
          and "\n     1\t第1行\n     2\t第2行" in text, first_line)
    check("  一次回給模型的字數有上限", len(text) <= x.AGENT_READ_CHARS + 200, len(text))
    nxt = int(first_line.split("start_line=")[1].split("。")[0])
    text2 = x.agent_read_tool(tt, "long.txt", nxt, 50)
    check("  照提示的start_line接著讀，行號接得上", text2.split("\n")[1].startswith(f"{nxt:>6}\t第{nxt}行"), text2[:120])
    text3 = x.agent_read_tool(tt, "long.txt", 4995, 100)
    check("  讀到結尾會說「已經讀到結尾」", "第4995～5000行" in text3 and "已經讀到結尾" in text3, text3.split("\n")[0])
    check("  起始行超過總行數：說之後沒有內容", "沒有內容" in x.agent_read_tool(tt, "long.txt", 9000, 10))
    with open(os.path.join(proj, "empty.txt"), "w") as fh:
        pass
    check("  空檔案講清楚", "是空的" in x.agent_read_tool(tt, "empty.txt", None, None))
    OLD_AGENT.add("/agent/fs/read:old")
    old = x.agent_read_tool(tt, "long.txt", 3, 2)
    check("  舊版代理（不認分段參數）也能分段：主控台自己切", "第3～4行" in old and "     3\t第3行" in old and "共5000行" in old, old[:200])
    OLD_AGENT.discard("/agent/fs/read:old")
    with open(os.path.join(proj, "pic.bin"), "wb") as fh:
        fh.write(b"\x89PNG\x00\x00\x01binary")
    out = x.agent_read_tool(tt, "pic.bin", None, None)
    check("二進位檔照舊不讀內容", isinstance(out, dict) and "二進位" in out.get("note", ""), out)
    with open(os.path.join(proj, "需求.docx"), "wb") as fh:
        fh.write(docx(["第一章 需求", "要有登入頁", "要能匯出報表"]))
    calls = []
    real_extract = x.extract_doc

    def counted(name, b64):
        calls.append(name)
        return real_extract(name, b64)
    x.extract_doc = counted
    out = x.agent_read_tool(tt, "需求.docx", None, None)
    check("Word：經獨立行程抽字、帶行號", isinstance(out, str) and out.startswith("文件需求.docx：共3行") and "     2\t要有登入頁" in out, out)
    out2 = x.agent_read_tool(tt, "需求.docx", 3, 1)
    check("  同一份文件分段讀不重抽（只抽一次）", calls == ["需求.docx"] and "     3\t要能匯出報表" in out2, (calls, out2))
    with open(os.path.join(proj, "需求.docx"), "wb") as fh:
        fh.write(docx(["改過的內容"]))
    out3 = x.agent_read_tool(tt, "需求.docx", None, None)
    check("  檔案內容變了就重抽", len(calls) == 2 and "改過的內容" in out3, out3)
    with open(os.path.join(proj, "報表.xlsx"), "wb") as fh:
        fh.write(xlsx([["月份", "營收"], ["一月", "120"]]))
    out = x.agent_read_tool(tt, "報表.xlsx", None, None)
    check("Excel：抽得出工作表與儲存格", isinstance(out, str) and "工作表：月報" in out and "一月\t120" in out, out)
    try:
        import pypdf  # noqa: F401
        has_pypdf = True
    except ImportError:
        has_pypdf = False
    with open(os.path.join(proj, "規格.pdf"), "wb") as fh:
        fh.write(pdf("Hello agent PDF"))
    out = x.agent_read_tool(tt, "規格.pdf", None, None)
    if has_pypdf:
        check("PDF：抽得出字、帶頁碼與行號", isinstance(out, str) and "Hello agent PDF" in out and "\t## 第1頁" in out, out)
    else:
        check("PDF：走抽字流程（這台沒裝pypdf，回抽字的錯誤而不是當二進位）", isinstance(out, dict) and "error" in out and "二進位" not in json.dumps(out, ensure_ascii=False), out)
        print("[略過] PDF實際抽字要pypdf（CI有裝）")
    with open(os.path.join(proj, "old.doc"), "wb") as fh:
        fh.write(b"\xd0\xcf\x11\xe0old office")
    out = x.agent_read_tool(tt, "old.doc", None, None)
    check("  舊版 .doc：講清楚要另存成 .docx", "docx" in out.get("error", ""), out)
    keep, before = A.AGENT_DOC_MAX, len(calls)
    A.AGENT_DOC_MAX = 10
    out = x.agent_read_tool(tt, "報表.xlsx", None, None)
    check("  文件超過大小上限 → 回錯誤、不送去抽字", "15 MB" in out.get("error", "") and len(calls) == before, (out, calls))
    A.AGENT_DOC_MAX = keep
    OLD_AGENT.add("/agent/fs/read:old")
    out = x.agent_read_tool(tt, "報表.xlsx", None, None)
    check("  舊版代理拿不到原檔：講清楚要更新那台", "更新" in out.get("error", ""), out)
    OLD_AGENT.discard("/agent/fs/read:old")
    x.extract_doc = real_extract
    check("read_file的參數有start_line與max_lines", {"start_line", "max_lines"} <= set(
        next(f for f in x.AGENT_TOOLS if f["function"]["name"] == "read_file")["function"]["parameters"]["properties"]))

    # ================================================================ 4. 串流
    lines = sse(say("我先看看", call("write_file", path="網站/首頁.html", content="<h1>" + "很長" * 300 + "</h1>"),
                    call("update_plan", steps=[{"text": "寫首頁"}]), reasoning="使用者要一個首頁，先建檔"))
    seen = []
    msg = x.sse_assemble(iter(lines), lambda c, r, k: seen.append((c, r, k and dict(k["function"]))), every=0)
    calls_ = msg["tool_calls"]
    check("串流片段組回完整訊息：內容、思考、兩個工具呼叫（名稱與參數都接好）",
          msg["content"] == "我先看看" and msg["reasoning_content"] == "使用者要一個首頁，先建檔"
          and [c["function"]["name"] for c in calls_] == ["write_file", "update_plan"]
          and json.loads(calls_[0]["function"]["arguments"])["content"].count("很長") == 300 and msg["usage"]["completion_tokens"] == 5, msg)
    check("  一邊收一邊回報：參數越來越長、看得到在寫哪個檔", any(k and k["name"] == "write_file" for _, _, k in seen)
          and max(len(k["arguments"]) for _, _, k in seen if k) > 100, len(seen))
    alt = [b'data: {"choices":[{"delta":{"reasoning":"\xe6\x83\xb3"}}]}\n', b": keep-alive\n", b"\n",
           b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a1","function":{"name":"read_","arguments":"{\\"pa"}}]}}]}\n',
           b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"file","arguments":"th\\": \\"a.txt\\"}"}}]}}]}\n',
           b"data: [DONE]\n"]
    m2 = x.sse_assemble(iter(alt))
    check("  另一種寫法也接得起來：reasoning欄位、名稱分片送、保活行", m2["reasoning_content"] == "想"
          and m2["tool_calls"][0]["function"]["name"] == "read_file" and json.loads(m2["tool_calls"][0]["function"]["arguments"]) == {"path": "a.txt"}, m2)
    try:
        x.sse_assemble(iter([b'data: {"error":{"message":"context length exceeded"}}\n']))
        err = None
    except x.StreamError as exc:
        err = str(exc)
    check("  串流裡回錯誤 → StreamError帶對方訊息", err == "context length exceeded", err)
    n = [0]

    def stop_after_two():
        n[0] += 1
        return n[0] > 2
    m3 = x.sse_assemble(iter(sse(say("一二三四五六七八九十" * 3))), should_stop=stop_after_two)
    check("  should_stop為真就不再讀", len(m3["content"]) < 30, m3["content"])
    m4 = x.sse_assemble(iter(sse(say("", call("finish", summary="ok")))))
    check("  沒給id的也補上", m4["tool_calls"][0]["id"])

    # ---- 經任務：慢慢串流時看得到即時狀態
    big = "<p>" + "內容" * 4000 + "</p>"
    SCRIPT[:] = [("slow", say("", call("write_file", path="大頁面.html", content=big), reasoning="先想一下要怎麼排版"), 3.0),
                 say("", call("finish", summary="大頁面寫好了"))]
    REQS.clear()
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "proj", "model": model, "prompt": "寫一個大頁面"})
    tid = t["id"]
    lives, end_at = [], time.time() + 20
    while time.time() < end_at:
        st, cur = req("GET", f"/api/agent/tasks/{tid}?since=0")
        if cur.get("live"):
            lives.append(cur["live"])
        if cur["state"] not in x.AGENT_LIVE:
            break
        time.sleep(0.1)
    writing = [lv for lv in lives if lv.get("phase") == "writing"]
    check("即時狀態：串流時看得到「正在寫 大頁面.html」與已產生的大小，而且越來越大",
          writing and writing[-1]["path"] == "大頁面.html" and writing[-1]["bytes"] > writing[0]["bytes"] > 0,
          [(lv.get("phase"), lv.get("path"), lv.get("bytes")) for lv in lives][:8])
    check("  思考內容即時帶給畫面", any("排版" in (lv.get("reasoning") or "") for lv in lives))
    check("  回應有turn_started與伺服器時間（畫面每秒算用時）", "turn_started" in cur and "now" in cur)
    t = wait(tid, ("done", "failed"))
    check("  串流組回的參數完整：檔案寫對", t["state"] == "done" and open(os.path.join(proj, "大頁面.html"), encoding="utf-8").read() == big, t.get("error"))
    th = [e for e in t["log"] if e["type"] == "thinking"]
    check("  思考過程記進紀錄（畫面收合顯示）", th and "排版" in th[0]["text"], th)
    check("  做完不再帶即時狀態", "live" not in t)
    st, part = req("GET", f"/api/agent/tasks/{tid}?since={t['log_next'] - 1}&tree={t['tree_rev']}")
    check("  since只回後面新的紀錄、檔案樹沒變不再送", len(part["log"]) == 1 and part["log_from"] == t["log_next"] - 1 and "tree" not in part,
          (len(part.get("log", [])), "tree" in part))

    # ---- 推理引擎不支援串流：退回一般呼叫，之後都不再試
    SCRIPT[:] = [(400, {"error": {"message": "Cannot use tools with stream"}}),
                 {"_json": True, **say("", call("write_file", path="ns.txt", content="一般呼叫"))},
                 {"_json": True, **say("", call("finish", summary="好"))}]
    REQS.clear()
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "proj", "model": model, "prompt": "不支援串流"})
    t = wait(t["id"], ("done", "failed"))
    check("不支援串流：退回一般呼叫做完", t["state"] == "done" and open(os.path.join(proj, "ns.txt"), encoding="utf-8").read() == "一般呼叫", t.get("error"))
    check("  第一次要串流、被拒後改一般呼叫、之後都不再試串流", [r.get("stream") for r in REQS] == [True, False, False], [r.get("stream") for r in REQS])

    # ---- 按停止：串流在下一個片段就斷
    SCRIPT[:] = [("slow", say("", call("write_file", path="停.html", content="x" * 30000)), 10.0)]
    STREAM_CUT.clear()
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "proj", "model": model, "prompt": "寫很久"})
    for _ in range(100):
        if (req("GET", f"/api/agent/tasks/{t['id']}")[1].get("live") or {}).get("phase") == "writing":
            break
        time.sleep(0.05)
    t0 = time.time()
    req("POST", f"/api/agent/tasks/{t['id']}/stop", {})
    for _ in range(60):
        if STREAM_CUT:
            break
        time.sleep(0.1)
    check("按停止：主控台關掉串流連線，不等模型寫完", STREAM_CUT and STREAM_CUT[0] - t0 < 5 and not os.path.exists(os.path.join(proj, "停.html")),
          (STREAM_CUT[:1], t0))
    check("  任務是已停止", wait(t["id"], ("stopped",))["state"] == "stopped")

    # ================================================================ 1. 追問
    SCRIPT[:] = [say("", call("write_file", path="pomo/index.html", content="<h1>番茄鐘</h1>")),
                 say("", call("run_command", command="echo ok")),
                 say("", call("finish", summary="番茄鐘做好了，打開pomo/index.html就能用"))]
    REQS.clear()
    st, t = req("POST", "/api/agent/tasks", {"node": "n1", "folder": "proj", "model": model, "prompt": "做一個番茄鐘網頁"})
    tid = t["id"]
    wait(tid, ("waiting_approval",))
    req("POST", f"/api/agent/tasks/{tid}/approve", {"allow": True, "always": True})
    t = wait(tid, ("done", "failed"))
    check("第一輪做完", t["state"] == "done" and t["turns"] == 1, t.get("error"))
    sumlog = [e for e in t["log"] if e["type"] == "summary"]
    check("  每一輪的總結記進紀錄（畫面上接在那一輪後面）", sumlog and "番茄鐘做好了" in sumlog[-1]["text"], sumlog)
    SCRIPT[:] = [say("", call("edit_file", path="pomo/index.html", old_text="番茄鐘", new_text="番茄鐘（25分鐘）")),
                 say("", call("finish", summary="標題加上時間了"))]
    REQS.clear()
    st, t2 = req("POST", f"/api/agent/tasks/{tid}/continue", {"prompt": "標題加上25分鐘"})
    check("追問：同一件任務接著做（id不變、第2輪）", st == 200 and t2["id"] == tid and t2["turns"] == 2 and t2["state"] == "running", t2)
    t2 = wait(tid, ("done", "failed"))
    first = REQS[0]["messages"]
    um = user_msgs(REQS[0])
    asst = [m["content"] for m in first if m["role"] == "assistant"]
    check("  模型看得到前一輪的交代", um[0] == "做一個番茄鐘網頁" and um[-1] == "標題加上25分鐘", um)
    check("  也看得到前一輪做過的操作與結論", asst and "寫檔pomo/index.html" in asst[0] and "執行指令echo ok" in asst[0]
          and "番茄鐘做好了" in asst[0], asst[:1])
    check("  系統提示說明這是追問、不要從頭重做", "追問" in first[0]["content"] and "不要從頭重做" in first[0]["content"])
    check("  第二輪做完、檔案改好", t2["state"] == "done" and "（25分鐘）" in open(os.path.join(proj, "pomo", "index.html"), encoding="utf-8").read(), t2.get("error"))
    check("  紀錄裡有第二輪的交代（畫面依序顯示）", [e["text"] for e in t2["log"] if e["type"] == "user"] == ["標題加上25分鐘"])
    check("  「這個任務之後不再詢問」不帶到新的一輪", not x.AGENT_TASKS[tid].always)
    st, lst = req("GET", "/api/agent/tasks")
    check("  任務清單只有一件（沒有開新任務）", sum(1 for i in lst["items"] if i["id"] == tid) == 1 and lst["items"][0]["turns"] == 2)

    # ---- 不能接的情況
    SCRIPT[:] = [say("", call("run_command", command="echo wait"))]
    st, t3 = req("POST", f"/api/agent/tasks/{tid}/continue", {"prompt": "再改一次"})
    wait(tid, ("waiting_approval",))
    st, out = req("POST", f"/api/agent/tasks/{tid}/continue", {"prompt": "又一次"})
    check("進行中的任務不能再追問 → 409", st == 409, out)
    req("POST", f"/api/agent/tasks/{tid}/stop", {})
    wait(tid, ("stopped",))
    st, out = req("POST", f"/api/agent/tasks/{tid}/continue", {"prompt": ""})
    check("  沒寫內容 → 400", st == 400)
    tk = x.AGENT_TASKS[tid]
    tk.user = "bob"
    st, out = req("POST", f"/api/agent/tasks/{tid}/continue", {"prompt": "幫別人改"})
    check("  別人的任務不能接著交代 → 403", st == 403, out)
    tk.user = "admin"
    check("  一般使用者的路由有continue", x.user_may("POST", f"/api/agent/tasks/{tid}/continue"))

    # ---- 停掉之後馬上追問：舊的背景執行緒晚回來不會動到新的一輪
    SCRIPT[:] = [say("", call("run_command", command="echo stale > stale.txt"))]
    st, _ = req("POST", f"/api/agent/tasks/{tid}/continue", {"prompt": "跑一條指令"})
    wait(tid, ("waiting_approval",))
    req("POST", f"/api/agent/tasks/{tid}/stop", {})
    SCRIPT[:] = [say("", call("finish", summary="不跑指令了"))]
    st, _ = req("POST", f"/api/agent/tasks/{tid}/continue", {"prompt": "算了，不用跑"})
    t4 = wait(tid, ("done", "failed"))
    time.sleep(1.5)
    t4 = req("GET", f"/api/agent/tasks/{tid}")[1]
    check("停止後馬上追問：新的一輪做完，舊的那條指令沒有執行", t4["state"] == "done" and t4["summary"] == "不跑指令了"
          and not os.path.exists(os.path.join(proj, "stale.txt")), (t4["state"], t4.get("summary")))
    prev = x.AGENT_TASKS[tid].turns
    check("  每一輪各自記狀態（完成、完成、停止、停止、完成）", [p["state"] for p in prev] == ["done", "done", "stopped", "stopped", "done"],
          [p["state"] for p in prev])

    # ---- 落地：重啟主控台後也接得上
    rows = x.STORE.agent_task_rows()
    check("輪次存進資料庫也是密文", all(r["body"].startswith("enc:v1:") for r in rows) and not any("番茄鐘" in r["body"] for r in rows))
    with x._AGENT_LOCK:
        x.AGENT_TASKS.clear()
    x.agent_tasks_load()
    SCRIPT[:] = [say("", call("finish", summary="記得前情"))]
    REQS.clear()
    st, t5 = req("POST", f"/api/agent/tasks/{tid}/continue", {"prompt": "重啟後再問一次"})
    wait(tid, ("done", "failed"))
    um = user_msgs(REQS[0])
    check("重啟主控台後接著問：前面幾輪的交代都帶到", st == 200 and um[0] == "做一個番茄鐘網頁" and "標題加上25分鐘" in um and um[-1] == "重啟後再問一次", um)

    # ---- 能追問之前存的任務（沒有turns）
    legacy = {"id": "aaaaaaaaaaaa", "node": "n1", "folder": "proj", "model": {"kind": "provider", "id": model["id"], "model": "fake-tools"},
              "prompt": "舊任務", "label": "假模型", "state": "done", "summary": "舊的結論", "plan": [], "files": {}, "tree": [],
              "log": [{"type": "tool", "tool": "write_file", "args": {"path": "old.txt"}}, {"type": "tool", "tool": "update_plan", "args": {}}],
              "steps": 3, "created": time.time() - 100, "updated": time.time() - 50}
    lt = x.AgentTask.restore("admin", legacy)
    check("舊格式的任務讀回來當成一輪，操作清單從紀錄補", len(lt.turns) == 1 and lt.turns[0]["summary"] == "舊的結論"
          and lt.turns[0]["ops"] == ["寫檔old.txt"], lt.turns)
    hist = x.agent_history(lt.turns)
    check("  可以接著問：前情有交代、操作與結論", hist[0]["content"] == "舊任務" and "寫檔old.txt" in hist[1]["content"] and "舊的結論" in hist[1]["content"])

    # ---- 前情太長：只留最近幾輪，前面只留結論摘要
    turns = [dict(x.agent_turn(f"第{i}次交代" + "細節" * 500, "m"), state="done", summary=f"結論{i}" + "說明" * 400,
                  ops=[f"寫檔f{i}_{j}.txt" for j in range(60)]) for i in range(1, 21)]
    hist = x.agent_history(turns)
    size = sum(len(m["content"]) for m in hist)
    check("前情有總字數上限（20輪也不會無限長）", size <= x.AGENT_HISTORY_CHARS * 1.5, size)
    check("  最近一輪完整帶（交代與操作清單）", hist[-2]["content"].startswith("第20次交代") and "寫檔f20_59.txt" in hist[-1]["content"])
    check("  每輪的操作清單只留最後40項", "寫檔f20_19.txt" not in hist[-1]["content"] and "寫檔f20_20.txt" in hist[-1]["content"])
    check("  更早的幾輪只留結論摘要", "只留結論摘要" in hist[0]["content"] and "結論1" in hist[0]["content"] and "細節細節細節細節" not in hist[0]["content"][:400]
          or "只留結論摘要" in hist[0]["content"], hist[0]["content"][:200])
    full_pairs = (len(hist) - 2) // 2
    check(f"  完整帶的輪數不超過{x.AGENT_HISTORY_TURNS}", 1 <= full_pairs <= x.AGENT_HISTORY_TURNS, full_pairs)
    check("  訊息一問一答交錯（第三方API才收）", all(hist[i]["role"] == ("user" if i % 2 == 0 else "assistant") for i in range(len(hist))))

    # ================================================================ 畫面：拿掉功能就會失敗
    js = open(os.path.join(HERE, "static", "pages.js"), encoding="utf-8").read()
    css = open(os.path.join(HERE, "static", "style.css"), encoding="utf-8").read()
    check("畫面：做完的任務送出時接在同一件後面（/continue），另有「開新任務」", "/continue`" in js and '"開新任務"' in js and "canFollow()" in js)
    check("  即時狀態列：正在思考、正在寫（已產生）、正在執行指令，用時與步數", all(s in js for s in ("正在思考", "正在寫", "已產生", "正在執行指令", "用時", "步`")))
    check("  思考過程可收合", '"思考過程"' in js and 'el("details", "agent-think")' in js)
    check("  輪詢只拿新的紀錄（since）、檔案樹沒變不重送", "?since=${next}" in js and "&tree=" in js)
    check("  每秒的計時器只換用時那幾個字", "setInterval(() => { if (!liveBar.isConnected)" in js and "drawClock(); }, 1000)" in js)
    check("  狀態列與思考過程的樣式在", ".agent-live " in css and ".agent-think " in css)

    srv.shutdown()
    fake.shutdown()
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
