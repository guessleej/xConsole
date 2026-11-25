"""ComfyUI串接：連線與驗證、工作流轉成API格式並檢查缺什麼、送出後用WebSocket追進度、取回產出。

只連管理者在網頁上設定的網址（自動尋找只試本機與納管機器的固定埠）。對方回的任何東西
（節點定義、進度訊息、檔名、檔案內容）一律當不可信資料：檢查型別與長度，檔案看檔頭決定能不能給瀏覽器看。
"""
import base64
import copy
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlencode, urlparse

PORTS = (8188, 8000, 8189)            # 8188是ComfyUI預設；8000是ComfyUI桌面版
MAX_JSON = 64 << 20                   # 裝了很多自訂節點時/object_info會到十幾MB
MAX_WORKFLOW = 8 << 20
MAX_FILE = 1 << 30
MAX_JOB_BYTES = 2 << 30
MAX_OUTPUTS = 32
MAX_NODES = 3000
JOB_TIMEOUT = 6 * 3600
WS_FRAME_MAX = 64 << 20               # 預覽圖走二進位訊框，會到幾MB
STALL_LIMIT = 90                      # ComfyUI完全斷線這麼久就判定失敗
CANCEL_WAIT = 600                     # 取消只在節點之間生效，一個節點可能跑很久

URL_RX = re.compile(r"[\x00-\x20\x7f]")
NAME_RX = re.compile(r"[^\x00-\x1f\x7f<>]{1,60}")
FILE_RX = re.compile(r"[^/\\\x00-\x1f\x7f]{1,255}")
SUBFOLDER_RX = re.compile(r"[^\\\x00-\x1f\x7f]{0,255}")
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
SEED_CONTROL = {"fixed", "increment", "decrement", "randomize"}
VIRTUAL = {"Reroute", "PrimitiveNode", "Note", "MarkdownNote"}
DYNAMIC_COMBO = "COMFY_DYNAMICCOMBO_V3"
DYNAMIC_GROUP = "COMFY_DYNAMICGROUP_V3"
UPLOAD_FLAGS = ("image_upload", "video_upload", "audio_upload", "file_upload")
UPLOAD_VALUES = {"image", "video", "audio"}
FRONTEND_VALUES = {"IMAGECOMPARE": {"__value__": ["", ""]}}     # 只在ComfyUI畫面上有作用的欄位，前端送出時固定填這個值
IMAGE_LOADERS = {"LoadImage", "LoadImageMask"}
VIDEO_OUTPUTS = {"SaveVideo", "SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG", "VHS_VideoCombine", "CreateVideo"}

# 輸入名稱 → 放在ComfyUI的哪個models子資料夾
MODEL_FOLDERS = {"ckpt_name": "checkpoints", "unet_name": "diffusion_models", "vae_name": "vae",
                 "clip_name": "text_encoders", "clip_name1": "text_encoders", "clip_name2": "text_encoders",
                 "clip_name3": "text_encoders", "clip_name4": "text_encoders", "lora_name": "loras",
                 "control_net_name": "controlnet", "clip_vision": "clip_vision", "style_model_name": "style_models",
                 "upscale_model": "upscale_models", "gligen_name": "gligen", "audio_encoder_name": "audio_encoders"}
CLASS_FOLDERS = {"UpscaleModelLoader": "upscale_models", "CLIPVisionLoader": "clip_vision"}
MODEL_EXT = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf")

NUM_LABELS = {"width": "寬", "height": "高", "length": "長度（影格數）", "num_frames": "長度（影格數）", "frames": "長度（影格數）",
              "batch_size": "張數", "steps": "步數", "cfg": "引導強度", "denoise": "重繪強度",
              "frame_rate": "每秒格數", "fps": "每秒格數", "duration": "秒數"}
SEED_NAMES = {"seed", "noise_seed"}


class ComfyError(Exception):
    def __init__(self, msg, code=502):
        super().__init__(msg)
        self.msg, self.code = msg, code


def _private_ok(ip):
    a = ipaddress.ip_address(ip)
    return (a.is_private or a.is_loopback) and not a.is_link_local and not a.is_multicast and not a.is_unspecified


def check_url(url):
    """回 (正規化後的網址, 錯誤)。網址可帶路徑（反向代理掛在子路徑），不准帶帳密與查詢字串。"""
    url = str(url or "").strip().rstrip("/")
    if not url or len(url) > 300 or URL_RX.search(url):
        return None, "網址要填，例：http://127.0.0.1:8188"
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        return None, "網址只收 http(s)://主機:埠/路徑（不能帶帳密、查詢字串）"
    try:
        u.port
    except ValueError:
        return None, "埠號不對"
    if u.scheme == "http":
        try:
            ips = {i[4][0] for i in socket.getaddrinfo(u.hostname, None)}
        except OSError:
            return None, f"解析不到 {u.hostname}"
        if not ips or not all(_private_ok(i) for i in ips):
            return None, "http（不加密）只准連區網私有位址或本機；網際網路上的ComfyUI請用https"
    return url, None


def mask(text, key):
    text = str(text)
    return text.replace(key, "＊＊＊") if key else text


