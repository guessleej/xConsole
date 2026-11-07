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
import hashlib
import io
import json
import math
import os
import re
import secrets
import shlex
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
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


def own_scope(argv):
    """編譯與產圖由代理直接帶起來，在 systemd 底下會跟代理同一個 cgroup，被代理服務的 MemoryMax=256M／CPUQuota=25% 框住
    （FLUX 產一張圖要 20 GiB 上下，編譯會被 OOM 砍掉）。改由 systemd-run --scope 開出去：同一個行程、PID 不變，
    看門狗與取消照樣用 PID 收，只是不再吃代理的資源上限。
    執行檔不在就不包：讓 Popen 當場報「啟動失敗」，而不是 systemd-run 起來之後才失敗。"""
    if SYSTEMD and shutil.which(str(argv[0])) and _scope_ok():
        return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", *argv]
    return argv


@functools.cache
def _scope_ok():
    """連不上使用者的 systemd（沒有 user bus 的容器、CI）就照原樣執行，不要每件工作都失敗"""
    try:
        return subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "true"],
                              capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

if OS == "windows":
    class _NoWindowPopen(subprocess.Popen):
        """Windows 用 pythonw 在背景跑時，每帶起一支主控台程式（nvidia-smi、tasklist、typeperf…）就會閃一個黑色視窗。
        整支代理的子行程一律加 CREATE_NO_WINDOW，不必每個呼叫點各自記得帶。"""
        def __init__(self, *a, **kw):
            kw.setdefault("creationflags", 0x08000000)
            super().__init__(*a, **kw)
    subprocess.Popen = _NoWindowPopen
REGISTRY = os.path.join(STATE_DIR, "registry.json")
TOKEN_FILE = os.path.join(STATE_DIR, "token")
PORT = int(os.environ.get("XCONSOLE_AGENT_PORT", "9401"))
# 桌面版（xConsole 應用程式帶起來的）：給一般人單機用。新模型預設只給這台自己用（不會跳防火牆詢問）、
# 預設不先思考（回答快）；第一次啟動直接套用偵測到的記憶體架構（一般人不會也不必懂這個）
DESKTOP_AGENT = os.environ.get("XCONSOLE_DESKTOP") == "1"
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

# 外部指令輸出的編碼：Windows 的命令列工具（typeperf、PowerShell、tasklist…）用系統的 OEM 字碼頁（繁中 Windows 是 cp950），
# 桌面版用 UTF-8 模式跑 Python，照 UTF-8 解會整段例外、讀數變 0（HP 實機踩到：顯示卡已用量永遠 0）。解不出的字換掉，不丟例外
CMD_TEXT = {"encoding": "oem", "errors": "replace"} if os.name == "nt" else {"errors": "replace"}


def sh(argv, timeout=20):
    """跑一條指令，回 (returncode, stdout, stderr)。失敗不丟例外。
    一律傳參數清單、不經過 shell：服務名稱、路徑、主機名稱這些值不管長怎樣都不會被當成指令解讀
    （以前是組字串＋shlex.quote，漏一個 quote 就是命令注入；CodeQL py/command-line-injection）。"""
    if not isinstance(argv, (list, tuple)):
        raise TypeError("sh() 只收參數清單，不收整條指令字串")
    try:
        p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, **CMD_TEXT, timeout=timeout)
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
    if machine.get("memory_model") in ("unified_single", "discrete"):
        # 預設底線是照大機器定的（DGX Spark 120 GB 留 12 GB）。記憶體小的機器（32 GB 筆電）照比例留，
        # 至少 4 GB（LocModel 也是保留 4 GB 給系統）；AI 395 切兩塊的那條是實測出來的，不縮
        try:
            total = float(ram_pool().get("total_gib") or 0)
        except Exception:  # noqa: BLE001
            total = 0
        if total:
            base["ram"] = round(min(base["ram"], max(4.0, total * 0.18)), 1)
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


def probe_intel_sysfs(base=None):
    """Linux 的 Intel 顯示卡（llama.cpp 走 Vulkan）。Arc 獨立顯示卡的顯存讀 sysfs（xe 驅動在 tile0、i915 在卡的資料夾）；
    讀不到顯存的當成內建顯示晶片（跟系統共用記憶體）。已用量沒有便宜又可靠的讀法，記 0。"""
    base = base or "/sys/class/drm"
    if OS != "linux" or not os.path.isdir(base):
        return None
    gpus = []
    for card in sorted(os.listdir(base)):
        if not re.fullmatch(r"card\d+", card):
            continue
        dev = os.path.join(base, card, "device")
        if (load_text(os.path.join(dev, "vendor")) or "").strip() != "0x8086" \
                or not (load_text(os.path.join(dev, "class")) or "").strip().startswith("0x03"):
            continue
        vram = 0
        for f in (os.path.join(dev, "tile0", "physical_vram_size_bytes"), os.path.join(base, card, "lmem_total_bytes")):
            v = (load_text(f) or "").strip()
            if v.isdigit() and int(v) > 0:
                vram = int(v)
                break
        gpus.append({"id": card[4:], "name": "Intel Arc 顯示卡" if vram else "Intel 內建顯示晶片", "temp": None, "power": None,
                     "use": None, "vram_total": vram, "vram_used": 0, "vendor": "intel"})
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
        cards[0]["vram_used"] = _win_gpu_used()
    return cards or None


_TYPEPERF = {"at": 0.0, "val": 0, "busy": False}
_TYPEPERF_LOCK = threading.Lock()


def _typeperf_read():
    _, out, _ = sh(["typeperf", r"\GPU Adapter Memory(*)\Dedicated Usage", "-sc", "1"], timeout=15)
    rows = [ln for ln in out.splitlines() if ln.startswith('"') and "/" in ln[:15]]
    val = 0
    if rows:
        vals = [float(x.strip('"')) for x in rows[-1].split(",")[1:] if re.fullmatch(r'"[\d.]+"', x.strip())]
        val = int(max(vals)) if vals else 0                         # 計數器以 LUID 命名對不回登錄檔，取最大那張
    return val


def _typeperf_refresh():
    try:
        val = _typeperf_read()
        _TYPEPERF.update(at=time.time(), val=val)
    finally:
        _TYPEPERF["busy"] = False


def _win_gpu_used():
    """GPU 專用記憶體已用量。typeperf 起一次要 2 秒，頁面每次載入都會問：
    過期了先回上一次的數字、在背景重查（不讓使用者等）；只有從來沒查過的第一次要等。"""
    if not _TYPEPERF["at"]:
        _TYPEPERF.update(at=time.time(), val=_typeperf_read())
    elif time.time() - _TYPEPERF["at"] > 15:
        with _TYPEPERF_LOCK:
            if not _TYPEPERF["busy"]:
                _TYPEPERF["busy"] = True
                threading.Thread(target=_typeperf_refresh, daemon=True).start()
    return _TYPEPERF["val"]


def probe_gpus():
    return apply_gpu_override(probe_nvidia() or probe_amd_sysfs() or probe_intel_sysfs() or probe_apple() or probe_win_adapters() or [])


GPU_VENDORS = ("amd", "intel", "nvidia", "apple")


def gpu_override():
    """管理者在「這台的設定」手動指定的顯示卡：None＝自動偵測；"cpu"＝不用 GPU；
    {"name", "vendor", "vram_gib"}＝偵測不到或顯存讀錯時，照這張卡算。"""
    try:
        ov = registry()["machine"].get("gpu_override")
    except Exception:  # noqa: BLE001  # 登錄檔壞掉時照自動偵測，不擋硬體探測
        return None
    return ov if ov == "cpu" or isinstance(ov, dict) else None


def apply_gpu_override(gpus, used_bytes=None):
    ov = gpu_override()
    if ov is None:
        return gpus
    if ov == "cpu":
        return []
    used = used_bytes if used_bytes is not None else sum(g.get("vram_used") or 0 for g in gpus)
    total = float(ov["vram_gib"]) * GIB
    return [{"id": "0", "name": ov["name"], "vendor": ov["vendor"], "vram_total": total,
             "vram_used": min(used, total), "manual": True}]


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
    """硬體量測，套上管理者手動指定的顯示卡（偵測錯時的補救）。"""
    hw = _hardware_raw()
    if gpu_override() is None:
        return hw
    g = dict(hw.get("gpu") or {})
    cards = apply_gpu_override(g.get("gpus") or [], g.get("vram_used"))
    g.update(count=len(cards), gpus=cards, vram_total=sum(c["vram_total"] for c in cards),
             vram_used=sum(c["vram_used"] for c in cards))
    return {**hw, "gpu": g, "gpu_manual": True}


_PORT_SEEN = {}


def _port_open(url, ttl=60):
    """本機那個埠有沒有人在聽：先用 0.3 秒試連，沒有就記 60 秒。
    Windows 連沒人聽的本機埠不會馬上被拒絕，會重送到約 2 秒才放棄（HP 實測：每頁多等 4 秒）。"""
    u = urllib.parse.urlparse(url)
    hit = _PORT_SEEN.get(u.netloc)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        socket.create_connection((u.hostname, u.port), timeout=0.3).close()
        ok = True
    except OSError:
        ok = False
    _PORT_SEEN[u.netloc] = (time.time(), ok)
    return ok


def _hardware_raw():
    """先用同機已在跑的 node_agent，拿不到才自己量。拿不到就老實標未知，不假裝零。"""
    for url in () if DESKTOP_AGENT else NODE_AGENTS:      # 桌面版（一般人的電腦）不會有機房的監控程式
        if not _port_open(url):
            continue
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
    # 筆電 APU 的內顯吃系統記憶體：照雙通道 DDR5-5600 的 89.6 GB/s（LPDDR5X-7500 機種會到 120，管理者可在這台的設定改）
    ("Radeon 890M", 89.6), ("Radeon 880M", 89.6), ("Radeon 780M", 89.6), ("Radeon 760M", 89.6),
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
        h = re.sub(r"\((?:TM|R)\)|[™®]", "", h, flags=re.I)       # 「AMD Radeon(TM) 890M Graphics」→「AMD Radeon 890M Graphics」
        h = re.sub(r"\s+", " ", h)
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
    c["os"] = OS
    # 依這台實測校正的速度倍數（record_speed 算的）：推薦清單與預估速度都乘上它
    c["speed_factor"] = machine.get("speed_factor")
    c["speed_factor_n"] = machine.get("speed_factor_n") or 0
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


