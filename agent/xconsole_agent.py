#!/usr/bin/env python3
"""xConsole 節點代理 — 云碩機群模型主控台的每機端。

職責分工：硬體指標優先取用同機已在跑的 amd-node-agent（:9300／:9301），
它沒有或掛了才自己用 nvidia-smi／rocm-smi／sysfs 量。這支真正負責的是
node_agent 沒有的那一層：角色欄位與模型、准入控制、啟停、情境、埠口、記錄。

三種記憶體架構分開算（這是全機群版最關鍵的事）：
  unified_split  Strix Halo（.26/.27）：BIOS 切 96 GiB 給 GPU，系統只剩 31 GiB。
                 兩個池子，而且會把整台拖死的是「系統 RAM」不是 VRAM。
  unified_single DGX Spark／Jetson：統一記憶體一個池，GPU 與系統搶同一塊。
  discrete       多張獨立顯卡（MI50／P100／Ada／5090）：每張卡自己的 VRAM ＋ 另外的系統 RAM。

機密：對外一律 Bearer 權杖，存檔案 0600，不走環境變數、不進網址。
"""
import base64
import functools
import glob
import io
import json
import math
import os
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OS = "windows" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
# Windows 以 SYSTEM 帳號開機自啟時沒有一般的家目錄：安裝程式用 XCONSOLE_HOME 指到 C:\ProgramData\xConsole
HOME = (sys.argv[sys.argv.index("--home") + 1] if "--home" in sys.argv[:-1] else None) \
    or os.environ.get("XCONSOLE_HOME") or os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".xconsole")
# Linux 用 systemd --user 管模型服務；macOS、Windows 沒有，改由代理自己帶起來、看著（Supervisor）。
# 測試可以設 XCONSOLE_SUPERVISOR=1，在 Linux 上也走代理自己看管的那條路
SYSTEMD = OS == "linux" and os.environ.get("XCONSOLE_SUPERVISOR") != "1"
EXE = ".exe" if OS == "windows" else ""
REGISTRY = os.path.join(STATE_DIR, "registry.json")
TOKEN_FILE = os.path.join(STATE_DIR, "token")
PORT = int(os.environ.get("XCONSOLE_AGENT_PORT", "9401"))
NODE_AGENTS = ("http://127.0.0.1:9300/metrics", "http://127.0.0.1:9301/metrics")
GIB = 1024 ** 3

# KV 每個 cell 佔的位元組（strata --help 的數字）
KV_BYTES = {"fp16": 2112, "int8": 1056, "k8v4": 816, "q4_0": 528}

# 各記憶體架構的預設保護門檻（GiB）。可被 registry 的 machine 覆寫。
DEFAULT_FLOORS = {
    "unified_split": {"ram": 15.0, "vram": 2.0},
    "unified_single": {"ram": 12.0, "vram": 0.0},
    "discrete": {"ram": 8.0, "vram": 1.0},
}


# ---------------------------------------------------------------- 工具

def sh(argv, timeout=20, env=None):
    """跑一條指令，回 (returncode, stdout, stderr)。失敗不丟例外。
    一律傳參數清單、不經過 shell：服務名稱、路徑、主機名稱這些值不管長怎樣都不會被當成指令解讀
    （以前是組字串＋shlex.quote，漏一個 quote 就是命令注入；CodeQL py/command-line-injection）。"""
    if not isinstance(argv, (list, tuple)):
        raise TypeError("sh() 只收參數清單，不收整條指令字串")
    try:
        p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout, env=env,
                           creationflags=0x08000000 if OS == "windows" else 0)       # Windows：不要每條指令閃一個主控台視窗
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "逾時"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def tail_lines(path, n, cap=8 * 1024 * 1024):
    """檔案最後 n 行（取代 tail -n）。從檔尾往回一塊一塊讀，湊滿 n 行為止；
    最多讀 cap 位元組，幾 GB 的記錄檔也不會整個讀進來。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = pos = fh.tell()
            buf = b""
            while pos > 0 and buf.count(b"\n") <= n and end - pos < cap:
                step = min(65536, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
    except OSError:
        return []
    lines = buf.decode("utf-8", "replace").splitlines()
    if pos > 0 and lines:
        lines = lines[1:]          # 沒讀到檔頭時，第一行可能只讀到後半段
    return lines[-n:]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def ensure_token():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    if os.path.exists(TOKEN_FILE):
        tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


TOKEN = ensure_token()


# ---------------------------------------------------------------- 登錄檔

def default_registry():
    host = socket.gethostname()
    return {
        "machine": {
            "id": host,
            "host": host,
            "name": host,
            "group": "未分類",
            "memory_model": "discrete",
            "note": "",
        },
        "slots": [],
        "profiles": {},
        "measure": {},
    }


# 登錄檔是「讀進來、改一部分、整份寫回」：兩個請求交錯就會有一個人的修改被蓋掉。
# 網頁上改設定的入口與每 15 秒的狀態更新都在這把鎖裡做；狀態更新只寫回它自己量到的實測值。
REG_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        with REG_LOCK:
            return fn(*a, **k)
    return run


def registry():
    reg = load_json(REGISTRY, None)
    if reg is None:
        reg = default_registry()
        save_json(REGISTRY, reg)
    reg.setdefault("measure", {})
    reg.setdefault("profiles", {})
    reg.setdefault("slots", [])
    reg.setdefault("machine", {}).setdefault("memory_model", "discrete")
    return reg


def floors(machine):
    base = dict(DEFAULT_FLOORS.get(machine.get("memory_model"), DEFAULT_FLOORS["discrete"]))
    if machine.get("ram_floor_gib") is not None:
        base["ram"] = float(machine["ram_floor_gib"])
    if machine.get("vram_floor_gib") is not None:
        base["vram"] = float(machine["vram_floor_gib"])
    return base


# ---------------------------------------------------------------- 硬體量測

def expand(p):
    """~ 開頭換成代理的家目錄（Windows 以 SYSTEM 執行時不是 os.path.expanduser 看到的那個）。"""
    p = str(p or "")
    return HOME + p[1:] if p == "~" or p.startswith(("~/", "~\\")) else p


def _meminfo(key):
    """Linux：/proc/meminfo 的一項（位元組）。其他系統走 mem_stats()。"""
    if OS != "linux":
        m = mem_stats()
        return {"MemTotal": m["total"], "MemAvailable": m["avail"], "SwapTotal": m["swap_total"],
                "SwapFree": m["swap_free"]}.get(key, 0)
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) * 1024
    except Exception:  # noqa: BLE001
        pass
    return 0


def _sysctl_n(name):
    """macOS 的 sysctl -n。"""
    rc, out, _ = sh(["sysctl", "-n", name], timeout=5)
    return out.strip() if rc == 0 else ""


def mem_stats():
    """系統記憶體（位元組）：total、avail（還能給新程式用的）、swap_total、swap_free。三個系統各自讀。"""
    if OS == "linux":
        return {"total": _meminfo("MemTotal"), "avail": _meminfo("MemAvailable"),
                "swap_total": _meminfo("SwapTotal"), "swap_free": _meminfo("SwapFree")}
    if OS == "darwin":
        total = int(_sysctl_n("hw.memsize") or 0)
        _, out, _ = sh(["vm_stat"], timeout=5)
        m = re.search(r"page size of (\d+)", out)
        page = int(m.group(1)) if m else 16384
        pages = {k.lower(): int(v) for k, v in re.findall(r"^Pages ([\w ]+):\s+(\d+)\.?$", out, re.M)}
        # 跟「活動監視器」的可用記憶體同一個算法：空閒＋非使用中＋推測快取＋可清除
        avail = sum(pages.get(k, 0) for k in ("free", "inactive", "speculative", "purgeable")) * page
        sw = _sysctl_n("vm.swapusage")

        def mb(key):
            mm = re.search(key + r" = ([\d.]+)([MG])", sw)
            return float(mm.group(1)) * (1024 ** 2 if mm.group(2) == "M" else GIB) if mm else 0
        return {"total": total, "avail": min(avail, total), "swap_total": mb("total"), "swap_free": mb("free")}
    import ctypes

    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                   [(n, ctypes.c_ulonglong) for n in ("tp", "ap", "tf", "af", "tv", "av", "ae")]
    ms = MS()
    ms.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    # 分頁檔總量含實體記憶體，扣掉才是「虛擬記憶體」那一塊
    return {"total": ms.tp, "avail": ms.ap, "swap_total": max(0, ms.tf - ms.tp), "swap_free": max(0, ms.af - ms.ap)}


def cpu_name():
    if OS == "darwin":
        return _sysctl_n("machdep.cpu.brand_string")
    if OS == "windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            return ""
    return next((ln.split(":", 1)[1].strip() for ln in (load_text("/proc/cpuinfo") or "").splitlines()
                 if ln.startswith("model name")), "")


def load_and_uptime():
    """(一分鐘負載, 開機秒數)。Windows 沒有負載平均，回 None。"""
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    up = None
    try:
        if OS == "linux":
            up = float(open("/proc/uptime", encoding="utf-8").read().split()[0])
        elif OS == "darwin":
            m = re.search(r"sec = (\d+)", _sysctl_n("kern.boottime"))
            up = time.time() - int(m.group(1)) if m else None
        else:
            import ctypes
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            up = ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:  # noqa: BLE001
        pass
    return load, up


def disk_free(path):
    """(剩餘, 總量) GiB。os.statvfs Windows 沒有，用 shutil.disk_usage。"""
    du = shutil.disk_usage(path)
    return round(du.free / GIB, 1), round(du.total / GIB, 1)


def ram_pool():
    m = mem_stats()
    return {"total_gib": round(m["total"] / GIB, 1), "free_gib": round(m["avail"] / GIB, 1),
            "used_gib": round((m["total"] - m["avail"]) / GIB, 1)}


def swap_pool():
    m = mem_stats()
    return {"total_gib": round(m["swap_total"] / GIB, 1), "used_gib": round((m["swap_total"] - m["swap_free"]) / GIB, 1)}


def probe_nvidia():
    rc, out, _ = sh(["nvidia-smi", "--query-gpu=index,name,temperature.gpu,power.draw,utilization.gpu,"
                     "memory.total,memory.used", "--format=csv,noheader,nounits"], timeout=15)
    if rc != 0 or not out:
        return None
    gpus = []
    for line in out.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 7:
            continue
        def num(x):
            try:
                return float(x)
            except ValueError:
                return None
        gpus.append({
            "id": f[0], "name": f[1], "temp": num(f[2]), "power": num(f[3]),
            "use": num(f[4]),
            "vram_total": (num(f[5]) or 0) * 1024 * 1024,
            "vram_used": (num(f[6]) or 0) * 1024 * 1024,
        })
    return gpus or None


def probe_amd_sysfs():
    """ROCm 的 rocm-smi 偏慢，直接讀 sysfs 便宜很多。"""
    gpus = []
    base = "/sys/class/drm"
    if not os.path.isdir(base):
        return None
    for card in sorted(os.listdir(base)):
        if not re.fullmatch(r"card\d+", card):
            continue
        dev = os.path.join(base, card, "device")
        vt = os.path.join(dev, "mem_info_vram_total")
        vu = os.path.join(dev, "mem_info_vram_used")
        if not (os.path.exists(vt) and os.path.exists(vu)):
            continue
        def rd(p, cast=int, default=None):
            try:
                return cast(open(p, encoding="utf-8").read().strip())
            except Exception:  # noqa: BLE001
                return default
        temp = power = use = None
        hw = os.path.join(dev, "hwmon")
        if os.path.isdir(hw):
            for h in os.listdir(hw):
                t = rd(os.path.join(hw, h, "temp1_input"), int)
                if t:
                    temp = round(t / 1000.0, 1)
                p = rd(os.path.join(hw, h, "power1_average"), int)
                if p:
                    power = round(p / 1_000_000.0, 1)
        use = rd(os.path.join(dev, "gpu_busy_percent"), float)
        gpus.append({
            "id": card.replace("card", ""), "name": "AMD GPU", "temp": temp,
            "power": power, "use": use,
            "vram_total": rd(vt, int, 0), "vram_used": rd(vu, int, 0),
        })
    return gpus or None


def apple_gpu_limit(total):
    """Apple Silicon 的 GPU 最多能用多少統一記憶體（Metal 的 recommendedMaxWorkingSetSize）。
    管理者用 sysctl iogpu.wired_limit_mb 調過就照那個；沒調過是系統預設：36 GB 以下約 2/3，以上約 3/4。"""
    lim = int(_sysctl_n("iogpu.wired_limit_mb") or 0)
    if lim:
        return lim * 1024 * 1024
    return int(total * (0.75 if total > 36 * GIB else 2 / 3))


def probe_apple():
    """Apple Silicon：一顆晶片、一個統一記憶體池。GPU 已用量讀 IOAccelerator 的統計（ioreg）。"""
    if OS != "darwin" or platform_machine() != "arm64":
        return None
    total = int(_sysctl_n("hw.memsize") or 0)
    _, out, _ = sh(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], timeout=10)
    used = re.search(r'"In use system memory"=(\d+)', out)
    use = re.search(r'"Device Utilization %"=(\d+)', out)
    return [{"id": "0", "name": _sysctl_n("machdep.cpu.brand_string") + " GPU", "temp": None, "power": None,
             "use": float(use.group(1)) if use else None,
             "vram_total": apple_gpu_limit(total), "vram_used": int(used.group(1)) if used else 0}]


def platform_machine():
    import platform
    return platform.machine().lower()


GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def probe_win_adapters():
    """Windows 的 AMD／Intel 顯示卡（NVIDIA 交給 nvidia-smi）：名稱與專用記憶體讀登錄檔，
    已用量讀效能計數器 GPU Adapter Memory（typeperf，Windows 內建）。AI 395 在 BIOS 切給 GPU 的那塊就是專用記憶體。"""
    if OS != "windows":
        return None
    import winreg
    cards = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, GPU_CLASS) as cls:
            for i in range(64):
                try:
                    sub = winreg.EnumKey(cls, i)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(cls, sub) as k:
                        name = str(winreg.QueryValueEx(k, "DriverDesc")[0])
                        try:
                            mem = int(winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0])
                        except OSError:
                            raw = winreg.QueryValueEx(k, "HardwareInformation.MemorySize")[0]
                            mem = int.from_bytes(raw, "little") if isinstance(raw, bytes) else int(raw)
                except OSError:
                    continue
                if re.search(r"AMD|Radeon|Intel|Arc", name, re.I) and mem:
                    cards.append({"id": str(len(cards)), "name": name, "temp": None, "power": None, "use": None,
                                  "vram_total": mem, "vram_used": 0})
    except OSError:
        return None
    if cards:
        _, out, _ = sh(["typeperf", r"\GPU Adapter Memory(*)\Dedicated Usage", "-sc", "1"], timeout=15)
        rows = [ln for ln in out.splitlines() if ln.startswith('"') and "/" in ln[:15]]
        if rows:
            vals = [float(x.strip('"')) for x in rows[-1].split(",")[1:] if re.fullmatch(r'"[\d.]+"', x.strip())]
            cards[0]["vram_used"] = int(max(vals)) if vals else 0     # 計數器以 LUID 命名對不回登錄檔，取最大那張
    return cards or None


def probe_gpus():
    return probe_nvidia() or probe_amd_sysfs() or probe_apple() or probe_win_adapters() or []


def has_nvidia():
    """這台真的有 NVIDIA 卡：nvidia-smi 要回報得出卡。只看裝了 nvidia-smi 不準——
    .26（AMD AI 395）裝了 nvidia-smi 卻沒有 NVIDIA 驅動，以前因此被當成 NVIDIA 機器（綁卡、編譯都會走錯路）。"""
    return bool(probe_nvidia())


# ---- 多卡：欄位綁哪幾張卡
# 卡號一律用 probe_gpus() 的 id：NVIDIA 是 nvidia-smi 的 index，AMD 是 /sys/class/drm/cardN 的 N。
# 啟動時要換成引擎看得懂的序號，對不上就拒絕啟動——綁錯卡比不綁更糟（會把別的欄位的卡吃爆）。
KFD_NODES = "/sys/class/kfd/kfd/topology/nodes"
DRM_DIR = "/sys/class/drm"


def amd_hip_order(drm=DRM_DIR, kfd=KFD_NODES):
    """drm 的 cardN → HIP 裝置序號。HIP 依 KFD 拓撲節點順序編號（跳過 CPU 節點），兩邊用 render minor 對起來。"""
    minors = []
    try:
        nodes = sorted((n for n in os.listdir(kfd) if n.isdigit()), key=int)
    except OSError:
        return {}
    for n in nodes:
        props = load_text(os.path.join(kfd, n, "properties")) or ""
        mi = re.search(r"^drm_render_minor (\d+)", props, re.M)
        simd = re.search(r"^simd_count (\d+)", props, re.M)
        if mi and simd and int(simd.group(1)) > 0 and int(mi.group(1)) > 0:
            minors.append(int(mi.group(1)))
    order = {}
    for card in os.listdir(drm) if os.path.isdir(drm) else []:
        if not re.fullmatch(r"card\d+", card):
            continue
        rd = glob.glob(os.path.join(drm, card, "device", "drm", "renderD*"))
        if rd:
            minor = int(os.path.basename(rd[0])[len("renderD"):])
            if minor in minors:
                order[card[4:]] = minors.index(minor)
    return order


def visible_env(gpu_ids, nvidia=None):
    """綁卡的環境變數。沒綁（None 或空）就不設，引擎看得到全部卡。"""
    ids = [str(x) for x in gpu_ids or []]
    if not ids:
        return {}
    if nvidia is None:
        nvidia = has_nvidia()
    if nvidia:
        # CUDA 預設把最快的卡排第一，跟 nvidia-smi 的編號不同；照 PCI 匯流排排才對得上
        return {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": ",".join(ids)}
    order = amd_hip_order()
    miss = [i for i in ids if i not in order]
    if miss:
        raise ValueError(f"對不到這些卡的 HIP 序號：{', '.join(miss)}")
    return {"HIP_VISIBLE_DEVICES": ",".join(str(order[i]) for i in ids)}


def pick_gpus(want, cards):
    """檢查要綁的卡。回 (卡號清單或 None, 錯誤)。None＝自動（全部）；全選也當成自動，登錄檔不留多餘的綁定。"""
    if want is None:
        return None, None
    if not isinstance(want, list):
        return None, "gpus 要是卡號清單"
    have = [str(c["id"]) for c in cards]
    ids = list(dict.fromkeys(str(x) for x in want))
    bad = [i for i in ids if i not in have]
    if bad:
        return None, f"這台沒有這些卡：{', '.join(bad)}（偵測到 {', '.join(have) or '沒有卡'}）"
    if not ids or sorted(ids) == sorted(have):
        return None, None
    return sorted(ids, key=have.index), None


def card_split(gib, gpu_ids, cards):
    """把一個欄位的顯存用量攤到它用的每張卡：綁卡的照綁的卡，沒綁的攤到全部卡；照卡的大小比例分（llama.cpp 預設也是這樣分層）。"""
    ids = {str(x) for x in gpu_ids or []}
    sel = [c for c in cards if not ids or str(c["id"]) in ids]
    tot = sum(c["total_gib"] for c in sel) or 1
    return {str(c["id"]): float(gib or 0) * c["total_gib"] / tot for c in sel}


# 連本機（模型服務、node_agent）一律不走代理：macOS 的 urllib 會讀系統的代理設定，連 127.0.0.1 也送去代理，
# 結果健康檢查永遠不通（GitHub 的 macOS 主機就是這樣）。
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def hardware():
    """先用同機已在跑的 node_agent，拿不到才自己量。拿不到就老實標未知，不假裝零。"""
    for url in NODE_AGENTS:
        try:
            with LOCAL.open(url, timeout=4) as resp:
                data = json.load(resp)
            if data.get("gpu") or data.get("cpu"):
                data["_source"] = url
                return data
        except Exception:  # noqa: BLE001
            continue
    gpus = probe_gpus()
    load, uptime = load_and_uptime()
    total_t = sum(g.get("vram_total") or 0 for g in gpus)
    total_u = sum(g.get("vram_used") or 0 for g in gpus)
    return {
        "_source": "self",
        "gpu": {
            "count": len(gpus), "gpus": gpus,
            "vram_total": total_t, "vram_used": total_u,
            "temp": max((g.get("temp") or 0) for g in gpus) if gpus else None,
            "power": sum(g.get("power") or 0 for g in gpus) if gpus else None,
            "use": max((g.get("use") or 0) for g in gpus) if gpus else None,
        },
        "cpu": {"load": load, "cores": os.cpu_count()},
        "ram": {"total": mem_stats()["total"]},
        "uptime": uptime,
    }


# 每產生一個 token 都要把用到的權重從記憶體讀一遍，所以速度上限看記憶體頻寬。數字是各家規格書的標稱值（GB/s）；
# 依序比對，越具體的放越前面。比不到就是未知，管理者可在「這台的設定」自己填。
BANDWIDTH_GBS = (
    ("Radeon 8060S", 256), ("Radeon 8050S", 256), ("GB10", 273), ("AGX Thor", 273), ("Thor", 273),
    ("AGX Orin", 204.8), ("Orin NX", 102.4), ("Orin Nano", 68),
    ("B300", 8000), ("B200", 8000), ("H200", 4800), ("H100 PCIe", 2000), ("H100", 3350),
    ("A100 80GB", 2039), ("A100", 1555), ("A30", 933), ("L40S", 864), ("L40", 864), ("L4", 300),
    ("RTX PRO 6000", 1792), ("RTX 6000 Ada", 960), ("RTX 5090", 1792), ("RTX 5080", 960),
    ("RTX 4090", 1008), ("RTX 4080", 717), ("RTX 3090", 936), ("RTX A6000", 768),
    ("P100", 732), ("P40", 347), ("V100", 900), ("T4", 320), ("GTX 1650", 128),
    ("MI300X", 5300), ("MI250", 3277), ("MI210", 1638), ("MI100", 1229), ("MI50", 1024), ("MI60", 1024),
    ("RX 7900 XTX", 960),
)
# AMD 獨立卡在 sysfs 只認得出「AMD GPU」：用 KFD 的 gfx 版本反查型號
AMD_GFX = {"90006": "AMD Instinct MI50／MI60", "90008": "AMD Instinct MI100", "90010": "AMD Instinct MI210／MI250",
           "90402": "AMD Instinct MI300X", "110000": "AMD Radeon RX 7900 XTX", "110501": "AMD Radeon 8060S"}


# Apple Silicon：同一型號依 GPU 核心數有兩種頻寬（例 M4 Max 32 核 410、40 核 546 GB/s）。(型號, GPU 核心數下限, GB/s)，越具體越前面
APPLE_BW = (("M4 Max", 40, 546), ("M4 Max", 0, 410), ("M3 Max", 40, 400), ("M3 Max", 0, 300), ("M3 Ultra", 0, 819),
            ("M2 Ultra", 0, 800), ("M1 Ultra", 0, 800), ("M2 Max", 0, 400), ("M1 Max", 0, 400), ("M4 Pro", 0, 273),
            ("M3 Pro", 0, 150), ("M2 Pro", 0, 200), ("M1 Pro", 0, 200), ("M5", 0, 153), ("M4", 0, 120),
            ("M3", 0, 100), ("M2", 0, 100), ("M1", 0, 68))


def apple_bandwidth(cpu, cores=None):
    if cores is None:
        _, out, _ = sh(["system_profiler", "SPDisplaysDataType"], timeout=20)
        m = re.search(r"Total Number of Cores:\s*(\d+)", out)
        cores = int(m.group(1)) if m else 0
    # 「M5」不能吃到「M5 Max」：沒列在表上的高階型號回未知，請管理者自己填，不拿基本款的數字充數
    tail = r"(?! (?:Pro|Max|Ultra)\b)"
    return next((bw for name, need, bw in APPLE_BW
                 if re.search(rf"\b{name}\b" + ("" if " " in name else tail), cpu) and cores >= need), None)


def spec_bandwidth(hay):
    """名稱 → 規格書頻寬。名稱是「MI210／MI250」這種認不出是哪一張的（同一個 gfx 版本），兩種頻寬不同就回未知，
    不拿第一個比到的充數（MI250 是 MI210 的兩倍，速度估算會整個翻倍）。"""
    def one(h):
        return next((v for k, v in BANDWIDTH_GBS if k.lower() in h.lower()), None)
    vals = {one(a) for a in hay.split("／")}
    return vals.pop() if len(vals) == 1 else None


@functools.lru_cache(maxsize=1)
def _chip_probe():
    cpu = cpu_name()
    board = (load_text("/proc/device-tree/model") or "").replace("\x00", "").strip() if OS == "linux" else ""
    gpus = probe_gpus()
    names = [g["name"] for g in gpus if g.get("name") and g["name"] != "AMD GPU"]
    if not names and gpus:
        for props in glob.glob(os.path.join(KFD_NODES, "*", "properties")):
            m = re.search(r"gfx_target_version (\d+)", load_text(props) or "")
            if m and m.group(1) in AMD_GFX:
                names.append(AMD_GFX[m.group(1)])
                break
    if "Radeon" in cpu and (not names or names[0].startswith("AMD Radeon 8")):
        names = [cpu.split("w/", 1)[-1].strip()] if "w/" in cpu else names       # APU：顯示晶片寫在 CPU 型號裡
    gpu = names[0] if names else (board if "Jetson" in board else None)
    hay = " ".join(x for x in (gpu or "", board) if x)
    bw = apple_bandwidth(cpu) if OS == "darwin" else spec_bandwidth(hay)
    sug, why = suggest_memory_model(gpus)
    return {"cpu": cpu or None, "cores": os.cpu_count(), "board": board or None, "gpu": gpu,
            "gpu_count": len(gpus), "bandwidth_spec_gbs": bw, "suggest": sug, "suggest_why": why}


def chip(machine=None):
    """這台是什麼晶片、記憶體頻寬多少（推薦清單估速度用）。管理者在網頁上填的值優先於規格表。"""
    machine = machine if machine is not None else registry()["machine"]
    c = dict(_chip_probe())
    own = machine.get("bandwidth_gbs")
    c["bandwidth_gbs"] = own or c["bandwidth_spec_gbs"]
    c["bandwidth_src"] = "管理者設定" if own else ("規格書標稱值" if c["bandwidth_spec_gbs"] else None)
    c["ram_bandwidth_gbs"] = machine.get("ram_bandwidth_gbs")
    return c


def temps():
    """主機板／硬碟／網卡這類溫度，從 hwmon 掃；掃不到就不列，不要編。"""
    out, base = [], "/sys/class/hwmon"
    if not os.path.isdir(base):
        return out
    for entry in sorted(os.listdir(base)):
        d = os.path.join(base, entry)
        try:
            name = open(os.path.join(d, "name"), encoding="utf-8").read().strip()
        except Exception:  # noqa: BLE001
            continue
        for f in sorted(os.listdir(d)):
            if not re.fullmatch(r"temp\d+_input", f):
                continue
            try:
                val = int(open(os.path.join(d, f), encoding="utf-8").read().strip()) / 1000.0
            except Exception:  # noqa: BLE001
                continue
            if not 0 < val < 150:
                continue
            label = ""
            lf = os.path.join(d, f.replace("_input", "_label"))
            if os.path.exists(lf):
                try:
                    label = open(lf, encoding="utf-8").read().strip()
                except Exception:  # noqa: BLE001
                    label = ""
            if not label or re.fullmatch(r"temp\d+_input", label):
                label = name          # temp1_input 這種對人沒意義，改用感測器名稱
            out.append({"source": name, "label": label, "celsius": round(val, 1)})
    return out


def gpu_clock():
    base = "/sys/class/drm"
    if os.path.isdir(base):
        for card in sorted(os.listdir(base)):
            p = f"{base}/{card}/device/pp_dpm_sclk"
            if not os.path.exists(p):
                continue
            try:
                lines = open(p, encoding="utf-8").read().strip().splitlines()
            except Exception:  # noqa: BLE001
                continue
            cur = mx = None
            for line in lines:
                m = re.search(r"(\d+)Mhz", line, re.I)
                if not m:
                    continue
                mhz = int(m.group(1))
                mx = mhz if mx is None else max(mx, mhz)
                if line.strip().endswith("*"):
                    cur = mhz
            if mx:
                return {"current_mhz": cur, "max_mhz": mx}
    rc, out, _ = sh(["nvidia-smi", "--query-gpu=clocks.sm,clocks.max.sm", "--format=csv,noheader,nounits"], timeout=10)
    if rc == 0 and out:
        f = [x.strip() for x in out.splitlines()[0].split(",")]
        try:
            return {"current_mhz": int(f[0]), "max_mhz": int(f[1])}
        except Exception:  # noqa: BLE001
            pass
    return None


# ---------------------------------------------------------------- 埠口

# 拿不到行程名時（例 root 的 sshd，非 root 看不到）用埠號認
WELL_KNOWN = {
    22: ("系統", "SSH"), 53: ("系統", "DNS 解析"), 631: ("系統", "印表機服務"),
    5353: ("系統", "mDNS"), 5939: ("系統", "TeamViewer"), 3389: ("系統", "遠端桌面"),
    9300: ("主控台", "機群監控代理"), 9301: ("主控台", "機群監控代理"),
    9400: ("主控台", "xConsole 主控台"), 9401: ("主控台", "xConsole 代理"),
}

PORT_KIND = [
    (re.compile(r"sshd"), "系統", "SSH"),
    (re.compile(r"systemd-resolve"), "系統", "DNS 解析"),
    (re.compile(r"cupsd?"), "系統", "印表機服務"),
    (re.compile(r"tailscale"), "系統", "Tailscale"),
    (re.compile(r"dockerd|containerd"), "系統", "容器執行階段"),
    (re.compile(r"xconsole_agent"), "主控台", "xConsole 代理"),
    (re.compile(r"xconsole_server"), "主控台", "xConsole 主控台"),
    (re.compile(r"node_agent"), "主控台", "機群監控代理"),
    (re.compile(r"llama-server|strata|vllm|ollama"), "模型", "推論服務"),
]


def _listen_lines():
    """各系統列出正在聽的 TCP 埠，換成跟 Linux ss 一樣的一行一筆：「 位址:埠 … users:((\"行程\"」。"""
    if OS == "linux":
        rc, out, _ = sh(["ss", "-tlnpH"])
        return out if rc == 0 else ""
    rows = []
    if OS == "darwin":
        _, out, _ = sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fcn"], timeout=15)
        proc = ""
        for ln in out.splitlines():
            if ln.startswith("c"):
                proc = ln[1:]
            elif ln.startswith("n") and ":" in ln:
                addr, port = ln[1:].rsplit(":", 1)
                rows.append(f' {addr.replace("*", "0.0.0.0")}:{port} x users:(("{proc}"')
    else:
        _, out, _ = sh(["netstat", "-ano", "-p", "TCP"], timeout=15)
        for ln in out.splitlines():
            f = ln.split()
            if len(f) >= 5 and f[3] == "LISTENING":
                addr, port = f[1].rsplit(":", 1)
                rows.append(f" {addr}:{port} x pid={f[4]}")
    return "\n".join(rows)


def ports():
    out = _listen_lines()
    if not out:
        return []
    reg = registry()
    by_port = {}
    for sl in reg["slots"]:
        pno = sl.get("port")
        if not pno:
            c = slot_config(sl)          # strata 的埠寫在設定檔裡，不在欄位上
            pno = (c or {}).get("port")
        if pno:
            by_port[int(pno)] = sl
    seen = {}
    for line in out.splitlines():
        m = re.search(r"\s(\S+):(\d+)\s+\S+\s+users:\(\(\"([^\"]+)\"", line)
        if m:
            addr, pno, proc = m.group(1), int(m.group(2)), m.group(3)
        else:
            m2 = re.search(r"\s(\S+):(\d+)\s", line)
            if not m2:
                continue
            addr, pno, proc = m2.group(1), int(m2.group(2)), ""
        external = addr not in ("127.0.0.1", "::1", "[::1]")
        if pno in seen:
            seen[pno]["external"] = seen[pno]["external"] or external
            continue
        kind, label = "其他", proc or f"埠 {pno}"
        if pno in by_port:
            kind = "模型"
            label = by_port[pno].get("model") or by_port[pno]["role"]
        else:
            cmdline = ""
            mm = re.search(r"pid=(\d+)", line)
            if mm and OS == "linux":
                try:
                    with open(f"/proc/{mm.group(1)}/cmdline", "rb") as fh:
                        cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    cmdline = ""
            hit = False
            for rx, k, lb in PORT_KIND:
                if (proc and rx.search(proc)) or (cmdline and rx.search(cmdline)):
                    kind, label, hit = k, lb, True
                    break
            if not hit and pno in WELL_KNOWN:
                kind, label = WELL_KNOWN[pno]
        seen[pno] = {"port": pno, "process": proc, "kind": kind,
                     "label": label, "external": external}
    return sorted(seen.values(), key=lambda r: r["port"])


# ---------------------------------------------------------------- 欄位與模型

ACTIVE_LABEL = {
    "active": "使用中", "activating": "啟動中", "deactivating": "停止中",
    "inactive": "未啟動", "failed": "失敗",
}


UNIT_NAME_RX = re.compile(r"[A-Za-z0-9][\w@.:-]{0,120}\.(?:service|socket|target|timer)")


def unit_name(unit):
    """服務名稱進 systemctl／journalctl 之前一律比對格式（另外參數前也加 --）：不經過 shell 之後，
    剩下的風險只有「名稱被當成選項」，這裡兩道都擋。"""
    unit = str(unit or "")
    if not UNIT_NAME_RX.fullmatch(unit):
        raise ValueError(f"服務名稱格式不對：{unit[:60]}")
    return unit


# ---------------------------------------------------------------- 沒有 systemd 的系統：代理自己看管模型服務

UNITS_FILE = os.path.join(STATE_DIR, "units.json")


def proc_image(pid):
    """這個 PID 的程式名稱（不含路徑、小寫）；不存在回空字串。"""
    if not pid:
        return ""
    if OS == "windows":
        _, out, _ = sh(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], timeout=10)
        return out.split(",")[0].strip('"').lower() if out.startswith('"') else ""
    try:
        os.kill(int(pid), 0)
    except OSError:
        return ""
    _, comm, _ = sh(["ps", "-o", "comm=", "-p", str(int(pid))], timeout=10)
    # 去掉結尾的版本號：python3.15 帶起來不久會變成 python（macOS 的 Python.app），要當成同一支
    return re.sub(r"[\d.]+$", "", os.path.basename(comm.strip()).lower()) or "?"


def pid_alive(pid, image=None):
    """這個 PID 還在、而且還是當初帶起來的那支程式（PID 會被重複使用，只看活著會把別的程式當成模型服務）。
    image 是帶起來當下記下的程式名稱，不用設定檔裡的路徑比：包裝過的程式（例 macOS 的 Python.app）名稱跟路徑對不上。"""
    now = proc_image(pid)
    return bool(now) and (not image or now == image)


def proc_rss_gib(pid):
    if not pid:
        return 0.0
    if OS == "windows":
        _, out, _ = sh(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], timeout=10)
        m = re.search(r'"([\d,.]+) K"', out)
        return round(int(re.sub(r"[,.]", "", m.group(1))) * 1024 / GIB, 2) if m else 0.0
    _, out, _ = sh(["ps", "-o", "rss=", "-p", str(int(pid))], timeout=10)
    return round(int(out.strip()) * 1024 / GIB, 2) if out.strip().isdigit() else 0.0