class Conn:
    """一台ComfyUI。每次請求開一條新連線；不跟轉址（免得把金鑰送到別處）；金鑰只走Authorization標頭。"""

    def __init__(self, url, key="", ca_pem="", timeout=30):
        self.url, self.key, self.ca_pem, self.timeout = url, key or "", ca_pem or "", timeout
        u = urlparse(url)
        self.scheme, self.host, self.netloc = u.scheme, u.hostname, u.netloc
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.base = (u.path or "").rstrip("/")

    def _ctx(self):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if self.ca_pem:
            ctx.load_verify_locations(cadata=self.ca_pem)        # 加上對方的CA，不是關掉驗證
        return ctx

    def _addr(self):
        """http時先解析、確認是私有位址才直接連那個IP（不讓DNS在檢查和連線之間換位址）。"""
        if self.scheme == "https":
            return self.host
        try:
            ips = sorted({i[4][0] for i in socket.getaddrinfo(self.host, self.port, proto=socket.IPPROTO_TCP)})
        except OSError:
            raise ComfyError(f"解析不到 {self.host}") from None
        if not ips or not all(_private_ok(i) for i in ips):
            raise ComfyError("http只准連區網私有位址或本機，這個網址現在解析到外部位址，已拒絕", 400)
        return ips[0]

    def headers(self, extra=None):
        h = {"Host": self.netloc, "User-Agent": "xconsole", "Accept": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        h.update(extra or {})
        return h

    def _fail(self, exc):
        if isinstance(exc, ssl.SSLError):
            return ComfyError(f"{self.host} 的憑證驗證不過（{exc.reason or exc}）。自建CA的ComfyUI請在連線設定匯入對方的CA憑證")
        if isinstance(exc, ConnectionRefusedError):
            return ComfyError(f"連不到 {self.netloc}：對方沒有在這個埠服務。確認ComfyUI有啟動；給別台連要加 --listen")
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return ComfyError(f"連 {self.netloc} 逾時：確認位址、防火牆，以及ComfyUI有沒有卡住")
        return ComfyError(f"連不到 {self.netloc}：{mask(exc, self.key)[:160]}")

    def request(self, method, path, body=None, ctype="application/json", limit=MAX_JSON, timeout=None):
        """回 (狀態碼, 回應本文)。本文超過 limit 丟 ComfyError。"""
        addr = self._addr()
        t = timeout or self.timeout
        conn = (http.client.HTTPSConnection(addr, self.port, timeout=t, context=self._ctx()) if self.scheme == "https"
                else http.client.HTTPConnection(addr, self.port, timeout=t))
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        try:
            conn.request(method, self.base + path, body=data, headers=self.headers({"Content-Type": ctype} if data is not None else None))
            resp = conn.getresponse()
            if 300 <= resp.status < 400:
                raise ComfyError(f"{self.netloc} 回 {resp.status} 轉址；為了不把金鑰送到別處，不跟轉址。請填ComfyUI實際的網址")
            raw = resp.read(limit + 1)
            if len(raw) > limit:
                raise ComfyError("ComfyUI回的內容太大")
            return resp.status, raw
        except ComfyError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise self._fail(exc) from None
        finally:
            conn.close()

    def json(self, method, path, body=None, limit=MAX_JSON, timeout=None):
        st, raw = self.request(method, path, body, limit=limit, timeout=timeout)
        if st in (401, 403):
            raise ComfyError(f"ComfyUI回 {st}：需要金鑰或金鑰不對（ComfyUI前面的反向代理或登入外掛擋下來）", 400)
        if st == 404:
            raise ComfyError(f"這個網址沒有 {path.split('?')[0]}：不是ComfyUI，或網址的路徑不對", 400)
        try:
            out = json.loads(raw or b"null")
        except ValueError:
            snippet = raw[:80].decode("utf-8", "replace").strip()
            raise ComfyError(f"對方回的不是JSON（HTTP {st}），這個網址可能不是ComfyUI，例如登入頁或別的網站"
                             + (f"：{mask(snippet, self.key)}" if snippet else "")) from None
        if st >= 400:
            raise ComfyError(f"ComfyUI回 {st}：{describe_error(out)}", 400 if st < 500 else 502)
        return out

    def ws(self, client_id, timeout=5):
        return WebSocket.open(self, f"{self.base}/ws?clientId={quote(client_id)}", timeout)


# ---------------------------------------------------------------- WebSocket（RFC 6455，用戶端只需要收）

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocket:
    def __init__(self, sock, rest=b""):
        self.sock, self.buf, self.frags = sock, rest, []

    @classmethod
    def open(cls, conn, path, timeout):
        addr = conn._addr()
        try:
            raw = socket.create_connection((addr, conn.port), timeout=conn.timeout)
            sock = conn._ctx().wrap_socket(raw, server_hostname=conn.host) if conn.scheme == "https" else raw
            key = base64.b64encode(os.urandom(16)).decode()
            h = conn.headers({"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": key,
                              "Sec-WebSocket-Version": "13"})
            h.pop("Accept", None)
            sock.sendall((f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk or len(data) > 16384:
                    raise ComfyError("ComfyUI沒有接受WebSocket連線")
                data += chunk
        except ComfyError:
            raise
        except OSError as exc:
            raise conn._fail(exc) from None
        head, rest = data.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        hdr = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        want = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        if " 101 " not in lines[0] + " " or hdr.get("sec-websocket-accept") != want:
            sock.close()
            raise ComfyError(f"ComfyUI沒有接受WebSocket連線（{lines[0][:60]}）")
        sock.settimeout(timeout)
        return cls(sock, rest)

    def _frame(self):
        b = self.buf
        if len(b) < 2:
            return None
        n, at = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, at = int.from_bytes(b[2:4], "big"), 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, at = int.from_bytes(b[2:10], "big"), 10
        if n > WS_FRAME_MAX:
            raise ComfyError("ComfyUI送來的WebSocket訊框太大")
        masked = b[1] & 0x80
        mkey = b[at:at + 4] if masked else b""
        at += 4 if masked else 0
        if len(b) < at + n:
            return None
        payload = b[at:at + n]
        if masked:
            payload = bytes(c ^ mkey[i % 4] for i, c in enumerate(payload))
        self.buf = b[at + n:]
        return b[0] & 0x80, b[0] & 0x0F, payload

    def _send(self, op, payload=b""):
        mk = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0xFE]) + n.to_bytes(2, "big"))
        self.sock.sendall(head + mk + bytes(c ^ mk[i % 4] for i, c in enumerate(payload)))

    def recv(self):
        """回一則文字訊息（dict）、"idle"（逾時沒東西）、None（對方關了）。二進位訊框（預覽圖）略過。"""
        while True:
            f = self._frame()
            if f is None:
                try:
                    chunk = self.sock.recv(1 << 16)
                except (socket.timeout, TimeoutError):
                    return "idle"
                except OSError:
                    return None
                if not chunk:
                    return None
                self.buf += chunk
                continue
            fin, op, payload = f
            if op == 0x9:
                self._send(0xA, payload[:125])
                continue
            if op == 0x8:
                return None
            if op in (0x1, 0x2, 0x0):
                self.frags.append((op, payload))
                if not fin:
                    continue
                first, data = self.frags[0][0], b"".join(p for _, p in self.frags)
                self.frags = []
                if first != 0x1:
                    continue
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    return msg

    def close(self):
        try:
            self._send(0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- 連線測試與自動尋找

def system_info(conn, timeout=None):
    """打/system_stats確認對方真的是ComfyUI。回 {version, python, devices}。"""
    d = conn.json("GET", "/system_stats", limit=1 << 20, timeout=timeout)
    sysd = d.get("system") if isinstance(d, dict) else None
    if not isinstance(sysd, dict) or not isinstance(d.get("devices"), list):
        raise ComfyError("對方有回JSON，但格式不像ComfyUI的 /system_stats；這個網址可能不是ComfyUI", 400)
    devs = []
    for x in d["devices"][:16]:
        if isinstance(x, dict):
            devs.append({"name": str(x.get("name") or "")[:120], "type": str(x.get("type") or "")[:20],
                         "vram_gib": round(x["vram_total"] / 2**30, 1) if isinstance(x.get("vram_total"), (int, float)) else None})
    ver = sysd.get("comfyui_version")
    return {"version": str(ver)[:40] if isinstance(ver, str) else "", "python": str(sysd.get("python_version") or "")[:20],
            "devices": devs}


def detect(hosts, timeout=1.5):
    """試本機與納管機器的固定埠，回找到的ComfyUI [{url, version}]。"""
    cands = [f"http://127.0.0.1:{p}" for p in PORTS]
    cands += [f"http://{h}:{PORTS[0]}" for h in hosts if h and h not in ("127.0.0.1", "localhost", "::1")]
    found, lock = [], threading.Lock()

    def probe(url):
        try:
            info = system_info(Conn(url, timeout=timeout), timeout)
        except (ComfyError, ValueError):
            return
        with lock:
            found.append({"url": url, "version": info["version"]})
    ts = [threading.Thread(target=probe, args=(u,), daemon=True) for u in dict.fromkeys(cands)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout * 3)
    return sorted(found, key=lambda f: cands.index(f["url"]))


# ---------------------------------------------------------------- 工作流：讀檔、一般格式轉API格式、檢查

def describe_error(d):
    """ComfyUI回的錯誤（/prompt的error與node_errors、execution_error）寫成人看得懂的一句，指出哪個節點。"""
    if not isinstance(d, dict):
        return str(d)[:300]
    parts = []
    e = d.get("error")
    if isinstance(e, dict):
        parts.append(str(e.get("message") or e.get("type") or "")[:200])
    elif e:
        parts.append(str(e)[:200])
    ne = d.get("node_errors")
    if isinstance(ne, dict):
        for nid, info in list(ne.items())[:6]:
            if not isinstance(info, dict):
                continue
            msgs = [f"{x.get('message', '')}{('：' + str(x.get('details'))) if x.get('details') else ''}"
                    for x in (info.get("errors") or [])[:3] if isinstance(x, dict)]
            parts.append(f"節點 #{str(nid)[:20]}（{str(info.get('class_type') or '')[:60]}）{'；'.join(msgs)[:300]}")
    return "；".join(p for p in parts if p) or "沒有說明"


def load_workflow(raw):
    """回 (格式, 內容)。格式＝"ui"（ComfyUI存的一般工作流）或"api"（匯出（API））。不是工作流丟ValueError。"""
    if len(raw) > MAX_WORKFLOW:
        raise ValueError("工作流檔最大 8 MB")
    try:
        d = json.loads(raw)
    except ValueError:
        raise ValueError("讀不出這個檔：不是JSON") from None
    if isinstance(d, dict) and isinstance(d.get("prompt"), dict) and not isinstance(d.get("nodes"), list):
        d = d["prompt"]
    if not isinstance(d, dict) or not d:
        raise ValueError("這不是ComfyUI的工作流檔")
    if isinstance(d.get("nodes"), list):
        if isinstance(d.get("extra"), dict) and d["extra"].get("groupNodes"):
            raise ValueError("這個工作流用了舊式的群組節點（Group Node）；請在ComfyUI按「匯出（API）」存一份再匯入")
        subs = (d.get("definitions") or {}).get("subgraphs") if isinstance(d.get("definitions"), dict) else None
        total = len(d["nodes"]) + sum(len(x.get("nodes") or []) for x in subs or [] if isinstance(x, dict))
        if total > MAX_NODES:
            raise ValueError(f"節點太多（最多 {MAX_NODES} 個）")
        return "ui", d
    if all(isinstance(v, dict) and isinstance(v.get("class_type"), str) and isinstance(v.get("inputs"), dict) for v in d.values()):
        return "api", clean_api(d)
    raise ValueError("這不是ComfyUI的工作流檔（找不到節點）")


def clean_api(d):
    if len(d) > MAX_NODES:
        raise ValueError(f"節點太多（最多 {MAX_NODES} 個）")
    out = {}
    for nid, n in d.items():
        nid = str(nid)
        if not re.fullmatch(r"[\w:.-]{1,64}", nid) or not re.fullmatch(r"[^\x00-\x1f]{1,200}", n["class_type"]):
            raise ValueError("工作流裡有節點編號或類型格式不對")
        meta = n.get("_meta") if isinstance(n.get("_meta"), dict) else {}
        title = str(meta.get("title") or "")[:200]
        out[nid] = {"class_type": n["class_type"], "inputs": dict(n["inputs"])}
        if title:
            out[nid]["_meta"] = {"title": title}
    return out


def _specs(inp):
    out = []
    for grp in ("required", "optional"):
        g = inp.get(grp) if isinstance(inp, dict) else None
        for name, s in (g.items() if isinstance(g, dict) else ()):
            s = s if isinstance(s, (list, tuple)) and s else [s]
            cfg = s[1] if len(s) > 1 and isinstance(s[1], dict) else {}
            out.append((str(name), s[0], cfg))
    return out


def spec_of(info, cls):
    """回這種節點的輸入清單 [(名稱, 型別, 設定)]，必填在前、選填在後。"""
    d = info.get(cls) if isinstance(info, dict) else None
    return _specs(d.get("input") if isinstance(d, dict) else None)


def sub_specs(cfg, key):
    """動態選單（V3 DynamicCombo）選了 key 之後多出來的輸入；API格式裡的名稱是「選單名.子輸入名」。"""
    for o in cfg.get("options") or []:
        if isinstance(o, dict) and o.get("key") == key:
            return _specs(o.get("inputs"))
    return []


def flat_spec(info, cls, inputs):
    """跟 spec_of 一樣，但把動態選單照目前選的值展開成「選單名.子輸入名」。"""
    def walk(specs, prefix, depth):
        for name, typ, cfg in specs:
            yield prefix + name, typ, cfg
            if typ == DYNAMIC_COMBO and depth < 8:
                yield from walk(sub_specs(cfg, inputs.get(prefix + name)), f"{prefix}{name}.", depth + 1)
    return list(walk(spec_of(info, cls), "", 0))


def options(typ, cfg):
    if isinstance(typ, list):
        return typ
    if typ == "COMBO" and isinstance(cfg.get("options"), list):
        return cfg["options"]
    return None


def is_widget(typ, cfg):
    """畫面上是可以直接填的欄位（不是只能接線的輸入）。V3節點的自訂欄位型別用 socketless／widgetType 標示。"""
    if cfg.get("forceInput"):
        return False
    return isinstance(typ, list) or typ in WIDGET_TYPES or typ in (DYNAMIC_COMBO, DYNAMIC_GROUP) \
        or bool(cfg.get("socketless")) or bool(cfg.get("widgetType"))


def default_of(typ, cfg):
    """新版節點多出來的欄位，舊工作流沒存值：ComfyUI前端用定義裡的預設值，這裡也一樣。回 (有沒有, 值)。"""
    if "default" in cfg:
        return True, cfg["default"]
    if typ == DYNAMIC_COMBO:
        first = next((o for o in cfg.get("options") or [] if isinstance(o, dict) and "key" in o), None)
        return (True, first["key"]) if first else (False, None)
    opts = options(typ, cfg)
    return (True, opts[0]) if opts else (False, None)


class Mismatch(ValueError):
    """存的設定值對不上ComfyUI現在的節點定義（節點版本不同，或是前端自己加的欄位），照順序對下去會對錯欄位。"""


def _links(links):
    out = {}
    for ln in links or []:
        if isinstance(ln, list) and len(ln) >= 5:
            out[ln[0]] = (str(ln[1]), ln[2], str(ln[3]), ln[4])
        elif isinstance(ln, dict) and "id" in ln:
            out[ln["id"]] = (str(ln.get("origin_id")), ln.get("origin_slot"), str(ln.get("target_id")), ln.get("target_slot"))
    return out


class _Graph:
    """一層圖：最外層，或子圖（subgraph）的一個實例。實例裡的節點編號是「實例編號:內部編號」，跟ComfyUI匯出（API）一樣。"""

    def __init__(self, nodes, links, prefix="", parent=None, inst=None, sub=None):
        self.nodes = {str(n["id"]): n for n in nodes or [] if isinstance(n, dict) and "id" in n and isinstance(n.get("type"), str)}
        self.links, self.prefix, self.parent, self.inst, self.sub = _links(links), prefix, parent, inst, sub


_KEEP = ("keep", None)                  # 子圖輸入沒接也沒有提升的值：用內部節點自己的值


def ui_to_api(ui, info):
    """一般格式 → API格式。回 (API格式, 缺的節點類型清單)。跟ComfyUI前端「匯出（API）」同一套規則：
    靜音（mode 2）的節點不送；略過（mode 4）的節點把同型別的輸入直接接到下游；Reroute、PrimitiveNode、註解只存在畫面上，攤平；
    子圖展開成「實例編號:內部編號」，子圖輸入用外面接進來的線或實例上提升的值；動態選單的子輸入叫「選單名.子輸入名」。"""
    defs = {}
    d = ui.get("definitions") if isinstance(ui.get("definitions"), dict) else {}
    for sg in d.get("subgraphs") or []:
        if isinstance(sg, dict) and isinstance(sg.get("id"), str):
            defs[sg["id"]] = sg
    missing, api = set(), {}

    def child(g, nid, n):
        sg = defs[n["type"]]
        return _Graph(sg.get("nodes"), sg.get("links"), f"{g.prefix}{nid}:", g, n, sg)

    def promoted(g, slot, depth):
        ins = g.sub.get("inputs") or []
        if not isinstance(slot, int) or not 0 <= slot < len(ins) or not isinstance(ins[slot], dict):
            return None
        name = ins[slot].get("name")
        for i in g.inst.get("inputs") or []:
            if isinstance(i, dict) and i.get("name") == name and i.get("link") is not None:
                return source(g.parent, i["link"], depth + 1)
        named = g.inst.get("widgets_values_named")
        if isinstance(named, dict) and name in named:
            return ("value", named[name])
        wv = g.inst.get("widgets_values") if isinstance(g.inst.get("widgets_values"), list) else []
        at = 0
        for x in ins:
            if not isinstance(x, dict) or x.get("type") not in WIDGET_TYPES:
                continue
            if at >= len(wv):
                return _KEEP
            if x is ins[slot]:
                return ("value", wv[at])
            at += 1
            if x.get("type") == "INT" and at < len(wv) and isinstance(wv[at], str) and wv[at] in SEED_CONTROL:
                at += 1
        return _KEEP

    def source(g, link, depth=0):
        """沿著連線往上游找：回 ("link", [節點, 輸出序號])、("value", 值)、_KEEP 或 None（上游不存在或被靜音）。"""
        if depth > 64 or link not in g.links:
            return None
        nid, slot = g.links[link][:2]
        if nid == "-10" and g.sub is not None:
            return promoted(g, slot, depth)
        n = g.nodes.get(nid)
        if not n or n.get("mode", 0) == 2:
            return None
        if n["type"] == "Reroute":
            ins = n.get("inputs") or []
            return source(g, ins[0].get("link"), depth + 1) if ins and isinstance(ins[0], dict) else None
        if n["type"] == "PrimitiveNode":
            wv = n.get("widgets_values")
            return ("value", wv[0]) if isinstance(wv, list) and wv else None
        if n.get("mode", 0) == 4:
            outs = n.get("outputs") or []
            want = outs[slot].get("type") if isinstance(slot, int) and 0 <= slot < len(outs) and isinstance(outs[slot], dict) else None
            for i in n.get("inputs") or []:
                if isinstance(i, dict) and i.get("type") == want and i.get("link") is not None:
                    return source(g, i["link"], depth + 1)
            return None
        if n["type"] in defs and g.prefix.count(":") < 8:
            c = child(g, nid, n)
            for lid, (_o, _s, tgt, tslot) in c.links.items():
                if tgt == "-20" and tslot == slot:
                    return source(c, lid, depth + 1)
            return None
        return ("link", [g.prefix + nid, slot if isinstance(slot, int) else 0])

    def node_inputs(g, n):
        linked = {i.get("name"): i.get("link") for i in n.get("inputs") or [] if isinstance(i, dict) and isinstance(i.get("name"), str)}
        wv = n.get("widgets_values")
        seq = wv if isinstance(wv, list) else []
        pos, inputs = [0], {}

        def take(name):
            if isinstance(wv, dict):
                return (True, wv[name]) if name in wv else (False, None)
            if pos[0] < len(seq):
                pos[0] += 1
                return True, seq[pos[0] - 1]
            return False, None

        def consume(specs, prefix, depth):
            for name, typ, cfg in specs:
                full = prefix + name
                if is_widget(typ, cfg):
                    ok, v = take(full)
                    if not ok:
                        ok, v = default_of(typ, cfg)
                    if v is None and typ in FRONTEND_VALUES:
                        ok, v = True, copy.deepcopy(FRONTEND_VALUES[typ])
                    if ok and typ == DYNAMIC_GROUP:
                        tpl = _specs(cfg.get("template"))
                        for k in range(v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 64 else 0):
                            consume(tpl, f"{full}.{k}.", depth + 1)
                    elif ok:
                        inputs[full] = v
                        nxt = seq[pos[0]] if not isinstance(wv, dict) and pos[0] < len(seq) else None
                        if typ == "INT" and (cfg.get("control_after_generate") or name in SEED_NAMES) \
                                and isinstance(nxt, str) and nxt in SEED_CONTROL:
                            pos[0] += 1                # 種子後面「產生後」那個下拉選單也佔一格
                        elif any(cfg.get(f) for f in UPLOAD_FLAGS) and isinstance(nxt, str) and nxt in UPLOAD_VALUES:
                            pos[0] += 1                # 上傳按鈕也存了一格
                        if typ == DYNAMIC_COMBO and depth < 8:
                            consume(sub_specs(cfg, v), full + ".", depth + 1)
                if linked.get(full) is not None:
                    got = source(g, linked[full])
                    if got is None:
                        if not is_widget(typ, cfg):
                            inputs.pop(full, None)
                    elif got is not _KEEP:
                        inputs[full] = got[1]
        consume(spec_of(info, n["type"]), "", 0)
        if not isinstance(wv, dict) and pos[0] < len(seq) and any(x not in (None, "") for x in seq[pos[0]:]):
            raise Mismatch(f"節點 #{g.prefix}{n.get('id')}（{n['type']}）")
        for name, link in linked.items():       # 數量會長的輸入（V3 Autogrow）不在定義裡，照線上的名稱接
            if name not in inputs and link is not None:
                got = source(g, link)
                if got and got is not _KEEP:
                    inputs[name] = got[1]
        return inputs

    def emit(g):
        for nid, n in g.nodes.items():
            if n.get("mode", 0) in (2, 4) or n["type"] in VIRTUAL:
                continue
            if n["type"] in defs:
                if g.prefix.count(":") < 8:
                    emit(child(g, nid, n))
                continue
            if n["type"] not in info:
                missing.add(n["type"])
                continue
            node = {"class_type": n["type"], "inputs": node_inputs(g, n)}
            if isinstance(n.get("title"), str) and n["title"].strip():
                node["_meta"] = {"title": n["title"][:200]}
            api[g.prefix + nid] = node
            if len(api) > MAX_NODES:
                raise ValueError(f"子圖展開後節點太多（最多 {MAX_NODES} 個）")
    emit(_Graph(ui.get("nodes"), ui.get("links")))
    return api, sorted(missing)


def _is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)


