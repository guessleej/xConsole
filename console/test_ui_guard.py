#!/usr/bin/env python3
"""前端守衛測試：純掃原始碼，防兩個踩過的坑回來。

1. 含輸入欄位的彈窗不准點背景關閉（欄位內拖曳選字、滑鼠在窗外放開，整張表單就蒸發）。
2. Enter 送出要擋中文輸入法組字（注音選字的 Enter 不能被當成送出）。
3. 自動更新不准讓畫面抖：定時重畫不播進場動畫、不整頁清空重建（2026-10-22 模型庫每 3 秒、總覽每 15 秒閃一次）。
跑法：python3 test_ui_guard.py（不需要任何套件）
"""
import os
import re
import sys

JS = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "app.js"), encoding="utf-8").read()
OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


# ---- 1. 彈窗
m = re.search(r"function modal\(\{[^}]*\}\)\s*\{(.*?)\n\}", JS, re.S)
check("modal() 存在", m is not None)
if m:
    body = m.group(1)
    click = re.search(r"(.{0,80})overlay\.addEventListener\(\"click\"", body, re.S)
    check("點背景關閉只在沒有輸入欄位時才掛上", click is not None and "!hasInput" in click.group(1),
          click.group(0) if click else "找不到背景點擊的處理")

bad = []
for call in re.finditer(r"\bmodal\(\{", JS):
    start = call.start()
    depth, i = 0, call.end() - 1
    while i < len(JS):
        if JS[i] == "{":
            depth += 1
        elif JS[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    args = JS[start:i]
    before = JS[max(0, start - 1500):start]
    fn_start = before.rfind("\nfunction ")
    before = before[fn_start:] if fn_start >= 0 else before
    builds_input = bool(re.search(r'el\("(input|textarea|select)"\)', args + before))
    if builds_input and "hasInput: true" not in args:
        line = JS[:start].count("\n") + 1
        bad.append(f"第 {line} 行")
check("每個帶輸入欄位的彈窗都設了 hasInput: true", not bad, "、".join(bad))

# ---- 2. 輸入法
g = re.search(r"function imeGuard\(.*?\n\}", JS, re.S)
check("imeGuard() 三重防護都在", g is not None and all(x in g.group(0) for x in
                                                   ("compositionstart", "isComposing", "keyCode === 229")))
raw = [JS[:mm.start()].count("\n") + 1 for mm in re.finditer(r'e\.key\s*===\s*"Enter"', JS)
       if not re.search(r"function imeGuard", JS[max(0, mm.start() - 400):mm.start()])]
check("沒有繞過 imeGuard 自己判斷 Enter 的地方", not raw, f"第 {raw} 行")

# ---- 3. 自動更新不准讓畫面抖
rf = re.search(r"async function refresh\(\)\s*\{(.*?)\n\}", JS, re.S)
check("自動更新走 quietRender()，不直接整頁 render()", rf is not None and "quietRender()" in rf.group(1)
      and not re.search(r"\belse render\(\)", rf.group(1)))
rv = re.search(r"function reveal\(root\)\s*\{(.*?)\n\}", JS, re.S)
check("reveal() 在自動更新時不播進場動畫", rv is not None and "S.quiet" in rv.group(1))
timers = []
for mm in re.finditer(r"set(?:Timeout|Interval)\(", JS):
    seg = JS[mm.end():mm.end() + 200].split("\n")[0]       # 單行計時器：直接在回呼裡整頁重畫就是當初的寫法
    if re.search(r"\bview[A-Z]\w*\(|\brender\(\)", seg):
        timers.append(JS[:mm.start()].count("\n") + 1)
check("計時器回呼裡沒有直接整頁重畫（只更新有變的那一塊）", not timers, f"第 {timers} 行")

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