def _win_proc(pid):
    """Windows：直接問系統這個 PID 的程式名稱與記憶體（tasklist 起一次要 0.7 秒，頁面每次載入要問好幾個 PID）。
    回 (名稱小寫, 工作集位元組) ；行程不在回 ("", 0)；問不到（權限、舊系統）回 None，呼叫端改用 tasklist。"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000 | 0x0010, False, int(pid)) or k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            # 87＝參數不對：這個 PID 不存在；其他（例如 5 存取被拒）交給 tasklist 再問一次
            return ("", 0) if k32.GetLastError() == 87 else None
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:
                return "", 0                                         # 已經結束（STILL_ACTIVE＝259）
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            name = os.path.basename(buf.value).lower() if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                    [(f, ctypes.c_size_t) for f in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                     "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                     "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            ws = pmc.WorkingSetSize if k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb) else 0
            return (name or None), ws
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return None


def proc_image(pid):
    """這個 PID 的程式名稱（不含路徑、小寫）；不存在回空字串。"""
    if not pid:
        return ""
    if OS == "windows":
        got = _win_proc(pid)
        if got is not None and got[0] is not None:
            return got[0]
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
        got = _win_proc(pid)
        if got is not None and got[0] is not None:
            return round(got[1] / GIB, 2)
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

    def stop_all(self):
        """應用程式關掉時：停掉這次帶起來的模型服務（啟用狀態不動，下次打開會照樣帶起來）。"""
        self.running = False
        with self.lock:
            for unit in list(self._meta()):
                self._stop(unit)

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


# ---------------------------------------------------------------- 載入進度
# 換模型的請求本身是同步的（等到好才回），前端在請求進行中每秒問一次 /slot/{id}/progress。
# 階段：prepare（行程剛起、還沒開始讀）→ read（讀權重）→ alloc（記錄檔出現 KV／context 配置）
#       → warmup（記錄檔出現 warming up）→ done（/health 回 200）。
# 讀了多少：Linux 看 /proc/<pid>/io（rchar 涵蓋從頁快取讀的、read_bytes 涵蓋檔案映射的缺頁讀取，取大的）；
# 其他系統用行程的 RSS 估；都拿不到 pct 給 None。
_LOAD = {}
_LOAD_LOCK = threading.Lock()
LOAD_KEEP = 600                  # 載入結束後，結果再留 10 分鐘給畫面看
ALLOC_RX = re.compile(r"llama_context:|llama_kv_cache|kv_cache_init|KV buffer size|KV self size|graph_reserve", re.I)
WARMUP_RX = re.compile(r"warming up|warmup", re.I)
READY_RX = re.compile(r"server is listening|all slots are idle|model loaded", re.I)


def _model_bytes(path):
    try:
        return sum(os.path.getsize(x) for x in shard_paths(path) if os.path.exists(x))
    except (OSError, TypeError):
        return 0


def _load_begin(slot_id, model_path, log_path, unit, port):
    with _LOAD_LOCK:
        _LOAD[slot_id] = {"state": "loading", "t0": time.time(), "t1": None, "model": model_path,
                          "total": _model_bytes(model_path or ""), "log": log_path, "log_off": None,
                          "unit": unit, "port": port, "message": "", "since": None, "stage": "prepare"}


def _load_mark(slot_id):
    """服務重啟的那一刻：記下記錄檔目前的長度，之後只看新寫的那一段（記錄檔是一直往後加的）。"""
    with _LOAD_LOCK:
        rec = _LOAD.get(slot_id)
        if rec:
            rec["since"] = time.time()
            try:
                rec["log_off"] = os.path.getsize(rec["log"]) if rec.get("log") else None
            except OSError:
                rec["log_off"] = 0


def _load_end(slot_id, ok, message=""):
    with _LOAD_LOCK:
        rec = _LOAD.get(slot_id)
        if rec and rec["state"] == "loading":
            rec.update(state="ready" if ok else "failed", t1=time.time(), message=message[:300])


def proc_read_bytes(pid):
    """這個行程到目前讀了多少位元組（只有 Linux 有）。拿不到回 None。"""
    if OS != "linux" or not pid:
        return None
    text = load_text(f"/proc/{int(pid)}/io")
    if not text:
        return None
    vals = dict(re.findall(r"^(\w+):\s*(\d+)", text, re.M))
    got = [int(vals[k]) for k in ("rchar", "read_bytes") if k in vals]
    return max(got) if got else None


def _unit_pid(unit):
    try:
        _, pid, _ = sysctl(unit, "show -p MainPID --value", timeout=10)
    except Exception:  # noqa: BLE001
        return None
    pid = (pid or "").strip()
    return int(pid) if pid.isdigit() and pid != "0" else None


def _load_log(rec, limit=2 << 20):
    """這一次載入之後寫進記錄檔的內容。llama.cpp 的服務寫自己的記錄檔；其他 systemd 服務看 journal。"""
    if rec.get("log") and rec.get("log_off") is not None:
        try:
            with open(rec["log"], "rb") as fh:
                fh.seek(0, os.SEEK_END)
                end = fh.tell()
                start = max(rec["log_off"], end - limit)
                fh.seek(start)
                return fh.read(end - start).decode("utf-8", "replace")
        except OSError:
            return ""
    if SYSTEMD and rec.get("unit") and rec.get("since"):
        try:
            flag = ["--user"] if unit_scope(rec["unit"]) == "user" else []
            _, out, _ = sh(["journalctl", *flag, "-u", unit_name(rec["unit"]), "--since", f"@{int(rec['since'])}",
                            "-n", "400", "--no-pager", "-o", "cat"], timeout=10)
            return out
        except ValueError:
            return ""
    return ""


def _port_ready(port):
    if not port:
        return False
    st = _http_json(f"http://127.0.0.1:{int(port)}/health", timeout=3)[0]
    if st == 200:
        return True
    if st in (0, 404):                     # 沒有 /health 的引擎（Strata）：模型清單回得出來就算好了
        return _http_json(f"http://127.0.0.1:{int(port)}/v1/models", timeout=3)[0] in (200, 401)
    return False


def load_progress(rec, pid=None, read_bytes=None, rss_gib=None, log_text="", ready=False, now=None):
    """把量到的數字換成畫面要的格式（純函式，測試直接餵數字）。"""
    now = now or time.time()
    total = rec.get("total") or 0
    elapsed = round(now - rec["t0"], 1)
    out = {"state": rec["state"], "stage": "prepare", "pct": None, "read_gib": None,
           "total_gib": round(total / GIB, 2) if total else None, "elapsed_sec": elapsed, "eta_sec": None,
           "message": rec.get("message") or ""}
    if rec["state"] == "ready":
        out.update(stage="done", pct=100.0, read_gib=out["total_gib"], eta_sec=0,
                   elapsed_sec=round((rec.get("t1") or now) - rec["t0"], 1))
        return out
    got = read_bytes
    if got is None and rss_gib:
        got = rss_gib * GIB                # 不是 Linux：用常駐記憶體估（統一記憶體機的 GPU 配置不一定算在裡面）
    if got is not None and total:
        out["read_gib"] = round(min(got, total) / GIB, 2)
        out["pct"] = round(min(99.0, 100.0 * got / total), 1)
    stage = "prepare"
    if pid and got:
        stage = "read"
    if ALLOC_RX.search(log_text or ""):
        stage = "alloc"
    if WARMUP_RX.search(log_text or ""):
        stage = "warmup"
    if ready or READY_RX.search(log_text or ""):
        stage = "done"
    if stage in ("alloc", "warmup", "done") and total:
        out.update(pct=100.0 if stage == "done" else max(out["pct"] or 0, 99.0), read_gib=out["total_gib"])
    if stage == "read" and out["pct"] and out["pct"] >= 2:
        out["eta_sec"] = round(elapsed * (100 - out["pct"]) / out["pct"])
    # 失敗時回報「卡在哪一個階段」：輪詢時記下的最後一個階段
    out["stage"] = stage if rec["state"] == "loading" else (rec.get("stage") or stage)
    return out


def slot_progress(slot_id):
    slot = _slot(slot_id)
    if not slot:
        return 404, {"error": "沒有這個欄位"}
    with _LOAD_LOCK:
        rec = dict(_LOAD.get(slot_id) or {})
    now = time.time()
    if not rec or (rec["state"] != "loading" and now - (rec.get("t1") or now) > LOAD_KEEP):
        # 沒有進行中的換模型：照服務本身的狀態回
        st = unit_state(slot["unit"]) if slot.get("unit") else "none"
        port = (slot_config(slot) or {}).get("port") or slot.get("port")
        if st == "active" and _port_ready(port):
            return 200, {"state": "ready", "stage": "done", "pct": 100.0, "read_gib": None, "total_gib": None,
                         "elapsed_sec": None, "eta_sec": None, "message": ""}
        state = {"activating": "loading", "active": "loading", "failed": "failed"}.get(st, "idle")
        return 200, {"state": state, "stage": "prepare" if state == "loading" else None, "pct": None, "read_gib": None,
                     "total_gib": None, "elapsed_sec": None, "eta_sec": None,
                     "message": "服務起來了，還在載入" if state == "loading" else ""}
    if rec["state"] != "loading":
        return 200, load_progress(rec, now=now)
    if not rec.get("since"):
        return 200, {**load_progress(rec, now=now), "message": "檢查記憶體、準備設定"}
    pid = None
    if rec.get("unit"):
        try:
            pid = SUP._pid(unit_name(rec["unit"])) if not SYSTEMD else _unit_pid(rec["unit"])
        except ValueError:
            pid = None
    rb = proc_read_bytes(pid)
    rss = proc_rss_gib(pid) if rb is None and pid else None
    text = _load_log(rec)
    out = load_progress(rec, pid, rb, rss, text, ready=_port_ready(rec.get("port")) if pid else False, now=now)
    with _LOAD_LOCK:
        if slot_id in _LOAD and _LOAD[slot_id]["state"] == "loading":
            _LOAD[slot_id]["stage"] = out["stage"]
    return 200, out


QUANT_RX = re.compile(r"[-_.]((?:UD-)?(?:I?Q\d\w*|TQ\d\w*|F16|BF16|MXFP4\w*))$", re.I)   # TQ1_0／TQ2_0：三元量化


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
    _load_begin(slot_id, pick["native"], slot.get("log"), slot["unit"], (new_cfg or {}).get("port") or slot.get("port"))
    _load_mark(slot_id)
    rc, out, err = sysctl(slot["unit"], "restart")
    ok, why = wait_healthy(slot, seconds=240)
    _load_end(slot_id, ok, "" if ok else f"{pick['name']} 載不起來（{why}），正在還原")
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
        "run": run_options(slot, raw) if llama else None,
    }


RUN_CTX = (8192, 16384, 32768, 65536, 131072)


def run_options(slot, raw):
    """執行設定頁：每一種上下文長度載下去放不放得下（用准入同一條公式試算，不動真的設定）。"""
    cfg = slot_config(slot) or {}
    prof = _profile_cached(raw.get("model", "")) or {}
    par = int(cfg.get("parallel") or 1)
    top = prof.get("ctx_train") or max(RUN_CTX)
    out = []
    for ctx in [c for c in RUN_CTX if c <= top] or [min(RUN_CTX)]:
        per = (prof.get("kv_full_bpt", 0) + prof.get("kv_swa_bpt", 0) * min(1.0, (prof.get("swa_window") or ctx) / ctx)) if prof else None
        what = {**cfg, "max_context": ctx, **({"kv_bytes_per_cell": int(per)} if per else {})}
        v = admit(slot["id"], cfg_override=what)
        worst = min([d.get("worst") for d in (v.get("pools") or {}).values() if isinstance(d, dict) and "worst" in d] or [None],
                    key=lambda x: x if x is not None else 1e9)
        out.append({"ctx": ctx, "kv_gib": round(llama_kv_gib(prof, ctx, par), 2) if prof else None, "allow": v["allow"],
                    "worst_gib": worst, "reason": (v.get("reasons") or [None])[0]})
    size = sum(os.path.getsize(x) for x in shard_paths(raw["model"]) if os.path.exists(x)) / GIB if raw.get("model") else 0
    load = (registry().get("load_sec") or {}).get(os.path.abspath(raw.get("model") or ""))
    real = os.path.realpath(raw.get("model") or "")
    store = max((x for x in storage_choices() if real.startswith(x["mount"].rstrip("/\\") + os.sep) or real == x["mount"]),
                key=lambda x: len(x["mount"]), default=None)
    lay = _run_layout(slot, raw, prof, size, int(cfg.get("max_context") or raw.get("ctx") or 4096), par)
    mm = registry()["machine"].get("memory_model", "discrete")
    return {"options": out, "ctx_train": prof.get("ctx_train"), "size_gib": round(size, 1), "load_sec": load,
            "split": lay["split"] if lay else None, "mode": lay["mode"] if lay else None,
            "tiered": bool(lay and lay.get("tiered")), "cpu_moe": (lay or {}).get("cpu_moe") or 0,
            "disk_mode": bool(raw.get("disk")), "disk_allowed": mm != "unified_split",
            "disk_needed": bool(lay and lay["mode"] == "disk"),
            "disk_note": None if mm != "unified_split" else DISK_DENIED,
            "bandwidth_gbs": chip().get("bandwidth_gbs"), "hdd": (store or {}).get("hdd"),
            "profile": {k: prof.get(k) for k in ("active_frac", "layers", "moe", "arch")},
            "measured_tok_s": ((registry().get("speed") or {}).get(os.path.abspath(raw.get("model") or "")) or {}).get("tok_s"),
            "speed_factor": registry()["machine"].get("speed_factor"),
            "cmd": llama_cmd_text(raw) if raw.get("exe") and raw.get("model") else None,
            "extra_args": " ".join(shlex.quote(t) for t in llama_custom_args(raw)),
            "auto_layers_off": bool({t.split("=", 1)[0] for t in llama_custom_args(raw)} & LLAMA_AUTO_LAYERS),
            "thinking": raw.get("reasoning") or "auto", "lan": (raw.get("host") or "0.0.0.0") != "127.0.0.1",
            "thinking_supported": bool(reasoning_args(raw["exe"], "off")) if raw.get("exe") else False}


def _run_layout(slot, raw, prof, size_gib, ctx, par):
    """執行設定頁的分層：照目前這台剩下的記憶體試算一般放法；放不下而且這台允許，再試用硬碟的放法。"""
    if not raw.get("model") or not size_gib or not prof:
        return None
    reg = registry()
    m = {**prof, "size_gib": size_gib, "mmproj_gib": round(os.path.getsize(raw["mmproj"]) / GIB, 2)
         if raw.get("mmproj") and os.path.exists(raw["mmproj"]) else 0.0}
    p = pools(reg, hardware())
    cards = p["gpus"]
    back_v, back_r = _usage_reclaim(slot) if _alive(slot) else (0.0, 0.0)
    lay = llama_layout(m, reg["machine"], cards, slot.get("gpu_ids"), ctx, par, back_v)
    mm = reg["machine"].get("memory_model", "discrete")
    if mm == "unified_single":
        room = p["unified"]["free_gib"] + max(back_v, back_r) - floors(reg["machine"])["ram"]
        fits = lay["split"]["gpu"] <= room
    else:
        fits = (not cards or lay["est_vram"] + lay["kv"] <= p["vram"]["free_gib"] + back_v - floors(reg["machine"])["vram"]) \
            and lay["est_ram"] <= p["ram"]["free_gib"] + back_r - floors(reg["machine"])["ram"]
    if fits or mm == "unified_split":
        return lay
    dl = disk_layout(m, reg["machine"], cards, slot.get("gpu_ids"), ctx, par, back_v, ram_pool()["free_gib"] + back_r)
    return dl or lay


SPEED_EFF, SPEED_LAYER_MS, SPEED_DEFAULT_BW = 0.6, 0.1, 50   # 跟 console/recommend.py 的 EFFICIENCY、LAYER_MS、DEFAULT_RAM_BW 一致
_SPEED_LAST = {}
_SPEED_SEEN = {}


def estimate_decode(prof, size_gib, machine):
    """公式估的生成速度（全部放進快速層時）：跟推薦清單同一條公式，拿來跟實測比出校正倍數。"""
    c = chip(machine)
    bw = c.get("bandwidth_gbs") or c.get("ram_bandwidth_gbs") or SPEED_DEFAULT_BW
    act = prof.get("active_frac") or 1.0
    t = size_gib * act * GIB / (SPEED_EFF * bw * 1e9) + (prof.get("layers") or 0) * SPEED_LAYER_MS / 1000
    return 1 / t if t > 0 else None


def record_speed(slot_id, tok_s, n_tokens):
    """一次對話的實測生成速度：記在這顆模型上（模型庫顯示實測值）。
    一般放法（全部放進快速層、沒有搬專家、沒有用硬碟）時，另外跟公式估的比，更新這台的校正倍數。
    太短的回答（不到 32 個 token）不算，啟動成本會讓數字偏低。最多每 20 秒寫一次登錄檔。"""
    if not tok_s or not n_tokens or n_tokens < 32 or not 0.01 < tok_s < 100000:
        return
    slot = _slot(slot_id) or {}
    raw = (load_json(slot.get("config"), {}) or {}) if slot.get("kind") == "llama" and slot.get("config") else {}
    key = os.path.abspath(raw["model"]) if raw.get("model") else f"{slot.get('kind')}:{slot.get('model')}"
    # 載入後的第一個回答不算：Vulkan／Metal 第一次要編譯著色器，HP 實測第一次 22.6、第二次 47.6 tok/s
    loaded = (_LOAD.get(slot_id) or {}).get("t0") or 0
    seen = _SPEED_SEEN.get((slot_id, key))
    if seen is None or seen < loaded:
        _SPEED_SEEN[(slot_id, key)] = time.time()
        return
    now = time.time()
    if now - _SPEED_LAST.get(key, 0) < 5:
        return
    _SPEED_LAST[key] = now
    with REG_LOCK:
        reg = registry()
        sp = reg.setdefault("speed", {}).setdefault(key, {})
        sp["tok_s"] = round(tok_s if not sp.get("tok_s") else 0.7 * sp["tok_s"] + 0.3 * tok_s, 2)
        sp["n"] = int(sp.get("n") or 0) + 1
        sp["at"] = int(now)
        normal = raw.get("model") and not raw.get("disk") and not raw.get("cpu_moe") and int(raw.get("ngl", 999)) >= 999 \
            and not llama_custom_args(raw, reg)
        if normal:
            prof = _profile_cached(raw["model"]) or {}
            size = sum(os.path.getsize(x) for x in shard_paths(raw["model"]) if os.path.exists(x)) / GIB
            est = estimate_decode(prof, size, reg["machine"]) if prof and size else None
            if est:
                f = max(0.2, min(5.0, tok_s / est))
                m = reg["machine"]
                m["speed_factor"] = round(f if not m.get("speed_factor") else 0.7 * m["speed_factor"] + 0.3 * f, 3)
                m["speed_factor_n"] = int(m.get("speed_factor_n") or 0) + 1
        save_json(REGISTRY, reg)


def record_load(model_path, seconds):
    """記下這顆模型實際載入花了幾秒（執行設定頁顯示「上次載入 N 秒」，是實測不是估的）。"""
    with REG_LOCK:
        r = registry()
        r.setdefault("load_sec", {})[os.path.abspath(model_path)] = round(seconds)
        save_json(REGISTRY, r)


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
        if "thinking" in body or "lan" in body:
            if slot.get("kind") != "llama":
                errors.append("思考模式與區網存取只有 llama.cpp 的欄位能在這裡改")
            else:
                th = body.get("thinking")
                if th is not None:
                    if th not in ("on", "off", "auto"):
                        errors.append("思考模式只能是 on、off、auto")
                    elif th != (raw.get("reasoning") or "auto"):
                        want["thinking"] = th
                if "lan" in body:
                    host = "0.0.0.0" if body.get("lan") else "127.0.0.1"
                    if host != (raw.get("host") or "0.0.0.0"):
                        want["lan"] = host
        if "extra_args" in body:
            if slot.get("kind") != "llama":
                errors.append("自訂啟動引數只有 llama.cpp 的欄位能設")
            else:
                toks, err = parse_llama_args(body.get("extra_args"))
                if err:
                    errors.append(err)
                elif toks != llama_custom_args(raw):
                    want["extra_args"] = toks
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


def llama_args_get(model_id):
    """執行頁「高階：啟動引數」：這顆模型的自訂引數；有欄位正在用它跑時，附上完整的啟動指令。"""
    reg = registry()
    _, models, _ = scan_gguf(reg)
    m = next((x for x in models if x["id"] == model_id), None)
    if not m:
        return 404, {"error": "存放區裡沒有這顆"}
    toks = llama_custom_args({"model": m["path"]}, reg)
    cmd = None
    for sl in reg["slots"]:
        if sl.get("kind") == "llama" and sl.get("config"):
            raw = load_json(sl["config"], {}) or {}
            if os.path.abspath(raw.get("model") or "") == os.path.abspath(m["path"]) and raw.get("exe"):
                cmd = llama_cmd_text(raw, reg)
                break
    return 200, {"model": model_id, "args": " ".join(shlex.quote(t) for t in toks),
                 "auto_layers_off": bool({t.split("=", 1)[0] for t in toks} & LLAMA_AUTO_LAYERS), "cmd": cmd,
                 "allowed": sorted(LLAMA_ARG_OK)}


def llama_args_set(model_id, text):
    _, models, _ = scan_gguf()
    m = next((x for x in models if x["id"] == model_id), None)
    if not m:
        return 404, {"error": "存放區裡沒有這顆"}
    toks, err = parse_llama_args(text)
    if err:
        return 400, {"error": err}
    set_custom_args(m["path"], toks)
    return 200, {"ok": True, "args": " ".join(shlex.quote(t) for t in toks),
                 "auto_layers_off": bool({t.split("=", 1)[0] for t in toks} & LLAMA_AUTO_LAYERS)}


def set_custom_args(model_path, toks):
    with REG_LOCK:
        r = registry()
        d = r.setdefault("llama_custom_args", {})
        key = os.path.abspath(model_path or "")
        if toks:
            d[key] = list(toks)
        else:
            d.pop(key, None)
        save_json(REGISTRY, r)


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
        elif k == "thinking":
            new["reasoning"] = v
        elif k == "lan":
            new["host"] = v                    # 0.0.0.0＝區網連得到；127.0.0.1＝只有這台自己
            new["lan_chosen"] = True           # 使用者自己選的：桌面版換模型時照樣沿用
    old_custom = llama_custom_args(old)
    if "extra_args" in want:
        set_custom_args(old.get("model"), want["extra_args"])
    save_json(cfg_path, new)
    os.chmod(cfg_path, 0o600)

    def revert(msg):
        if "extra_args" in want:
            set_custom_args(old.get("model"), old_custom)
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
    t0 = time.time()
    ok, why = wait_healthy(slot, seconds=900)
    if not ok:
        return revert(f"套用後服務起不來（{why}）")
    record_load(new.get("model") or "", time.time() - t0)
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
EXT_MIN_BYTES = 50 << 20                  # 其他模型資料夾裡小於這個的 .gguf 不列（詞表、測試檔）
EXT_MAX_DIRS = 400                        # 其他模型資料夾最多走這麼多個子資料夾，免得選到整顆碟時掃很久
MODEL_DIRS_MAX = 8
ENGINE_DIR = os.path.join(STATE_DIR, "engines")
DOWNLOADS_FILE = os.path.join(STATE_DIR, "downloads.json")
SAFE_ID_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def inside(base, name):
    """組出 base 底下的路徑，並確認真的落在 base 裡面（擋 ../ 與符號連結跳出去）。
    欄位 id、模型 id 雖然都先比對過登錄檔與掃描清單，組路徑時一律再過這一道。"""
    root = os.path.realpath(base)
    full = os.path.realpath(os.path.join(root, name))
    # 磁碟根目錄（C:\、/）本身就以分隔字元結尾，不能再加一個
    if not full.startswith(root if root.endswith(os.sep) else root + os.sep) or full == root:
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


# 跟模型放在一起、但自己不能跑的 GGUF：MTP／DFlash／EAGLE 推測解碼草稿頭、imatrix 量化校正資料
AUX_GGUF_RX = re.compile(r"(?<![a-z])(mtp|dflash|eagle\d*|imatrix)(?![a-z])", re.I)


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


def _num(v):
    """檔頭的數值欄位：新架構有的寫成逐層陣列（Nemotron 3.5 的 expert_used_count 每層一個，非 MoE 層是 0），取最大值。"""
    if isinstance(v, list):
        return int(max((x for x in v if isinstance(x, (int, float))), default=0))
    return int(v or 0)


def gguf_profile(meta):
    """從檔頭推出：架構、訓練上下文、每個 token 的 KV 位元組（f16）、是否中國來源。
    KV 照每層的 KV 頭數加總（混合架構的 SSM 層 KV 頭數是 0，自然不算）；
    滑動視窗層只留視窗內的 token，在 kv_worst 那裡另外處理。"""
    if not meta:
        return None
    arch = meta.get("general.architecture") or "?"
    g = lambda k, d=None: meta.get(f"{arch}.{k}", d)  # noqa: E731
    n_layer = _num(g("block_count", 0))
    heads = g("attention.head_count", 0)
    heads = max(heads) if isinstance(heads, list) else int(heads or 0)
    emb = _num(g("embedding_length", 0))
    k_len = _num(g("attention.key_length", 0)) or (emb // heads if heads else 0)
    v_len = _num(g("attention.value_length", 0)) or k_len
    kv_heads = g("attention.head_count_kv", heads)
    per_layer = kv_heads if isinstance(kv_heads, list) else [int(kv_heads or 0)] * n_layer
    swa = _num(g("attention.sliding_window", 0))
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
    return {"arch": arch, "meta_name": name, "ctx_train": _num(g("context_length", 0)) or None,
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
    n_exp = _num(meta.get(f"{arch}.expert_count"))
    used = _num(meta.get(f"{arch}.expert_used_count"))
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


def store_hdd(root=None):
    """存放區所在的碟是不是機械硬碟（True／False；認不出來回 None）。"""
    real = os.path.realpath(root or gguf_root())
    store = max((x for x in storage_choices() if real == x["mount"] or real.startswith(x["mount"].rstrip("/") + "/")),
                key=lambda x: len(x["mount"]), default=None)
    return (store or {}).get("hdd")


def _scan_folder(full, folder, id_prefix, models, problems, external=None):
    """掃一個資料夾裡的 .gguf：同一顆模型的多個量化各自算一顆，分片要齊才算可用。"""
    try:
        files = sorted(f for f in os.listdir(full) if f.lower().endswith(".gguf"))
    except OSError:
        return
    projs = [f for f in files if is_mmproj(f)]
    groups = {}
    for f in files:
        if is_mmproj(f) or AUX_GGUF_RX.search(f[:-5]):
            continue
        base = f[:-5]
        m = SHARD_RX.search(base)
        stem = base[:m.start()] if m else base
        groups.setdefault(stem, []).append((f, int(m.group(2)) if m else 1))
    for stem, parts in groups.items():
        want = parts[0][1]
        paths = [os.path.join(full, f) for f, _ in sorted(parts)]
        mmproj = _pick_mmproj(full, projs, stem, len(groups), shared=bool(external) or not folder)
        size = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
        mm_size = os.path.getsize(mmproj) if mmproj else 0
        name, quant = pretty_model(paths[0])
        mid = f"{id_prefix}{folder or '.'}/{stem}"
        if len(parts) != want:
            problems.append({"name": mid, "missing": f"分片不齊：有 {len(parts)} 個，應有 {want} 個（下載可能還沒完成）"})
            continue
        if external and size < EXT_MIN_BYTES:
            continue                                          # 別的程式留下的小檔（詞表、測試檔）不當成模型
        prof = _profile_cached(paths[0])
        if not prof:
            problems.append({"name": mid, "missing": "讀不到 GGUF 檔頭，檔案可能損壞或還在下載"})
            continue
        models.append({
            # 存放區的用檔頭裡的正式名稱（例 Gemma-4-E4B-It），沒有才用檔名推。其他資料夾的用檔名：
            # 別人轉檔的檔頭常留著訓練時的代號（「Fable Ep2」），使用者認得的是自己下載時看到的檔名
            "engine": "llama", "id": mid, "name": name if external else prof.get("meta_name") or name, "quant": quant,
            "folder": folder or os.path.basename(full.rstrip("\\/")), "dir": full, "external": external,
            "path": paths[0], "shards": want, "size_gib": round(size / GIB, 1),
            "mmproj": mmproj, "mmproj_gib": round(mm_size / GIB, 1), "vision": bool(mmproj),
            **prof})


def _pick_mmproj(full, projs, stem, n_models, shared):
    """這顆模型配哪個看圖投影檔。存放區一顆模型一個資料夾，資料夾裡的 mmproj 就是它的。
    其他模型資料夾、存放區根目錄（shared）常是好幾顆模型平放在一起：只有一顆模型時才直接用，
    否則要 mmproj 的檔名含這顆模型的名稱（mmproj-Muse-Glimmer-30B-BF16 → Muse-Glimmer-30B-*），認不出來寧可不配，
    配錯的投影檔會讓模型起不來。"""
    if not projs:
        return None
    if not shared or n_models == 1:
        return os.path.join(full, projs[0])           # 資料夾裡只有這顆模型：投影檔都是它的（有好幾種精度取第一個）
    base = pretty_model(stem + ".gguf")[0].lower()
    hit = [f for f in projs if base and base in f.lower()]
    return os.path.join(full, hit[0]) if hit else None


def _walk_dirs(base, depth, skip=()):
    """base 與它底下 depth 層以內的資料夾（相對路徑用 / 連接）；隱藏資料夾不進去，最多走 EXT_MAX_DIRS 個。
    skip：不要走進去的資料夾（xConsole 自己的存放區、畫圖模型區；~/models 底下就是預設存放區，不然會算兩次）。"""
    skip = {os.path.normcase(os.path.realpath(x)) for x in skip}
    out, todo = [], [("", 0)]
    while todo and len(out) < EXT_MAX_DIRS:
        rel, d = todo.pop(0)
        full = os.path.join(base, *rel.split("/")) if rel else base
        if os.path.normcase(os.path.realpath(full)) in skip:
            continue
        out.append((rel, full))
        if d >= depth:
            continue
        try:
            subs = sorted(x for x in os.listdir(full) if not x.startswith(".") and os.path.isdir(os.path.join(full, x)))
        except OSError:
            continue
        todo += [((rel + "/" if rel else "") + x, d + 1) for x in subs]
    return out


def _under(path, base):
    """path 是不是 base 本身或在它底下。"""
    p, b = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(base))
    return p == b or p.startswith(b.rstrip(os.sep) + os.sep)


def model_dirs(reg=None):
    """管理者加進來的其他模型資料夾（別的程式下載的、自己放的）。只讀：xConsole 不在裡面寫入、搬動或刪檔。"""
    reg = reg or registry()
    return [d for d in (reg["machine"].get("model_dirs") or []) if isinstance(d, str)]


def dir_tag(d):
    """其他模型資料夾的代號：放進模型 id 前面，同名資料夾在不同位置也不會撞在一起。"""
    return "@" + hashlib.sha1(os.path.normcase(os.path.realpath(d)).encode("utf-8")).hexdigest()[:8]


def scan_gguf(reg=None):
    """掃存放區（第一層的每個資料夾＋直接放在根目錄的檔），再掃管理者加進來的其他模型資料夾（往下三層）。"""
    reg = reg or registry()
    root = gguf_root(reg)
    models, problems = [], []
    if os.path.isdir(root):
        _scan_folder(root, "", "", models, problems)
        for folder in sorted(os.listdir(root)):
            full = os.path.join(root, folder)
            if os.path.isdir(full) and not folder.startswith("."):
                _scan_folder(full, folder, "", models, problems)
    seen = {os.path.normcase(os.path.realpath(root))}
    own = [root, sd_root(reg)]
    for d in model_dirs(reg):
        real = os.path.normcase(os.path.realpath(d))
        if real in seen or not os.path.isdir(d) or _under(d, root):
            continue
        seen.add(real)
        for rel, full in _walk_dirs(d, 3, own + [x for x in model_dirs(reg) if x != d]):   # 巢狀加入的另一個資料夾各掃各的
            _scan_folder(full, rel, dir_tag(d) + "/", models, problems, external=d)
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


DISK_DENIED = ("這台是 BIOS 把記憶體切成兩塊的機器（例 AI 395）：用硬碟執行時，檔案映射的頁快取吃的是系統那一小塊，"
               "擠爆會把整台拖死（連代理與 SSH 一起），所以一律不允許。請換小一點的量化，或縮短上下文")


def llama_layout(m, machine, cards, gpu_ids, ctx, parallel, back_gib):
    """一般啟動（不開檔案映射）的放法：權重全部進快速層；獨立顯卡放不下的 MoE 自動把專家搬到系統記憶體。
    split 是給畫面三色條的：放顯示卡（統一記憶體機＝那一整塊）／系統記憶體／硬碟各幾 GiB。"""
    mm = machine.get("memory_model", "discrete")
    cpu_moe = auto_cpu_moe(m, machine, cards, gpu_ids, ctx, parallel, back_gib)
    est = llama_est(m, machine, ctx, parallel, cpu_moe)
    return {"mode": "cpu_moe" if cpu_moe else "normal", "cpu_moe": cpu_moe, "ngl": 999, "mmap": False,
            "est_vram": est["vram"], "est_ram": est["ram"], "kv": est["kv"], "kv_ram_frac": 0.0,
            "moved_gib": est["moved_gib"],
            "split": {"gpu": round(est["vram"] + est["kv"] + (est["ram"] if mm == "unified_single" else 0), 1),
                      "ram": round(est["moved_gib"], 1), "disk": 0.0}}


def disk_layout(m, machine, cards, gpu_ids, ctx, parallel, back_gib, ram_free_gib=None):
    """用硬碟執行：以檔案映射（mmap）啟動，權重不必全部放進記憶體，沒放進去的按需從硬碟讀。
    准入只算「不是檔案映射」的部分：KV、引擎計算緩衝、投影檔、放進 GPU 的層（那幾層是複製進顯存的，不能被換出）。
      unified_split   一律不給（回 None，呼叫端講人話）
      unified_single  Apple：-ngl 999（Metal 直接用映射的記憶體，不另外複製）；
                      其他（DGX Spark、Jetson）：-ngl 0——CUDA 會把卸載的層複製成不可換頁的記憶體，等於沒映射
      discrete        -ngl 照顯存放得下的層數；KV 跟著層走，沒放 GPU 的層的 KV 在系統記憶體
    ram_free_gib：系統記憶體現在可用多少（算頁快取放得下多少、溢出到硬碟多少），None＝不算溢出量"""
    mm = machine.get("memory_model", "discrete")
    if mm == "unified_split":
        return None
    fl = floors(machine)
    size, mmproj = float(m["size_gib"]), float(m.get("mmproj_gib") or 0)
    kv = llama_kv_gib(m, ctx, parallel)
    layers = max(1, int(m.get("layers") or 1))
    moe = moe_disk_layout(m, machine, cards, gpu_ids, ctx, parallel, back_gib, ram_free_gib)
    if moe:
        return moe
    if mm == "unified_single":
        ngl = 999 if OS == "darwin" else 0
        est_vram, est_ram, frac = round(1.0 + mmproj, 1), 1.5, 0.0
        pinned = est_vram + est_ram + kv
        cache = None if ram_free_gib is None else max(0.0, ram_free_gib - fl["ram"] - pinned)
        cached = size if cache is None else min(size, cache)
        split = {"gpu": pinned + cached, "ram": 0.0, "disk": size - cached}
        gpu_w = 0.0
    else:
        use = [c for c in cards if not gpu_ids or str(c["id"]) in gpu_ids]
        avail = sum(c["free_gib"] for c in use) + back_gib - fl["vram"] if use else 0.0
        per = size / layers + kv / layers
        room = avail - 1.0 - mmproj
        n = max(0, min(layers, int(room // per))) if use and room > 0 and per > 0 else 0
        ngl = n
        gpu_w = size * n / layers
        frac = round(1 - n / layers, 4)
        est_vram = round(gpu_w + 1.0 + mmproj, 1) if use else 0.0
        est_ram = round(2.5 + (0.0 if use else 1.0 + mmproj), 1)
        rest = size - gpu_w
        cache = None if ram_free_gib is None else max(0.0, ram_free_gib - fl["ram"] - est_ram - kv * frac)
        cached = rest if cache is None else min(rest, cache)
        split = {"gpu": est_vram + kv * (1 - frac), "ram": cached + kv * frac, "disk": rest - cached}
    return {"mode": "disk", "cpu_moe": 0, "ngl": ngl, "mmap": True, "est_vram": est_vram, "est_ram": est_ram,
            "kv": round(kv, 2), "kv_ram_frac": frac, "moved_gib": 0.0, "gpu_weights_gib": round(gpu_w, 1),
            "split": {k: round(max(0.0, v), 1) for k, v in split.items()}}


def moe_disk_layout(m, machine, cards, gpu_ids, ctx, parallel, back_gib, ram_free_gib=None):
    """MoE 模型用硬碟執行：照張量種類分層，不是整層切。
    注意力、共用權重、KV 放顯示卡（-ngl 999）→ 專家層從第一層起留在檔案映射的記憶體（--n-cpu-moe N）→
    記憶體放不下的專家由作業系統按需從 SSD 讀。每 token 只用到少數幾個專家，所以比整層搬出去快得多。
      discrete        顯卡先放注意力＋共用＋KV，剩下的空間放得下幾層專家就放幾層，其餘 N 層留在映射記憶體
      unified_single  Apple 以外（DGX Spark、Jetson）：CUDA 會把放上 GPU 的張量複製成不可換頁的記憶體，
                      所以專家全部留在映射記憶體（N＝全部），只有注意力與共用部分常駐；Apple 走原本的整層映射
    不是 MoE、拿不到專家比例、或連注意力＋KV 都放不進顯卡時回 None，由呼叫端用整層的放法。"""
    mm = machine.get("memory_model", "discrete")
    frac, n_exp = m.get("expert_layer_frac"), int(m.get("expert_layers") or 0)
    if not m.get("moe") or not frac or n_exp <= 0 or mm == "unified_split" or (mm == "unified_single" and OS == "darwin"):
        return None
    fl = floors(machine)
    size, mmproj = float(m["size_gib"]), float(m.get("mmproj_gib") or 0)
    kv = llama_kv_gib(m, ctx, parallel)
    per = frac * size                                    # expert_layer_frac＝每一層專家占整顆的比例
    experts = min(size, per * n_exp)
    dense = size - experts
    if mm == "unified_single":
        n_cpu = n_exp
        pinned = dense + 1.0 + mmproj + kv
        est_vram, est_ram = round(dense + 1.0 + mmproj, 1), 1.5
        cache = None if ram_free_gib is None else max(0.0, ram_free_gib - fl["ram"] - pinned - est_ram)
        cached = experts if cache is None else min(experts, cache)
        split = {"gpu": pinned + est_ram + cached, "ram": 0.0, "disk": experts - cached}
        gpu_w = dense
    else:
        use = [c for c in cards if not gpu_ids or str(c["id"]) in gpu_ids]
        if not use:
            return None
        avail = sum(c["free_gib"] for c in use) + back_gib - fl["vram"]
        room = avail - 1.0 - mmproj - kv - dense
        if room < 0:
            return None
        n_gpu = min(n_exp, int(room // per)) if per > 0 else n_exp
        n_cpu = n_exp - n_gpu
        gpu_w = dense + n_gpu * per
        est_vram, est_ram = round(gpu_w + 1.0 + mmproj, 1), 2.5
        rest = n_cpu * per
        cache = None if ram_free_gib is None else max(0.0, ram_free_gib - fl["ram"] - est_ram)
        cached = rest if cache is None else min(rest, cache)
        split = {"gpu": est_vram + kv, "ram": cached, "disk": rest - cached}
    if n_cpu == 0:
        return None                                     # 全部放得進顯卡就不必用硬碟執行
    return {"mode": "disk", "tiered": True, "cpu_moe": n_cpu, "ngl": 999, "mmap": True, "est_vram": est_vram,
            "est_ram": est_ram, "kv": round(kv, 2), "kv_ram_frac": 0.0, "moved_gib": 0.0,
            "gpu_weights_gib": round(gpu_w, 1), "split": {k: round(max(0.0, v), 1) for k, v in split.items()}}


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


def imported_exes(prefix):
    """手動匯入的包：照 .xconsole-exe 記的相對位置找執行檔（不能跳出那個資料夾）。"""
    out = []
    for mark in glob.glob(os.path.join(HOME, prefix + "-import-*", ".xconsole-exe")):
        d = os.path.dirname(mark)
        rel = (load_text(mark) or "").strip()
        exe = os.path.realpath(os.path.join(d, rel))
        if rel and exe.startswith(os.path.realpath(d) + os.sep) and os.path.isfile(exe):
            out.append(os.path.join(d, rel))
    return out


def llama_globs():
    """家目錄下所有 llama-server：網頁編譯的（llama.cpp-bNNNN/build/bin/）與官方預編譯的（llama.cpp-bNNNN-<平台>/）。"""
    pats = ("llama.cpp*/build*/bin/llama-server", "llama.cpp*/build*/bin/Release/llama-server", "llama.cpp*/llama-server")
    found = {c for pat in pats for c in glob.glob(os.path.join(HOME, pat + EXE))} | set(imported_exes("llama.cpp"))
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
        rc, out, err = exe_version(cand)
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
            # 用硬碟執行（檔案映射）：沒放 GPU 的層，KV 在系統記憶體（admit 照這個比例分到兩個池）
            "kv_ram_frac": float(raw.get("kv_ram_frac") or 0) if raw.get("disk") else 0.0,
            "disk_mode": bool(raw.get("disk")),
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


# 自訂啟動引數（執行頁「高階：啟動引數」）：只收這些調校用的參數，任何會指到檔案、改監聽位址、
# 換模型或金鑰的參數一律不收（--host、--port 由主控台管；-m、--mmproj、--api-key-file 由欄位設定管）
LLAMA_ARG_OK = {
    "-ngl", "--n-gpu-layers", "--gpu-layers", "-c", "--ctx-size", "-ot", "--override-tensor", "--n-cpu-moe", "-ncmoe",
    "--cpu-moe", "-cmoe", "-ts", "--tensor-split", "-sm", "--split-mode", "-mg", "--main-gpu", "-t", "--threads",
    "-tb", "--threads-batch", "-b", "--batch-size", "-ub", "--ubatch-size", "-fa", "--flash-attn", "-ctk", "--cache-type-k",
    "-ctv", "--cache-type-v", "-np", "--parallel", "-cb", "--cont-batching", "-nocb", "--no-cont-batching", "--mlock",
    "--no-mmap", "--mmap", "--numa", "--prio", "--poll", "--temp", "--top-k", "--top-p", "--min-p", "--repeat-penalty",
    "--presence-penalty", "--frequency-penalty", "--seed", "-n", "--n-predict", "--keep", "--rope-scaling", "--rope-scale",
    "--rope-freq-base", "--rope-freq-scale", "--yarn-orig-ctx", "--yarn-ext-factor", "--yarn-attn-factor",
    "--yarn-beta-slow", "--yarn-beta-fast", "--no-kv-offload", "-nkvo", "--kv-unified", "-kvu", "--swa-full",
    "--cache-reuse", "--defrag-thold", "-dt", "--no-warmup", "--reasoning-budget", "--reasoning-format", "--chat-template",
    "--draft-max", "--draft-min", "--draft-p-min", "--spec-replace", "--override-kv", "--slot-prompt-similarity", "-sps",
    "--context-shift", "--no-context-shift", "--cache-ram", "-cram", "--threads-http", "--load-mode",
}
# 自己寫了這些，自動分層就讓位（對應的自動參數不加）
LLAMA_AUTO_LAYERS = {"-ngl", "--n-gpu-layers", "--gpu-layers", "-ot", "--override-tensor", "--n-cpu-moe", "-ncmoe",
                     "--cpu-moe", "-cmoe", "-ts", "--tensor-split"}
LLAMA_AUTO_CTX = {"-c", "--ctx-size"}
LLAMA_VAL_RX = re.compile(r"[\w.,:=+*\\\[\]()|^$?{}<> -]{1,200}")


def parse_llama_args(text):
    """網頁貼上的啟動引數 → 參數清單。回 (清單, 錯誤)。不准檔案路徑、不准 shell 字元、最多 40 個。"""
    text = str(text or "").strip()
    if not text:
        return [], None
    if len(text) > 2000:
        return None, "啟動引數太長（上限 2,000 字）"
    try:
        toks = shlex.split(text)
    except ValueError:
        return None, "啟動引數的引號沒有成對"
    if len(toks) > 40:
        return None, "啟動引數最多 40 個"
    for t in toks:
        if t.startswith("-") and not re.fullmatch(r"-?\d+(\.\d+)?", t):
            flag = t.split("=", 1)[0]
            if flag not in LLAMA_ARG_OK:
                return None, f"不接受 {flag}：這裡只收調校效能與取樣的參數（模型、監聽位址、金鑰、任何檔案路徑由主控台管）"
            if "=" in t and not LLAMA_VAL_RX.fullmatch(t.split("=", 1)[1]):
                return None, f"{flag} 的值有不允許的字元"
        elif "/" in t or (OS == "windows" and re.search(r"^[A-Za-z]:|\\\\", t)) or t.startswith("~") or not LLAMA_VAL_RX.fullmatch(t):
            return None, f"「{t[:40]}」不像參數值：不能放檔案路徑或特殊字元"
    return toks, None


def llama_custom_args(raw, reg=None):
    reg = reg or registry()
    return list((reg.get("llama_custom_args") or {}).get(os.path.abspath(raw.get("model") or ""), []))


def llama_argv(raw, reg=None):
    """設定檔 → llama-server 參數。金鑰只給檔案路徑（--api-key-file），不放進指令列（ps 看得到）。
    這顆模型有自訂啟動引數就接在最後；自己寫了分層或上下文，對應的自動參數就不加。"""
    reg = reg or registry()
    custom = llama_custom_args(raw, reg)
    flags = {t.split("=", 1)[0] for t in custom if t.startswith("-")}
    own_layers, own_ctx = bool(flags & LLAMA_AUTO_LAYERS), bool(flags & LLAMA_AUTO_CTX)
    ctx, par = int(raw.get("ctx", 4096)), int(raw.get("parallel", 1))
    # fit：多張卡又要把專家搬到系統記憶體時，交給 llama.cpp 的 --fit 依每張卡當下實際剩多少去分。
    # 自己帶 --n-cpu-moe 只會搬「前幾層」，那幾層全在第一張卡，後面那張一點都沒變輕（2× 48 GB 換 118B MoE 實測爆在卡 1）
    use_fit = bool(raw.get("fit")) and not own_layers and "--fit" in _llama_help(raw["exe"])
    argv = [raw["exe"], "-m", raw["model"], "--host", raw.get("host", "0.0.0.0"), "--port", str(raw["port"])]
    if not own_ctx:
        argv += ["-c", str(ctx * par)]
    if not flags & {"-np", "--parallel"}:
        argv += ["-np", str(par)]
    if not own_layers and not use_fit:
        argv += ["-ngl", str(raw.get("ngl", 999))]
    argv += ["--alias", raw["alias"], "--jinja"]
    if raw.get("mmproj"):
        argv += ["--mmproj", raw["mmproj"]]
    if raw.get("cpu_moe") and not own_layers and not use_fit:
        argv += ["--n-cpu-moe", str(int(raw["cpu_moe"]))]
    argv += reasoning_args(raw["exe"], raw.get("reasoning"))
    if raw.get("max_tokens"):
        argv += ["-n", str(int(raw["max_tokens"]))]
    if raw.get("api_key_file") and os.path.exists(raw["api_key_file"]):
        argv += ["--api-key-file", raw["api_key_file"]]
    if not raw.get("mmap", False) and not flags & {"--mmap", "--no-mmap", "--load-mode"}:
        # 統一記憶體機開 mmap 會多一份頁快取，系統記憶體那塊先爆
        argv += no_mmap_args(raw["exe"])
    extra = reg["machine"].get("llama_args") or []
    return argv + [str(x) for x in extra if isinstance(x, (str, int))] + custom


def llama_cmd_text(raw, reg=None):
    """畫面上顯示的完整啟動指令（金鑰本身不在指令裡，只有金鑰檔的位置）。"""
    argv = llama_argv(raw, reg)
    q = (lambda a: f'"{a}"' if re.search(r'[\s"]', a) else a) if OS == "windows" else shlex.quote
    return " ".join(q(str(a)) for a in [os.path.basename(argv[0])] + argv[1:])


@functools.lru_cache(maxsize=8)
def _llama_help(exe):
    try:
        p = subprocess.run([exe, "--help"], capture_output=True, text=True, **CMD_TEXT, timeout=20, env=llama_launch_env(exe))
        return p.stdout + p.stderr
    except Exception:  # noqa: BLE001
        return ""


def reasoning_args(exe, mode):
    """思考模式（on／off／auto）的寫法跟著 llama.cpp 版本走：新版有 --reasoning，舊版只能用 --reasoning-budget 0 關掉。"""
    if mode not in ("on", "off"):
        return []
    text = _llama_help(exe)
    if "--reasoning [" in text or re.search(r"--reasoning\s+\[", text):
        return ["--reasoning", mode]
    if mode == "off" and "--reasoning-budget" in text:
        return ["--reasoning-budget", "0"]
    return []


@functools.lru_cache(maxsize=8)
def no_mmap_args(exe):
    """關 mmap 的寫法跟著 llama.cpp 版本走：build 11626（2026-10）起拿掉 --no-mmap、改成 --load-mode none，
    舊版帶新旗標、新版帶舊旗標都會 invalid argument 直接退出。看 --help 決定。"""
    # 用跟啟動時一樣的環境去問：靠 LD_LIBRARY_PATH 或 llama_env 才載得起來的那支，裸跑 --help 會失敗，
    # 結果就退回帶舊旗標，新版照樣起不來
    try:
        p = subprocess.run([exe, "--help"], capture_output=True, text=True, **CMD_TEXT, timeout=20, env=llama_launch_env(exe))
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


def set_llama_model(slot_id, model_id, ctx=None, force=False, gpus=..., allow_disk=False, thinking=None, lan=None):
    """把欄位換成存放區裡的某顆 GGUF（llama.cpp）。欄位原本跑 Strata、另一顆 GGUF、或是空欄位都可以。
    allow_disk：一般放法放不下時，允許以檔案映射啟動（用硬碟執行）；BIOS 切兩塊的機器一律 400。
    thinking（auto／on／off）、lan（True／False）：同一次啟動就套用；沒帶就沿用欄位原本的設定。
    整段同步做完才回；進行中的階段另由 slot_progress() 回報（前端換模型時每秒問一次）。"""
    reg = registry()
    slot = next((x for x in reg["slots"] if x["id"] == slot_id), None)
    if not slot:
        return 404, {"error": f"沒有這個欄位：{slot_id}"}
    if thinking not in (None, "auto", "on", "off"):
        return 400, {"error": "思考模式只能是 auto、on、off"}
    if lan is not None and not isinstance(lan, bool):
        return 400, {"error": "區網存取要是 true 或 false"}
    if allow_disk and reg["machine"].get("memory_model", "discrete") == "unified_split":
        return 400, {"error": DISK_DENIED, "disk_denied": True}
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
    # 思考模式與區網存取：這次有帶就用帶的，沒帶就沿用欄位原本的（換引擎也一樣）；全新的欄位預設 auto、區網開
    prev_raw = (load_json(slot["config"], {}) or {}) if slot.get("config") and slot.get("kind") in ("llama", "strata") else {}
    if thinking is None:
        thinking = (prev_raw.get("reasoning") if slot.get("kind") == "llama" else None) or ("off" if DESKTOP_AGENT else "auto")
    # 桌面版：只有使用者自己打開過區網存取（lan_chosen）才沿用；舊版留下的 0.0.0.0 不算，預設只給這台自己用
    lan_chosen = lan is not None or bool(prev_raw.get("lan_chosen"))
    if lan is not None:
        host = "0.0.0.0" if lan else "127.0.0.1"
    elif DESKTOP_AGENT and not prev_raw.get("lan_chosen"):
        host = "127.0.0.1"
    else:
        host = prev_raw.get("host") or ("127.0.0.1" if DESKTOP_AGENT else "0.0.0.0")
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
    lay = llama_layout(m, reg["machine"], cards, gpu_ids, want_ctx, par, back)
    multi = len([c for c in cards if not gpu_ids or str(c["id"]) in [str(g) for g in gpu_ids]]) > 1
    base_raw = {"engine": "llama", "exe": exe, "model": m["path"], "mmproj": m.get("mmproj"),
                "host": host, "port": int(port), "ctx": want_ctx, "parallel": par,
                "alias": alias, "max_tokens": carry_max if carry_max and carry_max < want_ctx else None,
                "api_key_file": keyfile, "gpus": gpu_ids, **({"reasoning": thinking} if thinking != "auto" else {}),
                **({"lan_chosen": True} if lan_chosen else {})}

    def raw_for(ly):
        # 多卡又要搬專家：交給 llama.cpp 的 --fit（#27）
        r = {**base_raw, "ngl": ly["ngl"], "cpu_moe": ly["cpu_moe"] or None, "fit": bool(ly["cpu_moe"]) and multi}
        if ly["mode"] == "disk":
            r.update(mmap=True, disk=True, kv_ram_frac=ly["kv_ram_frac"])
        return r
    raw = raw_for(lay)
    save_json(cfg_path, raw)
    os.chmod(cfg_path, 0o600)
    unit = ensure_llama_unit(slot_id, cfg_path)
    cpu_moe = lay["cpu_moe"]
    est = {"vram": lay["est_vram"], "ram": lay["est_ram"], "kv": lay["kv"], "moved_gib": lay["moved_gib"]}
    _load_begin(slot_id, m["path"], engine_file(slot_id, "-llama.log"), unit, int(port))

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

    def undo(msg, code=500, extra=None, restart_old=True):
        """restart_old=False：還沒動到在跑的服務就失敗（例：准入擋下），只還原設定，不重啟原服務。
        以前一律重啟，關鍵服務會因為一次被擋下的換模型白白中斷好幾分鐘。"""
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
            if old_alive and restart_old:
                sysctl(old_unit, "reset-failed", timeout=20)
                sysctl(old_unit, "restart")
                back_ok, _ = wait_healthy(old, seconds=600)
        _load_end(slot_id, False, msg)
        return code, {"error": msg + ("，已換回原本的設定" if old_unit else "，欄位維持空的")
                      + ("" if back_ok else "，但原本的服務還沒起來，請查記錄"),
                      "reverted": True, "restored_ok": back_ok, **(extra or {})}

    # 同一個服務（GGUF 換 GGUF）：admit 看得到這個服務開著，會自己把它占的量算回，這裡不能再加一次。
    # 跨引擎：舊服務是另一個 unit，admit 看不到，要另外算回
    ov, orr = _usage_reclaim(old) if old_unit and old_unit != unit and old_alive else (0.0, 0.0)
    # 逐張卡試算時，舊模型釋放的量要還給它「原本」用的卡，不是新綁的卡
    old_v = _usage_reclaim(old)[0] if old_unit and old_alive else 0.0
    verdict = admit(slot_id, extra_free=(ov, orr), reclaim_cards=card_split(old_v, old.get("gpu_ids"), cards))
    if not verdict["allow"] and not force and allow_disk:
        # 一般放法放不下、管理者勾了「用硬碟執行」：改用檔案映射的放法再試算一次（底線照樣守住）
        dl = disk_layout(m, reg["machine"], cards, gpu_ids, want_ctx, par, back,
                         ram_pool()["free_gib"] + (orr if old_unit != unit else _usage_reclaim(old)[1] if old_alive else 0))
        if dl:
            lay = dl
            raw = raw_for(lay)
            save_json(cfg_path, raw)
            os.chmod(cfg_path, 0o600)
            est = {"vram": lay["est_vram"], "ram": lay["est_ram"], "kv": lay["kv"], "moved_gib": 0.0}
            cpu_moe = lay["cpu_moe"]                    # MoE 依張量分層時，專家層留在映射記憶體的層數
            put({**fields, "kind": "llama", "unit": unit, "config": cfg_path, "engines": engines,
                 "port": int(port), "est_vram_gib": est["vram"], "est_ram_gib": est["ram"],
                 "caps": sorted((set(old.get("caps") or []) - {"看圖"}) | {"文字"} | ({"看圖"} if m.get("vision") else set())),
                 "model": m["name"], "quant": m.get("quant"), "cpu_moe": cpu_moe or None})
            verdict = admit(slot_id, extra_free=(ov, orr), reclaim_cards=card_split(old_v, old.get("gpu_ids"), cards))
    auto_ctx = None
    if not verdict["allow"] and not force and ctx is None:
        # 使用者沒指定上下文：跟 LocModel 一樣先把上下文縮短再試（一般對話 8K 夠用），不要直接說放不下
        for c in (16384, 8192):
            if c >= want_ctx:
                continue
            base_raw["ctx"] = c
            if base_raw.get("max_tokens") and base_raw["max_tokens"] >= c:
                base_raw["max_tokens"] = None
            ly = llama_layout(m, reg["machine"], cards, gpu_ids, c, par, back)
            raw = raw_for(ly)
            save_json(cfg_path, raw)
            os.chmod(cfg_path, 0o600)
            put({**fields, "kind": "llama", "unit": unit, "config": cfg_path, "engines": engines,
                 "port": int(port), "est_vram_gib": ly["est_vram"], "est_ram_gib": ly["est_ram"],
                 "caps": sorted((set(old.get("caps") or []) - {"看圖"}) | {"文字"} | ({"看圖"} if m.get("vision") else set())),
                 "model": m["name"], "quant": m.get("quant"), "cpu_moe": ly["cpu_moe"] or None})
            v2 = admit(slot_id, extra_free=(ov, orr), reclaim_cards=card_split(old_v, old.get("gpu_ids"), cards))
            if v2["allow"]:
                verdict, lay, want_ctx, auto_ctx = v2, ly, c, c
                est = {"vram": ly["est_vram"], "ram": ly["est_ram"], "kv": ly["kv"], "moved_gib": ly["moved_gib"]}
                cpu_moe = ly["cpu_moe"]
                break
    if not verdict["allow"] and not force:
        return undo("記憶體不夠" + ("（用硬碟執行也放不下：KV、計算緩衝與放 GPU 的層仍要占記憶體）"
                                   if lay["mode"] == "disk" else ""), 409, {"admit": verdict}, restart_old=False)

    used_before = gpu_used_gib()
    if old_unit and old_unit != unit:
        sysctl(old_unit, "stop")
        if old_enabled:
            sysctl(old_unit, "disable", timeout=30)      # 開機自啟一起換，不然重開機兩個引擎搶同一個埠
    elif old_alive:
        sysctl(unit, "stop")
    if old_alive:
        wait_vram_released(used_before, old_v)
    sysctl(unit, "reset-failed", timeout=20)
    sysctl(unit, "enable", timeout=30)
    _load_mark(slot_id)                                  # 記錄檔從這裡之後才是這一次的載入
    sysctl(unit, "restart")
    new_slot = next(x for x in registry()["slots"] if x["id"] == slot_id)
    t0 = time.time()
    ok, why = wait_healthy(new_slot, seconds=900)        # 幾十 GB 的權重從 NVMe 讀要幾分鐘
    if ok:
        record_load(m["path"], time.time() - t0)
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
    _load_end(slot_id, True)
    return 200, {"ok": True, "engine": "llama", "model": m["name"], "quant": m.get("quant"),
                 "api_model_name": alias, "port": int(port), "ctx": want_ctx,
                 "switched_engine": old.get("kind") not in (None, "llama"),
                 "kv_gib": est["kv"], "est_vram_gib": est["vram"], "gpus": gpu_ids,
                 "cpu_moe": cpu_moe, "moved_gib": est["moved_gib"],
                 "disk_mode": lay["mode"] == "disk", "ngl": lay["ngl"], "split": lay["split"],
                 "thinking": thinking, "lan": host != "127.0.0.1", "ctx_auto": auto_ctx,
                 "warnings": verdict.get("warnings") or []}


def gpu_used_gib():
    """各卡顯存用量加總（GiB），直接問驅動不走快取；讀不到回 None。"""
    gpus = probe_gpus()
    if not gpus or any(g.get("vram_used") is None for g in gpus):
        return None
    return sum(g["vram_used"] for g in gpus) / GIB


def wait_vram_released(before_gib, expect_gib, seconds=60):
    """停掉舊模型後，等驅動真的把顯存收回再啟動新的。llama.cpp 照「啟動當下每張卡剩多少」分層，
    還沒收回的那張分得少、另一張被塞爆（實測：48 GB 的卡被分到 58 GB 而 OOM，平台自動換回原模型）。
    讀不到用量、或預期釋出不到 1 GiB 就不等；等不到也不擋，交給後面的健康檢查與還原。"""
    if before_gib is None or not expect_gib or expect_gib < 1:
        return True
    t0 = time.time()
    while time.time() - t0 < seconds:
        now = gpu_used_gib()
        if now is None or now <= before_gib - expect_gib * 0.9:
            return True
        time.sleep(1)
    return False


def _usage_reclaim(old_slot):
    """切換引擎時，舊引擎占的量要算成「會被釋放」。用舊欄位的原樣去量。"""
    reg = registry()
    hw = hardware()
    # 量「換之前」的樣子：換模型時登錄檔裡這個欄位已經先寫成新模型的預估，沒綁卡時顯存是照各欄位的預估比例分攤，
    # 分母若用新的、分子用舊的，舊模型會釋出的量就被少算（128B 換 118B 時 93.1 GiB 只算成 80.6，誤擋下來）
    probe = dict(old_slot)
    reg["slots"] = [probe if sl.get("id") == probe.get("id") else sl for sl in reg["slots"]]
    for sl in reg["slots"]:
        sl["_busy_cache"] = _alive(sl)
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
    loads = reg.get("load_sec") or {}
    speeds = reg.get("speed") or {}
    for m in ggufs:
        m["in_use"] = in_use.get(os.path.abspath(m["path"]))
        m["load_sec"] = loads.get(os.path.abspath(m["path"]))
        sp = speeds.get(os.path.abspath(m["path"])) or {}
        m["measured_tok_s"] = sp.get("tok_s")
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
    return {"root": root, "free_gib": free_gib, "hdd": store_hdd(root),
            "total_gib": total_gib,
            "gguf": ggufs, "strata": strata, "problems": problems,
            "model_dirs": [{"path": d, "exists": os.path.isdir(d),
                            "count": sum(1 for m in ggufs if m.get("external") == d)} for d in model_dirs(reg)],
            "dir_suggestions": model_dir_suggestions(reg),
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
    return f"{hf_base()}/{q(repo, safe='/')}/resolve/main/{q(path, safe='/')}"


class _HFRedirect(urllib.request.HTTPRedirectHandler):
    """Hugging Face 下載會轉到它自己的 CDN；轉到 Hugging Face 以外的網域時，權杖不跟著送過去。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        host = (urllib.parse.urlparse(newurl).hostname or "").lower()
        if new is not None and not (host == "huggingface.co" or host.endswith((".huggingface.co", ".hf.co")) or host == "hf.co"):
            new.remove_header("Authorization")
        return new


