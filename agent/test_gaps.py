#!/usr/bin/env python3
"""補齊 LocModel 差距那一批的代理端自我檢查。
  1. Linux 的 Intel 顯示卡：sysfs 偵測、Arc 讀得到顯存／內建晶片讀不到、記憶體架構建議、不給綁卡、
     獨立顯存架構下只有讀不到顯存的卡時照「沒有 GPU」算（不會把每個模型都擋下）、Vulkan 編譯旗標
  2. 機械硬碟或 SSD：macOS 讀 diskutil 的 SolidState、Windows 讀 Get-PhysicalDisk 的 MediaType
  3. 推理引擎預編譯版：下載到一半的檔接著下載（Range）、整個檔核對 sha256、裝好後清掉下載檔；暫停記成 paused
  4. 手動匯入安裝包：分塊續傳、檔頭檢查、sha256 對不上不裝、找得到包在子資料夾的執行檔、試跑、裝進家目錄
  5. 畫圖：參考圖只收 PNG／JPEG／WebP、大小上限；sd-cli 參數帶 -i／--strength／-b；一次多張的產出檔名
跑法：python3 test_gaps.py
"""
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-gaps-")
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

GIB = A.GIB
OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def eq(name, got, want):
    case(name, got == want, f"得到 {got}，預期 {want}")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------- 1. Intel
drm = tempfile.mkdtemp(prefix="drm-")
write(os.path.join(drm, "card0/device/vendor"), "0x8086\n")
write(os.path.join(drm, "card0/device/class"), "0x030000\n")
write(os.path.join(drm, "card0/device/tile0/physical_vram_size_bytes"), str(16 * GIB))
write(os.path.join(drm, "card1/device/vendor"), "0x8086\n")
write(os.path.join(drm, "card1/device/class"), "0x038000\n")
write(os.path.join(drm, "card2/device/vendor"), "0x1a03\n")           # ASPEED 的 BMC 顯示：不是 GPU
write(os.path.join(drm, "card2/device/class"), "0x030000\n")
write(os.path.join(drm, "card0-DP-1/status"), "connected\n")          # 輸出埠，不是卡
real_os = A.OS
A.OS = "linux"
g = A.probe_intel_sysfs(drm) or []
eq("Intel：只認 0x8086 的顯示類裝置", [x["id"] for x in g], ["0", "1"])
eq("Intel：Arc 讀得到顯存（xe 的 tile0）", g[0]["vram_total"] if g else None, 16 * GIB)
eq("Intel：讀不到顯存的當成內建顯示晶片", (g[1]["name"], g[1]["vram_total"]) if len(g) > 1 else None, ("Intel 內建顯示晶片", 0))
A.OS = real_os

igpu = [{"id": "1", "name": "Intel 內建顯示晶片", "vram_total": 0, "vram_used": 0, "vendor": "intel"}]
orig = {k: getattr(A, k) for k in ("has_nvidia", "probe_amd_sysfs", "probe_intel_sysfs", "ram_pool", "_vulkan_ready", "shutil")}
A.has_nvidia = lambda: False
A.probe_amd_sysfs = lambda: None
if A.OS == "linux" and not os.path.exists("/etc/nv_tegra_release"):
    eq("Intel 內建晶片：建議統一記憶體（單一池）", A.suggest_memory_model(igpu)[0], "unified_single")

reg = {"machine": {"memory_model": "discrete"}, "slots": []}
hw = {"gpu": {"gpus": igpu, "vram_total": 0, "vram_used": 0}}
A.ram_pool = lambda: {"total_gib": 64.0, "used_gib": 8.0, "free_gib": 56.0}
p = A.pools(reg, hw)
real_os = A.OS
A.OS = "linux"
case("Intel：Linux 上不給綁卡", A.pools(reg, hw)["bindable"] is False)
A.OS = real_os
case("獨立顯存架構、只有讀不到顯存的卡：卡的顯存是 0", p["gpus"][0]["total_gib"] == 0)

# 准入：這種情況權重要算進系統記憶體，不是顯存（否則每個模型都被「顯存不夠」擋下）
case("准入：只有讀不到顯存的卡時照沒有 GPU 算（程式裡的判斷）",
     'not any(g["total_gib"] > 0 for g in p["gpus"])' in open(A.__file__, encoding="utf-8").read())

