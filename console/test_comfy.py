#!/usr/bin/env python3
"""ComfyUI串接的自我檢查：起一台假的ComfyUI（HTTP＋WebSocket，另一台走HTTPS自簽），從主控台的API走完整流程。
涵蓋：
- 網址：只收http(s)，不能帶帳密與查詢字串；http只准私有位址；自動尋找只試固定埠
- 連線測試：/system_stats回HTML、404、401、轉址各講人話；HTTPS沒匯入CA驗不過，匯入CA才過（不提供關閉驗證）
- 金鑰：AES-256-GCM加密落地（fleet.json找不到明文）；只走Authorization標頭、不進網址；換網址不沿用舊金鑰
- 工作流：一般格式轉API格式（Reroute、PrimitiveNode、略過、靜音、註解、種子後面的「產生後」選單）；匯出（API）直接收
- 缺節點列名稱並提示ComfyUI Manager；缺模型講要放哪個models子資料夾；沒有輸出節點擋下
- 產生：上傳輸入圖片、WebSocket追進度、取回產出（看檔頭，HTML不收）、取消、節點出錯指出哪個節點、WebSocket不通改輪詢
- 權限：使用者只看得到自己的產出；設定、匯入、資料夾只有管理員；刪帳號一起刪；主控台重啟標成中斷
- ComfyUI資料夾：代理只列模型檔名稱與大小，根目錄或models資料夾都收，不是ComfyUI資料夾講清楚
- 反向驗尺：每一道檢查都先拿會出錯的樣本確認抓得到
跑法：python3 test_comfy.py（不連外、不碰正式環境）
"""
import base64
import copy
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TMP = tempfile.mkdtemp(prefix="xc-comfy-")
os.environ["XCONSOLE_HOME"] = TMP
os.environ["HOME"] = TMP
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "agent")]
import comfy  # noqa: E402
import tls  # noqa: E402
import xconsole_server as x  # noqa: E402
import xconsole_agent as A  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

OK = True
KEY = "comfy-test-key-" + os.urandom(6).hex()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
ADMIN_PW = "comfy-admin-pass-2026"


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def wait(fn, secs=15):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return None


# ---------------------------------------------------------------- 假ComfyUI

def spec(t, **cfg):
    return [t, cfg] if cfg else [t]


OBJECT_INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["sd15.safetensors", "sub/flux.safetensors"]]}},
                               "output": ["MODEL", "CLIP", "VAE"], "output_node": False},
    "CLIPTextEncode": {"input": {"required": {"text": spec("STRING", multiline=True), "clip": spec("CLIP")}},
                       "output": ["CONDITIONING"], "output_node": False},
    "FluxGuidance": {"input": {"required": {"conditioning": spec("CONDITIONING"), "guidance": spec("FLOAT", min=0.0, max=100.0)}},
                     "output": ["CONDITIONING"], "output_node": False},
    "EmptyLatentImage": {"input": {"required": {"width": spec("INT", min=16, max=8192, step=8), "height": spec("INT", min=16, max=8192, step=8),
                                                "batch_size": spec("INT", min=1, max=64)}}, "output": ["LATENT"], "output_node": False},
    "KSampler": {"input": {"required": {"model": spec("MODEL"), "seed": spec("INT", min=0, max=2**64 - 1, control_after_generate=True),
                                        "steps": spec("INT", min=1, max=10000), "cfg": spec("FLOAT", min=0.0, max=100.0),
                                        "sampler_name": [["euler", "dpmpp_2m"]], "scheduler": ["COMBO", {"options": ["normal", "karras"]}],
                                        "positive": spec("CONDITIONING"), "negative": spec("CONDITIONING"), "latent_image": spec("LATENT"),
                                        "denoise": spec("FLOAT", min=0.0, max=1.0, step=0.01)}}, "output": ["LATENT"], "output_node": False},
    "VAEDecode": {"input": {"required": {"samples": spec("LATENT"), "vae": spec("VAE")}}, "output": ["IMAGE"], "output_node": False},
    "SaveImage": {"input": {"required": {"images": spec("IMAGE"), "filename_prefix": spec("STRING", default="ComfyUI")}},
                  "output": [], "output_node": True},
    "SaveVideo": {"input": {"required": {"video": spec("VIDEO"), "filename_prefix": spec("STRING")}}, "output": [], "output_node": True},
    "LoadImage": {"input": {"required": {"image": [["example.png"], {"image_upload": True}]}}, "output": ["IMAGE", "MASK"], "output_node": False},
    "ImageScale": {"input": {"required": {"image": spec("IMAGE"), "width": spec("INT", min=0, max=8192), "height": spec("INT", min=0, max=8192)}},
                   "output": ["IMAGE"], "output_node": False},
}


class Fake:
    """一台ComfyUI的狀態：收到的請求、上傳的圖、送出的工作、連上的WebSocket。"""

    def __init__(self):
        self.reqs, self.uploads, self.history, self.ws = [], {}, {}, {}
        self.interrupted, self.deleted = set(), []
        self.mode = {"stats": "ok", "ws": True, "output": "png"}
        self.lock = threading.Lock()


