# -*- coding: utf-8 -*-
"""xConsole 技術長版簡報的圖組（Visio 風藍色簡報圖，照 xcloud-visio-diagram 簡報版規格）。

只畫能力與場景，不畫元件名稱、協定與呼叫箭頭；唯一的箭頭是階段流程的時間順序。
元件名稱、版本、埠號一律寫在投影片的文字頁與規格表。

畫布 1600×620：放進投影片約 12 吋寬，最小字 23px 換算約 12.4pt（使用者規定字級不低於 12pt）。
全景圖與記憶體架構圖的內容沿用 Sales Kit，但依這個比例與字級重畫，不直接縮 Sales Kit 那張。
每一層之間插 STEP，save_layers() 另出分層 PNG，簡報放映時一層一層出現。

    python diagrams_tech.py          # 產出到 手冊/Sales Kit/技術長版/圖/
"""
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                                   # code/
OUT = ROOT / "手冊" / "Sales Kit" / "技術長版" / "圖"

sys.path.insert(0, os.path.expanduser("~/.claude/skills/xcloud-visio-diagram"))
sys.path.insert(0, os.path.expanduser("~/.claude/skills/arch-diagram-style"))
import vdiagram as _vd  # noqa: E402
from vdiagram import rect, icon, save_layers, STEP, NAVY, SOLID, LABEL, PALE, PALE2, CORE, BODY, LINE, RED  # noqa: E402
from arch_diagram import text_width  # noqa: E402

_CJK = r"[㐀-鿿　-〿＀-￯]"
PROBLEMS = []
W, H = 1600, 620
X, CX, R = 20, 270, 1440          # 左欄層名、卡片起點、層帶右緣（右側留給縱向層別欄）
TB, SB, CT, CD, PS = 32, 23, 29, 24, 25      # 層名、層說明、卡標、卡內文、膠囊字級（px）
MINPX = 23


def nz(s):
    """中文與英數之間不留空白。"""
    s = re.sub(rf"({_CJK}) +([A-Za-z0-9])", r"\1\2", str(s))
    return re.sub(rf"([A-Za-z0-9%]) +({_CJK})", r"\1\2", s)


def t(x, y, s, size, *a, **k):
    if size < MINPX:
        PROBLEMS.append(f"字級 {size}px 低於 {MINPX}px：{s}")
    return _vd.t(x, y, nz(s), size, *a, **k)


def fit(label, size, width, where):
    if text_width(nz(label), size) > width:
        PROBLEMS.append(f"{where}：「{label}」約 {text_width(nz(label), size):.0f}px 超過 {width:.0f}px")


def vcol(x, y, w, h, groups):
    """右側縱向層別欄（字級放大版，vdiagram 原版 20px 放進投影片會低於 12pt）。"""
    out = [rect(x, y, w, h, PALE2, 16)]
    gh = h / len(groups)
    for i, g in enumerate(groups):
        cy = y + gh * i + gh / 2 - (len(g) - 1) * 15
        for k, ch in enumerate(g):
            out.append(t(x + w / 2, cy + k * 30 + 9, ch, 25, NAVY, 700, "middle"))
        if i:
            out.append(f'<line x1="{x+14}" y1="{y+gh*i}" x2="{x+w-14}" y2="{y+gh*i}" stroke="{LINE}" stroke-width="1.6"/>')
    return out


