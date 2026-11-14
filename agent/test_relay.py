#!/usr/bin/env python3
"""代理的加密入口轉送（/relay/<欄位>）與舊金鑰雜湊（/apikey/<欄位>）：
- 換上模型自己的金鑰再送；狀態碼與回應原樣送回（含模型回的 4xx）
- 串流一行一行轉、llama.cpp 的 timings 照樣記進實測速度
- 路徑白名單：Anthropic／Responses 只有 Strata 欄位能用；方法不對、欄位不存在、沒權杖都擋
- /apikey 只回雜湊與前 7 個字元，不回金鑰本身
不需要 systemd：假引擎直接起，欄位只用來查埠與金鑰。跑法：python3 test_relay.py
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
W = tempfile.mkdtemp(prefix="xc-relay-")
os.environ["XCONSOLE_HOME"] = W
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

OK = True
KEY = "xc-engine-key-for-relay-test-0001"
SPEED = []
A.record_speed = lambda sid, tok_s, n: SPEED.append((sid, tok_s, n))


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    lp = free_port()
    cfg = os.path.join(W, "engine.json")
    json.dump({"port": lp, "model_name": "mock-a", "api_key": KEY, "args": ["--max-context", "8192"]}, open(cfg, "w"))
    eng = subprocess.Popen([sys.executable, os.path.join(ROOT, "test", "fixtures", "mock_llm.py"), cfg],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lcfg = os.path.join(W, "llama.json")
    json.dump({"port": lp, "alias": "mock-a", "model": "/x.gguf", "exe": "/bin/true"}, open(lcfg, "w"))
    A.save_json(A.REGISTRY, {"machine": {"name": "relay", "ip": "127.0.0.1", "memory_model": "discrete"},
                             "slots": [{"id": "brain", "role": "主腦", "unit": "xc-fake.service", "kind": "strata", "config": cfg},
                                       {"id": "small", "role": "小", "unit": "xc-fake2.service", "kind": "llama", "config": lcfg},
                                       {"id": "empty", "role": "空"}]})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{lp}/v1/models", timeout=1)
        except urllib.error.HTTPError:
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)

    def call(path, body=None, token=A.TOKEN, raw=False):
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST",
                                   headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            resp = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        data = resp.read()
        return resp.status, data if raw else json.loads(data or b"{}"), resp.headers

    try:
        chat = {"model": "mock-a", "messages": [{"role": "user", "content": "hi"}]}
        st, d, _ = call("/relay/brain", {"path": "chat/completions", "method": "POST", "body": chat}, token="")
        case("沒帶代理權杖 → 401", st == 401)
        st, d, _ = call("/relay/brain", {"path": "chat/completions", "method": "POST", "body": chat})
        case("非串流：換上模型金鑰送過去、回應原樣回來（含用量）", st == 200 and d["choices"][0]["message"]["content"] == "連線成功"
             and d["usage"]["prompt_tokens"] == 7, d)
        st, d, _ = call("/relay/brain", {"path": "models", "method": "GET"})
        case("模型清單（GET）", st == 200 and d["data"][0]["id"] == "mock-a", d)
        st, d, _ = call("/relay/brain", {"path": "chat/completions", "method": "POST", "body": dict(chat, model="nope")})
        case("模型回的 404 原樣送回", st == 404 and "not found" in json.dumps(d), (st, d))
        st, raw, h = call("/relay/brain", {"path": "chat/completions", "method": "POST",
                                           "body": dict(chat, stream=True, stream_options={"include_usage": True})}, raw=True)
        lines = [ln for ln in raw.decode().split("\n") if ln.startswith("data:")]
        case("串流：一段一段原樣轉、Content-Type 是 event-stream", st == 200 and "event-stream" in h.get("Content-Type", "")
             and len(lines) == 4 and lines[-1] == "data: [DONE]" and '"usage"' in lines[2], lines)
        case("  串流最後一段的 timings 照樣記進實測速度", SPEED and SPEED[-1] == ("brain", 42.0, 2), SPEED)
        st, d, _ = call("/relay/brain", {"path": "embeddings", "method": "POST", "body": {"model": "mock-a", "input": "x"}})
        case("embeddings", st == 200 and d["data"][0]["embedding"] == [0.1, 0.2], d)
        st, d, _ = call("/relay/brain", {"path": "messages", "method": "POST", "body": chat})
        case("Strata 欄位可以用 Anthropic 格式", st == 200 and d["usage"]["input_tokens"] == 7, d)
        st, d, _ = call("/relay/small", {"path": "messages", "method": "POST", "body": chat})
        case("llama.cpp 欄位不開 Anthropic 格式 → 404", st == 404, (st, d))
        st, d, _ = call("/relay/brain", {"path": "models", "method": "POST", "body": {}})
        case("方法不對 → 404", st == 404, (st, d))
        st, d, _ = call("/relay/brain", {"path": "../../admin", "method": "POST", "body": {}})
        case("路徑不在白名單 → 404", st == 404, (st, d))
        st, d, _ = call("/relay/nope", {"path": "models", "method": "GET"})
        case("欄位不存在 → 404", st == 404 and d.get("error") == "沒有這個欄位", d)
        st, d, _ = call("/relay/empty", {"path": "models", "method": "GET"})
        case("空欄位 → 404", st == 404, d)

        st, d, _ = call("/apikey/brain")
        case("/apikey：回雜湊與前 7 個字元，不回金鑰", st == 200 and d["set"] and d["hash"] == hashlib.sha256(KEY.encode()).hexdigest()
             and d["prefix"] == KEY[:7] and KEY not in json.dumps(d) and d["port"] == lp, d)
        st, d, _ = call("/apikey/small")
        case("/apikey：沒設金鑰的欄位 set=false、沒有雜湊", st == 200 and not d["set"] and d["hash"] is None and d["bind"] == "0.0.0.0", d)
        st, d, _ = call("/apikey/nope")
        case("/apikey：欄位不存在 → 404", st == 404)

        eng.terminate()
        eng.wait(5)
        st, d, _ = call("/relay/small", {"path": "models", "method": "GET"})
        case("模型服務沒在跑 → 502", st == 502 and "連不到模型服務" in d.get("error", ""), (st, d))
    finally:
        eng.kill()
        srv.shutdown()
    print("全部通過" if OK else "有失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