def kill_tree(pid):
    if OS == "windows":
        sh(["taskkill", "/PID", str(int(pid)), "/T", "/F"], timeout=30)
        return
    try:
        os.killpg(int(pid), 15)                # 用自己的行程群組帶起來的，連子行程一起收
    except OSError:
        try:
            os.kill(int(pid), 15)
        except OSError:
            pass


class Supervisor:
    """macOS／Windows 沒有 systemd --user：模型服務（llama-server）由代理自己帶起來、看著。
    行為照 systemd 那份服務檔：Restart=on-failure、RestartSec=10、一分鐘內掛五次就停（失敗）、enable＝代理開機時帶起來。
    狀態（定義、啟用、PID）存在 units.json；代理重開時照 PID 認回還在跑的服務，不重複帶起第二份去搶同一個埠。
    代理本身由 launchd（macOS）或工作排程器（Windows）開機自啟。"""
    RESTART_SEC, BURST, WINDOW = 10, 5, 60

    def __init__(self):
        self.lock = threading.RLock()
        self.procs = {}           # unit → Popen（這次帶起來的）
        self.st = {}              # unit → {"want", "failed", "restart_at", "fails"}
        self.thread = None
        self.running = True

    def _meta(self):
        return load_json(UNITS_FILE, {}) or {}

    def _put(self, unit, **kw):
        m = self._meta()
        m.setdefault(unit, {}).update(kw)
        save_json(UNITS_FILE, m)

    def define(self, unit, cfg, log):
        with self.lock:
            self._put(unit, cfg=cfg, log=log)

    def _pid(self, unit):
        p = self.procs.get(unit)
        if p is not None:
            return p.pid if p.poll() is None else None
        m = self._meta().get(unit) or {}
        return m.get("pid") if pid_alive(m.get("pid"), m.get("image")) else None

    def _spawn(self, unit):
        m = self._meta().get(unit)
        if not m:
            raise ValueError(f"沒有這個服務：{unit}")
        raw = load_json(m["cfg"], None)
        if not raw:
            raise ValueError(f"讀不到 {m['cfg']}")
        reg = registry()
        argv = llama_argv(raw, reg)
        env = llama_launch_env(raw["exe"], reg, raw.get("gpus"))
        kw = ({"creationflags": 0x08000000 | 0x00000200} if OS == "windows"     # 不開視窗＋自己一個行程群組
              else {"start_new_session": True})
        with open(m["log"], "ab") as log:
            p = subprocess.Popen([str(a) for a in argv], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 env=env, cwd=os.path.dirname(raw["exe"]) or None, **kw)
        self.procs[unit] = p
        self._put(unit, pid=p.pid, image=proc_image(p.pid) or None)
        self.st.setdefault(unit, {}).update(want=True, failed=False, restart_at=None)

    def _stop(self, unit):
        pid = self._pid(unit)
        p = self.procs.pop(unit, None)
        if pid:
            kill_tree(pid)
            for _ in range(40):
                if (p.poll() is not None) if p else not pid_alive(pid):
                    break
                time.sleep(0.5)
            else:
                if OS != "windows":
                    try:
                        os.killpg(int(pid), 9)
                    except OSError:
                        pass
        self._put(unit, pid=None)

    def state(self, unit):
        with self.lock:
            if self._pid(unit):
                return "active"
            st = self.st.get(unit) or {}
            if st.get("restart_at"):
                return "activating"
            return "failed" if st.get("failed") else "inactive"

    def ctl(self, unit, verb):
        v = verb.split()
        with self.lock:
            self._ensure_thread()
            st = self.st.setdefault(unit, {})
            try:
                if v[0] == "start":
                    if not self._pid(unit):
                        self._spawn(unit)
                elif v[0] == "stop":
                    st.update(want=False, restart_at=None)
                    self._stop(unit)
                elif v[0] == "restart":
                    st.update(restart_at=None)
                    self._stop(unit)
                    self._spawn(unit)
                elif v[0] in ("enable", "disable"):
                    self._put(unit, enabled=v[0] == "enable")
                elif v[0] == "is-enabled":
                    on = bool((self._meta().get(unit) or {}).get("enabled"))
                    return (0 if on else 1), "enabled" if on else "disabled", ""
                elif v[0] == "reset-failed":
                    st.update(failed=False, fails=[])
                elif v[0] == "show":
                    prop = v[v.index("-p") + 1] if "-p" in v else ""
                    if prop == "MainPID":
                        return 0, str(self._pid(unit) or 0), ""
                    if prop == "SubState":
                        s_ = self.state(unit)
                        return 0, {"active": "running", "activating": "auto-restart", "failed": "failed"}.get(s_, "dead"), ""
                    return 0, "", ""
            except (OSError, ValueError) as ex:
                st.update(failed=True, want=False)
                return 1, "", str(ex)
        return 0, "", ""

    def _ensure_thread(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._watch, daemon=True)
            self.thread.start()

    def _crashed(self, unit, st, now):
        st["fails"] = [t for t in st.get("fails", []) if now - t < self.WINDOW] + [now]
        if len(st["fails"]) >= self.BURST:
            st.update(failed=True, want=False, restart_at=None)
        else:
            st["restart_at"] = now + self.RESTART_SEC

    def _watch(self):
        while self.running:
            time.sleep(3)
            now = time.time()
            with self.lock:
                for unit, p in list(self.procs.items()):
                    st = self.st.setdefault(unit, {})
                    if p.poll() is None:
                        # 帶起來之後才換掉自己的程式（例 macOS 的 python 轉成 Python.app）會讓名稱變掉：
                        # 跟著更新，代理重開時才認得回來
                        img = proc_image(p.pid)
                        if img and img != (self._meta().get(unit) or {}).get("image"):
                            self._put(unit, image=img)
                        continue
                    if not st.get("want"):
                        continue
                    del self.procs[unit]
                    self._put(unit, pid=None)
                    if p.returncode == 0:
                        st["want"] = False                     # Restart=on-failure：正常結束不重開
                        continue
                    self._crashed(unit, st, now)
                # 代理重開後認回來的（不是這次帶起來的，拿不到結束碼）：不見了就當成異常結束
                for unit, m in self._meta().items():
                    st = self.st.get(unit) or {}
                    if unit in self.procs or not m.get("pid") or not st.get("want"):
                        continue
                    if not pid_alive(m["pid"], m.get("image")):
                        self._put(unit, pid=None)
                        self._crashed(unit, st, now)
                for unit, st in self.st.items():
                    if st.get("restart_at") and now >= st["restart_at"]:
                        st["restart_at"] = None
                        try:
                            self._spawn(unit)
                        except (OSError, ValueError):
                            st.update(failed=True, want=False)

    def boot(self):
        """代理啟動時：還在跑的認回來，該開機自啟但沒在跑的帶起來。"""
        with self.lock:
            self._ensure_thread()
            for unit, m in self._meta().items():
                if pid_alive(m.get("pid"), m.get("image")):
                    self.st.setdefault(unit, {})["want"] = True  # 代理重開，模型服務還在：不動它，但要接著看管
                    continue
                self._put(unit, pid=None)
                if m.get("enabled"):
                    try:
                        self._spawn(unit)
                    except (OSError, ValueError):
                        self.st.setdefault(unit, {}).update(failed=True)

    def log_path(self, unit):
        return (self._meta().get(unit) or {}).get("log")


SUP = Supervisor()


def unit_state(unit):
    """回 systemd 的真實狀態字串，不要壓成開／關兩種。

    卡在 activating 的服務（Type=notify 但程式沒送就緒訊號，或 ExecStartPre 很久）
    壓成「關」會讓人以為它沒跑、壓成「開」又會以為它好了，兩種都會誤導。
    """
    try:
        unit = unit_name(unit)
    except ValueError:
        return "unknown"          # 登錄檔裡的名字不合格式：當成查不到，不要讓整個狀態頁出錯
    if not SYSTEMD:
        return SUP.state(unit)
    _, out, _ = sh(["systemctl", "--user", "is-active", "--", unit])
    st = out.strip()
    if st in ("unknown", "", "inactive"):
        _, out2, _ = sh(["systemctl", "is-active", "--", unit])
        if out2.strip() not in ("unknown", ""):
            st = out2.strip()
    return st or "unknown"


def unit_active(unit):
    return unit_state(unit) == "active"


def unit_scope(unit):
    """這個 unit 是 user 層還是 system 層。兩層指令不一樣，猜錯就全部失敗。"""
    if not SYSTEMD:
        return "user"
    try:
        rc, _, _ = sh(["systemctl", "--user", "cat", "--", unit_name(unit)])
    except ValueError:
        return "user"
    return "user" if rc == 0 else "system"


def sysctl(unit, verb, timeout=200):
    if not SYSTEMD:
        try:
            return SUP.ctl(unit_name(unit), verb)
        except ValueError as ex:
            return 1, "", str(ex)
    scope = unit_scope(unit)
    flag = "--user " if scope == "user" else ""
    try:
        return sh(["systemctl", *flag.split(), *verb.split(), "--", unit_name(unit)], timeout=timeout)
    except ValueError as ex:
        return 1, "", str(ex)


def unit_enabled(unit):
    _, out, _ = sysctl(unit, "is-enabled", timeout=20)
    return out.strip() in ("enabled", "enabled-runtime")


def unit_rss_gib(unit):
    """unit 底下所有進程的 RSS 合計。cgroup 的 memory.current 含 page cache 會高估，不用它。"""
    _, pid, _ = sysctl(unit, "show -p MainPID --value", timeout=20)
    pid = pid.strip()
    if not pid.isdigit() or pid == "0":
        return 0.0
    if not SYSTEMD:
        return proc_rss_gib(int(pid))
    _, pgid, _ = sh(["ps", "-o", "pgid=", "-p", pid])
    pgid = pgid.strip()
    if not pgid.isdigit():
        return 0.0
    _, out, _ = sh(["ps", "-o", "rss=", "-g", pgid])
    total = sum(int(x) for x in out.split() if x.isdigit())
    return round(total * 1024 / GIB, 2)


def strata_config(path):
    cfg = load_json(path, None)
    if not cfg:
        return None
    args = cfg.get("args") or []

    def val(flag, default=None):
        try:
            return args[args.index(flag) + 1]
        except (ValueError, IndexError):
            return default

    def num(x, cast=int, default=0):
        try:
            return cast(x)
        except (TypeError, ValueError):
            return default

    return {
        "max_context": num(val("--max-context", 4096), int, 4096),
        "kv": val("--kv", "fp16"),
        "kv_grow": "--kv-grow" in args,
        "parallel": num(cfg.get("parallel", 1), int, 1),
        "expert_cache": val("--expert-cache"),
        "resident_budget_gib": num(val("--resident-budget-gib", 0), float, 0.0),
        "fit_max_tokens": bool(cfg.get("fit_max_tokens")),
        "port": cfg.get("port"),
        "model_name": cfg.get("model_name"),
        "exe": cfg.get("exe"),
        "kv_bytes_per_cell": None,
        "_native": val("--native"),
        "_pack": val("--pack"),
    }


def kv_worst_gib(cfg):
    """最壞情況 KV 會長到多大 = 每 token 位元組 × 上下文 × 並行。

    ⚠️ 每 token 要幾位元組「因模型而異，差好幾個數量級」，不要套同一個數字：
    Qwen3.8-Flash-Next 這種 GDN／線性混合注意力，int8 下只要 1,056 B/token，
    128K 全開也才 0.13 GiB（實測：.27 把 max-context 由 32,768 改成 131,072，
    載入後剩餘 VRAM 33,026→33,094 MiB，等於沒變）；
    傳統全注意力模型可以到幾十萬 B/token，同樣 128K 就要吃掉幾十 GiB。
    所以欄位可以用 kv_bytes_per_cell 指定自己的值，沒指定才用引擎預設表。
    """
    if not cfg:
        return None
    per = cfg.get("kv_bytes_per_cell") or KV_BYTES.get(cfg["kv"], KV_BYTES["fp16"])
    return round(cfg["max_context"] * cfg["parallel"] * per / GIB, 2)


def slot_config(slot):
    """欄位的有效設定 = 設定檔讀到的 ＋ 欄位自己的覆寫（例 kv_bytes_per_cell）。"""
    cfg = None
    if slot.get("kind") == "strata" and slot.get("config"):
        cfg = strata_config(slot["config"])
    elif slot.get("kind") == "llama" and slot.get("config"):
        cfg = llama_config(slot["config"])
    if cfg is None and slot.get("max_context"):
        cfg = {"max_context": int(slot["max_context"]), "kv": slot.get("kv", "fp16"),
               "kv_grow": False, "parallel": int(slot.get("parallel", 1)),
               "port": slot.get("port"), "model_name": slot.get("model"),
               "expert_cache": None, "resident_budget_gib": 0.0,
               "fit_max_tokens": False, "exe": None, "kv_bytes_per_cell": None}
    if cfg is not None and slot.get("kv_bytes_per_cell"):
        cfg["kv_bytes_per_cell"] = int(slot["kv_bytes_per_cell"])
    return cfg


MEASURE_RX = re.compile(
    r"prompt (\d+) tokens.*?\(([\d.]+) tok/s\).*?(\d+) generated in \d+ ms \(([\d.]+) tok/s\)")


def harvest_measurements(slot, reg):
    """從服務自己的 log 撈實測速度，累積成區間。數字一律是量到的，不是填的。"""
    log = slot.get("log")
    prev = reg["measure"].get(slot["id"])
    if not log or not os.path.exists(log):
        return prev
    out = "\n".join(ln for ln in tail_lines(log, 4000) if "serve: prompt" in ln)
    if not out:
        return prev
    pre, dec, ctx = [], [], 0
    for line in out.splitlines():
        m = MEASURE_RX.search(line)
        if not m:
            continue
        ctx = max(ctx, int(m.group(1)))
        pre.append(float(m.group(2)))
        dec.append(float(m.group(4)))
    if not pre:
        return prev
    rec = {
        "prefill_min": round(min(pre), 1), "prefill_max": round(max(pre), 1),
        "decode_min": round(min(dec), 1), "decode_max": round(max(dec), 1),
        "longest_prompt": ctx, "samples": len(pre), "updated": int(time.time()),
    }
    if (prev or {}).get("peak_vram_gib"):
        rec["peak_vram_gib"] = prev["peak_vram_gib"]
    return rec


def slot_state(slot, reg, hw):
    state = unit_state(slot["unit"]) if slot.get("unit") else "none"
    active = state == "active"
    busy = state in ("active", "activating", "deactivating")
    cfg = slot_config(slot)
    rss = unit_rss_gib(slot["unit"]) if busy and slot.get("unit") else 0.0
    meas = harvest_measurements(slot, reg) if busy else reg["measure"].get(slot["id"])
    if meas:
        reg["measure"][slot["id"]] = meas

    vram_now = 0.0
    if busy:
        gpu = (hw or {}).get("gpu") or {}
        gpus = gpu.get("gpus") or []
        if slot.get("gpu_ids"):
            # 綁定特定卡的服務（例 MI50 農場每張卡一個實例）可以精確歸帳
            want = {str(g) for g in slot["gpu_ids"]}
            vram_now = round(sum((g.get("vram_used") or 0) for g in gpus
                                 if str(g.get("id")) in want) / GIB, 1)
        else:
            # 單 GPU 機器無法逐進程分 VRAM。全部記在每個欄位上會重複計算
            # （實測：主腦與抓料都標 63.9，加起來 127.8 > 整機 96，長條就爆掉）。
            # 改成依各欄位「預估顯存需求」的比重分攤，沒宣告需求的欄位分到 0。
            total_used = (gpu.get("vram_used") or 0) / GIB
            mine = float(slot.get("est_vram_gib") or 0)
            denom = sum(float(x.get("est_vram_gib") or 0) for x in reg["slots"]
                        if x.get("unit") and x.get("_busy_cache"))
            vram_now = round(total_used * (mine / denom), 1) if denom > 0 and mine > 0 else 0.0

    return {
        "id": slot["id"], "role": slot["role"], "tier": slot.get("tier", ""),
        "unit": slot.get("unit"),
        "port": (cfg or {}).get("port") or slot.get("port"),
        "model": slot.get("model"), "quant": slot.get("quant"),
        "caps": slot.get("caps") or [], "kind": slot.get("kind"),
        "gpu_ids": slot.get("gpu_ids"),
        "active": active,
        "state": state,
        "state_label": ACTIVE_LABEL.get(state, state),
        "busy": busy,
        "enabled": unit_enabled(slot["unit"]) if slot.get("unit") else False,
        "ram_gib": rss, "vram_gib": vram_now,
        "vram_exact": bool(slot.get("gpu_ids")),
        "est_vram_gib": slot.get("est_vram_gib"), "est_ram_gib": slot.get("est_ram_gib"),
        "config": cfg, "kv_worst_gib": kv_worst_gib(cfg), "measure": meas,
        "critical": slot.get("critical", False), "note": slot.get("note", ""),
        # 換得回 Strata 的欄位：本來就是 Strata，或現在跑 llama.cpp 但還留著 Strata 的設定
        "has_strata": slot.get("kind") == "strata" or bool((slot.get("engines") or {}).get("strata")),
    }


# ---------------------------------------------------------------- 可換的模型

def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def key_file(pack_name):
    """pack 目錄名就是 setup 用來命名設定檔的鍵（coder-iq1_m → strata-coder-iq1_m.json）。"""
    return pack_name


def model_estimate(reg, name, experts_gib, size_gib, cfg=None, baseline=None):
    """這顆模型大概要吃多少記憶體。優先序：實測 > 推自現用模型 > 保底估計。

    為什麼不用公式硬算：Strata 的常駐專家量是「看機器還剩多少 RAM 自己決定」的
    （UD-IQ4_XS 的 --resident-budget-gib 寫 20，log 顯示它實際只放 5.80 GiB），
    所以從 experts.bin 大小推 RAM 會嚴重高估。實測值才有意義。
    """
    rec = (reg.get("model_est") or {}).get(name)
    if rec and rec.get("ram") is not None:
        return {"ram": rec["ram"], "vram": rec["vram"], "source": "實測"}

    if baseline and baseline.get("ram"):
        # 同一個引擎、同一組常駐預算跑另一顆模型：以實測過的那顆為基準，
        # 用整包權重大小的比例調整，並且只往下調不往上調
        # （比現用的小就該更省；比現用的大則保守沿用現用值，等真的載過再更新）。
        ratio = min(1.0, (size_gib or 0) / baseline["size"]) if baseline.get("size") else 1.0
        return {"ram": round(baseline["ram"] * max(ratio, 0.5), 1),
                "vram": round((baseline["vram"] or 0) * max(ratio, 0.5), 1),
                "source": "推自現用模型"}

    budget = float((cfg or {}).get("resident_budget_gib") or 0) or 20.0
    return {"ram": round(budget * 0.6 + 4.0, 1),
            "vram": round(min(size_gib or 0, 90.0), 1), "source": "保底估計"}


def record_model_measure(slot_id, model_name):
    """載入成功並穩定之後，把這顆模型實際吃掉多少記下來，下次換它就不用猜。"""
    reg = registry()
    hw = hardware()
    slot = next((x for x in reg["slots"] if x["id"] == slot_id), None)
    if not slot:
        return
    for sl in reg["slots"]:
        sl["_busy_cache"] = (unit_state(sl["unit"]) in ("active", "activating")
                             if sl.get("unit") else False)
    st = slot_state(slot, reg, hw)
    for sl in reg["slots"]:
        sl.pop("_busy_cache", None)
    if not st["busy"]:
        return
    size_gib = None
    cfgx = slot_config(slot)
    if cfgx and cfgx.get("_native"):
        d = os.path.dirname(cfgx["_native"])
        try:
            size_gib = round(sum(os.path.getsize(os.path.join(d, f))
                                 for f in os.listdir(d) if f.endswith(".gguf")) / GIB, 1)
        except OSError:
            size_gib = None
    reg.setdefault("model_est", {})[model_name] = {
        "ram": round(st["ram_gib"], 1), "vram": round(st["vram_gib"], 1),
        "size_gib": size_gib, "measured_at": int(time.time()),
    }
    for sl in reg["slots"]:
        if sl["id"] == slot_id:
            sl["est_ram_gib"] = round(st["ram_gib"], 1)
            sl["est_vram_gib"] = round(st["vram_gib"], 1)
    save_json(REGISTRY, reg)


def catalog(slot_id=None):
    """換模型視窗要的清單：Strata 家族＋GGUF 存放區，兩種引擎一起列。"""
    reg = registry()
    slot = next((x for x in reg["slots"] if x["id"] == slot_id), None) if slot_id else None
    probe = dict(slot) if slot else None
    if probe and probe.get("kind") == "llama" and (probe.get("engines") or {}).get("strata"):
        probe.update(kind="strata", **probe["engines"]["strata"])      # 欄位現在跑 llama.cpp，Strata 的清單照它原本的服務算
    out = catalog_for(probe, reg) if probe and probe.get("kind") == "strata" else {"models": [], "orphans": []}
    for m in out.get("models", []):
        m["folder"] = m["name"]
        m["name"], m["quant"] = pretty_model(m["native"])      # 畫面顯示權重檔名的模型名稱，不是資料夾名
        if slot and slot.get("kind") == "llama":
            m["current"] = False
    root, ggufs, problems = scan_gguf(reg)
    cur = (slot_config(slot) or {}) if slot else {}
    ctx_default = int(cur.get("max_context") or 32768)
    par = int(cur.get("parallel") or 1)
    p = pools(reg, hardware())
    cap = (p.get("unified") or p.get("vram") or {}).get("total_gib") or 0
    for m in ggufs:
        ctx = min(ctx_default, m["ctx_train"]) if m.get("ctx_train") else ctx_default
        # 用整台的容量（不是現在剩多少）看要不要自動分層，給換模型視窗當預覽；真正換的時候照當下的剩餘量再算一次
        tier = auto_cpu_moe(m, reg["machine"], [{**c, "free_gib": c["total_gib"]} for c in p["gpus"]],
                            (slot or {}).get("gpu_ids"), ctx, par, 0.0)
        est = llama_est(m, reg["machine"], ctx, par, tier)
        m.update(est_vram_gib=est["vram"], est_ram_gib=est["ram"], est_kv_gib=est["kv"], ctx_default=ctx, parallel=par,
                 auto_cpu_moe=tier, moved_gib=est["moved_gib"],
                 fits_total=bool(cap) and est["vram"] + est["kv"] < cap,
                 current=bool(slot and slot.get("kind") == "llama" and os.path.abspath(cur.get("_native") or "") == os.path.abspath(m["path"])))
    out.update(gguf=ggufs, gguf_problems=problems, gguf_root=root,
               llama_server=llama_server_exe(reg), engine=(slot or {}).get("kind"),
               can_llama=bool(slot) and slot.get("kind") in (None, "strata", "llama"))
    return out


def catalog_for(slot, reg):
    """掃出這台裝了哪些可以換的 Strata 模型。

    Strata 一顆模型是「pack（含 tokenizer）＋ 權重檔」兩件成套的，
    少一邊就起不來，所以這裡只回報配得起來的組合，配不起來的另外列出來並說明缺什麼。
    """
    cur = slot_config(slot) if slot else None
    root = (slot or {}).get("model_root")
    if not root:
        # 從現用設定推出模型根目錄，不要寫死路徑
        for sl in reg["slots"]:
            c = slot_config(sl)
            if c and sl.get("config"):
                raw = load_json(sl["config"], {}) or {}
                args = raw.get("args") or []
                if "--pack" in args:
                    root = os.path.dirname(os.path.dirname(args[args.index("--pack") + 1]))
                    break
    if not root or not os.path.isdir(root):
        return {"root": root, "models": [], "orphans": [],
                "error": "找不到模型目錄，請在欄位登錄填 model_root"}

    # 基準＝現在跑著、而且量過的那顆
    baseline = None
    est_tbl = reg.get("model_est") or {}
    for sl in reg["slots"]:
        c = slot_config(sl)
        if not c or not c.get("_native"):
            continue
        nm = os.path.basename(os.path.dirname(c["_native"]))
        if nm in est_tbl and est_tbl[nm].get("ram"):
            baseline = {"ram": est_tbl[nm]["ram"], "vram": est_tbl[nm]["vram"],
                        "size": est_tbl[nm].get("size_gib")}
            break

    packs, weights = {}, {}
    pdir, mdir = os.path.join(root, "packs"), os.path.join(root, "models")
    if os.path.isdir(pdir):
        for d in sorted(os.listdir(pdir)):
            full = os.path.join(pdir, d)
            if os.path.isdir(full):
                packs[_norm(d)] = {"name": d, "path": full,
                                   "has_tokenizer": os.path.exists(os.path.join(full, "tokenizer"))}
    if os.path.isdir(mdir):
        for d in sorted(os.listdir(mdir)):
            full = os.path.join(mdir, d)
            if not os.path.isdir(full):
                continue
            ggufs = sorted(f for f in os.listdir(full) if f.endswith(".gguf"))
            if not ggufs:
                continue
            size = 0
            for f in ggufs:
                try:
                    size += os.path.getsize(os.path.join(full, f))
                except OSError:
                    pass
            weights[_norm(d)] = {"name": d, "path": full, "first": os.path.join(full, ggufs[0]),
                                 "shards": len(ggufs), "size_gib": round(size / GIB, 1)}

    cur_native = (cur or {}).get("_native")
    models, orphans = [], []
    for key, w in weights.items():
        pk = packs.get(key)
        if not pk:
            orphans.append({"name": w["name"], "missing": "缺同名的 pack（內含 tokenizer），無法啟動"})
            continue
        experts_gib = 0.0
        eb = os.path.join(pk["path"], "experts.bin")
        if os.path.exists(eb):
            try:
                experts_gib = round(os.path.getsize(eb) / GIB, 1)
            except OSError:
                pass
        est = model_estimate(reg, w["name"], experts_gib, w["size_gib"], cur, baseline)
        # 換模型要換的是「一整份設定」，不是兩個路徑：
        # 每顆模型的 --ple-gguf、--expert-profile、--expert-cache 都不一樣
        # （實際踩到：只換 pack 與 native，專家剖析檔還指著舊模型，引擎三秒就崩）。
        # setup.sh 安裝時會產生 strata-<id>.json，沒有那份就不讓換。
        cfg_file = None
        if slot and slot.get("config"):
            cand = os.path.join(os.path.dirname(slot["config"]), f"strata-{key_file(pk['name'])}.json")
            if os.path.exists(cand):
                cfg_file = cand
        models.append({
            "id": key, "name": w["name"], "pack": pk["path"], "native": w["first"],
            "shards": w["shards"], "size_gib": w["size_gib"],
            "experts_gib": experts_gib,
            "est_ram_gib": est["ram"], "est_vram_gib": est["vram"],
            "est_source": est["source"],
            "has_tokenizer": pk["has_tokenizer"],
            "config_file": cfg_file,
            "ready": bool(cfg_file and pk["has_tokenizer"]),
            "current": bool(cur_native and os.path.abspath(cur_native) == os.path.abspath(w["first"])),
        })
    for key, pk in packs.items():
        if key not in weights:
            orphans.append({"name": pk["name"], "missing": "只有 pack、沒有權重檔"})
    for m in models:
        if not m["config_file"]:
            orphans.append({"name": m["name"],
                            "missing": "缺 strata-<名稱>.json 設定檔，請用 setup.sh 重新安裝這顆"})
    return {"root": root, "models": sorted(models, key=lambda m: m["name"]), "orphans": orphans}