A.probe_intel_sysfs = lambda base=None: igpu
A._vulkan_ready = lambda: True


class FakeShutil:
    @staticmethod
    def which(x):
        return "/usr/bin/" + x if x in ("git", "cmake", "c++") else None


A.shutil = FakeShutil
real_os = A.OS
A.OS = "linux"
pre = A.build_prereqs("llama")
eq("Intel＋Vulkan 套件齊：用 Vulkan 編", (pre["backend"], pre["flags"]), ("vulkan", ["-DGGML_VULKAN=ON"]))
eq("Intel＋Vulkan：畫圖引擎的旗標", A.build_prereqs("sd")["flags"], ["-DSD_VULKAN=ON"])
A._vulkan_ready = lambda: False
pre = A.build_prereqs("llama")
case("Intel 但缺 Vulkan 套件：編 CPU 版並寫出要 IT 裝什麼", pre["backend"] == "cpu" and any("libvulkan-dev glslc" in n for n in pre["notes"]))
A.probe_intel_sysfs = lambda base=None: None
A.probe_amd_sysfs = lambda: [{"id": "0"}]
A._hipcc = lambda: None
A._vulkan_ready = lambda: True
eq("AMD 沒有 ROCm、有 Vulkan：改用 Vulkan", A.build_prereqs("llama")["backend"], "vulkan")
A.OS = real_os
for k, v in orig.items():
    setattr(A, k, v)

# ---------------------------------------------------------------- 2. 機械硬碟或 SSD
real_sh, real_os = A.sh, A.OS
A.OS = "darwin"
import plistlib  # noqa: E402
A.sh = lambda argv, timeout=20: (0, plistlib.dumps({"SolidState": True}).decode(), "")
eq("macOS：SolidState=True → 不是機械硬碟", A.disk_is_hdd("/", "/"), False)
A.sh = lambda argv, timeout=20: (0, plistlib.dumps({"SolidState": False}).decode(), "")
eq("macOS：SolidState=False → 機械硬碟", A.disk_is_hdd("/Volumes/HDD", "/Volumes/HDD"), True)
A.sh = lambda argv, timeout=20: (1, "", "沒有這個磁碟")
eq("macOS：讀不到 → 不知道", A.disk_is_hdd("/x", "/x"), None)
A.OS = "windows"
A._WIN_MEDIA.update(at=0, map={})
A.sh = lambda argv, timeout=20: (0, "C=SSD\nD=HDD\nE=Unspecified\n", "")
eq("Windows：C 是 SSD", A.disk_is_hdd("C:\\", "C:\\"), False)
eq("Windows：D 是機械硬碟", A.disk_is_hdd("D:\\", "D:\\"), True)
eq("Windows：MediaType 沒標的 → 不知道", A.disk_is_hdd("E:\\", "E:\\"), None)
calls = []
A.sh = lambda argv, timeout=20: (calls.append(1), (0, "C=HDD", ""))[1]
A.disk_is_hdd("C:\\", "C:\\")
eq("Windows：查一次快取十分鐘（PowerShell 慢）", len(calls), 0)
A.sh, A.OS = real_sh, real_os

# ---------------------------------------------------------------- 3／4 假的執行檔
POSIX = os.name == "posix"
FAKE_EXE = f"#!{sys.executable}\nimport sys\nprint('version: 9999 (test)')\n"


def make_zip(inner_dir, target):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        info = zipfile.ZipInfo(f"{inner_dir}{target}")
        info.external_attr = 0o755 << 16
        z.writestr(info, FAKE_EXE)
        z.writestr(f"{inner_dir}libggml-vulkan.so", b"\0")
        top = inner_dir.split("/")[0]
        if inner_dir.count("/") > 1:
            z.writestr(f"{top}/lib/libextra.so", b"\0")      # 函式庫放在旁邊的資料夾
    return b.getvalue()


def wait_build(kind="llama", secs=30):
    t0 = time.time()
    while time.time() - t0 < secs:
        if not A._BUILD.get("thread_alive"):
            break
        time.sleep(0.1)
    return A.load_json(A.ENGINES[kind]["file"], {})