_HF_OPENER = urllib.request.build_opener(_HFRedirect)


HF_HOSTS = ("huggingface.co", "hf.co")


def _is_hf_host(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in HF_HOSTS or host.endswith((".huggingface.co", ".hf.co"))


def hf_base():
    """Hugging Face 的位址：這台的設定填了鏡像就走鏡像，沒填走官方（測試時 XCONSOLE_HF_BASE 指到本機假站）。"""
    try:
        mirror = registry()["machine"].get("hf_mirror")
    except Exception:  # noqa: BLE001
        mirror = None
    return mirror or HF_BASE


def _hf_get(url, token="", rng=None, timeout=30):
    h = {"User-Agent": "xconsole-agent"}
    # 令牌只送給 Hugging Face 自己（與測試用的假站）：走鏡像時不送，鏡像是第三方，送過去等於交出去
    if token and (_is_hf_host(url) or url.startswith(HF_BASE + "/")):
        h["Authorization"] = f"Bearer {token}"
    if rng:
        h["Range"] = f"bytes={rng}-"
    return _HF_OPENER.open(urllib.request.Request(url, headers=h), timeout=timeout)


def hf_files(repo, token=""):
    """列出 Hugging Face 上這個 repo 的 GGUF，依量化分組，並估每組放不放得下。"""
    if not HF_REPO_RX.fullmatch(repo or ""):
        return 400, {"error": "名稱格式是「作者/模型」，例：unsloth/gemma-4-E4B-it-GGUF"}
    try:
        with _hf_get(f"{hf_base()}/api/models/{urllib.parse.quote(repo, safe='/')}/tree/main?recursive=true", token) as resp:
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
        # 另外兩種也不是主模型：imatrix_unsloth.gguf（量化用的校正資料）、dflash-*.gguf（推測解碼的草稿頭）
        aux = bool(AUX_GGUF_RX.search(g["stem"])) or (bool(sub) and not SHARD_RX.search(os.path.basename(g["files"][0])[:-5]))
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
        with _hf_get(f"{hf_base()}/api/models/{urllib.parse.quote(repo, safe='/')}", token, timeout=20) as resp:
            mi = json.load(resp)
        lic = (mi.get("cardData") or {}).get("license") or next(
            (t.split(":", 1)[1] for t in mi.get("tags") or [] if t.startswith("license:")), None)
        card = {"license": lic if isinstance(lic, str) else None, "released": (mi.get("createdAt") or "")[:7] or None}
    except Exception:  # noqa: BLE001
        pass                                    # 授權與日期只是顯示用，拿不到不影響推薦
    return 200, {**info, "profile": prof, "profiled": pick["stem"], "card": card}


HF_SEARCH_RX = re.compile(r"[\w .+/-]{1,80}")


def hf_search(q, token=""):
    """用關鍵字找 Hugging Face 上的 GGUF（照下載數排）。中國來源標出來，選不選由人決定（內部測試可以用）。"""
    q = (q or "").strip()
    if not HF_SEARCH_RX.fullmatch(q):
        return 400, {"error": "關鍵字 1～80 字，只能有英數、空白與 . _ + - /"}
    url = (f"{hf_base()}/api/models?search={urllib.parse.quote(q)}&filter=gguf&sort=downloads&direction=-1&limit=24")
    try:
        with _hf_get(url, token, timeout=20) as resp:
            rows = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"連不到 Hugging Face：{str(exc)[:120]}"}
    out = [{"repo": r.get("id") or r.get("modelId"), "downloads": r.get("downloads"), "likes": r.get("likes"),
            "updated": (r.get("lastModified") or r.get("createdAt") or "")[:10],
            "cn_origin": any(c in str(r.get("id") or "").lower() for c in CN_ORIGIN)}
           for r in rows if isinstance(r, dict) and HF_REPO_RX.fullmatch(str(r.get("id") or r.get("modelId") or ""))]
    return 200, {"q": q, "results": out}


