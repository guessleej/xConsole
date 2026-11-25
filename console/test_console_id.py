#!/usr/bin/env python3
"""主控台識別碼（清單#26）：第一次產生、之後不變、檔案只給自己讀、壞掉換新並留舊檔、同時啟動只會有一個。
跑法：python3 console/test_console_id.py"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modules

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


d = tempfile.mkdtemp(prefix="xc-cid-")
a = modules.console_id(d)
check("第一次產生UUID格式的識別碼", modules.CONSOLE_ID_RX.fullmatch(a), a)
check("再讀一次一樣（重開機、升級不變）", modules.console_id(d) == a)
if os.name != "nt":
    check("檔案權限600", os.stat(os.path.join(d, "console_id")).st_mode & 0o777 == 0o600)
check("沒有留下暫存檔", sorted(os.listdir(d)) == ["console_id"], os.listdir(d))

with open(os.path.join(d, "console_id"), "w", encoding="utf-8") as f:
    f.write("被改壞了\n")
b = modules.console_id(d)
bad = [n for n in os.listdir(d) if n.startswith("console_id.bad-")]
check("內容壞掉：換新的，舊檔改名留著", b != a and modules.CONSOLE_ID_RX.fullmatch(b) and len(bad) == 1, os.listdir(d))

d2 = tempfile.mkdtemp(prefix="xc-cid-")
got = []
ts = [threading.Thread(target=lambda: got.append(modules.console_id(d2))) for _ in range(16)]
[t.start() for t in ts]
[t.join() for t in ts]
check("同時啟動16個：拿到同一個識別碼", len(set(got)) == 1 and len(got) == 16, set(got))
check("  沒有被當成壞檔改名、沒有暫存檔", sorted(os.listdir(d2)) == ["console_id"], os.listdir(d2))

print("全部通過" if OK else "有失敗")
sys.exit(0 if OK else 1)
