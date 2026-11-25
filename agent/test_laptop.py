#!/usr/bin/env python3
"""筆電：電池與計費網路（清單 #17）。驗：三種系統的電池狀態判讀、計費網路判讀、群組的筆電省電開著時
用電池不下載、不換上大模型（小模型照換）、計費網路不下載、都只是先等（PeerWait）不算失敗、關掉省電就照常、群組設定檢查。
跑法：python3 agent/test_laptop.py（只用標準函式庫＋代理本身）
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-laptop-")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "console"))
import groups  # noqa: E402
import xconsole_agent as A  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


case("macOS：用電池中", A.parse_pmset("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t67%; discharging") == (True, True, 67))
case("  macOS：接著電", A.parse_pmset("Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged")[1] is False)
case("  macOS：桌機沒有電池", A.parse_pmset("Now drawing from 'AC Power'\n") == (False, False, None))

ps = tempfile.mkdtemp(prefix="xc-ps-")


def supply(name, **kv):
    d = os.path.join(ps, name)
    os.makedirs(d, exist_ok=True)
    for k, v in kv.items():
        with open(os.path.join(d, k), "w", encoding="utf-8") as fh:
            fh.write(v + "\n")


supply("BAT0", type="Battery", capacity="42")
supply("AC", type="Mains", online="0")
case("Linux：電池在、電源沒接", A.parse_linux_power(ps) == (True, True, 42), A.parse_linux_power(ps))
supply("AC", type="Mains", online="1")
case("  Linux：接上電源", A.parse_linux_power(ps)[1] is False)
case("  Linux：沒有電池的桌機", A.parse_linux_power(tempfile.mkdtemp())[:2] == (False, False))
case("Windows：用電池中", A.parse_win_power(0, 1, 55) == (True, True, 55))
case("  Windows：接著電", A.parse_win_power(1, 8, 90)[1] is False)
case("  Windows：沒有電池（128）、不知道（255）", A.parse_win_power(0, 128, 255)[:2] == (False, False)
     and A.parse_win_power(0, 255, 255)[:2] == (False, False))
case("計費網路：NetworkManager 標 yes（含猜的）", A.parse_metered("linux", "GENERAL.METERED:yes (guessed)\nGENERAL.METERED:no")
     and not A.parse_metered("linux", "GENERAL.METERED:no\nGENERAL.METERED:unknown"))
case("  計費網路：Windows 的 Fixed／Variable 算，Unrestricted 不算", A.parse_metered("windows", "Variable\r\n")
     and not A.parse_metered("windows", "Unrestricted"))

POWER = {"battery": True, "on_battery": True, "percent": 30}
MET = {"v": False}
SET = {"laptop_care": True, "sources": ["huggingface"]}
A.power_state = lambda: dict(POWER)
A.metered = lambda: MET["v"]
A.group_state = lambda: {"group_id": "abcdef12", "settings": SET}
A.hf_files = lambda repo, token="": (502, {"error": "測試：沒有連外"})     # 不連 Hugging Face
case("用電池：不下載", "接上電源再下載" in A.laptop_hold("download"))
case("  用電池：不換上大模型", "接上電源再換上" in A.laptop_hold("load", 20))
case("  用電池：小模型照換", A.laptop_hold("load", 2) == "")
try:
    A._group_fetch({"settings": SET, "steps": []}, "a/b:Q4_K_M")
    waited = False
except A.PeerWait as exc:
    waited = "電池" in str(exc)
except RuntimeError:
    waited = False
case("  群組準備：用電池時先等（不算失敗）", waited)
POWER.update(on_battery=False)
MET["v"] = True
case("計費網路：不下載", "計費連線" in A.laptop_hold("download"))
case("  計費網路：換模型照常（不用下載）", A.laptop_hold("load", 20) == "")
MET["v"] = False
case("接著電、一般網路：都不用等", A.laptop_hold("download") == "" and A.laptop_hold("load", 50) == "")
POWER.update(on_battery=True)
SET["laptop_care"] = False
try:
    A._group_fetch({"settings": SET, "steps": []}, "a/b:Q4_K_M")
    passed = False
except A.PeerWait:
    passed = False
except RuntimeError as exc:
    passed = "查不到" in str(exc)
case("群組關掉筆電省電：用電池也照常下載", passed)

case("群組設定：筆電省電預設開", groups.check({})["laptop_care"] is True)
try:
    groups.check({"laptop_care": "yes"})
    bad = False
except ValueError:
    bad = True
case("  只收開或關", bad and groups.check({"laptop_care": False})["laptop_care"] is False)

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