# ---- 從瀏覽器上傳 GGUF：分塊送、可以續傳；最後一塊到齊才核對檔頭、改成正式檔名
UP_FOLDER_RX = re.compile(r"[A-Za-z0-9][\w.-]{0,79}")
UP_NAME_RX = re.compile(r"[A-Za-z0-9][\w.+-]{0,150}\.gguf")
UP_CHUNK_MAX = 64 << 20
_UP_LOCK = threading.Lock()


def _up_paths(folder, name):
    if not UP_FOLDER_RX.fullmatch(folder or "") or not UP_NAME_RX.fullmatch(name or ""):
        raise ValueError("資料夾只能用英數、底線、點、連字號；檔名要以 .gguf 結尾")
    d = inside(gguf_root(), folder)
    return d, inside(d, name), inside(d, name + ".part")


UP_EDGE = 4 << 20


def edge_sha256(path):
    """檔案頭尾各 4 MB 的 sha256：同名檔已經在時，拿來比對是不是同一個檔（不必把幾十 GB 整個讀一遍）。"""
    import hashlib
    h = hashlib.sha256()
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        h.update(fh.read(UP_EDGE))
        if size > UP_EDGE:
            fh.seek(max(UP_EDGE, size - UP_EDGE))
            h.update(fh.read(UP_EDGE))
    return h.hexdigest()


def upload_status(folder, name):
    try:
        _, final, part = _up_paths(folder, name)
    except ValueError as ex:
        return 400, {"error": str(ex)}
    out = {"exists": os.path.exists(final), "received": os.path.getsize(part) if os.path.exists(part) else 0}
    if out["exists"]:
        out.update(size=os.path.getsize(final), edge_sha256=edge_sha256(final))
    return 200, out


def upload_chunk(folder, name, offset, total, data):
    try:
        d, final, part = _up_paths(folder, name)
    except ValueError as ex:
        return 400, {"error": str(ex)}
    if os.path.exists(final):
        return 409, {"error": f"{folder}/{name} 已經在了，要換掉請先在已安裝的清單刪除"}
    if not (0 <= offset < total) or offset + len(data) > total or not data:
        return 400, {"error": "分塊的位置或大小不對"}
    with _UP_LOCK:
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if offset != have:
            return 409, {"error": "要從已收到的位置接著送", "received": have}
        if offset == 0:
            os.makedirs(d, exist_ok=True)
            free = disk_free(d)[0]
            if total / GIB + 10 >= free:
                return 400, {"error": f"磁碟不夠：檔案 {total / GIB:.1f} GiB，存放區只剩 {free} GiB（另需保留 10 GiB）"}
        with open(part, "ab") as fh:
            fh.write(data)
        have += len(data)
        if have < total:
            return 200, {"received": have}
        with open(part, "rb") as fh:
            ok = fh.read(4) == b"GGUF"
        if not ok:
            os.remove(part)
            return 400, {"error": "上傳完了，但這不是 GGUF 檔（檔頭不對），已刪除"}
        os.replace(part, final)
    return 200, {"received": have, "done": True, "id": f"{folder}/{name[:-5]}"}


def upload_cancel(folder, name):
    try:
        _, _, part = _up_paths(folder, name)
    except ValueError as ex:
        return 400, {"error": str(ex)}
    if os.path.exists(part):
        os.remove(part)
    return 200, {"ok": True}


# ---- 網頁對話：主控台經代理轉給這台的模型服務（金鑰留在代理這邊，不給瀏覽器）
CHAT_ROLES = ("system", "user", "assistant", "tool")


CHAT_SAMPLING = {
    "temperature": (float, 0, 2), "top_p": (float, 0, 1), "top_k": (int, 0, 1000), "min_p": (float, 0, 1),
    "repeat_penalty": (float, 0.5, 2), "presence_penalty": (float, -2, 2), "max_tokens": (int, 1, 131072),
    "seed": (int, -1, 2 ** 31 - 1),
}


def chat_request(slot_id, body):
    """檢查並組好要送給模型的請求。回 (錯誤碼, 錯誤訊息) 或 (埠, 金鑰, 請求本文)。"""
    slot = _slot(slot_id)
    if not slot or not slot.get("unit"):
        return 404, "沒有這個欄位"
    raw, port, key = _api_parts(slot)
    if not port:
        return 400, "這個欄位沒有 API 埠"
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not 1 <= len(msgs) <= 200:
        return 400, "對話內容不對"
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in CHAT_ROLES:
            return 400, "對話內容不對"
    req = {"messages": msgs, "stream": bool(body.get("stream", True))}
    if body.get("stream", True):
        req["stream_options"] = {"include_usage": True}
    # 取樣參數：llama.cpp 的 OpenAI 相容端點都認得（Top K、Min P、重複懲罰是它的擴充）；種子 -1＝隨機，不送
    for k, (typ, lo, hi) in CHAT_SAMPLING.items():
        if body.get(k) is None or body.get(k) == "":
            continue
        try:
            if isinstance(body[k], bool):
                raise ValueError
            v = float(body[k])
            if typ is int and v != int(v):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return 400, f"{k} 要是{'整數' if typ is int else '數字'}"
        if not lo <= v <= hi:
            return 400, f"{k} 要在 {lo}～{hi} 之間"
        if k == "seed" and v == -1:
            continue
        req[k] = int(v) if typ is int else v
    if isinstance(body.get("tools"), list) and len(body["tools"]) <= 16:
        req["tools"] = body["tools"]
    name = (raw or {}).get("model_name")
    if not name:
        # 登記進來的既有服務沒有 xConsole 管的設定檔：跟 API 實測一樣，問 /v1/models 取第一個（有些伺服器沒帶 model 會拒絕）
        st, models, _ = _http_json(f"http://127.0.0.1:{int(port)}/v1/models", key=key, timeout=8)
        ids = [m.get("id") for m in (models or {}).get("data") or [] if m.get("id")]
        name = ids[0] if ids else None
    if name:
        req["model"] = name
    return int(port), key, req


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
    if (m or {}).get("external") or model_id.startswith("@"):
        return 400, {"error": "這顆在其他模型資料夾裡，xConsole 不刪那裡的檔案（可能是別的程式在用）。"
                              "不想在清單上看到它，到「存放區」把那個資料夾移出清單；檔案本身不會動"}
    lib = library()
    if m and next((x for x in lib["gguf"] if x["id"] == model_id), {}).get("in_use"):
        return 409, {"error": "有欄位正在使用這顆，先換成別的模型再刪"}
    folder, stem = model_id.rsplit("/", 1)
    root = gguf_root(reg)
    if folder == ".":
        full = os.path.realpath(root)                    # 直接放在存放區根目錄的檔
    else:
        try:
            full = inside(root, folder)
        except ValueError:
            return 400, {"error": "路徑不在存放區內"}
        if os.path.dirname(full) != os.path.realpath(root):
            return 400, {"error": "只能刪存放區第一層的模型資料夾"}
    removed, freed = [], 0
    for f in os.listdir(full):
        low = f.lower()                                   # 跟掃描一樣不分大小寫（MODEL.GGUF 也列得出來，就要刪得掉）
        base = f[:-5] if low.endswith(".gguf") else f[:-10] if low.endswith(".gguf.part") else None
        if base is not None and SHARD_RX.sub("", base) == stem:
            p = inside(full, f)
            freed += os.path.getsize(p)
            os.remove(p)
            removed.append(f)
    rest = [f for f in os.listdir(full) if f.lower().endswith(".gguf") and not is_mmproj(f)]
    if not rest:
        for f in os.listdir(full):
            if is_mmproj(f) and f.lower().endswith(".gguf"):      # 只刪 mmproj 的 GGUF，同名的其他檔不動
                p = inside(full, f)
                freed += os.path.getsize(p)
                os.remove(p)
                removed.append(f)
        if folder != "." and not os.listdir(full):
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
        # Linux 的 Intel（Vulkan）同樣沒有對應，不綁
        "bindable": has_nvidia() or (OS == "linux" and not any(g.get("vendor") == "intel" for g in gpu.get("gpus") or [])),
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
        elif OS == "windows" and vram_total and not has_nvidia():
            # Windows 的內顯：專用記憶體（BIOS 切的那塊）＋共用 GPU 記憶體（Windows 固定讓 GPU 借最多一半的系統記憶體）。
            # 有的驅動在登錄檔回報的「顯存」已經含共用的部分（HP 的 890M 回 32 GB，跟整台記憶體一樣大）：
            # 大到超過系統記憶體四分之三就當作不可信，只算共用的那一半
            dedicated = vram_total if vram_total < ram["total_gib"] * 0.75 else 0.0
            out["unified"]["gpu_limit_gib"] = round(min(ram["total_gib"], dedicated + ram["total_gib"] / 2), 1)
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


# Windows／macOS：記憶體不夠時作業系統會改用硬碟分頁（變慢），不會像 Linux 統一記憶體機（DGX、Jetson、AI 395）那樣整台卡死。
# 所以底線在這兩個系統是「建議留」，低於它只提醒；真的剩不到 HARD_MIN_GIB 才擋（一般人的電腦也要跑得起來，慢沒關係）。
SOFT_FLOOR = OS in ("windows", "darwin")
HARD_MIN_GIB = 1.5
def soft_note(worst, floor):
    return (f"載完只剩約 {worst} GiB（建議留 {floor} GiB）。這台是 Windows／macOS，記憶體不夠時會改用硬碟分頁，"
            f"會變慢但不會當機；要快一點可以先關掉其他程式")


