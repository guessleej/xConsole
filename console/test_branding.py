#!/usr/bin/env python3
"""客戶品牌與頁尾：另起一個隔離的主控台（暫存家目錄、純 HTTP），經 API 走完整套流程。
跑法：python3 test_branding.py（需要 requirements.txt 的套件；不碰正式環境）
"""
import base64
import json
import os
import struct
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zlib

TMP = tempfile.mkdtemp(prefix="xc-brand-")
os.environ["HOME"] = TMP                       # 狀態目錄跟著家目錄走，一定要在 import 之前
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xconsole_server as x  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def png(w, h):
    row = b"\x00" + b"\x00\x00\x00\xff" * w
    raw = zlib.compress(row * h)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def jpeg(w, h, sos=True, eoi=True, sof_first=True):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    scan = (b"\xff\xda" + struct.pack(">HB", 8, 1) + b"\x01\x00\x00\x3f\x00" + b"\x12\x34\x56") if sos else b""
    body = app0 + sof0 + scan if sof_first else app0 + scan + sof0
    return b"\xff\xd8" + body + (b"\xff\xd9" if eoi else b"")


def webp(kind, w, h, image=True):
    vp8l = b"VP8L" + struct.pack("<I", 5) + b"\x2f" + ((w - 1) | ((h - 1) << 14)).to_bytes(4, "little")
    if kind == "VP8X":
        body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
        if image:
            body += vp8l + b"\x00"                                # 奇數長度的區塊後面補一個位元組
    elif kind == "VP8L":
        body = vp8l + b"\x00"
    else:
        body = b"VP8 " + struct.pack("<I", 10) + b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", w, h)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