def roles(api):
    """每個文字節點接到哪裡：正向提示詞或負向（經過FluxGuidance、ConditioningZeroOut這類轉接也找得到）。"""
    out = {}

    def mark(nid, role, depth):
        n = api.get(nid)
        if not n or depth > 6:
            return
        out.setdefault(nid, role)
        for v in n["inputs"].values():
            if _is_link(v):
                mark(v[0], role, depth + 1)
    for n in api.values():
        for name in ("positive", "negative"):
            v = n["inputs"].get(name)
            if _is_link(v):
                mark(v[0], name, 0)
    return out


def _title(n, nid):
    return (n.get("_meta") or {}).get("title") or f"{n['class_type']} #{nid}"


def analyze(api, info, models_dir=""):
    """檢查工作流缺什麼，並找出網頁上可以調整的欄位。回 dict：
    missing_nodes、missing_models［{node, title, input, value, folder, where}］、bad_values、outputs、kind、fields。"""
    where = (models_dir.rstrip("/\\") + "/") if models_dir else "ComfyUI資料夾裡的 models/"
    missing_nodes = sorted({n["class_type"] for n in api.values() if n["class_type"] not in info})
    missing_models, bad_values, outputs, fields = [], [], [], []
    rl = roles(api)
    for nid, n in api.items():
        cls = n["class_type"]
        if cls not in info:
            continue
        if isinstance(info[cls], dict) and info[cls].get("output_node"):
            outputs.append(nid)
        title = _title(n, nid)
        for name, typ, cfg in flat_spec(info, cls, n["inputs"]):
            v = n["inputs"].get(name)
            if v is None or _is_link(v):
                continue
            opts = options(typ, cfg)
            if cls in IMAGE_LOADERS and name == "image":
                fields.append({"id": f"{nid}.{name}", "node": nid, "input": name, "kind": "image",
                               "label": "輸入圖片" if title.startswith(cls) else title, "required": opts is not None and v not in opts})
                continue
            if opts is not None and v not in opts:
                folder = MODEL_FOLDERS.get(name) or CLASS_FOLDERS.get(cls) or ("對應的子資料夾" if str(v).lower().endswith(MODEL_EXT) else "")
                row = {"node": nid, "title": title, "input": name, "value": str(v)[:200]}
                if folder:
                    missing_models.append({**row, "folder": folder, "where": where + (folder if folder != "對應的子資料夾" else "")})
                else:
                    bad_values.append({**row, "options": [str(o)[:80] for o in opts[:8]]})
                continue
            if typ == "STRING" and cfg.get("multiline") and isinstance(v, str):
                role = rl.get(nid)
                fields.append({"id": f"{nid}.{name}", "node": nid, "input": name, "kind": "text", "value": v[:20000],
                               "role": role or "", "label": {"positive": "提示詞", "negative": "不要出現的內容"}.get(role) or f"{title}・{name}"})
            elif typ == "INT" and name in SEED_NAMES:
                fields.append({"id": f"{nid}.{name}", "node": nid, "input": name, "kind": "seed", "value": v, "label": "種子"})
            elif typ in ("INT", "FLOAT") and name in NUM_LABELS and isinstance(v, (int, float)) and not isinstance(v, bool):
                f = {"id": f"{nid}.{name}", "node": nid, "input": name, "kind": "number", "int": typ == "INT", "value": v,
                     "label": NUM_LABELS[name]}
                for k in ("min", "max", "step"):
                    if isinstance(cfg.get(k), (int, float)) and not isinstance(cfg.get(k), bool):
                        f[k] = cfg[k]
                fields.append(f)
    order = {"text": 0, "image": 1, "number": 2, "seed": 3}
    fields.sort(key=lambda f: (order[f["kind"]], f.get("role") != "positive", f["kind"] == "number" and f["label"] not in ("寬", "高")))
    _dedupe_labels(fields, api)
    kind = "video" if any(api[o]["class_type"] in VIDEO_OUTPUTS for o in outputs) else "image"
    return {"missing_nodes": missing_nodes, "missing_models": missing_models, "bad_values": bad_values,
            "outputs": outputs, "kind": kind, "fields": fields}