def admit(slot_id, reg=None, hw=None, extra_free=None, reclaim_cards=None, cfg_override=None):
    """這台還塞不塞得下這個欄位的模型。用「載下去之後最壞剩多少」判斷，不是「現在剩多少」。"""
    reg = reg or registry()
    hw = hw if hw is not None else hardware()
    slot = next((s for s in reg["slots"] if s["id"] == slot_id), None)
    if not slot:
        return {"allow": False, "reasons": [f"沒有這個欄位：{slot_id}"]}

    p = pools(reg, hw)
    mm = p["memory_model"]
    fl = p["floors"]
    cfg = cfg_override or slot_config(slot)          # cfg_override：試算「改成這樣放不放得下」，不動真的設定
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
            frac = min(1.0, max(0.0, float(cfg.get("kv_ram_frac") or 0)))
            need_vram += kv * (1 - frac)
            need_ram += kv * frac
            kv_note = (f"上下文 {cfg['max_context']:,}×{cfg['parallel']}、KV {cfg['kv']} "
                       f"要先預留 {kv} GiB（沒有 --kv-grow）")
            if cfg.get("disk_mode"):
                kv_note += "；用硬碟執行：權重以檔案映射載入，不算進准入，只算 KV、計算緩衝與放進 GPU 的層"

    if mm == "discrete" and not any(g["total_gib"] > 0 for g in p["gpus"]):
        # 沒有 GPU 的機器（例：只用 CPU 的 Windows），或只有讀不到顯存的內建顯示晶片：
        # 權重與 KV 全在系統記憶體，照顯存算的話永遠載不進去
        need_ram += need_vram
        need_vram = 0.0

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
        if need and worst < fl["ram"] and SOFT_FLOOR and worst >= HARD_MIN_GIB:
            warnings.append(soft_note(worst, fl["ram"]))
        elif need and worst < fl["ram"]:
            reasons.append(
                f"記憶體不夠：需要約 {need:.0f} GiB，現在只剩 {free:.0f} GiB，"
                f"載下去會剩 {worst} GiB（低於 {fl['ram'] if not SOFT_FLOOR else HARD_MIN_GIB} GiB）。"
                + ("先關掉一些程式，或換小一點的模型" if SOFT_FLOOR else
                   "這台是統一記憶體，吃爆會把整台拖死，不是只有服務掛掉"))
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
        if need_ram and wr < fl["ram"] and SOFT_FLOOR and mm != "unified_split" and wr >= HARD_MIN_GIB:
            warnings.append(soft_note(wr, fl["ram"]))
        elif need_ram and wr < fl["ram"]:
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
              "cuda": "-DGGML_CUDA=ON", "hip": "-DGGML_HIP=ON", "hip_targets": "AMDGPU_TARGETS", "vulkan": "-DGGML_VULKAN=ON",
              "gh": "/repos/ggml-org/llama.cpp/releases?per_page=10"},
    # stable-diffusion.cpp 的正式版標籤是 master-<編號>-<commit 前七碼>；ggml 等是 git 子模組，要一起抓
    "sd": {"name": "stable-diffusion.cpp", "repo": SD_REPO, "file": os.path.join(STATE_DIR, "sd-build.json"),
           "tag_rx": r"master-(\d+)-[0-9a-f]+", "glob": "refs/tags/master-*", "dir": "stable-diffusion.cpp-{}",
           "target": "sd-cli", "submodules": True, "extra": [],
           "cuda": "-DSD_CUDA=ON", "hip": "-DSD_HIPBLAS=ON", "hip_targets": "GPU_TARGETS", "vulkan": "-DSD_VULKAN=ON",
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


def _vulkan_ready():
    """編 Vulkan 版要的東西：著色器編譯器 glslc 與 Vulkan 標頭（libvulkan-dev）。"""
    return bool(shutil.which("glslc")) and any(os.path.exists(p) for p in ("/usr/include/vulkan/vulkan.h", "/usr/local/include/vulkan/vulkan.h"))


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
        elif _vulkan_ready():
            backend, flags = "vulkan", [eng["vulkan"]]
            notes.append("有 AMD 顯示卡但沒有 ROCm（hipcc），改用 Vulkan 編（比 ROCm 慢一些）。要更快請 IT 安裝 ROCm")
        else:
            notes.append("有 AMD 顯示卡但沒有 ROCm（hipcc），會編成只用 CPU。要用 GPU 請 IT 安裝 ROCm，"
                         "或安裝 Vulkan 開發套件改用 Vulkan：sudo apt install libvulkan-dev glslc")
    elif probe_intel_sysfs():
        if _vulkan_ready():
            backend, flags = "vulkan", [eng["vulkan"]]
            notes.append("Intel 顯示卡，用 Vulkan 編")
        else:
            notes.append("有 Intel 顯示卡但沒有 Vulkan 開發套件，會編成只用 CPU。要用 GPU 請 IT 執行："
                         "sudo apt install libvulkan-dev glslc mesa-vulkan-drivers")
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
            proc = subprocess.Popen(own_scope([str(a) for a in argv]), cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
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
            # 有 GitHub 加速字首就經它下載；commit 編號是直接問官方 repo 拿的，下載完照樣核對，對不上就不編譯
            src = (registry()["machine"].get("github_proxy") or "") + eng["repo"]
            step("下載原始碼", ["git", "clone", "--depth", "1", *sub, "--branch", st["tag"], src, dest])
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
        ov = gpu_override()
        if ov == "cpu":
            return "win-cpu-x64", None, "這台的設定指定不用 GPU，只用 CPU"
        if (isinstance(ov, dict) and ov.get("vendor") == "nvidia") or (not isinstance(ov, dict) and has_nvidia()):
            return "win-cuda12-x64", "cudart-sd-bin-win-cu12-x64.zip", "NVIDIA 顯示卡，用 CUDA 12"
        if probe_win_adapters():
            return "win-vulkan-x64", None, "AMD／Intel 顯示卡，用 Vulkan 加速"
        return "win-cpu-x64", None, "沒有偵測到 GPU，只用 CPU"
    if OS == "darwin":
        return ("macos-arm64", None, "Apple Silicon，用 Metal 加速") if arm else ("macos-x64", None, "Intel Mac，只用 CPU")
    if OS != "windows":
        return None
    a = "arm64" if arm else "x64"
    ov = gpu_override()
    if ov == "cpu":
        return f"win-cpu-{a}", None, "這台的設定指定不用 GPU，只用 CPU"
    if isinstance(ov, dict) and ov.get("vendor") in ("amd", "intel"):
        return f"win-vulkan-{a}", None, f"這台的設定指定 {ov['name']}，用 Vulkan 加速"
    if (isinstance(ov, dict) and ov.get("vendor") == "nvidia") or has_nvidia():   # 偵測失敗時，手動指定 NVIDIA 也要裝 CUDA 版
        _, out, _ = sh(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], timeout=15)
        m = re.match(r"(\d+)", out.strip())
        drv = int(m.group(1)) if m else 0
        cu = "13.4" if drv >= 580 or arm else "12.4"      # CUDA 13 要 580 版以上的驅動
        return f"win-cuda-{cu}-{a}", f"cudart-llama-bin-win-cuda-{cu}-{a}.zip", f"NVIDIA 顯示卡，用 CUDA {cu}（驅動 {drv or '未知'}）"
    if probe_win_adapters():
        return f"win-vulkan-{a}", None, "AMD／Intel 顯示卡，用 Vulkan 加速"
    return f"win-cpu-{a}", None, "沒有偵測到 GPU，只用 CPU"


def move_dir(src, dest, tries=15):
    """把解好的資料夾搬到最終位置。Windows 上防毒軟體會在剛解壓的執行檔上掃描幾秒、暫時鎖住資料夾，
    直接改名會 WinError 5（存取被拒）：等一下重試，還是不行就改用複製（HP 實機遇到）。"""
    for i in range(tries):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if OS != "windows":
                raise
            time.sleep(1)
    shutil.copytree(src, dest)
    shutil.rmtree(src, ignore_errors=True)


def _gh_json(path):
    """問 GitHub API。不帶權杖一小時只能問 60 次（以對外 IP 計，公司共用一個出口時很快用完）；
    放了 ~/.xconsole/github-token（權限 600，只放唯讀的公開權杖）就帶上，一小時 5,000 次。權杖只送給 GH_API 這一台。"""
    h = {"User-Agent": "xconsole-agent", "Accept": "application/vnd.github+json"}
    tok = (load_text(os.path.join(STATE_DIR, "github-token")) or "").strip()
    if re.fullmatch(r"[\x21-\x7e]{20,4096}", tok):   # 可見字元、不含空白與換行（不能被拿來多塞一個標頭）；GitHub 權杖格式會變，不鎖死字元集
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(GH_API + path, headers=h)
    for i in range(3):                    # GitHub 偶爾回 502／503／504（版本清單很大時常見）：等一下再問，最多三次
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as ex:
            if ex.code in (502, 503, 504) and i < 2:
                time.sleep(3 * (i + 1))
                continue
            _gh_limit_error(ex, h)
            raise


def _gh_limit_error(ex, h):
    """次數用完就換成講人話的錯誤；其他錯誤照原樣往上丟。"""
    lim, reset = ex.headers.get("X-RateLimit-Limit"), ex.headers.get("X-RateLimit-Reset")
    if ex.code in (403, 429) and ex.headers.get("X-RateLimit-Remaining") == "0":
        when = time.strftime("%H:%M", time.localtime(int(reset))) if (reset or "").isdigit() else "一小時內"
        tk = "有帶權杖" if "Authorization" in h else "沒有帶權杖"
        raise OSError(f"GitHub 一小時的查詢次數用完了（上限 {lim} 次、{tk}，{when} 後恢復）") from ex


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
        """下載到 path（先寫 path.part）。part 還在就用 Range 從斷掉的位置接著下載；整個檔核對官方 sha256 才改名。"""
        import hashlib
        h = hashlib.sha256()
        total = int(asset.get("size") or 0)
        part = path + ".part"
        if os.path.exists(path) and not os.path.exists(part):
            os.replace(path, part)             # 上次下載完、但後面的步驟失敗：重新核對一次就好，不必重下
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if total and have > total:
            os.remove(part)
            have = 0
        if have:
            with open(part, "rb") as fh:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    h.update(b)
        done, last = have, -1
        if not (total and have == total):
            hdr = {"User-Agent": "xconsole-agent", **({"Range": f"bytes={have}-"} if have else {})}
            # GitHub 加速字首（例 https://ghfast.top/）只用在下載安裝包；版本資訊與 sha256 仍直接問 GitHub API，
            # 所以就算加速站被動手腳，下載下來的檔對不上官方 sha256 就不會裝
            req = urllib.request.Request((registry()["machine"].get("github_proxy") or "") + asset["browser_download_url"], headers=hdr)
            with urllib.request.urlopen(req, timeout=60) as resp:
                if have and resp.status != 206:   # 對方不支援續傳：從頭來
                    h, done = hashlib.sha256(), 0
                    note(f"下載站不支援續傳，{asset['name']} 從頭下載")
                elif have:
                    note(f"接著上次的 {have / 1048576:.0f} MB 繼續下載 {asset['name']}")
                with open(part, "ab" if done else "wb") as fh:
                    while True:
                        if _BUILD["cancel"]:
                            raise RuntimeError("已暫停")
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
            os.remove(part)
            raise RuntimeError(f"{asset['name']} 的 sha256 跟官方公布的不一樣，已刪除，不安裝")
        os.replace(part, path)

    dest = st["dest"]
    tmp = dest + ".part"
    # 下載的檔放 engines/dl/，暫停、失敗、代理重啟都不刪：下次按「繼續下載」從斷掉的位置接著下
    dl = os.path.join(ENGINE_DIR, "dl")
    wanted = {a["name"] for a in (st["asset"], st.get("extra")) if a}
    try:
        st["phase"] = "下載"
        save_json(eng["file"], st)
        os.makedirs(dl, mode=0o700, exist_ok=True)
        for f in os.listdir(dl):               # 別的版本留下的半成品：用不到了
            if f.split(".part")[0] not in wanted and f.startswith(("llama-", "sd-", "cudart-")):
                os.remove(os.path.join(dl, f))
        arc = os.path.join(dl, st["asset"]["name"])
        span = 80 if st.get("extra") else 90
        fetch(st["asset"], arc, 0, span)
        arc2 = None
        if st.get("extra"):
            arc2 = os.path.join(dl, st["extra"]["name"])
            fetch(st["extra"], arc2, span, 10)
        st["phase"] = "核對與解壓縮"
        save_json(eng["file"], st)
        shutil.rmtree(tmp, ignore_errors=True)
        out_dir = os.path.join(tmp, "out")
        os.makedirs(out_dir)
        _safe_extract(arc, out_dir)
        if arc2:
            _safe_extract(arc2, out_dir)
        # macOS 的壓縮檔多包一層 llama-bNNNN/，拿掉讓執行檔直接在資料夾裡
        inner = [x for x in os.listdir(out_dir) if not x.startswith(".")]
        src = os.path.join(out_dir, inner[0]) if len(inner) == 1 and os.path.isdir(os.path.join(out_dir, inner[0])) else out_dir
        exe = os.path.join(src, eng["target"] + EXE)
        if not os.path.isfile(exe):
            raise RuntimeError(f"壓縮檔裡找不到 {eng['target']}，官方的檔案結構可能改了")
        if OS != "windows":
            os.chmod(exe, 0o700)               # 只有代理自己（同一個使用者）要執行它
        flag = "--version" if kind == "llama" else "--help"
        try:
            vp = subprocess.run([exe, flag], capture_output=True, text=True, **CMD_TEXT, timeout=60,
                                env=llama_launch_env(exe) if kind == "llama" else None)
            rc, out, err = vp.returncode, vp.stdout.strip(), vp.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as ex:
            rc, out, err = 1, "", str(ex)
        if rc != 0:
            raise RuntimeError(f"裝好了但跑不起來（{flag} 失敗）：{(err or out)[-200:]}")
        note(f"[100%] 完成：{(out + err).splitlines()[-1] if kind == 'llama' and (out + err) else os.path.join(dest, eng['target'] + EXE)}")
        if os.path.exists(dest):
            shutil.rmtree(dest)
        move_dir(src, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        for a in (arc, arc2):
            if a and os.path.exists(a):
                os.remove(a)
        st.update(state="done", phase="完成", exe=os.path.join(dest, eng["target"] + EXE))
    except Exception as ex:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        if _BUILD["cancel"] and st.get("phase") == "下載":
            st.update(state="paused", error="已暫停；再按「繼續下載」會從斷掉的位置接著下載")
        else:
            st.update(state="cancelled" if _BUILD["cancel"] else "failed", error=str(ex)[:300],
                      resumable=st.get("phase") == "下載")
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


# ---- 手動匯入安裝包：不能連外的機器，把官方的壓縮檔（或自己編好的）從瀏覽器送上來裝
# 分塊送、可以續傳（同 GGUF 匯入）；送齊後算 sha256（有填官方的就要對得上），解壓縮時任何檔案落到資料夾外面就整個拒絕
ENG_IMPORT_RX = re.compile(r"[A-Za-z0-9][\w.+-]{0,150}\.(?:zip|tar\.gz|tgz)")
ENG_IMPORT_MAX = 4 << 30
IMPORT_MARK = ".xconsole-exe"        # 手動匯入的包：執行檔在包裡的相對位置
IMPORT_DIR = os.path.join(ENGINE_DIR, "import")


def _imp_paths(kind, name):
    if kind not in ENGINES or not ENG_IMPORT_RX.fullmatch(name or ""):
        raise ValueError("安裝包要是 .zip、.tar.gz 或 .tgz，檔名只能用英數、底線、點、加號、連字號")
    return inside(IMPORT_DIR, f"{kind}-{name}"), inside(IMPORT_DIR, f"{kind}-{name}.part")


def engine_import_status(kind, name):
    try:
        final, part = _imp_paths(kind, name)
    except ValueError as ex:
        return 400, {"error": str(ex)}
    out = {"exists": os.path.exists(final), "size": os.path.getsize(final) if os.path.exists(final) else 0,
           "received": os.path.getsize(part) if os.path.exists(part) else 0}
    if out["received"]:
        # 已收到那段開頭（最多 4 MB）的 sha256：瀏覽器比對是不是同一個檔，不是才接著送
        import hashlib
        with open(part, "rb") as fh:
            out["head_sha256"] = hashlib.sha256(fh.read(min(out["received"], UP_EDGE))).hexdigest()
    return 200, out


def engine_import_chunk(kind, name, offset, total, data):
    try:
        final, part = _imp_paths(kind, name)
    except ValueError as ex:
        return 400, {"error": str(ex)}
    if not 0 < total <= ENG_IMPORT_MAX:
        return 400, {"error": "安裝包最大 4 GiB"}
    if not (0 <= offset < total) or offset + len(data) > total or not data:
        return 400, {"error": "分塊的位置或大小不對"}
    with _UP_LOCK:
        if os.path.exists(final):
            os.remove(final)                   # 同名的舊包（上次裝失敗留下的）：以這次送的為準
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if offset != have:
            return 409, {"error": "要從已收到的位置接著送", "received": have}
        if offset == 0:
            os.makedirs(IMPORT_DIR, mode=0o700, exist_ok=True)
            free = disk_free(IMPORT_DIR)[0]
            if total * 3 / GIB + 5 >= free:    # 壓縮檔＋解開後＋餘裕
                return 400, {"error": f"磁碟不夠：安裝包 {total / GIB:.1f} GiB，解開要約三倍空間，這台只剩 {free} GiB"}
        with open(part, "ab") as fh:
            fh.write(data)
        have += len(data)
        if have < total:
            return 200, {"received": have}
        with open(part, "rb") as fh:
            head = fh.read(4)
        if not (head.startswith(b"PK\x03\x04") or head.startswith(b"\x1f\x8b")):
            os.remove(part)
            return 400, {"error": "送完了，但這不是 zip 或 tar.gz（檔頭不對），已刪除"}
        os.replace(part, final)
    return 200, {"received": have, "done": True}


def engine_import_cancel(kind, name):
    try:
        final, part = _imp_paths(kind, name)
    except ValueError as ex:
        return 400, {"error": str(ex)}
    for f in (final, part):
        if os.path.exists(f):
            os.remove(f)
    return 200, {"ok": True}


def _find_exe(root, target, depth=4):
    """解開的資料夾裡找執行檔（官方的包有的直接放、有的包一層 build/bin/）：取最淺的那支。"""
    hits = []
    base = root.rstrip(os.sep).count(os.sep)
    for d, dirs, files in os.walk(root):
        if d.count(os.sep) - base >= depth:
            dirs[:] = []
        if target + EXE in files:
            hits.append(os.path.join(d, target + EXE))
    return min(hits, key=lambda x: x.count(os.sep), default=None)


def _import_run(st, log, kind):
    import hashlib
    eng = ENGINES[kind]
    arc = st["archive"]
    tmp = os.path.join(IMPORT_DIR, f"{kind}-unpack")

    def note(text):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    try:
        st["phase"] = "核對"
        save_json(eng["file"], st)
        h = hashlib.sha256()
        with open(arc, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                if _BUILD["cancel"]:
                    raise RuntimeError("已取消")
                h.update(b)
        sha = h.hexdigest()
        st["sha256"] = sha
        note(f"sha256：{sha}")
        if st.get("expect") and st["expect"] != sha:
            raise RuntimeError(f"sha256 跟你填的不一樣（算出來是 {sha}），可能下載不完整或被改過，不安裝")
        if not st.get("expect"):
            note("沒有填官方的 sha256：請自己到官方版本頁核對上面這串")

        def stop_if_cancelled():
            if _BUILD["cancel"]:
                raise RuntimeError("已取消")

        stop_if_cancelled()
        st["phase"] = "解壓縮"
        save_json(eng["file"], st)
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, mode=0o700)
        _safe_extract(arc, tmp)
        stop_if_cancelled()
        exe = _find_exe(tmp, eng["target"])
        if not exe:
            raise RuntimeError(f"安裝包裡找不到 {eng['target']}{EXE}。請確認是 {eng['name']} 這台系統用的版本")
        if OS != "windows":
            os.chmod(exe, 0o700)
        st["phase"] = "試跑"
        save_json(eng["file"], st)
        flag = "--version" if kind == "llama" else "--help"
        try:
            vp = subprocess.run([exe, flag], capture_output=True, text=True, **CMD_TEXT, timeout=60,
                                env=llama_launch_env(exe) if kind == "llama" else None)
            rc, out, err = vp.returncode, vp.stdout.strip(), vp.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as ex:
            rc, out, err = 1, "", str(ex)
        if rc != 0:
            raise RuntimeError(f"裝好了但跑不起來（{flag} 失敗），可能不是這台系統或顯示卡用的版本：{(err or out)[-200:]}")
        note(f"試跑成功：{(out + err).splitlines()[-1] if kind == 'llama' and (out + err) else os.path.basename(exe)}")
        stop_if_cancelled()
        # 整包搬過去（不是只搬執行檔那一層）：有的包把函式庫或執行期資料放在旁邊的資料夾。
        # 執行檔在包裡的位置寫進 .xconsole-exe，llama_globs／sd_candidates 照這個找
        inner = [x for x in os.listdir(tmp) if not x.startswith(".")]
        root = os.path.join(tmp, inner[0]) if len(inner) == 1 and os.path.isdir(os.path.join(tmp, inner[0])) else tmp
        rel = os.path.relpath(exe, root)
        dest = inside(HOME, eng["dir"].format(f"import-{sha[:8]}"))
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.replace(root, dest)
        with open(os.path.join(dest, IMPORT_MARK), "w", encoding="utf-8") as fh:
            fh.write(rel.replace(os.sep, "/"))
        shutil.rmtree(tmp, ignore_errors=True)
        os.remove(arc)
        st.update(state="done", phase="完成", dest=dest, exe=os.path.join(dest, rel))
    except Exception as ex:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        st.update(state="cancelled" if _BUILD["cancel"] else "failed", error=str(ex)[:300])
    st["ended"] = time.time()
    save_json(eng["file"], st)
    with _BUILD_LOCK:
        _BUILD["thread_alive"] = False


def engine_import_install(kind, name, expect=""):
    try:
        final, _ = _imp_paths(kind, name)
    except ValueError as ex:
        return 400, {"error": str(ex)}
    expect = str(expect or "").strip().lower()
    if expect and not re.fullmatch(r"[0-9a-f]{64}", expect):
        return 400, {"error": "sha256 要是 64 個十六進位字元（官方版本頁每個檔案旁邊那串），不確定就留空"}
    if not os.path.exists(final):
        return 404, {"error": "這個安裝包還沒送齊"}
    eng = ENGINES[kind]
    with _BUILD_LOCK:
        if _BUILD.get("thread_alive"):
            return 409, {"error": "這台正在安裝，等它裝完再匯入"}
        os.makedirs(ENGINE_DIR, mode=0o700, exist_ok=True)
        log = engine_file("build", f"-{kind}-build.log")
        open(log, "w").close()
        st = {"state": "running", "phase": "準備", "tag": name, "backend": "手動匯入", "mode": "import", "archive": final,
              "expect": expect, "log": log, "started": time.time()}
        save_json(eng["file"], st)
        _BUILD.update(cancel=False, thread_alive=True, kind=kind, proc=None)
    threading.Thread(target=_import_run, args=(st, log, kind), daemon=True).start()
    return 200, {"ok": True}


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
    if gpus and all(g.get("vendor") == "intel" and not g.get("vram_total") for g in gpus):
        return "unified_single", "Intel 內建顯示晶片：跟系統共用同一塊記憶體"
    if OS == "windows" and not nvidia and gpus and (_apu_like() or all(g.get("vendor") == "intel" for g in gpus)):
        return "unified_single", ("Windows 上的內建顯示晶片（AMD APU／Intel）：除了 BIOS 切的專用記憶體，Windows 還會讓 GPU 借用"
                                  "最多一半的系統記憶體（共用 GPU 記憶體），所以當成一個池算，走 Vulkan 版引擎")
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


_WIN_MEDIA = {"at": 0, "map": {}}


def _win_media_types():
    """Windows：磁碟機代號 → 是不是機械硬碟。Get-PhysicalDisk 的 MediaType（HDD／SSD／Unspecified）。
    一次查全部，快取十分鐘（PowerShell 起一次要一兩秒）。"""
    if time.time() - _WIN_MEDIA["at"] < 600:
        return _WIN_MEDIA["map"]
    ps = ("Get-Partition | Where-Object DriveLetter | ForEach-Object { $n = $_.DiskNumber; "
          "$m = (Get-PhysicalDisk | Where-Object DeviceId -eq \"$n\").MediaType; \"$($_.DriveLetter)=$m\" }")
    rc, out, _ = sh(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30)
    got = {}
    for ln in out.splitlines() if rc == 0 else []:
        m = re.fullmatch(r"([A-Za-z])=(\w*)", ln.strip())
        if m and m.group(2) in ("HDD", "SSD"):
            got[m.group(1).upper()] = m.group(2) == "HDD"
    _WIN_MEDIA.update(at=time.time(), map=got)
    return got


def disk_is_hdd(mnt, dev):
    """存放區那顆碟是不是機械硬碟：True／False，認不出來（虛擬磁碟、USB、RAID 卡）回 None。"""
    if OS == "linux":
        return _rotational(dev)
    if OS == "darwin":
        import plistlib
        rc, out, _ = sh(["diskutil", "info", "-plist", mnt], timeout=15)
        try:
            info = plistlib.loads(out.encode()) if rc == 0 else {}
        except Exception:  # noqa: BLE001
            info = {}
        return (not info["SolidState"]) if isinstance(info.get("SolidState"), bool) else None
    return _win_media_types().get(str(mnt)[:1].upper())


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
                    "hdd": disk_is_hdd(mnt, dev), "path": path,
                    "writable": os.access(path if os.path.isdir(path) else mnt, os.W_OK)})
    return sorted(out, key=lambda x: (x["hdd"] is True, -x["free_gib"]))


_VERSION_CACHE = {}


def exe_version(path):
    """跑一次 `<執行檔> --version`，依「路徑＋修改時間＋大小」快取：同一支執行檔結果不會變，
    Windows 的 llama-server 一啟動就初始化 Vulkan，每問一次要 2 秒（頁面每次載入會問好幾次）。"""
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return 1, "", "找不到"
    if key not in _VERSION_CACHE:
        _VERSION_CACHE[key] = sh([path, "--version"], timeout=15)
    return _VERSION_CACHE[key]


def llama_candidates():
    out = []
    for cand in llama_globs():
        rc, o, e = exe_version(cand)
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
                     "model_dirs": model_dirs(reg),
                     "bandwidth_gbs": m.get("bandwidth_gbs"), "ram_bandwidth_gbs": m.get("ram_bandwidth_gbs"),
                     "sd_cli": m.get("sd_cli") or "", "agent_enabled": bool(m.get("agent_enabled")),
                     "gpu_override": m.get("gpu_override"), "hf_mirror": m.get("hf_mirror") or "",
                     "github_proxy": m.get("github_proxy") or "", "desktop_enabled": bool(m.get("desktop_enabled"))},
        # 只回「怎麼操作」與不能用的原因；桌面小幫手的埠與權杖絕不出這台
        "desktop": (lambda r: {"how": r[0], "why": r[1] if r[0] is None else None})(desktop_route()),
        "agent": {"enabled": bool(m.get("agent_enabled")), "root": AGENT_ROOT},
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
        # 系統碟也可以（C:\AI-Models 這種常見放法，LocModel 的預設就是它），但系統資料夾、整個使用者資料夾、AppData 由 check_root 擋；
        # 其他固定磁碟整顆都可以（D:\models）
        sysd = os.environ.get("SystemDrive", "C:").upper().rstrip("\\") + "\\"
        return [HOME, sysd, *(m for m, _ in _mounts() if not m.upper().startswith(sysd[:2]))]
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
        if real.startswith(rb if rb.endswith(os.sep) else rb + os.sep) and real != rb:
            try:
                return inside(rb, os.path.relpath(real, rb))
            except ValueError:
                return None
    return None


def _place_ok(raw, store, allow_hidden=()):
    """存放區（store=True）／其他模型資料夾共用的位置檢查：在允許的位置底下、不是系統目錄、不是整個使用者資料夾或 AppData、
    不是隱藏資料夾。allow_hidden：可以在隱藏資料夾裡的例外（LM Studio 的 ~/.lmstudio/models），要完全等於或在它底下。
    訊息分兩組寫死（不用「{名稱}要…」拼字），介面翻譯才對得上整句。"""
    p = expand(str(raw or "").strip())
    if not os.path.isabs(p):
        return None, ("存放區要寫完整路徑（/ 開頭、磁碟代號開頭，或 ~/ 開頭）" if store
                      else "模型資料夾要寫完整路徑（/ 開頭、磁碟代號開頭，或 ~/ 開頭）")
    bases = data_bases()
    real = contained(p, bases)
    if real is None:
        if OS == "windows":
            return None, ("存放區要放在磁碟上的一個資料夾裡，例：C:\\AI-Models、D:\\models（不能是磁碟根目錄本身）" if store
                          else "模型資料夾要是磁碟上的一個資料夾，例：C:\\models、D:\\AI-Models（不能是磁碟根目錄本身）")
        return None, ("存放區要放在家目錄、另外掛載的資料碟，或 /data、/models、/srv、/opt 底下（不能是這些位置本身）" if store
                      else "模型資料夾要在家目錄、另外掛載的資料碟，或 /data、/models、/srv、/opt 底下（不能是這些位置本身）")
    if OS == "linux" and any(real == d or real.startswith(d + "/") for d in SYSTEM_DIRS):
        return None, f"{real} 是系統目錄，不能當存放區" if store else f"{real} 是系統目錄，不能當模型資料夾"
    if OS == "windows" and not real.lower().startswith(os.path.realpath(HOME).lower() + os.sep):
        parts = [x for x in re.split(r"[\\/]+", os.path.splitdrive(real)[1]) if x]
        if not parts or parts[0].lower() in WIN_DENY:
            return None, f"{real} 是系統資料夾，不能當存放區" if store else f"{real} 是系統資料夾，不能當模型資料夾"
        if parts[0].lower() == "users" and (len(parts) < 3 or parts[2].lower() == "appdata"):
            return None, "不能直接放在使用者資料夾或 AppData 裡；請選裡面的一個資料夾，或像 C:\\AI-Models 這樣另外建一個"
    ok_hidden = any(os.path.normcase(real) == os.path.normcase(h) or os.path.normcase(real).startswith(os.path.normcase(h) + os.sep)
                    for h in (os.path.realpath(x) for x in allow_hidden))
    if not ok_hidden and any(part.startswith(".") for part in re.split(r"[\\/]", real) if part):
        return None, "不能放在隱藏資料夾（例 ~/.ssh、~/.config）裡"
    return real, None


def check_root(raw):
    """存放區：在允許的位置底下、不是隱藏資料夾、不在系統目錄、建得起來而且寫得進去。"""
    if not str(raw or "").strip():
        return "", None
    real, err = _place_ok(raw, True)
    if err:
        return None, err
    try:
        os.makedirs(real, exist_ok=True)
    except OSError as ex:
        return None, f"建不了 {real}：{ex.strerror}"
    if not os.access(real, os.W_OK):
        return None, f"代理的帳號寫不進 {real}，請先調整這個資料夾的權限"
    return real, None



def known_model_dirs():
    """別的程式常把 GGUF 放的地方：LM Studio、自己建的 models 資料夾、LocModel 的預設 C:\\AI-Models。"""
    c = [os.path.join(HOME, ".lmstudio", "models"), os.path.join(HOME, ".cache", "lm-studio", "models"),
         os.path.join(HOME, "models"), os.path.join(HOME, "AI-Models")]
    if OS == "windows":
        for drv, _ in _mounts():
            c += [os.path.join(drv, x) for x in ("models", "AI-Models", "LLM", "gguf")]
    return c


def check_model_dir(raw):
    """其他模型資料夾：位置規則跟存放區一樣（LM Studio 的隱藏資料夾例外），要已經存在；不必寫得進去，xConsole 只讀。"""
    real, err = _place_ok(raw, False, allow_hidden=[d for d in known_model_dirs() if os.sep + "." in d])
    if err:
        return None, err
    if not os.path.isdir(real):
        return None, f"{real} 不存在或不是資料夾"
    if not os.access(real, os.R_OK):
        return None, f"代理的帳號讀不到 {real}，請先調整這個資料夾的權限"
    return real, None


_SUGGEST = {"at": 0, "out": []}


def model_dir_suggestions(reg=None):
    """這台已經有 GGUF、但還沒加進來的資料夾（給模型庫頁一鍵加入）。60 秒內重複問直接回上次的結果。"""
    if time.time() - _SUGGEST["at"] < 60:
        return _SUGGEST["out"]
    reg = reg or registry()
    have = {os.path.normcase(os.path.realpath(x)) for x in [gguf_root(reg), *model_dirs(reg)]}
    out = []
    for d in known_model_dirs():
        real = os.path.realpath(d)
        if os.path.normcase(real) in have or not os.path.isdir(real) or _under(real, gguf_root(reg)) \
                or any(_under(real, x) for x in model_dirs(reg)) or check_model_dir(real)[1]:
            continue
        n, size = 0, 0
        for _, full in _walk_dirs(real, 3, [gguf_root(reg), sd_root(reg), *model_dirs(reg)]):
            try:
                for f in os.listdir(full):
                    fp = os.path.join(full, f)
                    if f.lower().endswith(".gguf") and not is_mmproj(f) and not AUX_GGUF_RX.search(f[:-5]) and os.path.isfile(fp):
                        sz = os.path.getsize(fp)
                        if sz >= EXT_MIN_BYTES:
                            n, size = n + 1, size + sz
            except OSError:
                continue
        if n:
            out.append({"path": real, "count": n, "size_gib": round(size / GIB, 1)})
    _SUGGEST.update(at=time.time(), out=out)
    return out


@_locked
def set_model_dirs(body):
    """加入或移出一個其他模型資料夾。移出只是不再列出，檔案不動。"""
    reg = registry()
    cur = model_dirs(reg)
    if body.get("add"):
        real, err = check_model_dir(body["add"])
        if err:
            return 400, {"error": err}
        if _under(real, gguf_root(reg)):
            return 400, {"error": "這是 xConsole 自己的存放區（或在它裡面），裡面的模型本來就會列出來，不用另外加"}
        if any(os.path.normcase(os.path.realpath(x)) == os.path.normcase(real) for x in cur):
            return 200, {"ok": True, "dirs": cur}
        if len(cur) >= MODEL_DIRS_MAX:
            return 400, {"error": f"其他模型資料夾最多 {MODEL_DIRS_MAX} 個"}
        cur = [*cur, real]
    elif body.get("remove"):
        want = os.path.normcase(os.path.realpath(expand(str(body["remove"]))))
        left = [x for x in cur if os.path.normcase(os.path.realpath(x)) != want]
        if len(left) == len(cur):
            return 404, {"error": "清單裡沒有這個資料夾"}
        gone = {os.path.realpath(x) for x in cur} - {os.path.realpath(x) for x in left}
        for sl in reg["slots"]:
            c = slot_config(sl) or {}
            if c.get("_native") and any(os.path.realpath(c["_native"]).startswith(g + os.sep) for g in gone):
                return 409, {"error": f"欄位 {sl['id']} 正在用這個資料夾裡的模型，先換成別的模型再移出"}
        cur = left
    else:
        return 400, {"error": "要帶 add 或 remove"}
    reg["machine"]["model_dirs"] = cur
    save_json(REGISTRY, reg)
    _SUGGEST["at"] = 0
    return 200, {"ok": True, "dirs": cur}

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
    rc, o, e = sh([p, "--version"], timeout=20)
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


def check_mirror(v, kind):
    """Hugging Face 鏡像／GitHub 加速字首：只收 https、不帶帳密與查詢字串。空白＝不用。"""
    v = str(v or "").strip()
    if not v:
        return None, None
    u = urllib.parse.urlparse(v)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or len(v) > 200 \
            or re.search(r"[\s<>\"']", v):
        return None, ("Hugging Face 鏡像" if kind == "hf_mirror" else "GitHub 加速字首") + "只收 https:// 開頭、不帶帳密與查詢字串的網址"
    return (v.rstrip("/") if kind == "hf_mirror" else (v if v.endswith("/") else v + "/")), None


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
    if "gpu_override" in body:
        ov = body.get("gpu_override")
        if ov in (None, "", "auto"):
            new["gpu_override"] = None
        elif ov == "cpu":
            new["gpu_override"] = "cpu"
        elif isinstance(ov, dict):
            name = str(ov.get("name") or "").strip()
            if not 1 <= len(name) <= 80 or re.search(r"[\x00-\x1f<>]", name):
                return 400, {"error": "顯示卡名稱要 1～80 個字"}
            if ov.get("vendor") not in GPU_VENDORS:
                return 400, {"error": "顯示卡廠牌只能是 " + "、".join(GPU_VENDORS)}
            try:
                vg = float(ov.get("vram_gib"))
            except (TypeError, ValueError):
                return 400, {"error": "顯存要填數字（GiB）"}
            if not 0.5 <= vg <= 4096:
                return 400, {"error": "顯存要在 0.5～4096 GiB 之間"}
            new["gpu_override"] = {"name": name, "vendor": ov["vendor"], "vram_gib": vg}
        else:
            return 400, {"error": "手動指定顯示卡的格式不對"}
    for k in ("hf_mirror", "github_proxy"):
        if k in body:
            v, err = check_mirror(body.get(k), k)
            if err:
                return 400, {"error": err}
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
    if "desktop_enabled" in body:
        if not isinstance(body["desktop_enabled"], bool):
            return 400, {"error": "操作電腦要是 true 或 false"}
        if body["desktop_enabled"]:
            m["desktop_enabled"] = True
        else:
            m.pop("desktop_enabled", None)
    if "agent_enabled" in body:
        # 沒帶就不動（只改其他設定的畫面不會順手把它關掉）
        if not isinstance(body["agent_enabled"], bool):
            return 400, {"error": "Agent 模式要是 true 或 false"}
        if body["agent_enabled"]:
            m["agent_enabled"] = True
        else:
            m.pop("agent_enabled", None)
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

