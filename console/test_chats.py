#!/usr/bin/env python3
"""對話紀錄與對話參數的自我檢查：
- 存進主控台後內容與附圖都是 enc:v1: 密文（資料庫檔、WAL 都找不到明文）
- 別人的對話看不到、改不到、刪不到；附圖也一樣
- 搜尋找得到內容（要解密才比得到）、改名、刪除、每人上限 500 則
- 附圖拆成獨立 blob，訊息刪掉後附圖跟著刪
- 取樣參數：範圍檢查；第三方 API 只轉送 OpenAI 標準參數（Top K、Min P、重複懲罰不送）；種子 -1 不送
- 代理端 chat_request 同一套範圍
跑法：python3 test_chats.py（隔離 HOME，不連外）
"""
import base64
import glob
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-chat-")
os.environ["HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent"))
import chats  # noqa: E402
import xconsole_server as x  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

OK = True
SECRET = "機密內容-客戶名單-0912345678"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 50).decode()
SEEN = []


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        SEEN.append(body)
        b = json.dumps({"choices": [{"message": {"role": "assistant", "content": "好"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=fake.serve_forever, daemon=True).start()
FAKE = f"http://127.0.0.1:{fake.server_address[1]}"

x.set_credentials("admin", "chat-test-pw")
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
    st, _, _ = req("GET", "/api/chats")
    check("沒登入看不到對話紀錄", st == 401)
    req("POST", "/api/login", {"username": "admin", "password": "chat-test-pw"})

    msgs = [{"role": "user", "content": f"請記住：{SECRET}", "image": f"data:image/png;base64,{PNG}",
             "docs": [{"name": "會議記錄.pdf", "text": "附件裡才有的字：海龜湯", "chars": 10}]},
            {"role": "assistant", "content": "好的，記住了。", "alts": [{"content": "第一版"}, {"content": "好的，記住了。"}], "alt": 1}]
    st, out, _ = req("POST", "/api/chats/save", {"title": "標題也機密-客戶甲", "target": "slot:n1:s1", "msgs": msgs,
                                                 "settings": {"system": "用條列", "temperature": 0.7, "top_k": 40}})
    check("新增對話：回編號與附圖對應", st == 200 and len(out.get("id", "")) == 12 and out["images"].get("0", "").startswith("blob:"), out)
    cid, ref = out["id"], out["images"]["0"]

    row = x.STORE.chat_get("admin", cid)
    check("  資料庫裡內容是 enc:v1: 密文", row["body"].startswith("enc:v1:") and SECRET not in row["body"])
    check("  標題也是 enc:v1: 密文（取自第一句話，一樣是使用者內容）", row["title"].startswith("enc:v1:") and "客戶甲" not in row["title"])
    blob = x.STORE.blob_get("admin", ref[5:])
    check("  附圖也是 enc:v1: 密文", blob["data"].startswith("enc:v1:"))
    files = glob.glob(os.path.join(TMP, ".xconsole", "xconsole.db*"))
    check("  資料庫檔案（含 WAL）找不到明文（內容與標題）", files and not any(SECRET.encode() in open(f, "rb").read()
                                                       or "客戶甲".encode() in open(f, "rb").read() for f in files), files)
    try:
        x.STORE.chat_put("admin", {"id": "0" * 12, "body": "plain"})
        check("  儲存層拒收沒加密的內容", False)
    except ValueError:
        check("  儲存層拒收沒加密的內容", True)

    st, out, _ = req("GET", f"/api/chats/{cid}")
    check("讀回：訊息、版本、設定都在，附圖換成 blob 參照", st == 200 and out["msgs"][0]["content"].endswith(SECRET)
          and out["msgs"][0]["image"] == ref and out["msgs"][1]["alt"] == 1 and out["settings"] == {"system": "用條列", "temperature": 0.7, "top_k": 40}, out)
    st, raw, h = req("GET", f"/api/chats/blob/{ref[5:]}", raw=True)
    check("附圖取回原始位元組與型別", st == 200 and raw == base64.b64decode(PNG) and h.get("Content-Type") == "image/png")

    st, out, _ = req("GET", "/api/chats?q=" + urllib.parse.quote("客戶名單"))
    check("搜尋找得到內容（解密後比對）並帶摘錄", st == 200 and [i["id"] for i in out["items"]] == [cid] and "客戶名單" in out["items"][0]["snippet"], out)
    st, out, _ = req("GET", "/api/chats?q=" + urllib.parse.quote("海龜湯"))
    check("  附件抽出的文字也搜得到", [i["id"] for i in out["items"]] == [cid], out)
    st, out, _ = req("GET", "/api/chats?q=" + urllib.parse.quote("客戶甲"))
    check("  搜得到標題（解密後比對），列表回的是明文標題", [i["title"] for i in out["items"]] == ["標題也機密-客戶甲"], out)
    st, out, _ = req("GET", "/api/chats?q=" + urllib.parse.quote("不存在的字"))
    check("  找不到就是空的", out["items"] == [])
    st, out, _ = req("GET", "/api/chats")
    check("列表不回內容（只有標題、時間、則數）", st == 200 and "body" not in out["items"][0] and out["items"][0]["n_msgs"] == 2, out)

    st, out, _ = req("POST", f"/api/chats/{cid}/rename", {"title": "改過的標題"})
    check("改名（存的是密文、讀回是新標題）", st == 200 and x.STORE.chat_get("admin", cid)["title"].startswith("enc:v1:")
          and req("GET", f"/api/chats/{cid}")[1]["title"] == "改過的標題")

    # ---- 別人的對話
    x.STORE.chat_put("alice", {"id": "a" * 12, "title": x.chat_seal("alice 的".encode()), "body": x.chat_seal(b'{"msgs":[]}')})
    st, out, _ = req("GET", "/api/chats/" + "a" * 12)
    check("別人的對話讀不到（當作不存在）", st == 404)
    st, out, _ = req("POST", "/api/chats/save", {"id": "a" * 12, "msgs": [{"role": "user", "content": "蓋掉"}]})
    check("  也蓋不掉", st == 404 and x.chat_unseal(x.STORE.chat_get("alice", "a" * 12)["title"]).decode() == "alice 的")
    st, _, _ = req("POST", "/api/chats/" + "a" * 12 + "/delete", {})
    check("  也刪不掉", st == 404 and x.STORE.chat_get("alice", "a" * 12))
    x.STORE.blob_put("alice", "a" * 12, "b" * 16, "image/png", x.chat_seal(b"x"))
    st, _, _ = req("GET", "/api/chats/blob/" + "b" * 16)
    check("  別人的附圖讀不到", st == 404)
    st, out, _ = req("POST", "/api/chats/save", {"id": cid, "msgs": [{"role": "user", "content": "x", "image": "blob:" + "b" * 16}]})
    check("  不能引用別人的附圖", st == 400, out)

    # ---- 格式檢查
    for bad, why in (({"msgs": [{"role": "system", "content": "x"}]}, "角色只能是使用者與助理"),
                     ({"msgs": [{"role": "user", "content": "x", "image": "data:image/svg+xml;base64,AAAA"}]}, "附圖不收 SVG"),
                     ({"msgs": [{"role": "user", "content": "x" * 400_001}]}, "單則太長"),
                     ({"msgs": [], "settings": {"temperature": 3}}, "溫度超出範圍"),
                     ({"msgs": [], "settings": {"top_k": 1.5}}, "Top K 要整數"),
                     ({"id": "../etc/passwd", "msgs": []}, "編號格式"),
                     ({"msgs": [{"role": "assistant", "content": "", "gen": {"node": "n1", "job": "zz", "kind": "image"}}]}, "畫圖工作編號格式"),
                     ({"msgs": [{"role": "assistant", "content": "", "gen": {"node": "n1", "job": "0123abcd", "kind": "image", "edit": True,
                                                                              "base": {"node": "../x", "job": "0123abcd"}}}]}, "改圖來源的機器格式")):
        st, out, _ = req("POST", "/api/chats/save", bad)
        check(f"擋下：{why}", st == 400, out)

    gen = {"node": "n1", "job": "0123abcd", "kind": "image", "edit": True, "base": {"node": "n1", "job": "89abcdef", "extra": "x"}}
    st, out, _ = req("POST", "/api/chats/save", {"msgs": [{"role": "user", "content": "把背景換成海邊"}, {"role": "assistant", "content": "", "gen": gen}]})
    saved = chats.clean_msgs([{"role": "assistant", "content": "", "gen": gen}])[0][0]["gen"] if st == 200 else None
    check("接著改上一張：存下改的是哪一件（只存機器與工作編號）", st == 200 and saved and saved.get("base") == {"node": "n1", "job": "89abcdef"}, (st, out, saved))

    # ---- 訊息刪掉，附圖跟著刪
    st, out, _ = req("POST", "/api/chats/save", {"id": cid, "title": "t", "msgs": [{"role": "user", "content": "只剩文字"}]})
    check("訊息改掉後沒被引用的附圖一併刪除", st == 200 and x.STORE.blob_get("admin", ref[5:]) is None)

    # ---- 上限：每人 500 則，超過刪最舊的
    for i in range(chats.KEEP_PER_USER + 3):
        x.STORE.chat_put("bob", {"id": f"{i:012x}", "title": x.chat_seal(str(i).encode()), "body": x.chat_seal(b"{}")}, keep=chats.KEEP_PER_USER)
    left = x.STORE.chat_list("bob")
    check("每人最多 500 則，超過刪最舊的", len(left) == chats.KEEP_PER_USER and "0" * 12 not in {r["id"] for r in left})

    # ---- 附圖總量上限（一個帳號不能把主控台的磁碟塞滿）
    real_pc = chats.BLOB_PER_CHAT
    chats.BLOB_PER_CHAT = 500
    try:
        st, out, _ = req("POST", "/api/chats/save", {"id": cid, "msgs": [{"role": "user", "content": "x", "image": f"data:image/png;base64,{PNG}"},
                                                                       {"role": "user", "content": "y", "image": f"data:image/png;base64,{PNG}"}]})
        check("附圖超過這則對話的總量上限：拒收", st == 413, out)
    finally:
        chats.BLOB_PER_CHAT = real_pc
    try:
        x.STORE.chat_put("admin", {"id": cid, "title": "plain", "body": x.chat_seal(b"{}")})
        check("儲存層拒收沒加密的標題", False)
    except ValueError:
        check("儲存層拒收沒加密的標題", True)

    # ---- 收藏、分類、標籤
    st, out, _ = req("POST", f"/api/chats/{cid}/meta", {"fav": True, "category": "工作", "tags": ["#客戶甲", "報價", "報價"]})
    check("設收藏、分類、標籤（標籤去掉 #、去重）", st == 200 and out["fav"] is True and out["category"] == "工作"
          and out["tags"] == ["客戶甲", "報價"], out)
    row = x.STORE.chat_get("admin", cid)
    check("  分類標籤是 enc:v1: 密文、收藏是 0／1", row["labels"].startswith("enc:v1:") and "報價" not in row["labels"] and row["fav"] == 1)
    before = row["updated"]
    st, out, _ = req("GET", "/api/chats")
    it = next(i for i in out["items"] if i["id"] == cid)
    check("  列表帶收藏、分類、標籤、訊息數", it["fav"] is True and it["category"] == "工作" and it["tags"] == ["客戶甲", "報價"]
          and "n_msgs" in it, it)
    check("  按收藏不改最後更新時間（列表順序不跳）", x.STORE.chat_get("admin", cid)["updated"] == before)
    req("POST", "/api/chats/save", {"id": cid, "title": "t", "msgs": [{"role": "user", "content": "存一次內容"}]})
    st, out, _ = req("GET", f"/api/chats/{cid}")
    check("  存對話內容不會把收藏與分類標籤洗掉", out["fav"] is True and out["category"] == "工作" and out["tags"] == ["客戶甲", "報價"], out)
    st, out, _ = req("GET", "/api/chats?q=" + urllib.parse.quote("報價"))
    check("  搜尋找得到標籤", [i["id"] for i in out["items"]] == [cid], out)
    st, out, _ = req("POST", f"/api/chats/{cid}/meta", {"category": "", "tags": []})
    check("  清掉分類標籤（收藏不動）", st == 200 and out["fav"] is True and out["category"] == "" and out["tags"] == []
          and not x.STORE.chat_get("admin", cid)["labels"], out)
    for bad, why in (({"fav": "yes"}, "收藏要布林"), ({"category": "長" * 41}, "分類太長"), ({"tags": ["t"] * 11}, "標籤太多"),
                     ({"tags": ["長" * 31]}, "標籤太長"), ({"tags": "abc"}, "標籤要是清單")):
        st, out, _ = req("POST", f"/api/chats/{cid}/meta", bad)
        check(f"  擋下：{why}", st == 400, out)
    st, out, _ = req("POST", "/api/chats/" + "a" * 12 + "/meta", {"fav": True})
    check("  別人的對話不能設收藏", st == 404 and not x.STORE.chat_get("alice", "a" * 12)["fav"])
    check("  使用者也能用（路由開放）", x.user_may("POST", f"/api/chats/{cid}/meta"))
    st, out, _ = req("POST", "/api/chats/save", {"id": cid, "msgs": [{"role": "user", "content": "x"}],
                                                 "settings": {"think": False, "tool_allow": ["command", "亂寫"]}})
    st, out, _ = req("GET", f"/api/chats/{cid}")
    check("對話設定存「思考：關」與不再詢問的工具類別（只收認得的）", out["settings"] == {"think": False, "tool_allow": ["command"]}, out["settings"])

    st, out, _ = req("POST", f"/api/chats/{cid}/delete", {})
    check("刪除", st == 200 and x.STORE.chat_get("admin", cid) is None)

    # ---- 取樣參數
    try:
        chats.sampling({"temperature": 0.5, "top_k": 40, "min_p": 0.05, "repeat_penalty": 1.1, "seed": 7})
        check("取樣參數：合法的全收", True)
    except chats.Bad as exc:
        check("取樣參數：合法的全收", False, str(exc))
    for bad in ({"top_p": 1.5}, {"seed": "abc"}, {"max_tokens": 0}, {"temperature": True}):
        try:
            chats.sampling(bad)
            check(f"  擋下 {bad}", False)
        except chats.Bad:
            check(f"  擋下 {bad}", True)

    st, out, _ = req("POST", "/api/providers/save", {"name": "本機假服務", "base_url": FAKE + "/v1", "api_key": "sk-" + "x" * 20,
                                                     "default_model": "m"})
    pid = out["provider"]["id"]
    req("POST", f"/api/providers/{pid}/chat", {"messages": [{"role": "user", "content": "嗨"}], "stream": False,
                                               "temperature": 0.3, "top_p": 0.9, "top_k": 40, "min_p": 0.05, "repeat_penalty": 1.1,
                                               "presence_penalty": 0.5, "seed": -1, "max_tokens": 100})
    sent = SEEN[-1]
    check("第三方 API 只送標準參數（Top K、Min P、重複懲罰不送）", sent.get("temperature") == 0.3 and sent.get("top_p") == 0.9
          and sent.get("presence_penalty") == 0.5 and sent.get("max_tokens") == 100
          and not any(k in sent for k in ("top_k", "min_p", "repeat_penalty")), sent)
    check("  種子 -1（隨機）不送", "seed" not in sent, sent)
    st, out, _ = req("POST", f"/api/providers/{pid}/chat", {"messages": [{"role": "user", "content": "嗨"}], "top_p": 2})
    check("  範圍不對回 400", st == 400, out)

    # ---- 代理端同一套
    import xconsole_agent as A  # noqa: E402
    A._slot = lambda sid: {"unit": "u", "id": sid}
    A._api_parts = lambda slot: ({"model_name": "m"}, 8080, None)
    r = A.chat_request("s1", {"messages": [{"role": "user", "content": "嗨"}], "top_k": 40, "min_p": 0.05,
                              "repeat_penalty": 1.1, "seed": 3, "presence_penalty": 0.2})
    check("代理：llama.cpp 擴充參數照送", isinstance(r[2], dict) and r[2]["top_k"] == 40 and r[2]["min_p"] == 0.05
          and r[2]["repeat_penalty"] == 1.1 and r[2]["seed"] == 3, r)
    r = A.chat_request("s1", {"messages": [{"role": "user", "content": "嗨"}], "seed": -1})
    check("  種子 -1 不送", "seed" not in r[2], r)
    r = A.chat_request("s1", {"messages": [{"role": "user", "content": "嗨"}], "top_k": 1.5})
    check("  Top K 不是整數回 400", r[0] == 400, r)
    r = A.chat_request("s1", {"messages": [{"role": "user", "content": "嗨"}], "chat_template_kwargs": {"enable_thinking": False, "x": 1}})
    check("  思考開關：只轉送 enable_thinking 一個布林", r[2].get("chat_template_kwargs") == {"enable_thinking": False}, r)
    r = A.chat_request("s1", {"messages": [{"role": "user", "content": "嗨"}], "chat_template_kwargs": {"enable_thinking": "no"}})
    check("  不是布林就不送", "chat_template_kwargs" not in r[2], r)
    check("  對話樣板認得 enable_thinking 才算可切換思考",
          A.gguf_profile({"general.architecture": "qwen3", "tokenizer.chat_template": "{% if enable_thinking %}<think>{% endif %}"})["thinking"]
          and not A.gguf_profile({"general.architecture": "llama", "tokenizer.chat_template": "{{ messages }}"})["thinking"])
    check("  換模型時「思考」能力依新模型重算", A.model_caps({"caps": ["文字", "思考"]}, {"thinking": False}) == ["文字"]
          and A.model_caps({"caps": []}, {"thinking": True, "vision": True}) == ["思考", "文字", "看圖"])

    print("全部通過" if OK else "有失敗")
    sys.exit(0 if OK else 1)


if __name__ == "__main__":
    import urllib.parse  # noqa: E402,F401
    main()
