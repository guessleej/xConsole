# -*- coding: utf-8 -*-
"""xConsole Sales Kit 的共用圖組（五個版本共用同一套圖）。

簡報級圖照 xcloud-visio-diagram／xcloud-arch-diagram：藍色分層、只畫場景與能力，不畫元件、協定與呼叫箭頭。
使用者操作流程圖照 user-flow-diagram：圖示節點、由左到右、編號箭頭、四色流向、虛線框圈角色。

    python diagrams.py            # 產出到 data/手冊/Sales Kit/圖/
    python diagrams.py --shots    # 另外重裁真實系統截圖（原始檔在 圖/截圖/原始/）
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                       # code/
OUT = ROOT / "手冊" / "Sales Kit" / "圖"
SHOT_RAW = OUT / "截圖" / "原始"
SHOT_OUT = OUT / "截圖"

sys.path.insert(0, os.path.expanduser("~/.claude/skills/xcloud-visio-diagram"))
sys.path.insert(0, os.path.expanduser("~/.claude/skills/arch-diagram-style"))
from vdiagram import (rect, icon, vcol, save, save_layers, STEP, NAVY, SOLID, LABEL, PALE, PALE2, CORE,  # noqa: E402
                      BODY, MUTED, RED)
from arch_diagram import text_width  # noqa: E402
import re  # noqa: E402
import vdiagram as _vd  # noqa: E402

_CJK = r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]"


def nz(s):
    """中文與英數之間不留空白（使用者規定）。"""
    s = re.sub(rf"({_CJK}) +([A-Za-z0-9])", r"\1\2", str(s))
    return re.sub(rf"([A-Za-z0-9%]) +({_CJK})", r"\1\2", s)


def t(x, y, s, *a, **k):
    return _vd.t(x, y, nz(s), *a, **k)

PROBLEMS = []


def fit(label, size, width, where):
    """文字寬度超過容器就記下來，最後一起列出（不靠目視）。"""
    if text_width(label, size) > width:
        PROBLEMS.append(f"{where}：「{label}」約 {text_width(label, size):.0f}px 超過 {width:.0f}px")


def pill(x, y, label, size=24, h=50, fill="#FFFFFF", color=NAVY):
    """白底膠囊（無描邊，靠色底對比分層）。回傳 (parts, 寬度)。"""
    w = text_width(label, size) + 48
    return [rect(x, y, w, h, fill, h // 2), t(x + w / 2, y + h / 2 + size * 0.36, label, size, color, 700, "middle")], w


def pill_row(p, y, labels, x0, x1, size=24, h=50, gap=18):
    ws = [text_width(s, size) + 48 for s in labels]
    total = sum(ws) + gap * (len(ws) - 1)
    if total > x1 - x0:
        PROBLEMS.append(f"膠囊列寬 {total:.0f} 超過 {x1 - x0:.0f}：{labels}")
    x = x0 + (x1 - x0 - total) / 2
    for s in labels:
        parts, w = pill(x, y, s, size, h)
        p += parts
        x += w + gap


def band_label(p, x, y, h, title, sub, color=NAVY, subc=BODY):
    fit(title, 30, 220, "層名")
    fit(sub, 21, 220, "層說明")
    p.append(t(x + 26, y + h / 2 - 4, title, 30, color, 700))
    p.append(t(x + 26, y + h / 2 + 32, sub, 21, subc))


# 圖一：一頁看懂（四層精簡圖）
def overview():
    W, H = 1600, 760
    X, CX, R = 20, 270, 1430
    p = []
    y, h = 10, 150
    p.append(rect(X, y, R - X + 10, h, SOLID, 18))
    band_label(p, X, y, h, "誰在用", "單位內部", "#FFFFFF", "#D5E3F3")
    who = [("people", "寫程式的同仁"), ("desk", "研究團隊"), ("shield", "資訊室"), ("doc", "主管"), ("search", "稽核")]
    n = len(who); gw = (R - CX - (n - 1) * 12) / n
    for i, (ic, lab) in enumerate(who):
        cx = CX + i * (gw + 12)
        p.append(rect(cx, y + 16, gw, h - 32, "#2B6CB5", 12))
        p.append(icon(ic, cx + gw / 2 - 20, y + 28, 40))
        fit(lab, 24, gw - 20, "誰在用")
        p.append(t(cx + gw / 2, y + 110, lab, 24, "#FFFFFF", 700, "middle"))
    p.append(STEP)
    y, h = 174, 150
    p.append(rect(X, y, R - X + 10, h, CORE, 18))
    band_label(p, X, y, h, "模型主控台", "一個網頁管全部")
    p.append(t((CX + R) / 2, y + 56, "xConsole　一台也管，十台也管", 30, NAVY, 700, "middle"))
    pill_row(p, y + 82, ["載入前先試算", "網頁上換模型", "IDE一頁串好", "每月使用報告"], CX, R, 23, 48)
    p.append(STEP)
    y, h = 338, 262
    p.append(rect(X, y, R - X + 10, h, PALE, 18))
    band_label(p, X, y, h, "能做什麼", "從載入到報告")
    caps = [("meter", "記憶體准入", "放不下就擋"), ("seat", "角色欄位", "誰擔任什麼一眼看"),
            ("cube", "換模型", "兩種引擎同一畫面"), ("rack", "模型庫", "下載、放入、刪除"),
            ("plug", "API 串接", "8 種工具設定範例"), ("shield", "HTTPS 憑證", "網頁上自己換"),
            ("doc", "使用報告", "給主管的月報"), ("clock", "操作紀錄", "誰做了什麼都留")]
    cw = (R - CX - 3 * 14) / 4; ch = (h - 16 * 2 - 14) / 2
    for i, (ic, tt, d) in enumerate(caps):
        cx = CX + (i % 4) * (cw + 14); cy = y + 16 + (i // 4) * (ch + 14)
        p.append(rect(cx, cy, cw, ch, "#FFFFFF", 12))
        p.append(icon(ic, cx + 18, cy + 22, 38, LABEL))
        fit(tt, 25, cw - 90, "能力卡標")
        fit(d, 20, cw - 90, "能力卡說明")
        p.append(t(cx + 72, cy + 48, tt, 25, NAVY, 700))
        p.append(t(cx + 72, cy + 86, d, 20, BODY))
    p.append(STEP)
    y, h = 614, 136
    p.append(rect(X, y, R - X + 10, h, PALE2, 18))
    band_label(p, X, y, h, "跑在哪裡", "單位自己的AI機")
    pill_row(p, y + 44, ["AMD AI 395", "Jetson／MIC-743", "DGX Spark", "多卡伺服器", "資料不出內網"], CX, R, 22, 48, 14)
    p = vcol(1462, 10, 118, 740, [["使", "用"], ["主", "控"], ["能", "力"], ["機", "器"]]) + p
    save_layers(str(OUT / "fig-overview"), W, H, p)


# 圖二：以前 vs 裝了 xConsole（前後對比）
def contrast():
    W, H = 1500, 760
    p = []

    def col(x, fill, head, items, color, ic_color, sub_c):
        p.append(rect(x, 10, 650, 520, fill, 18))
        fit(head, 29, 600, "對比欄標")
        p.append(t(x + 32, 62, head, 29, color, 700))
        for i, (ic, tt, sub) in enumerate(items):
            y = 92 + i * 144
            p.append(rect(x + 28, y, 594, 128, "#FFFFFF", 12))
            p.append(icon(ic, x + 52, y + 38, 50, ic_color))
            fit(tt, 27, 460, "對比卡標"); fit(sub, 21, 460, "對比卡說明")
            p.append(t(x + 128, y + 54, tt, 27, color, 700))
            p.append(t(x + 128, y + 96, sub, 21, sub_c))

    col(20, "#E6E8EB", "以前：靠一兩位會指令的工程師",
        [("meter", "開下去才知道夠不夠", "吃爆就整台卡死，到機房重開"),
         ("rack", "換模型要連進機器", "改設定檔、重啟，失敗就停擺"),
         ("doc", "狀態記在個人筆記", "哪台跑什麼、網址多少要問人")], "#4A5561", MUTED, MUTED)
    p.append(STEP)
    p.append(f'<path d="M690 220 H740 V180 L810 270 L740 360 V320 H690 Z" fill="{SOLID}"/>')
    col(830, "#DCE8F4", "xConsole：一個網頁收起來",
        [("shield", "載入前先試算", "最壞剩多少不夠，就擋下來"),
         ("cube", "網頁上換模型", "起不來自動換回原本那顆"),
         ("plug", "串接資訊一頁看完", "範例直接複製，按一下實測")], NAVY, LABEL, BODY)
    p.append(STEP)
    p.append(rect(20, 550, 1460, 200, "#FFFFFF", 18))
    stats = [("214", "項", "自動化測試全數通過", NAVY), ("52", "項", "攻擊式安全測試全數通過", RED),
             ("3", "種", "記憶體架構分開算", NAVY), ("2", "種", "推論引擎同一個畫面", NAVY)]
    for i, (n, u, lab, c) in enumerate(stats):
        cx = 200 + i * 365
        if i:
            p.append(f'<line x1="{cx-182}" y1="584" x2="{cx-182}" y2="716" stroke="#C6D4E2" stroke-width="2"/>')
        p.append(f'<text x="{cx}" y="664" text-anchor="middle"><tspan font-size="68" font-weight="800" fill="{c}">{n}</tspan>'
                 f'<tspan font-size="26" fill="{MUTED}" dx="8">{u}</tspan></text>')
        fit(lab, 22, 340, "數字帶說明")
        p.append(t(cx, 712, lab, 22, MUTED, 400, "middle"))
    save_layers(str(OUT / "fig-contrast"), W, H, p)


# 圖三：三種記憶體架構分開算
def memory():
    W, H = 1600, 760
    X, CX, R = 20, 270, 1430
    p = []
    rows = [
        ("AMD AI 395", "GPU與系統切兩塊", SOLID, "#FFFFFF", "#D5E3F3",
         ["96 GiB 給 GPU", "系統只剩 31 GiB", "會拖死整台的是系統這塊", "系統底線 15 GiB"]),
        ("Jetson／DGX", "單一池子共用", CORE, NAVY, BODY,
         ["GPU 與系統搶同一塊", "合併計算，不分開說都夠", "Thor 122 GiB", "底線 12 GiB"]),
        ("多卡伺服器", "每張卡各自算", PALE, NAVY, BODY,
         ["每張顯示卡各自的顯存", "系統記憶體另外算", "顯存底線 1 GiB", "系統底線 8 GiB"]),
    ]
    y = 10
    for i, (name, sub, fill, c1, c2, pills) in enumerate(rows):
        h = 168
        p.append(rect(X, y, R - X + 10, h, fill, 18))
        band_label(p, X, y, h, name, sub, c1, c2)
        ws = [text_width(s, 22) + 48 for s in pills]
        x = CX
        for k, s_ in enumerate(pills):
            hot = k == len(pills) - 1
            p.append(rect(x, y + 60, ws[k], 50, "#BF181F" if (hot and i == 0) else "#FFFFFF", 25))
            p.append(t(x + ws[k] / 2, y + 60 + 25 + 8, s_, 22, "#FFFFFF" if (hot and i == 0) else NAVY, 700, "middle"))
            x += ws[k] + 16
        if x - 16 > R:
            PROBLEMS.append(f"記憶體架構列寬超過：{name}")
        p.append(STEP)
        y += h + 14
    h = 750 - y
    p.append(rect(X, y, R - X + 10, h, PALE2, 18))
    band_label(p, X, y, h, "每次都算", "啟動、換模型前")
    p.append(t((CX + R) / 2, y + h / 2 + 10, "載下去之後最壞剩多少，低於底線就擋，並說明差多少", 26, NAVY, 700, "middle"))
    p = vcol(1462, 10, 118, 740, [["切", "兩", "塊"], ["單", "一", "池"], ["多", "卡"], ["試", "算"]]) + p
    save_layers(str(OUT / "fig-memory"), W, H, p)


# 圖四：使用者操作流程（user-flow-diagram 畫法）
def user_flow():
    from arch_diagram import Diagram, BLUE, GREEN, VIOLET, ORANGE
    from msicons import ms_icon
    W, H = 1400, 780
    d = Diagram(width=W, height=H)
    RD, INK, GR = "#bf181f", "#16202a", "#64748b"
    BC, GC, VC, OC = BLUE.main, GREEN.main, VIOLET.main, ORANGE.main
    T, N, S_, A = 25, 20, 18, 20
    hue = {BC: BLUE, GC: GREEN, VC: VIOLET, OC: ORANGE}

    _text = d.text
    d.text = lambda x, y, s, *a, **k: _text(x, y, nz(s), *a, **k)

    def node(x, y, ic, color, title, name, size=80, lines=()):
        d.parts.append(ms_icon(ic, x - size / 2, y - size / 2, size, color))
        if title:
            d.text(x, y - size / 2 - 14, title, size=T, fill=RD, bold=True, anchor="middle")
        if name:
            d.text(x, y + size / 2 + 26, name, size=N, fill=INK, bold=True, anchor="middle")
        for i, ln in enumerate(lines):
            d.text(x, y + size / 2 + 50 + i * 21, ln, size=S_, fill=GR, anchor="middle")

    def arrow(path, color, label="", lx=0, ly=0, anchor="middle"):
        d.arrow(0, 0, 0, 0, hue[color], path=path)
        for i, ln in enumerate(label.split("\n") if label else []):
            d.text(lx, ly + i * (A + 4), ln, size=A, fill=INK, bold=True, anchor=anchor)

    def dashed_box(x, y, w, h, label):
        d.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="none" stroke="#94a3b8" '
                       f'stroke-width="2" stroke-dasharray="10 6"/>')
        d.text(x + w - 14, y + h - 14, label, size=T, fill=RD, bold=True, anchor="end")

    # 放映時依序出現：主控台 → 管理者操作 → 機器端 → 同仁使用 → 取樣與月報
    S = d.step
    node(700, 400, "Management console", VC, "", "", size=100)
    d.text(772, 340, "xConsole 主控台", size=T, fill=RD, bold=True)
    d.text(640, 476, "先算再載入", size=N, fill=INK, bold=True, anchor="end")
    d.text(640, 501, "全程 HTTPS", size=S_, fill=GR, anchor="end")
    d.text(640, 524, "動作全程留紀錄", size=S_, fill=GR, anchor="end")
    lx = 770
    for h_, lab in [(GREEN, "資料進來"), (BLUE, "使用者操作"), (ORANGE, "管理者設定"), (VIOLET, "平台輸出")]:
        d.parts.append(f'<rect x="{lx}" y="{H-36}" width="20" height="20" rx="5" fill="{h_.main}"/>')
        d.text(lx + 30, H - 20, lab, size=A, fill="#334155")
        lx += 30 + len(lab) * A + 26
    S()
    dashed_box(470, 16, 660, 212, "管理者")
    node(680, 96, "User -Enterprise", OC, "資訊室", "主控台管理者", size=72)
    node(960, 96, "Laptop", OC, "老師或助教", "也可以是管理者", size=72)
    arrow("M680 176 V 346", OC, "1.看各機記憶體\n換模型、下載模型", 664, 250, anchor="end")
    arrow("M960 176 V 300 H 732 V 346", OC, "2.開金鑰、調上下文", 976, 262, anchor="start")
    S()
    # AI 機的說明放右側：正下方要留給第 5 條線進來
    d.parts.append(ms_icon("Server farm", 1100 - 40, 400 - 40, 80, VC))
    d.text(1100, 346, "AI 機", size=T, fill=RD, bold=True, anchor="middle")
    d.text(1154, 396, "AI 395、Jetson", size=N, fill=INK, bold=True)
    d.text(1154, 421, "DGX Spark", size=N, fill=INK, bold=True)
    d.text(1154, 446, "失敗自動換回", size=S_, fill=GR)
    arrow("M754 384 H 1052", VC, "3.先試算，再載入並驗證", 903, 368)
    arrow("M1052 424 H 754", GC, "6.每 5 分鐘取樣", 903, 456)
    S()
    node(110, 400, "Laptop", BC, "寫程式的同仁", "Copilot、OpenCode", size=80)
    arrow("M170 400 H 646", BC, "4.照 API 串接頁的範例設定", 408, 382)
    arrow("M110 486 V 716 H 1100 V 448", BC, "5.用 IDE 直接打自家模型", 400, 698)
    S()
    node(700, 612, "Reports", VC, "", "主管", size=64, lines=("服務使用報告",))
    arrow("M700 452 V 576", VC, "7.每月報告與操作紀錄", 716, 522, anchor="start")
    d.save_layers(str(OUT / "fig-userflow.svg"), zoom=3)


# 真實系統截圖（測試前已在正式環境以元素座標截取，這裡只做縮放與複製）
SHOTS = ["node27-slots", "node27-mem", "node20-slots", "swap", "lib-pair", "lib-steps", "api-test", "api-info",
         "apis", "tls-steps", "report", "overview"]


# 要放在故事頁右欄的截圖先裁成直一點的比例（寬高比 >2.2 會被排成整列，配上文字後圖會太小）
CROPS = {
    "api-test": (0, 0, 1416, 1098),          # 左半：實測結果
    "node20-slots": (0, 0, 2124, 1246),      # 左邊三欄角色
    "tls-steps": (0, 516, 1400, 1196),       # 目前使用的憑證
    "overview": (0, 0, 1430, 1220),          # 主視覺＋第一台機器
}


def shots():
    from PIL import Image
    SHOT_OUT.mkdir(parents=True, exist_ok=True)
    for n in SHOTS:
        im = Image.open(SHOT_RAW / f"{n}.png").convert("RGB")
        if n in CROPS:
            im = im.crop(CROPS[n])
        if im.width > 2200:
            im = im.resize((2200, round(im.height * 2200 / im.width)), Image.LANCZOS)
        im.save(SHOT_OUT / f"shot-{n}.png")
        print("截圖", n, im.size)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    shots()
    overview(); contrast(); memory(); user_flow()
    if PROBLEMS:
        raise SystemExit("圖內文字溢出：\n" + "\n".join(PROBLEMS))
    print("圖組完成：", OUT)