# ---------------------------------------------------------------- Agent 模式：在工作資料夾裡讀寫檔案、執行指令
# 每台預設關閉（registry 的 machine.agent_enabled），管理者在「這台的設定」打開才能用。
# 檔案只能動 ~/xconsole-agent/<資料夾>/ 底下：路徑一律 realpath 後檢查（符號連結指出去也擋）。
# 指令本身擋不住（它可以 cd 出去）：所以每一條都要主控台那邊有人按「允許」才會送到這裡，這裡再做三件事——
# 工作目錄固定在該資料夾、有逾時、環境變數拿掉代理權杖與任何 *_KEY／*_TOKEN／*_SECRET。
AGENT_ROOT = os.path.join(HOME, "xconsole-agent")
AGENT_FOLDER_RX = re.compile(r"[^\W_][\w .-]{0,62}[\w-]|[^\W_]")
AGENT_READ_MAX = 1 << 20
AGENT_WRITE_MAX = 2 << 20
AGENT_OUT_MAX = 32 * 1024
AGENT_TIMEOUT = (120, 600)          # 預設、上限（秒）
AGENT_LIST_MAX = 2000
SECRET_ENV_RX = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)S?$|^XCONSOLE_", re.I)


def agent_enabled(reg=None):
    return bool((reg or registry())["machine"].get("agent_enabled"))


def _agent_off():
    return 403, {"error": "這台沒有開 Agent 模式。請到「設定 → 機器 → 這台的設定」打開", "disabled": True}


# 連結進來的資料夾（管理者在網頁上用資料夾瀏覽器選這台的任一資料夾）：存在 machine.agent_links {名稱: 絕對路徑}。
# 系統資料夾、磁碟根目錄、家目錄本身、家目錄底下的隱藏設定夾（.ssh、.gnupg…）、xConsole 自己的狀態目錄一律不收。
WIN_DENY = ("windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "system volume information",
            "recovery", "boot")
MAC_DENY = ("/system", "/library", "/applications", "/private", "/cores", "/opt", "/usr", "/bin", "/sbin")
ROOT_ONLY_DENY = ("/volumes", "/mnt", "/media")          # 只擋這幾個本身（裡面的外接碟可以選）