def pill(x, y, label, size=PS, h=52, fill="#FFFFFF", color=NAVY):
    w = text_width(nz(label), size) + 48
    return [rect(x, y, w, h, fill, h // 2), t(x + w / 2, y + h / 2 + size * 0.36, label, size, color, 700, "middle")], w


def pill_row(p, y, items, x0, x1, size=PS, h=52, gap=16):
    """items：字串，或 (字串, 'red') 表示紅底白字的重點膠囊。"""
    norm = [(i, None) if isinstance(i, str) else i for i in items]
    ws = [text_width(nz(s), size) + 48 for s, _ in norm]
    total = sum(ws) + gap * (len(ws) - 1)
    if total > x1 - x0:
        PROBLEMS.append(f"膠囊列寬 {total:.0f} 超過 {x1 - x0:.0f}：{[s for s, _ in norm]}")
    x = x0 + (x1 - x0 - total) / 2
    for s, kind in norm:
        parts, w = pill(x, y, s, size, h, RED if kind == "red" else "#FFFFFF", "#FFFFFF" if kind == "red" else NAVY)
        p += parts
        x += w + gap


def band(p, y, h, fill, title, sub, solid=False):
    p.append(rect(X, y, R - X + 10, h, fill, 18))
    fit(title, TB, CX - X - 30, "層名")
    fit(sub, SB, CX - X - 30, "層說明")
    p.append(t(X + 24, y + h / 2 - 2, title, TB, "#FFFFFF" if solid else NAVY, 700))
    p.append(t(X + 24, y + h / 2 + 34, sub, SB, "#D5E3F3" if solid else BODY))


def layered(name, p, groups):
    p = vcol(1462, 10, 128, H - 20, groups) + p
    save_layers(str(OUT / name), W, H, p)


# 系統全景（內容同 Sales Kit 的 fig-overview）
def overview():
    p = []
    y, h = 10, 132
    band(p, y, h, SOLID, "誰在用", "單位內部", solid=True)
    who = [("people", "寫程式的同仁"), ("desk", "研究團隊"), ("shield", "資訊室"), ("doc", "主管"), ("search", "稽核")]
    gw = (R - CX - 4 * 12) / 5
    for i, (ic, lab) in enumerate(who):
        cx = CX + i * (gw + 12)
        p.append(rect(cx, y + 14, gw, h - 28, "#2B6CB5", 12))
        p.append(icon(ic, cx + gw / 2 - 19, y + 24, 38))
        fit(lab, 26, gw - 20, "誰在用")
        p.append(t(cx + gw / 2, y + 100, lab, 26, "#FFFFFF", 700, "middle"))
    p.append(STEP)
    y, h = 152, 136
    band(p, y, h, CORE, "主控台", "一個網頁管全部")
    p.append(t((CX + R) / 2, y + 48, "xConsole　一台也管，十台也管", 30, NAVY, 700, "middle"))
    pill_row(p, y + 68, ["載入前先試算", "網頁上換模型", "IDE一頁串好", "每月使用報告"], CX, R, PS, 52)
    p.append(STEP)
    y, h = 298, 202
    band(p, y, h, PALE, "能做什麼", "從載入到報告")
    caps = [("meter", "記憶體准入", "放不下就擋"), ("seat", "角色欄位", "一眼看缺什麼"),
            ("cube", "換模型", "兩種引擎"), ("rack", "模型庫", "下載與刪除"),
            ("plug", "API串接", "8種範例"), ("shield", "HTTPS憑證", "網頁上換"),
            ("doc", "使用報告", "給主管月報"), ("clock", "操作紀錄", "保存400天")]
    cw = (R - CX - 3 * 12) / 4; ch = (h - 12 * 2 - 10) / 2
    for i, (ic, tt, dd) in enumerate(caps):
        cx = CX + (i % 4) * (cw + 12); cy = y + 12 + (i // 4) * (ch + 10)
        p.append(rect(cx, cy, cw, ch, "#FFFFFF", 12))
        p.append(icon(ic, cx + 16, cy + 22, 36, LABEL))
        fit(tt, 27, cw - 76, "能力卡標"); fit(dd, 23, cw - 76, "能力卡說明")
        p.append(t(cx + 64, cy + 37, tt, 27, NAVY, 700))
        p.append(t(cx + 64, cy + 71, dd, 23, BODY))
    p.append(STEP)
    y, h = 510, 100
    band(p, y, h, PALE2, "跑在哪裡", "單位自己的AI機")
    pill_row(p, y + 24, ["AMD AI 395", "Jetson／MIC-743", "DGX Spark", "多卡伺服器", "資料不出內網"], CX, R, 24, 52, 12)
    layered("fig-overview", p, [["使", "用"], ["主", "控"], ["能", "力"], ["機", "器"]])


# 部署型態：單機與多機
def deploy():
    p = []
    y, h = 10, 170
    band(p, y, h, SOLID, "單機模式", "賣一台時附上", solid=True)
    cards = [("rack", "同一台AI機", "主控台與節點代理"), ("shield", "第一次開機", "就是HTTPS"), ("people", "開瀏覽器就用", "不必另備管理機")]
    gw = (R - CX - 2 * 14) / 3
    for i, (ic, tt, dd) in enumerate(cards):
        cx = CX + i * (gw + 14)
        p.append(rect(cx, y + 14, gw, h - 28, "#2B6CB5", 12))
        p.append(icon(ic, cx + 22, y + 40, 40))
        fit(tt, CT, gw - 100, "單機卡標"); fit(dd, CD, gw - 100, "單機卡說明")
        p.append(t(cx + 82, y + 72, tt, CT, "#FFFFFF", 700))
        p.append(t(cx + 82, y + 112, dd, CD, "#D5E3F3"))
    p.append(STEP)
    y, h = 190, 200
    band(p, y, h, CORE, "多機模式", "機器變多時")
    roles = [("desk", "主控台", "選一台裝"), ("rack", "節點代理", "每台各裝一個"),
             ("shield", "互相驗證", "各機憑證經SSH取回"), ("clock", "每5分鐘", "取樣各機狀態")]
    cw = (R - CX - 3 * 14) / 4
    for i, (ic, tt, a) in enumerate(roles):
        cx = CX + i * (cw + 14)
        p.append(rect(cx, y + 14, cw, h - 28, "#FFFFFF", 12))
        p.append(icon(ic, cx + 20, y + 34, 38, LABEL))
        fit(tt, CT, cw - 84, "角色名"); fit(a, CD, cw - 40, "角色說明")
        p.append(t(cx + 70, y + 64, tt, CT, NAVY, 700))
        p.append(t(cx + 22, y + 126, a, CD, BODY))
    p.append(STEP)
    y, h = 400, 100
    band(p, y, h, PALE, "擴充", "不必重做")
    pill_row(p, y + 24, [("各機設定不用重做", "red"), "加一台＝裝代理再納管", "總覽一頁看完"], CX, R)
    p.append(STEP)
    y, h = 510, 100
    band(p, y, h, PALE2, "資源上限", "不壓垮機器")
    pill_row(p, y + 24, ["主控台記憶體512 MiB", "節點代理256 MiB", "CPU使用量設上限"], CX, R)
    layered("fig-deploy", p, [["單", "機"], ["多", "機"], ["擴", "充"], ["上", "限"]])


# 三種記憶體架構（內容同 Sales Kit 的 fig-memory）
def memory():
    p = []
    rows = [("AMD AI 395", "切兩塊", SOLID, True, [("96 GiB給GPU", None), ("系統只剩31 GiB", None), ("系統底線15 GiB", "red")]),
            ("Jetson／DGX", "單一池", CORE, False, [("GPU與系統搶同一塊", None), ("合併計算", None), ("底線12 GiB", None)]),
            ("多卡伺服器", "每卡各自算", PALE, False, [("每張卡的顯存", None), ("系統記憶體另算", None), ("顯存1／系統8 GiB", None)])]
    y = 10
    for name, sub, fill, solid, items in rows:
        h = 140
        band(p, y, h, fill, name, sub, solid=solid)
        pill_row(p, y + 44, items, CX, R)
        p.append(STEP)
        y += h + 10
    h = H - 10 - y
    band(p, y, h, PALE2, "每次都算", "啟動與換模型前")
    p.append(t((CX + R) / 2, y + h / 2 + 10, "載下去之後最壞剩多少，低於底線就擋", 28, NAVY, 700, "middle"))
    layered("fig-memory", p, [["切", "兩", "塊"], ["單", "一", "池"], ["多", "卡"], ["試", "算"]])


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    overview(); deploy(); memory()
    if PROBLEMS:
        raise SystemExit("圖內文字問題：\n" + "\n".join(PROBLEMS))
    print("技術長版圖組完成：", OUT)
