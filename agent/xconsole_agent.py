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
import json
import os
import re
import secrets
import shlex
import socket
import ssl
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".xconsole")
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

def sh(cmd, timeout=20):
    """跑一條指令，回 (returncode, stdout, stderr)。失敗不丟例外。"""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "逾時"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


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

def _meminfo(key):
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) * 1024
    except Exception:  # noqa: BLE001
        pass
    return 0


def ram_pool():
    total = _meminfo("MemTotal")
    avail = _meminfo("MemAvailable")
    return {"total_gib": round(total / GIB, 1), "free_gib": round(avail / GIB, 1),
            "used_gib": round((total - avail) / GIB, 1)}


def swap_pool():
    total = _meminfo("SwapTotal")
    free = _meminfo("SwapFree")
    return {"total_gib": round(total / GIB, 1), "used_gib": round((total - free) / GIB, 1)}


def probe_nvidia():
    rc, out, _ = sh(
        "nvidia-smi --query-gpu=index,name,temperature.gpu,power.draw,utilization.gpu,"
        "memory.total,memory.used --format=csv,noheader,nounits", timeout=15)
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


def probe_gpus():
    return probe_nvidia() or probe_amd_sysfs() or []


def hardware():
    """先用同機已在跑的 node_agent，拿不到才自己量。拿不到就老實標未知，不假裝零。"""
    for url in NODE_AGENTS:
        try:
            with urllib.request.urlopen(url, timeout=4) as resp:
                data = json.load(resp)
            if data.get("gpu") or data.get("cpu"):
                data["_source"] = url
                return data
        except Exception:  # noqa: BLE001
            continue
    gpus = probe_gpus()
    rc, load_out, _ = sh("cat /proc/loadavg")
    load = None
    try:
        load = float(load_out.split()[0])
    except Exception:  # noqa: BLE001
        pass
    uptime = None
    try:
        uptime = float(open("/proc/uptime", encoding="utf-8").read().split()[0])
    except Exception:  # noqa: BLE001
        pass
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
        "ram": {"total": _meminfo("MemTotal")},
        "uptime": uptime,
    }


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
    rc, out, _ = sh("nvidia-smi --query-gpu=clocks.sm,clocks.max.sm "
                    "--format=csv,noheader,nounits", timeout=10)
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


def ports():
    rc, out, _ = sh("ss -tlnpH 2>/dev/null")
    if rc != 0 or not out:
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
            if mm:
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


def unit_state(unit):
    """回 systemd 的真實狀態字串，不要壓成開／關兩種。

    卡在 activating 的服務（Type=notify 但程式沒送就緒訊號，或 ExecStartPre 很久）
    壓成「關」會讓人以為它沒跑、壓成「開」又會以為它好了，兩種都會誤導。
    """
    _, out, _ = sh(f"systemctl --user is-active {shlex.quote(unit)}")
    st = out.strip()
    if st in ("unknown", "", "inactive"):
        _, out2, _ = sh(f"systemctl is-active {shlex.quote(unit)}")
        if out2.strip() not in ("unknown", ""):
            st = out2.strip()
    return st or "unknown"


def unit_active(unit):
    return unit_state(unit) == "active"


def unit_scope(unit):
    """這個 unit 是 user 層還是 system 層。兩層指令不一樣，猜錯就全部失敗。"""
    _, out, _ = sh(f"systemctl --user cat {shlex.quote(unit)} >/dev/null 2>&1 && echo user")
    return "user" if out.strip() == "user" else "system"


def sysctl(unit, verb, timeout=200):
    scope = unit_scope(unit)
    flag = "--user " if scope == "user" else ""
    return sh(f"systemctl {flag}{verb} {shlex.quote(unit)}", timeout=timeout)


def unit_enabled(unit):
    _, out, _ = sysctl(unit, "is-enabled", timeout=20)
    return out.strip() in ("enabled", "enabled-runtime")


def unit_rss_gib(unit):
    """unit 底下所有進程的 RSS 合計。cgroup 的 memory.current 含 page cache 會高估，不用它。"""
    _, pid, _ = sysctl(unit, "show -p MainPID --value", timeout=20)
    pid = pid.strip()
    if not pid.isdigit() or pid == "0":
        return 0.0
    _, out, _ = sh(f"ps -o rss= -g $(ps -o pgid= -p {pid} 2>/dev/null | tr -d ' ') 2>/dev/null")
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
    rc, out, _ = sh(f"tail -n 4000 {shlex.quote(log)} 2>/dev/null | grep -F 'serve: prompt'")
    if rc != 0 or not out:
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
            weights = [float(x.get("est_vram_gib") or 0) for x in reg["slots"]
                       if unit_state(x["unit"]) in ("active", "activating")] if False else None
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
    """掃出這台裝了哪些可以換的模型。

    Strata 一顆模型是「pack（含 tokenizer）＋ 權重檔」兩件成套的，
    少一邊就起不來，所以這裡只回報配得起來的組合，配不起來的另外列出來並說明缺什麼。
    """
    reg = registry()
    slot = next((x for x in reg["slots"] if x["id"] == slot_id), None) if slot_id else None
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
        if st == "failed":
            _, tail, _ = sh(f"journalctl --user -u {shlex.quote(slot['unit'])} -n 3 --no-pager -o cat")
            return False, (tail or "服務退出").replace("\n", " ")[:200]
        if st == "active":
            if not port:
                return True, ""
            rc, code, _ = sh(f"curl -s -o /dev/null -m 6 -w '%{{http_code}}' "
                             f"http://127.0.0.1:{int(port)}/v1/models")
            if code.strip() == "200":
                return True, ""
            last = f"服務在跑但 :{port} 還沒回應"
        time.sleep(5)
    return False, last


