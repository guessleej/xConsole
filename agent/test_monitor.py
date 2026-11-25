#!/usr/bin/env python3
"""監測與執行頁的即時數字（LocModel 3.6.2比對5.2每天#1～#3、#18、#19）。驗：
- CPU使用率：Linux讀/proc/stat兩次相減、Windows的GetSystemTimes同一套算法、讀不到回None、本機真的讀得到
- 記憶體種類：dmidecode、Windows的SMBIOS代碼、macOS的system_profiler、焊在板上的統一記憶體照型號表
- /live只讀、每個欄位帶狀態與記憶體；主控台的檢視帳號看得到、使用者看不到
- 畫面守衛：導覽列「執行」旁的小圓點、監測在概覽的第二列、2秒輪詢換頁就停、執行頁換上下文就重算分層
跑法：python3 agent/test_monitor.py（只用標準函式庫＋代理本身）
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-monitor-")
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

ROOT = os.path.dirname(HERE)
OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


# ---- CPU使用率
orig_os, orig_load = A.OS, A.load_text
stats = iter(["cpu  100 0 100 800 0 0 0 0 0 0\n", "cpu  200 0 200 1400 0 0 0 0 0 0\n"])
A.OS = "linux"
A.load_text = lambda p: next(stats) if p == "/proc/stat" else orig_load(p)
A._CPU_PREV.clear()
first = A.cpu_usage()
case("Linux：第一次沒有前一筆，隔一下再讀一次；閒置600／總計800＝25%", first == 25.0, first)
stats = iter(["cpu  200 0 200 1400 0 0 0 0 0 0\n", "cpu  290 0 290 1420 0 0 0 0 0 0\n"])
case("  第二次直接跟上一次相減：閒置20／總計200＝90%", A.cpu_usage() == 90.0)
A.load_text = lambda p: "garbage\n" if p == "/proc/stat" else orig_load(p)
A._CPU_PREV.clear()
case("  /proc/stat讀不懂就回None，不編數字", A.cpu_usage() is None)
A.OS, A.load_text = orig_os, orig_load
A._CPU_PREV.clear()
real = A.cpu_usage()
case(f"這台（{A.OS}）真的讀得到CPU使用率，在0～100之間", real is not None and 0 <= real <= 100, real)

# ---- 記憶體種類
orig_sh, orig_chip = A.sh, A._chip_probe
A._chip_probe = lambda: {"gpu": None}
A.OS = "linux"
A.sh = lambda argv, timeout=20: (0, "Memory Device\n\tSize: 32 GB\n\tType: DDR5\n\tConfigured Memory Speed: 5600 MT/s\n", "") \
    if argv[0] == "dmidecode" else (1, "", "")
case("Linux：dmidecode讀得到就寫種類與速度", A._probe_mem_kind() == "DDR5-5600", A._probe_mem_kind())
A.sh = lambda argv, timeout=20: (1, "", "Permission denied")
A._chip_probe = lambda: {"gpu": "AMD Radeon 8060S"}
case("  沒有root讀不到dmidecode：AI 395照型號表是LPDDR5X", A._probe_mem_kind() == "LPDDR5X", A._probe_mem_kind())
A._chip_probe = lambda: {"gpu": "NVIDIA RTX 4090"}
case("  獨立顯示卡的機器讀不到就不寫（不猜插槽式記憶體）", A._probe_mem_kind() is None)
A.OS = "windows"
A.sh = lambda argv, timeout=20: (0, json.dumps({"SMBIOSMemoryType": 34, "ConfiguredClockSpeed": 5600}), "")
case("Windows：SMBIOS代碼34是DDR5", A._probe_mem_kind() == "DDR5-5600", A._probe_mem_kind())
A.sh = lambda argv, timeout=20: (0, json.dumps({"SMBIOSMemoryType": 35, "ConfiguredClockSpeed": 0}), "")
case("  代碼35是LPDDR5，速度0就不寫速度", A._probe_mem_kind() == "LPDDR5", A._probe_mem_kind())
A.OS = "darwin"
A.sh = lambda argv, timeout=20: (0, json.dumps({"SPMemoryDataType": [{"_name": "Memory", "dimm_type": "LPDDR5X"}]}), "")
case("macOS：system_profiler的dimm_type", A._probe_mem_kind() == "LPDDR5X", A._probe_mem_kind())
A.sh = lambda argv, timeout=20: (1, "not json", "")
A._chip_probe = lambda: {"gpu": None}
case("  指令失敗也不丟例外", A._probe_mem_kind() is None)
A.OS, A.sh, A._chip_probe = orig_os, orig_sh, orig_chip

# ---- /live
reg = A.registry()
reg["slots"] = [{"id": "main", "role": "主模型", "unit": "xc-main", "model": "gemma"}, {"id": "empty", "role": "空", "unit": None}]
orig_reg, orig_state, orig_rss, orig_gpus = A.registry, A.unit_state, A.unit_rss_gib, A.probe_gpus
A.registry = lambda: reg
A.unit_state = lambda u: "active"
A.unit_rss_gib = lambda u: 3.25
A.probe_gpus = lambda: [{"id": "0", "name": "RTX", "use": 40, "temp": 61, "power": 120, "vram_total": 24 * A.GIB, "vram_used": 6 * A.GIB}]
lv = A.live_stats()
A.registry, A.unit_state, A.unit_rss_gib, A.probe_gpus = orig_reg, orig_state, orig_rss, orig_gpus
case("/live：每張卡的使用率、溫度、功耗、顯示記憶體", lv["gpus"] == [{"id": "0", "name": "RTX", "use": 40, "temp": 61, "power": 120,
                                                        "total_gib": 24.0, "used_gib": 6.0}], lv["gpus"])
case("  只列有服務的欄位，帶狀態與佔用的系統記憶體", [(s["id"], s["state"], s["ram_gib"]) for s in lv["slots"]] == [("main", "active", 3.25)], lv["slots"])
case("  有記憶體總量與已用、CPU核心數", lv["ram"]["total_gib"] > 0 and 0 <= lv["ram"]["used_gib"] <= lv["ram"]["total_gib"]
     and lv["cpu"]["cores"] == os.cpu_count())
src = open(os.path.join(HERE, "xconsole_agent.py"), encoding="utf-8").read()
case("  代理的/live跟/state一樣要先驗身分", bool(re.search(r"if not self\._auth\(\):\s*return None.*?if path == \"/live\":", src, re.S)))

# ---- 主控台：誰看得到
sys.path.insert(0, os.path.join(ROOT, "console"))
try:
    import xconsole_server as X  # noqa: E402
except ImportError as ex:               # 沒裝cryptography的環境只驗前面的代理部分
    print(f"[略過] 主控台權限（{ex}）")
else:
    case("主控台：檢視與維運帳號看得到即時數字", X.user_may("GET", "/api/node/n1/live", "viewer") and X.user_may("GET", "/api/node/n1/live", "operator"))
    case("  一般使用者看不到機器的即時數字", not X.user_may("GET", "/api/node/n1/live", "user"))

# ---- 畫面守衛
JS = open(os.path.join(ROOT, "console", "static", "app.js"), encoding="utf-8").read()
PG = open(os.path.join(ROOT, "console", "static", "pages.js"), encoding="utf-8").read()
nav = re.search(r"function renderNav\(\) \{(.*?)\n\}", JS, re.S).group(1)
case("導覽列「執行」旁有小圓點：有模型在跑就亮、載入中換忙碌樣式", 'key === "run" && dotCls' in nav and '"activating"' in nav and "run-dot busy" in nav)
case("監測放在概覽的第二列（頂層維持八格）", 'subBtn("監測", "monitor"' in nav and re.search(r'v === "monitor"\) return "home"', PG))
case("監測頁有自己的入口與局部更新，不被15秒的整頁重畫打斷",
     'else if (v === "monitor") viewMonitor();' in JS and re.search(r'\[[^\]]*"monitor"\]\.includes\(S\.view\)', JS))
loop = re.search(r"function liveLoop\(.*?\n\}", JS, re.S).group(0)
case("2秒輪詢：換頁或區塊被拿掉就停、分頁藏到背景不讀", "anchor.isConnected && S.view === v" in loop and "document.hidden" in loop
     and "if (alive()) setTimeout(tick, ms)" in loop)
mon = re.search(r"function viewMonitor\(\) \{(.*?)\n\}", PG, re.S).group(1)
case("監測頁只換有變的數字：卡片建一次，表格內容沒變不重畫", "liveLoop(n.id, grid, onLive)" in mon and "dataset.sig === sig" in mon
     and "dataset.sig !== sig" in mon and "innerHTML" not in mon)
case("  讀不到的數值寫「讀不到」，不寫0", '"讀不到"' in mon and '"使用率讀不到"' in mon)
case("執行頁換上下文：用預先算好的那一份重畫分層與速度", "const p = planAt(m, st.ctx);" in PG and "st.replan(ctx)" in PG
     and "drawSettings(right, n, cur, planAt(cur, st.ctx)" in PG)
case("  執行中的狀態列每2秒只換文字", "liveLoop(n.id, statusHolder, (lv) => statusLive(statusHolder, n, rs, lv))" in PG
     and "b.textContent !== text" in PG)
case("  分層寫出剩幾層專家放系統記憶體、記憶體種類", "層專家放這裡" in PG and "n.state.mem_kind" in PG)

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