# ---- 3. 預編譯版接著下載
pkg = make_zip("", "llama-server")
digest = "sha256:" + hashlib.sha256(pkg).hexdigest()
seen = {"range": []}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        seen["range"].append(rng)
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        body = pkg[start:]
        self.send_response(206 if rng else 200)
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{len(pkg) - 1}/{len(pkg)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/llama-b9999-bin-test.zip"
asset = {"name": "llama-b9999-bin-test.zip", "browser_download_url": url, "size": len(pkg), "digest": digest}
dl = os.path.join(A.ENGINE_DIR, "dl")
os.makedirs(dl, exist_ok=True)
with open(os.path.join(dl, asset["name"] + ".part"), "wb") as fh:
    fh.write(pkg[: len(pkg) // 2])                    # 上次下載到一半
with open(os.path.join(dl, "llama-b1-bin-old.zip.part"), "wb") as fh:
    fh.write(b"old")                                  # 別的版本留下的半成品
if POSIX:
    dest = os.path.join(A.HOME, "llama.cpp-b9999-test")
    log = A.engine_file("build", "-llama-build.log")
    open(log, "w").close()
    st = {"state": "running", "phase": "準備", "tag": "b9999", "dest": dest, "log": log, "mode": "prebuilt", "asset": asset, "extra": None}
    A._BUILD.update(cancel=False, thread_alive=True, kind="llama")
    A._prebuilt_run(st, log, "llama")
    eq("接著下載：裝好", st["state"], "done")
    eq("接著下載：送 Range 從一半接著要", seen["range"][:1], [f"bytes={len(pkg) // 2}-"])
    case("接著下載：記錄寫明接著上次", "接著上次" in open(log, encoding="utf-8").read())
    case("裝好後下載檔清掉、別的版本的半成品也清掉", os.listdir(dl) == [], os.listdir(dl))
    case("執行檔裝進家目錄", os.path.isfile(os.path.join(dest, "llama-server")))

    # sha256 對不上：刪掉不裝
    bad = dict(asset, digest="sha256:" + "0" * 64)
    st = {"state": "running", "phase": "準備", "tag": "b9999", "dest": dest + "x", "log": log, "mode": "prebuilt", "asset": bad, "extra": None}
    A._BUILD.update(cancel=False, thread_alive=True, kind="llama")
    A._prebuilt_run(st, log, "llama")
    case("sha256 對不上：失敗、下載檔刪掉", st["state"] == "failed" and "sha256" in st["error"] and not os.listdir(dl), st)

    # 暫停：下載中按取消 → paused，半成品保留
    st = {"state": "running", "phase": "準備", "tag": "b9999", "dest": dest + "y", "log": log, "mode": "prebuilt", "asset": asset, "extra": None}
    A._BUILD.update(cancel=True, thread_alive=True, kind="llama")
    A._prebuilt_run(st, log, "llama")
    eq("下載中按停：記成暫停", st["state"], "paused")
    A._BUILD["cancel"] = False
srv.shutdown()

# ---- 4. 手動匯入
eq("匯入：檔名要是壓縮檔", A.engine_import_chunk("llama", "evil.sh", 0, 10, b"x" * 10)[0], 400)
eq("匯入：不准跳出資料夾", A.engine_import_chunk("llama", "../x.zip", 0, 10, b"x" * 10)[0], 400)
eq("匯入：不認得的引擎", A.engine_import_chunk("bad", "a.zip", 0, 10, b"x" * 10)[0], 400)
c, r = A.engine_import_chunk("llama", "notzip.zip", 0, 4, b"ABCD")
case("匯入：檔頭不是 zip／gzip 就刪掉", c == 400 and "檔頭" in r["error"] and not A.engine_import_status("llama", "notzip.zip")[1]["received"], r)
pkg2 = make_zip("llama-b9999-bin-ubuntu-vulkan-x64/build/bin/", "llama-server")
half = len(pkg2) // 2
eq("匯入：第一塊", A.engine_import_chunk("llama", "llama-pkg.zip", 0, len(pkg2), pkg2[:half])[1], {"received": half})
c, r = A.engine_import_chunk("llama", "llama-pkg.zip", 0, len(pkg2), pkg2[:half])
case("匯入：位置不對要從已收到的接著送", c == 409 and r.get("received") == half, r)
eq("匯入：狀態回已收到多少", A.engine_import_status("llama", "llama-pkg.zip")[1]["received"], half)
eq("匯入：狀態回已收到那段開頭的 sha256（瀏覽器比對是不是同一個檔）",
   A.engine_import_status("llama", "llama-pkg.zip")[1].get("head_sha256"), hashlib.sha256(pkg2[:min(half, 4 << 20)]).hexdigest())
c, r = A.engine_import_chunk("llama", "llama-pkg.zip", half, len(pkg2), pkg2[half:])
case("匯入：送齊", c == 200 and r.get("done"), r)
eq("匯入：sha256 格式不對", A.engine_import_install("llama", "llama-pkg.zip", "abc")[0], 400)
if POSIX:
    c, r = A.engine_import_install("llama", "llama-pkg.zip", "f" * 64)
    st = wait_build()
    case("匯入：sha256 對不上不裝", st.get("state") == "failed" and "不一樣" in (st.get("error") or ""), st.get("error"))
    A.engine_import_chunk("llama", "llama-pkg.zip", 0, len(pkg2), pkg2)
    c, r = A.engine_import_install("llama", "llama-pkg.zip", hashlib.sha256(pkg2).hexdigest())
    st = wait_build()
    eq("匯入：sha256 對得上、裝好", st.get("state"), "done")
    case("匯入：找得到包在 build/bin/ 的執行檔、整包裝進 llama.cpp-import-<sha>",
         st.get("exe", "").endswith(os.path.join(f"llama.cpp-import-{hashlib.sha256(pkg2).hexdigest()[:8]}", "build", "bin", "llama-server")), st.get("exe"))
    case("匯入：旁邊資料夾的檔案也一起搬過去", os.path.isfile(os.path.join(st["dest"], "lib", "libextra.so")))
    case("匯入：裝好的會被找到（llama_globs）", os.path.realpath(st.get("exe")) in map(os.path.realpath, A.llama_globs()), A.llama_globs())
    case("匯入：函式庫跟著搬過去（辨認 GPU 後端）", A.llama_backend(st["exe"]) == "Vulkan")
    case("匯入：安裝包刪掉", not A.engine_import_status("llama", "llama-pkg.zip")[1]["exists"])
    # 核對完之後按取消：解壓縮後就停，不會裝上去
    A.engine_import_chunk("llama", "llama-pkg.zip", 0, len(pkg2), pkg2)
    real_extract = A._safe_extract

    def extract_then_cancel(arc, dest):
        real_extract(arc, dest)
        A._BUILD["cancel"] = True

    A._safe_extract = extract_then_cancel
    shutil.rmtree(st["dest"])
    A.engine_import_install("llama", "llama-pkg.zip")
    st2 = wait_build()
    A._safe_extract = real_extract
    case("匯入：核對完之後取消，停下來、不裝上去", st2.get("state") == "cancelled" and not os.path.exists(st["dest"]), st2.get("state"))
    nope = make_zip("x/", "other-tool")
    A.engine_import_chunk("sd", "sd.zip", 0, len(nope), nope)
    A.engine_import_install("sd", "sd.zip")
    st = wait_build("sd")
    case("匯入：包裡沒有 sd-cli 就講清楚", st.get("state") == "failed" and "找不到 sd-cli" in (st.get("error") or ""), st.get("error"))
eq("匯入：取消會清掉半成品", A.engine_import_cancel("sd", "sd.zip")[0], 200)

# ---------------------------------------------------------------- 5. 畫圖參數
png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
eq("參考圖：PNG data URL", A.sd_init_image("data:image/png;base64," + base64.b64encode(png).decode())[1], "png")
eq("參考圖：JPEG", A.sd_init_image(base64.b64encode(b"\xff\xd8\xff\xe0" + b"\0" * 10).decode())[1], "jpg")
eq("參考圖：WebP", A.sd_init_image(base64.b64encode(b"RIFF\0\0\0\0WEBPVP8 ").decode())[1], "webp")
eq("參考圖：沒給", A.sd_init_image(None), (None, None))
for label, raw in (("不是圖", base64.b64encode(b"<svg></svg>").decode()), ("RIFF 但不是 WebP", base64.b64encode(b"RIFF\0\0\0\0WAVEfmt ").decode()),
                   ("不是 base64", "!!!"), ("超過 4 MB", base64.b64encode(png + b"\0" * (4 << 20)).decode())):
    try:
        A.sd_init_image(raw)
        case(f"參考圖：{label} 要擋", False)
    except ValueError:
        case(f"參考圖：{label} 要擋", True)

m_img = {"kind": "image", "parts": [{"role": "model", "path": "a.gguf", "repo": "r"}]}
A._sd_path = lambda root, part: "/m/" + part["path"]
prm = {"prompt": "p", "width": 1024, "height": 1024, "steps": 4, "cfg": 1, "sampler": "euler", "seed": 1, "count": 3,
       "init": "/g/x-init.png", "strength": 0.6}
a = A.sd_argv("sd-cli", m_img, "/m", prm, "/g/x.png", {})
case("畫圖：參考圖帶 -i 與 --strength", a[a.index("-i") + 1] == "/g/x-init.png" and a[a.index("--strength") + 1] == "0.6", a)
eq("畫圖：一次三張帶 -b 3", a[a.index("-b") + 1], "3")
m_vid = {"kind": "video", "parts": m_img["parts"]}
a = A.sd_argv("sd-cli", m_vid, "/m", {**prm, "frames": 33, "fps": 24}, "/g/x.webm", {})
case("影片：第一格畫面帶 -i、不帶 --strength、不帶 -b", "-i" in a and "--strength" not in a and "-b" not in a, a)
a = A.sd_argv("sd-cli", m_img, "/m", {**prm, "count": 1, "init": None}, "/g/x.png", {})
case("畫圖：一張、沒有參考圖就不帶這些參數", "-i" not in a and "-b" not in a, a)
eq("產出檔：一次多張用 outs", A.sd_outs({"out": "ab.png", "outs": ["ab_0.png", "ab_1.png"]}), ["ab_0.png", "ab_1.png"])
eq("產出檔：單張用 out", A.sd_outs({"out": "ab.png"}), ["ab.png"])

# ---- GitHub API：有權杖檔才帶，沒有就不帶
seen = []


class _R(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


real_open = A.urllib.request.urlopen
A.urllib.request.urlopen = lambda req, timeout=30: (seen.append(req.get_header("Authorization")), _R(b"[]"))[1]
try:
    A._gh_json("/repos/x/y/releases")
    tp = os.path.join(A.STATE_DIR, "github-token")
    os.makedirs(A.STATE_DIR, exist_ok=True)
    open(tp, "w").write("ghs_" + "a" * 36 + "\n")
    A._gh_json("/repos/x/y/releases")
    open(tp, "w").write("ghs_abc.def-" + "x" * 300)
    A._gh_json("/repos/x/y/releases")
    open(tp, "w").write("not a token; rm -rf")
    A._gh_json("/repos/x/y/releases")
    os.remove(tp)
finally:
    A.urllib.request.urlopen = real_open
eq("GitHub API：沒有權杖檔就不帶、有就帶（含點號的長權杖也收）、有空白的不帶", seen,
   [None, "Bearer ghs_" + "a" * 36, "Bearer ghs_abc.def-" + "x" * 300, None])


def _limited(req, timeout=30):
    import email.message
    hd = email.message.Message()
    for k, v in (("X-RateLimit-Limit", "60"), ("X-RateLimit-Remaining", "0"), ("X-RateLimit-Reset", "1900000000")):
        hd[k] = v
    raise A.urllib.error.HTTPError(req.full_url, 403, "rate limit exceeded", hd, None)


A.urllib.request.urlopen = _limited
try:
    A._gh_json("/repos/x/y/releases")
    msg = ""
except OSError as ex:
    msg = str(ex)
finally:
    A.urllib.request.urlopen = real_open
case("GitHub 次數用完：講清楚上限、有沒有帶權杖、幾點恢復", "上限 60" in msg and "沒有帶權杖" in msg and "恢復" in msg, msg)
tries = []


def _flaky(req, timeout=30):
    tries.append(1)
    if len(tries) < 3:
        raise A.urllib.error.HTTPError(req.full_url, 504, "Gateway Timeout", {}, None)
    return _R(b"[1]")


A.urllib.request.urlopen, real_sleep = _flaky, A.time.sleep
A.time.sleep = lambda s: None
try:
    got = A._gh_json("/repos/x/y/releases")
finally:
    A.urllib.request.urlopen, A.time.sleep = real_open, real_sleep
case("GitHub 偶爾回 504：等一下再問，第三次拿到就好", got == [1] and len(tries) == 3, (got, tries))

# ---------------------------------------------------------------- 6. 檔頭的逐層陣列
# Nemotron 3.5 的 expert_count／expert_used_count 每層一個值（非 MoE 層是 0）：以前 int(list) 直接當掉
meta = {"general.architecture": "t", "t.expert_count": [0, 64, 64], "t.expert_used_count": [0, 8, 8],
        "t.block_count": 3, "t.attention.head_count": 8, "t.attention.head_count_kv": [2, 0, 2], "t.embedding_length": 1024,
        "_tensors": [("blk.1.ffn_up_exps.weight", 0), ("blk.1.attn_q.weight", 1000)], "_data_start": 0}
wp = A.weight_profile([(meta, 2000)])
eq("逐層陣列：每次動用的專家取 MoE 層的值", wp["expert_used_frac"], 0.125)
case("逐層陣列：檔頭推得出 KV（SSM 層 0 不算）", A.gguf_profile(meta)["kv_full_bpt"] == 4 * 128 * 2 * 2, A.gguf_profile(meta))

eq("不能單獨跑的 GGUF：imatrix、DFlash／MTP／EAGLE 草稿頭認得出來，正常量化不誤判",
   [bool(A.AUX_GGUF_RX.search(x)) for x in ("imatrix_unsloth", "dflash-kquant", "Qwen3.8-27B-MTP-Q8_0", "eagle3-head",
                                             "Muse-Glimmer-30B-UD-Q4_K_XL", "Meagle-7B-Q4_K_M")],
   [True, True, True, True, False, False])

eq("三元量化 TQ1_0／TQ2_0 認得出來", [A.pretty_model(x)[1] for x in ("Kimi-K3-UD-TQ1_0-00001-of-00010.gguf", "GLM-4.7-Flash-UD-TQ2_0.gguf")],
   ["UD-TQ1_0", "UD-TQ2_0"])

# CUDA 綁的 GCC 跟系統預設不同版（Ubuntu 26.04：CUDA 12.4 綁 gcc-13、系統 gcc-15）：整個專案改用 nvcc 那一版編
real_sh, real_shutil = A.sh, A.shutil
DRY = "#$ cicc --c++17 --gnu_version=130400 --display_error_number"
VER = {"c++": "15"}
VTXT = {"c++": "c++ (Ubuntu 15.2.0-4ubuntu4) 15.2.0\nCopyright (C) 2025 Free Software Foundation, Inc.",
        "clang++": "Ubuntu clang version 20.1.8\nTarget: x86_64-pc-linux-gnu"}


class GccShutil:
    have = {"c++", "gcc-13", "g++-13"}

    @classmethod
    def which(cls, x):
        return "/usr/bin/" + x if x in cls.have else None


def fake_sh(argv, timeout=20):
    b = os.path.basename(argv[0])
    if "--dryrun" in argv:
        return 0, "", DRY
    if "--version" in argv:
        return 0, VTXT.get(b, ""), ""
    return 0, VER.get(b, ""), ""


A.shutil, A.sh = GccShutil, fake_sh
eq("CUDA 綁 GCC 13、系統 GCC 15：改用 gcc-13／g++-13", A.cuda_host_gcc("/usr/bin/nvcc"),
   ("/usr/bin/gcc-13", "/usr/bin/g++-13", 13, 15))
VER["c++"] = "13.4.0"
eq("同一版就不動", A.cuda_host_gcc("/usr/bin/nvcc"), None)
VER["c++"] = "15"
GccShutil.have = {"clang++", "gcc-13", "g++-13"}
eq("只有 clang++（沒有 c++）：也改用 gcc-13", A.cuda_host_gcc("/usr/bin/nvcc", "/usr/bin/clang++"),
   ("/usr/bin/gcc-13", "/usr/bin/g++-13", 13, "clang"))
GccShutil.have = {"c++", "clang++"}
eq("缺 gcc-13 套件：回版本讓畫面說要裝什麼", A.cuda_host_gcc("/usr/bin/nvcc"), (None, None, 13, 15))
A.sh = lambda argv, timeout=20: (1, "", "nvcc fatal")
eq("讀不出 nvcc 的 GCC 版本：不動", A.cuda_host_gcc("/usr/bin/nvcc"), None)
A.sh, A.shutil = real_sh, real_shutil

print("全部通過" if OK else "有項目失敗")
sys.exit(0 if OK else 1)