def browse_roots():
    """資料夾瀏覽器、連結資料夾、打開資料夾能碰到的範圍：家目錄、資料磁碟與外接碟（Windows 是各磁碟）。"""
    roots = [os.path.realpath(HOME), os.path.realpath(os.path.expanduser("~"))]
    if OS == "windows":
        roots += [f"{d}:\\" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{d}:\\")]
    elif OS == "darwin":
        roots += ["/Volumes"]
    else:
        roots += [d for d in ("/mnt", "/media", "/data", "/srv") if os.path.isdir(d)]
    return roots


def in_browse_roots(path, roots=None):
    """正規化之後要落在某個允許的根目錄底下，才回正規化後的路徑；否則回 None。"""
    full = os.path.realpath(os.path.expanduser(str(path or "")))
    for root in roots or browse_roots():
        r = os.path.realpath(root)
        base = r if r.endswith(os.sep) else r + os.sep
        # 分開寫：剛好是根目錄就回根目錄本身（不是使用者給的字串），其餘要 startswith 通過才回（CodeQL 認得這個檢查）
        if full == r:
            return r
        if full.startswith(base):
            return full
    return None


def agent_link_denied(path):
    """這個資料夾能不能給 Agent 用。回 None＝可以，否則回原因。路徑先 realpath（符號連結指到系統資料夾也擋）。"""
    full = in_browse_roots(path)
    if full is None:
        return "只能選家目錄、資料磁碟或外接碟裡的資料夾"
    home = os.path.realpath(HOME)
    low = full.lower().rstrip("\\/")
    if OS == "windows":
        drive, rest = os.path.splitdrive(full)
        parts = [x for x in re.split(r"[\\/]+", rest) if x]
        if not parts:
            return "不能直接用整顆磁碟"
        if parts[0].lower() in WIN_DENY:
            return f"{parts[0]} 是系統資料夾"
        if parts[0].lower() == "users" and len(parts) <= 2:
            return "不能直接用整個使用者資料夾；請選裡面的一個資料夾（例：文件、桌面底下的專案）"
        if parts[0].lower() == "users" and parts[1].lower() not in win_own_users():
            return f"{parts[1]} 是別人的使用者資料夾；只能選這台桌面使用者自己的資料夾"
        if len(parts) >= 3 and parts[0].lower() == "users" and parts[2].lower() == "appdata":
            return "AppData 放的是程式設定與登入資料，不能給 Agent"
        if len(parts) >= 3 and parts[0].lower() == "users" and parts[2].startswith("."):
            return f"{parts[2]} 是隱藏的設定資料夾（可能有金鑰或登入資料）"
    elif not full.startswith(home + os.sep):             # 家目錄底下的不套系統資料夾規則（macOS 的家目錄可能在 /private 底下）
        if full == "/" or any(full == d or full.startswith(d + "/") for d in SYSTEM_DIRS):
            return "這是系統資料夾"
        if OS == "darwin" and any(low == d or low.startswith(d + "/") for d in MAC_DENY):
            return "這是系統資料夾"
        if low in ROOT_ONLY_DENY:
            return "請選裡面的資料夾"
    parent = os.path.dirname(home)
    other = OS != "windows" and parent not in ("/", "") and full.startswith(parent + os.sep) \
        and not full.startswith(home + os.sep) and full.split(os.sep)[len(parent.split(os.sep))] != "Shared"
    if full == home or home.startswith(full + os.sep) or other:
        return "不能直接用整個家目錄、包含家目錄的上層、或別人的家目錄；請選家目錄底下的一個資料夾"
    st = os.path.realpath(STATE_DIR)
    if full == st or full.startswith(st + os.sep) or st.startswith(full + os.sep):
        return "這個資料夾含 xConsole 自己的狀態與金鑰"
    if full.startswith(home + os.sep):
        first = os.path.relpath(full, home).split(os.sep)[0]
        if first.startswith("."):
            return f"{first} 是隱藏的設定資料夾（可能有金鑰或登入資料）"
    if not os.path.isdir(full):
        return "沒有這個資料夾"
    if not os.access(full, os.R_OK | os.W_OK | os.X_OK):
        return "代理帳號對這個資料夾沒有讀寫權限"
    return None


def win_own_users():
    """Windows：代理可以碰的使用者資料夾名稱（小寫）。以 SYSTEM 執行時只有安裝時記下的桌面使用者；
    其他人的 C:\\Users\\<名字> 底下一律不給（SYSTEM 讀得到所有人的檔案）。Public 是共用資料夾，可以。"""
    own = {"public"}
    du = (load_text(os.path.join(STATE_DIR, "desktop-user.txt")) or "").strip()
    if du:
        own.add(du.lower())
    parts = [x for x in re.split(r"[\\/]+", os.path.splitdrive(os.path.realpath(HOME))[1]) if x]
    if len(parts) >= 2 and parts[0].lower() == "users":
        own.add(parts[1].lower())
    me = os.environ.get("USERNAME") or ""
    if me and not me.endswith("$") and me.upper() != "SYSTEM":
        own.add(me.lower())
    return own


def agent_links(reg=None):
    return dict((reg or registry())["machine"].get("agent_links") or {})


def agent_browse(path=""):
    """資料夾瀏覽器（Agent 工作資料夾用）。"""
    if not agent_enabled():
        return _agent_off()
    return browse_dirs(path, agent_link_denied)


def model_dir_browse(path=""):
    """資料夾瀏覽器（加入其他模型資料夾用）：能不能選照 check_model_dir 判斷；
    瀏覽範圍跟 check_model_dir 收的一樣（Linux 的 /models、/opt、另外掛的資料碟也看得到）。"""
    extra = [d for d in data_bases() if os.path.isdir(d)]
    return browse_dirs(path, lambda p: check_model_dir(p)[1], [*browse_roots(), *extra], extra)


def browse_dirs(path, denied, allowed=None, extra_roots=()):
    """資料夾瀏覽器：只列子資料夾的名稱（不列檔案），每一個用 denied(路徑) 標出能不能選。沒給路徑＝從家目錄開始。
    allowed：能瀏覽的根目錄（預設 browse_roots）；extra_roots：另外列成快捷鍵的根目錄。"""
    path = str(path or "").strip() or HOME
    if "\x00" in path or len(path) > 1000:
        return 400, {"error": "路徑不對"}
    full = in_browse_roots(path, allowed)
    if full is None:
        return 400, {"error": "只能瀏覽家目錄、資料磁碟或外接碟"}
    if not os.path.isdir(full):
        return 404, {"error": "沒有這個資料夾"}
    dirs = []
    try:
        names = sorted(os.listdir(full), key=str.lower)
    except OSError as ex:
        return 400, {"error": f"打不開這個資料夾：{ex.strerror}"}
    for n in names[:1000]:
        p = os.path.join(full, n)
        if n.startswith(".") or not os.path.isdir(p) or os.path.islink(p):
            continue
        why = denied(p)
        dirs.append({"name": n, "path": p, "ok": why is None, "why": why})
    roots = [{"name": "家目錄", "path": os.path.realpath(HOME)}]
    if OS == "windows":
        roots += [{"name": f"{d}:\\", "path": f"{d}:\\"} for d in "CDEFGHIJ" if os.path.isdir(f"{d}:\\")]
    elif OS == "darwin":
        roots.append({"name": "外接磁碟", "path": "/Volumes"})
    else:
        roots += [{"name": d, "path": d} for d in ("/mnt", "/media", "/data", "/srv") if os.path.isdir(d)]
    have = {r["path"] for r in roots}
    roots += [{"name": d, "path": d} for d in extra_roots
              if OS == "linux" and d not in have and os.path.realpath(d) != os.path.realpath(HOME)]
    parent = os.path.dirname(full)
    parent = parent if parent != full and in_browse_roots(parent, allowed) else None
    return 200, {"path": full, "parent": parent, "dirs": dirs, "roots": roots,
                 "ok": denied(full) is None, "why": denied(full)}


def agent_link(path, name=None):
    if not agent_enabled():
        return _agent_off()
    full = os.path.realpath(os.path.expanduser(str(path or "")))
    why = agent_link_denied(full)
    if why:
        return 400, {"error": why}
    with REG_LOCK:
        reg = registry()
        links = reg["machine"].setdefault("agent_links", {})
        if full in links.values():
            return 200, {"ok": True, "name": next(k for k, v in links.items() if v == full)}
        base = str(name or os.path.basename(full) or "資料夾").strip()[:60]
        base = re.sub(r"[^\w .-]", "_", base).strip(" .") or "資料夾"
        nm, i = base, 2
        taken = set(links) | (set(os.listdir(AGENT_ROOT)) if os.path.isdir(AGENT_ROOT) else set())
        while nm in taken:
            nm, i = f"{base} {i}", i + 1
        if not AGENT_FOLDER_RX.fullmatch(nm):
            return 400, {"error": "資料夾名稱只能用中英文、數字、空白、點、底線與連字號"}
        links[nm] = full
        save_json(REGISTRY, reg)
    return 200, {"ok": True, "name": nm, "path": full}


def agent_unlink(name):
    """取消連結：只是不再給 Agent 用，資料夾與裡面的檔案原封不動。"""
    if not agent_enabled():
        return _agent_off()
    with REG_LOCK:
        reg = registry()
        links = reg["machine"].get("agent_links") or {}
        if name not in links:
            return 404, {"error": "沒有這個連結的資料夾"}
        links.pop(name)
        save_json(REGISTRY, reg)
    return 200, {"ok": True}


def agent_folder(name):
    """工作資料夾的真實路徑：~/xconsole-agent/ 底下的一層，或管理者連結進來的資料夾；已經存在、不是符號連結指出去的。"""
    name = str(name or "")
    if not AGENT_FOLDER_RX.fullmatch(name) or name in (".", ".."):
        raise ValueError("資料夾名稱只能用中英文、數字、空白、點、底線與連字號，1～64 字")
    links = agent_links()
    if name in links:
        why = agent_link_denied(links[name])
        if why:
            raise ValueError(f"連結的資料夾不能用了：{why}")
        return os.path.realpath(links[name])
    os.makedirs(AGENT_ROOT, mode=0o700, exist_ok=True)
    full = inside(AGENT_ROOT, name)
    if os.path.dirname(full) != os.path.realpath(AGENT_ROOT):
        raise ValueError("資料夾只能在工作根目錄的第一層")
    if not os.path.isdir(full):
        raise FileNotFoundError(f"沒有這個工作資料夾：{name}")
    return full


def agent_path(folder, rel):
    """資料夾裡的一個路徑：不准絕對路徑、不准 ..、realpath 之後要還在資料夾裡（符號連結指出去會被擋）。"""
    base = agent_folder(folder)
    rel = str(rel or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in rel.split("/") or "\x00" in rel:
        raise ValueError("路徑要是資料夾裡的相對路徑，不能有 .. 或以 / 開頭")
    if len(rel) > 400:
        raise ValueError("路徑太長")
    return base, inside(base, rel)


def agent_folders():
    if not agent_enabled():
        return _agent_off()
    os.makedirs(AGENT_ROOT, mode=0o700, exist_ok=True)
    out = []
    for name in sorted(os.listdir(AGENT_ROOT)):
        full = os.path.join(AGENT_ROOT, name)
        if name.startswith(".") or os.path.islink(full) or not os.path.isdir(full) or not AGENT_FOLDER_RX.fullmatch(name):
            continue
        n, newest = 0, os.path.getmtime(full)
        for dp, dns, fns in os.walk(full):
            dns[:] = [d for d in dns if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
            n += len(fns)
            if n > AGENT_LIST_MAX:
                break
        out.append({"name": name, "files": n, "updated": int(newest)})
    for name, full in sorted(agent_links().items()):
        why = agent_link_denied(full)
        out.append({"name": name, "linked": True, "path": full, "files": None,
                    "updated": int(os.path.getmtime(full)) if os.path.isdir(full) else None, "broken": why})
    return 200, {"enabled": True, "root": AGENT_ROOT, "os": OS, "folders": out}


def agent_folder_create(name):
    if not agent_enabled():
        return _agent_off()
    name = str(name or "").strip()
    if not AGENT_FOLDER_RX.fullmatch(name):
        return 400, {"error": "資料夾名稱只能用中英文、數字、空白、點、底線與連字號，1～64 字，不能以點開頭"}
    os.makedirs(AGENT_ROOT, mode=0o700, exist_ok=True)
    try:
        full = inside(AGENT_ROOT, name)
    except ValueError as ex:
        return 400, {"error": str(ex)}
    if os.path.lexists(full):
        return 409, {"error": f"已經有 {name} 這個資料夾"}
    os.makedirs(full, mode=0o700)
    return 200, {"ok": True, "name": name}


def agent_list(folder, rel=""):
    if not agent_enabled():
        return _agent_off()
    try:
        base = agent_folder(folder)
        top = agent_path(folder, rel)[1] if rel else base
    except FileNotFoundError as ex:
        return 404, {"error": str(ex)}
    except ValueError as ex:
        return 400, {"error": str(ex)}
    if not os.path.isdir(top):
        return 404, {"error": "沒有這個資料夾"}
    rows, more = [], False
    for dp, dns, fns in os.walk(top):
        # 不跟著符號連結走進去；隱藏資料夾與套件目錄只列名字不展開（.git、node_modules 一展開就幾萬筆）
        keep = []
        for d in sorted(dns):
            full = os.path.join(dp, d)
            rows.append({"path": os.path.relpath(full, base).replace(os.sep, "/"), "dir": True, "size": None,
                         "mtime": int(os.lstat(full).st_mtime), "link": os.path.islink(full)})
            if not os.path.islink(full) and not d.startswith(".") and d not in ("node_modules", "__pycache__", ".venv", "venv"):
                keep.append(d)
        dns[:] = keep
        for f in sorted(fns):
            full = os.path.join(dp, f)
            st = os.lstat(full)
            rows.append({"path": os.path.relpath(full, base).replace(os.sep, "/"), "dir": False, "size": st.st_size,
                         "mtime": int(st.st_mtime), "link": os.path.islink(full)})
        if len(rows) > AGENT_LIST_MAX:
            more = True
            break
    rows.sort(key=lambda r: r["path"])
    return 200, {"folder": folder, "files": rows[:AGENT_LIST_MAX], "truncated": more}


def agent_read(folder, rel):
    if not agent_enabled():
        return _agent_off()
    try:
        _, full = agent_path(folder, rel)
    except FileNotFoundError as ex:
        return 404, {"error": str(ex)}
    except ValueError as ex:
        return 400, {"error": str(ex)}
    if not os.path.isfile(full):
        return 404, {"error": f"沒有這個檔案：{rel}"}
    size = os.path.getsize(full)
    with open(full, "rb") as fh:
        data = fh.read(AGENT_READ_MAX)
    if b"\x00" in data[:8192]:
        return 200, {"path": rel, "size": size, "binary": True, "content": None, "truncated": size > AGENT_READ_MAX}
    return 200, {"path": rel, "size": size, "binary": False, "content": data.decode("utf-8", "replace"),
                 "truncated": size > AGENT_READ_MAX}


def agent_write(folder, rel, content):
    if not agent_enabled():
        return _agent_off()
    if not isinstance(content, str):
        return 400, {"error": "內容要是文字"}
    data = content.encode("utf-8")
    if len(data) > AGENT_WRITE_MAX:
        return 400, {"error": f"一次最多寫 2 MB（這次 {len(data) / 1048576:.1f} MB）"}
    try:
        base, full = agent_path(folder, rel)
    except FileNotFoundError as ex:
        return 404, {"error": str(ex)}
    except ValueError as ex:
        return 400, {"error": str(ex)}
    if os.path.isdir(full):
        return 400, {"error": f"{rel} 是資料夾"}
    parent = os.path.dirname(full)
    os.makedirs(parent, exist_ok=True)
    # 建完上層再驗一次：上層若是剛被換成符號連結，realpath 會跑出資料夾外
    if not os.path.realpath(parent).startswith(base + os.sep) and os.path.realpath(parent) != base:
        return 400, {"error": "路徑不在工作資料夾內"}
    created = not os.path.exists(full)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(full, flags, 0o600)   # 只有代理帳號讀得到，Agent 寫的程式可能含機密
    except OSError as ex:
        return 400, {"error": f"寫不進 {rel}：{ex.strerror}"}
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return 200, {"ok": True, "path": rel, "size": len(data), "created": created}


def agent_env():
    """指令的環境變數：拿掉代理權杖與任何看起來像機密的（*_KEY、*_TOKEN、*_SECRET…），值等於代理權杖的也拿掉。"""
    return {k: v for k, v in os.environ.items() if not SECRET_ENV_RX.search(k) and v != TOKEN}


def agent_shell(cmd):
    if OS == "windows":
        return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd]
    if OS == "darwin":
        return ["/bin/zsh", "-lc", cmd]
    return ["bash", "-lc", cmd]


class _Capped:
    """只留開頭與結尾，中間丟掉：指令狂吐輸出（例 yes）也不會把代理的記憶體吃掉。"""
    def __init__(self, cap):
        self.cap, self.head, self.tail, self.total = cap, bytearray(), bytearray(), 0

    def add(self, b):
        self.total += len(b)
        room = self.cap // 4 - len(self.head)
        if room > 0:
            self.head += b[:room]
            b = b[room:]
        self.tail += b
        keep = self.cap - self.cap // 4
        if len(self.tail) > keep:
            del self.tail[:len(self.tail) - keep]

    def text(self):
        dropped = self.total - len(self.head) - len(self.tail)
        mid = f"\n…（中間省略 {dropped:,} 位元組）…\n".encode() if dropped > 0 else b""
        raw = bytes(self.head) + mid + bytes(self.tail)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("mbcs" if OS == "windows" else "utf-8", "replace")


AGENT_PROCS = set()            # 執行中的 Agent 指令；主控台按「停止」時一次砍掉
AGENT_PROCS_LOCK = threading.Lock()


def agent_exec_cancel():
    """砍掉所有執行中的 Agent 指令（同一時間只會有一個任務）。"""
    with AGENT_PROCS_LOCK:
        procs = list(AGENT_PROCS)
    for p in procs:
        kill_tree(p.pid)
    return 200, {"ok": True, "killed": len(procs)}


def agent_exec(folder, cmd, timeout=None):
    if not agent_enabled():
        return _agent_off()
    cmd = str(cmd or "")
    if not cmd.strip() or len(cmd) > 8000 or "\x00" in cmd:
        return 400, {"error": "指令是空的、太長（最多 8000 字）或含不合法的字元"}
    try:
        t = AGENT_TIMEOUT[0] if timeout in (None, "") else int(timeout)
    except (TypeError, ValueError):
        return 400, {"error": "逾時要填秒數"}
    t = max(1, min(AGENT_TIMEOUT[1], t))
    try:
        cwd = agent_folder(folder)
    except FileNotFoundError as ex:
        return 404, {"error": str(ex)}
    except ValueError as ex:
        return 400, {"error": str(ex)}
    kw = ({"creationflags": 0x08000000 | 0x00000200} if OS == "windows" else {"start_new_session": True})
    t0 = time.time()
    try:
        # 在 systemd 底下另開一個 scope：不被代理服務的 256 MB／25% CPU 上限框住（npm install、編譯會被 OOM 砍）
        p = subprocess.Popen(own_scope(agent_shell(cmd)), cwd=cwd, env=agent_env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    except OSError as ex:
        return 500, {"error": f"指令起不來：{ex}"}
    with AGENT_PROCS_LOCK:
        AGENT_PROCS.add(p)
    bufs = {"stdout": _Capped(AGENT_OUT_MAX), "stderr": _Capped(AGENT_OUT_MAX)}

    def pump(stream, buf):
        for chunk in iter(lambda: stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536), b""):
            buf.add(chunk)
    th = [threading.Thread(target=pump, args=(p.stdout, bufs["stdout"]), daemon=True),
          threading.Thread(target=pump, args=(p.stderr, bufs["stderr"]), daemon=True)]
    for x in th:
        x.start()
    timed_out = False
    try:
        p.wait(timeout=t)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(p.pid)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if OS != "windows":
                try:
                    os.killpg(p.pid, 9)
                except OSError:
                    pass
            p.kill()
            p.wait(timeout=10)
    finally:
        with AGENT_PROCS_LOCK:
            AGENT_PROCS.discard(p)
    for x in th:
        x.join(timeout=5)
    return 200, {"code": p.returncode, "timed_out": timed_out, "timeout": t,
                 "duration_ms": int((time.time() - t0) * 1000),
                 "stdout": bufs["stdout"].text(), "stderr": bufs["stderr"].text(),
                 "stdout_bytes": bufs["stdout"].total, "stderr_bytes": bufs["stderr"].total,
                 "truncated": any(b.total > AGENT_OUT_MAX for b in bufs.values())}


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
        # 不用 gethostbyname_ex 反查自己的主機名稱：沒有對應 DNS 的 Mac 會卡在這裡很久，代理一直起不來
        names.add(_local_ip())
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

    def server_bind(self):
        # 不做 HTTPServer 的 getfqdn 反解：沒有反解的機器（實際遇到：macOS 主機）會卡在這裡幾十秒才開始聽
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = socket.gethostname(), self.server_address[1]

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
            "connectors": "--embeddings-connectors",
            # Wan 2.2 A14B 是兩個 14B 專家輪流：高雜訊那個另外給；Qwen-Image-Edit 的看圖元件（mmproj）另外給
            "high_noise": "--high-noise-diffusion-model", "llm_vision": "--llm_vision"}
SD_MODEL_ARGS_RX = re.compile(r"[a-z][a-z0-9_]{0,40}=[A-Za-z0-9_.]{1,20}(?:,[a-z][a-z0-9_]{0,40}=[A-Za-z0-9_.]{1,20}){0,3}")
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
    for cand in sorted({c for pat in pats for c in glob.glob(os.path.join(HOME, pat + EXE))} | set(imported_exes("stable-diffusion.cpp"))):
        m = re.search(r"master-(\d+)", cand)
        pre = re.search(r"-(macos-arm64|win-[\w]+-x64)", cand)
        out.append({"path": cand, "build": int(m.group(1)) if m else None, "ok": os.access(cand, os.X_OK),
                    "backend": f"預編譯 {pre.group(1)}" if pre else "手動匯入" if "-import-" in cand else "在這台編譯"})
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
    return {"root": root, "free_gib": disk_free(root)[0], "models": sd_models(reg),
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
    # 各家影格規則不同：LTX 8n+1、Wan 4n+1、MiniMax-H3 引擎自己對齊（step 1＝不限）
    step = int(d.get("frame_step") or 8)
    if step not in (1, 4, 8):
        raise ValueError("影格規則只能是 1、4、8")
    if kind == "video" and not (1 <= out["frames"] <= 241 and (step == 1 or out["frames"] % step == 1)):
        raise ValueError(f"影格數要在 1～241 之間，而且是 {step} 的倍數加 1（例：{'、'.join(str(step * k + 1) for k in (2, 4, 6))}）")
    if step != 8:
        out["frame_step"] = step
    # 這顆模型固定的參數（清單上寫，使用者這次產圖不能改）
    if d.get("flow_shift") not in (None, ""):
        try:
            fs = float(d["flow_shift"])
        except (TypeError, ValueError):
            raise ValueError("flow shift 要是數字") from None
        if not 0 < fs <= 20:
            raise ValueError("flow shift 要在 0～20 之間")
        out["flow_shift"] = fs
    if d.get("model_args"):
        if not SD_MODEL_ARGS_RX.fullmatch(str(d["model_args"])):
            raise ValueError("模型參數格式是 名稱=值（最多 4 組，逗號分隔）")
        out["model_args"] = str(d["model_args"])
    img = d.get("image") or "init"
    if img not in ("init", "ref"):
        raise ValueError("參考圖的用法只能是 init（當起始畫面）或 ref（當參考圖）")
    if img != "init":
        out["image"] = img
    if d.get("needs_image"):
        out["needs_image"] = True
    if d.get("high_steps") not in (None, ""):
        try:
            hs, hc = int(d["high_steps"]), float(d.get("high_cfg") if d.get("high_cfg") is not None else out["cfg"])
        except (TypeError, ValueError):
            raise ValueError("高雜訊步數、引導強度要是數字") from None
        if not 1 <= hs <= 150 or not 0 <= hc <= 30:
            raise ValueError("高雜訊步數 1～150、引導強度 0～30")
        out["high_steps"], out["high_cfg"] = hs, hc
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
        if not 1 <= len(parts) <= 8 or len(roles & {"model", "diffusion"}) != 1:
            # -m（完整模型）與 --diffusion-model（只有擴散模型、其餘元件另給）是兩種模式，不能同時給
            raise ValueError("要有、而且只能有一個主體（完整模型或擴散模型），最多 8 個檔")
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
        with _hf_get(f"{hf_base()}/api/models/{urllib.parse.quote(repo, safe='/')}/tree/main?recursive=true", token) as resp:
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
    spec["cn_origin"] = bool(cn)                       # 照樣下載，畫面標「中國來源・政府案不可用」
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
    free = disk_free(root)[0]                  # Windows 沒有 os.statvfs
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
        lim = p["unified"].get("gpu_limit_gib")      # Apple Silicon：Metal 只准 GPU 用統一記憶體的一部分
        if lim:
            plan["max_vram"] = min(plan["max_vram"], lim)
            if need > lim:
                reasons.append(f"超過 GPU 能用的上限：產這一件要約 {need:.0f} GiB，這台 GPU 最多只能用 {lim:.0f} GiB"
                               "（macOS 預設只給 GPU 統一記憶體的 2/3～3/4；可用 sudo sysctl iogpu.wired_limit_mb 調高）")
        if worst < fl["ram"] and SOFT_FLOOR and worst >= HARD_MIN_GIB:
            warnings.append(soft_note(worst, fl["ram"]))
        elif worst < fl["ram"]:
            reasons.append(f"記憶體不夠：產這一件要約 {need + SD_RAM_SELF:.0f} GiB，現在只剩 {free:.0f} GiB，"
                           f"產的時候會剩 {worst} GiB（低於 {HARD_MIN_GIB if SOFT_FLOOR else fl['ram']} GiB）。"
                           + ("" if SOFT_FLOOR else "統一記憶體吃爆會把整台拖死；") + tip)
    else:
        rfree = p["ram"]["free_gib"]
        if mm == "discrete" and p["gpus"]:
            card = max(p["gpus"], key=lambda c: c["free_gib"])
            # Windows 的 AMD／Intel 走 Vulkan，沒辦法用環境變數綁卡（綁卡的對照只有 Linux 的 KFD）：只拿來算額度，不綁
            plan["gpu"] = str(card["id"]) if p.get("bindable", True) else None
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
        if wr < fl["ram"] and SOFT_FLOOR and mm != "unified_split" and wr >= HARD_MIN_GIB:
            warnings.append(soft_note(wr, fl["ram"]))
        elif wr < fl["ram"]:
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
    if prm.get("flow_shift"):
        a += ["--flow-shift", prm["flow_shift"]]
    if prm.get("model_args"):
        a += ["--model-args", prm["model_args"]]
    if prm.get("high_steps"):
        # Wan 2.2 A14B：高雜訊專家自己的步數與引導強度，取樣方法跟主體一樣
        a += ["--high-noise-steps", prm["high_steps"], "--high-noise-cfg-scale", prm["high_cfg"],
              "--high-noise-sampling-method", prm["sampler"]]
    if prm.get("init") and prm.get("image") == "ref":
        # 改圖（Qwen-Image-Edit、FLUX.2 klein）與多圖參考（MiniMax-H3 Ref2VA）：圖片當參考，不是起始畫面
        a += ["-r", prm["init"]]
    elif prm.get("init"):
        # 畫圖：參考圖重繪（strength 越大改越多）；影片：當第一格畫面，接著往下演（LTX、Wan、H3 的圖生影片）
        a += ["-i", prm["init"]] + (["--strength", prm["strength"]] if m["kind"] == "image" else [])
    if m["kind"] == "image" and (prm.get("count") or 1) > 1:
        a += ["-b", prm["count"]]              # 一次多張：sd-cli 存成 <檔名>_<序號>.png
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
        try:
            kw = ({"creationflags": 0x08000000 | 0x00000200} if OS == "windows"     # 不開視窗＋自己一個行程群組
                  else {"start_new_session": True})
            proc = subprocess.Popen(own_scope(argv), stdout=fh, stderr=subprocess.STDOUT, env={**os.environ, **env}, **kw)
        except OSError as ex:                  # 執行檔被刪掉、沒有執行權限：不能讓這件一直停在「產生中」擋住後面
            j.update(state="failed", error=f"sd-cli 啟動失敗：{ex}"[:240], ended=int(time.time()))
            _sd_jobs_save()
            return
        with _GEN_LOCK:
            _GEN["proc"] = proc
        last_save = 0
        while proc.poll() is None:
            time.sleep(1)
            rf, g = _gen_sample()
            peak_r = max(peak_r, r0 - rf)
            peak_g = max(peak_g, max((g.get(k, 0) - g0.get(k, 0) for k in g), default=0.0))
            # 看門狗：系統記憶體跌破底線就中止。統一記憶體機（兩種）系統這塊被吃光整台會卡死；獨立顯卡機也一樣會拖垮系統
            # Windows／macOS 不會整台卡死（改用硬碟分頁）：只在剩不到硬下限時才中止
            if rf < (HARD_MIN_GIB if SOFT_FLOOR else fl["ram"]) and not killed:
                killed = (f"產的時候系統記憶體只剩 {rf:.1f} GiB，已中止，避免整台被拖死。"
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
    if (j.get("count") or 1) > 1:
        stem = j["out"].rsplit(".", 1)[0]
        hits = sorted((f for f in os.listdir(GEN_DIR) if re.fullmatch(re.escape(stem) + r"_\d+\.png", f)),
                      key=lambda f: int(f.rsplit("_", 1)[1].split(".")[0]))
        if hits:
            j["outs"] = hits
            out = os.path.join(GEN_DIR, hits[0])
    if killed:
        j.update(state="cancelled" if killed == "已取消" else "failed", error=killed)
    elif rc != 0 or not os.path.isfile(out) or not os.path.getsize(out):
        tail = [x for x in tail_lines(log, 12) if x.strip()]
        j.update(state="failed", error=f"sd-cli 結束碼 {rc}：" + (tail[-1][-240:] if tail else "沒有輸出"))
    else:
        peak = peak_r if mm == "unified_single" else peak_g if mm == "unified_split" else max(peak_g, 0.0)
        j.update(state="done", phase="完成", peak_gib=round(peak, 1),
                 bytes=sum(os.path.getsize(os.path.join(GEN_DIR, f)) for f in sd_outs(j) if os.path.isfile(os.path.join(GEN_DIR, f))))
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


SD_INIT_MAX = 4 << 20
SD_INIT_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"RIFF", "webp"))


def sd_init_image(raw):
    """參考圖／第一格畫面：瀏覽器送 data URL 或 base64。只收 PNG、JPEG、WebP（看檔頭，不信副檔名），最大 4 MB。
    回 (位元組, 副檔名)；沒給回 (None, None)；不合法丟 ValueError。"""
    if raw in (None, ""):
        return None, None
    if not isinstance(raw, str):
        raise ValueError("參考圖格式不對")
    b64 = raw.split(",", 1)[1] if raw.startswith("data:") else raw
    if len(b64) > SD_INIT_MAX * 4 // 3 + 8:
        raise ValueError("參考圖最大 4 MB")
    try:
        data = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        raise ValueError("參考圖不是有效的 base64") from None
    ext = next((e for magic, e in SD_INIT_MAGIC if data.startswith(magic) and (e != "webp" or data[8:12] == b"WEBP")), None)
    if not ext or len(data) > SD_INIT_MAX:
        raise ValueError("參考圖只收 PNG、JPEG、WebP，最大 4 MB")
    return data, ext


def sd_outs(j):
    """這件的產出檔：一次多張時是 <id>_<序號>.png，單張是 <id>.png，影片是 <id>.webm。"""
    if j.get("outs"):
        return list(j["outs"])
    return [j["out"]] if j.get("out") else []


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
        count = int(body.get("count") or 1)
        strength = float(body.get("strength") if body.get("strength") not in (None, "") else 0.75)
        init, init_ext = sd_init_image(body.get("init_image"))
    except (TypeError, ValueError) as ex:
        return 400, {"error": str(ex) if str(ex) else "種子、張數、重繪強度要是數字"}
    if prm.get("needs_image") and init is None:
        return 400, {"error": "這顆要先上傳一張圖片（圖生影片／改圖的模型）"}
    if m["kind"] == "image":
        if not 1 <= count <= 4:
            return 400, {"error": "一次可以產 1～4 張"}
        prm["count"] = count
        if init is not None and prm.get("image") != "ref":
            if not 0.05 <= strength <= 1.0:
                return 400, {"error": "重繪強度要在 0.05～1 之間（越大改越多）"}
            prm["strength"] = round(strength, 2)
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
    if init is not None:
        prm["init"] = os.path.join(GEN_DIR, f"{jid}-init.{init_ext}")
        fd = os.open(prm["init"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(init)
    argv = sd_argv(exe, m, root, prm, os.path.join(GEN_DIR, out), verdict["plan"])
    if prm.get("init"):
        prm["init"] = os.path.basename(prm["init"])       # 工作紀錄只記檔名，不記這台的路徑
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
    for f in [os.path.join(GEN_DIR, x) for x in sd_outs(j) + ([j["init"]] if j.get("init") else [])] + [_sd_log(j["id"])]:
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


def sd_output(jid, i=0, init=False):
    """第 i 張產出（一次多張時）；init＝這件用的參考圖（「用同樣的設定」要帶回去）。"""
    j = _sd_job(jid)
    if init:
        if not j or not j.get("init"):
            return 404, {"error": "這件沒有參考圖"}
        f = os.path.join(GEN_DIR, j["init"])
        if not os.path.isfile(f):
            return 404, {"error": "參考圖不見了"}
        with open(f, "rb") as fh:
            data = base64.b64encode(fh.read()).decode()
        ext = j["init"].rsplit(".", 1)[1]
        return 200, {"mime": {"jpg": "image/jpeg"}.get(ext, f"image/{ext}"), "name": j["init"], "b64": data}
    if not j or j["state"] != "done":
        return 404, {"error": "這件沒有產出"}
    outs = sd_outs(j)
    if not 0 <= i < len(outs):
        return 404, {"error": "沒有這一張"}
    f = os.path.join(GEN_DIR, outs[i])
    if not os.path.isfile(f):
        return 404, {"error": "產出的檔案不見了"}
    if os.path.getsize(f) > 20 * 1024 * 1024:
        return 413, {"error": "檔案超過 20 MB，請到這台的 ~/.xconsole/gen/ 取"}
    with open(f, "rb") as fh:
        data = base64.b64encode(fh.read()).decode()
    return 200, {"mime": "video/webm" if f.endswith(".webm") else "image/png", "name": outs[i], "b64": data, "count": len(outs)}


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

    def _chat(self, slot_id, body):
        """轉給模型服務。串流（SSE）一行一行原樣轉回去，不整包收完：長回答要邊產邊看得到。"""
        r = chat_request(slot_id, body)
        if len(r) == 2:
            return self._send(r[0], {"error": r[1]})
        port, key, req = r
        hreq = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(req).encode(),
                                      headers={"Content-Type": "application/json",
                                               **({"Authorization": f"Bearer {key}"} if key else {})})
        try:
            resp = LOCAL.open(hreq, timeout=600)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc)
            except Exception:  # noqa: BLE001
                detail = {}
            return self._send(exc.code, {"error": (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict)
                                         else detail.get("error") or f"模型服務回 {exc.code}"})
        except Exception as exc:  # noqa: BLE001
            return self._send(502, {"error": f"連不到模型服務：{str(exc)[:120]}"})
        with resp:
            if not req["stream"]:
                out = json.load(resp)
                tm = out.get("timings") or {}
                record_speed(slot_id, tm.get("predicted_per_second"), tm.get("predicted_n"))
                return self._send(200, out)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            timing = None
            try:
                for line in resp:
                    self.wfile.write(line)
                    self.wfile.flush()
                    if b'"timings"' in line:                # llama.cpp 在最後幾行附實測速度
                        try:
                            timing = json.loads(line.decode("utf-8", "replace").split("data:", 1)[1]).get("timings") or timing
                        except Exception:  # noqa: BLE001
                            pass
            except (BrokenPipeError, ConnectionResetError):
                pass                                   # 使用者按了停止：模型那邊也跟著斷
            if timing:
                try:
                    record_speed(slot_id, timing.get("predicted_per_second"), timing.get("predicted_n"))
                except Exception:  # noqa: BLE001  # 記速度失敗不影響對話
                    pass
        return None

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
        m = re.fullmatch(r"/sd/jobs/([0-9a-f]{8})(?:/(output)(?:/([0-3]))?|/(init))?", path)
        if m:
            if m.group(2) or m.group(4):
                return self._send(*sd_output(m.group(1), int(m.group(3) or 0), init=bool(m.group(4))))
            return self._send(*sd_job(m.group(1)))
        m = re.fullmatch(r"/apiinfo/([\w.-]+)", path)
        if m:
            code, out = api_info(m.group(1))
            return self._send(code, out)
        m = re.fullmatch(r"/catalog/([\w.-]+)", path)
        if m:
            return self._send(200, catalog(m.group(1)))
        m = re.fullmatch(r"/slot/([\w.-]+)/progress", path)
        if m:
            return self._send(*slot_progress(m.group(1)))
        if path == "/agent/folders":
            return self._send(*agent_folders())
        if path == "/self":
            return self._send(200, {"sha256": agent_sha(), "os": OS, "python": sys.version.split()[0]})
        if path == "/desktop/status":
            return self._send(*desktop_status())
        if path == "/desktop/screenshot":
            if not desktop_enabled():
                return self._send(403, {"error": "這台沒有開「讓模型操作這台的桌面」，請到這台的設定打開", "disabled": True})
            return self._send(*desktop_call("GET", "/desktop/screenshot", timeout=60))
        if path == "/llama/args":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(*llama_args_get((q.get("model") or [""])[0]))
        if path == "/library/browse":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(*model_dir_browse((q.get("path") or [""])[0]))
        if path == "/agent/browse":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(*agent_browse((q.get("path") or [""])[0]))
        return self._send(404, {"error": "找不到"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if not self._auth():
            return None
        if path == "/library/upload":
            # 上傳的本文是檔案的位元組，不是 JSON：參數放在網址上（都只是名稱與位置，沒有機密）
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            g = lambda k: (qs.get(k) or [""])[0]  # noqa: E731
            try:
                n = int(self.headers.get("Content-Length") or 0)
                offset, total = int(g("offset")), int(g("total"))
            except ValueError:
                return self._send(400, {"error": "分塊的位置或大小不對"})
            if not 0 < n <= UP_CHUNK_MAX:
                return self._send(413, {"error": "一次最多送 64 MB"})
            return self._send(*upload_chunk(g("folder"), g("name"), offset, total, self.rfile.read(n)))
        m = re.fullmatch(r"/(llama|sd)/import", path)
        if m:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            g = lambda k: (qs.get(k) or [""])[0]  # noqa: E731
            try:
                n = int(self.headers.get("Content-Length") or 0)
                offset, total = int(g("offset")), int(g("total"))
            except ValueError:
                return self._send(400, {"error": "分塊的位置或大小不對"})
            if not 0 < n <= UP_CHUNK_MAX:
                return self._send(413, {"error": "一次最多送 64 MB"})
            return self._send(*engine_import_chunk(m.group(1), g("name"), offset, total, self.rfile.read(n)))
        body = self._body()
        m = re.fullmatch(r"/(llama|sd)/import/(status|cancel|install)", path)
        if m:
            kind, act, name = m.group(1), m.group(2), str(body.get("name") or "")
            if act == "install":
                return self._send(*engine_import_install(kind, name, body.get("sha256")))
            return self._send(*(engine_import_status if act == "status" else engine_import_cancel)(kind, name))
        m = re.fullmatch(r"/chat/([\w.-]+)", path)
        if m:
            return self._chat(m.group(1), body)
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
                                            allow_disk=body.get("allow_disk") is True,
                                            thinking=body.get("thinking"), lan=body.get("lan"),
                                            **({"gpus": body.get("gpus")} if "gpus" in body else {}))
            elif cur.get("kind") == "llama":
                code, out = back_to_strata(sid, body.get("pack") or "", body.get("native") or "", force=force)
            else:
                code, out = set_model(sid, body.get("pack") or "", body.get("native") or "", force=force)
            return self._send(code, out)
        if path in ("/library/hf", "/library/hf/profile"):
            repo, tok = str(body.get("repo") or "").strip(), str(body.get("token") or "")
            code, out = hf_profile(repo, tok, str(body.get("match") or "")[:60]) if path.endswith("profile") else hf_files(repo, tok)
            if code == 200:
                out["hdd"] = store_hdd()          # 主控台估第一次載入與用硬碟執行的速度要用
            return self._send(code, out)
        if path == "/library/hf/search":
            return self._send(*hf_search(str(body.get("q") or ""), str(body.get("token") or "")))
        if path == "/library/upload/status":
            return self._send(*upload_status(str(body.get("folder") or ""), str(body.get("name") or "")))
        if path == "/library/upload/cancel":
            return self._send(*upload_cancel(str(body.get("folder") or ""), str(body.get("name") or "")))
        if path == "/library/download":
            return self._send(*start_download(str(body.get("repo") or "").strip(), str(body.get("stem") or ""),
                                              body.get("mmproj") or None, str(body.get("token") or "")))
        m = re.fullmatch(r"/library/download/([0-9a-f]{8})/cancel", path)
        if m:
            return self._send(*cancel_download(m.group(1)))
        if path == "/library/dirs":
            return self._send(*set_model_dirs(body))
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
        if path == "/agent/folders/create":
            return self._send(*agent_folder_create(body.get("name")))
        if path == "/llama/args":
            return self._send(*llama_args_set(str(body.get("model") or ""), body.get("args")))
        if path == "/self/update":
            return self._send(*self_update(body))
        if path == "/desktop/act":
            if not desktop_enabled():
                return self._send(403, {"error": "這台沒有開「讓模型操作這台的桌面」，請到這台的設定打開", "disabled": True})
            return self._send(*desktop_call("POST", "/desktop/act", body, timeout=60))
        if path == "/desktop/open":
            # 只開模型存放區、畫圖模型區與 Agent 工作資料夾底下的資料夾（「開啟位置」用），不必打開操作電腦
            full = os.path.realpath(str(body.get("path") or ""))
            reg = registry()
            roots = [gguf_root(reg), sd_root(reg), AGENT_ROOT, *agent_links(reg).values(), *model_dirs(reg)]
            if not any(full == os.path.realpath(r) or full.startswith(os.path.realpath(r) + os.sep) for r in roots if r):
                return self._send(400, {"error": "只能打開模型存放區或 Agent 工作資料夾底下的資料夾"})
            return self._send(*desktop_call("POST", "/desktop/open", {"path": full}, timeout=20))
        if path == "/agent/folders/link":
            return self._send(*agent_link(body.get("path"), body.get("name")))
        if path == "/agent/folders/unlink":
            return self._send(*agent_unlink(str(body.get("name") or "")))
        if path == "/agent/fs/list":
            return self._send(*agent_list(body.get("folder"), body.get("path") or ""))
        if path == "/agent/fs/read":
            return self._send(*agent_read(body.get("folder"), body.get("path")))
        if path == "/agent/fs/write":
            return self._send(*agent_write(body.get("folder"), body.get("path"), body.get("content")))
        if path == "/agent/exec":
            return self._send(*agent_exec(body.get("folder"), body.get("cmd"), body.get("timeout")))
        if path == "/agent/exec/cancel":
            return self._send(*agent_exec_cancel())
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


# ---------------------------------------------------------------- 操作電腦（這台的桌面）
# 讓模型看這台的螢幕、控制滑鼠鍵盤。每台預設關閉（machine.desktop_enabled），要管理者在「這台的設定」打開；
# 每一步由主控台那邊有人按允許才送到這裡（或使用者在那次任務選了「這次都允許」）。
# 截圖一律縮到最寬 1280 給模型看；模型給的座標以截圖為準，這裡換回實際像素。
# Windows 以 SYSTEM 跑在看不到桌面的工作階段 0：改由「桌面小幫手」（--desktop-helper，使用者登入後以使用者身分啟動、
# 只聽 127.0.0.1、權杖放在使用者自己的 %LOCALAPPDATA%\xConsole）代為截圖與操作，代理只轉送。
DESKTOP_MAX_W = 1280
DESKTOP_TEXT_MAX = 500
DESKTOP_ACTIONS = ("click", "double_click", "right_click", "move", "drag", "type", "key", "scroll", "wait")
DESKTOP_KEY_RX = re.compile(r"(?:(?:ctrl|control|shift|alt|option|cmd|command|win|super|meta)\+){0,3}"
                            r"(?:[a-z0-9]|f[1-9]|f1[0-2]|enter|return|tab|esc|escape|backspace|delete|del|space|up|down|left|right|"
                            r"home|end|pageup|pagedown|insert|[-=\[\];',./`\\]|win|super|meta|cmd|command|ctrl|control|alt|option|shift)", re.I)


def _png(rgb, w, h):
    """RGB 位元組 → PNG（標準函式庫：zlib＋struct）。"""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib_crc(t + d))
    stride = w * 3
    raw = b"".join(b"\x00" + bytes(rgb[i * stride:(i + 1) * stride]) for i in range(h))
    import zlib
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def zlib_crc(b):
    import zlib
    return zlib.crc32(b) & 0xffffffff


def _bgra_to_rgb(buf, w, h):
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = buf[2::4], buf[1::4], buf[0::4]
    return rgb


def desktop_user_dir(user=None):
    """桌面小幫手放權杖與程式的地方（使用者自己的資料夾，SYSTEM 也讀得到）。"""
    if user:
        return os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "Users", user, "AppData", "Local", "xConsole")
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "xConsole")


def _win_session_id():
    try:
        import ctypes
        sid = ctypes.c_ulong()
        ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
        return sid.value
    except Exception:  # noqa: BLE001
        return None


def desktop_route():
    """回 ("direct", None)：代理自己就能操作；("helper", (port, token))：轉給桌面小幫手；(None, 原因)：這台不能。"""
    if OS == "windows":
        if _win_session_id() not in (0, None):
            return "direct", None
        user = (load_text(os.path.join(STATE_DIR, "desktop-user.txt")) or "").strip()
        if not user or not re.fullmatch(r"[\w .$-]{1,64}", user):
            return None, "這台的代理以系統服務身分執行，看不到桌面；請在這台重新執行一次安裝程式，裝上「桌面小幫手」"
        info = load_json(os.path.join(desktop_user_dir(user), "desktop-helper.json"), None)
        if not info or not info.get("port") or not info.get("token"):
            return None, f"桌面小幫手還沒啟動：請用 {user} 登入這台的桌面（登入後會自動啟動）"
        return "helper", (int(info["port"]), str(info["token"]))
    if OS == "darwin":
        return "direct", None
    # ponytail: 只做 X11（xdotool）；純 Wayland 截得到圖但點不了，所以照實說不能用，要支援再補 ydotool
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY") or _linux_display_env()):
        return None, "這台沒有圖形桌面（沒有 X11 或 Wayland 工作階段）"
    if not _linux_env().get("DISPLAY"):
        return None, "這台是純 Wayland 桌面，滑鼠鍵盤送不進去；請登入時改選 X11（Xorg）工作階段"
    if not shutil.which("xdotool"):
        return None, "這台沒有送滑鼠鍵盤事件的工具：sudo apt install xdotool"
    return "direct", None