def wait_healthy(slot, seconds=240):
    """等服務真的能服務。看 systemd 活著還不夠，要它的埠回得出模型清單。"""
    port = (slot_config(slot) or {}).get("port") or slot.get("port")
    deadline = time.time() + seconds
    last = "逾時"
    while time.time() < deadline:
        st = unit_state(slot["unit"])
        if st == "activating":
            # 設了 Restart= 的服務一退出就進 auto-restart，狀態看起來仍是 activating，
            # 只看 failed 會白等到逾時（實際踩到：壞掉的模型讓換模型等了整整 15 分鐘才還原）
            _, sub, _ = sysctl(slot["unit"], "show -p SubState --value", timeout=20)
            if sub.strip() == "auto-restart":
                st = "failed"
        if st == "failed":
            try:
                if SYSTEMD:
                    _, tail, _ = sh(["journalctl", "--user", "-u", unit_name(slot["unit"]), "-n", "3", "--no-pager", "-o", "cat"])
                else:
                    tail = "\n".join(tail_lines(SUP.log_path(unit_name(slot["unit"])) or "", 3))
            except ValueError as ex:
                tail = str(ex)
            return False, (tail or "服務退出").replace("\n", " ")[:200]
        if st == "active":
            if not port:
                return True, ""
            code = str(_http_json(f"http://127.0.0.1:{int(port)}/v1/models", timeout=6)[0])
            # 401＝服務活著、只是設了金鑰（不帶金鑰打 /v1/models 一定 401）。
            # 只認 200 的話，開了金鑰之後每次重啟都會被判成「起不來」而自動還原
            if code.strip() in ("200", "401"):
                return True, ""
            last = f"服務在跑但 :{port} 還沒回應"
        time.sleep(5)
    return False, last


QUANT_RX = re.compile(r"[-_.]((?:UD-)?(?:I?Q\d\w*|F16|BF16|MXFP4\w*))$", re.I)


def pretty_model(native):
    """從權重檔名取給人看的模型名稱與量化：
    Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf → ("Qwen3.8-Flash-Next", "UD-IQ4_XS")。
    （以前直接拿資料夾名，畫面就顯示成 unsloth-UD-IQ4_XS，看不出是哪顆模型。）"""
    base = re.sub(r"-\d{5}-of-\d{5}$", "", os.path.basename(native).removesuffix(".gguf"))
    m = QUANT_RX.search(base)
    return (base[:m.start()], m.group(1)) if m else (base, None)


def set_model(slot_id, pack, native, force=False, extra_free=None):
    """把某個欄位換成另一顆模型：先驗證成套、備份、改設定、重啟，失敗自動還原。"""
    reg = registry()
    slot = next((x for x in reg["slots"] if x["id"] == slot_id), None)
    if not slot or not slot.get("config"):
        return 404, {"error": f"{slot_id} 沒有可改的設定檔"}

    allowed = {m["pack"]: m for m in (catalog_for(slot, reg).get("models") or [])}
    pick = allowed.get(pack)
    if not pick or os.path.abspath(pick["native"]) != os.path.abspath(native):
        # 只接受掃描掃得到的組合，避免有人用這支 API 指到任意路徑
        return 400, {"error": "這個組合不在本機掃到的模型清單內"}
    if not pick["has_tokenizer"]:
        return 400, {"error": f"{pick['name']} 的 pack 裡沒有 tokenizer，換過去會起不來"}

    # ⚠️ 准入試算要用「要換過去那顆」的大小，不是欄位上為舊模型估的數字。
    # 實際踩到：Coder 的 experts.bin 只有 23.4 GiB，比現用的小得多，
    # 卻因為沿用舊模型的 14 GiB 估值而被擋下來。
    reg2 = registry()
    for sl in reg2["slots"]:
        if sl["id"] == slot_id:
            sl["est_ram_gib"] = pick.get("est_ram_gib", sl.get("est_ram_gib"))
            sl["est_vram_gib"] = pick.get("est_vram_gib", sl.get("est_vram_gib"))
    save_json(REGISTRY, reg2)

    if not pick.get("config_file"):
        return 400, {"error": f"{pick['name']} 沒有對應的 strata-<名稱>.json 設定檔。"
                              f"換模型要換的是一整份設定（--ple-gguf、--expert-profile 每顆都不同），"
                              f"請先用 setup.sh 安裝這顆模型產生它。"}

    old_cfg = load_json(slot["config"], None)
    if old_cfg is None:
        return 500, {"error": "讀不到現用設定檔"}
    new_cfg = load_json(pick["config_file"], None)
    if new_cfg is None:
        return 500, {"error": f"讀不到 {pick['config_file']}"}
    backup = slot["config"] + f".bak-model-{int(time.time())}"
    save_json(backup, old_cfg)

    # 以該模型自己的設定為底，只覆寫「跟這台機器有關、不跟模型有關」的項目：
    # 引擎執行檔與函式庫路徑（我們自己編的 build-145 ＋ 系統 ROCm 執行，
    # setup 會寫成 TheRock 的路徑，那個編得動但跑不起來）、服務埠、並行數。
    for key in ("exe", "lib_dirs", "port", "parallel", "fit_max_tokens", "host", "api_key", "max_tokens"):
        if key in old_cfg:
            new_cfg[key] = old_cfg[key]
    # 使用者在網頁上自己取過 API 模型名稱，就沿用（IDE 那邊不必跟著改）；沒取過就用新模型自己的名稱
    if slot.get("api_model_name"):
        new_cfg["model_name"] = slot["api_model_name"]
    new_cfg["log"] = old_cfg.get("log", new_cfg.get("log"))
    save_json(slot["config"], new_cfg)
    os.chmod(slot["config"], 0o600)          # 可能帶著 API 金鑰

    verdict = admit(slot_id, extra_free=extra_free)
    if not verdict["allow"] and not force:
        save_json(slot["config"], load_json(backup, {}))
        reg3 = registry()
        for sl in reg3["slots"]:
            if sl["id"] == slot_id:
                sl["est_ram_gib"] = slot.get("est_ram_gib")
                sl["est_vram_gib"] = slot.get("est_vram_gib")
        save_json(REGISTRY, reg3)
        return 409, {"error": "擋下來了，設定已還原", "admit": verdict}

    sysctl(slot["unit"], "reset-failed", timeout=20)   # 清掉先前的重啟限流，否則 start 會被擋
    rc, out, err = sysctl(slot["unit"], "restart")
    ok, why = wait_healthy(slot, seconds=240)
    if not ok:
        # ⚠️ 不可以只看 systemctl 的回傳值：Type=simple 的 unit 只要被接受就回 0，
        # 載模型失敗是幾秒後才退出的（實際踩到：回傳成功但服務其實已經死了，
        # 自動還原沒觸發，.27 的引擎就那樣躺在那裡）。
        save_json(slot["config"], load_json(backup, {}))
        sysctl(slot["unit"], "reset-failed", timeout=20)
        sysctl(slot["unit"], "restart")
        back_ok, _ = wait_healthy(slot, seconds=240)
        return 500, {"error": f"{pick['name']} 載不起來（{why}），已還原成原本的模型"
                              + ("" if back_ok else "，但原本那顆也還沒起來，請查記錄"),
                     "reverted": True, "restored_ok": back_ok}
    reg = registry()
    for sl in reg["slots"]:
        if sl["id"] == slot_id:
            sl["model"], quant = pretty_model(pick["native"])
            if quant:
                sl["quant"] = quant
            sl.pop("api_last_test", None)
    reg["measure"].pop(slot_id, None)   # 換了模型，舊的速度實測不再代表它
    save_json(REGISTRY, reg)
    time.sleep(8)                        # 等它把權重載進去再量，不然量到載入中的瞬間
    record_model_measure(slot_id, pick["name"])
    return 200, {"ok": True, "model": pretty_model(pick["native"])[0], "folder": pick["name"], "backup": backup,
                 "api_model_name": new_cfg.get("model_name")}


# ---------------------------------------------------------------- API 串接
# 換完模型之後，IDE（GitHub Copilot、OpenCode、Continue、Cline…）與其他服務要知道四件事：
# 打哪個網址、模型名稱叫什麼、上下文多長、要不要金鑰。這一段負責讀、改、實測。

API_NAME_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")
API_URL_RX = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~/-]*)?")


def _slot(slot_id):
    return next((x for x in registry()["slots"] if x["id"] == slot_id), None)


def _first_key(v):
    if isinstance(v, list):
        return next((str(x) for x in v if str(x).strip()), "")
    return str(v or "").split(",")[0].strip()


def _local_ip():
    """對外網卡的 IP（跟 ip route get 1.1.1.1 的 src 同一個）。UDP connect 不送任何封包，三個系統都能用。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.connect(("1.1.1.1", 80))
            return so.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _http_json(url, body=None, key="", timeout=8):
    """打本機的模型服務。回 (狀態碼, JSON 或 None, 毫秒)。連不上回 (0, None, 毫秒)。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {key}"} if key else {})})
    t0 = time.time()
    try:
        with LOCAL.open(req, timeout=timeout) as resp:
            return resp.status, json.load(resp), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc), int((time.time() - t0) * 1000)
        except Exception:  # noqa: BLE001
            return exc.code, None, int((time.time() - t0) * 1000)
    except Exception:  # noqa: BLE001
        return 0, None, int((time.time() - t0) * 1000)


def _api_parts(slot):
    """回 (可改的設定檔內容或 None, 埠, 金鑰)。Strata 與 llama.cpp 的設定檔格式不同，名稱欄位也不同，
    這裡統一成同一組鍵（model_name、max_tokens），寫回時再轉回各自的格式（_api_write）。"""
    cfg = slot_config(slot) or {}
    if slot.get("kind") == "strata" and slot.get("config"):
        raw = load_json(slot["config"], None)
        key = _first_key((raw or {}).get("api_key"))
    elif slot.get("kind") == "llama" and slot.get("config"):
        raw = load_json(slot["config"], None)
        key = llama_key(raw)
        if raw is not None:
            raw = {**raw, "model_name": raw.get("alias")}
    else:
        raw, key = None, ""
    port = (raw or {}).get("port") or cfg.get("port") or slot.get("port")
    return raw, port, key


def _no_key_blocked(port):
    """沒帶金鑰打不打得進去。用 POST 對話端點測：llama.cpp 的 /v1/models 本來就不需要金鑰，拿它測會誤判。"""
    st, _, _ = _http_json(f"http://127.0.0.1:{int(port)}/v1/chat/completions", body={}, timeout=8)
    return st == 401, st


def api_info(slot_id):
    slot = _slot(slot_id)
    if not slot or not slot.get("unit"):
        return 404, {"error": "沒有這個欄位"}
    raw, port, key = _api_parts(slot)
    if not port:
        return 400, {"error": "這個欄位沒有對外的 API 埠"}
    st, models, _ = _http_json(f"http://127.0.0.1:{int(port)}/v1/models", key=key, timeout=5)
    served = []
    for m in (models or {}).get("data") or []:
        arch = m.get("architecture") or {}
        served.append({"id": m.get("id"), "ctx": (m.get("meta") or {}).get("n_ctx"),
                       "vision": "image" in (arch.get("input_modalities") or [])})
    cfg = slot_config(slot) or {}
    reg = registry()
    ip = reg["machine"].get("ip") or _local_ip()
    strata = slot.get("kind") == "strata" and raw is not None
    llama = slot.get("kind") == "llama" and raw is not None
    return 200, {
        "slot": slot_id, "role": slot.get("role"), "tier": slot.get("tier", ""),
        "model": slot.get("model"), "kind": slot.get("kind"), "critical": bool(slot.get("critical")),
        "editable": strata or llama, "engine": slot.get("kind"), "port": int(port),
        "bind": (raw or {}).get("host") or "0.0.0.0",
        "default_url": f"http://{ip}:{int(port)}/v1",
        "public_url": slot.get("api_public_url") or f"http://{ip}:{int(port)}/v1",
        "public_url_custom": bool(slot.get("api_public_url")),
        "model_name": (raw or {}).get("model_name") or (served[0]["id"] if served else None),
        "online": st == 200 and bool(served), "status": st, "served": served,
        "max_context": cfg.get("max_context") or (served[0]["ctx"] if served else None),
        "max_tokens": (raw or {}).get("max_tokens"),
        "fit_max_tokens": bool(cfg.get("fit_max_tokens")),
        "parallel": cfg.get("parallel"),
        "api_key_set": bool(key),
        # Strata 同時講三種方言；其他引擎只保證 OpenAI 那種
        "dialects": ["openai", "anthropic", "responses"] if strata else ["openai"],
        # llama.cpp 能不能呼叫工具要看模型自己的對話範本，實測那一項會告訴你
        "tools": True if strata else None,
        "vision": any(m["vision"] for m in served),
        "last_test": slot.get("api_last_test"),
    }


def _set_slot_fields(slot_id, **fields):
    reg = registry()
    for sl in reg["slots"]:
        if sl["id"] == slot_id:
            for k, v in fields.items():
                if v is None:
                    sl.pop(k, None)
                else:
                    sl[k] = v
    save_json(REGISTRY, reg)


def api_settings(slot_id, body, force=False):
    """網頁改 API 設定。對外網址只記在登錄檔（不重啟）；其餘寫進模型設定檔並重啟，
    重啟後要真的用新名稱、新金鑰打得通才算數，否則換回原本的設定。"""
    slot = _slot(slot_id)
    if not slot or not slot.get("unit"):
        return 404, {"error": "沒有這個欄位"}
    raw, port, key = _api_parts(slot)
    if not port:
        return 400, {"error": "這個欄位沒有對外的 API 埠"}
    errors = []

    url = body.get("public_url")
    if url is not None:
        url = str(url).strip().rstrip("/")
        if url and (len(url) > 200 or not API_URL_RX.fullmatch(url)):
            errors.append("對外網址格式不對，例：http://192.168.11.26:8081/v1")

    want = {}
    if raw is not None:
        if "model_name" in body:
            nm = str(body.get("model_name") or "").strip()
            if not API_NAME_RX.fullmatch(nm):
                errors.append("模型名稱只能用英數字與 . _ - : /，1～80 字")
            elif nm != raw.get("model_name"):
                want["model_name"] = nm
        cur_ctx = (slot_config(slot) or {}).get("max_context")
        ctx = cur_ctx
        if body.get("max_context") not in (None, ""):
            try:
                ctx = int(body["max_context"])
            except (TypeError, ValueError):
                ctx = -1
            if not 2048 <= ctx <= 1048576:
                errors.append("上下文長度要在 2,048～1,048,576 之間")
            elif ctx != cur_ctx:
                want["max_context"] = ctx
        if "max_tokens" in body:
            mt = body.get("max_tokens")
            if mt in (None, "", 0):
                if raw.get("max_tokens") is not None:
                    want["max_tokens"] = None
            else:
                try:
                    mt = int(mt)
                except (TypeError, ValueError):
                    mt = -1
                if not 256 <= mt <= 131072 or (ctx and mt >= ctx):
                    errors.append("單次輸出上限要在 256～131,072 之間，而且要小於上下文長度")
                elif mt != raw.get("max_tokens"):
                    want["max_tokens"] = mt
        k = body.get("key")
        if k not in (None, "keep"):
            if k == "none":
                if key:
                    want["api_key"] = None
            elif k == "new":
                want["api_key"] = "xc-" + secrets.token_urlsafe(32)
            else:
                errors.append("金鑰的選項不對")
    elif any(x in body for x in ("model_name", "max_context", "max_tokens", "key")):
        errors.append("這個欄位的服務不是由 xConsole 管理設定檔，只能改對外網址")
    if errors:
        return 400, {"error": "；".join(errors), "errors": errors}

    if url is not None:
        _set_slot_fields(slot_id, api_public_url=url or None)
    if not want:
        return 200, {"ok": True, "restarted": False, "message": "已儲存" if url is not None else "沒有變更"}

    if slot.get("kind") == "llama":
        return _api_apply_llama(slot, raw, port, want, force)
    old = dict(raw)
    new = dict(raw)
    for k, v in want.items():
        if k == "max_context":
            args = list(new.get("args") or [])
            if "--max-context" in args:
                args[args.index("--max-context") + 1] = str(v)
            else:
                args += ["--max-context", str(v)]
            new["args"] = args
        elif v is None:
            new.pop(k, None)
        else:
            new[k] = v
    backup = slot["config"] + f".bak-api-{int(time.time())}"
    save_json(backup, old)
    os.chmod(backup, 0o600)
    save_json(slot["config"], new)
    os.chmod(slot["config"], 0o600)          # 設定檔裡有金鑰

    def revert(msg, extra=None):
        save_json(slot["config"], old)
        sysctl(slot["unit"], "reset-failed", timeout=20)
        sysctl(slot["unit"], "restart")
        back_ok, _ = wait_healthy(slot, seconds=240)
        return 500, {"error": msg + "，已換回原本的設定" + ("" if back_ok else "，但原本的也還沒起來，請查記錄"),
                     "reverted": True, "restored_ok": back_ok, **(extra or {})}

    if "max_context" in want:
        verdict = admit(slot_id)
        if not verdict["allow"] and not force:
            save_json(slot["config"], old)
            return 409, {"error": "上下文調大之後記憶體不夠，設定已還原", "admit": verdict}

    sysctl(slot["unit"], "reset-failed", timeout=20)
    sysctl(slot["unit"], "restart")
    ok, why = wait_healthy(slot, seconds=240)
    if not ok:
        return revert(f"套用後服務起不來（{why}）")
    # 埠不開放在網頁改：引擎的 systemd 服務用 --port 帶埠，會蓋過設定檔；而且一改埠，所有已串接的工具一起斷
    nport = port
    nkey = _first_key(new.get("api_key"))
    st, models, _ = _http_json(f"http://127.0.0.1:{int(nport)}/v1/models", key=nkey, timeout=8)
    ids = [m.get("id") for m in (models or {}).get("data") or []]
    if st != 200 or (new.get("model_name") and new["model_name"] not in ids):
        return revert(f"套用後用新設定打不通（/v1/models 回 {st}，模型清單 {ids}）")
    if nkey:
        blocked, st2 = _no_key_blocked(nport)
        if not blocked:
            return revert(f"設了金鑰但沒帶金鑰也打得進去（回 {st2}），引擎沒有吃到金鑰")
    # 換模型時要沿用使用者自己取的名稱；設定一變，上一次的實測就不算數
    _set_slot_fields(slot_id, api_model_name=new.get("model_name") if "model_name" in want else slot.get("api_model_name"),
                     api_last_test=None)
    out = {"ok": True, "restarted": True, "backup": backup,
           "changed": [k for k in want if k != "api_key"] + (["api_key"] if "api_key" in want else []),
           "message": "已套用並重啟，用新設定實際打過一次 /v1/models"}
    if want.get("api_key"):
        out["api_key"] = want["api_key"]       # 只在這一次回傳，之後任何 API 都不再回
    return 200, out


def _api_apply_llama(slot, raw, port, want, force):
    """llama.cpp 欄位的 API 設定寫回：名稱＝--alias、上下文＝ctx、輸出上限＝-n、金鑰＝--api-key-file。"""
    slot_id = slot["id"]
    cfg_path = slot["config"]
    old = load_json(cfg_path, {}) or {}
    new = dict(old)
    old_key_text = load_text(engine_file(slot_id, ".key"))
    for k, v in want.items():
        if k == "model_name":
            new["alias"] = v
        elif k == "max_context":
            new["ctx"] = int(v)
        elif k == "max_tokens":
            new["max_tokens"] = v
        elif k == "api_key":
            new["api_key_file"] = write_llama_key(slot_id, v)
    save_json(cfg_path, new)
    os.chmod(cfg_path, 0o600)

    def revert(msg):
        save_json(cfg_path, old)
        if old_key_text is not None:
            write_llama_key(slot_id, old_key_text.strip())
        elif "api_key" in want:
            write_llama_key(slot_id, "")
        sysctl(slot["unit"], "reset-failed", timeout=20)
        sysctl(slot["unit"], "restart")
        back_ok, _ = wait_healthy(slot, seconds=900)
        return 500, {"error": msg + "，已換回原本的設定" + ("" if back_ok else "，但原本的也還沒起來，請查記錄"),
                     "reverted": True, "restored_ok": back_ok}

    if "max_context" in want:
        verdict = admit(slot_id)
        if not verdict["allow"] and not force:
            save_json(cfg_path, old)
            if "api_key" in want:
                write_llama_key(slot_id, (old_key_text or "").strip())
            return 409, {"error": "上下文調大之後記憶體不夠，設定已還原", "admit": verdict}
    sysctl(slot["unit"], "reset-failed", timeout=20)
    sysctl(slot["unit"], "restart")
    ok, why = wait_healthy(slot, seconds=900)
    if not ok:
        return revert(f"套用後服務起不來（{why}）")
    nkey = llama_key(new)
    st, models, _ = _http_json(f"http://127.0.0.1:{int(port)}/v1/models", key=nkey, timeout=8)
    ids = [m.get("id") for m in (models or {}).get("data") or []]
    if st != 200 or new.get("alias") not in ids:
        return revert(f"套用後用新設定打不通（/v1/models 回 {st}，模型清單 {ids}）")
    if nkey:
        blocked, st2 = _no_key_blocked(port)
        if not blocked:
            return revert(f"設了金鑰但沒帶金鑰也打得進去（回 {st2}），引擎沒有吃到金鑰")
    _set_slot_fields(slot_id, api_model_name=new.get("alias") if "model_name" in want else slot.get("api_model_name"),
                     api_last_test=None)
    out = {"ok": True, "restarted": True, "changed": list(want),
           "message": "已套用並重啟，用新設定實際打過一次 /v1/models"}
    if want.get("api_key"):
        out["api_key"] = want["api_key"]
    return 200, out


