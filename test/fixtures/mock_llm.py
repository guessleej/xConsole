#!/usr/bin/env python3
"""測試用的假模型服務：讀跟 Strata 同格式的設定檔（port、model_name、api_key、args 的 --max-context），
講 OpenAI 的 /v1/models 與 /v1/chat/completions（含工具呼叫）。
--max-context 超過 200000 就直接退出，用來測「套用後起不來 → 自動換回」。
跑法：python3 mock_llm.py <設定檔>（由 test_api_integration.py 掛成臨時 systemd 服務）"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.argv[1].startswith("-"):
    # 當成 llama-server：吃同樣的參數（-m、--port、--alias、-c、--api-key-file…）
    a = sys.argv[1:]
    opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d  # noqa: E731
    if "broken" in os.path.basename(opt("-m", "")):
        sys.exit("模型檔壞掉，起不來")
    cfg = {"port": int(opt("--port")), "model_name": opt("--alias", "mock")}
    ctx = int(opt("-c", 4096))
    kf = opt("--api-key-file")
    KEY = open(kf).read().strip() if kf else ""
    MODELS_PUBLIC = True              # llama.cpp 的 /v1/models 不需要金鑰，要能測出「不能拿它判斷有沒有金鑰」
else:
    cfg = json.load(open(sys.argv[1]))
    args = cfg.get("args") or []
    ctx = int(args[args.index("--max-context") + 1]) if "--max-context" in args else 4096
    if ctx > 200000:
        sys.exit("上下文太大，起不來")
    KEY = cfg.get("api_key") or ""
    MODELS_PUBLIC = False
NAME = cfg.get("model_name") or "mock"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _ok(self):
        if KEY and self.headers.get("Authorization", "") != f"Bearer {KEY}":
            self._send(401, {"error": "need key"})
            return False
        return True

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/redir/"):          # 測「實測不跟轉址」用
            self.send_response(302)
            self.send_header("Location", "/v1/models")   # 固定目標：測試只需要「有轉址」
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.rstrip("/") == "/v1/models" and (MODELS_PUBLIC or self._ok()):
            self._send(200, {"data": [{"id": NAME, "meta": {"n_ctx": ctx},
                                       "architecture": {"input_modalities": ["text"]}}]})
        elif not self.path.startswith("/v1/models"):
            self._send(404, {})

    def do_POST(self):  # noqa: N802
        if not self._ok():
            return
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if req.get("model") != NAME:
            return self._send(404, {"error": f"model {req.get('model')} not found"})
        if req.get("tools"):
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_time", "arguments": '{"city":"台北"}'}}]}
        else:
            msg = {"role": "assistant", "content": "連線成功"}
        self._send(200, {"choices": [{"message": msg}], "usage": {"total_tokens": 12}})


ThreadingHTTPServer(("0.0.0.0", int(cfg["port"])), H).serve_forever()