def _dedupe_labels(fields, api):
    seen = {}
    for f in fields:
        seen[f["label"]] = seen.get(f["label"], 0) + 1
    for f in fields:
        if seen[f["label"]] > 1:
            f["label"] = f"{f['label']}（{_title(api[f['node']], f['node'])}）"


def problems_text(a):
    """匯入與產生前共用：把analyze的結果寫成給人看的幾句。回 (擋下來的, 警告)。"""
    block, warn = [], []
    if a["missing_nodes"]:
        block.append("ComfyUI少了這些節點：" + "、".join(a["missing_nodes"][:20])
                     + "。請在ComfyUI打開這個工作流，用ComfyUI Manager的「安裝缺失節點」（Install Missing Custom Nodes）裝好，重啟ComfyUI後再試")
    if not a["outputs"] and not a["missing_nodes"]:
        block.append("這個工作流沒有輸出節點（例如「儲存圖片」Save Image、「儲存影片」Save Video），跑完不會留下檔案；請在ComfyUI加一個再匯入")
    for m in a["missing_models"][:20]:
        warn.append(f"節點 #{m['node']}（{m['title']}）要用的 {m['value']} 不在ComfyUI裡：請放進 {m['where']}，然後重新整理或重啟ComfyUI")
    for b in a["bad_values"][:10]:
        warn.append(f"節點 #{b['node']}（{b['title']}）的 {b['input']} 填的是 {b['value']}，ComfyUI沒有這個選項（可選：{'、'.join(b['options'])}）")
    return block, warn