def _linux_display_env():
    """systemd 使用者服務沒有 DISPLAY：找這個帳號正在用的桌面工作階段。"""
    env = {}
    uid = os.getuid() if hasattr(os, "getuid") else 0
    run = f"/run/user/{uid}"
    way = sorted(glob.glob(os.path.join(run, "wayland-[0-9]")))
    if way:
        env.update(WAYLAND_DISPLAY=os.path.basename(way[0]), XDG_RUNTIME_DIR=run)
    if os.path.exists("/tmp/.X11-unix/X0"):
        env["DISPLAY"] = ":0"
        for xa in (os.path.join(HOME, ".Xauthority"), *glob.glob(os.path.join(run, "gdm", "Xauthority")),
                   *glob.glob(os.path.join(run, ".mutter-Xwaylandauth.*"))):
            if os.path.exists(xa):
                env["XAUTHORITY"] = xa
                break
    return env


# ---- Windows：GDI 截圖、SendInput
def _win_api():
    import ctypes
    from ctypes import wintypes
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    try:
        u32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass
    for f, res, args in ((u32.GetDC, ctypes.c_void_p, [ctypes.c_void_p]), (u32.ReleaseDC, ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
                         (g32.CreateCompatibleDC, ctypes.c_void_p, [ctypes.c_void_p]),
                         (g32.CreateCompatibleBitmap, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
                         (g32.SelectObject, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
                         (g32.DeleteObject, ctypes.c_int, [ctypes.c_void_p]), (g32.DeleteDC, ctypes.c_int, [ctypes.c_void_p]),
                         (g32.SetStretchBltMode, ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
                         (g32.StretchBlt, ctypes.c_int, [ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_void_p] + [ctypes.c_int] * 4 + [wintypes.DWORD]),
                         (g32.GetDIBits, ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint])):
        f.restype, f.argtypes = res, args
    return ctypes, u32, g32


def _win_capture(max_w):
    ctypes, u32, g32 = _win_api()
    sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
    k = min(1.0, max_w / sw) if sw else 1.0
    w, h = max(1, int(sw * k)), max(1, int(sh * k))
    hdc = u32.GetDC(None)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    old = g32.SelectObject(mdc, bmp)
    try:
        g32.SetStretchBltMode(mdc, 4)                       # HALFTONE：縮小不會一格一格糊掉
        if not g32.StretchBlt(mdc, 0, 0, w, h, hdc, 0, 0, sw, sh, 0x00CC0020 | 0x40000000):
            raise RuntimeError("擷取不到螢幕（可能在登入畫面、鎖定畫面或 UAC 確認視窗）")

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]
        bi = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4 + 1024)
        g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    finally:
        g32.SelectObject(mdc, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(None, hdc)
    return _png(_bgra_to_rgb(buf.raw[:w * h * 4], w, h), w, h), w, h, sw, sh


WIN_VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
          "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21,
          "pagedown": 0x22, "insert": 0x2D, "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "option": 0x12,
          "win": 0x5B, "super": 0x5B, "meta": 0x5B, "cmd": 0x11, "command": 0x11,
          **{f"f{i}": 0x6F + i for i in range(1, 13)}, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE,
          ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0, "\\": 0xDC}


def _win_input():
    import ctypes

    class KI(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]

    class MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]

    class U(ctypes.Union):
        _fields_ = [("ki", KI), ("mi", MI), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("u", U)]

    u32 = ctypes.windll.user32
    u32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]

    def send(*items):
        arr = (INPUT * len(items))(*items)
        u32.SendInput(len(items), ctypes.byref(arr), ctypes.sizeof(INPUT))

    def key(vk=0, scan=0, flags=0):
        i = INPUT(1)
        i.u.ki = KI(vk, scan, flags, 0, 0)
        return i

    def mouse(flags, data=0):
        i = INPUT(0)
        i.u.mi = MI(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        return i
    return u32, send, key, mouse


def _win_act(a, x, y, x2, y2, text, keys, dy):
    u32, send, key, mouse = _win_input()
    if a in ("click", "double_click", "right_click", "move", "drag"):
        u32.SetCursorPos(x, y)
        time.sleep(0.05)
    if a == "click":
        send(mouse(0x02), mouse(0x04))
    elif a == "double_click":
        send(mouse(0x02), mouse(0x04))
        time.sleep(0.08)
        send(mouse(0x02), mouse(0x04))
    elif a == "right_click":
        send(mouse(0x08), mouse(0x10))
    elif a == "drag":
        send(mouse(0x02))
        for i in range(1, 11):
            u32.SetCursorPos(int(x + (x2 - x) * i / 10), int(y + (y2 - y) * i / 10))
            time.sleep(0.02)
        send(mouse(0x04))
    elif a == "scroll":
        if x is not None:
            u32.SetCursorPos(x, y)
        send(mouse(0x0800, int(-dy * 120)))
    elif a == "type":
        units = text.encode("utf-16-le")
        for i in range(0, len(units), 2):
            cu = int.from_bytes(units[i:i + 2], "little")
            send(key(0, cu, 0x0004), key(0, cu, 0x0004 | 0x0002))     # KEYEVENTF_UNICODE（中文也打得出來）
            time.sleep(0.005)
    elif a == "key":
        parts = keys.lower().split("+")
        vks = [WIN_VK.get(p) or (ord(p.upper()) if len(p) == 1 and p.isalnum() else None) for p in parts]
        if None in vks:
            raise ValueError(f"不認得的按鍵：{keys}")
        send(*[key(v) for v in vks])
        send(*[key(v, 0, 0x0002) for v in reversed(vks)])


# ---- macOS：screencapture／sips 截圖、CoreGraphics 送事件
MAC_KEY = {"enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51, "delete": 117, "del": 117, "esc": 53, "escape": 53,
           "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
           **{c: i for i, c in enumerate("asdfhgzxcv")}, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
           "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
           "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
           ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50, **{f"f{i}": c for i, c in
                                                                 enumerate((122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111), 1)}}
MAC_FLAG = {"cmd": 0x100000, "command": 0x100000, "meta": 0x100000, "win": 0x100000, "super": 0x100000,
            "shift": 0x20000, "ctrl": 0x40000, "control": 0x40000, "alt": 0x80000, "option": 0x80000}


def _mac_cg():
    import ctypes
    cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")

    class P(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, P, ctypes.c_uint32]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
    cg.CGEventCreateScrollWheelEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
    cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
    cg.CFRelease.argtypes = [ctypes.c_void_p]
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGDisplayPixelsWide.argtypes = cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsWide.restype = cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    return ctypes, cg, P


def _mac_capture(max_w):
    _, cg, _ = _mac_cg()
    did = cg.CGMainDisplayID()
    lw, lh = cg.CGDisplayPixelsWide(did), cg.CGDisplayPixelsHigh(did)       # 邏輯座標（滑鼠事件用這個）
    d = tempfile.mkdtemp(prefix="xc-shot-")
    try:
        p = os.path.join(d, "s.png")
        rc, _, err = sh(["screencapture", "-x", "-m", "-t", "png", p], timeout=20)
        if rc != 0 or not os.path.exists(p) or os.path.getsize(p) < 100:
            raise RuntimeError("擷取不到螢幕：請到「系統設定 › 隱私權與安全性 › 螢幕錄製」允許執行代理的 Python")
        sh(["sips", "--resampleWidth", str(min(max_w, lw)), p], timeout=30)
        _, info, _ = sh(["sips", "-g", "pixelWidth", "-g", "pixelHeight", p], timeout=20)
        w = int(re.search(r"pixelWidth:\s*(\d+)", info).group(1))
        h = int(re.search(r"pixelHeight:\s*(\d+)", info).group(1))
        with open(p, "rb") as fh:
            return fh.read(), w, h, lw, lh
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _mac_act(a, x, y, x2, y2, text, keys, dy):
    ctypes, cg, P = _mac_cg()

    def post(ev):
        cg.CGEventPost(0, ev)
        cg.CFRelease(ev)

    def m(kind, px, py, btn=0, clicks=1):
        ev = cg.CGEventCreateMouseEvent(None, kind, P(px, py), btn)
        if clicks > 1:
            cg.CGEventSetIntegerValueField(ev, 1, clicks)            # kCGMouseEventClickState
        post(ev)
    if a in ("click", "double_click", "move", "drag", "right_click"):
        m(5, x, y)
        time.sleep(0.05)
    if a == "click":
        m(1, x, y); m(2, x, y)
    elif a == "double_click":
        m(1, x, y); m(2, x, y); m(1, x, y, 0, 2); m(2, x, y, 0, 2)
    elif a == "right_click":
        m(3, x, y, 1); m(4, x, y, 1)
    elif a == "drag":
        m(1, x, y)
        for i in range(1, 11):
            m(6, x + (x2 - x) * i / 10, y + (y2 - y) * i / 10)
            time.sleep(0.02)
        m(2, x2, y2)
    elif a == "scroll":
        ev = cg.CGEventCreateScrollWheelEvent(None, 0, 1, int(-dy * 3))
        post(ev)
    elif a == "type":
        for ch in text:
            units = ch.encode("utf-16-le")
            arr = (ctypes.c_uint16 * (len(units) // 2)).from_buffer_copy(units)
            for down in (True, False):
                ev = cg.CGEventCreateKeyboardEvent(None, 0, down)
                cg.CGEventKeyboardSetUnicodeString(ev, len(arr), arr)
                post(ev)
            time.sleep(0.005)
    elif a == "key":
        parts = keys.lower().split("+")
        if parts == ["cmd"] or parts == ["command"] or parts == ["win"]:
            parts = ["cmd", "space"]                         # macOS 沒有單按的「開始」鍵：換成 Spotlight
        flags = sum(MAC_FLAG.get(p, 0) for p in parts[:-1])
        code = MAC_KEY.get(parts[-1])
        if code is None or any(p not in MAC_FLAG for p in parts[:-1]):
            raise ValueError(f"不認得的按鍵：{keys}")
        for down in (True, False):
            ev = cg.CGEventCreateKeyboardEvent(None, code, down)
            if flags:
                cg.CGEventSetFlags(ev, flags)
            post(ev)


# ---- Linux：grim／ImageMagick 截圖、xdotool 送事件（X11）
def _linux_env():
    return {**os.environ, **_linux_display_env(), **{k: v for k, v in os.environ.items() if k in ("DISPLAY", "WAYLAND_DISPLAY")}}


def _linux_capture(max_w):
    env = _linux_env()
    d = tempfile.mkdtemp(prefix="xc-shot-")
    try:
        p = os.path.join(d, "s.png")
        tries = ([["grim", p]] if env.get("WAYLAND_DISPLAY") else []) + [["import", "-window", "root", p], ["gnome-screenshot", "-f", p], ["scrot", "-o", p]]
        for cmd in tries:
            if shutil.which(cmd[0]):
                subprocess.run(cmd, env=env, capture_output=True, timeout=30)
                if os.path.exists(p) and os.path.getsize(p) > 100:
                    break
        else:
            raise RuntimeError("這台沒有截圖工具：sudo apt install imagemagick xdotool")
        if not os.path.exists(p):
            raise RuntimeError("擷取不到螢幕")
        sw, sh_ = _png_size(p)
        if sw > max_w and shutil.which("convert"):
            subprocess.run(["convert", p, "-resize", f"{max_w}x", p], capture_output=True, timeout=30)
        w, h = _png_size(p)
        with open(p, "rb") as fh:
            return fh.read(), w, h, sw, sh_
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _png_size(p):
    with open(p, "rb") as fh:
        head = fh.read(24)
    return struct.unpack(">II", head[16:24])


def _linux_act(a, x, y, x2, y2, text, keys, dy):
    env = _linux_env()
    if shutil.which("xdotool") and env.get("DISPLAY"):
        run = lambda *args: subprocess.run(["xdotool", *[str(v) for v in args]], env=env, capture_output=True, timeout=30)  # noqa: E731
        if a in ("click", "double_click", "right_click", "move", "drag"):
            run("mousemove", x, y)
        if a == "click":
            run("click", 1)
        elif a == "double_click":
            run("click", "--repeat", 2, 1)
        elif a == "right_click":
            run("click", 3)
        elif a == "drag":
            run("mousedown", 1); run("mousemove", x2, y2); run("mouseup", 1)
        elif a == "scroll":
            run("click", "--repeat", max(1, abs(int(dy))), 5 if dy > 0 else 4)
        elif a == "type":
            run("type", "--delay", 8, "--", text)
        elif a == "key":
            run("key", "--", keys.replace("cmd", "super").replace("win", "super").replace("enter", "Return").replace("esc", "Escape"))
        return
    raise RuntimeError("這台沒有可以送滑鼠鍵盤事件的工具：sudo apt install xdotool（Wayland 桌面請改用 X11 工作階段）")


def desktop_capture_direct(max_w=DESKTOP_MAX_W):
    fn = {"windows": _win_capture, "darwin": _mac_capture}.get(OS, _linux_capture)
    png, w, h, pw, ph = fn(max_w)
    return {"png": base64.b64encode(png).decode(), "width": w, "height": h, "screen_w": pw, "screen_h": ph}


def desktop_check_action(body, w, h):
    """模型給的動作：種類白名單、座標在截圖範圍內、文字長度、按鍵格式。回 (乾淨的動作, 錯誤)。"""
    a = body.get("action")
    if a not in DESKTOP_ACTIONS:
        return None, f"不支援的動作：{a}"
    out = {"action": a}
    if a in ("click", "double_click", "right_click", "move", "drag") or (a == "scroll" and body.get("x") is not None):
        for k in ("x", "y") + (("x2", "y2") if a == "drag" else ()):
            try:
                v = int(round(float(body.get(k))))
            except (TypeError, ValueError):
                return None, f"{k} 要是數字"
            lim = w if k.startswith("x") else h
            if not 0 <= v < lim:
                return None, f"{k}={v} 超出截圖範圍（0～{lim - 1}）"
            out[k] = v
    if a == "type":
        t = body.get("text")
        if not isinstance(t, str) or not t or len(t) > DESKTOP_TEXT_MAX or "\x00" in t:
            return None, f"要打的字要是 1～{DESKTOP_TEXT_MAX} 字"
        out["text"] = t
    if a == "key":
        k = str(body.get("keys") or "").strip()
        if not DESKTOP_KEY_RX.fullmatch(k):
            return None, f"不認得的按鍵組合：{k[:40]}"
        out["keys"] = k
    if a == "scroll":
        try:
            out["dy"] = max(-20, min(20, int(body.get("dy") or 3)))
        except (TypeError, ValueError):
            return None, "捲動量要是整數"
    if a == "wait":
        try:
            out["seconds"] = max(0.5, min(10.0, float(body.get("seconds") or 2)))
        except (TypeError, ValueError):
            out["seconds"] = 2.0
    return out, None


def desktop_act_direct(body):
    """執行一個動作。座標以截圖（width×height）為準，換算成實際螢幕座標。"""
    w, h = int(body.get("width") or 0), int(body.get("height") or 0)
    sw, sh_ = int(body.get("screen_w") or 0), int(body.get("screen_h") or 0)
    if not (w and h and sw and sh_):
        return 400, {"error": "缺少截圖尺寸，請先截一張圖"}
    act, err = desktop_check_action(body, w, h)
    if err:
        return 400, {"error": err}
    a = act["action"]
    if a == "wait":
        time.sleep(act["seconds"])
        return 200, {"ok": True}
    sc = lambda v, a_, b_: None if v is None else int(round(v * b_ / a_))  # noqa: E731
    x, y = sc(act.get("x"), w, sw), sc(act.get("y"), h, sh_)
    x2, y2 = sc(act.get("x2"), w, sw), sc(act.get("y2"), h, sh_)
    fn = {"windows": _win_act, "darwin": _mac_act}.get(OS, _linux_act)
    try:
        fn(a, x, y, x2, y2, act.get("text", ""), act.get("keys", ""), act.get("dy", 0))
    except (RuntimeError, ValueError, OSError) as ex:
        return 400, {"error": str(ex)}
    return 200, {"ok": True}


def desktop_open_direct(path):
    """用檔案總管（Finder）打開一個資料夾。只開允許範圍裡的（代理那邊另外限定模型存放區與工作資料夾）。"""
    full = in_browse_roots(path)
    if full is None:
        return 400, {"error": "只能打開家目錄、資料磁碟或外接碟裡的資料夾"}
    if not os.path.isdir(full):
        return 404, {"error": "沒有這個資料夾"}
    if OS == "windows":
        subprocess.Popen(["explorer", full])
    elif OS == "darwin":
        subprocess.Popen(["open", full])
    else:
        subprocess.Popen(["xdg-open", full], env=_linux_env())
    return 200, {"ok": True}


def desktop_call(method, path, body=None, timeout=60):
    """操作電腦的入口：代理自己能做就直接做，不能就轉給桌面小幫手。"""
    how, extra = desktop_route()
    if how is None:
        return 409, {"error": extra, "unavailable": True}
    if how == "helper":
        port, tok = extra
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        try:
            with LOCAL.open(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as ex:
            try:
                return ex.code, json.load(ex)
            except Exception:  # noqa: BLE001
                return ex.code, {"error": f"桌面小幫手回 {ex.code}"}
        except Exception as ex:  # noqa: BLE001
            return 502, {"error": f"連不上桌面小幫手（{str(ex)[:80]}）：請確認有人登入這台的桌面"}
    return desktop_dispatch(method, path, body or {})


def desktop_dispatch(method, path, body):
    try:
        if path == "/desktop/info":
            return 200, {"ok": True, "os": OS, "how": desktop_route()[0]}
        if path == "/desktop/screenshot":
            return 200, desktop_capture_direct()
        if path == "/desktop/act":
            return desktop_act_direct(body)
        if path == "/desktop/open":
            return desktop_open_direct(body.get("path"))
    except (RuntimeError, OSError) as ex:
        return 500, {"error": str(ex)}
    return 404, {"error": "找不到"}


def desktop_enabled(reg=None):
    return bool((reg or registry())["machine"].get("desktop_enabled"))


def desktop_status():
    how, why = desktop_route()
    return 200, {"enabled": desktop_enabled(), "available": how is not None, "how": how, "why": why if how is None else None, "os": OS}


AGENT_FILE = os.path.abspath(__file__)


def agent_sha():
    try:
        with open(AGENT_FILE, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def self_update(body):
    """主控台推來的新版代理：核對 sha256、先編譯一次，舊的留 .bak，再換上；回應送出後結束，由服務管理員重新帶起。
    Windows 的桌面小幫手那一份也一起換（它自己發現檔案換了會重新啟動）。"""
    import py_compile
    try:
        data = base64.b64decode(str(body.get("code") or ""), validate=True)
    except (ValueError, TypeError):
        return 400, {"error": "程式內容格式不對"}
    want = str(body.get("sha256") or "").lower()
    if not data or len(data) > 4 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != want:
        return 400, {"error": "程式內容的 sha256 對不上，不更新"}
    if b"xconsole-agent" not in data:
        return 400, {"error": "這不像 xConsole 的代理程式"}
    tmp = AGENT_FILE + ".new"
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        py_compile.compile(tmp, doraise=True, cfile=tmp + "c")
    except py_compile.PyCompileError as ex:
        os.remove(tmp)
        return 400, {"error": f"新版編譯不過，不更新：{str(ex)[:200]}"}
    finally:
        if os.path.exists(tmp + "c"):
            os.remove(tmp + "c")
    shutil.copy2(AGENT_FILE, AGENT_FILE + ".bak")
    os.replace(tmp, AGENT_FILE)
    if OS == "windows":
        user = (load_text(os.path.join(STATE_DIR, "desktop-user.txt")) or "").strip()
        hp = os.path.join(desktop_user_dir(user), "desktop", "xconsole_agent.py") if user else None
        if hp and os.path.isfile(hp):
            try:
                shutil.copy2(AGENT_FILE, hp)
            except OSError:
                pass

    def bye():
        time.sleep(1.5)
        os._exit(3)                       # 不是 0：Windows 排程要「失敗」才會重新啟動；systemd／launchd 一律重啟
    threading.Thread(target=bye, daemon=True).start()
    return 200, {"ok": True, "sha256": want, "restarting": True}


def run_desktop_helper():
    """桌面小幫手：使用者登入後以使用者身分啟動，只聽 127.0.0.1、要權杖。權杖與埠寫在使用者自己的資料夾給代理讀。"""
    d = desktop_user_dir()
    os.makedirs(d, exist_ok=True)
    tok = secrets.token_urlsafe(32)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _out(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _ok(self):
            got = self.headers.get("Authorization") or ""
            if not (got.startswith("Bearer ") and secrets.compare_digest(got[7:], tok)):
                self._out(401, {"error": "需要權杖"})
                return False
            return True

        def do_GET(self):  # noqa: N802
            if self._ok():
                self._out(*desktop_dispatch("GET", self.path.split("?")[0], {}))

        def do_POST(self):  # noqa: N802
            if not self._ok():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
            except Exception:  # noqa: BLE001
                body = {}
            self._out(*desktop_dispatch("POST", self.path.split("?")[0], body))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    p = os.path.join(d, "desktop-helper.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"port": srv.server_address[1], "token": tok, "pid": os.getpid()}, fh)
    os.replace(tmp, p)

    def watch():
        # 代理更新時會把這份程式換掉：發現檔案變了就用新版重新啟動自己（排程只在登入時帶一次，不會幫忙重啟）
        try:
            mt = os.path.getmtime(AGENT_FILE)
        except OSError:
            return
        while True:
            time.sleep(20)
            try:
                if os.path.getmtime(AGENT_FILE) != mt:
                    kw = {"creationflags": 0x00000008 | 0x00000200} if OS == "windows" else {"start_new_session": True}
                    subprocess.Popen([sys.executable, AGENT_FILE, "--desktop-helper"], **kw)
                    os._exit(0)
            except OSError:
                continue
    threading.Thread(target=watch, daemon=True).start()
    srv.serve_forever()



def _warm_caches():
    """代理一啟動就在背景把慢的查詢先跑一次（磁碟種類、llama-server 版本、顯示卡用量），使用者第一次開頁不用等。"""
    for fn in (storage_choices, llama_candidates, probe_gpus):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


@_locked
def desktop_first_run():
    """桌面版第一次啟動：記憶體架構還沒確認過就直接套用偵測結果（之後在「這台的設定」照樣能改）。"""
    reg = registry()
    m = reg["machine"]
    if m.get("confirmed"):
        return
    try:
        sug, _ = suggest_memory_model()
    except Exception:  # noqa: BLE001  偵測失敗就照舊，網頁上會請人確認
        return
    if sug:
        m["memory_model"], m["confirmed"] = sug, True
        save_json(REGISTRY, reg)
        print(f"桌面版第一次啟動：記憶體架構自動套用偵測結果 {sug}", flush=True)


def _watch_parent(pid):
    """父行程（xConsole 應用程式）不在了就停掉自己帶起來的模型服務並結束。"""
    while True:
        time.sleep(3)
        if not pid_alive(pid):
            try:
                if not SYSTEMD:
                    SUP.stop_all()
            finally:
                os._exit(0)



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
    if DESKTOP_AGENT:
        desktop_first_run()
    threading.Thread(target=_warm_caches, daemon=True).start()
    if not SYSTEMD:
        SUP.boot()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    # 桌面版（xConsole 應用程式帶起來的）只給同一台的主控台用：只聽 127.0.0.1；應用程式關掉就跟著結束
    bind = sys.argv[sys.argv.index("--bind") + 1] if "--bind" in sys.argv[:-1] else "0.0.0.0"
    if bind not in ("0.0.0.0", "127.0.0.1"):
        raise SystemExit("--bind 只收 0.0.0.0 或 127.0.0.1")
    if "--parent-pid" in sys.argv[:-1]:
        threading.Thread(target=_watch_parent, args=(int(sys.argv[sys.argv.index("--parent-pid") + 1]),), daemon=True).start()
    srv = AgentTLSServer((bind, PORT), Handler)
    srv.ctx = ctx
    print(f"xconsole-agent 聽在 https://{bind}:{PORT}（憑證 {CERT_FILE}，權杖在 {TOKEN_FILE}）", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--launch-llama":
        launch_llama(sys.argv[2])
    elif "--desktop-helper" in sys.argv:
        run_desktop_helper()
    else:
        main()