def set_model(slot_id, pack, native, force=False):
    """把某個欄位換成另一顆模型：先驗證成套、備份、改設定、重啟，失敗自動還原。"""
    reg = registry()
    slot = next((x for x in reg["slots"] if x["id"] == slot_id), None)
    if not slot or not slot.get("config"):
        return 404, {"error": f"{slot_id} 沒有可改的設定檔"}

    allowed = {m["pack"]: m for m in (catalog(slot_id).get("models") or [])}
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
    for key in ("exe", "lib_dirs", "port", "parallel", "fit_max_tokens", "host"):
        if key in old_cfg:
            new_cfg[key] = old_cfg[key]
    new_cfg["log"] = old_cfg.get("log", new_cfg.get("log"))
    save_json(slot["config"], new_cfg)

    verdict = admit(slot_id)
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
            sl["model"] = pick["name"]
    reg["measure"].pop(slot_id, None)   # 換了模型，舊的速度實測不再代表它
    save_json(REGISTRY, reg)
    time.sleep(8)                        # 等它把權重載進去再量，不然量到載入中的瞬間
    record_model_measure(slot_id, pick["name"])
    return 200, {"ok": True, "model": pick["name"], "backup": backup}


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


def admit(slot_id, reg=None, hw=None):
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
        if need_vram and wv < fl["vram"]:
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
    slots = [slot_state(s, reg, hw) for s in reg["slots"]]
    for sl in reg["slots"]:
        sl.pop("_busy_cache", None)
    save_json(REGISTRY, reg)  # 把這一輪撈到的實測值寫回去

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


def do_logs(slot_id, lines=300):
    reg = registry()
    slot = next((s for s in reg["slots"] if s["id"] == slot_id), None)
    if not slot or not slot.get("unit"):
        return 404, {"error": "沒有這個欄位"}
    scope = unit_scope(slot["unit"])
    flag = "--user " if scope == "user" else ""
    _, out, err = sh(f"journalctl {flag}-u {shlex.quote(slot['unit'])} "
                     f"-n {int(lines)} --no-pager -o short-iso")
    text = out or err
    if slot.get("log") and os.path.exists(slot["log"]):
        _, out2, _ = sh(f"tail -n {int(lines)} {shlex.quote(slot['log'])}")
        if out2:
            text = (text + "\n----- 服務自己的 log -----\n" + out2).strip()
    return 200, {"slot": slot_id, "unit": slot["unit"], "text": text[-150000:]}


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
    _, out, _ = sh("hostname -I")
    names.update(out.split())
    return sorted(n for n in names if n and not n.startswith("fe80") and "%" not in n)


def ensure_cert():
    """代理也走 HTTPS：主控台用 Bearer 權杖連過來，明文 http 等於權杖在內網裸奔。
    第一次啟動產一張自簽，SAN 涵蓋本機所有 IP；主控台部署時經 SSH 取回這張當作信任依據。"""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    san = ",".join(("IP:" if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", n) and (":" in n or n.count(".") == 3)
                    else "DNS:") + n for n in local_names())
    rc, _, err = sh(f"openssl req -x509 -newkey rsa:2048 -nodes -days 3650 "
                    f"-keyout {shlex.quote(KEY_FILE)} -out {shlex.quote(CERT_FILE)} "
                    f"-subj /C=TW/O=xCloudinfo/CN=xconsole-agent-{shlex.quote(socket.gethostname())} "
                    f"-addext {shlex.quote('subjectAltName=' + san)} "
                    f"-addext basicConstraints=critical,CA:FALSE", timeout=60)
    if rc != 0:
        raise SystemExit(f"產不出代理憑證（{err[:200]}），停止啟動，不退回明文 http")
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


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "xconsole-agent/1.0"
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
            code, out = set_model(m.group(1), body.get("pack") or "",
                                  body.get("native") or "", force=force)
            return self._send(code, out)
        m = re.fullmatch(r"/profile/([\w.-]+)", path)
        if m:
            code, out = do_profile(m.group(1), force=force)
            return self._send(code, out)
        m = re.fullmatch(r"/all/(start|stop|restart)", path)
        if m:
            code, out = do_all(m.group(1), force=force)
            return self._send(code, out)
        if path == "/registry":
            if not isinstance(body, dict) or "machine" not in body:
                return self._send(400, {"error": "登錄檔格式不對"})
            save_json(REGISTRY, body)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "找不到"})


def main():
    registry()
    ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    srv = AgentTLSServer(("0.0.0.0", PORT), Handler)
    srv.ctx = ctx
    print(f"xconsole-agent 聽在 https://0.0.0.0:{PORT}（憑證 {CERT_FILE}，權杖在 {TOKEN_FILE}）", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