def apply_values(api, fields, values, images):
    """把網頁上填的值放回工作流。values＝{欄位id: 值}；images＝{欄位id: 已上傳的檔名}。回 (新工作流, 錯誤)。"""
    out = copy.deepcopy(api)
    values = values if isinstance(values, dict) else {}
    for f in fields:
        node = out.get(f["node"])
        if not node:
            continue
        v = values.get(f["id"])
        if f["kind"] == "image":
            if f["id"] in images:
                node["inputs"][f["input"]] = images[f["id"]]
            elif f.get("required"):
                return None, f"「{f['label']}」要先選一張圖"
            continue
        if v is None or v == "":
            continue                          # 沒填就照工作流原本的值
        if f["kind"] == "text":
            if not isinstance(v, str) or len(v) > 20000 or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", v):
                return None, f"「{f['label']}」最多 20000 字，不能有控制字元"
            node["inputs"][f["input"]] = v
        elif f["kind"] == "seed":
            try:
                s = int(v)
            except (TypeError, ValueError):
                return None, "種子要是整數，-1＝隨機"
            if s < 0:
                s = secrets.randbelow(2**50)
            node["inputs"][f["input"]] = min(s, 2**64 - 1)
        else:
            try:
                n = float(v)
            except (TypeError, ValueError):
                return None, f"「{f['label']}」要是數字"
            if n != n or n in (float("inf"), float("-inf")):
                return None, f"「{f['label']}」要是數字"
            if "min" in f and n < f["min"] or "max" in f and n > f["max"]:
                return None, f"「{f['label']}」要在 {f.get('min', '')}～{f.get('max', '')} 之間"
            node["inputs"][f["input"]] = int(n) if f.get("int") else n
    return out, None