# ---- 1. 圖檔辨識：看檔頭不看副檔名
check("PNG 讀得到尺寸", x.image_info(png(265, 65)) == ("png", 265, 65))
check("JPEG 讀得到尺寸", x.image_info(jpeg(300, 80)) == ("jpg", 300, 80))
check("WebP VP8X 讀得到尺寸", x.image_info(webp("VP8X", 400, 100)) == ("webp", 400, 100))
check("WebP VP8L 讀得到尺寸", x.image_info(webp("VP8L", 120, 40)) == ("webp", 120, 40))
check("WebP VP8 讀得到尺寸", x.image_info(webp("VP8 ", 64, 32)) == ("webp", 64, 32))
check("SVG 一律不認", x.image_info(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>') is None)
check("GIF 不認", x.image_info(b"GIF89a" + b"\x00" * 20) is None)
check("隨便的位元組不認", x.image_info(os.urandom(64)) is None)
check("截斷的 PNG 不會丟例外", x.image_info(b"\x89PNG\r\n\x1a\n\x00\x00") is None)
check("截斷的 JPEG 不會丟例外", x.image_info(b"\xff\xd8\xff\xe0\x00\x10JFIF") is None)
check("截斷的 WebP 不會丟例外", x.image_info(b"RIFF\x00\x00\x00\x00WEBPVP8X\x00") is None)

# 只看檔頭會被騙：下面這些瀏覽器都會顯示破圖，一律擋（2026-10-27 審查意見）
good = png(64, 16)
forged = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 265, 65)
check("只偽造 PNG 檔頭（沒有 CRC、影像資料、結尾）擋下", x.image_info(forged) is None)
check("PNG 少了 IEND 擋下", x.image_info(good[:-12]) is None)
bad_crc = bytearray(good); bad_crc[29] ^= 0xFF
check("PNG 區塊 CRC 對不上擋下", x.image_info(bytes(bad_crc)) is None)
no_idat = good[:8 + 25] + good[-12:]
check("PNG 沒有影像資料（IDAT）擋下", x.image_info(no_idat) is None)
check("JPEG 少了結尾 EOI 擋下", x.image_info(jpeg(300, 80, eoi=False)) is None)
check("JPEG 沒有影像資料（SOS）擋下", x.image_info(jpeg(300, 80, sos=False)) is None)
check("JPEG 尺寸在影像資料之後擋下", x.image_info(jpeg(300, 80, sof_first=False)) is None)
check("WebP 宣告大小跟實際不符擋下", x.image_info(webp("VP8X", 400, 100) + b"\x00\x00") is None)
check("WebP 只有 VP8X、沒有影像區塊擋下", x.image_info(webp("VP8X", 400, 100, image=False)) is None)
w = bytearray(webp("VP8 ", 64, 32)); w[23] = 0
check("WebP VP8 起始碼不對擋下", x.image_info(bytes(w)) is None)

# ---- 2. 起一個隔離的主控台
x.set_credentials("admin", "brand-test-pw")
srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
COOKIE = {}


def req(method, path, body=None, ctype="application/json", raw=False):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", ctype)
    if COOKIE.get("v"):
        r.add_header("Cookie", "xconsole=" + COOKIE["v"])
    try:
        resp = urllib.request.urlopen(r, timeout=10)
    except urllib.error.HTTPError as e:
        resp = e
    payload = resp.read()
    for k, v in resp.headers.items():
        if k.lower() == "set-cookie" and v.startswith("xconsole="):
            COOKIE["v"] = v.split(";")[0].split("=", 1)[1]
    return resp.status, (payload if raw else json.loads(payload or b"{}")), resp.headers


st, b, _ = req("GET", "/api/branding")
check("沒登入也拿得到品牌資料（登入頁要用）", st == 200 and b["custom"] is False and b["org"] == "云碩科技" and b["logo"] is None, b)
st, _, _ = req("GET", "/branding/logo", raw=True)
check("還沒上傳時 Logo 回 404", st == 404)
st, _, _ = req("POST", "/api/branding", {"org": "中原大學", "product": "AI 模型主控台"})
check("沒登入不能改品牌", st == 401)
st, _, _ = req("POST", "/api/branding/logo", {"data": base64.b64encode(png(64, 16)).decode()})
check("沒登入不能上傳 Logo", st == 401)

st, _, _ = req("POST", "/api/login", {"username": "admin", "password": "brand-test-pw"})
check("登入", st == 200 and COOKIE.get("v"))

# ---- 3. 名稱
for org, why in (("", "空白"), ("<script>", "含 < >"), ("中" * 41, "超過 40 字"), ("中原\x07大學", "控制字元")):
    st, b, _ = req("POST", "/api/branding", {"org": org, "product": "AI 模型主控台"})
    check(f"單位名稱{why}被擋", st == 400, b)
st, b, _ = req("POST", "/api/branding", {"org": "中原大學", "product": ""})
check("系統名稱空白被擋", st == 400, b)
st, b, _ = req("POST", "/api/branding", b"org=x", ctype="application/x-www-form-urlencoded")
check("跨站表單送不進來（只收 JSON）", st == 415)
st, b, _ = req("POST", "/api/branding", {"org": "  中原大學 ", "product": "AI 模型主控台"})
check("名稱存好、前後空白去掉", st == 200 and b["branding"]["org"] == "中原大學" and b["branding"]["custom"] is True, b)
st, b, _ = req("GET", "/api/branding")
check("登出前後都讀得到新名稱", b["product"] == "AI 模型主控台")

# ---- 4. Logo
bad = [("SVG", b'<svg xmlns="http://www.w3.org/2000/svg"/>'), ("GIF", b"GIF89a" + b"\x00" * 30),
       ("太小 8x8", png(8, 8)), ("太大 5000 寬", png(5000, 20)),
       ("超過 1 MB", png(64, 16) + b"\x00" * (1024 * 1024))]
for name, data in bad:
    st, b, _ = req("POST", "/api/branding/logo", {"data": base64.b64encode(data).decode()})
    check(f"{name}被擋", st == 400, b)
st, b, _ = req("POST", "/api/branding/logo", {"data": "這不是 base64!!"})
check("壞掉的 base64 被擋", st == 400, b)
st, b, _ = req("POST", "/api/branding/logo", {})
check("沒附檔案被擋", st == 400, b)

logo = png(265, 65)
st, b, _ = req("POST", "/api/branding/logo", {"data": base64.b64encode(logo).decode()})
check("上傳 PNG", st == 200 and b["branding"]["logo"]["width"] == 265 and b["branding"]["logo"]["height"] == 65, b)
url = b["branding"]["logo"]["url"]
COOKIE.clear()                                          # Logo 要給登入頁用，不登入也要拿得到
st, body, hdr = req("GET", url, raw=True)
check("不登入也拿得到 Logo、內容一樣", st == 200 and body == logo)
check("Logo 的 Content-Type 照檔頭判斷", hdr.get("Content-Type") == "image/png")
check("Logo 回應帶 nosniff", hdr.get("X-Content-Type-Options") == "nosniff")
mode = oct(os.stat(os.path.join(x.BRAND_DIR, "logo.png")).st_mode & 0o777)
check("Logo 檔權限 600", mode == "0o600", mode)

req("POST", "/api/login", {"username": "admin", "password": "brand-test-pw"})
st, b, _ = req("POST", "/api/branding/logo", {"data": base64.b64encode(jpeg(300, 80)).decode()})
check("換成 JPEG", st == 200 and b["branding"]["logo"]["url"] != url)
check("舊的 PNG 檔已刪除", not os.path.exists(os.path.join(x.BRAND_DIR, "logo.png"))
      and os.path.exists(os.path.join(x.BRAND_DIR, "logo.jpg")))
st, body, hdr = req("GET", b["branding"]["logo"]["url"], raw=True)
check("JPEG 的 Content-Type 正確", hdr.get("Content-Type") == "image/jpeg")

# 兩個上傳重疊：不准回 500、不准留下暫存檔、最後的中繼資料一定指到一個真的存在的檔（2026-10-27 審查意見）
import concurrent.futures as _cf
payloads = [base64.b64encode(png(64 + i, 16) if i % 2 else jpeg(300 + i, 80)).decode() for i in range(12)]
with _cf.ThreadPoolExecutor(max_workers=6) as pool:
    codes = list(pool.map(lambda d: req("POST", "/api/branding/logo", {"data": d})[0], payloads))
check("同時上傳 12 次全部成功", codes == [200] * 12, codes)
left = sorted(os.listdir(x.BRAND_DIR))
check("同時上傳後只剩一個 Logo 檔、沒有暫存檔", len(left) == 1 and left[0].startswith("logo."), left)
meta = x.fleet()["branding"]["logo"]
check("中繼資料指到的檔案真的存在", os.path.isfile(os.path.join(x.BRAND_DIR, meta["file"])), meta)
st, body, hdr = req("GET", "/branding/logo", raw=True)
check("同時上傳後 Logo 還讀得到", st == 200 and x.image_info(body) is not None)

st, b, _ = req("POST", "/api/branding/logo", {"remove": True})
check("移除 Logo", st == 200 and b["branding"]["logo"] is None)
check("Logo 檔已刪除", not os.listdir(x.BRAND_DIR))
st, _, _ = req("GET", "/branding/logo", raw=True)
check("移除後 Logo 回 404", st == 404)
check("移除 Logo 後名稱還在", b["branding"]["custom"] is True and b["branding"]["org"] == "中原大學")

# ---- 5. 稽核：每次變更都有紀錄，而且不記圖檔內容
items = x.STORE.audit_list(200)
acts = [a["action"] for a in items]
check("改名稱有稽核", "branding.update" in acts)
check("換 Logo 有稽核", acts.count("branding.logo") >= 3)
blob = json.dumps(items, ensure_ascii=False)
check("稽核不記圖檔內容", base64.b64encode(logo).decode()[:40] not in blob)

# ---- 6. 頁尾寫死在頁面裡：登入頁與主畫面各一個，不能被拿掉
html = open(os.path.join(x.STATIC, "index.html"), encoding="utf-8").read()
check("登入頁與主畫面都有 Powered by xCloudinfo", html.count('<footer class="powered">Powered by <strong>xCloudinfo</strong> 云碩科技</footer>') == 2)
js = open(os.path.join(x.STATIC, "app.js"), encoding="utf-8").read()
check("前端沒有任何地方動頁尾", ".powered" not in js and "powered" not in js.replace("Powered by xCloudinfo", ""))
check("退回沒自訂時整組還原預設（不是直接 return）", "if (!b.custom) { resetBranding(); return; }" in js and "function resetBranding()" in js)
css = open(os.path.join(x.STATIC, "style.css"), encoding="utf-8").read()
check("登入頁頁尾排在版面裡、不浮在登入鈕上", ".login-page { grid-template-rows: 1fr auto; }" in css
      and "position: absolute" not in css.split(".login-page .powered {", 1)[1].split("}", 1)[0])
check("品牌資料回來前先藏住品牌區，避免先閃一下預設名稱", 'class="brand-pending"' in html and 'classList.remove("brand-pending")' in js)

srv.shutdown()
print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