def make_handler(fk):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = json.dumps(body).encode() if not isinstance(body, bytes) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _log(self):
            with fk.lock:
                fk.reqs.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization") or ""})

        def do_GET(self):  # noqa: N802
            self._log()
            u = urllib.parse.urlparse(self.path)
            if u.path == "/system_stats":
                m = fk.mode["stats"]
                if m == "html":
                    return self._send(200, b"<html><body>login</body></html>", "text/html")
                if m == "404":
                    return self._send(404, b"not found", "text/plain")
                if m == "401":
                    return self._send(401, {"error": "unauthorized"})
                if m == "302":
                    self.send_response(302)
                    self.send_header("Location", "http://169.254.169.254/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                if m == "json":
                    return self._send(200, {"hello": "world"})
                return self._send(200, {"system": {"os": "posix", "comfyui_version": "0.3.99", "python_version": "3.12.1"},
                                        "devices": [{"name": "cpu", "type": "cpu", "vram_total": 0}]})
            if u.path == "/object_info":
                return self._send(200, OBJECT_INFO)
            if u.path.startswith("/history/"):
                pid = u.path.rsplit("/", 1)[1]
                with fk.lock:
                    h = fk.history.get(pid)
                return self._send(200, {pid: h} if h else {})
            if u.path == "/view":
                q = urllib.parse.parse_qs(u.query)
                with fk.lock:
                    fk.reqs[-1]["view"] = q
                if fk.mode["output"] == "html":
                    return self._send(200, b"<html><script>alert(1)</script></html>", "text/html")
                return self._send(200, PNG, "image/png")
            if u.path == "/ws":
                if not fk.mode["ws"]:
                    return self._send(404, b"no ws", "text/plain")
                cid = urllib.parse.parse_qs(u.query)["clientId"][0]
                acc = base64.b64encode(hashlib.sha1(self.headers["Sec-WebSocket-Key"].encode() + comfy.WS_GUID).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", acc)
                self.end_headers()
                self.wfile.flush()
                with fk.lock:
                    fk.ws[cid] = self.connection
                self.connection.settimeout(None)
                try:
                    while self.connection.recv(1024):
                        pass
                except OSError:
                    pass
                self.close_connection = True
                return None
            return self._send(404, b"nope", "text/plain")

        def do_POST(self):  # noqa: N802
            self._log()
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if self.path == "/upload/image":
                m = re.search(rb'filename="([^"]+)"', raw)
                name = m.group(1).decode()
                with fk.lock:
                    fk.uploads[name] = raw
                return self._send(200, {"name": name, "subfolder": "", "type": "input"})
            body = json.loads(raw or b"{}")
            if self.path == "/prompt":
                pid = "p" + os.urandom(6).hex()
                bad = [nid for nid, nd in body["prompt"].items() if nd["class_type"] not in OBJECT_INFO]
                if bad:
                    return self._send(400, {"error": {"type": "invalid_prompt", "message": "Cannot execute because node is missing"},
                                            "node_errors": {bad[0]: {"errors": [{"message": "missing"}], "class_type": "?"}}})
                with fk.lock:
                    fk.last_prompt = body["prompt"]
                threading.Thread(target=execute, args=(fk, pid, body), daemon=True).start()
                return self._send(200, {"prompt_id": pid, "number": 1, "node_errors": {}})
            if self.path == "/interrupt":
                with fk.lock:
                    fk.interrupted.add(body.get("prompt_id"))
                return self._send(200, {})
            if self.path == "/queue":
                with fk.lock:
                    fk.deleted.extend(body.get("delete") or [])
                return self._send(200, {})
            return self._send(404, b"nope", "text/plain")
    return H


def ws_send(fk, cid, msg):
    data = json.dumps(msg).encode()
    head = bytes([0x81]) + (bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack(">H", len(data)))
    with fk.lock:
        sock = fk.ws.get(cid)
    if sock:
        try:
            sock.sendall(head + data)
        except OSError:
            pass


def execute(fk, pid, body):
    """照工作流內容決定劇本：步數99＝慢（給取消用）、提示詞含BOOM＝節點出錯。"""
    cid, prompt = body.get("client_id"), body["prompt"]
    say = lambda t, d: ws_send(fk, cid, {"type": t, "data": {**d, "prompt_id": pid}})  # noqa: E731
    time.sleep(0.2)
    say("status", {"status": {"exec_info": {"queue_remaining": 1}}})
    say("execution_start", {})
    texts = " ".join(str(n["inputs"].get("text", "")) for n in prompt.values())
    steps = next((n["inputs"].get("steps") for n in prompt.values() if n["class_type"] == "KSampler"), 3)
    for nid in prompt:
        say("executing", {"node": nid})
        if prompt[nid]["class_type"] == "KSampler":
            if "BOOM" in texts:
                say("execution_error", {"node_id": nid, "node_type": "KSampler", "exception_message": "CUDA out of memory"})
                with fk.lock:
                    fk.history[pid] = {"status": {"status_str": "error", "completed": False, "messages": []}, "outputs": {}}
                return
            for i in range(steps if steps != 99 else 400):
                with fk.lock:
                    stop = pid in fk.interrupted
                if stop:
                    say("execution_interrupted", {"node_id": nid})
                    with fk.lock:
                        fk.history[pid] = {"status": {"status_str": "error", "completed": False, "messages": []}, "outputs": {}}
                    return
                say("progress", {"value": i + 1, "max": steps, "node": nid})
                time.sleep(0.05 if steps == 99 else 0.01)
    files = [{"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"},
             {"filename": "../../etc/passwd", "subfolder": "", "type": "output"},
             {"filename": "x.png", "subfolder": "../..", "type": "output"},
             {"filename": "prev.png", "subfolder": "", "type": "temp"}]
    with fk.lock:
        fk.history[pid] = {"status": {"status_str": "success", "completed": True, "messages": []},
                           "outputs": {"9": {"images": files}}}
    say("executing", {"node": None})


def serve(fk, ctx=None):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fk))
    srv.daemon_threads = True
    srv.handle_error = lambda *a: None          # 驗證不過的那次握手對方會直接斷線，不印堆疊
    if ctx:
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


# ---------------------------------------------------------------- 工作流樣本