# ---------------------------------------------------------------- 上傳、產出

def sniff(data):
    """看檔頭決定這是什麼。回 (mime, 副檔名) 或 None（不是能給瀏覽器直接看的圖片、影片、聲音）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if data[4:8] == b"ftyp":
        return ("video/quicktime", "mov") if data[8:10] == b"qt" else ("video/mp4", "mp4")
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm", "webm"
    if data[:4] == b"OggS":
        return "audio/ogg", "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return "audio/mpeg", "mp3"
    if data[:4] == b"fLaC":
        return "audio/flac", "flac"
    return None


def upload_image(conn, data, name):
    """上傳到ComfyUI的input資料夾（/upload/image）。回放進工作流用的檔名。"""
    bnd = "xc" + secrets.token_hex(12)
    body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + \
        f"\r\n--{bnd}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput\r\n--{bnd}--\r\n".encode()
    st, raw = conn.request("POST", "/upload/image", body, ctype=f"multipart/form-data; boundary={bnd}", limit=1 << 16)
    try:
        d = json.loads(raw)
    except ValueError:
        d = None
    if st != 200 or not isinstance(d, dict) or not isinstance(d.get("name"), str) or not FILE_RX.fullmatch(d["name"]):
        raise ComfyError(f"上傳輸入圖片到ComfyUI失敗（HTTP {st}）")
    sub = d.get("subfolder") if isinstance(d.get("subfolder"), str) else ""
    return f"{sub}/{d['name']}" if sub and SUBFOLDER_RX.fullmatch(sub) and ".." not in sub else d["name"]


def output_files(entry):
    """從/history的一筆結果挑出要取回的檔案。有output類型的就不拿temp（預覽）。回 [{filename, subfolder, type}]。"""
    outs = entry.get("outputs") if isinstance(entry, dict) else None
    files = []
    for node_out in (outs.values() if isinstance(outs, dict) else ()):
        if not isinstance(node_out, dict):
            continue
        for key in ("images", "gifs", "videos", "video", "audio"):
            for f in node_out.get(key) or []:
                if not isinstance(f, dict):
                    continue
                name, sub, typ = f.get("filename"), f.get("subfolder") or "", f.get("type") or "output"
                if (isinstance(name, str) and FILE_RX.fullmatch(name) and name not in (".", "..") and isinstance(sub, str)
                        and SUBFOLDER_RX.fullmatch(sub) and ".." not in sub.replace("\\", "/").split("/")
                        and typ in ("output", "temp")):
                    files.append({"filename": name, "subfolder": sub, "type": typ})
    if any(f["type"] == "output" for f in files):
        files = [f for f in files if f["type"] == "output"]
    uniq = {(f["filename"], f["subfolder"], f["type"]): f for f in files}
    return list(uniq.values())[:MAX_OUTPUTS]


def fetch_outputs(conn, entry, out_dir):
    """取回這件的產出存到 out_dir。回 [{name, mime, size}]。不是圖片、影片、聲音的檔案不收。"""
    saved, total = [], 0
    for i, f in enumerate(output_files(entry)):
        st, data = conn.request("GET", "/view?" + urlencode(f), ctype=None, limit=MAX_FILE, timeout=300)
        if st != 200:
            continue
        kind = sniff(data)
        if not kind:
            continue
        total += len(data)
        if total > MAX_JOB_BYTES:
            break
        path = os.path.join(out_dir, f"{i}.{kind[1]}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        saved.append({"file": os.path.basename(path), "name": f["filename"][:200], "mime": kind[0], "size": len(data)})
    return saved


# ---------------------------------------------------------------- 執行一件

def run(conn, api, uploads, job, lock, out_dir):
    """送出、追進度、取回產出；job是共用的dict（lock保護），job["cancel"]是threading.Event。
    uploads＝[(欄位id, 圖片位元組)]。結束時 job["state"] 是 done／failed／cancelled。"""
    def set_(**kw):
        with lock:
            job.update(kw)
    t0 = time.time()
    try:
        images = {}
        for fid, data in uploads:
            set_(phase="上傳輸入圖片")
            images[fid] = upload_image(conn, data, f"xconsole_{job['id']}_{len(images)}.{(sniff(data) or ('', 'png'))[1]}")
        prompt, err = apply_values(api, job["fields"], job["values"], images)
        if err:
            raise ComfyError(err, 400)
        cid = secrets.token_hex(16)
        ws = None
        try:
            ws = conn.ws(cid)
        except ComfyError:
            ws = None                         # 前面有不支援WebSocket的反向代理：改成輪詢/history
        try:
            st, raw = conn.request("POST", "/prompt", {"prompt": prompt, "client_id": cid}, limit=1 << 20)
            try:
                d = json.loads(raw)
            except ValueError:
                d = None
            if st != 200 or not isinstance(d, dict) or not isinstance(d.get("prompt_id"), str):
                raise ComfyError("ComfyUI不接受這個工作流：" + describe_error(d if isinstance(d, dict) else {"error": raw[:200].decode("utf-8", "replace")}), 400)
            pid = d["prompt_id"]
            if not re.fullmatch(r"[\w-]{1,64}", pid):
                raise ComfyError("ComfyUI回的工作編號格式不對")
            if isinstance(d.get("node_errors"), dict) and d["node_errors"]:
                set_(warnings=["這幾個節點有錯、ComfyUI不會執行它們（其他照跑）：" + describe_error({"node_errors": d["node_errors"]})])
            set_(prompt_id=pid, phase="排隊中", total_nodes=len(prompt))
            entry = _follow(conn, ws, pid, job, set_, t0, {nid: _title(n, nid) for nid, n in prompt.items()})
        finally:
            if ws:
                ws.close()
        if job["cancel"].is_set():
            set_(state="cancelled", phase="已取消", secs=time.time() - t0)
            return
        set_(phase="取回產出")
        outs = fetch_outputs(conn, entry, out_dir)
        if not outs:
            raise ComfyError("ComfyUI跑完了，但沒有可以取回的圖片或影片：工作流的輸出節點可能只存到別處，或產出的不是圖片、影片、聲音")
        set_(state="done", phase="完成", outputs=outs, secs=time.time() - t0)
    except ComfyError as exc:
        set_(state="cancelled" if job["cancel"].is_set() else "failed", error=exc.msg, phase="", secs=time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        set_(state="failed", error=f"處理時出錯：{type(exc).__name__}", phase="", secs=time.time() - t0)


def _cancel(conn, pid, started):
    try:
        if not started:
            conn.request("POST", "/queue", {"delete": [pid]}, limit=1 << 16, timeout=10)
        conn.request("POST", "/interrupt", {"prompt_id": pid}, limit=1 << 16, timeout=10)
    except ComfyError:
        pass


def _history(conn, pid):
    d = conn.json("GET", f"/history/{pid}", limit=MAX_JSON, timeout=30)
    return d.get(pid) if isinstance(d, dict) else None


def _follow(conn, ws, pid, job, set_, t0, titles):
    """等到這件跑完。回/history裡這件的結果；取消時回None；出錯丟ComfyError。"""
    started, asked_cancel, last_ok, seen = False, 0.0, time.time(), set()
    while True:
        now = time.time()
        if now - t0 > JOB_TIMEOUT:
            _cancel(conn, pid, started)
            raise ComfyError("超過 6 小時還沒跑完，已請ComfyUI停下")
        if job["cancel"].is_set() and not asked_cancel:
            asked_cancel = now
            _cancel(conn, pid, started)
            set_(phase="取消中：ComfyUI要等正在跑的這個節點做完才會停")
        if asked_cancel and now - asked_cancel > CANCEL_WAIT:
            raise ComfyError(f"已請ComfyUI停下，但過了 {CANCEL_WAIT // 60} 分鐘它還在跑這件；請到ComfyUI確認")
        msg = ws.recv() if ws else "idle"
        if msg is None:
            ws = None
            set_(live=False)
            continue
        if msg == "idle":
            try:
                entry = _history(conn, pid)
                last_ok = time.time()
            except ComfyError:
                if time.time() - last_ok > STALL_LIMIT:
                    raise ComfyError("ComfyUI斷線超過 90 秒，這件沒追完；ComfyUI恢復後可以到它的output資料夾找") from None
                entry = None
            if entry:
                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                if status.get("status_str") == "error":
                    raise ComfyError(_history_error(status, titles))
                if status.get("completed", True) or entry.get("outputs"):
                    return entry
            if not ws:
                time.sleep(1.5)
            continue
        last_ok = time.time()
        typ, data = msg.get("type"), msg.get("data")
        if not isinstance(data, dict) or (data.get("prompt_id") not in (None, pid)):
            continue
        if typ == "status":
            q = ((data.get("status") or {}).get("exec_info") or {}).get("queue_remaining") if isinstance(data.get("status"), dict) else None
            if not started and isinstance(q, int):
                set_(queue=q)
        elif typ == "execution_start":
            started = True
            set_(phase="開始產生", queue=0)
        elif typ == "execution_cached":
            nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
            seen.update(str(n) for n in nodes)
            set_(done_nodes=len(seen))
        elif typ == "executing":
            node = data.get("node")
            started = True
            if node is None:
                return None if job["cancel"].is_set() else _wait_history(conn, pid)
            node = str(node)
            seen.add(node)
            set_(phase=f"執行節點 #{node[:20]}（{titles.get(node, '')[:60]}）", node=node[:20], step=0, steps=0, done_nodes=len(seen))
        elif typ == "progress":
            v, mx = data.get("value"), data.get("max")
            if isinstance(v, int) and isinstance(mx, int) and 0 < mx < 10**7:
                set_(step=v, steps=mx)
        elif typ == "execution_error":
            nid = str(data.get("node_id") or "")[:20]
            raise ComfyError(f"節點 #{nid}（{str(data.get('node_type') or titles.get(nid, ''))[:60]}）出錯：{str(data.get('exception_message') or '').strip()[:400]}")
        elif typ == "execution_interrupted":
            if job["cancel"].is_set():
                return None
            raise ComfyError("ComfyUI那邊中斷了這件（有人在ComfyUI按了停止）")
        elif typ == "execution_success":
            return _wait_history(conn, pid)


def _wait_history(conn, pid):
    for _ in range(20):
        entry = _history(conn, pid)
        if entry:
            return entry
        time.sleep(0.5)
    raise ComfyError("ComfyUI說跑完了，但歷史紀錄裡找不到這件")


def _history_error(status, titles):
    for m in status.get("messages") or []:
        if isinstance(m, list) and len(m) == 2 and m[0] == "execution_error" and isinstance(m[1], dict):
            nid = str(m[1].get("node_id") or "")[:20]
            return f"節點 #{nid}（{str(m[1].get('node_type') or titles.get(nid, ''))[:60]}）出錯：{str(m[1].get('exception_message') or '').strip()[:400]}"
    return "ComfyUI回報這件失敗，沒有說明"
