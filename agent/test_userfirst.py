#!/usr/bin/env python3
"""工作站准入（清單 #16）：群組的「保留給使用者的記憶體」加在底線上；使用者優先：使用者自己的程式把可用記憶體壓到
底線以下，先卸載最大的那顆模型並留下說明。驗：底線加上保留、准入照新底線擋、瞬間尖峰不卸、連續低於底線才卸、
一次只卸一顆（最大的）、群組選「不動」不卸、沒加入群組的機器不卸、群組設定的選項檢查。
跑法：python3 agent/test_userfirst.py（只用標準函式庫＋代理本身）
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-userfirst-")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "console"))
import groups  # noqa: E402
import xconsole_agent as A  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


GS = {"group_id": "", "settings": None, "active": None}
A.group_state = lambda: GS
A.ram_pool = lambda: {"total_gib": 64.0, "free_gib": 30.0}
M = {"memory_model": "discrete"}
base = A.floors(M)["ram"]
GS.update(group_id="abcdef12", settings={"reserve_user_gib": 8})
case("群組的保留記憶體加在系統記憶體的底線上", A.floors(M)["ram"] == round(base + 8, 1), (base, A.floors(M)["ram"]))
GS.update(settings={"reserve_user_gib": None})
case("  沒設就照原本的底線", A.floors(M)["ram"] == base)

slots = [{"id": "small", "unit": "u-small", "est_ram_gib": 2, "est_vram_gib": 4, "model": "小"},
         {"id": "big", "unit": "u-big", "est_ram_gib": 6, "est_vram_gib": 20, "model": "大"},
         {"id": "empty", "unit": None}]
reg = {"machine": M, "slots": slots}
A._alive = lambda sl: bool(sl.get("unit"))
stopped = []
GS.update(group_id="abcdef12", settings={"reserve_user_gib": 4}, active={"user_first": "unload", "reserve_user_gib": 4})
line = A.floors(M)["ram"]
hits = {}
for _ in range(A.USER_FIRST_HITS - 1):
    A.user_first_check(reg, line - 1, hits, stop=stopped.append)
A.user_first_check(reg, line + 5, hits, stop=stopped.append)
for _ in range(A.USER_FIRST_HITS - 1):
    A.user_first_check(reg, line - 1, hits, stop=stopped.append)
case("瞬間尖峰（沒有連續低於底線）不卸載", stopped == [])
got = A.user_first_check(reg, line - 1, hits, stop=stopped.append)
case("連續低於底線：先卸載最大的那顆、一次只卸一顆", got == ["big"] and stopped == ["u-big"], (got, stopped))
note = A.watchdog_notes().get("big") or {}
case("  留下說明：剩多少、底線多少、按啟動就能再用", note.get("user_first") and "啟動" in note.get("message", "")
     and note.get("line") == round(line, 1), note)
case("  只是卸載，沒有取消開機自啟", note.get("disabled") is False)
GS.update(active={"user_first": "keep"})
stopped.clear()
for _ in range(A.USER_FIRST_HITS * 2):
    A.user_first_check(reg, 0.5, hits, stop=stopped.append)
case("群組選「不動」：不卸載", stopped == [])
GS.update(group_id="", active=None)
for _ in range(A.USER_FIRST_HITS * 2):
    A.user_first_check(reg, 0.5, hits, stop=stopped.append)
case("沒加入群組的機器（個人桌面版）：不卸載", stopped == [])

case("群組設定：預設先卸載", groups.check({})["user_first"] == "unload")
try:
    groups.check({"user_first": "kill"})
    bad = False
except ValueError:
    bad = True
case("  只收「先卸載」或「不動」", bad and groups.check({"user_first": "keep"})["user_first"] == "keep")

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
