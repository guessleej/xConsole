#!/usr/bin/env python3
"""第三方 API（OpenAI 相容）的自我檢查：金鑰加密落地、任何 GET 都不回金鑰、編輯時空白保留原金鑰、
中國來源標 cn_origin、http 只准私有位址與本機、不跟轉址、錯誤與稽核都遮罩金鑰、
對話的串流格式跟本機對話完全一樣（前端共用同一套解析）。
跑法：python3 test_providers.py（另起一個隔離的主控台與一個假的 OpenAI 相容服務，不連外）
"""
import glob
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-prov-")
os.environ["HOME"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xconsole_server as x  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

OK = True
KEY = "sk" + "-test-0123456789abcdefPLAINTEXT"
SSE = [b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n', b"\n",
       b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}\n', b"\n",
       b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n', b"\n", b"data: [DONE]\n", b"\n"]
SEEN = []
FLAGS = {"reject_stream_options": False}


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _out(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        SEEN.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            # 有的服務會把收到的金鑰原樣寫在錯誤裡：主控台要遮掉
            return self._out(401, {"error": {"message": f"Incorrect API key provided: {self.headers.get('Authorization')}"}})
        self._out(200, {"data": [{"id": "gpt-test"}, {"id": "qwen2.5-72b-instruct"}]})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        SEEN.append(("POST", self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self._out(401, {"error": {"message": "bad key"}})
        if FLAGS["reject_stream_options"] and "stream_options" in body:
            return self._out(400, {"error": {"message": "Unknown parameter: 'stream_options'"}})
        if not body.get("stream"):
            return self._out(200, {"choices": [{"message": {"role": "assistant", "content": "你好"}}], "usage": {"total_tokens": 5}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in SSE:
            self.wfile.write(line)
            self.wfile.flush()


fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=fake.serve_forever, daemon=True).start()
FAKE = f"http://127.0.0.1:{fake.server_address[1]}"

x.set_credentials("admin", "prov-test-pw")
srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
COOKIE = {}


def req(method, path, body=None, raw=False):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if COOKIE.get("v"):
        r.add_header("Cookie", "xconsole=" + COOKIE["v"])
    try:
        resp = urllib.request.urlopen(r, timeout=20)
    except urllib.error.HTTPError as e:
        resp = e
    payload = resp.read()
    sc = resp.headers.get("Set-Cookie") or ""
    if sc.startswith("xconsole="):
        COOKIE["v"] = sc.split(";")[0].split("=", 1)[1]
    return resp.status, (payload if raw else json.loads(payload or b"{}")), resp.headers


def main():
    st, _, _ = req("GET", "/api/providers")
    check("沒登入看不到第三方 API", st == 401)
    req("POST", "/api/login", {"username": "admin", "password": "prov-test-pw"})

    st, out, _ = req("GET", "/api/providers")
    names = [p["name"] for p in out["presets"]]
    check("預設清單：OpenAI、Gemini、Anthropic、Mistral、自訂", st == 200 and names[:4] == ["OpenAI", "Google Gemini", "Anthropic Claude", "Mistral"]
          and out["presets"][-1]["key"] == "custom", names)
    check("  預設清單不內建中國廠商", not any(x.cn_origin_host(p["base_url"], p["name"]) for p in out["presets"]))
    check("  Gemini 用 OpenAI 相容端點", any(p["base_url"].endswith("/v1beta/openai") for p in out["presets"]))

    # ---- 網址檢查
    for bad, why in (("http://8.8.8.8/v1", "外部位址不准 http"), ("http://169.254.169.254/latest", "雲端 metadata"),
                     ("ftp://example.com/v1", "不是 http(s)"), ("https://user:pw@example.com/v1", "網址帶帳密"),
                     ("https://example.com/v1?key=abc", "網址帶查詢字串（金鑰不准進網址）"), ("", "空白")):
        st, out, _ = req("POST", "/api/providers/save", {"name": "x", "base_url": bad})
        check(f"擋下：{why}", st == 400, out)
    st, out, _ = req("POST", "/api/providers/save", {"name": "<b>", "base_url": "https://api.openai.com/v1"})
    check("名稱不能有 < >", st == 400)

    # ---- 新增：金鑰加密落地
    st, out, _ = req("POST", "/api/providers/save", {"name": "本機假服務", "base_url": FAKE + "/v1/", "api_key": KEY,
                                                     "default_model": "gpt-test"})
    check("新增（http 只准本機／私有位址，這筆是本機）", st == 200 and out["provider"]["key_set"], out)
    pid = out["provider"]["id"]
    check("  回應不含金鑰、網址去掉結尾斜線", KEY not in json.dumps(out) and out["provider"]["base_url"] == FAKE + "/v1")
    row = x.STORE.provider_get(pid)
    check("  資料庫裡是 enc:v1: 密文（AES-256-GCM）", row["api_key"].startswith("enc:v1:") and KEY not in row["api_key"])
    check("  解得回原文（主金鑰在 master.key）", x.provider_key(row) == KEY)
    files = glob.glob(os.path.join(TMP, ".xconsole", "xconsole.db*"))
    check("  資料庫檔案（含 WAL）找不到明文金鑰", files and not any(KEY.encode() in open(f, "rb").read() for f in files), files)
    try:
        x.STORE.provider_put({**row, "api_key": "plaintext-key"})
        check("  儲存層拒收沒加密的金鑰", False)
    except ValueError:
        check("  儲存層拒收沒加密的金鑰", True)
    st, out, _ = req("GET", "/api/providers")
    check("GET 只回 key_set，不回金鑰內容", KEY not in json.dumps(out) and "api_key" not in out["items"][0]
          and out["items"][0]["key_set"] is True, out)

    # ---- 編輯：金鑰欄空白＝保留
    st, out, _ = req("POST", "/api/providers/save", {"id": pid, "name": "改名", "base_url": FAKE + "/v1", "api_key": "",
                                                     "default_model": "gpt-test"})
    check("編輯時金鑰空白：保留原金鑰", st == 200 and x.provider_key(x.STORE.provider_get(pid)) == KEY and out["provider"]["name"] == "改名", out)
    st, out, _ = req("POST", f"/api/providers/{pid}/models", {})
    check("取模型清單：用存著的金鑰打 {base}/models", st == 200 and [m["id"] for m in out["models"]] == ["gpt-test", "qwen2.5-72b-instruct"], out)
    check("  模型名稱是中國來源的標 cn_origin", [m["cn_origin"] for m in out["models"]] == [False, True])
    check("  金鑰走 Authorization 標頭，不進網址", SEEN[-1][2] == f"Bearer {KEY}" and KEY not in SEEN[-1][1])

    st, out, _ = req("POST", "/api/providers/save", {"id": pid, "name": "改名", "base_url": FAKE + "/v1", "api_key": "sk" + "-wrong-key-123456789"})
    st, out, _ = req("POST", f"/api/providers/{pid}/models", {})
    check("金鑰不對：回錯誤並講是金鑰的問題", st == 400 and "金鑰" in out["error"], out)
    check("  對方把金鑰寫在錯誤裡：回給畫面前遮掉", "sk" + "-wrong-key-123456789" not in json.dumps(out) and "[已遮罩]" in out["error"], out)
    st, out, _ = req("POST", "/api/providers/save", {"id": pid, "name": "改名", "base_url": FAKE + "/v1", "api_key": KEY})
    check("換回正確的金鑰", st == 200)

    st, out, _ = req("POST", "/api/providers/save", {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "api_key": "sk" + "-ds-123456789012"})
    check("中國來源的服務標 cn_origin（使用者自己選自訂填）", st == 200 and out["provider"]["cn_origin"] is True, out)
    ds = out["provider"]["id"]
    st, out, _ = req("POST", "/api/providers/save", {"name": "OpenAI", "base_url": "https://api.openai.com/v1"})
    check("  OpenAI 不標", out["provider"]["cn_origin"] is False and out["provider"]["key_set"] is False)
    st, out, _ = req("POST", "/api/providers/save", {"id": ds, "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "clear_key": True})
    check("清除金鑰（clear_key）", out["provider"]["key_set"] is False)

    # ---- 不跟轉址
    st, out, _ = req("POST", "/api/providers/save", {"name": "轉址", "base_url": FAKE + "/redir", "api_key": KEY})
    rid = out["provider"]["id"]
    st, out, _ = req("POST", f"/api/providers/{rid}/models", {})
    check("對方回轉址：不跟（金鑰不能被帶到別的主機）", st == 502 and "轉址" in out["error"], out)

    # ---- 對話：串流格式跟本機對話一樣
    msgs = [{"role": "user", "content": "嗨"}]
    st, raw, h = req("POST", f"/api/providers/{pid}/chat", {"messages": msgs, "stream": True}, raw=True)
    check("串流對話：回 text/event-stream、內容逐行原樣轉送", st == 200 and h.get("Content-Type") == "text/event-stream; charset=utf-8"
          and raw == b"".join(SSE), raw[:200])
    sent = SEEN[-1][3]
    check("  送出去的請求：模型用預設、帶 stream_options（用量統計）", sent["model"] == "gpt-test" and sent["stream_options"] == {"include_usage": True})

    # 本機對話（_chat）經代理轉送：同一份上游串流，主控台送出的位元組與標頭要一模一樣
    real_nr = x.node_request
    x.node_request = lambda nid, path, data, ctype="application/json": (urllib.request.Request(
        FAKE + "/v1/chat/completions", data=json.dumps({"messages": msgs, "stream": True}).encode(), method="POST",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}), None)
    try:
        st2, raw2, h2 = req("POST", "/api/node/n1/chat/s1", {"messages": msgs, "stream": True}, raw=True)
    finally:
        x.node_request = real_nr
    check("  跟本機對話的輸出逐位元組相同、Content-Type 相同", st2 == 200 and raw2 == raw and h2.get("Content-Type") == h.get("Content-Type"),
          (raw2[:120], h2.get("Content-Type")))

    FLAGS["reject_stream_options"] = True
    st, raw, h = req("POST", f"/api/providers/{pid}/chat", {"messages": msgs, "stream": True}, raw=True)
    FLAGS["reject_stream_options"] = False
    check("對方不認 stream_options：自動拿掉重送", st == 200 and raw == b"".join(SSE) and "stream_options" not in SEEN[-1][3])
    st, out, _ = req("POST", f"/api/providers/{pid}/chat", {"messages": msgs, "stream": False})
    check("不串流：回 JSON 原樣", st == 200 and out["choices"][0]["message"]["content"] == "你好", out)
    st, out, _ = req("POST", f"/api/providers/{pid}/chat", {"messages": [{"role": "hacker", "content": "x"}]})
    check("對話內容不對 → 400", st == 400)
    st, out, _ = req("POST", f"/api/providers/{ds}/chat", {"messages": msgs})
    check("沒有預設模型也沒指定 → 400 講清楚", st == 400 and "模型" in out["error"], out)

    # ---- 刪除與稽核
    st, out, _ = req("POST", f"/api/providers/{rid}/delete", {})
    check("刪除", st == 200 and x.STORE.provider_get(rid) is None)
    st, out, _ = req("GET", "/api/admin/audit?limit=200")
    dump = json.dumps(out, ensure_ascii=False)
    check("稽核記了新增、編輯、取模型、對話、刪除", all(a in dump for a in ("provider.save", "provider.models", "provider.chat", "provider.delete")))
    check("  稽核沒有任何金鑰（明文或密文）", KEY not in dump and "sk" + "-wrong-key" not in dump and "enc:v1:" not in dump)
    check("  稽核記了金鑰是換新、保留還是清除", all(w in dump for w in ("換新", "保留", "清除")))
    srv.shutdown()
    fake.shutdown()
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