def api_test(slot_id):
    """在這台本機實際打一次：模型清單 → 一般對話 → 工具呼叫（Copilot、OpenCode 的代理模式靠它）。"""
    slot = _slot(slot_id)
    if not slot or not slot.get("unit"):
        return 404, {"error": "沒有這個欄位"}
    raw, port, key = _api_parts(slot)
    if not port:
        return 400, {"error": "這個欄位沒有對外的 API 埠"}
    base = f"http://127.0.0.1:{int(port)}/v1"
    checks = []
    st, models, ms = _http_json(base + "/models", key=key, timeout=8)
    ids = [m.get("id") for m in (models or {}).get("data") or []]
    checks.append({"name": "模型清單 /v1/models", "ok": st == 200 and bool(ids), "ms": ms,
                   "detail": "、".join(ids) if ids else f"回 {st or '連不上'}"})
    name = (raw or {}).get("model_name") or (ids[0] if ids else "")
    if key:
        blocked, st0 = _no_key_blocked(port)
        checks.append({"name": "沒帶金鑰要被擋下", "ok": blocked, "ms": 0,
                       "detail": "回 401，金鑰保護有效" if blocked else f"回 {st0}，沒有被擋"})
    if ids:
        st, r, ms = _http_json(base + "/chat/completions", key=key, timeout=180, body={
            "model": name, "max_tokens": 512, "stream": False,
            "messages": [{"role": "user", "content": "請只回答：連線成功"}]})
        msg = (((r or {}).get("choices") or [{}])[0].get("message") or {})
        use = (r or {}).get("usage") or {}
        checks.append({"name": "一般對話 /v1/chat/completions", "ok": st == 200 and bool(msg.get("content")),
                       "ms": ms, "detail": (msg.get("content") or f"回 {st}").strip()[:200],
                       "tokens": use.get("total_tokens")})
        st, r, ms = _http_json(base + "/chat/completions", key=key, timeout=180, body={
            "model": name, "max_tokens": 1024, "stream": False,
            "messages": [{"role": "user", "content": "台北現在幾點？請用工具查。"}],
            "tools": [{"type": "function", "function": {
                "name": "get_time", "description": "查某個城市現在的時間",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                               "required": ["city"]}}}]})
        calls = (((r or {}).get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
        fn = (calls[0].get("function") or {}) if calls else {}
        try:                                   # 引擎回的參數常是 \uXXXX 跳脫，轉回中文給人看
            args_txt = json.dumps(json.loads(fn.get("arguments") or "{}"), ensure_ascii=False)
        except (TypeError, ValueError):
            args_txt = str(fn.get("arguments"))
        checks.append({"name": "工具呼叫（IDE 代理模式要用）", "ok": st == 200 and fn.get("name") == "get_time",
                       "ms": ms, "detail": f"呼叫了 {fn.get('name')}（{args_txt}）" if calls
                       else f"回 {st}，模型沒有呼叫工具"})
    ok = all(c["ok"] for c in checks)
    rec = {"at": int(time.time()), "ok": ok, "where": "本機", "model": name}
    _set_slot_fields(slot_id, api_last_test=rec)
    return 200, {"ok": ok, "checks": checks, "model": name, "at": rec["at"]}


# ---------------------------------------------------------------- 模型庫與 llama.cpp 引擎
# Strata 只跑 Qwen3.8-Flash-Next 一家；其他模型（Gemma 4、gpt-oss、Llama…）一律走 llama.cpp。
# 方法固定成四件事：
#   1. 放哪裡：每台一個 GGUF 存放區（登錄檔 machine.gguf_root，預設 ~/models/gguf），
#      一顆模型一個資料夾：<存放區>/<資料夾>/<模型>.gguf（可分片），看圖模型把 mmproj*.gguf 放同資料夾
#   2. 怎麼進來：自己複製進去，或網頁貼 Hugging Face 名稱由代理在背景下載
#   3. 怎麼選：換模型一次列出 Strata 與 GGUF；記憶體需求讀 GGUF 檔頭算（權重＋上下文的 KV）
#   4. 怎麼切：同一個欄位、同一個埠；跨引擎時停用舊引擎、啟用新引擎，用新模型實際打一次，打不通整組還原


GGUF_ROOT_DEFAULT = "~/models/gguf"
ENGINE_DIR = os.path.join(STATE_DIR, "engines")
DOWNLOADS_FILE = os.path.join(STATE_DIR, "downloads.json")
SAFE_ID_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def inside(base, name):
    """組出 base 底下的路徑，並確認真的落在 base 裡面（擋 ../ 與符號連結跳出去）。
    欄位 id、模型 id 雖然都先比對過登錄檔與掃描清單，組路徑時一律再過這一道。"""
    root = os.path.realpath(base)
    full = os.path.realpath(os.path.join(root, name))
    if not full.startswith(root + os.sep):
        raise ValueError(f"路徑不在 {base} 內")
    return full


def engine_file(slot_id, suffix):
    """欄位在引擎目錄裡的檔案（設定檔、金鑰檔、記錄檔）。欄位 id 只准英數、底線、連字號。"""
    if not SAFE_ID_RX.fullmatch(slot_id or ""):
        raise ValueError("欄位 id 不合法")
    os.makedirs(ENGINE_DIR, mode=0o700, exist_ok=True)
    return inside(ENGINE_DIR, f"{slot_id}{suffix}")
HF_BASE = os.environ.get("XCONSOLE_HF_BASE", "https://huggingface.co")   # 測試時指到本機假站
SHARD_RX = re.compile(r"-(\d{5})-of-(\d{5})$")
HF_REPO_RX = re.compile(r"[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}")
# 政府案不得使用中國來源模型；這裡只負責標示，選不選由人決定（內部測試可以用）
CN_ORIGIN = ("qwen", "deepseek", "glm", "chatglm", "kimi", "moonshot", "minimax", "hunyuan",
             "internlm", "baichuan", "doubao", "ernie", "seed-oss")


def is_mmproj(name):
    """看圖用的投影檔：名稱裡有 mmproj 就算（有的放在前面 mmproj-F16.gguf，有的在後面 …-OCR-v1-mmproj-f16.gguf）。"""
    return "mmproj" in os.path.basename(name).lower()


def gguf_root(reg=None):
    reg = reg or registry()
    return expand(reg["machine"].get("gguf_root") or GGUF_ROOT_DEFAULT)


def gguf_meta(path, tensors=False):
    """讀 GGUF 檔頭的 key-value（不讀權重）。tokenizer 那種幾十萬筆的陣列直接跳過。
    path 也可以是已開好的檔案物件（例：從 Hugging Face 用 Range 抓回來的前幾 MB）。
    tensors=True 時再讀張量清單（名稱與 offset），放在 _tensors，權重資料起點放在 _data_start。"""
    sz = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    fm = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
    try:
        with (open(path, "rb") if isinstance(path, str) else path) as f:
            if f.read(4) != b"GGUF":
                return None
            f.read(4)
            n_tensors, n_kv = struct.unpack("<QQ", f.read(16))

            def rstr():
                n, = struct.unpack("<Q", f.read(8))
                return f.read(n).decode("utf-8", "replace")

            def rval(t):
                if t == 8:
                    return rstr()
                if t == 9:
                    et, = struct.unpack("<I", f.read(4))
                    cnt, = struct.unpack("<Q", f.read(8))
                    if et == 8:
                        if cnt > 64:
                            for _ in range(cnt):
                                n, = struct.unpack("<Q", f.read(8))
                                f.seek(n, 1)
                            return None
                        return [rstr() for _ in range(cnt)]
                    if et in sz:
                        if cnt > 4096:
                            f.seek(sz[et] * cnt, 1)
                            return None
                        return list(struct.unpack("<" + fm[et] * cnt, f.read(sz[et] * cnt)))
                    raise ValueError("不支援的巢狀陣列")
                return struct.unpack("<" + fm[t], f.read(sz[t]))[0]

            out = {}
            for _ in range(min(n_kv, 100000)):
                k = rstr()
                t, = struct.unpack("<I", f.read(4))
                out[k] = rval(t)
            if tensors:
                infos = []
                for _ in range(min(n_tensors, 200000)):
                    name = rstr()
                    nd, = struct.unpack("<I", f.read(4))
                    f.read(8 * nd + 4)                       # 各維大小＋量化型別，用不到
                    off, = struct.unpack("<Q", f.read(8))
                    infos.append((name, off))
                align = int(out.get("general.alignment") or 32)
                out["_tensors"] = infos
                out["_data_start"] = -(-f.tell() // align) * align
            return out
    except (OSError, struct.error, ValueError):
        return None


def gguf_profile(meta):
    """從檔頭推出：架構、訓練上下文、每個 token 的 KV 位元組（f16）、是否中國來源。
    KV 照每層的 KV 頭數加總（混合架構的 SSM 層 KV 頭數是 0，自然不算）；
    滑動視窗層只留視窗內的 token，在 kv_worst 那裡另外處理。"""
    if not meta:
        return None
    arch = meta.get("general.architecture") or "?"
    g = lambda k, d=None: meta.get(f"{arch}.{k}", d)  # noqa: E731
    n_layer = int(g("block_count", 0) or 0)
    heads = g("attention.head_count", 0)
    heads = max(heads) if isinstance(heads, list) else int(heads or 0)
    emb = int(g("embedding_length", 0) or 0)
    k_len = int(g("attention.key_length", 0) or (emb // heads if heads else 0))
    v_len = int(g("attention.value_length", 0) or k_len)
    kv_heads = g("attention.head_count_kv", heads)
    per_layer = kv_heads if isinstance(kv_heads, list) else [int(kv_heads or 0)] * n_layer
    swa = int(g("attention.sliding_window", 0) or 0)
    pattern = g("attention.sliding_window_pattern")
    full_b = swa_b = 0
    for i, h in enumerate(per_layer):
        b = int(h or 0) * (k_len + v_len) * 2
        is_swa = bool(swa) and isinstance(pattern, list) and i < len(pattern) and bool(pattern[i])
        if is_swa:
            swa_b += b
        else:
            full_b += b
    name = meta.get("general.name") or meta.get("general.basename") or ""
    low = f"{arch} {name}".lower()
    return {"arch": arch, "meta_name": name, "ctx_train": int(g("context_length", 0) or 0) or None,
            "kv_full_bpt": full_b, "kv_swa_bpt": swa_b, "swa_window": swa if swa_b else 0,
            "layers": n_layer, "cn_origin": any(c in low for c in CN_ORIGIN)}


def weight_profile(parts):
    """parts：每個分片一筆 (含 _tensors 的 meta, 檔案總位元組)。回：
    active_frac   每產生一個 token 實際要從記憶體讀的權重比例（MoE 只讀被選中的專家；決定速度）
    expert_layers 有專家張量的層數；expert_layer_frac 每層專家占整顆的比例（自動分層一次搬一層）。
    張量大小用相鄰 offset 相減、最後一個用檔尾，不必認得每一種量化格式。"""
    sizes = {}
    for meta, file_bytes in parts:
        ts = sorted((meta or {}).get("_tensors") or [], key=lambda x: x[1])
        end = file_bytes - (meta or {}).get("_data_start", 0)
        for i, (name, off) in enumerate(ts):
            sizes[name] = max(0, (ts[i + 1][1] if i + 1 < len(ts) else end) - off)
    total = sum(sizes.values())
    if not total:
        return None
    meta = parts[0][0]
    arch = meta.get("general.architecture")
    n_exp = int(meta.get(f"{arch}.expert_count") or 0)
    used = int(meta.get(f"{arch}.expert_used_count") or 0)
    exps = {n: b for n, b in sizes.items() if "_exps." in n}
    exp_b = sum(exps.values())
    layers = len({n.split(".")[1] for n in exps if n.startswith("blk.")})
    # token_embd 每個 token 只查一列；有獨立 output.weight 時它不算讀取量。共用權重的模型 token_embd 就是輸出層，要算
    # Gemma 3n／4 E 系列的 per_layer_token_embd 也是查表，占了大半檔案卻不是每個 token 整個讀
    skip = (sizes.get("token_embd.weight", 0) if "output.weight" in sizes else 0) + sizes.get("per_layer_token_embd.weight", 0)
    active = total - exp_b - skip + (exp_b * used / n_exp if n_exp else exp_b)
    return {"active_frac": round(active / total, 4), "moe": bool(n_exp and exp_b), "expert_layers": layers,
            "expert_used_frac": round(used / n_exp, 4) if n_exp else None,
            "expert_layer_frac": round(exp_b / layers / total, 6) if layers else 0.0}


def shard_paths(path):
    """分片模型的第一片 → 全部分片的路徑；不是分片就只有自己。"""
    m = SHARD_RX.search(path[:-5]) if path.endswith(".gguf") else None
    if not m:
        return [path]
    base, n = path[:-5][:m.start()], int(m.group(2))
    return [f"{base}-{i:05d}-of-{n:05d}.gguf" for i in range(1, n + 1)]


_META_CACHE = {}


def _profile_cached(path):
    # 鍵要涵蓋每一個分片：只看第一片的話，後面的分片換過（下載完成、修好）快取還是舊的
    try:
        key = tuple((sp, os.path.getmtime(sp), os.path.getsize(sp)) for sp in shard_paths(path))
    except OSError:
        return None
    if key not in _META_CACHE:
        parts = []
        for sp in shard_paths(path):
            meta = gguf_meta(sp, tensors=True)
            if meta is None or not os.path.exists(sp):
                parts = None
                break
            parts.append((meta, os.path.getsize(sp)))
        prof = gguf_profile(parts[0][0]) if parts else None
        if prof:
            prof.update(weight_profile(parts) or {})
        _META_CACHE[key] = prof
    return _META_CACHE[key]


def scan_gguf(reg=None):
    """掃存放區：一個資料夾裡可以放同一顆模型的多個量化，各自算一顆；分片要齊才算可用。"""
    reg = reg or registry()
    root = gguf_root(reg)
    models, problems = [], []
    if not os.path.isdir(root):
        return root, models, problems
    for folder in sorted(os.listdir(root)):
        full = os.path.join(root, folder)
        if not os.path.isdir(full) or folder.startswith("."):
            continue
        files = sorted(f for f in os.listdir(full) if f.endswith(".gguf"))
        mmproj = next((os.path.join(full, f) for f in files if is_mmproj(f)), None)
        groups = {}
        for f in files:
            if is_mmproj(f):
                continue
            base = f[:-5]
            m = SHARD_RX.search(base)
            stem = base[:m.start()] if m else base
            groups.setdefault(stem, []).append((f, int(m.group(2)) if m else 1))
        for stem, parts in groups.items():
            want = parts[0][1]
            paths = [os.path.join(full, f) for f, _ in sorted(parts)]
            size = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
            mm_size = os.path.getsize(mmproj) if mmproj else 0
            name, quant = pretty_model(paths[0])
            mid = f"{folder}/{stem}"
            if len(parts) != want:
                problems.append({"name": mid, "missing": f"分片不齊：有 {len(parts)} 個，應有 {want} 個（下載可能還沒完成）"})
                continue
            prof = _profile_cached(paths[0])
            if not prof:
                problems.append({"name": mid, "missing": "讀不到 GGUF 檔頭，檔案可能損壞或還在下載"})
                continue
            models.append({
                # 名稱用檔頭裡的正式名稱（例 Gemma-4-E4B-It），沒有才用檔名推
                "engine": "llama", "id": mid, "name": prof.get("meta_name") or name, "quant": quant, "folder": folder,
                "path": paths[0], "shards": want, "size_gib": round(size / GIB, 1),
                "mmproj": mmproj, "mmproj_gib": round(mm_size / GIB, 1), "vision": bool(mmproj),
                **prof})
    return root, models, problems


def llama_est(m, machine, ctx, parallel=1, cpu_moe=0):
    """llama.cpp 跑這顆要多少：權重（＋mmproj）＋1 GiB 計算緩衝放 GPU 那邊，KV 另由 kv_worst 算；
    系統記憶體那邊只剩引擎本身（--no-mmap 不會再多一份頁快取）。
    cpu_moe＝自動分層搬到系統記憶體的專家層數：那幾層從 GPU 那邊扣掉、加到系統記憶體。"""
    mm = machine.get("memory_model", "discrete")
    moved = cpu_moe * (m.get("expert_layer_frac") or 0) * m["size_gib"]
    vram = round(m["size_gib"] + m.get("mmproj_gib", 0) + 1.0 - moved, 1)
    return {"vram": vram, "ram": round((1.5 if mm == "unified_single" else 2.5) + moved, 1),
            "kv": round(llama_kv_gib(m, ctx, parallel), 2), "moved_gib": round(moved, 1)}


def auto_cpu_moe(m, machine, cards, gpu_ids, ctx, parallel, back_gib):
    """獨立顯卡放不下 MoE 時，從第一層起把專家搬到系統記憶體，搬到放得下為止（llama.cpp 的 --n-cpu-moe）。
    注意力與共用部分留在卡上，速度掉得比整層搬出去少。統一記憶體機只有一個池，搬了也沒用，回 0。
    back_gib：這次換掉的舊模型會釋出的顯存。搬完全部專家還放不下也回 0，交給准入擋。"""
    if machine.get("memory_model", "discrete") != "discrete" or not m.get("moe") or not m.get("expert_layer_frac"):
        return 0
    use = [c for c in cards if not gpu_ids or str(c["id"]) in gpu_ids]
    if not use:
        return 0
    avail = sum(c["free_gib"] for c in use) + back_gib - floors(machine)["vram"]
    est = llama_est(m, machine, ctx, parallel)
    over = est["vram"] + est["kv"] - avail
    if over <= 0:
        return 0
    n = math.ceil(over / (m["expert_layer_frac"] * m["size_gib"]))
    return n if n <= m.get("expert_layers", 0) else 0


def llama_kv_gib(prof, ctx, parallel=1):
    """全注意力層：上下文 × 並行；滑動視窗層：每條序列各自留一個視窗，所以是 min(上下文, 視窗) × 並行。"""
    swa = prof.get("swa_window") or ctx
    return (prof.get("kv_full_bpt", 0) * ctx * parallel
            + prof.get("kv_swa_bpt", 0) * min(ctx, swa) * parallel) / GIB


def llama_build_no(path, version_text):
    """llama-server 的版本號。網頁編譯是淺層下載（只抓那一版），--version 會回 1；
    這時改用資料夾名稱裡的正式版編號（llama.cpp-b11870 → 11870），不然自動挑選會把新編的當成最舊的。"""
    m = re.search(r"version: [^\n]*?build (\d+)|version: (\d+)", version_text or "")
    ver = int(next(x for x in m.groups() if x)) if m else 0
    d = re.search(r"llama\.cpp-b(\d+)", path)
    return int(d.group(1)) if d and ver <= 1 else ver     # 只有淺層編譯（回 0 或 1）才看資料夾名稱


def llama_globs():
    """家目錄下所有 llama-server：網頁編譯的（llama.cpp-bNNNN/build/bin/）與官方預編譯的（llama.cpp-bNNNN-<平台>/）。"""
    pats = ("llama.cpp*/build*/bin/llama-server", "llama.cpp*/build*/bin/Release/llama-server", "llama.cpp*/llama-server")
    found = {c for pat in pats for c in glob.glob(os.path.join(HOME, pat + EXE))}
    if OS == "darwin":
        found |= {c for c in ("/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server") if os.path.exists(c)}
    return sorted(found)


GPU_LIBS = (("CUDA", "ggml-cuda"), ("ROCm", "ggml-hip"), ("Vulkan", "ggml-vulkan"), ("Metal", "ggml-metal"))


def llama_backend(exe):
    try:
        libs = os.listdir(os.path.dirname(exe))
    except OSError:
        return "只有 CPU"
    return next((b for b, key in GPU_LIBS if any(key in x for x in libs)), "只有 CPU")


def llama_server_exe(reg=None):
    """這台用哪支 llama-server：登錄檔指定的優先，否則挑家目錄下版本號最大、有 GPU 後端的那支。"""
    reg = reg or registry()
    want = reg["machine"].get("llama_server")
    if want and os.path.exists(expand(want)):
        return expand(want)
    best, best_ver = None, -1
    for cand in llama_globs():
        rc, out, err = sh([cand, "--version"], timeout=15)
        ver = llama_build_no(cand, out + err)
        score = ver + (10 ** 6 if llama_backend(cand) != "只有 CPU" else 0)
        if rc == 0 and score > best_ver:
            best, best_ver = cand, score
    return best


def llama_config(path):
    raw = load_json(path, None)
    if not raw:
        return None
    prof = _profile_cached(raw.get("model", "")) or {}
    par = int(raw.get("parallel", 1) or 1)
    ctx = int(raw.get("ctx", 4096) or 4096)
    # kv_worst 用「每 token 位元組 × 上下文 × 並行」：滑動視窗層折算成「每條序列」平均每 token 的位元組
    per = (prof.get("kv_full_bpt", 0) + prof.get("kv_swa_bpt", 0) * min(1.0, (prof.get("swa_window") or ctx) / ctx)) if prof else None
    return {"max_context": ctx, "kv": "f16", "kv_grow": False, "parallel": par,
            "expert_cache": None, "resident_budget_gib": 0.0, "fit_max_tokens": False,
            "port": raw.get("port"), "model_name": raw.get("alias"), "exe": raw.get("exe"),
            "kv_bytes_per_cell": int(per) if per else None,
            "_native": raw.get("model"), "_pack": None, "engine": "llama"}


def llama_key(raw):
    p = (raw or {}).get("api_key_file")
    try:
        with open(p, encoding="utf-8") as fh:
            return next((ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")), "")
    except (OSError, TypeError):
        return ""


def write_llama_key(slot_id, key):
    p = engine_file(slot_id, ".key")
    if not key:
        if os.path.exists(p):
            os.remove(p)
        return None
    os.makedirs(ENGINE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    os.replace(p + ".tmp", p)
    return p


def llama_argv(raw, reg=None):
    """設定檔 → llama-server 參數。金鑰只給檔案路徑（--api-key-file），不放進指令列（ps 看得到）。"""
    reg = reg or registry()
    ctx, par = int(raw.get("ctx", 4096)), int(raw.get("parallel", 1))
    argv = [raw["exe"], "-m", raw["model"], "--host", raw.get("host", "0.0.0.0"), "--port", str(raw["port"]),
            "-c", str(ctx * par), "-np", str(par), "-ngl", str(raw.get("ngl", 999)),
            "--alias", raw["alias"], "--jinja"]
    if raw.get("mmproj"):
        argv += ["--mmproj", raw["mmproj"]]
    if raw.get("cpu_moe"):
        argv += ["--n-cpu-moe", str(int(raw["cpu_moe"]))]
    if raw.get("max_tokens"):
        argv += ["-n", str(int(raw["max_tokens"]))]
    if raw.get("api_key_file") and os.path.exists(raw["api_key_file"]):
        argv += ["--api-key-file", raw["api_key_file"]]
    if not raw.get("mmap", False):
        # 統一記憶體機開 mmap 會多一份頁快取，系統記憶體那塊先爆
        argv += no_mmap_args(raw["exe"])
    extra = reg["machine"].get("llama_args") or []
    return argv + [str(x) for x in extra if isinstance(x, (str, int))]


@functools.lru_cache(maxsize=8)
def no_mmap_args(exe):
    """關 mmap 的寫法跟著 llama.cpp 版本走：build 11626（2026-10）起拿掉 --no-mmap、改成 --load-mode none，
    舊版帶新旗標、新版帶舊旗標都會 invalid argument 直接退出。看 --help 決定。"""
    # 用跟啟動時一樣的環境去問：靠 LD_LIBRARY_PATH 或 llama_env 才載得起來的那支，裸跑 --help 會失敗，
    # 結果就退回帶舊旗標，新版照樣起不來
    try:
        p = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20, env=llama_launch_env(exe))
        text = p.stdout + p.stderr
    except Exception:  # noqa: BLE001
        text = ""
    return ["--load-mode", "none"] if "--load-mode" in text else ["--no-mmap"]


def llama_launch_env(exe, reg=None, gpus=None):
    """llama-server 執行時的環境：登錄檔的 llama_env，加上執行檔目錄放進 LD_LIBRARY_PATH。啟動與偵測版本共用。
    欄位綁了卡就再加上綁卡的變數（蓋過 llama_env 裡同名的設定）。"""
    reg = reg or registry()
    env = {**os.environ, **{str(k): str(v) for k, v in (reg["machine"].get("llama_env") or {}).items()}}
    env.update(visible_env(gpus))
    lib = os.path.dirname(exe)
    # 執行檔旁邊的函式庫：Linux 靠 LD_LIBRARY_PATH；Windows 找 DLL 會看 PATH；macOS 的預編譯版用 @rpath，不必設
    var = {"linux": "LD_LIBRARY_PATH", "windows": "PATH"}.get(OS)
    if var:
        env[var] = lib + (os.pathsep + env[var] if env.get(var) else "")
    return env


def launch_llama(cfg_path):
    """systemd 服務的進入點：讀設定檔、組參數、exec 成 llama-server（之後就是它本人，不多一層）。"""
    raw = load_json(cfg_path, None)
    if not raw:
        raise SystemExit(f"讀不到 {cfg_path}")
    reg = registry()
    argv = llama_argv(raw, reg)
    os.execvpe(argv[0], argv, llama_launch_env(raw["exe"], reg, raw.get("gpus")))


def llama_unit(slot_id):
    if not SAFE_ID_RX.fullmatch(slot_id or ""):
        raise ValueError("欄位 id 不合法")
    return f"xc-{slot_id}-llama.service"


def ensure_llama_unit(slot_id, cfg_path):
    unit = llama_unit(slot_id)
    if not SYSTEMD:
        SUP.define(unit, cfg_path, engine_file(slot_id, "-llama.log"))
        return unit
    # systemd --user 只看「登入帳號」的設定目錄；測試時代理的 HOME 是暫存目錄，所以可以另外指定
    unit_dir = os.environ.get("XCONSOLE_UNIT_DIR") or os.path.join(HOME, ".config/systemd/user")
    path = inside(unit_dir, unit)
    body = (f"[Unit]\nDescription=xConsole 欄位 {slot_id}：llama.cpp\nAfter=network.target\n\n"
            f"[Service]\nType=simple\nExecStart={sys.executable} {os.path.abspath(__file__)} --launch-llama {cfg_path}\n"
            f"Restart=on-failure\nRestartSec=10\nStandardOutput=append:{engine_file(slot_id, '-llama.log')}\n"
            f"StandardError=inherit\n\n[Install]\nWantedBy=default.target\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if load_text(path) != body:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        sh(["systemctl", "--user", "daemon-reload"], timeout=30)
    return unit


def load_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


MODEL_PORTS = (8090, 8290)     # 模型服務可以用的埠；Windows 安裝程式的防火牆規則開同一段（8090-8289）


def _free_port(reg, start=MODEL_PORTS[0]):
    used = set()
    for sl in reg["slots"]:
        p = (slot_config(sl) or {}).get("port") or sl.get("port")
        if p:
            used.add(int(p))
    for p in range(start, MODEL_PORTS[1]):
        if p in used or p == PORT:
            continue
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return None


def _slug(text):
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-")[:80] or "model"


def _alive(slot):
    return unit_state(slot["unit"]) in ("active", "activating") if slot.get("unit") else False


def set_llama_model(slot_id, model_id, ctx=None, force=False, gpus=...):
    """把欄位換成存放區裡的某顆 GGUF（llama.cpp）。欄位原本跑 Strata、另一顆 GGUF、或是空欄位都可以。"""
    reg = registry()
    slot = next((x for x in reg["slots"] if x["id"] == slot_id), None)
    if not slot:
        return 404, {"error": f"沒有這個欄位：{slot_id}"}
    if not SAFE_ID_RX.fullmatch(slot_id):
        return 400, {"error": "欄位 id 只能用英數、底線與連字號，才能替它建 llama.cpp 服務"}
    if slot.get("kind") not in (None, "strata", "llama") or (slot.get("unit") and not slot.get("kind")):
        return 400, {"error": "這個欄位跑的不是語言模型引擎，不能換成 GGUF"}
    _, models, _ = scan_gguf(reg)
    m = next((x for x in models if x["id"] == model_id), None)
    if not m:
        return 400, {"error": "這顆不在存放區掃到的模型清單內"}
    exe = llama_server_exe(reg)
    if not exe:
        return 400, {"error": "這台找不到 llama-server。請先編好 llama.cpp，或在登錄檔 machine.llama_server 指定路徑"}

    hw0 = hardware()
    cards = pools(reg, hw0)["gpus"]
    # gpus 沒帶＝維持原本的綁定；帶 None 或空清單＝自動（全部卡）；帶清單＝只用這幾張
    gpu_ids, err = pick_gpus(slot.get("gpu_ids") if gpus is ... else gpus, cards)
    if err:
        return 400, {"error": err}
    if gpu_ids and not (has_nvidia() or OS == "linux"):
        return 400, {"error": "這台的顯示卡（Windows 的 AMD／Intel，或 Apple）不能指定用哪幾張，請改回自動"}
    if gpu_ids and not has_nvidia():
        try:
            visible_env(gpu_ids)
        except ValueError as ex:
            return 400, {"error": f"{ex}，不能綁卡；請改回自動"}

    old = json.loads(json.dumps(slot))                       # 整個欄位的原樣，還原時直接寫回
    old_unit = slot.get("unit")
    old_enabled = unit_enabled(old_unit) if old_unit else False
    old_alive = _alive(slot)
    old_cfg = slot_config(slot) or {}
    port = old_cfg.get("port") or slot.get("port") or _free_port(reg)
    if not port:
        return 500, {"error": "找不到可用的埠"}

    # 金鑰、輸出上限、自己取的名稱、並行數：跟著欄位走，換引擎也不變
    carry_key, carry_max = "", None
    if slot.get("kind") == "strata" and slot.get("config"):
        sraw = load_json(slot["config"], {}) or {}
        carry_key, carry_max = _first_key(sraw.get("api_key")), sraw.get("max_tokens")
    elif slot.get("kind") == "llama" and slot.get("config"):
        lraw = load_json(slot["config"], {}) or {}
        carry_key, carry_max = llama_key(lraw), lraw.get("max_tokens")
    want_ctx = int(ctx or old_cfg.get("max_context") or 32768)
    if m.get("ctx_train"):
        want_ctx = min(want_ctx, int(m["ctx_train"]))
    want_ctx = max(2048, want_ctx)
    par = int(old_cfg.get("parallel") or 1)
    alias = slot.get("api_model_name") or _slug(f"{m['name']}-{m['quant']}" if m.get("quant") else m["name"])
    os.makedirs(ENGINE_DIR, mode=0o700, exist_ok=True)
    cfg_path = engine_file(slot_id, "-llama.json")
    prev_llama_raw = load_json(cfg_path, None)
    prev_key = load_text(engine_file(slot_id, ".key"))
    keyfile = write_llama_key(slot_id, carry_key)
    # 獨立顯卡放不下的 MoE：自動把前幾層的專家搬到系統記憶體（使用者不必自己調 --n-cpu-moe）
    # 舊模型釋出的顯存只算新綁定也會用到的那幾張卡：從卡 0 換到卡 1，卡 0 空出來的對卡 1 沒有幫助
    back = 0.0
    if old_unit and old_alive and cards:
        split = card_split(_usage_reclaim(old)[0], old.get("gpu_ids"), cards)
        back = sum(v for cid, v in split.items() if not gpu_ids or cid in [str(g) for g in gpu_ids])
    cpu_moe = auto_cpu_moe(m, reg["machine"], cards, gpu_ids, want_ctx, par, back)
    raw = {"engine": "llama", "exe": exe, "model": m["path"], "mmproj": m.get("mmproj"),
           "host": "0.0.0.0", "port": int(port), "ctx": want_ctx, "parallel": par, "ngl": 999,
           "alias": alias, "max_tokens": carry_max if carry_max and carry_max < want_ctx else None,
           "api_key_file": keyfile, "gpus": gpu_ids, "cpu_moe": cpu_moe or None}
    save_json(cfg_path, raw)
    os.chmod(cfg_path, 0o600)
    unit = ensure_llama_unit(slot_id, cfg_path)
    est = llama_est(m, reg["machine"], want_ctx, par, cpu_moe)

    engines = dict(slot.get("engines") or {})
    if slot.get("kind") == "strata":
        engines["strata"] = {"unit": old_unit, "config": slot.get("config")}

    def put(fields):
        r = registry()
        for sl in r["slots"]:
            if sl["id"] == slot_id:
                sl.clear()
                sl.update(fields)
        save_json(REGISTRY, r)

    fields = {k: v for k, v in old.items() if k != "gpu_ids"}
    if gpu_ids:
        fields["gpu_ids"] = gpu_ids
    put({**fields, "kind": "llama", "unit": unit, "config": cfg_path, "engines": engines,
         "port": int(port), "est_vram_gib": est["vram"], "est_ram_gib": est["ram"],
         # 跟模型有關的能力（看圖）每次依新模型重算，不沿用上一顆的
         "caps": sorted((set(old.get("caps") or []) - {"看圖"}) | {"文字"} | ({"看圖"} if m.get("vision") else set())),
         "model": m["name"], "quant": m.get("quant"), "cpu_moe": cpu_moe or None})

    def undo(msg, code=500, extra=None):
        if old_unit != unit:
            sysctl(unit, "stop")
            sysctl(unit, "disable", timeout=30)
        else:
            # 同一個服務：試的時候強制 enable 並重啟過，要照原本的狀態還原，不能一律留著開
            if not old_enabled:
                sysctl(unit, "disable", timeout=30)
            if not old_alive:
                sysctl(unit, "stop")
        if prev_llama_raw is not None:
            save_json(cfg_path, prev_llama_raw)
        if prev_key is not None:
            write_llama_key(slot_id, prev_key.strip())
        put(old)
        back_ok = True
        if old_unit:
            if old_enabled:
                sysctl(old_unit, "enable", timeout=30)
            if old_alive:
                sysctl(old_unit, "reset-failed", timeout=20)
                sysctl(old_unit, "restart")
                back_ok, _ = wait_healthy(old, seconds=600)
        return code, {"error": msg + ("，已換回原本的設定" if old_unit else "，欄位維持空的")
                      + ("" if back_ok else "，但原本的服務還沒起來，請查記錄"),
                      "reverted": True, "restored_ok": back_ok, **(extra or {})}

    # 同一個服務（GGUF 換 GGUF）：admit 看得到這個服務開著，會自己把它占的量算回，這裡不能再加一次。
    # 跨引擎：舊服務是另一個 unit，admit 看不到，要另外算回
    ov, orr = _usage_reclaim(old) if old_unit and old_unit != unit and old_alive else (0.0, 0.0)
    # 逐張卡試算時，舊模型釋放的量要還給它「原本」用的卡，不是新綁的卡
    old_v = _usage_reclaim(old)[0] if old_unit and old_alive else 0.0
    verdict = admit(slot_id, extra_free=(ov, orr), reclaim_cards=card_split(old_v, old.get("gpu_ids"), cards))
    if not verdict["allow"] and not force:
        return undo("記憶體不夠", 409, {"admit": verdict})

    if old_unit and old_unit != unit:
        sysctl(old_unit, "stop")
        if old_enabled:
            sysctl(old_unit, "disable", timeout=30)      # 開機自啟一起換，不然重開機兩個引擎搶同一個埠
    sysctl(unit, "reset-failed", timeout=20)
    sysctl(unit, "enable", timeout=30)
    sysctl(unit, "restart")
    new_slot = next(x for x in registry()["slots"] if x["id"] == slot_id)
    ok, why = wait_healthy(new_slot, seconds=900)        # 幾十 GB 的權重從 NVMe 讀要幾分鐘
    if ok:
        st, data, _ = _http_json(f"http://127.0.0.1:{int(port)}/v1/models", key=carry_key, timeout=10)
        ids = [x.get("id") for x in (data or {}).get("data") or []]
        if st != 200 or alias not in ids:
            ok, why = False, f"/v1/models 回 {st}，模型清單 {ids}"
    if not ok:
        tail = load_text(engine_file(slot_id, "-llama.log")) or ""
        hint = next((ln for ln in reversed(tail.splitlines()) if "error" in ln.lower() or "failed" in ln.lower()), "")
        return undo(f"{m['name']} 載不起來（{why}{'；' + hint[-200:] if hint else ''}）")

    r = registry()
    for sl in r["slots"]:
        if sl["id"] == slot_id:
            sl.pop("api_last_test", None)
    r["measure"].pop(slot_id, None)
    save_json(REGISTRY, r)
    return 200, {"ok": True, "engine": "llama", "model": m["name"], "quant": m.get("quant"),
                 "api_model_name": alias, "port": int(port), "ctx": want_ctx,
                 "switched_engine": old.get("kind") not in (None, "llama"),
                 "kv_gib": est["kv"], "est_vram_gib": est["vram"], "gpus": gpu_ids,
                 "cpu_moe": cpu_moe, "moved_gib": est["moved_gib"]}


def _usage_reclaim(old_slot):
    """切換引擎時，舊引擎占的量要算成「會被釋放」。用舊欄位的原樣去量。"""
    reg = registry()
    hw = hardware()
    for sl in reg["slots"]:
        sl["_busy_cache"] = _alive(sl)
    probe = dict(old_slot)
    st = slot_state(probe, reg, hw)
    return (float(st["vram_gib"] or 0), float(st["ram_gib"] or 0)) if st["busy"] else (0.0, 0.0)


def back_to_strata(slot_id, pack, native, force=False):
    """欄位現在跑 llama.cpp，要換回 Strata 家族的某顆：先切回 Strata 的服務，再走原本的 Strata 換模型流程；
    任何一步失敗就整組換回 llama.cpp。"""
    reg = registry()
    slot = next((x for x in reg["slots"] if x["id"] == slot_id), None)
    eng = (slot or {}).get("engines", {}).get("strata")
    if not slot or slot.get("kind") != "llama" or not eng:
        return 400, {"error": "這個欄位沒有記錄原本的 Strata 服務，無法切回"}
    old = json.loads(json.dumps(slot))
    lunit = slot["unit"]
    lraw = load_json(slot["config"], {}) or {}
    key, mt = llama_key(lraw), lraw.get("max_tokens")
    l_enabled = unit_enabled(lunit)
    lv, lr = _usage_reclaim(old) if _alive(old) else (0.0, 0.0)

    def put(fields):
        r = registry()
        for sl in r["slots"]:
            if sl["id"] == slot_id:
                sl.clear()
                sl.update(fields)
        save_json(REGISTRY, r)

    put({**old, "kind": "strata", "unit": eng["unit"], "config": eng["config"],
         "caps": sorted(set(old.get("caps") or []) - {"看圖"})})      # Strata 這一家目前只有文字
    # Strata 設定檔要帶上欄位目前的金鑰與輸出上限（set_model 會沿用設定檔裡的這兩項）
    sraw = load_json(eng["config"], {}) or {}
    sold = dict(sraw)
    if key:
        sraw["api_key"] = key
    else:
        sraw.pop("api_key", None)
    if mt:
        sraw["max_tokens"] = mt
    else:
        sraw.pop("max_tokens", None)          # 使用者在 llama.cpp 那邊清掉了上限，不要讓 Strata 的舊值復活
    save_json(eng["config"], sraw)
    os.chmod(eng["config"], 0o600)
    sysctl(lunit, "stop")
    if l_enabled:
        sysctl(lunit, "disable", timeout=30)
    sysctl(eng["unit"], "enable", timeout=30)
    code, out = set_model(slot_id, pack, native, force=force, extra_free=(lv, lr))
    if code == 200:
        return code, {**out, "engine": "strata", "switched_engine": True}
    # 換回 llama.cpp
    sysctl(eng["unit"], "stop")
    sysctl(eng["unit"], "disable", timeout=30)
    save_json(eng["config"], sold)
    put(old)
    if l_enabled:
        sysctl(lunit, "enable", timeout=30)
    sysctl(lunit, "reset-failed", timeout=20)
    sysctl(lunit, "restart")
    back_ok, _ = wait_healthy(old, seconds=900)
    out = dict(out)
    out["error"] = (out.get("error") or "切回 Strata 失敗") + "；已換回原本的 llama.cpp 模型" + ("" if back_ok else "，但它還沒起來，請查記錄")
    out["reverted"] = True
    out["restored_ok"] = back_ok
    return code, out


def library():
    """這台的模型庫：Strata 家族＋GGUF 存放區、磁碟剩多少、哪些正在被欄位使用、下載中的工作。"""
    reg = registry()
    # 先讀下載狀態再掃存放區：反過來的話，下載剛好在兩步之間完成，會回「已完成」但清單裡還沒有它
    jobs = downloads_list()
    root, ggufs, problems = scan_gguf(reg)
    in_use = {}
    for sl in reg["slots"]:
        c = slot_config(sl) or {}
        if c.get("_native"):
            in_use[os.path.abspath(c["_native"])] = sl["id"]
        for e in (sl.get("engines") or {}).values():
            ec = strata_config(e.get("config")) if e.get("config") else None
            if ec and ec.get("_native"):
                in_use.setdefault(os.path.abspath(ec["_native"]), sl["id"] + "（停用中的 Strata）")
    for m in ggufs:
        m["in_use"] = in_use.get(os.path.abspath(m["path"]))
    strata = []
    sslot = next((s for s in reg["slots"] if s.get("kind") == "strata" or (s.get("engines") or {}).get("strata")), None)
    if sslot:
        probe = dict(sslot)
        if probe.get("kind") != "strata":
            probe.update(kind="strata", **probe["engines"]["strata"])
        cat = catalog_for(probe, reg)
        for m in cat.get("models", []):
            strata.append({"engine": "strata", "id": m["id"], "name": pretty_model(m["native"])[0],
                           "quant": pretty_model(m["native"])[1], "folder": m["name"], "size_gib": m["size_gib"],
                           "ready": m["ready"], "in_use": in_use.get(os.path.abspath(m["native"])),
                           "cn_origin": True, "arch": "qwen3.8-flash-next"})
    os.makedirs(root, exist_ok=True)
    free_gib, total_gib = disk_free(root)
    real = os.path.realpath(root)
    store = max((x for x in storage_choices() if real == x["mount"] or real.startswith(x["mount"].rstrip("/") + "/")),
                key=lambda x: len(x["mount"]), default=None)
    return {"root": root, "free_gib": free_gib, "hdd": (store or {}).get("hdd"),
            "total_gib": total_gib,
            "gguf": ggufs, "strata": strata, "problems": problems,
            "llama_server": llama_server_exe(reg), "downloads": jobs}


# ---- 下載（Hugging Face）

_DL = {}
_DL_LOCK = threading.Lock()


def _dl_save():
    with _DL_LOCK:
        rows = [{k: v for k, v in j.items() if not k.startswith("_")} for j in _DL.values()]
    save_json(DOWNLOADS_FILE, rows[-50:])


def downloads_list():
    if not _DL:
        for j in load_json(DOWNLOADS_FILE, []) or []:
            if j.get("state") in ("running", "queued"):
                j["state"] = "interrupted"                 # 代理重啟過；.part 還在，重新下載會接續
            _DL[j["id"]] = j
    with _DL_LOCK:
        return sorted(({k: v for k, v in j.items() if not k.startswith("_")} for j in _DL.values()),
                      key=lambda j: -j["created"])[:20]


def _hf_file_url(repo, path):
    """Hugging Face 檔案網址。repo 已比對過格式、path 取自它自己的檔案清單；這裡再逐段編碼，
    任何字元都只能留在路徑裡，不能改到主機或查詢字串。"""
    if not HF_REPO_RX.fullmatch(repo) or ".." in path.split("/"):
        raise ValueError("Hugging Face 名稱或檔名不合法")
    q = urllib.parse.quote
    return f"{HF_BASE}/{q(repo, safe='/')}/resolve/main/{q(path, safe='/')}"


class _HFRedirect(urllib.request.HTTPRedirectHandler):
    """Hugging Face 下載會轉到它自己的 CDN；轉到 Hugging Face 以外的網域時，權杖不跟著送過去。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        host = (urllib.parse.urlparse(newurl).hostname or "").lower()
        if new is not None and not (host == "huggingface.co" or host.endswith((".huggingface.co", ".hf.co")) or host == "hf.co"):
            new.remove_header("Authorization")
        return new


_HF_OPENER = urllib.request.build_opener(_HFRedirect)


def _hf_get(url, token="", rng=None, timeout=30):
    h = {"User-Agent": "xconsole-agent"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if rng:
        h["Range"] = f"bytes={rng}-"
    return _HF_OPENER.open(urllib.request.Request(url, headers=h), timeout=timeout)


def hf_files(repo, token=""):
    """列出 Hugging Face 上這個 repo 的 GGUF，依量化分組，並估每組放不放得下。"""
    if not HF_REPO_RX.fullmatch(repo or ""):
        return 400, {"error": "名稱格式是「作者/模型」，例：unsloth/gemma-4-E4B-it-GGUF"}
    try:
        with _hf_get(f"{HF_BASE}/api/models/{urllib.parse.quote(repo, safe='/')}/tree/main?recursive=true", token) as resp:
            tree = json.load(resp)
    except urllib.error.HTTPError as exc:
        msg = {401: "要登入才看得到（請貼 Hugging Face 權杖）", 403: "要先到 Hugging Face 頁面同意授權條款",
               404: "找不到這個 repo"}.get(exc.code, f"Hugging Face 回 {exc.code}")
        return 400, {"error": msg}
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"連不到 Hugging Face：{str(exc)[:120]}"}
    files = [{"path": x["path"], "size": (x.get("lfs") or {}).get("size") or x.get("size") or 0}
             for x in tree if x.get("type") == "file" and x["path"].endswith(".gguf")]
    groups, mmproj = {}, []
    for f in files:
        base = os.path.basename(f["path"])
        if is_mmproj(base):
            mmproj.append(f)
            continue
        stem = SHARD_RX.sub("", base[:-5])
        g = groups.setdefault(f"{os.path.dirname(f['path'])}|{stem}", {"stem": stem, "files": [], "size": 0})
        g["files"].append(f["path"])
        g["size"] += f["size"]
    reg = registry()
    free = disk_free(gguf_root(reg))[0] if os.path.isdir(gguf_root(reg)) else None
    out = []
    for g in groups.values():
        name, quant = pretty_model(g["stem"] + ".gguf")
        gib = round(g["size"] / GIB, 1)
        sub = os.path.dirname(g["files"][0])
        # 推測解碼用的 MTP 草稿頭、放在子資料夾的檔案不是主模型：標出來、排到最後（實際踩到：0.1 GiB 的 MTP
        # 檔排在清單第一列，看起來像最小的量化）
        aux = "mtp" in g["stem"].lower() or (bool(sub) and not SHARD_RX.search(os.path.basename(g["files"][0])[:-5]))
        out.append({"stem": g["stem"], "name": name, "quant": quant, "files": sorted(g["files"]), "dir": sub,
                    "aux": aux, "size_gib": gib, "fits_disk": free is None or gib + 10 < free,
                    "cn_origin": any(c in f"{repo} {name}".lower() for c in CN_ORIGIN)})
    out.sort(key=lambda c: (c["aux"], c["size_gib"]))
    return 200, {"repo": repo, "choices": out, "mmproj": sorted(x["path"] for x in mmproj), "free_gib": free,
                 "mmproj_sizes": {x["path"]: round(x["size"] / GIB, 2) for x in mmproj}}


def _remote_meta(repo, path, token=""):
    """用 Range 只抓檔頭（含張量清單），不下載權重。大詞表模型的檔頭可到十幾 MB：先抓 16 MB，不夠再抓 64 MB。"""
    url = _hf_file_url(repo, path)
    for want in (16 << 20, 64 << 20):
        with _hf_get(url, token, rng="0", timeout=60) as resp:
            cr = resp.headers.get("Content-Range") or ""
            total = int(cr.rsplit("/", 1)[1]) if "/" in cr and cr.rsplit("/", 1)[1].isdigit() \
                else int(resp.headers.get("Content-Length") or 0)
            buf = resp.read(want)
        meta = gguf_meta(io.BytesIO(buf), tensors=True)
        if meta is not None and total:
            return meta, total
        if len(buf) < want:
            break
    return None, 0


def hf_profile(repo, token="", match=""):
    """不下載就知道這顆模型的底細：各量化的大小（hf_files）＋從檔頭讀出的架構、上下文、KV 與 MoE 比例。
    推薦清單靠這個算「放不放得下、多快」。比例各量化差不多，只讀一個 Q4 一帶的量化。"""
    code, info = hf_files(repo, token)
    if code != 200:
        return code, info
    # 一個 repo 放好幾個不同的模型時（例 xGemable 同時有 coder-v2 與 Fable），只留名稱含 match 的那一家
    if match:
        info["choices"] = [c for c in info["choices"] if match.lower() in c["stem"].lower()]
    mains = [c for c in info["choices"] if not c["aux"]]
    if not mains:
        return 400, {"error": f"這個 repo 裡沒有{'檔名含「' + match + '」的' if match else ''} GGUF 主模型檔"}
    pick = next((c for c in mains if (c["quant"] or "").upper().endswith("Q4_K_M")), None) or mains[len(mains) // 2]
    parts = []
    try:
        for f in pick["files"]:
            meta, total = _remote_meta(repo, f, token)
            if not meta:
                return 502, {"error": f"讀不到 {f} 的檔頭"}
            parts.append((meta, total))
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"讀檔頭失敗：{str(exc)[:160]}"}
    prof = gguf_profile(parts[0][0]) or {}
    prof.update(weight_profile(parts) or {})
    prof["cn_origin"] = prof.get("cn_origin") or any(c in repo.lower() for c in CN_ORIGIN)
    card = {}
    try:
        with _hf_get(f"{HF_BASE}/api/models/{urllib.parse.quote(repo, safe='/')}", token, timeout=20) as resp:
            mi = json.load(resp)
        lic = (mi.get("cardData") or {}).get("license") or next(
            (t.split(":", 1)[1] for t in mi.get("tags") or [] if t.startswith("license:")), None)
        card = {"license": lic if isinstance(lic, str) else None, "released": (mi.get("createdAt") or "")[:7] or None}
    except Exception:  # noqa: BLE001
        pass                                    # 授權與日期只是顯示用，拿不到不影響推薦
    return 200, {**info, "profile": prof, "profiled": pick["stem"], "card": card}


def start_download(repo, stem, mmproj=None, token=""):
    code, info = hf_files(repo, token)
    if code != 200:
        return code, info
    pick = next((c for c in info["choices"] if c["stem"] == stem and not c["aux"]), None) \
        or next((c for c in info["choices"] if c["stem"] == stem), None)
    if not pick:
        return 400, {"error": "這個量化不在這個 repo 的檔案清單內"}
    if mmproj and mmproj not in info["mmproj"]:
        return 400, {"error": "這個 mmproj 不在這個 repo 的檔案清單內"}
    need = pick["size_gib"] + (info["mmproj_sizes"].get(mmproj, 0) if mmproj else 0)
    if info["free_gib"] is not None and need + 10 >= info["free_gib"]:
        return 400, {"error": f"磁碟不夠：要 {round(need, 1)} GiB，存放區只剩 {info['free_gib']} GiB（另需保留 10 GiB）"}
    owner, name = repo.split("/", 1)
    # 資料夾帶作者：alice/model-GGUF 與 bob/model-GGUF 同名檔案不能互相蓋掉或被誤認成已下載
    folder = _slug(f"{owner}--{name.removesuffix('-GGUF').removesuffix('-gguf')}")
    dest = inside(gguf_root(), folder)
    os.makedirs(dest, exist_ok=True)
    with _DL_LOCK:
        if any(j["state"] in ("running", "queued") for j in _DL.values()):
            return 409, {"error": "已經有一個下載在進行，一次只下載一個，避免把網路與磁碟塞滿"}
        jid = secrets.token_hex(4)
        files = pick["files"] + ([mmproj] if mmproj else [])
        _DL[jid] = {"id": jid, "repo": repo, "stem": stem, "folder": folder, "files": files,
                    "total": 0, "done": 0, "state": "queued", "created": int(time.time()), "error": None,
                    "_token": token, "_cancel": False}
    threading.Thread(target=_download_job, args=(jid, dest), daemon=True).start()
    return 200, {"ok": True, "id": jid, "folder": folder, "size_gib": pick["size_gib"]}


def _job_files(j, dest):
    """(repo, 檔案路徑, 存到哪)。文字模型整批來自同一個 repo、放同一個資料夾；畫圖模型每個檔各有來源與資料夾。"""
    out = []
    for f in j["files"]:
        if isinstance(f, list):
            repo, path, folder, name = f
            d = inside(dest, folder)
            os.makedirs(d, exist_ok=True)
            out.append((repo, path, inside(d, name)))
        else:
            out.append((j["repo"], f, inside(dest, os.path.basename(f))))
    return out


def _download_job(jid, dest):
    j = _DL[jid]
    j["state"] = "running"
    _dl_save()
    try:
        files = _job_files(j, dest)
        sizes = {}
        for repo, f, _ in files:
            with _hf_get(_hf_file_url(repo, f), j["_token"], timeout=60) as resp:
                sizes[repo, f] = int(resp.headers.get("Content-Length") or 0)
        j["total"] = sum(sizes.values())
        done_before = 0
        for repo, f, out in files:
            if os.path.exists(out) and os.path.getsize(out) == sizes[repo, f]:
                done_before += sizes[repo, f]
                j["done"] = done_before
                continue
            part = out + ".part"
            have = os.path.getsize(part) if os.path.exists(part) else 0
            with _hf_get(_hf_file_url(repo, f), j["_token"],
                         rng=have if have else None, timeout=60) as resp:
                mode = "ab" if have and resp.status == 206 else "wb"
                if mode == "wb":
                    have = 0
                with open(part, mode) as fh:
                    last = 0
                    while True:
                        if j["_cancel"]:
                            raise InterruptedError("已取消")
                        chunk = resp.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
                        j["done"] = done_before + have
                        if time.time() - last > 5:
                            _dl_save()
                            last = time.time()
            if os.path.getsize(part) != sizes[repo, f]:
                raise IOError(f"{os.path.basename(f)} 大小不對（{os.path.getsize(part)}／{sizes[repo, f]}），可能連線中斷，重新下載會接續")
            os.replace(part, out)
            done_before += sizes[repo, f]
        j["state"] = "done"
    except InterruptedError as exc:
        j["state"], j["error"] = "cancelled", str(exc)
    except Exception as exc:  # noqa: BLE001
        j["state"], j["error"] = "failed", str(exc)[:240]
    finally:
        j["_token"] = ""                                   # 權杖只在下載期間放在記憶體
        j["finished"] = int(time.time())
        _dl_save()


def cancel_download(jid):
    j = _DL.get(jid)
    if not j:
        return 404, {"error": "沒有這個下載"}
    j["_cancel"] = True
    return 200, {"ok": True}


def delete_model(model_id):
    """刪除存放區裡的一顆 GGUF（不可復原）。有欄位正在用、或是 Strata 家族，都不准從這裡刪。"""
    reg = registry()
    _, models, problems = scan_gguf(reg)
    m = next((x for x in models if x["id"] == model_id), None)
    broken = next((p for p in problems if p["name"] == model_id), None)
    if not m and not broken:
        return 404, {"error": "存放區沒有這顆"}
    lib = library()
    if m and next((x for x in lib["gguf"] if x["id"] == model_id), {}).get("in_use"):
        return 409, {"error": "有欄位正在使用這顆，先換成別的模型再刪"}
    folder, stem = model_id.split("/", 1)
    root = gguf_root(reg)
    try:
        full = inside(root, folder)
    except ValueError:
        return 400, {"error": "路徑不在存放區內"}
    if os.path.dirname(full) != os.path.realpath(root):
        return 400, {"error": "只能刪存放區第一層的模型資料夾"}
    removed, freed = [], 0
    for f in os.listdir(full):
        base = f[:-5] if f.endswith(".gguf") else f[:-10] if f.endswith(".gguf.part") else None
        if base is not None and SHARD_RX.sub("", base) == stem:
            p = inside(full, f)
            freed += os.path.getsize(p)
            os.remove(p)
            removed.append(f)
    rest = [f for f in os.listdir(full) if f.endswith(".gguf") and not is_mmproj(f)]
    if not rest:
        for f in os.listdir(full):
            if is_mmproj(f) and f.endswith(".gguf"):      # 只刪 mmproj 的 GGUF，同名的其他檔不動
                p = inside(full, f)
                freed += os.path.getsize(p)
                os.remove(p)
                removed.append(f)
        if not os.listdir(full):
            os.rmdir(full)
    return 200, {"ok": True, "removed": removed, "freed_gib": round(freed / GIB, 1)}


# ---------------------------------------------------------------- 准入控制

def pools(reg, hw):
    """依記憶體架構算出這台的池子。這是全機群版的核心差異。"""
    mm = reg["machine"].get("memory_model", "discrete")
    gpu = (hw or {}).get("gpu") or {}
    vram_total = (gpu.get("vram_total") or 0) / GIB
    vram_used = (gpu.get("vram_used") or 0) / GIB
    ram = ram_pool()
    fl = floors(reg["machine"])

    out = {
        "memory_model": mm,
        "swap": swap_pool(),
        "floors": fl,
        # 綁卡靠 CUDA_VISIBLE_DEVICES（NVIDIA）或 KFD 序號換 HIP_VISIBLE_DEVICES（Linux 的 AMD）；
        # Windows 的 AMD／Intel（Vulkan）與 Apple 沒有可靠的對應，不提供綁卡、一律用全部
        "bindable": has_nvidia() or OS == "linux",
        "gpus": [{
            "id": g.get("id"), "name": g.get("name"),
            "total_gib": round((g.get("vram_total") or 0) / GIB, 1),
            "used_gib": round((g.get("vram_used") or 0) / GIB, 1),
            "free_gib": round(((g.get("vram_total") or 0) - (g.get("vram_used") or 0)) / GIB, 1),
            "temp": g.get("temp"), "power": g.get("power"), "use": g.get("use"),
        } for g in (gpu.get("gpus") or [])],
    }

    if mm == "unified_single":
        # 統一記憶體一個池：GPU 與系統搶同一塊，分開畫兩條會誤導人
        out["model_note"] = "統一記憶體，GPU 與系統共用同一個池"
        out["unified"] = {
            "total_gib": ram["total_gib"],
            "used_gib": ram["used_gib"],
            "free_gib": ram["free_gib"],
            "gpu_used_gib": round(vram_used, 1),
            "floor_gib": fl["ram"],
        }
        if OS == "darwin" and vram_total:
            # Apple Silicon：GPU 只能用到統一記憶體的一部分（Metal 的上限），模型超過就載不起來
            out["unified"]["gpu_limit_gib"] = round(vram_total, 1)
        out["ram"] = out["unified"]
    else:
        out["ram"] = {**ram, "floor_gib": fl["ram"]}
        out["vram"] = {
            "total_gib": round(vram_total, 1), "used_gib": round(vram_used, 1),
            "free_gib": round(vram_total - vram_used, 1), "floor_gib": fl["vram"],
        }
        if mm == "unified_split":
            out["model_note"] = ("BIOS 把記憶體切成兩塊：GPU 拿走大的，系統只剩小的。"
                                 "會把整台拖死的是系統這一塊")
        else:
            out["model_note"] = "每張卡各自的顯存，另外一塊系統記憶體"
    return out


def admit(slot_id, reg=None, hw=None, extra_free=None, reclaim_cards=None):
    """這台還塞不塞得下這個欄位的模型。用「載下去之後最壞剩多少」判斷，不是「現在剩多少」。"""
    reg = reg or registry()
    hw = hw if hw is not None else hardware()
    slot = next((s for s in reg["slots"] if s["id"] == slot_id), None)
    if not slot:
        return {"allow": False, "reasons": [f"沒有這個欄位：{slot_id}"]}

    p = pools(reg, hw)
    mm = p["memory_model"]
    fl = p["floors"]
    cfg = slot_config(slot)
    need_vram = float(slot.get("est_vram_gib") or 0)
    need_ram = float(slot.get("est_ram_gib") or 0)

    # 這個欄位自己已經開著的話，重啟會先把它佔的釋放掉，要先把那份加回可用量，
    # 否則「重啟現有模型」永遠會被自己擋住（實際踩到：.26 的主腦佔 64 GiB，
    # 不加回去就算成「剩 32 要 64」而擋下重啟）。
    for sl in reg["slots"]:
        sl.setdefault("_busy_cache",
                      unit_state(sl["unit"]) in ("active", "activating") if sl.get("unit") else False)
    self_state = slot_state(slot, reg, hw) if slot.get("unit") else None
    for sl in reg["slots"]:
        sl.pop("_busy_cache", None)
    reclaim_vram = reclaim_ram = 0.0
    if self_state and self_state["busy"]:
        reclaim_vram = float(self_state["vram_gib"] or 0)
        reclaim_ram = float(self_state["ram_gib"] or 0)
    if extra_free:
        # 跨引擎切換：舊引擎是另一個服務，切換時會先停掉，它占的量也算回可用
        reclaim_vram += float(extra_free[0] or 0)
        reclaim_ram += float(extra_free[1] or 0)

    reasons, warnings, kv_note = [], [], None
    kv = kv_worst_gib(cfg)
    if cfg and kv is not None:
        if cfg["kv_grow"]:
            kv_note = (f"上下文 {cfg['max_context']:,}×{cfg['parallel']}、KV {cfg['kv']} "
                       f"理論要 {kv} GiB，但有 --kv-grow，會向 expert cache 借、不另外預留")
        else:
            need_vram += kv
            kv_note = (f"上下文 {cfg['max_context']:,}×{cfg['parallel']}、KV {cfg['kv']} "
                       f"要先預留 {kv} GiB（沒有 --kv-grow）")

    detail = {}
    if mm == "unified_single":
        free = round(p["unified"]["free_gib"] + max(reclaim_vram, reclaim_ram), 1)
        need = need_vram + need_ram if need_ram else need_vram
        worst = round(free - need, 1)
        detail["unified"] = {"total": p["unified"]["total_gib"], "free": free,
                             "need": round(need, 1), "worst": worst, "floor": fl["ram"]}
        lim = p["unified"].get("gpu_limit_gib")
        if lim and need_vram > lim:
            reasons.append(f"超過 GPU 能用的上限：這顆要約 {need_vram:.0f} GiB，這台 GPU 最多只能用 {lim:.0f} GiB"
                           "（macOS 預設只給 GPU 統一記憶體的 2/3～3/4；可用 sudo sysctl iogpu.wired_limit_mb 調高）")
        if need and worst < fl["ram"]:
            reasons.append(
                f"統一記憶體不足：需要約 {need:.0f} GiB，現在只剩 {free:.0f} GiB，"
                f"載下去會剩 {worst} GiB（低於 {fl['ram']} GiB 底線）。"
                f"這台是統一記憶體，吃爆會把整台拖死，不是只有服務掛掉")
        elif need and worst < fl["ram"] + 4:
            warnings.append(f"可以載，但載完只剩 {worst} GiB，長請求時要留意")
    else:
        vfree = round(p["vram"]["free_gib"] + reclaim_vram, 1)
        rfree = round(p["ram"]["free_gib"] + reclaim_ram, 1)
        wv = round(vfree - need_vram, 1)
        wr = round(rfree - need_ram, 1)
        detail["vram"] = {"total": p["vram"]["total_gib"], "free": vfree,
                          "need": round(need_vram, 1), "worst": wv, "floor": fl["vram"]}
        detail["ram"] = {"total": p["ram"]["total_gib"], "free": rfree,
                         "need": round(need_ram, 1), "worst": wr, "floor": fl["ram"]}
        cards = p["gpus"]
        if mm == "discrete" and len(cards) > 1 and need_vram:
            # 多卡：總量夠不代表每張都夠。模型照卡的大小攤到它用的每張卡，任何一張跌破底線就擋
            share = card_split(need_vram, slot.get("gpu_ids"), cards)
            back = reclaim_cards if reclaim_cards is not None else card_split(reclaim_vram, slot.get("gpu_ids"), cards)
            # 底線是「這個模型總共要留的餘裕」，照卡的大小分到它用的每張卡；每張都留滿一份等於多留好幾倍，
            # 會把本來就跑得好好的跨卡大模型擋掉（中原 2× 48 GB 的 Mistral 128B 實際踩到：重啟被擋）
            floor_c = card_split(fl["vram"], slot.get("gpu_ids"), cards)
            detail["cards"] = []
            for c in cards:
                cid = str(c["id"])
                if cid not in share:
                    continue
                free_c = round(c["free_gib"] + back.get(cid, 0.0), 1)
                worst_c = round(free_c - share[cid], 1)
                detail["cards"].append({"id": cid, "total": c["total_gib"], "free": free_c,
                                        "need": round(share[cid], 1), "worst": worst_c, "floor": round(floor_c[cid], 2)})
                if worst_c < floor_c[cid]:
                    reasons.append(
                        f"卡 {cid} 顯存不足：這張要放約 {share[cid]:.0f} GiB，現在只剩 {free_c:.0f} GiB，"
                        f"載下去會剩 {worst_c} GiB（低於這張分到的 {floor_c[cid]:.1f} GiB 底線）")
        elif need_vram and wv < fl["vram"]:
            reasons.append(
                f"顯存不足：需要約 {need_vram:.0f} GiB，現在只剩 {vfree:.0f} GiB，"
                f"載下去會剩 {wv} GiB（低於 {fl['vram']} GiB 底線）")
        if need_ram and wr < fl["ram"]:
            extra = ("，這台曾因為系統記憶體耗盡整台卡死"
                     if mm == "unified_split" else "")
            reasons.append(
                f"系統記憶體不足：需要約 {need_ram:.0f} GiB，現在只剩 {rfree:.0f} GiB，"
                f"載下去會剩 {wr} GiB（低於 {fl['ram']} GiB 底線{extra}）")
        if not reasons and need_ram and wr < fl["ram"] + 4:
            warnings.append(f"可以載，但載完系統記憶體只剩 {wr} GiB，長請求時要留意")

    return {"allow": not reasons, "slot": slot_id, "memory_model": mm,
            "reasons": reasons, "warnings": warnings, "kv_note": kv_note,
            "restart_of_running": bool(reclaim_vram or reclaim_ram),
            "reclaim": {"vram_gib": round(reclaim_vram, 1), "ram_gib": round(reclaim_ram, 1)},
            "pools": detail}


# ---------------------------------------------------------------- 狀態總成

def full_state():
    reg = registry()
    hw = hardware()
    # 先把每個欄位的死活問一遍並快取，VRAM 分攤要用到「同時有哪些在跑」
    for sl in reg["slots"]:
        sl["_busy_cache"] = (unit_state(sl["unit"]) in ("active", "activating")
                             if sl.get("unit") else False)
    before = json.loads(json.dumps(reg["measure"]))
    slots = [slot_state(s, reg, hw) for s in reg["slots"]]
    for sl in reg["slots"]:
        sl.pop("_busy_cache", None)
    # 只把這一輪新撈到的實測值寫回最新的登錄檔。以前整份寫回：這一輪開始後管理者在網頁上存的設定，
    # 會被這份舊的蓋掉（Codex 審查抓到）
    changed = {k: v for k, v in reg["measure"].items() if before.get(k) != v}
    if changed:
        with REG_LOCK:
            cur = registry()
            live = {sl["id"] for sl in cur["slots"]}
            cur["measure"].update({k: v for k, v in changed.items() if k in live})
            save_json(REGISTRY, cur)

    p = pools(reg, hw)
    # 最壞情況：已開著、但 KV 還沒長滿的模型，若長滿還要再吃多少
    extra = sum(s["kv_worst_gib"] for s in slots
                if s["active"] and s["config"] and not s["config"]["kv_grow"]
                and s["kv_worst_gib"])
    if p["memory_model"] == "unified_single":
        p["unified"]["worst_free_gib"] = round(p["unified"]["free_gib"] - extra, 1)
    else:
        p["vram"]["worst_free_gib"] = round(p["vram"]["free_gib"] - extra, 1)

    cpu = (hw or {}).get("cpu") or {}
    return {
        "ts": time.time(),
        "machine": reg["machine"],
        "chip": chip(reg["machine"]),
        # 總覽的「安裝推理引擎」那一步用：只看檔案在不在，不跑 --version（每 15 秒一次太貴）
        "engine": {"llama_server": bool((reg["machine"].get("llama_server")
                                         and os.path.exists(expand(reg["machine"]["llama_server"])))
                                        or llama_globs()),
                   "building": bool(_BUILD.get("thread_alive")) and _BUILD.get("kind") == "llama",
                   "sd_cli": bool(sd_cli_exe(reg)),
                   "sd_building": bool(_BUILD.get("thread_alive")) and _BUILD.get("kind") == "sd"},
        "slots": slots,
        "profiles": reg["profiles"],
        "pools": p,
        "cpu": {"load": cpu.get("load"), "cores": cpu.get("cores"),
                "usage": cpu.get("usage"), "temp": cpu.get("temp")},
        "gpu": {"use": ((hw or {}).get("gpu") or {}).get("use"),
                "temp": ((hw or {}).get("gpu") or {}).get("temp"),
                "power": ((hw or {}).get("gpu") or {}).get("power"),
                "clock": gpu_clock()},
        "disks": (hw or {}).get("disks") or [],
        "nets": (hw or {}).get("nets") or [],
        "temps": temps(),
        "ports": ports(),
        "uptime_sec": (hw or {}).get("uptime"),
        "hw_source": (hw or {}).get("_source"),
        "hw_error": (hw or {}).get("error"),
    }


# ---------------------------------------------------------------- 動作

def do_control(slot_id, action, force=False):
    reg = registry()
    slot = next((s for s in reg["slots"] if s["id"] == slot_id), None)
    if not slot or not slot.get("unit"):
        return 404, {"error": f"沒有這個欄位或它沒有對應的服務：{slot_id}"}
    if action not in ("start", "stop", "restart"):
        return 400, {"error": f"不支援的動作：{action}"}

    if action in ("start", "restart"):
        verdict = admit(slot_id, reg)
        if not verdict["allow"] and not force:
            return 409, {"error": "擋下來了", "admit": verdict}
    if action in ("stop", "restart") and slot.get("critical") and not force:
        return 409, {"error": f"「{slot['role']}」標記為關鍵服務，要動它請明確確認"}

    rc, out, err = sysctl(slot["unit"], action)
    return (200 if rc == 0 else 500), {
        "ok": rc == 0, "unit": slot["unit"], "action": action,
        "message": (err or out or "已送出").strip()[:400],
    }


def do_profile(name, force=False):
    """套用情境：改設定檔再重啟，失敗自動還原回上一版並重啟。"""
    reg = registry()
    prof = reg["profiles"].get(name)
    if not prof:
        return 404, {"error": f"沒有這個情境：{name}"}
    applied, errors = [], []
    for change in prof.get("changes", []):
        slot = next((s for s in reg["slots"] if s["id"] == change["slot"]), None)
        if not slot or not slot.get("config"):
            errors.append(f"{change['slot']} 沒有可改的設定檔")
            continue
        if slot.get("kind") != "strata":
            errors.append(f"{change['slot']} 現在跑的是 llama.cpp，情境裡的參數是給 Strata 的，這一項略過")
            continue
        cfg = load_json(slot["config"], None)
        if cfg is None:
            errors.append(f"讀不到 {slot['config']}")
            continue
        backup = slot["config"] + f".bak-profile-{int(time.time())}"
        save_json(backup, cfg)
        args = list(cfg.get("args") or [])
        for flag, value in (change.get("args") or {}).items():
            if value is None:
                if flag in args:
                    i = args.index(flag)
                    args.pop(i)
                    if i < len(args) and not args[i].startswith("--"):
                        args.pop(i)
            elif value is True:
                if flag not in args:
                    args.append(flag)
            else:
                if flag in args:
                    args[args.index(flag) + 1] = str(value)
                else:
                    args += [flag, str(value)]
        cfg["args"] = args
        for key, value in (change.get("config") or {}).items():
            cfg[key] = value
        save_json(slot["config"], cfg)
        applied.append({"slot": slot["id"], "backup": backup})

    results = []
    for item in applied:
        code, body = do_control(item["slot"], "restart", force=True)
        results.append({"slot": item["slot"], "code": code, **body})
        if code != 200:
            slot = next(s for s in reg["slots"] if s["id"] == item["slot"])
            cfg = load_json(item["backup"], None)
            if cfg is not None:
                save_json(slot["config"], cfg)
                sysctl(slot["unit"], "restart")
            errors.append(f"{item['slot']} 套用失敗，已還原設定並重啟")

    if not errors:
        reg = registry()
        reg["machine"]["profile"] = name
        save_json(REGISTRY, reg)
    return (200 if not errors else 500), {
        "ok": not errors, "profile": name, "applied": results, "errors": errors}



# ---- 情境：網頁上新增、修改、刪除
# 只開放三項：上下文長度、並行數、自動縮輸出上限。引擎的其他參數可能帶檔案路徑（例如記錄檔），
# 從網頁開放等於能寫任意檔案；情境裡原本就有的其他設定原樣保留，不會被網頁的編輯洗掉。
PROFILE_KEY_RX = re.compile(r"[a-z][a-z0-9_-]{0,31}")


@_locked
def save_profile(body):
    reg = registry()
    key = str(body.get("key") or "").strip()
    if key and key not in reg["profiles"]:
        return 404, {"error": f"沒有這個情境：{key}"}
    name = str(body.get("name") or "").strip()
    why = str(body.get("why") or "").strip()
    if not 1 <= len(name) <= 20 or re.search(r"[\x00-\x1f<>]", name):
        return 400, {"error": "情境名稱要 1～20 個字"}
    if len(why) > 200 or re.search(r"[\x00-\x08\x0b-\x1f]", why):
        return 400, {"error": "說明最多 200 字"}
    strata = {sl["id"] for sl in reg["slots"] if sl.get("kind") == "strata"}
    old = {c["slot"]: c for c in (reg["profiles"].get(key) or {}).get("changes", [])}
    changes = []
    if not isinstance(body.get("changes") or [], list):
        return 400, {"error": "changes 要是清單"}
    for ch in body.get("changes") or []:
        if not isinstance(ch, dict):
            return 400, {"error": "changes 的每一項要是物件"}
        sid = str(ch.get("slot") or "")
        if sid not in strata:
            return 400, {"error": f"{sid} 不是 Strata 欄位，情境只能調 Strata 的參數"}
        c = json.loads(json.dumps(old.get(sid) or {"slot": sid}))
        c.setdefault("args", {})
        c.setdefault("config", {})
        for field, lo, hi in (("max_context", 2048, 1048576), ("parallel", 1, 16)):
            v = ch.get(field)
            if v in (None, ""):
                (c["args"].pop("--max-context", None) if field == "max_context" else c["config"].pop("parallel", None))
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                return 400, {"error": "上下文長度與並行數要填整數"}
            if not lo <= v <= hi:
                return 400, {"error": f"{'上下文長度' if field == 'max_context' else '並行數'}要在 {lo}～{hi}"}
            if field == "max_context":
                c["args"]["--max-context"] = str(v)
            else:
                c["config"]["parallel"] = v
        fm = ch.get("fit_max_tokens")
        if fm is None:
            c["config"].pop("fit_max_tokens", None)
        else:
            c["config"]["fit_max_tokens"] = bool(fm)
        if not c["args"]:
            c.pop("args")
        if not c["config"]:
            c.pop("config")
        if len(c) > 1:
            changes.append(c)
    if not changes:
        return 400, {"error": "至少要調一個欄位的一項參數，否則這個情境套用了也沒有作用"}
    if not key:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        base = base if PROFILE_KEY_RX.fullmatch(base or "") else "profile"
        key = base if base not in reg["profiles"] else next(f"{base}{i}" for i in range(2, 999) if f"{base}{i}" not in reg["profiles"])
    reg["profiles"][key] = {"name": name, "why": why, "changes": changes}
    save_json(REGISTRY, reg)
    return 200, {"ok": True, "key": key, "profile": reg["profiles"][key]}


@_locked
def delete_profile(key):
    reg = registry()
    if key not in reg["profiles"]:
        return 404, {"error": f"沒有這個情境：{key}"}
    reg["profiles"].pop(key)
    if reg["machine"].get("profile") == key:
        reg["machine"].pop("profile")      # 正在用的情境被刪了：服務照原本的設定跑，只是不再標示情境名稱
    save_json(REGISTRY, reg)
    return 200, {"ok": True}


# ---- 在網頁上安裝推理引擎（llama.cpp、stable-diffusion.cpp）
# Linux 在這台編譯（CUDA／ROCm 版本跟著這台的驅動走）：系統套件（git、cmake、編譯器、CUDA／ROCm）要 sudo，網頁不代裝，
# 只檢查並列出要請 IT 裝什麼；其餘（下載原始碼、設定、編譯）由代理在背景做，可以看進度、可以取消。
# macOS／Windows 裝官方預編譯版，核對官方公布的 sha256。版本一律釘在官方最新的正式版編號。
# 兩種引擎共用一把鎖：同時編兩個會把系統記憶體吃光。
LLAMA_REPO = "https://github.com/ggml-org/llama.cpp"
SD_REPO = "https://github.com/leejet/stable-diffusion.cpp"
BUILD_FILE = os.path.join(STATE_DIR, "llama-build.json")
ENGINES = {
    "llama": {"name": "llama.cpp", "repo": LLAMA_REPO, "file": BUILD_FILE, "tag_rx": r"b(\d+)", "glob": "refs/tags/b*",
              "dir": "llama.cpp-{}", "target": "llama-server", "submodules": False, "extra": ["-DLLAMA_CURL=OFF"],
              "cuda": "-DGGML_CUDA=ON", "hip": "-DGGML_HIP=ON", "hip_targets": "AMDGPU_TARGETS",
              "gh": "/repos/ggml-org/llama.cpp/releases?per_page=10"},
    # stable-diffusion.cpp 的正式版標籤是 master-<編號>-<commit 前七碼>；ggml 等是 git 子模組，要一起抓
    "sd": {"name": "stable-diffusion.cpp", "repo": SD_REPO, "file": os.path.join(STATE_DIR, "sd-build.json"),
           "tag_rx": r"master-(\d+)-[0-9a-f]+", "glob": "refs/tags/master-*", "dir": "stable-diffusion.cpp-{}",
           "target": "sd-cli", "submodules": True, "extra": [],
           "cuda": "-DSD_CUDA=ON", "hip": "-DSD_HIPBLAS=ON", "hip_targets": "GPU_TARGETS",
           "gh": "/repos/leejet/stable-diffusion.cpp/releases?per_page=10"},
}
_BUILD = {"proc": None, "cancel": False, "kind": None}
_BUILD_LOCK = threading.Lock()


def _nvcc():
    return shutil.which("nvcc") or next((p for p in ("/usr/local/cuda/bin/nvcc",) if os.access(p, os.X_OK)), None)


def _hipcc():
    return shutil.which("hipcc") or next((p for p in ("/opt/rocm/bin/hipcc",) if os.access(p, os.X_OK)), None)


def cuda_arch(nvcc):
    """要編給哪個 CUDA 架構：顯示卡的運算能力；編譯器不認得（例：CUDA 12.4 碰上 sm_120）就退到它支援的最高架構，
    執行時由驅動即時轉換（中原 2× RTX PRO 5000 就是這樣跑的）。"""
    rc, out, _ = sh(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"], timeout=15)
    caps = sorted({int(x.strip().replace(".", "")) for x in out.splitlines() if re.fullmatch(r"\d+\.\d+", x.strip())}) if rc == 0 else []
    _, o2, _ = sh([nvcc, "--list-gpu-arch"], timeout=15)
    known = sorted({int(m) for m in re.findall(r"compute_(\d+)", o2)})
    if not caps:
        return "native", "讀不到顯示卡的運算能力，交給編譯器自己偵測"
    want = caps[-1]
    if not known or want in known:
        return ";".join(str(c) for c in caps), f"照顯示卡編：sm_{want}"
    best = max((k for k in known if k <= want), default=None)
    if best is None:
        return "native", "編譯器太舊，交給它自己偵測"
    return str(best), f"這版 CUDA 編譯器不認得 sm_{want}，改編 sm_{best}，執行時由驅動即時轉換（第一次載入會慢一點）"


def build_prereqs(kind="llama"):
    eng = ENGINES[kind]
    if OS != "linux":
        variant, _, why = prebuilt_variant(kind)
        return {"mode": "prebuilt", "tools": {}, "missing": [], "apt": "", "backend": variant or "none", "flags": [], "env": {},
                "notes": [f"這台是 {'macOS' if OS == 'darwin' else 'Windows'}，直接下載 {eng['name']} 官方預編譯版：{why}。"
                          "下載後核對官方公布的 sha256，對不上就不裝"], "jobs": 0}
    cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    tools = {"git": bool(shutil.which("git")), "cmake": bool(shutil.which("cmake")), "C++ 編譯器": bool(cxx)}
    apt = {"git": "git", "cmake": "cmake", "C++ 編譯器": "build-essential"}
    missing = [apt[k] for k, ok in tools.items() if not ok]
    notes, backend, flags, env = [], "cpu", [], {}
    if has_nvidia():
        nv = _nvcc()
        if nv:
            arch, why = cuda_arch(nv)
            backend, flags, env = "cuda", [eng["cuda"], f"-DCMAKE_CUDA_ARCHITECTURES={arch}"], {"PATH": os.path.dirname(nv) + ":" + os.environ.get("PATH", "")}
            notes.append(why)
        else:
            notes.append("有 NVIDIA 顯示卡但沒有 CUDA 編譯器（nvcc），會編成只用 CPU。要用 GPU 請 IT 安裝 CUDA Toolkit（nvidia-cuda-toolkit 或 NVIDIA 官方套件）")
    elif probe_amd_sysfs():
        hc = _hipcc()
        if hc:
            _, ri, _ = sh(["rocminfo"], timeout=30)
            gfx = sorted(set(re.findall(r"Name:\s+(gfx\w+)", ri)))
            # hipcc 常是 /usr/bin/hipcc → /etc/alternatives → /opt/rocm-x.y/bin/hipcc 的符號連結：要追到底才找得到 ROCm 根目錄
            # （.27 實際踩到：照 /usr/bin 推成 /usr，HIPCXX=/usr/llvm/bin/clang 不存在，cmake 設定就失敗）
            rocm = os.path.dirname(os.path.dirname(os.path.realpath(hc)))
            backend = "hip"
            flags = [eng["hip"]] + ([f"-D{eng['hip_targets']}={';'.join(gfx)}"] if gfx else [])
            env = {"HIPCXX": os.path.join(rocm, "llvm/bin/clang"), "HIP_PATH": rocm}
            notes.append(f"用 ROCm 編，目標 {', '.join(gfx) or '自動偵測'}")
        else:
            notes.append("有 AMD 顯示卡但沒有 ROCm（hipcc），會編成只用 CPU。要用 GPU 請 IT 安裝 ROCm")
    else:
        notes.append("沒有偵測到 GPU，編成只用 CPU")
    return {"tools": tools, "missing": missing, "apt": ("sudo apt install " + " ".join(missing)) if missing else "",
            "backend": backend, "flags": flags, "env": env, "notes": notes, "jobs": max(1, (os.cpu_count() or 2) // 2)}


def build_progress(kind="llama"):
    """編譯進度（輪詢用，便宜）：工作狀態與記錄最後 40 行。代理重啟過的話，標成中斷。"""
    st = load_json(ENGINES[kind]["file"], {}) or {}
    mine = _BUILD.get("thread_alive") and _BUILD.get("kind") == kind
    if st.get("state") == "running" and not mine:
        st.update(state="interrupted", error="代理重啟過，編譯中斷；再按一次會接著下載好的原始碼繼續")
    log = st.get("log")
    return {"job": st, "running": bool(mine), "other": (_BUILD.get("kind") if _BUILD.get("thread_alive") and not mine else None),
            "log_tail": tail_lines(log, 40) if log and os.path.exists(log) else []}


def build_status(kind="llama"):
    """打開編譯視窗時看一次：工具齊不齊、會用哪種加速、已經有哪些編好的執行檔。"""
    pre = build_prereqs(kind)
    return {**build_progress(kind), "prereqs": {k: v for k, v in pre.items() if k != "env"},
            "installed": llama_candidates() if kind == "llama" else sd_candidates()}


def _build_run(st, pre, log, kind="llama"):
    eng = ENGINES[kind]

    def step(phase, argv, cwd=None, timeout=7200):
        st["phase"] = phase
        save_json(eng["file"], st)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {phase} =====\n")
            fh.flush()
            proc = subprocess.Popen([str(a) for a in argv], cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                                    env={**os.environ, **pre["env"]}, start_new_session=True)
            with _BUILD_LOCK:
                _BUILD["proc"] = proc
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = -9
            with _BUILD_LOCK:
                _BUILD["proc"] = None
        if _BUILD["cancel"]:
            raise RuntimeError("已取消")
        if rc != 0:
            raise RuntimeError(f"{phase}失敗（結束碼 {rc}），請看下面的記錄")

    dest = st["dest"]
    sub = ["--recurse-submodules", "--shallow-submodules"] if eng["submodules"] else []
    try:
        if not os.path.isdir(os.path.join(dest, ".git")):
            step("下載原始碼", ["git", "clone", "--depth", "1", *sub, "--branch", st["tag"], eng["repo"], dest])
        else:
            # 資料夾已經在（上次中斷）：先還原成乾淨的那一版，被改過的檔案與舊的 build 一律清掉，再核對 commit
            step("還原原始碼", ["git", "-C", dest, "reset", "--hard", "--quiet", st["commit"]])
            step("清掉舊檔", ["git", "-C", dest, "clean", "-xfdq"])
            if eng["submodules"]:
                step("還原子模組", ["git", "-C", dest, "submodule", "update", "--init", "--recursive", "--depth", "1", "--force"])
        # 編譯之前核對原始碼就是官方那個版本：資料夾早就存在（上次中斷、或別人放的）時也一樣要對得上
        rc, head, _ = sh(["git", "-C", dest, "rev-parse", "HEAD"], timeout=30)
        if rc != 0 or head.strip() != st["commit"]:
            raise RuntimeError(f"{dest} 的原始碼跟官方 {st['tag']} 對不上，不編譯。請把這個資料夾移走再按一次")
        step("設定", ["cmake", "-S", dest, "-B", os.path.join(dest, "build"), "-DCMAKE_BUILD_TYPE=Release",
                      *eng["extra"], *pre["flags"]])
        step("編譯", ["cmake", "--build", os.path.join(dest, "build"), "--config", "Release", "-j", pre["jobs"],
                      "--target", eng["target"]])
        exe = os.path.join(dest, "build", "bin", eng["target"])
        rc, out, err = sh([exe, "--version"] if kind == "llama" else [exe, "--help"], timeout=30)
        if rc != 0:
            raise RuntimeError(f"編好了但跑不起來（{'--version' if kind == 'llama' else '--help'} 失敗），可能缺 GPU 函式庫")
        st.update(state="done", phase="完成", exe=exe)
    except Exception as ex:  # noqa: BLE001
        st.update(state="cancelled" if _BUILD["cancel"] else "failed", error=str(ex)[:300])
    st["ended"] = time.time()
    save_json(eng["file"], st)
    with _BUILD_LOCK:
        _BUILD["thread_alive"] = False


# ---- macOS／Windows：裝官方預編譯版（不必裝編譯工具）。Linux 照舊在這台編譯（CUDA／ROCm 版本跟著這台的驅動走）
GH_API = os.environ.get("XCONSOLE_GH_API", "https://api.github.com")      # 測試時指到本機假站


def prebuilt_variant(kind="llama"):
    """這台該裝哪一版預編譯：(變體, 另外要下載的執行階段檔名或 None, 給人看的理由)。Linux 回 None（在這台編譯）。
    變體為 None 代表官方沒有這台能用的版本（理由寫在第三個）。"""
    arm = platform_machine() in ("arm64", "aarch64")
    if kind == "sd" and OS in ("darwin", "windows"):
        # stable-diffusion.cpp 官方只出 Apple Silicon 與 Windows x64（CPU／CUDA 12／Vulkan）
        if OS == "darwin":
            return ("macos-arm64", None, "Apple Silicon，用 Metal 加速") if arm else (None, None, "Intel Mac 沒有官方預編譯版")
        if arm:
            return None, None, "Windows on ARM 沒有官方預編譯版"
        if has_nvidia():
            return "win-cuda12-x64", "cudart-sd-bin-win-cu12-x64.zip", "NVIDIA 顯示卡，用 CUDA 12"
        if probe_win_adapters():
            return "win-vulkan-x64", None, "AMD／Intel 顯示卡，用 Vulkan 加速"
        return "win-cpu-x64", None, "沒有偵測到 GPU，只用 CPU"
    if OS == "darwin":
        return ("macos-arm64", None, "Apple Silicon，用 Metal 加速") if arm else ("macos-x64", None, "Intel Mac，只用 CPU")
    if OS != "windows":
        return None
    a = "arm64" if arm else "x64"
    if has_nvidia():
        _, out, _ = sh(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], timeout=15)
        m = re.match(r"(\d+)", out.strip())
        drv = int(m.group(1)) if m else 0
        cu = "13.4" if drv >= 580 or arm else "12.4"      # CUDA 13 要 580 版以上的驅動
        return f"win-cuda-{cu}-{a}", f"cudart-llama-bin-win-cuda-{cu}-{a}.zip", f"NVIDIA 顯示卡，用 CUDA {cu}（驅動 {drv or '未知'}）"
    if probe_win_adapters():
        return f"win-vulkan-{a}", None, "AMD／Intel 顯示卡，用 Vulkan 加速"
    return f"win-cpu-{a}", None, "沒有偵測到 GPU，只用 CPU"


def _gh_json(path):
    req = urllib.request.Request(GH_API + path, headers={"User-Agent": "xconsole-agent", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def pick_release(releases, variant, extra, kind="llama"):
    """最新一版正式版裡有這個變體的：回 (版本, 主檔 asset, 執行階段 asset 或 None)。每個檔都要有官方 sha256。
    llama.cpp：bNNNN、llama-bNNNN-bin-<變體>；stable-diffusion.cpp：master-NNN-<commit>、sd-master-<commit>-bin-<變體>.zip
    （macOS 的檔名帶系統版本，例 Darwin-macOS-26.6.2-arm64）。"""
    ext = ".zip" if variant.startswith("win-") or kind == "sd" else ".tar.gz"
    for rel in releases:
        tag = rel.get("tag_name") or ""
        m = re.fullmatch(r"master-\d+-([0-9a-f]+)" if kind == "sd" else r"b\d+", tag)
        if rel.get("draft") or not m:
            continue
        assets = {a["name"]: a for a in rel.get("assets") or [] if str(a.get("digest") or "").startswith("sha256:")}
        if kind == "sd":
            want = r"Darwin-macOS-[\d.]+-arm64" if variant == "macos-arm64" else re.escape(variant)
            main_a = next((a for n, a in assets.items() if re.fullmatch(rf"sd-master-{m.group(1)}-bin-{want}\.zip", n)), None)
        else:
            main_a = assets.get(f"llama-{tag}-bin-{variant}{ext}")
        if main_a and (not extra or extra in assets):
            return tag, main_a, assets.get(extra) if extra else None
    return None, None, None


def _safe_extract(archive, dest):
    """解壓縮，任何一個檔案會落到 dest 外面（../、絕對路徑、符號連結）就整個拒絕。"""
    import tarfile
    import zipfile
    root = os.path.realpath(dest)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                if not os.path.realpath(os.path.join(root, n)).startswith(root + os.sep):
                    raise RuntimeError(f"壓縮檔裡有不安全的路徑：{n}")
            z.extractall(root)
        return
    with tarfile.open(archive) as t:
        ok = []
        for mem in t.getmembers():
            inside_root = os.path.realpath(os.path.join(root, mem.name)).startswith(root + os.sep)
            # 同資料夾內的函式庫版本連結（libggml.0.dylib → libggml.dylib）可以；其他連結、裝置檔一律拒絕
            same_dir_link = mem.issym() and not os.path.isabs(mem.linkname) and ".." not in mem.linkname.split("/") \
                and "/" not in mem.linkname
            if not inside_root or not (mem.isfile() or mem.isdir() or same_dir_link):
                raise RuntimeError(f"壓縮檔裡有不安全的路徑：{mem.name}")
            ok.append(mem)
        # 只解開逐一檢查過的成員；Python 有 data 過濾器（3.12 起，3.9.17 起也有）就再加一道
        if hasattr(tarfile, "data_filter"):
            t.extractall(root, members=ok, filter="data")
        else:
            t.extractall(root, members=ok)


def _prebuilt_run(st, log, kind="llama"):
    eng = ENGINES[kind]

    def note(text):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def fetch(asset, path, base_pct, span):
        import hashlib
        h = hashlib.sha256()
        req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "xconsole-agent"})
        total, done, last = int(asset.get("size") or 0), 0, -1
        with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as fh:
            while True:
                if _BUILD["cancel"]:
                    raise RuntimeError("已取消")
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                pct = base_pct + int(span * done / total) if total else base_pct
                if pct != last:
                    note(f"[{pct:3d}%] 下載 {asset['name']}　{done / 1048576:.0f} / {total / 1048576:.0f} MB")
                    last = pct
        if "sha256:" + h.hexdigest() != asset["digest"]:
            raise RuntimeError(f"{asset['name']} 的 sha256 跟官方公布的不一樣，已刪除，不安裝")

    dest = st["dest"]
    tmp = dest + ".part"
    try:
        st["phase"] = "下載"
        save_json(eng["file"], st)
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        arc = os.path.join(tmp, st["asset"]["name"])
        span = 80 if st.get("extra") else 90
        fetch(st["asset"], arc, 0, span)
        st["phase"] = "核對與解壓縮"
        save_json(eng["file"], st)
        out_dir = os.path.join(tmp, "out")
        os.makedirs(out_dir)
        _safe_extract(arc, out_dir)
        if st.get("extra"):
            arc2 = os.path.join(tmp, st["extra"]["name"])
            fetch(st["extra"], arc2, span, 10)
            _safe_extract(arc2, out_dir)
        # macOS 的壓縮檔多包一層 llama-bNNNN/，拿掉讓執行檔直接在資料夾裡
        inner = [x for x in os.listdir(out_dir) if not x.startswith(".")]
        src = os.path.join(out_dir, inner[0]) if len(inner) == 1 and os.path.isdir(os.path.join(out_dir, inner[0])) else out_dir
        exe = os.path.join(src, eng["target"] + EXE)
        if not os.path.isfile(exe):
            raise RuntimeError(f"壓縮檔裡找不到 {eng['target']}，官方的檔案結構可能改了")
        if OS != "windows":
            os.chmod(exe, 0o755)
        flag = "--version" if kind == "llama" else "--help"
        rc, out, err = sh([exe, flag], timeout=60, env=llama_launch_env(exe) if kind == "llama" else None)
        if rc != 0:
            raise RuntimeError(f"裝好了但跑不起來（{flag} 失敗）：{(err or out)[-200:]}")
        note(f"[100%] 完成：{(out + err).splitlines()[-1] if kind == 'llama' and (out + err) else exe}")
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.replace(src, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        st.update(state="done", phase="完成", exe=os.path.join(dest, eng["target"] + EXE))
    except Exception as ex:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        st.update(state="cancelled" if _BUILD["cancel"] else "failed", error=str(ex)[:300])
    st["ended"] = time.time()
    save_json(eng["file"], st)
    with _BUILD_LOCK:
        _BUILD["thread_alive"] = False


def start_prebuilt(kind="llama"):
    eng = ENGINES[kind]
    variant, extra, why = prebuilt_variant(kind)
    if not variant:
        return 400, {"error": f"{why}，{eng['name']} 在這台只能自己編譯後把 {eng['target']} 放進家目錄"}
    try:
        tag, asset, extra_a = pick_release(_gh_json(eng["gh"]), variant, extra, kind)
    except Exception as ex:  # noqa: BLE001
        return 502, {"error": f"連不到 GitHub 取 {eng['name']} 的版本清單（{str(ex)[:120]}）；這台要能連外，或把 {eng['target']} 複製進家目錄"}
    if not tag:
        return 502, {"error": f"最近幾版 {eng['name']} 都沒有 {variant} 的預編譯檔"}
    with _BUILD_LOCK:
        if _BUILD.get("thread_alive"):
            return 409, {"error": "已經在安裝了" if _BUILD.get("kind") == kind
                         else f"這台正在安裝 {ENGINES[_BUILD['kind']]['name']}，等它裝完再裝"}
        dest = inside(HOME, eng["dir"].format(f"{tag}-{variant}"))
        os.makedirs(ENGINE_DIR, mode=0o700, exist_ok=True)
        log = engine_file("build", f"-{kind}-build.log")
        open(log, "w").close()
        st = {"state": "running", "phase": "準備", "tag": tag, "backend": variant, "dest": dest, "log": log,
              "mode": "prebuilt", "why": why, "asset": asset, "extra": extra_a, "started": time.time()}
        save_json(eng["file"], st)
        _BUILD.update(cancel=False, thread_alive=True, kind=kind)
    threading.Thread(target=_prebuilt_run, args=(st, log, kind), daemon=True).start()
    return 200, {"ok": True, "tag": tag, "backend": variant}


def parse_tag_refs(out, rx=r"b(\d+)"):
    """git ls-remote 的正式版標籤 → {編號: (標籤, commit)}。附註式標籤要取 ^{} 那一行（指向的 commit），不是標籤物件本身。"""
    refs, peeled = {}, {}
    for h, tag, n, pe in re.findall(r"^([0-9a-f]{40})\s+refs/tags/(" + rx + r")(\^\{\})?$", out, re.M):
        (peeled if pe else refs)[int(n)] = (tag, h)
    return {**refs, **peeled}


def start_build(kind="llama"):
    if OS != "linux":
        return start_prebuilt(kind)
    eng = ENGINES[kind]
    with _BUILD_LOCK:
        if _BUILD.get("thread_alive"):
            return 409, {"error": "已經在編譯了" if _BUILD.get("kind") == kind
                         else f"這台正在編譯 {ENGINES[_BUILD['kind']]['name']}，等它編完再編（同時編兩個會把記憶體吃光）"}
        pre = build_prereqs(kind)
        if pre["missing"]:
            return 400, {"error": f"這台缺少編譯工具，請 IT 先執行：{pre['apt']}", "missing": pre["missing"]}
        rc, out, _ = sh(["git", "ls-remote", "--tags", eng["repo"], eng["glob"]], timeout=60)
        refs = parse_tag_refs(out, eng["tag_rx"])
        if rc != 0 or not refs:
            return 502, {"error": f"連不到 GitHub 取 {eng['name']} 的版本清單；這台要能連外，或把編好的 {eng['target']} 複製進來"}
        tag, commit = refs[max(refs)]
        # 編譯很吃記憶體（每個並行約 1.5 GiB）：照系統記憶體扣掉底線後的餘裕決定並行數，連一個都放不下就不編。
        # 統一記憶體機邊跑大模型邊編譯，可能把整台拖死，這正是這套系統要防的事
        ram = ram_pool()
        room = ram["free_gib"] - floors(registry()["machine"])["ram"]
        jobs = min(pre["jobs"], int(room // 1.5))
        if jobs < 1:
            return 409, {"error": f"系統記憶體只剩 {ram['free_gib']:.0f} GiB，扣掉 {floors(registry()['machine'])['ram']:.0f} GiB 底線後"
                                  "不夠編譯。請先停掉一個模型再編"}
        pre["jobs"] = jobs
        dest = inside(HOME, eng["dir"].format(tag))
        os.makedirs(ENGINE_DIR, mode=0o700, exist_ok=True)
        log = engine_file("build", f"-{kind}-build.log")
        open(log, "w").close()
        st = {"state": "running", "phase": "準備", "tag": tag, "commit": commit, "backend": pre["backend"], "dest": dest, "jobs": jobs,
              "log": log, "started": time.time()}
        save_json(eng["file"], st)
        _BUILD.update(cancel=False, thread_alive=True, kind=kind)
    threading.Thread(target=_build_run, args=(st, pre, log, kind), daemon=True).start()
    return 200, {"ok": True, "tag": tag, "backend": pre["backend"]}


def cancel_build(kind="llama"):
    with _BUILD_LOCK:
        if not _BUILD.get("thread_alive") or _BUILD.get("kind") != kind:
            return 409, {"error": "沒有正在進行的編譯"}
        _BUILD["cancel"] = True
        proc = _BUILD["proc"]
    if proc:
        kill_tree(proc.pid)
    return 200, {"ok": True}


def do_logs(slot_id, lines=300):
    reg = registry()
    slot = next((s for s in reg["slots"] if s["id"] == slot_id), None)
    if not slot or not slot.get("unit"):
        return 404, {"error": "沒有這個欄位"}
    try:
        if SYSTEMD:
            flag = "--user " if unit_scope(slot["unit"]) == "user" else ""
            _, out, err = sh(["journalctl", *flag.split(), "-u", unit_name(slot["unit"]), "-n", int(lines), "--no-pager", "-o", "short-iso"])
        else:
            out, err = "\n".join(tail_lines(SUP.log_path(unit_name(slot["unit"])) or "", int(lines))), ""
    except ValueError as ex:
        return 400, {"error": str(ex)}
    text = out or err
    if slot.get("log") and os.path.exists(slot["log"]):
        out2 = "\n".join(tail_lines(slot["log"], int(lines)))
        if out2:
            text = (text + "\n----- 服務自己的 log -----\n" + out2).strip()
    return 200, {"slot": slot_id, "unit": slot["unit"], "text": text[-150000:]}


# ---------------------------------------------------------------- 網頁上管理這台：機器設定與欄位
# 不開放整份覆寫登錄檔：拿到權杖的人就能把欄位的記錄檔指到 ~/.ssh/id_rsa，再從 /logs 讀出來。
# 這裡只收白名單欄位；路徑類的設定（存放區、llama-server）逐項檢查，欄位的記錄檔與設定檔路徑一律不能從網頁改。
MEMORY_MODELS = ("unified_split", "unified_single", "discrete")
SYSTEM_DIRS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc", "/root",
               "/run", "/sbin", "/sys", "/usr", "/var")
UNIT_RX = re.compile(r"[A-Za-z0-9][\w@.-]{0,90}\.service")
OWN_UNITS = ("xconsole-agent.service", "xconsole.service")


def _apu_like():
    """AMD 的 APU（例 Ryzen AI 395）：CPU 型號裡帶內建顯示（「w/ Radeon 8060S」）。
    不能看 KFD 拓撲：它把同一顆晶片的 CPU 與 GPU 分成兩個節點（.27 實測），以前這樣判斷永遠判不出 APU。"""
    return "Radeon" in cpu_name()


def suggest_memory_model(gpus=None):
    """依硬體建議記憶體架構，回 (代號, 理由)。只是建議，管理者在網頁上確認後才寫入。"""
    gpus = probe_gpus() if gpus is None else gpus
    if OS == "darwin":
        return "unified_single", "Apple Silicon：CPU 與 GPU 共用同一塊統一記憶體（GPU 預設最多只能用到約 2/3～3/4）"
    if os.path.exists("/etc/nv_tegra_release"):
        return "unified_single", "偵測到 Jetson（/etc/nv_tegra_release），GPU 與系統共用同一塊記憶體"
    nvidia = has_nvidia()
    if nvidia and gpus and all(not g.get("vram_total") for g in gpus):
        return "unified_single", "nvidia-smi 回報不出顯存總量（DGX Spark 這類統一記憶體機就是這樣）"
    if not nvidia and gpus and _apu_like():
        return "unified_split", "AMD APU（例 Ryzen AI 395）：BIOS 切一塊給 GPU，系統只剩另一塊，兩塊都要盯"
    if gpus:
        return "discrete", f"偵測到 {len(gpus)} 張獨立顯示卡，各自有顯存，系統記憶體另外一塊"
    return "discrete", "沒有偵測到 GPU；先照獨立顯存算，確認後可以改"


def _rotational(dev):
    """裝置是不是機械硬碟。分割區要往上找到整顆碟的 queue/rotational。"""
    name = os.path.basename(os.path.realpath(dev))
    for cand in (name, re.sub(r"p?\d+$", "", name)):
        v = load_text(f"/sys/block/{cand}/queue/rotational")
        if v is not None:
            return v.strip() == "1"
    return None


def _mounts():
    """(掛載點, 裝置) 清單：只列真的檔案系統、可以放模型的地方。"""
    if OS == "linux":
        out = []
        for line in (load_text("/proc/mounts") or "").splitlines():
            f = line.split()
            if len(f) >= 3 and f[2] in ("ext4", "xfs", "btrfs", "f2fs"):
                mnt = f[1].replace("\\040", " ")
                if not mnt.startswith(("/boot", "/snap")):
                    out.append((mnt, f[0]))
        return out
    if OS == "darwin":
        # /Volumes 底下另外插的碟；系統自己的（復原、時光機快照、隱藏的）不列
        vols = [os.path.join("/Volumes", v) for v in sorted(os.listdir("/Volumes"))
                if not v.startswith((".", "com.apple")) and v not in ("Recovery", "Preboot", "VM", "Update")] \
            if os.path.isdir("/Volumes") else []
        return [(v, v) for v in ["/", *vols] if os.path.isdir(v) and not os.path.islink(v)]
    import ctypes
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if os.path.isdir(root) and ctypes.windll.kernel32.GetDriveTypeW(root) == 3:      # 3＝固定磁碟，不列光碟、USB、網路磁碟
            out.append((root, root))
    return out


def storage_choices():
    """可以當存放區的磁碟：實體檔案系統、50 GiB 以上。標出機械硬碟（大模型放上去載入要好幾分鐘）。"""
    out, seen = [], set()
    for mnt, dev in _mounts():
        if mnt in seen:
            continue
        try:
            free, total = disk_free(mnt)
        except OSError:
            continue
        if total < 50:
            continue
        seen.add(mnt)
        under_home = mnt in ("/", HOME[:3]) or HOME.startswith(mnt.rstrip("/\\") + os.sep)
        path = os.path.join(HOME, "models", "gguf") if under_home else os.path.join(mnt, "models", "gguf")
        out.append({"mount": mnt, "device": dev, "total_gib": total, "free_gib": free,
                    "hdd": _rotational(dev) if OS == "linux" else None, "path": path,
                    "writable": os.access(path if os.path.isdir(path) else mnt, os.W_OK)})
    return sorted(out, key=lambda x: (x["hdd"] is True, -x["free_gib"]))


def llama_candidates():
    out = []
    for cand in llama_globs():
        rc, o, e = sh([cand, "--version"], timeout=15)
        out.append({"path": cand, "ok": rc == 0, "build": llama_build_no(cand, o + e) or None,
                    "backend": llama_backend(cand)})
    return out


def machine_info():
    reg = registry()
    m = reg["machine"]
    gpus = probe_gpus()
    sug, why = suggest_memory_model(gpus)
    _, models, _ = scan_gguf(reg)
    return {
        "settings": {"memory_model": m.get("memory_model", "discrete"), "confirmed": bool(m.get("confirmed")),
                     "ram_floor_gib": m.get("ram_floor_gib"),
                     "vram_floor_gib": m.get("vram_floor_gib"), "note": m.get("note", ""),
                     "gguf_root": m.get("gguf_root") or "", "llama_server": m.get("llama_server") or "",
                     "bandwidth_gbs": m.get("bandwidth_gbs"), "ram_bandwidth_gbs": m.get("ram_bandwidth_gbs"),
                     "sd_cli": m.get("sd_cli") or ""},
        "defaults": {k: DEFAULT_FLOORS[k] for k in MEMORY_MODELS},
        "effective": {"floors": floors(m), "gguf_root": gguf_root(reg), "llama_server": llama_server_exe(reg),
                      "sd_cli": sd_cli_exe(reg), "sd_root": sd_root(reg)},
        "chip": chip(m),
        "detect": {"memory_model": sug, "why": why, "hostname": socket.gethostname(),
                   "gpus": [{"id": g["id"], "name": g.get("name"), "vram_gib": round((g.get("vram_total") or 0) / GIB, 1)}
                            for g in gpus],
                   "ram_gib": round(mem_stats()["total"] / GIB, 1), "os": OS},
        "storage": storage_choices(),
        "llama": llama_candidates(),
        "sd": sd_candidates(),
        "gguf_count": len(models),
        "units": register_candidates(reg),
    }


def data_bases():
    """存放區只准放在這些位置底下：家目錄、另外掛載的資料碟、常見的資料目錄。"""
    if OS == "darwin":
        return [HOME, *(m for m, _ in _mounts() if m.startswith("/Volumes/"))]
    if OS == "windows":
        # 系統碟只准家目錄；其他固定磁碟整顆都可以（D:\models 這種常見放法）
        return [HOME, *(m for m, _ in _mounts() if not m.upper().startswith(os.environ.get("SystemDrive", "C:").upper()))]
    mounts = [m for m, _ in _mounts()]
    # 家目錄所在的分割區（例：/home 獨立掛載）不能整個當允許位置，否則存放區可以設到別的使用者家目錄裡
    return [HOME, *(m for m in mounts if m != "/" and not m.startswith("/boot") and not HOME.startswith(m.rstrip("/") + "/")),
            "/data", "/models", "/srv", "/opt"]


def contained(raw, bases):
    """把管理者填的路徑換成「確認落在某個允許位置底下」的路徑（經 inside() 的 realpath＋前綴檢查）；不在任何一個底下回 None。"""
    real = os.path.realpath(raw)
    for b in bases:
        if not os.path.isdir(b):
            continue
        rb = os.path.realpath(b)
        if real.startswith(rb + os.sep):
            try:
                return inside(rb, os.path.relpath(real, rb))
            except ValueError:
                return None
    return None


def check_root(raw):
    """存放區：在允許的位置底下、不是隱藏資料夾、不在系統目錄、建得起來而且寫得進去。"""
    p = expand(str(raw or "").strip())
    if not p:
        return "", None
    if not os.path.isabs(p):
        return None, "存放區要寫完整路徑（/ 開頭、磁碟代號開頭，或 ~/ 開頭）"
    bases = data_bases()
    real = contained(p, bases)
    if real is None:
        return None, "存放區要放在家目錄、另外掛載的資料碟，或 /data、/models、/srv、/opt 底下（不能是這些位置本身）"
    if OS == "linux" and any(real == d or real.startswith(d + "/") for d in SYSTEM_DIRS):
        return None, f"{real} 是系統目錄，不能當存放區"
    if any(part.startswith(".") for part in re.split(r"[\\/]", real) if part):
        return None, "不能放在隱藏資料夾（例 ~/.ssh、~/.config）裡"
    try:
        os.makedirs(real, exist_ok=True)
    except OSError as ex:
        return None, f"建不了 {real}：{ex.strerror}"
    if not os.access(real, os.W_OK):
        return None, f"代理的帳號寫不進 {real}，請先調整這個資料夾的權限"
    return real, None


def check_llama(raw):
    """llama-server：只准家目錄、/opt、/usr/local 底下，檔名就是 llama-server，--version 跑得起來（代理會執行它）。"""
    raw = expand(str(raw or "").strip())
    if not raw:
        return "", None
    allowed = [HOME, "/opt", "/usr/local"] + (["/opt/homebrew"] if OS == "darwin" else [])
    p = contained(raw, allowed)
    if p is None:
        return None, "llama-server 要在家目錄、/opt 或 /usr/local 底下" if OS != "windows" else "llama-server.exe 要在代理的家目錄底下"
    if os.path.basename(p) != "llama-server" + EXE or not os.path.isfile(p) or not os.access(p, os.X_OK):
        return None, f"要指到編好的 llama-server{EXE} 執行檔"
    rc, o, e = sh([p, "--version"], timeout=20, env=llama_launch_env(p))
    if rc != 0 or "version" not in (o + e):
        return None, "這支 llama-server 跑不起來（--version 失敗），可能缺函式庫"
    return p, None


def check_sd_cli(raw):
    """sd-cli：同 llama-server 的規則，檔名就是 sd-cli，--help 跑得起來。"""
    raw = expand(str(raw or "").strip())
    if not raw:
        return "", None
    p = contained(raw, [HOME, "/opt", "/usr/local"] + (["/opt/homebrew"] if OS == "darwin" else []))
    if p is None:
        return None, "sd-cli 要在家目錄、/opt 或 /usr/local 底下" if OS != "windows" else "sd-cli.exe 要在代理的家目錄底下"
    if os.path.basename(p) != "sd-cli" + EXE or not os.path.isfile(p) or not os.access(p, os.X_OK):
        return None, "要指到編好的 sd-cli 執行檔（檔名就是 sd-cli）"
    rc, o, e = sh([p, "--help"], timeout=20)
    if rc != 0:
        return None, "這支 sd-cli 跑不起來（--help 失敗），可能缺函式庫"
    return p, None


@_locked
def save_machine(body):
    reg = registry()
    m = reg["machine"]
    mm = body.get("memory_model", m.get("memory_model"))
    if mm not in MEMORY_MODELS:
        return 400, {"error": "記憶體架構只能是 " + "、".join(MEMORY_MODELS)}
    new = {"memory_model": mm}
    for k in ("ram_floor_gib", "vram_floor_gib"):
        v = body.get(k)
        if v in (None, ""):
            new[k] = None
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 400, {"error": "底線要填數字（GiB）"}
        if not 0 <= v <= 256:
            return 400, {"error": "底線要在 0～256 GiB 之間"}
        new[k] = v
    for k in ("bandwidth_gbs", "ram_bandwidth_gbs"):
        v = body.get(k)
        if v in (None, ""):
            new[k] = None
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 400, {"error": "記憶體頻寬要填數字（GB/s）"}
        if not 1 <= v <= 20000:
            return 400, {"error": "記憶體頻寬要在 1～20000 GB/s 之間"}
        new[k] = v
    note = str(body.get("note") or "")
    if len(note) > 300 or re.search(r"[\x00-\x08\x0b-\x1f]", note):
        return 400, {"error": "備註最多 300 字，不能有控制字元"}
    new["note"] = note
    root, err = check_root(body.get("gguf_root"))
    if err:
        return 400, {"error": err}
    exe, err = check_llama(body.get("llama_server"))
    if err:
        return 400, {"error": err}
    sd, err = check_sd_cli(body.get("sd_cli"))
    if err:
        return 400, {"error": err}
    new["gguf_root"], new["llama_server"], new["sd_cli"] = root, exe, sd
    m["confirmed"] = True             # 管理者在網頁上確認過記憶體架構（不再拿偵測建議比對）
    for k, v in new.items():
        if v in (None, "") and k not in ("memory_model", "note"):
            m.pop(k, None)
        else:
            m[k] = v
    save_json(REGISTRY, reg)
    return 200, {"ok": True, "settings": machine_info()["settings"]}


def register_candidates(reg=None):
    """這台可以登記成欄位的服務：使用者自己建立的 systemd 使用者服務（服務檔在家目錄底下），
    扣掉 xConsole 自己的、已經登記過的。系統內建的桌面服務（GNOME、音效、印表機…）一台就上百個，全列出來管理者找不到自己的。"""
    if not SYSTEMD:
        return []                     # 沒有 systemd：只能用 xConsole 自己建的模型服務，沒有「既有服務」可登記
    reg = reg or registry()
    used = {s.get("unit") for s in reg["slots"]} | {e.get("unit") for s in reg["slots"]
                                                   for e in (s.get("engines") or {}).values()}
    _, out, _ = sh(["systemctl", "--user", "list-unit-files", "--type=service", "--no-legend", "--no-pager"], timeout=20)
    names = [u for u in ((ln.split() or [""])[0] for ln in out.splitlines())
             if UNIT_RX.fullmatch(u) and u not in OWN_UNITS and u not in used and "@" not in u
             and not re.fullmatch(r"xc-.*-llama\.service", u)]
    if not names:
        return []
    _, show, _ = sh(["systemctl", "--user", "show", "-p", "Id", "-p", "FragmentPath", "--", *names], timeout=30)
    mine = []
    for block in show.split("\n\n"):
        kv = dict(ln.split("=", 1) for ln in block.splitlines() if "=" in ln)
        frag = kv.get("FragmentPath", "")
        if kv.get("Id") in names and frag.startswith(("/home/", os.path.realpath(HOME) + "/")):
            mine.append(kv["Id"])
    return sorted(mine)


def _slot_fields(body, base):
    """欄位的白名單欄位。沒有記錄檔、設定檔路徑：那兩個從網頁開放就能讀任意檔案。"""
    out = {}
    role = str(body.get("role", base.get("role", "")) or "").strip()
    if not 1 <= len(role) <= 12 or re.search(r"[\x00-\x1f<>]", role):
        return None, "角色名稱要 1～12 個字"
    out["role"] = role
    tier = str(body.get("tier", base.get("tier", "")) or "").strip()
    if len(tier) > 12 or re.search(r"[\x00-\x1f<>]", tier):
        return None, "等級最多 12 個字"
    out["tier"] = tier
    caps = body.get("caps", base.get("caps") or [])
    if not isinstance(caps, list) or len(caps) > 6 or any(
            not isinstance(c, str) or not 1 <= len(c.strip()) <= 12 or re.search(r"[\x00-\x1f<>]", c) for c in caps):
        return None, "能力標籤最多 6 個，每個 1～12 個字"
    out["caps"] = [c.strip() for c in caps]
    for k in ("est_vram_gib", "est_ram_gib"):
        v = body.get(k, base.get(k))
        if v in (None, ""):
            out[k] = None
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None, "預估記憶體要填數字（GiB）"
        if not 0 <= v <= 1024:
            return None, "預估記憶體要在 0～1024 GiB 之間"
        out[k] = v
    if "port" in body or "port" in base:
        v = body.get("port", base.get("port"))
        if v in (None, ""):
            out["port"] = None
        else:
            try:
                v = int(v)
            except (TypeError, ValueError):
                return None, "埠要填 1～65535 的數字"
            if not 1 <= v <= 65535:
                return None, "埠要填 1～65535 的數字"
            out["port"] = v
    out["critical"] = bool(body.get("critical", base.get("critical", False)))
    note = str(body.get("note", base.get("note", "")) or "")
    if len(note) > 300 or re.search(r"[\x00-\x08\x0b-\x1f]", note):
        return None, "備註最多 300 字"
    out["note"] = note
    if "model" in body and base.get("kind") not in ("strata", "llama"):
        name = str(body.get("model") or "").strip()
        if len(name) > 60 or re.search(r"[\x00-\x1f<>]", name):
            return None, "顯示名稱最多 60 個字"
        out["model"] = name
    return out, None


@_locked
def add_slot(body):
    reg = registry()
    kind = body.get("type")
    if kind not in ("empty", "service"):
        return 400, {"error": "type 只能是 empty（空欄位）或 service（登記已在跑的服務）"}
    fields, err = _slot_fields(body, {})
    if err:
        return 400, {"error": err}
    taken = {s["id"] for s in reg["slots"]}
    sid = next(f"slot{i}" for i in range(1, 1000) if f"slot{i}" not in taken)
    slot = {"id": sid, **{k: v for k, v in fields.items() if v is not None}}
    if kind == "service":
        unit = str(body.get("unit") or "")
        if unit not in register_candidates(reg):
            return 400, {"error": f"{unit or '（沒填）'} 不是這台可以登記的服務（要是使用者層、還沒登記過的 systemd 服務）"}
        slot["unit"] = unit
        slot.setdefault("model", unit.removesuffix(".service"))
    reg["slots"].append(slot)
    save_json(REGISTRY, reg)
    return 200, {"ok": True, "slot": slot}


@_locked
def edit_slot(sid, body):
    reg = registry()
    slot = next((s for s in reg["slots"] if s["id"] == sid), None)
    if not slot:
        return 404, {"error": f"沒有這個欄位：{sid}"}
    fields, err = _slot_fields(body, slot)
    if err:
        return 400, {"error": err}
    for k, v in fields.items():
        if k == "port" and slot.get("kind") in ("strata", "llama"):
            continue        # 引擎欄位的埠跟著引擎設定走（API 串接頁改），這裡改了會跟服務實際的埠對不上
        if v is None:
            slot.pop(k, None)
        else:
            slot[k] = v
    save_json(REGISTRY, reg)
    return 200, {"ok": True, "slot": slot}


@_locked
def delete_slot(sid):
    reg = registry()
    slot = next((s for s in reg["slots"] if s["id"] == sid), None)
    if not slot:
        return 404, {"error": f"沒有這個欄位：{sid}"}
    if slot.get("unit") and unit_state(slot["unit"]) in ("active", "activating"):
        return 409, {"error": "這個欄位的服務還在跑，先停止再移除"}
    removed = []
    if slot.get("kind") == "llama" and slot.get("unit") == llama_unit(sid):
        # 這支服務是 xConsole 替這個欄位建的，移除欄位就一起收掉；登記進來的既有服務不動它
        sysctl(slot["unit"], "disable", timeout=30)
        unit_dir = os.environ.get("XCONSOLE_UNIT_DIR") or os.path.join(HOME, ".config/systemd/user")
        for f in (inside(unit_dir, slot["unit"]), engine_file(sid, "-llama.json"), engine_file(sid, ".key"),
                  engine_file(sid, "-llama.log")):
            if os.path.exists(f):
                os.remove(f)
                removed.append(os.path.basename(f))
        sh(["systemctl", "--user", "daemon-reload"], timeout=30)
    reg["slots"] = [s for s in reg["slots"] if s["id"] != sid]
    reg["measure"].pop(sid, None)
    save_json(REGISTRY, reg)
    return 200, {"ok": True, "removed_files": removed,
                 "kept_service": slot.get("unit") if slot.get("unit") and not removed else None}


def do_all(action, force=False):
    reg = registry()
    results = []
    for slot in reg["slots"]:
        if not slot.get("unit"):
            continue
        if action in ("stop", "restart") and slot.get("critical") and not force:
            results.append({"slot": slot["id"], "code": 409,
                            "message": "關鍵服務，略過（要動它請單獨操作）"})
            continue
        code, body = do_control(slot["id"], action, force=force)
        results.append({"slot": slot["id"], "code": code, **body})
    return 200, {"action": action, "results": results}


# ---------------------------------------------------------------- token 累計計數器

COUNTERS = os.path.join(STATE_DIR, "counters.json")
REQ_RX = re.compile(r"serve: prompt (\d+) tokens = (\d+) reused \+ \d+ read in [\d.]+ ms "
                    r"\([\d.]+ tok/s\), (\d+) generated")


def counters():
    """引擎每處理一個請求寫一行記錄，但那一行沒有時間戳——
    所以「本期處理多少 token」不能事後從記錄切。這裡把每個欄位的處理量一路累計，
    主控台每 5 分鐘取一次，兩次的差就是那段時間的量。
    只讀上次讀到之後新增的部分；記錄被輪替（inode 變了或檔案變小）就從頭讀。"""
    reg = registry()
    state = load_json(COUNTERS, {})
    out = {}
    for slot in reg["slots"]:
        log = slot.get("log")
        if not log or not os.path.exists(log):
            continue
        st = state.get(slot["id"]) or {"requests": 0, "prompt": 0, "reused": 0, "generated": 0}
        try:
            info = os.stat(log)
        except OSError:
            continue
        if st.get("inode") != info.st_ino or info.st_size < st.get("offset", 0):
            st["offset"] = 0
            st["inode"] = info.st_ino
        with open(log, "rb") as fh:
            fh.seek(st["offset"])
            chunk = fh.read()
        # 最後一行可能還沒寫完：只吃到最後一個換行為止，剩下的下次再讀
        cut = chunk.rfind(b"\n") + 1
        for line in chunk[:cut].decode("utf-8", "replace").splitlines():
            m = REQ_RX.search(line)
            if m:
                st["requests"] += 1
                st["prompt"] += int(m.group(1))
                st["reused"] += int(m.group(2))
                st["generated"] += int(m.group(3))
        st["offset"] += cut
        state[slot["id"]] = st
        out[slot["id"]] = {k: st[k] for k in ("requests", "prompt", "reused", "generated")}
    save_json(COUNTERS, state)
    return {"ts": time.time(), "slots": out}


# ---------------------------------------------------------------- HTTPS

CERT_FILE = os.path.join(STATE_DIR, "agent-cert.pem")
KEY_FILE = os.path.join(STATE_DIR, "agent-key.pem")


def local_names():
    names = {"localhost", "127.0.0.1", socket.gethostname()}
    if OS == "linux":
        _, out, _ = sh(["hostname", "-I"])
        names.update(out.split())
    else:
        names.add(_local_ip())
        try:
            names.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
    return sorted(n for n in names if n and not n.startswith("fe80") and "%" not in n)


def _cert_with_cryptography():
    """有 cryptography 就用它產自簽（Windows 沒有 openssl 指令；macOS 內建的 LibreSSL 版本參差）。沒裝回 False 改用 openssl。"""
    try:
        import datetime as dt
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = []
    for n in local_names():
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            sans.append(x509.DNSName(n))
    name = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "xCloudinfo"),
                      x509.NameAttribute(NameOID.COMMON_NAME, f"xconsole-agent-{socket.gethostname()}"[:64])])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with open(CERT_FILE, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return True


def ensure_cert():
    """代理也走 HTTPS：主控台用 Bearer 權杖連過來，明文 http 等於權杖在內網裸奔。
    第一次啟動產一張自簽，SAN 涵蓋本機所有 IP；主控台部署時經 SSH 取回這張當作信任依據。"""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    if _cert_with_cryptography():
        return
    san = ",".join(("IP:" if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", n) and (":" in n or n.count(".") == 3)
                    else "DNS:") + n for n in local_names())
    rc, _, err = sh(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                     "-keyout", KEY_FILE, "-out", CERT_FILE,
                     "-subj", f"/C=TW/O=xCloudinfo/CN=xconsole-agent-{socket.gethostname()}",
                     "-addext", "subjectAltName=" + san, "-addext", "basicConstraints=critical,CA:FALSE"], timeout=60)
    if rc != 0:
        raise SystemExit(f"產不出代理憑證（{err[:200]}），停止啟動，不退回明文 http。"
                         "請安裝 Python 的 cryptography 套件（安裝程式會自動裝），或安裝 openssl")
    os.chmod(KEY_FILE, 0o600)


class AgentTLSServer(ThreadingHTTPServer):
    """在處理執行緒裡才握手，掃描器之類不講 TLS 的連線直接關掉，不會卡住主執行緒。"""
    daemon_threads = True
    ctx = None

    def finish_request(self, request, client_address):
        try:
            request.settimeout(15)
            first = request.recv(1, socket.MSG_PEEK)
            if not first or first[0] != 0x16:
                return
            conn = self.ctx.wrap_socket(request, server_side=True)
        except Exception:  # noqa: BLE001
            return
        try:
            conn.settimeout(600)       # 換模型會等到 9 分鐘
            self.RequestHandlerClass(conn, client_address, self)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 畫圖與影片（stable-diffusion.cpp）
# 一顆畫圖／影片模型＝主體＋文字編碼器＋VAE 等好幾個檔，常來自不同 repo，而且彼此共用（FLUX 系列用同一份 t5xxl）。
# 每個檔放在 <畫圖存放區>/<repo 資料夾>/<檔名>，重複的只下載一次；哪顆模型由哪幾個檔組成記在存放區的 models.json。
# sd-cli 每次產圖才載入、產完就結束：記憶體只在產圖那幾分鐘被占用，所以每次產圖前都照「當下」剩多少試算一次，
# 產圖途中再由看門狗盯著，跌破底線就中止（統一記憶體機吃爆會把整台拖死）。
SD_ROLES = {"model": "-m", "diffusion": "--diffusion-model", "clip_l": "--clip_l", "clip_g": "--clip_g",
            "t5xxl": "--t5xxl", "llm": "--llm", "vae": "--vae", "audio_vae": "--audio-vae",
            "connectors": "--embeddings-connectors"}
SD_SAMPLERS = ("euler", "euler_a", "heun", "dpm2", "dpm++2s_a", "dpm++2m", "dpm++2mv2", "ipndm", "ipndm_v", "lcm",
               "ddim_trailing", "tcd")
SD_EXT = (".gguf", ".safetensors", ".sft")
# 中國來源（含它的文字編碼器與 VAE：文字編碼器也是一顆會在客戶機器上執行的模型）。與主控台 recommend.SD_CN_RX 相同
SD_CN_RX = re.compile(r"(?<![a-z0-9])(qwen|wan|z[-_ ]?image|hunyuan|minimax|hidream|ernie|longcat|ovis|moss|kolors|"
                      r"cogview|lumina|omnigen|bagel|seedream|seedance|sensenova|llada|ming[-_]|deepseek|glm|kimi|"
                      r"moonshot|baichuan|internlm|doubao)(?![a-z])", re.I)
SD_JOBS_FILE = os.path.join(STATE_DIR, "sd-jobs.json")
GEN_DIR = os.path.join(STATE_DIR, "gen")
SD_KEEP = 30                     # 產出只留最近 30 件
SD_RAM_SELF = 2.0                # sd-cli 自己（讀檔緩衝、CPU 端張量）要的系統記憶體，GiB
SD_COMPUTE = {"image": 2.0, "video": 0.3}   # 每百萬像素（影片是每一格）的計算緩衝，GiB；經驗值，產過一次改用實測
SD_PROGRESS_RX = re.compile(r"\|\s*(\d+)/(\d+)\s*-\s*([\d.]+)\s*(s/it|it/s)")
_SD_LOCK = threading.Lock()
_GEN = {"jobs": None, "proc": None}
_GEN_LOCK = threading.Lock()


def sd_root(reg=None):
    """畫圖與影片模型的存放區：跟文字模型（GGUF 存放區）同一層的 sd 資料夾，換存放區時跟著一起換。
    分開放是因為擴散模型也是 .gguf，放進 GGUF 存放區會被當成文字模型列進「已安裝」。"""
    g = gguf_root(reg).rstrip("/")
    return os.path.join(os.path.dirname(g) or HOME, "sd")


def sd_candidates():
    """家目錄下所有 sd-cli：網頁編譯的（stable-diffusion.cpp-master-…/build/bin/）與官方預編譯的（stable-diffusion.cpp-master-…-<平台>/）。"""
    pats = ("stable-diffusion.cpp*/build*/bin/sd-cli", "stable-diffusion.cpp*/sd-cli")
    out = []
    for cand in sorted({c for pat in pats for c in glob.glob(os.path.join(HOME, pat + EXE))}):
        m = re.search(r"master-(\d+)", cand)
        pre = re.search(r"-(macos-arm64|win-[\w]+-x64)", cand)
        out.append({"path": cand, "build": int(m.group(1)) if m else None, "ok": os.access(cand, os.X_OK),
                    "backend": f"預編譯 {pre.group(1)}" if pre else "在這台編譯"})
    return out


def sd_cli_exe(reg=None):
    """用哪支 sd-cli：管理者在「這台的設定」指定的優先，否則挑版本最新的那支。"""
    reg = reg or registry()
    want = expand(reg["machine"].get("sd_cli") or "")
    if want and os.access(want, os.X_OK):
        return want
    ok = [c for c in sd_candidates() if c["ok"]]
    return max(ok, key=lambda c: c["build"] or 0)["path"] if ok else None


def _sd_folder(repo):
    owner, name = repo.split("/", 1)
    return _slug(f"{owner}--{name.removesuffix('-GGUF').removesuffix('-gguf')}")


def _sd_file(path):
    """repo 裡的子資料夾併進檔名（vae/x.safetensors → vae__x.safetensors）：同一個 repo 不同資料夾的同名檔不會互相蓋掉。"""
    return path.replace("/", "__")


def sd_manifest(reg=None):
    data = load_json(os.path.join(sd_root(reg), "models.json"), {})
    return data if isinstance(data, dict) else {}


def _sd_save(rows, reg=None):
    root = sd_root(reg)
    os.makedirs(root, mode=0o700, exist_ok=True)
    save_json(os.path.join(root, "models.json"), rows)


def _sd_path(root, p):
    return inside(inside(root, p["folder"]), p["file"])


def _sd_have(root, p):
    try:
        f = _sd_path(root, p)
    except ValueError:
        return False
    return os.path.isfile(f) and os.path.getsize(f) == p["size"]


def sd_models(reg=None):
    reg = reg or registry()
    root = sd_root(reg)
    jobs = {j.get("sd_model"): j for j in downloads_list() if j.get("sd_model") and j["state"] in ("running", "queued")}
    out = []
    for mid, m in sd_manifest(reg).items():
        missing = [p["role"] for p in m["parts"] if not _sd_have(root, p)]
        j = jobs.get(mid)
        out.append({**m, "id": mid, "size_gib": round(sum(p["size"] for p in m["parts"]) / GIB, 2),
                    "ready": not missing, "missing": missing,
                    "job": {"id": j["id"], "done": j["done"], "total": j["total"]} if j else None})
    return sorted(out, key=lambda x: x.get("created") or 0)


def sd_library():
    reg = registry()
    root = sd_root(reg)
    os.makedirs(root, mode=0o700, exist_ok=True)
    du = os.statvfs(root)
    return {"root": root, "free_gib": round(du.f_bavail * du.f_frsize / GIB, 1), "models": sd_models(reg),
            "sd_cli": sd_cli_exe(reg), "building": bool(_BUILD.get("thread_alive")) and _BUILD.get("kind") == "sd",
            "memory_model": reg["machine"].get("memory_model", "discrete"),
            "downloads": [j for j in downloads_list() if j.get("sd_model")]}


def _clean_text(v, lim, name, multiline=False):
    v = str(v or "").strip()
    bad = r"[\x00-\x08\x0b-\x1f\x7f]" if multiline else r"[\x00-\x1f\x7f]"
    if len(v) > lim or re.search(bad, v):
        raise ValueError(f"{name}最多 {lim} 字，不能有控制字元")
    return v


def sd_check_defaults(d, kind):
    """預設參數（清單上的一顆）或使用者這次要產的參數，一律過同一道檢查。回乾淨的 dict，不合法丟 ValueError。"""
    d = d or {}
    out = {}
    try:
        out["width"], out["height"] = int(d.get("width") or 1024), int(d.get("height") or 1024)
        out["steps"] = int(d.get("steps") or 20)
        out["cfg"] = float(d.get("cfg") if d.get("cfg") is not None else 1.0)
        out["frames"] = int(d.get("frames") or (33 if kind == "video" else 1))
        out["fps"] = int(d.get("fps") or 24)
    except (TypeError, ValueError):
        raise ValueError("尺寸、步數、引導強度、影格數要是數字") from None
    for k in ("width", "height"):
        if not 64 <= out[k] <= 2048 or out[k] % 16:
            raise ValueError("寬與高要在 64～2048 之間，而且是 16 的倍數")
    if not 1 <= out["steps"] <= 150:
        raise ValueError("步數要在 1～150 之間")
    if not 0 <= out["cfg"] <= 30:
        raise ValueError("引導強度要在 0～30 之間")
    if kind == "video" and not (1 <= out["frames"] <= 241 and out["frames"] % 8 == 1):
        raise ValueError("影格數要在 1～241 之間，而且是 8 的倍數加 1（例：17、33、49、121）")
    if not 1 <= out["fps"] <= 60:
        raise ValueError("每秒格數要在 1～60 之間")
    s = d.get("sampler") or "euler"
    if s not in SD_SAMPLERS:
        raise ValueError("取樣方法只能是：" + "、".join(SD_SAMPLERS))
    out["sampler"] = s
    if kind != "video":
        out.pop("frames"), out.pop("fps")
    return out


def sd_check_spec(spec):
    """要下載的一顆畫圖／影片模型（主控台照推薦清單組好、或管理者在內部測試手動組）。回 (乾淨的 spec, 錯誤)。"""
    spec = spec or {}
    try:
        mid = str(spec.get("id") or "")
        if not SAFE_ID_RX.fullmatch(mid):
            raise ValueError("模型代號只能有英數、底線、連字號（最多 64 字）")
        title = _clean_text(spec.get("title"), 60, "顯示名稱")
        if not title:
            raise ValueError("要有顯示名稱")
        kind = spec.get("kind") or "image"
        if kind not in ("image", "video"):
            raise ValueError("種類只能是畫圖或影片")
        parts, roles = [], set()
        for p in spec.get("parts") or []:
            role, repo, path = p.get("role"), str(p.get("repo") or ""), str(p.get("path") or "")
            if role not in SD_ROLES or role in roles:
                raise ValueError(f"元件種類不合法或重複：{role}")
            if not HF_REPO_RX.fullmatch(repo):
                raise ValueError(f"Hugging Face 名稱格式是「作者/模型」：{repo}")
            if (not path or path.startswith("/") or ".." in path.split("/") or not path.lower().endswith(SD_EXT)
                    or len(path) > 200 or re.search(r"[\x00-\x1f\\]", path)):
                raise ValueError(f"檔名不合法（只收 .gguf、.safetensors、.sft）：{path}")
            roles.add(role)
            parts.append({"role": role, "repo": repo, "path": path})
        if not 1 <= len(parts) <= 8 or not roles & {"model", "diffusion"}:
            raise ValueError("至少要有主體（完整模型或擴散模型）一個檔，最多 8 個檔")
        defaults = sd_check_defaults(spec.get("defaults"), kind)
        desc = _clean_text(spec.get("desc"), 160, "說明")
    except ValueError as ex:
        return None, str(ex)
    return {"id": mid, "title": title, "kind": kind, "parts": parts, "defaults": defaults, "desc": desc,
            "quant": _clean_text(spec.get("quant"), 24, "量化") if spec.get("quant") else "",
            "catalog": spec.get("catalog") if HF_REPO_RX.fullmatch(str(spec.get("catalog") or "")) else None,
            "internal": bool(spec.get("internal"))}, None


def hf_tree(repo, token=""):
    """Hugging Face 上這個 repo 的所有檔案與大小（不下載）。"""
    if not HF_REPO_RX.fullmatch(repo or ""):
        return 400, {"error": "名稱格式是「作者/模型」"}
    try:
        with _hf_get(f"{HF_BASE}/api/models/{urllib.parse.quote(repo, safe='/')}/tree/main?recursive=true", token) as resp:
            tree = json.load(resp)
    except urllib.error.HTTPError as exc:
        msg = {401: "要登入才看得到（請貼 Hugging Face 權杖）", 403: "要先到 Hugging Face 頁面同意授權條款",
               404: "找不到這個 repo"}.get(exc.code, f"Hugging Face 回 {exc.code}")
        return 400, {"error": f"{repo}：{msg}"}
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"連不到 Hugging Face：{str(exc)[:120]}"}
    return 200, {"repo": repo, "files": [{"path": x["path"], "size": (x.get("lfs") or {}).get("size") or x.get("size") or 0}
                                         for x in tree if x.get("type") == "file"]}


def sd_download(spec, token=""):
    spec, err = sd_check_spec(spec)
    if err:
        return 400, {"error": err}
    words = " ".join([spec["id"], spec["title"]] + [f"{p['repo']} {p['path']}" for p in spec["parts"]])
    cn = SD_CN_RX.search(words)
    if cn and not spec["internal"]:
        return 400, {"error": f"這顆含中國來源的元件（{cn.group(1)}），政府案不得使用；內部測試要勾「內部測試」才能下載"}
    trees = {}
    for p in spec["parts"]:
        if p["repo"] not in trees:
            code, t = hf_tree(p["repo"], token)
            if code != 200:
                return code, t
            trees[p["repo"]] = {f["path"]: f["size"] for f in t["files"]}
        size = trees[p["repo"]].get(p["path"])
        if not size:
            return 400, {"error": f"{p['repo']} 裡沒有 {p['path']}"}
        p.update(size=size, folder=_sd_folder(p["repo"]), file=_sd_file(p["path"]))
    reg = registry()
    root = sd_root(reg)
    os.makedirs(root, mode=0o700, exist_ok=True)
    need = sum(p["size"] for p in spec["parts"] if not _sd_have(root, p)) / GIB
    du = os.statvfs(root)
    free = du.f_bavail * du.f_frsize / GIB
    if need + 10 >= free:
        return 400, {"error": f"磁碟不夠：還要下載 {need:.1f} GiB，存放區只剩 {free:.0f} GiB（另需保留 10 GiB）"}
    if any(j["state"] in ("running", "queued") for j in _DL.values()):
        return 409, {"error": "已經有一個下載在進行，一次只下載一個，避免把網路與磁碟塞滿"}
    with _SD_LOCK:
        rows = sd_manifest(reg)
        old = rows.get(spec["id"])
        rows[spec["id"]] = {k: spec[k] for k in ("title", "kind", "parts", "defaults", "desc", "quant", "catalog", "internal")} | {
            "cn_origin": bool(cn), "created": (old or {}).get("created") or int(time.time()),
            "measured": (old or {}).get("measured") if old and old.get("parts") == spec["parts"] else None}
        _sd_save(rows, reg)
    todo = [[p["repo"], p["path"], p["folder"], p["file"]] for p in spec["parts"] if not _sd_have(root, p)]
    if not todo:
        return 200, {"ok": True, "id": None, "model": spec["id"], "need_gib": 0}
    with _DL_LOCK:
        if any(j["state"] in ("running", "queued") for j in _DL.values()):
            return 409, {"error": "已經有一個下載在進行，一次只下載一個，避免把網路與磁碟塞滿"}
        jid = secrets.token_hex(4)
        _DL[jid] = {"id": jid, "repo": spec["parts"][0]["repo"], "stem": spec["quant"] or spec["title"],
                    "title": spec["title"], "folder": "sd", "files": todo, "sd_model": spec["id"],
                    "total": 0, "done": 0, "state": "queued", "created": int(time.time()), "error": None,
                    "_token": token, "_cancel": False}
    threading.Thread(target=_download_job, args=(jid, root), daemon=True).start()
    return 200, {"ok": True, "id": jid, "model": spec["id"], "need_gib": round(need, 1)}


def sd_delete(mid):
    """刪一顆畫圖／影片模型。別顆也在用的檔（例：共用的 t5xxl）留著，只刪這顆獨用的。"""
    reg = registry()
    root = sd_root(reg)
    if any(j.get("model") == mid and j["state"] == "running" for j in sd_jobs_list()):
        return 409, {"error": "這顆正在產圖，等它產完或取消再刪"}
    if any(j.get("sd_model") == mid and j["state"] in ("running", "queued") for j in downloads_list()):
        return 409, {"error": "這顆正在下載，先取消下載再刪"}
    with _SD_LOCK:
        rows = sd_manifest(reg)
        m = rows.pop(mid, None)
        if not m:
            return 404, {"error": "沒有這顆"}
        keep = {(p["folder"], p["file"]) for o in rows.values() for p in o["parts"]}
        freed, removed = 0, []
        for p in m["parts"]:
            if (p["folder"], p["file"]) in keep:
                continue
            for f in (_sd_path(root, p), _sd_path(root, p) + ".part"):
                if os.path.isfile(f):
                    freed += os.path.getsize(f)
                    os.remove(f)
                    removed.append(p["file"])
            d = inside(root, p["folder"])
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        _sd_save(rows, reg)
    return 200, {"ok": True, "removed": removed, "freed_gib": round(freed / GIB, 1)}


def sd_need(m, w, h, frames=1):
    """產這一件最多要多少記憶體（GiB）：所有權重＋計算緩衝。產過一次之後改用實測的最高用量（同尺寸以下直接用）。"""
    weights = sum(p["size"] for p in m["parts"]) / GIB
    mp = w * h / 1e6 * (frames if m["kind"] == "video" else 1)
    est = weights + 1.0 + SD_COMPUTE[m["kind"]] * mp
    meas = m.get("measured") or {}
    if meas.get("peak_gib"):
        mp0 = meas["w"] * meas["h"] / 1e6 * (meas.get("frames") or 1)
        est = max(est, meas["peak_gib"] + max(0.0, mp - mp0) * SD_COMPUTE[m["kind"]])
    return round(est, 1), round(weights, 1)


def sd_admit(m, w, h, frames=1, reg=None, hw=None):
    """產圖前的准入：照三種記憶體架構，用「產圖時最壞剩多少」判斷。plan＝要不要把權重放系統記憶體、用哪張卡。"""
    reg = reg or registry()
    hw = hw if hw is not None else hardware()
    p = pools(reg, hw)
    mm, fl = p["memory_model"], p["floors"]
    need, weights = sd_need(m, w, h, frames)
    biggest = max(x["size"] for x in m["parts"]) / GIB
    reasons, warnings, detail, plan = [], [], {}, {"offload": False, "gpu": None}
    tip = "先停掉一個模型、改小尺寸，或換小一級的量化"
    if mm == "unified_single":
        free = p["unified"]["free_gib"]
        worst = round(free - need - SD_RAM_SELF, 1)
        detail["unified"] = {"free": free, "need": round(need + SD_RAM_SELF, 1), "worst": worst, "floor": fl["ram"]}
        plan["max_vram"] = free - fl["ram"] - SD_RAM_SELF
        if worst < fl["ram"]:
            reasons.append(f"統一記憶體不足：產這一件要約 {need + SD_RAM_SELF:.0f} GiB，現在只剩 {free:.0f} GiB，"
                           f"產的時候會剩 {worst} GiB（低於 {fl['ram']} GiB 底線）。統一記憶體吃爆會把整台拖死；{tip}")
    else:
        rfree = p["ram"]["free_gib"]
        if mm == "discrete" and p["gpus"]:
            card = max(p["gpus"], key=lambda c: c["free_gib"])
            plan["gpu"] = str(card["id"])
            vfree, vfl = card["free_gib"], fl["vram"]
            if vfree - need < vfl and vfree - (biggest + need - weights) >= vfl and rfree - weights - SD_RAM_SELF >= fl["ram"]:
                plan["offload"] = True                  # 權重放系統記憶體，輪到誰算才搬進顯示卡：慢一些但放得下
                warnings.append(f"卡 {card['id']} 放不下全部權重，改成權重放系統記憶體、輪流搬進顯示卡算（會慢一些）")
        elif mm == "discrete":
            vfree, vfl = None, 0
        else:
            vfree, vfl = p["vram"]["free_gib"], fl["vram"]
        vneed = (biggest + need - weights) if plan["offload"] else need
        rneed = SD_RAM_SELF + (weights if plan["offload"] or vfree is None else 0)
        if vfree is not None:
            wv = round(vfree - vneed, 1)
            plan["max_vram"] = vfree - vfl
            detail["vram"] = {"free": vfree, "need": round(vneed, 1), "worst": wv, "floor": vfl}
            if wv < vfl:
                where = "GPU 那一塊" if mm == "unified_split" else f"卡 {plan['gpu']}" if plan["gpu"] else "顯示卡"
                reasons.append(f"{where}不夠：產這一件要約 {vneed:.0f} GiB，現在只剩 {vfree:.0f} GiB，"
                               f"產的時候會剩 {wv} GiB（低於 {vfl} GiB 底線）；{tip}")
        wr = round(rfree - rneed, 1)
        detail["ram"] = {"free": rfree, "need": round(rneed, 1), "worst": wr, "floor": fl["ram"]}
        if wr < fl["ram"]:
            extra = "，這台曾因為系統記憶體耗盡整台卡死" if mm == "unified_split" else ""
            reasons.append(f"系統記憶體不足：要約 {rneed:.0f} GiB，現在只剩 {rfree:.0f} GiB，"
                           f"產的時候會剩 {wr} GiB（低於 {fl['ram']} GiB 底線{extra}）")
    if plan.get("max_vram") is not None:
        plan["max_vram"] = max(1.0, math.floor(plan["max_vram"] * 10) / 10)
    # ponytail: AMD 一律關 flash attention。實測 .27（gfx1151、ROCm 7.1.1）開著會在載入擴散模型時
    # 「ROCm error: unspecified launch failure」整個中止，關掉 512×512 四步 12 秒正常出圖。之後 ROCm／sd.cpp 修好再拿掉
    plan["fa_off"] = not has_nvidia() and bool(probe_amd_sysfs())
    return {"allow": not reasons, "memory_model": mm, "need_gib": need, "weights_gib": weights,
            "measured": bool((m.get("measured") or {}).get("peak_gib")),
            "reasons": reasons, "warnings": warnings, "pools": detail, "plan": plan}


def sd_argv(exe, m, root, prm, out, plan):
    a = [exe] + (["-M", "vid_gen"] if m["kind"] == "video" else [])
    for p in m["parts"]:
        a += [SD_ROLES[p["role"]], _sd_path(root, p)]
    a += ["-p", prm["prompt"], "-W", prm["width"], "-H", prm["height"], "--steps", prm["steps"],
          "--cfg-scale", prm["cfg"], "--sampling-method", prm["sampler"], "-s", prm["seed"], "-o", out]
    if prm.get("negative"):
        a += ["-n", prm["negative"]]
    if m["kind"] == "video":
        a += ["--video-frames", prm["frames"], "--fps", prm["fps"]]
    if plan.get("offload"):
        a.append("--offload-to-cpu")
    # sd-cli 會自己依剩餘量把權重分到顯示卡／系統記憶體；給它上限＝准入算出來的額度，它自己也不會越過底線
    if plan.get("max_vram"):
        a += ["--max-vram", plan["max_vram"]]
    if plan.get("fa_off"):
        a += ["--flash-attn", "off"]
    # VAE 解碼是產圖時記憶體最高的一刻：大尺寸或影片一律分塊解碼，最高用量少很多，畫質一樣
    if prm["width"] * prm["height"] * (prm.get("frames") or 1) > 1024 * 1024:
        a.append("--vae-tiling")
    return [str(x) for x in a]


def parse_sd_progress(text, steps):
    """sd-cli 的進度列：|=====>   | 3/20 - 1.25s/it。載入權重也有同樣的進度列，只認總數等於步數的那幾行。"""
    hit = [x for x in SD_PROGRESS_RX.findall(text) if int(x[1]) == steps]
    if not hit:
        return None
    n, total, v, unit = hit[-1]
    spi = float(v) if unit == "s/it" else (1 / float(v) if float(v) else None)
    return {"step": int(n), "total": int(total), "s_per_it": round(spi, 2) if spi else None}


def _sd_jobs_save():
    with _GEN_LOCK:
        rows = [{k: v for k, v in j.items() if not k.startswith("_")} for j in _GEN["jobs"]]
    save_json(SD_JOBS_FILE, rows)


def sd_jobs_list():
    with _GEN_LOCK:
        if _GEN["jobs"] is None:
            rows = load_json(SD_JOBS_FILE, []) or []
            for j in rows:
                if j.get("state") == "running":
                    j.update(state="interrupted", error="代理重啟過，這件沒產完")
            _GEN["jobs"] = rows
        return [{k: v for k, v in j.items() if not k.startswith("_")} for j in _GEN["jobs"]]


def _gen_sample():
    """看門狗用的取樣：系統記憶體剩多少（/proc/meminfo）＋各卡顯存已用（NVIDIA 用 nvidia-smi，AMD 讀 sysfs，都很便宜）。"""
    g = probe_gpus()
    return ram_pool()["free_gib"], {str(x["id"]): (x.get("vram_used") or 0) / GIB for x in g}


def _gen_run(j, argv, env, mm, fl):
    log = _sd_log(j["id"])
    r0, g0 = _gen_sample()
    peak_r = peak_g = 0.0
    killed = None
    started = time.time()
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, env={**os.environ, **env},
                                start_new_session=True)
        with _GEN_LOCK:
            _GEN["proc"] = proc
        last_save = 0
        while proc.poll() is None:
            time.sleep(1)
            rf, g = _gen_sample()
            peak_r = max(peak_r, r0 - rf)
            peak_g = max(peak_g, max((g.get(k, 0) - g0.get(k, 0) for k in g), default=0.0))
            # 看門狗：系統記憶體跌破底線就中止。統一記憶體機（兩種）系統這塊被吃光整台會卡死；獨立顯卡機也一樣會拖垮系統
            if rf < fl["ram"] and not killed:
                killed = (f"產的時候系統記憶體只剩 {rf:.1f} GiB（低於 {fl['ram']} GiB 底線），已中止，避免整台被拖死。"
                          "請改小尺寸、換小一級的量化，或先停掉一個模型")
            if j["_cancel"] and not killed:
                killed = "已取消"
            if killed:
                kill_tree(proc.pid)
            pr = parse_sd_progress(tail_text(log), j["steps"])
            if pr:
                j.update(pr, phase="產生中")
            if pr and pr["step"] >= pr["total"]:
                j["phase"] = "解碼中" if j["kind"] == "image" else "解碼與編碼影片中"
            if time.time() - last_save > 5:
                _sd_jobs_save()
                last_save = time.time()
        rc = proc.returncode
    with _GEN_LOCK:
        _GEN["proc"] = None
    j["secs"] = round(time.time() - started, 1)
    j["ended"] = int(time.time())
    out = os.path.join(GEN_DIR, j["out"])
    if killed:
        j.update(state="cancelled" if killed == "已取消" else "failed", error=killed)
    elif rc != 0 or not os.path.isfile(out) or not os.path.getsize(out):
        tail = [x for x in tail_lines(log, 12) if x.strip()]
        j.update(state="failed", error=f"sd-cli 結束碼 {rc}：" + (tail[-1][-240:] if tail else "沒有輸出"))
    else:
        peak = peak_r if mm == "unified_single" else peak_g if mm == "unified_split" else max(peak_g, 0.0)
        j.update(state="done", phase="完成", peak_gib=round(peak, 1), bytes=os.path.getsize(out))
        # 實測最高用量寫回這顆模型：下次准入改用實測值（取比估算大的那個，不會因為實測偏小就放寬）
        with _SD_LOCK:
            rows = sd_manifest()
            if j["model"] in rows and peak > 0:
                rows[j["model"]]["measured"] = {"peak_gib": round(peak, 1), "w": j["width"], "h": j["height"],
                                                "frames": j.get("frames") or 1, "steps": j["steps"],
                                                "secs": j["secs"], "s_per_it": j.get("s_per_it"), "at": j["ended"]}
                _sd_save(rows)
    _sd_jobs_save()


def tail_text(path, n=8192):
    """記錄檔最後 n 個位元組（進度列用 \\r 覆寫同一行，所以不能照行讀）。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - n))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def sd_generate(body):
    reg = registry()
    m = sd_manifest(reg).get(str(body.get("model") or ""))
    if not m:
        return 404, {"error": "沒有這顆畫圖／影片模型"}
    root = sd_root(reg)
    if not all(_sd_have(root, p) for p in m["parts"]):
        return 409, {"error": "這顆還沒下載完整，到「推薦模型」按下載把缺的檔補齊"}
    exe = sd_cli_exe(reg)
    if not exe:
        return 409, {"error": "這台還沒安裝 stable-diffusion.cpp，先按「安裝畫圖引擎」"}
    try:
        prm = sd_check_defaults({**m["defaults"], **{k: body[k] for k in ("width", "height", "steps", "cfg", "sampler", "frames", "fps")
                                                    if body.get(k) not in (None, "")}}, m["kind"])
        prm["prompt"] = _clean_text(body.get("prompt"), 2000, "提示詞", multiline=True)
        prm["negative"] = _clean_text(body.get("negative"), 1000, "不要出現的內容", multiline=True)
        seed = int(body.get("seed") if body.get("seed") not in (None, "") else -1)
    except (TypeError, ValueError) as ex:
        return 400, {"error": str(ex) if str(ex) else "種子要是整數"}
    if not prm["prompt"]:
        return 400, {"error": "要寫提示詞"}
    if not -1 <= seed < 2 ** 31:
        return 400, {"error": "種子要在 0～2147483647 之間（-1＝隨機）"}
    prm["seed"] = seed if seed >= 0 else secrets.randbelow(2 ** 31)
    sd_jobs_list()
    with _GEN_LOCK:
        if _GEN["proc"] is not None or any(j["state"] == "running" for j in _GEN["jobs"]):
            return 409, {"error": "一次只產一件：前一件還在產，等它完成或取消"}
    verdict = sd_admit(m, prm["width"], prm["height"], prm.get("frames") or 1, reg)
    if not verdict["allow"]:
        return 409, {"error": "擋下來了", "admit": verdict}
    os.makedirs(GEN_DIR, mode=0o700, exist_ok=True)
    jid = secrets.token_hex(4)
    out = f"{jid}.{'webm' if m['kind'] == 'video' else 'png'}"
    argv = sd_argv(exe, m, root, prm, os.path.join(GEN_DIR, out), verdict["plan"])
    env = visible_env([verdict["plan"]["gpu"]]) if verdict["plan"]["gpu"] is not None else {}
    j = {"id": jid, "model": body["model"], "title": m["title"], "kind": m["kind"], **prm, "out": out,
         "state": "running", "phase": "載入模型中", "step": 0, "total": prm["steps"], "s_per_it": None,
         "created": int(time.time()), "need_gib": verdict["need_gib"], "offload": verdict["plan"]["offload"],
         "warnings": verdict["warnings"], "error": None, "_cancel": False}
    with _GEN_LOCK:
        if _GEN["proc"] is not None or any(x["state"] == "running" for x in _GEN["jobs"]):
            return 409, {"error": "一次只產一件：前一件還在產，等它完成或取消"}
        _GEN["jobs"].insert(0, j)
        for old in _GEN["jobs"][SD_KEEP:]:
            _sd_files_rm(old)
        del _GEN["jobs"][SD_KEEP:]
    _sd_jobs_save()
    p = pools(reg, hardware())
    threading.Thread(target=_gen_run, args=(j, argv, env, p["memory_model"], p["floors"]), daemon=True).start()
    return 200, {"ok": True, "id": jid, "admit": verdict}


def _sd_log(jid):
    return engine_file("sd", f"-{jid}.log")


def _sd_files_rm(j):
    for f in (os.path.join(GEN_DIR, j.get("out") or "x"), _sd_log(j["id"])):
        if os.path.isfile(f):
            os.remove(f)


def _sd_job(jid):
    sd_jobs_list()
    with _GEN_LOCK:
        return next((j for j in _GEN["jobs"] if j["id"] == jid), None)


def sd_job(jid):
    j = _sd_job(jid)
    if not j:
        return 404, {"error": "沒有這件"}
    out = {k: v for k, v in j.items() if not k.startswith("_")}
    log = _sd_log(jid)
    out["log_tail"] = [x for x in re.split(r"[\r\n]+", tail_text(log, 4096)) if x.strip()][-12:]
    return 200, out


def sd_output(jid):
    j = _sd_job(jid)
    if not j or j["state"] != "done":
        return 404, {"error": "這件沒有產出"}
    f = os.path.join(GEN_DIR, j["out"])
    if not os.path.isfile(f):
        return 404, {"error": "產出的檔案不見了"}
    if os.path.getsize(f) > 20 * 1024 * 1024:
        return 413, {"error": "檔案超過 20 MB，請到這台的 ~/.xconsole/gen/ 取"}
    with open(f, "rb") as fh:
        data = base64.b64encode(fh.read()).decode()
    return 200, {"mime": "video/webm" if f.endswith(".webm") else "image/png", "name": j["out"], "b64": data}


def sd_cancel(jid):
    j = _sd_job(jid)
    if not j or j["state"] != "running":
        return 409, {"error": "這件沒有在產"}
    j["_cancel"] = True
    return 200, {"ok": True}


def sd_job_delete(jid):
    j = _sd_job(jid)
    if not j:
        return 404, {"error": "沒有這件"}
    if j["state"] == "running":
        return 409, {"error": "還在產，先取消"}
    with _GEN_LOCK:
        _GEN["jobs"] = [x for x in _GEN["jobs"] if x["id"] != jid]
    _sd_files_rm(j)
    _sd_jobs_save()
    return 200, {"ok": True}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "xconsole-agent"      # 不公布版本號
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 不要把每個請求都寫進 journal
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        got = self.headers.get("Authorization", "")
        if got.startswith("Bearer ") and secrets.compare_digest(got[7:], TOKEN):
            return True
        self._send(401, {"error": "需要 Bearer 權杖"})
        return False

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/healthz":
            return self._send(200, {"ok": True, "service": "xconsole-agent"})
        if not self._auth():
            return None
        if path in ("/", "/state"):
            return self._send(200, full_state())
        if path == "/registry":
            return self._send(200, registry())
        m = re.fullmatch(r"/logs/([\w.-]+)", path)
        if m:
            code, body = do_logs(m.group(1))
            return self._send(code, body)
        if path == "/counters":
            return self._send(200, counters())
        if path == "/library":
            return self._send(200, library())
        if path == "/machine":
            return self._send(200, machine_info())
        m = re.fullmatch(r"/(llama|sd)/build(/progress)?", path)
        if m:
            return self._send(200, (build_progress if m.group(2) else build_status)(m.group(1)))
        if path == "/sd/library":
            return self._send(200, sd_library())
        if path == "/sd/jobs":
            return self._send(200, {"jobs": sd_jobs_list()})
        m = re.fullmatch(r"/sd/jobs/([0-9a-f]{8})(/output)?", path)
        if m:
            return self._send(*(sd_output if m.group(2) else sd_job)(m.group(1)))
        m = re.fullmatch(r"/apiinfo/([\w.-]+)", path)
        if m:
            code, out = api_info(m.group(1))
            return self._send(code, out)
        m = re.fullmatch(r"/catalog/([\w.-]+)", path)
        if m:
            return self._send(200, catalog(m.group(1)))
        return self._send(404, {"error": "找不到"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if not self._auth():
            return None
        body = self._body()
        force = bool(body.get("force"))
        m = re.fullmatch(r"/slot/([\w.-]+)/(start|stop|restart)", path)
        if m:
            code, out = do_control(m.group(1), m.group(2), force=force)
            return self._send(code, out)
        m = re.fullmatch(r"/admit/([\w.-]+)", path)
        if m:
            return self._send(200, admit(m.group(1)))
        m = re.fullmatch(r"/slot/([\w.-]+)/measure", path)
        if m:
            sl = next((x for x in registry()["slots"] if x["id"] == m.group(1)), None)
            c = slot_config(sl) if sl else None
            if not c or not c.get("_native"):
                return self._send(400, {"error": "這個欄位沒有可辨識的模型"})
            nm = os.path.basename(os.path.dirname(c["_native"]))
            record_model_measure(m.group(1), nm)
            return self._send(200, {"ok": True, "model": nm,
                                    "est": (registry().get("model_est") or {}).get(nm)})
        m = re.fullmatch(r"/slot/([\w.-]+)/model", path)
        if m:
            sid = m.group(1)
            cur = _slot(sid) or {}
            if body.get("engine") == "llama":
                code, out = set_llama_model(sid, str(body.get("model") or ""), body.get("ctx"), force=force,
                                            **({"gpus": body.get("gpus")} if "gpus" in body else {}))
            elif cur.get("kind") == "llama":
                code, out = back_to_strata(sid, body.get("pack") or "", body.get("native") or "", force=force)
            else:
                code, out = set_model(sid, body.get("pack") or "", body.get("native") or "", force=force)
            return self._send(code, out)
        if path == "/library/hf":
            return self._send(*hf_files(str(body.get("repo") or "").strip(), str(body.get("token") or "")))
        if path == "/library/hf/profile":
            return self._send(*hf_profile(str(body.get("repo") or "").strip(), str(body.get("token") or ""),
                                          str(body.get("match") or "")[:60]))
        if path == "/library/download":
            return self._send(*start_download(str(body.get("repo") or "").strip(), str(body.get("stem") or ""),
                                              body.get("mmproj") or None, str(body.get("token") or "")))
        m = re.fullmatch(r"/library/download/([0-9a-f]{8})/cancel", path)
        if m:
            return self._send(*cancel_download(m.group(1)))
        if path == "/library/delete":
            return self._send(*delete_model(str(body.get("id") or "")))
        m = re.fullmatch(r"/apiinfo/([\w.-]+)/settings", path)
        if m:
            code, out = api_settings(m.group(1), body, force=force)
            return self._send(code, out)
        m = re.fullmatch(r"/apiinfo/([\w.-]+)/test", path)
        if m:
            code, out = api_test(m.group(1))
            return self._send(code, out)
        m = re.fullmatch(r"/profile/([\w.-]+)", path)
        if m:
            code, out = do_profile(m.group(1), force=force)
            return self._send(code, out)
        m = re.fullmatch(r"/all/(start|stop|restart)", path)
        if m:
            code, out = do_all(m.group(1), force=force)
            return self._send(code, out)
        if path == "/machine":
            return self._send(*save_machine(body))
        if path == "/slots/add":
            return self._send(*add_slot(body))
        m = re.fullmatch(r"/(llama|sd)/build(/cancel)?", path)
        if m:
            return self._send(*(cancel_build if m.group(2) else start_build)(m.group(1)))
        if path == "/library/hf/tree":
            return self._send(*hf_tree(str(body.get("repo") or "").strip(), str(body.get("token") or "")))
        if path == "/sd/download":
            return self._send(*sd_download(body.get("spec"), str(body.get("token") or "")))
        if path == "/sd/delete":
            return self._send(*sd_delete(str(body.get("id") or "")))
        if path == "/sd/admit":
            mdl = sd_manifest().get(str(body.get("model") or ""))
            if not mdl:
                return self._send(404, {"error": "沒有這顆畫圖／影片模型"})
            try:
                prm = sd_check_defaults({**mdl["defaults"], **{k: body[k] for k in ("width", "height", "frames") if body.get(k)}}, mdl["kind"])
            except ValueError as ex:
                return self._send(400, {"error": str(ex)})
            return self._send(200, sd_admit(mdl, prm["width"], prm["height"], prm.get("frames") or 1))
        if path == "/sd/generate":
            return self._send(*sd_generate(body))
        m = re.fullmatch(r"/sd/jobs/([0-9a-f]{8})/(cancel|delete)", path)
        if m:
            return self._send(*(sd_cancel if m.group(2) == "cancel" else sd_job_delete)(m.group(1)))
        if path == "/profiles/save":
            return self._send(*save_profile(body))
        m = re.fullmatch(r"/profiles/([a-z][a-z0-9_-]{0,31})/delete", path)
        if m:
            return self._send(*delete_profile(m.group(1)))
        m = re.fullmatch(r"/slot/([\w.-]+)/(edit|delete)", path)
        if m:
            return self._send(*(edit_slot(m.group(1), body) if m.group(2) == "edit" else delete_slot(m.group(1))))
        # ⚠️ 不提供「經 API 覆寫整份登錄檔」：拿到權杖的人就能把欄位的記錄檔指到 ~/.ssh/id_rsa，
        # 再從 /logs 讀出來。網頁上的管理只走上面幾個白名單端點（save_machine／add_slot／edit_slot）。
        return self._send(404, {"error": "找不到"})


def main():
    for st in (sys.stdout, sys.stderr):
        # Windows 的主控台或管線預設 cp1252／cp950，印中文會直接例外結束：一律改 UTF-8，印不出的字換掉
        if st is not None and hasattr(st, "reconfigure"):
            st.reconfigure(encoding="utf-8", errors="replace")
    if sys.stdout is None:
        # Windows 用 pythonw 在背景跑時沒有主控台：訊息寫進記錄檔，不然出錯時什麼都看不到
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(STATE_DIR, "agent.log"), "a", encoding="utf-8", buffering=1)
    registry()
    ensure_cert()
    if not SYSTEMD:
        SUP.boot()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    srv = AgentTLSServer(("0.0.0.0", PORT), Handler)
    srv.ctx = ctx
    print(f"xconsole-agent 聽在 https://0.0.0.0:{PORT}（憑證 {CERT_FILE}，權杖在 {TOKEN_FILE}）", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--launch-llama":
        launch_llama(sys.argv[2])
    else:
        main()