def ui_workflow():
    """ComfyUI存的一般格式：Reroute、PrimitiveNode、略過的節點、靜音的節點、註解都放一個。"""
    nodes = [
        {"id": 4, "type": "CheckpointLoaderSimple", "mode": 0, "inputs": [], "outputs": [{"type": "MODEL"}, {"type": "CLIP"}, {"type": "VAE"}],
         "widgets_values": ["sd15.safetensors"]},
        {"id": 6, "type": "CLIPTextEncode", "mode": 0, "title": "正向", "inputs": [{"name": "clip", "link": 3}], "widgets_values": ["a red fox"]},
        {"id": 7, "type": "CLIPTextEncode", "mode": 0, "inputs": [{"name": "clip", "link": 5}], "widgets_values": ["blurry"]},
        {"id": 5, "type": "EmptyLatentImage", "mode": 0, "inputs": [{"name": "width", "link": 20, "widget": {"name": "width"}}],
         "widgets_values": [512, 512, 1]},
        {"id": 3, "type": "KSampler", "mode": 0,
         "inputs": [{"name": "model", "link": 1}, {"name": "positive", "link": 4}, {"name": "negative", "link": 6}, {"name": "latent_image", "link": 2}],
         "widgets_values": [156680208700286, "randomize", 20, 8, "euler", "normal", 1]},
        {"id": 8, "type": "VAEDecode", "mode": 0, "inputs": [{"name": "samples", "link": 7}, {"name": "vae", "link": 21}]},
        {"id": 12, "type": "ImageScale", "mode": 4, "inputs": [{"name": "image", "type": "IMAGE", "link": 9}],
         "outputs": [{"type": "IMAGE"}], "widgets_values": [1024, 1024]},
        {"id": 9, "type": "SaveImage", "mode": 0, "inputs": [{"name": "images", "link": 10}], "widgets_values": ["ComfyUI"]},
        {"id": 13, "type": "SaveImage", "mode": 2, "inputs": [{"name": "images", "link": 11}], "widgets_values": ["muted"]},
        {"id": 10, "type": "Reroute", "mode": 0, "inputs": [{"name": "", "link": 8}], "outputs": [{"type": "VAE"}]},
        {"id": 11, "type": "PrimitiveNode", "mode": 0, "outputs": [{"type": "INT"}], "widgets_values": [768, "fixed"]},
        {"id": 14, "type": "Note", "mode": 0, "widgets_values": ["說明"]},
    ]
    links = [[1, 4, 0, 3, 0, "MODEL"], [2, 5, 0, 3, 3, "LATENT"], [3, 4, 1, 6, 0, "CLIP"], [4, 6, 0, 3, 1, "CONDITIONING"],
             [5, 4, 1, 7, 0, "CLIP"], [6, 7, 0, 3, 2, "CONDITIONING"], [7, 3, 0, 8, 0, "LATENT"], [8, 4, 2, 10, 0, "VAE"],
             [21, 10, 0, 8, 1, "VAE"], [9, 8, 0, 12, 0, "IMAGE"], [10, 12, 0, 9, 0, "IMAGE"], [11, 8, 0, 13, 0, "IMAGE"],
             [20, 11, 0, 5, 0, "INT"]]
    return {"last_node_id": 14, "nodes": nodes, "links": links, "version": 0.4}


def api_workflow(text="a red fox", steps=4, ckpt="sd15.safetensors", with_image=False, save=True):
    w = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
         "6": {"class_type": "CLIPTextEncode", "inputs": {"text": text, "clip": ["4", 1]}},
         "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
         "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
         "3": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": 1, "steps": steps, "cfg": 7.0, "sampler_name": "euler",
                                                    "scheduler": "normal", "positive": ["6", 0], "negative": ["7", 0],
                                                    "latent_image": ["5", 0], "denoise": 1.0}},
         "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}}}
    if save:
        w["9"] = {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "ComfyUI"}}
    if with_image:
        w["20"] = {"class_type": "LoadImage", "inputs": {"image": "missing-example.png"}}
    return w


# ---------------------------------------------------------------- 主控台

class Client:
    def __init__(self, base):
        self.base, self.cookie = base, ""

    def req(self, method, path, body=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if self.cookie:
            r.add_header("Cookie", "xconsole=" + self.cookie)
        try:
            resp = urllib.request.urlopen(r, timeout=60)
        except urllib.error.HTTPError as e:
            resp = e
        body = resp.read()
        sc = resp.headers.get("Set-Cookie") or ""
        if sc.startswith("xconsole="):
            self.cookie = sc.split(";")[0].split("=", 1)[1]
        if raw:
            return resp.status, body, resp.headers.get("Content-Type")
        try:
            return resp.status, json.loads(body or b"{}")
        except ValueError:
            return resp.status, {}


def unit():
    check("網址：http本機可以", comfy.check_url("http://127.0.0.1:8188/")[0] == "http://127.0.0.1:8188")
    check("  反向：http連公開位址不收（要https）", comfy.check_url("http://8.8.8.8:8188")[1] is not None)
    check("  反向：帶帳密、查詢字串、非http都不收", all(comfy.check_url(u)[1] for u in
          ("http://a:b@127.0.0.1:8188", "http://127.0.0.1:8188/?k=1", "ftp://127.0.0.1", "file:///etc/passwd", "http://127.0.0.1:99999")))
    check("  https可帶子路徑（反向代理）", comfy.check_url("https://comfy.example.com/comfy")[0] == "https://comfy.example.com/comfy")

    api, missing = comfy.ui_to_api(ui_workflow(), OBJECT_INFO)
    check("一般格式轉API：缺的節點沒有", missing == [])
    check("  種子後面的「產生後」選單不吃掉步數", api["3"]["inputs"]["seed"] == 156680208700286 and api["3"]["inputs"]["steps"] == 20
          and api["3"]["inputs"]["sampler_name"] == "euler" and api["3"]["inputs"]["denoise"] == 1)
    check("  Reroute攤平：VAEDecode直接接到模型的VAE", api["8"]["inputs"]["vae"] == ["4", 2])
    check("  PrimitiveNode的值直接寫進寬度", api["5"]["inputs"]["width"] == 768 and api["5"]["inputs"]["height"] == 512)
    check("  略過的節點：下游接回它的輸入", api["9"]["inputs"]["images"] == ["8", 0] and "12" not in api)
    check("  靜音的節點與註解不送出", "13" not in api and "14" not in api and "10" not in api and "11" not in api)
    check("  標題留在_meta", api["6"]["_meta"]["title"] == "正向")
    ui_bad = ui_workflow()
    ui_bad["nodes"].append({"id": 30, "type": "WanVideoSampler", "mode": 0, "widgets_values": []})
    check("  反向：缺的節點列出名稱", comfy.ui_to_api(ui_bad, OBJECT_INFO)[1] == ["WanVideoSampler"])
    try:
        comfy.load_workflow(json.dumps({"nodes": [], "extra": {"groupNodes": {"g": {}}}}).encode())
        grp = False
    except ValueError as exc:
        grp = "匯出（API）" in str(exc)
    check("  舊式群組節點：請改用匯出（API）", grp)
    official()
    subgraph()
    fmt, d = comfy.load_workflow(json.dumps({"prompt": api_workflow()}).encode())
    check("匯出（API）格式直接收（含外面包一層prompt）", fmt == "api" and d["3"]["class_type"] == "KSampler")
    for bad in (b"not json", b"[]", json.dumps({"a": 1}).encode(), json.dumps({"x/../y": {"class_type": "K", "inputs": {}}}).encode()):
        try:
            comfy.load_workflow(bad)
            refused = False
        except ValueError:
            refused = True
        check(f"  反向：不是工作流的檔不收（{bad[:20]!r}）", refused)

    a = comfy.analyze(api_workflow(), OBJECT_INFO, "/srv/ComfyUI/models")
    labels = [f["label"] for f in a["fields"]]
    check("欄位：提示詞在最前、不要出現的內容第二", labels[:2] == ["提示詞", "不要出現的內容"], labels)
    check("  寬、高、張數、步數、引導強度、重繪強度、種子都找到", {"寬", "高", "張數", "步數", "引導強度", "重繪強度", "種子"} <= set(labels), labels)
    check("  沒有缺東西、輸出是圖片", not a["missing_models"] and not a["bad_values"] and a["outputs"] == ["9"] and a["kind"] == "image")
    flux = api_workflow()
    flux["10"] = {"class_type": "FluxGuidance", "inputs": {"conditioning": ["6", 0], "guidance": 3.5}}
    flux["3"]["inputs"]["positive"] = ["10", 0]
    check("  經過FluxGuidance轉接也認得是提示詞", comfy.analyze(flux, OBJECT_INFO)["fields"][0]["label"] == "提示詞")
    a = comfy.analyze(api_workflow(ckpt="wan2.2.safetensors"), OBJECT_INFO, "/srv/ComfyUI/models")
    mm = a["missing_models"]
    check("缺模型：講要放進哪個models子資料夾", mm and mm[0]["folder"] == "checkpoints" and mm[0]["where"] == "/srv/ComfyUI/models/checkpoints", mm)
    bad = api_workflow()
    bad["3"]["inputs"]["sampler_name"] = "nonexistent"
    check("  選項不存在列出可選的", comfy.analyze(bad, OBJECT_INFO)["bad_values"][0]["options"] == ["euler", "dpmpp_2m"])
    block, _ = comfy.problems_text(comfy.analyze(api_workflow(save=False), OBJECT_INFO))
    check("沒有輸出節點：擋下並說要加「儲存圖片」", block and "儲存圖片" in block[0])
    block, _ = comfy.problems_text({**comfy.analyze(api_workflow(), OBJECT_INFO), "missing_nodes": ["VHS_VideoCombine"]})
    check("缺節點：提示ComfyUI Manager「安裝缺失節點」後重啟", block and "安裝缺失節點" in block[0] and "重啟" in block[0])
    vid = api_workflow()
    vid["9"] = {"class_type": "SaveVideo", "inputs": {"video": ["8", 0], "filename_prefix": "v"}}
    check("輸出是儲存影片：分類自動判成影片", comfy.analyze(vid, OBJECT_INFO)["kind"] == "video")

    fields = comfy.analyze(api_workflow(with_image=True), OBJECT_INFO)["fields"]
    img = next(f for f in fields if f["kind"] == "image")
    check("輸入圖片欄位：範例圖不在ComfyUI裡＝一定要上傳", img["required"])
    check("  反向：沒選圖就擋", comfy.apply_values(api_workflow(with_image=True), fields, {}, {})[1] is not None)
    out, err = comfy.apply_values(api_workflow(with_image=True), fields, {"3.seed": -1, "5.width": 1024, "6.text": "雪地裡的狐狸"}, {img["id"]: "up.png"})
    check("  填的值放回工作流，種子-1換成隨機", not err and out["20"]["inputs"]["image"] == "up.png" and out["5"]["inputs"]["width"] == 1024
          and out["6"]["inputs"]["text"] == "雪地裡的狐狸" and out["3"]["inputs"]["seed"] >= 0, err)
    check("  反向：超出範圍、不是數字、控制字元都擋", all(comfy.apply_values(api_workflow(), fields, v, {img["id"]: "a"})[1] for v in
          ({"5.width": 99999}, {"3.steps": "abc"}, {"3.cfg": "nan"}, {"6.text": "a\x00b"})))

    check("看檔頭：PNG、JPEG、MP4、WebM、WebP認得", comfy.sniff(PNG)[0] == "image/png" and comfy.sniff(b"\xff\xd8\xff\xe0")[0] == "image/jpeg"
          and comfy.sniff(b"\x00\x00\x00\x18ftypmp42")[0] == "video/mp4" and comfy.sniff(b"\x1a\x45\xdf\xa3....")[0] == "video/webm"
          and comfy.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ")[0] == "image/webp")
    check("  反向：HTML、SVG、文字不收", comfy.sniff(b"<html>") is None and comfy.sniff(b"<svg xmlns") is None and comfy.sniff(b"hello") is None)
    files = comfy.output_files({"outputs": {"9": {"images": [{"filename": "../../etc/passwd"}, {"filename": "a.png", "subfolder": "../.."},
                                                             {"filename": "ok.png", "subfolder": "sub", "type": "output"},
                                                             {"filename": "p.png", "type": "temp"}, {"filename": "x", "type": "evil"}]}}})
    check("產出檔名：路徑穿越、怪類型擋掉；有output就不拿temp", files == [{"filename": "ok.png", "subfolder": "sub", "type": "output"}], files)
    check("  只有預覽（temp）時拿預覽", comfy.output_files({"outputs": {"1": {"images": [{"filename": "p.png", "type": "temp"}]}}})[0]["type"] == "temp")
    msg = comfy.describe_error({"error": {"message": "Prompt outputs failed validation"},
                                "node_errors": {"3": {"class_type": "KSampler", "errors": [{"message": "Value not in list", "details": "ckpt_name"}]}}})
    check("錯誤訊息指出哪個節點", "節點 #3（KSampler）" in msg and "Value not in list" in msg, msg)


def official():
    """ComfyUI官方範本：結果要跟ComfyUI前端「匯出（API）」（graphToPrompt）一模一樣。範本與前端的輸出存在 testdata。"""
    fx = json.load(open(os.path.join(HERE, "testdata", "comfy_templates.json"), encoding="utf-8"))
    info = fx["object_info"]
    strip = lambda a: {k: {"class_type": v["class_type"], "inputs": v["inputs"]} for k, v in a.items()}  # noqa: E731
    for c in fx["cases"]:
        api, missing = comfy.ui_to_api(c["workflow"], info)
        diff = sorted(k for k in set(api) | set(c["expected"]) if strip(api).get(k) != c["expected"].get(k))
        check(f"官方範本與ComfyUI前端匯出一致：{c['name']}", not missing and not diff, diff[:5])
    exp = json.dumps([c["expected"] for c in fx["cases"]])
    check("  範本涵蓋子圖、動態選單、動態群組、隱藏的舊欄位", re.search(r'"\d+:\d+"', exp) and all(x in exp for x in ('"resize_type.', '"loras.0.', '"format.codec"', '"codec"')))
    c = copy.deepcopy(next(c for c in fx["cases"] if c["name"] == "image_minimax_h3_image_character_swap"))
    node = next(n for n in c["workflow"]["nodes"] if n["type"] == "ResizeImageMaskNode")
    node["widgets_values"] = node["widgets_values"][1:]
    try:
        api, _ = comfy.ui_to_api(c["workflow"], info)
        caught = strip(api) != c["expected"]
    except comfy.Mismatch:
        caught = True
    check("  反向驗尺：少一格設定值就對不上（不會悄悄當成一樣）", caught)
    node["widgets_values"] = ["scale to multiple", 32, "area", "多出來的", 7]
    try:
        comfy.ui_to_api(c["workflow"], info)
        refused = False
    except comfy.Mismatch as exc:
        refused = "ResizeImageMaskNode" in str(exc)
    check("  設定值比定義多：不照順序硬對，指出哪個節點", refused)


def subgraph():
    """自己組的子圖：實例上提升的值蓋掉內部的值、外面接進來的線穿進子圖、子圖輸出接回外面、巢狀子圖。"""
    inner = {"id": "sub-inner", "inputs": [], "outputs": [{"name": "IMAGE", "type": "IMAGE", "linkIds": [3]}],
             "nodes": [{"id": 1, "type": "EmptyLatentImage", "mode": 0, "widgets_values": [64, 64, 1]},
                       {"id": 2, "type": "VAEDecode", "mode": 0, "inputs": [{"name": "samples", "link": 1}, {"name": "vae", "link": 2}]}],
             "links": [{"id": 1, "origin_id": 1, "origin_slot": 0, "target_id": 2, "target_slot": 0},
                       {"id": 2, "origin_id": -10, "origin_slot": 0, "target_id": 2, "target_slot": 1},
                       {"id": 3, "origin_id": 2, "origin_slot": 0, "target_id": -20, "target_slot": 0}]}
    inner["inputs"] = [{"name": "vae", "type": "VAE", "linkIds": [2]}]
    outer_sub = {"id": "sub-outer", "inputs": [{"name": "vae", "type": "VAE", "linkIds": [11]}, {"name": "text", "type": "STRING", "linkIds": [12]},
                                                {"name": "ckpt", "type": "COMBO", "linkIds": []}],
                 "outputs": [{"name": "IMAGE", "type": "IMAGE", "linkIds": [13]}],
                 "nodes": [{"id": 5, "type": "sub-inner", "mode": 0, "inputs": [{"name": "vae", "link": 11}], "outputs": [{"type": "IMAGE"}]},
                           {"id": 6, "type": "CLIPTextEncode", "mode": 0, "inputs": [{"name": "clip", "link": None}, {"name": "text", "link": 12, "widget": {"name": "text"}}],
                            "widgets_values": ["內部原本的字"]}],
                 "links": [{"id": 11, "origin_id": -10, "origin_slot": 0, "target_id": 5, "target_slot": 0},
                           {"id": 12, "origin_id": -10, "origin_slot": 1, "target_id": 6, "target_slot": 1},
                           {"id": 13, "origin_id": 5, "origin_slot": 0, "target_id": -20, "target_slot": 0}]}
    wf = {"nodes": [{"id": 4, "type": "CheckpointLoaderSimple", "mode": 0, "widgets_values": ["sd15.safetensors"]},
                    {"id": 7, "type": "sub-outer", "mode": 0, "inputs": [{"name": "vae", "link": 21}, {"name": "text", "link": None, "widget": {"name": "text"}}],
                     "outputs": [{"type": "IMAGE"}], "widgets_values": ["實例上改過的字", "sd15.safetensors"]},
                    {"id": 9, "type": "SaveImage", "mode": 0, "inputs": [{"name": "images", "link": 22}], "widgets_values": ["x"]}],
          "links": [[21, 4, 2, 7, 0, "VAE"], [22, 7, 0, 9, 0, "IMAGE"]],
          "definitions": {"subgraphs": [inner, outer_sub]}}
    api, missing = comfy.ui_to_api(wf, OBJECT_INFO)
    check("子圖：內部節點編號是「實例:內部」，巢狀一路接下去", {"4", "9", "7:6", "7:5:1", "7:5:2"} == set(api), sorted(api))
    check("  實例上提升的值蓋掉內部原本的值", api["7:6"]["inputs"]["text"] == "實例上改過的字")
    check("  外面接進來的線穿過兩層子圖", api["7:5:2"]["inputs"]["vae"] == ["4", 2])
    check("  子圖輸出接回外面", api["9"]["inputs"]["images"] == ["7:5:2", 0] and api["7:5:2"]["inputs"]["samples"] == ["7:5:1", 0])
    wf["nodes"][1]["inputs"][1]["link"] = 23
    wf["nodes"].append({"id": 10, "type": "PrimitiveNode", "mode": 0, "outputs": [{"type": "STRING"}], "widgets_values": ["外面的PrimitiveNode"]})
    wf["links"].append([23, 10, 0, 7, 1, "STRING"])
    check("  子圖輸入外面有接線：用接線那邊的值", comfy.ui_to_api(wf, OBJECT_INFO)[0]["7:6"]["inputs"]["text"] == "外面的PrimitiveNode")
    wf["definitions"]["subgraphs"][0]["nodes"].append({"id": 3, "type": "sub-inner", "mode": 0})
    check("  子圖自己包自己不會無限展開", len(comfy.ui_to_api(wf, OBJECT_INFO)[0]) < 200)


def agent_scan():
    root = os.path.join(os.path.realpath(TMP), "ComfyUI")
    for sub, fn in (("checkpoints", "sd15.safetensors"), ("diffusion_models/wan", "wan.gguf"), ("vae", "ae.sft"), ("loras", "notes.txt")):
        os.makedirs(os.path.join(root, "models", sub), exist_ok=True)
        with open(os.path.join(root, "models", sub, fn), "wb") as fh:
            fh.write(b"\x00" * 1024)
    os.symlink("/etc", os.path.join(root, "models", "checkpoints", "etc-link"))
    code, out = A.comfy_scan(root)
    check("ComfyUI資料夾：根目錄收", code == 200 and out["models"] == os.path.join(root, "models"), out)
    check("  列出模型檔（含子資料夾），不列非模型檔、不跟符號連結進去", [r["name"] for r in out["folders"]["checkpoints"]] == ["sd15.safetensors"]
          and [r["name"] for r in out["folders"]["diffusion_models"]] == ["wan/wan.gguf"] and out["folders"]["loras"] == [], out["folders"])
    check("  models資料夾本身也收", A.comfy_scan(os.path.join(root, "models"))[1].get("root") == root)
    check("  反向：不是ComfyUI資料夾講清楚", A.comfy_scan(TMP)[0] == 400 and "不像ComfyUI" in A.comfy_scan(TMP)[1]["error"])
    check("  反向：家目錄外面不准", A.comfy_scan("/etc")[0] == 400)
    code, out = A.comfy_browse(TMP)
    check("  資料夾瀏覽器標出哪個能選", code == 200 and any(d["name"] == "ComfyUI" and d["ok"] for d in out["dirs"]), out)


def e2e():
    fk = Fake()
    port = serve(fk)
    url = f"http://127.0.0.1:{port}"
    x.set_credentials("admin", ADMIN_PW)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    adm = Client(base)
    adm.login = adm.req("POST", "/api/login", {"username": "admin", "password": ADMIN_PW})
    check("管理員登入", adm.login[0] == 200, adm.login)

    old_ports = comfy.PORTS
    comfy.PORTS = (port, 1)
    st, out = adm.req("GET", "/api/comfy/detect")
    comfy.PORTS = old_ports
    check("自動尋找：找得到本機的ComfyUI與版本", st == 200 and out["found"] == [{"url": url, "version": "0.3.99"}], out)

    for mode, want in (("html", "不是JSON"), ("404", "不是ComfyUI"), ("401", "金鑰"), ("302", "不跟轉址"), ("json", "不像ComfyUI")):
        fk.mode["stats"] = mode
        st, out = adm.req("POST", "/api/comfy/settings", {"url": url})
        check(f"連線測試講人話：{mode} → {want}", st == 200 and not out["online"] and want in out["error"], out.get("error"))
    fk.mode["stats"] = "ok"
    st, out = adm.req("POST", "/api/comfy/settings", {"url": "http://8.8.8.8:8188"})
    check("  反向：http公開位址存不進去", st == 400)
    st, out = adm.req("POST", "/api/comfy/settings", {"url": url + "/", "key": KEY})
    check("儲存並測試：已連線、顯示版本", st == 200 and out["online"] and out["version"] == "0.3.99" and out["key_set"], out)
    raw = open(x.FLEET, encoding="utf-8").read()
    check("  金鑰加密落地（fleet.json沒有明文、是enc:v1:）", KEY not in raw and json.loads(raw)["comfy"]["key"].startswith("enc:v1:"))
    st, out = adm.req("GET", "/api/comfy")
    check("  GET不回金鑰", st == 200 and KEY not in json.dumps(out) and out["key_set"])

    st, out = adm.req("POST", "/api/comfy/workflows/import", {"name": "文生圖", "kind": "auto", "workflow": ui_workflow()})
    check("匯入一般格式（經/object_info轉API）", st == 200 and out["workflow"]["kind"] == "image" and out["workflow"]["source"] == "ui", out)
    wf_ui = out["workflow"]["id"]
    st, out = adm.req("POST", "/api/comfy/workflows/import", {"name": "API版", "kind": "auto", "workflow": json.dumps(api_workflow(with_image=True))})
    check("匯入匯出（API）格式（字串也收）", st == 200 and any(f["kind"] == "image" for f in out["workflow"]["fields"]), out)
    wf_api = out["workflow"]["id"]
    st, out = adm.req("POST", "/api/comfy/workflows/import", {"name": "缺節點", "workflow": {**api_workflow(), "30": {"class_type": "WanVideoSampler", "inputs": {}}}})
    check("  缺節點擋下並列名稱", st == 400 and out["missing_nodes"] == ["WanVideoSampler"] and "ComfyUI Manager" in out["error"], out)
    st, out = adm.req("POST", "/api/comfy/workflows/import", {"name": "沒輸出", "workflow": api_workflow(save=False)})
    check("  沒有輸出節點擋下", st == 400 and "儲存圖片" in out["error"], out)
    st, out = adm.req("POST", "/api/comfy/workflows/import", {"name": "缺模型", "workflow": api_workflow(ckpt="wan.safetensors")})
    check("  缺模型：匯入成功但列警告（放哪個資料夾）", st == 200 and out["warnings"] and "models/checkpoints" in out["warnings"][0], out)
    wf_miss = out["workflow"]["id"]
    st, out = adm.req("POST", f"/api/comfy/workflows/{wf_miss}/update", {"name": "改名了", "kind": "video"})
    _, lst = adm.req("GET", "/api/comfy")
    check("工作流改名、改分類", st == 200 and any(w["name"] == "改名了" and w["kind"] == "video" for w in lst["workflows"]))
    st, out = adm.req("POST", "/api/comfy/generate", {"workflow": wf_miss})
    check("  缺模型的工作流產生時擋下、講要放哪", st == 400 and "models/checkpoints" in out["error"], out)

    # ---- 一般使用者產生
    st, o = adm.req("POST", "/api/users/create", {"username": "u1@cloudinfo.com.tw", "display": "u1", "role": "user", "scope": []})
    u1, pw = Client(base), "u1-pass-" + os.urandom(4).hex()
    u1.req("POST", "/api/login", {"username": "u1@cloudinfo.com.tw", "password": o["password"]})
    u1.req("POST", "/api/account/password", {"old": o["password"], "new": pw})
    u1.req("POST", "/api/login", {"username": "u1@cloudinfo.com.tw", "password": pw})
    st, out = u1.req("GET", "/api/comfy")
    check("使用者看得到工作流與連線狀態，看不到網址", st == 200 and len(out["workflows"]) == 3 and "url" not in out and out["online"], out)
    miss = next(w for w in out["workflows"] if w["id"] == wf_miss)
    check("  缺模型的細節（伺服器資料夾路徑）只給管理員，使用者看到要找管理員", miss["warnings"] and "models" not in json.dumps(miss["warnings"])
          and "管理員" in miss["warnings"][0])
    st, out = u1.req("POST", "/api/comfy/generate", {"workflow": wf_miss})
    check("  使用者用缺模型的工作流：擋下、不洩漏路徑", st == 400 and "models" not in json.dumps(out), out)
    for p, b in (("/api/comfy/settings", {"url": url}), ("/api/comfy/workflows/import", {}), ("/api/comfy/folder", {}),
                 (f"/api/comfy/workflows/{wf_ui}/delete", {})):
        st, out = u1.req("POST", p, b)
        check(f"  反向：使用者不能 {p}", st == 403)
    check("  反向：使用者不能自動尋找", u1.req("GET", "/api/comfy/detect")[0] == 403)
    st, out = u1.req("POST", "/api/comfy/generate", {"workflow": wf_api, "values": {"6.text": "a cat"}})
    check("  反向：必填的輸入圖片沒給就擋", st == 400 and "要先選一張圖" in out["error"], out)
    st, out = u1.req("POST", "/api/comfy/generate", {"workflow": wf_api, "values": {"6.text": "a cat"},
                                                     "images": {"20.image": "data:text/html;base64," + base64.b64encode(b"<html>").decode()}})
    check("  反向：輸入圖片看檔頭，HTML不收", st == 400 and "PNG" in out["error"], out)
    fk.reqs.clear()
    st, out = u1.req("POST", "/api/comfy/generate", {"workflow": wf_api, "values": {"6.text": "a cat", "3.seed": 42, "5.width": 640},
                                                     "images": {"20.image": "data:image/png;base64," + base64.b64encode(PNG).decode()}})
    check("送出產生", st == 200 and re.fullmatch(r"[0-9a-f]{12}", out.get("id") or ""), out)
    jid = out.get("id")
    j = wait(lambda: (lambda r: r[1] if r[1].get("state") != "running" else None)(u1.req("GET", f"/api/comfy/jobs/{jid}")))
    check("  跑完、取回一個產出（路徑穿越與預覽都沒拿）", j and j["state"] == "done" and len(j["outputs"]) == 1, j)
    check("  送進ComfyUI的值是填的值，輸入圖片換成上傳的檔名", fk.last_prompt["6"]["inputs"]["text"] == "a cat" and fk.last_prompt["3"]["inputs"]["seed"] == 42
          and fk.last_prompt["5"]["inputs"]["width"] == 640 and fk.last_prompt["20"]["inputs"]["image"] in fk.uploads)
    st, body, ctype = u1.req("GET", f"/api/comfy/jobs/{jid}/output/0", raw=True)
    check("  下載產出：內容與型別對", st == 200 and body == PNG and ctype == "image/png")
    views = [r["view"] for r in fk.reqs if "view" in r]
    check("  取回只要了合格的那一個檔", len(views) == 1 and views[0]["filename"] == ["ComfyUI_00001_.png"], views)
    auths = {r["auth"] for r in fk.reqs}
    check("金鑰只走Authorization標頭", auths == {f"Bearer {KEY}"}, auths)
    leak = lambda reqs: [r["path"] for r in reqs if KEY in urllib.parse.unquote(r["path"])]  # noqa: E731
    check("  反向驗尺：網址帶金鑰的話抓得到", leak([{"path": "/view?token=" + KEY}]))
    check("  沒有任何請求的網址帶金鑰", not leak(fk.reqs))
    raw = open(os.path.join(x.STATE_DIR, "xconsole.db"), "rb").read() if os.path.exists(os.path.join(x.STATE_DIR, "xconsole.db")) else b""
    check("  產出紀錄加密落地（資料庫找不到提示詞明文）", raw and b"a cat" not in raw)

    adm2 = Client(base)
    st, o = adm.req("POST", "/api/users/create", {"username": "u2@cloudinfo.com.tw", "display": "u2", "role": "user", "scope": []})
    u2pw = "u2-pass-" + os.urandom(4).hex()
    adm2.req("POST", "/api/login", {"username": "u2@cloudinfo.com.tw", "password": o["password"]})
    adm2.req("POST", "/api/account/password", {"old": o["password"], "new": u2pw})
    adm2.req("POST", "/api/login", {"username": "u2@cloudinfo.com.tw", "password": u2pw})
    check("別人的產出看不到", adm2.req("GET", f"/api/comfy/jobs/{jid}")[0] == 404 and adm2.req("GET", f"/api/comfy/jobs/{jid}/output/0", raw=True)[0] == 404
          and adm2.req("GET", "/api/comfy/jobs")[1]["jobs"] == [] and adm2.req("POST", f"/api/comfy/jobs/{jid}/delete", {})[0] == 404)
    check("  管理員看得到全部並標出是誰", any(j["id"] == jid and j["by"] == "u1@cloudinfo.com.tw" for j in adm.req("GET", "/api/comfy/jobs")[1]["jobs"]))

    # 取消：步數99是慢劇本
    slow = api_workflow(steps=99)
    st, out = adm.req("POST", "/api/comfy/workflows/import", {"name": "慢", "workflow": slow})
    st, out = u1.req("POST", "/api/comfy/generate", {"workflow": out["workflow"]["id"]})
    sid = out["id"]
    got = wait(lambda: u1.req("GET", f"/api/comfy/jobs/{sid}")[1].get("step", 0) > 2)
    j = u1.req("GET", f"/api/comfy/jobs/{sid}")[1]
    check("WebSocket追進度：步數與正在跑的節點", got and j["steps"] == 99 and "節點 #3" in j["phase"], j)
    check("  還在跑的不能刪", u1.req("POST", f"/api/comfy/jobs/{sid}/delete", {})[0] == 409)
    check("  取消", u1.req("POST", f"/api/comfy/jobs/{sid}/cancel", {})[0] == 200)
    j = wait(lambda: (lambda r: r if r.get("state") != "running" else None)(u1.req("GET", f"/api/comfy/jobs/{sid}")[1]))
    check("  已取消，ComfyUI收到/interrupt", j and j["state"] == "cancelled" and fk.interrupted, j)

    st, out = adm.req("POST", "/api/comfy/workflows/import", {"name": "會壞", "workflow": api_workflow(text="BOOM")})
    st, out = u1.req("POST", "/api/comfy/generate", {"workflow": out["workflow"]["id"]})
    j = wait(lambda: (lambda r: r if r.get("state") != "running" else None)(u1.req("GET", f"/api/comfy/jobs/{out['id']}")[1]))
    check("節點出錯：指出是哪個節點與原因", j and j["state"] == "failed" and "節點 #3（KSampler）" in j["error"] and "out of memory" in j["error"], j)

    fk.mode["output"] = "html"
    st, out = u1.req("POST", "/api/comfy/generate", {"workflow": wf_ui})
    j = wait(lambda: (lambda r: r if r.get("state") != "running" else None)(u1.req("GET", f"/api/comfy/jobs/{out['id']}")[1]))
    check("反向：產出是HTML不收，講清楚", j and j["state"] == "failed" and "沒有可以取回" in j["error"], j)
    fk.mode.update(output="png", ws=False)
    fk.reqs.clear()
    st, out = u1.req("POST", "/api/comfy/generate", {"workflow": wf_ui})
    j = wait(lambda: (lambda r: r if r.get("state") != "running" else None)(u1.req("GET", f"/api/comfy/jobs/{out['id']}")[1]), 30)
    check("WebSocket不通：改輪詢/history照樣完成", j and j["state"] == "done" and any(r["path"].startswith("/ws") for r in fk.reqs), j)
    fk.mode["ws"] = True

    # 換網址不沿用舊金鑰
    st, out = adm.req("POST", "/api/comfy/settings", {"url": f"http://localhost:{port}"})
    check("換網址：舊金鑰不帶過去", st == 200 and not out["key_set"])

    # 主控台重啟：跑到一半的標成中斷
    x.STORE.comfy_job_put({"id": "a" * 12, "user": "u1@cloudinfo.com.tw", "wf": wf_ui, "kind": "image", "state": "running",
                           "created": time.time(), "body": x.chat_seal(json.dumps({"wf_name": "x", "state": "running"}).encode())})
    x.comfy_jobs_load()
    check("主控台重啟過：跑到一半的標成中斷並說明", x.comfy_job("a" * 12)["state"] == "interrupted" and "output" in x.comfy_job("a" * 12)["error"])

    check("刪掉自己的產出（連檔案）", u1.req("POST", f"/api/comfy/jobs/{jid}/delete", {})[0] == 200
          and not os.path.exists(os.path.join(x.COMFY_DIR, jid)))
    n_before = len(x.STORE.comfy_job_rows("u1@cloudinfo.com.tw"))
    st, out = adm.req("POST", "/api/users/u1@cloudinfo.com.tw/delete", {})
    check("刪帳號：他的產出一起刪", st == 200 and n_before and not x.STORE.comfy_job_rows("u1@cloudinfo.com.tw"), out)
    check("刪工作流", adm.req("POST", f"/api/comfy/workflows/{wf_ui}/delete", {})[0] == 200
          and all(w["id"] != wf_ui for w in adm.req("GET", "/api/comfy")[1]["workflows"]))

    # 資料夾：經代理掃（這裡把代理換成同一支程式的函式）
    real = x.call_node
    x.update_fleet(lambda c: c.__setitem__("nodes", [{"id": "n1", "name": "本機", "host": "127.0.0.1", "token": ""}]))
    x.call_node = lambda nid, path, method="GET", body=None, timeout=40: A.comfy_scan((body or {}).get("path"))
    try:
        st, out = adm.req("POST", "/api/comfy/folder", {"node": "n1", "path": os.path.join(os.path.realpath(TMP), "ComfyUI")})
        check("指定ComfyUI資料夾：存起來並列出模型", st == 200 and x.comfy_cfg()["folder"]["models"] == os.path.join(os.path.realpath(TMP), "ComfyUI", "models"), out)
        st, out = adm.req("POST", "/api/comfy/workflows/import", {"name": "缺模型2", "workflow": api_workflow(ckpt="none.safetensors")})
        check("  缺模型的提示寫出完整路徑", st == 200 and os.path.join(os.path.realpath(TMP), "ComfyUI", "models") + "/checkpoints" in out["warnings"][0], out)
        check("  重新掃描", adm.req("GET", "/api/comfy/models")[0] == 200)
        check("  反向：不是ComfyUI資料夾不存", adm.req("POST", "/api/comfy/folder", {"node": "n1", "path": TMP})[0] == 400)
        check("  清掉", adm.req("POST", "/api/comfy/folder", {"node": "n1", "path": ""})[0] == 200 and not x.comfy_cfg().get("folder"))
    finally:
        x.call_node = real


def https():
    fk = Fake()
    cert, key = tls.generate_self_signed(["127.0.0.1"], cn="comfy-test")
    cdir = tempfile.mkdtemp(dir=TMP)
    for n, d in (("c.pem", cert), ("k.pem", key)):
        with open(os.path.join(cdir, n), "wb") as fh:
            fh.write(d)
    port = serve(fk, tls.make_context(os.path.join(cdir, "c.pem"), os.path.join(cdir, "k.pem")))
    conn = comfy.Conn(f"https://127.0.0.1:{port}", KEY)
    try:
        comfy.system_info(conn)
        refused = ""
    except comfy.ComfyError as exc:
        refused = exc.msg
    check("HTTPS：自簽沒匯入CA就驗不過，提示匯入對方的CA", "匯入對方的CA" in refused, refused)
    leaf = cert.split(b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----\n"
    info = comfy.system_info(comfy.Conn(f"https://127.0.0.1:{port}", KEY, leaf.decode()))
    check("  匯入對方的憑證後通過（不是關掉驗證）", info["version"] == "0.3.99")
    ws = comfy.Conn(f"https://127.0.0.1:{port}", KEY, leaf.decode()).ws("abc")
    check("  WebSocket也走TLS並驗證", isinstance(ws, comfy.WebSocket))
    ws.close()
    check("  原始碼沒有關閉驗證的寫法", not re.search(r"CERT_NONE|check_hostname\s*=\s*False|_create_unverified_context",
                                                       open(os.path.join(HERE, "comfy.py"), encoding="utf-8").read()))


unit()
agent_scan()
e2e()
https()
print("全部通過" if OK else "有項目失敗")
sys.exit(0 if OK else 1)
