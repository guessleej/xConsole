# -*- coding: utf-8 -*-
"""xConsole 模型主控台 Sales Kit 產生器（通用版＋一般企業、政府機關、學校、醫院四個產業版）。

    python build_saleskit.py --industry all          # 重產圖組＋四版 pptx 與 pdf＋總覽圖
    python build_saleskit.py --industry 醫院 --no-pdf
    python build_saleskit.py --industry all --skip-diagrams

骨架照 saleskit-method（xHangar 定稿範本）：封面 → 問題場景 → 自有機群紀錄 → 宣言 → 全貌與對比圖 → 問題解決頁×N
（穿插圖與真實畫面）→ 證據 → 安全做法 → 產業重點 → 部署方式 → 導入前後 → 為什麼是云碩 → 導入流程 → Thank You。
每頁都有業務講稿。頁序與共用頁在 pages.py，產業文案在 content.py，圖組在 diagrams.py。
執行要用有 python-pptx 的 Python（本機是 /opt/homebrew/bin/python3.14）。
"""
import argparse
import math
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches as I, Pt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SKILLS = Path(os.path.expanduser("~/.claude/skills"))
_need = [SKILLS / "xcloud-deck-build/assets/xcloud-template.pptx", SKILLS / "xcloud-deck-build/assets/pptx_anim.py",
         SKILLS / "arch-diagram-style/msicon_png.py", SKILLS / "xcloud-visio-diagram"]
_miss = [str(x) for x in _need if not x.exists()]
if _miss:  # 內部素材不在公開 repo，見 手冊/Sales Kit/README.md「前置條件」
    sys.exit("缺少云碩內部簡報素材，無法建置：\n  " + "\n  ".join(_miss))
sys.path.insert(0, os.path.expanduser("~/.claude/skills/arch-diagram-style"))
from content import INDUSTRIES  # noqa: E402
from msicon_png import icon_png  # noqa: E402
sys.path.insert(0, os.path.expanduser("~/.claude/skills/xcloud-deck-build/assets"))
from pptx_anim import add_layers, animate_slide, check_timing  # noqa: E402

ROOT = HERE.parents[1]                       # code/
KIT = ROOT / "手冊" / "Sales Kit"
FIG = KIT / "圖"
SHOT = FIG / "截圖"
QA = KIT / "QA"
TPL = Path(os.path.expanduser("~/.claude/skills/xcloud-deck-build/assets/xcloud-template.pptx"))
SOFFICE = "/opt/homebrew/bin/soffice"
FONT = "Microsoft JhengHei"
DATE = "2026年10月"

RED = RGBColor(0xBF, 0x18, 0x1F); RED_D = RGBColor(0x9C, 0x12, 0x18); RTINT = RGBColor(0xFB, 0xEB, 0xEC)
CHAR = RGBColor(0x2D, 0x2D, 0x2D); SLATE = RGBColor(0x4A, 0x4A, 0x4A); GREY = RGBColor(0x6E, 0x6E, 0x6E)
LGREY = RGBColor(0xF1, 0xF2, 0xF4); WHITE = RGBColor(0xFF, 0xFF, 0xFF); MID = RGBColor(0xE4, 0xE6, 0xEA)

_CJK = r"[㐀-鿿　-〿＀-￯]"
PROBLEMS = []


def nz(s):
    """中文與英數之間不留空白（使用者規定）。"""
    s = re.sub(rf"({_CJK}) +([A-Za-z0-9])", r"\1\2", str(s))
    return re.sub(rf"([A-Za-z0-9%]) +({_CJK})", r"\1\2", s)


def text_in(s, size):
    """估算字串寬度（吋）：全形一字寬，半形約 0.55 字寬。"""
    w = 0.0
    for ch in s:
        o = ord(ch)
        if ch == " ":
            w += 0.3
        elif 0x2E80 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFF60:
            w += 1.0
        else:
            w += 0.56
    return w * size / 72 * 1.06      # LibreOffice／PowerPoint 實際斷行比估算寬一點，留餘裕


def need_height(paras, size, width, sp):
    lines = sum(max(1, math.ceil(text_in(p, size) / max(width, 0.1) - 0.02)) for p in paras)
    return lines * size * sp * 1.2 / 72


class Deck:
    def __init__(self):
        self.prs = Presentation(str(TPL))
        for sid in list(self.prs.slides._sldIdLst):
            try:
                self.prs.part.drop_rel(sid.get(qn("r:id")))
            except Exception:
                pass
            self.prs.slides._sldIdLst.remove(sid)
        self.lay = {l.name: l for l in self.prs.slide_layouts}
        self.icons = {}

    # 基本元件 
    def add(self, name):
        s = self.prs.slides.add_slide(self.lay[name])
        for ph in list(s.placeholders):
            ph._element.getparent().remove(ph._element)
        return s

    @staticmethod
    def _font(run, size, bold, color):
        run.font.size = Pt(size); run.font.bold = bold; run.font.color.rgb = color; run.font.name = FONT
        rPr = run._r.get_or_add_rPr()
        for tag in ("a:latin", "a:ea", "a:cs"):
            e = rPr.find(qn(tag))
            if e is None:
                e = rPr.makeelement(qn(tag), {}); rPr.append(e)
            e.set("typeface", FONT)

    def tx(self, s, l, t, w, h, runs, size=14, color=SLATE, bold=False, align=PP_ALIGN.LEFT,
           anchor=MSO_ANCHOR.TOP, sp=1.0, check=True):
        tb = s.shapes.add_textbox(I(l), I(t), I(w), I(h)); tf = tb.text_frame
        tf.word_wrap = True; tf.vertical_anchor = anchor
        for m in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
            setattr(tf, m, 0)
        if isinstance(runs, str):
            runs = [(runs, color, bold)]
        p = tf.paragraphs[0]; p.alignment = align; p.line_spacing = sp
        paras, cur = [], ""
        for item in runs:
            if item == "\n":
                p = tf.add_paragraph(); p.alignment = align; p.line_spacing = sp
                paras.append(cur); cur = ""; continue
            t_, c_, b_ = item if isinstance(item, tuple) else (item, color, bold)
            for k, part in enumerate(nz(t_).split("\n")):
                if k:
                    p = tf.add_paragraph(); p.alignment = align; p.line_spacing = sp
                    paras.append(cur); cur = ""
                if part:
                    r = p.add_run(); r.text = part
                    self._font(r, size, b_ if b_ is not None else bold, c_ if c_ is not None else color)
                    cur += part
        paras.append(cur)
        if check:
            need = need_height(paras, size, w, sp)
            if need > h + 0.05:
                PROBLEMS.append(f"第{len(self.prs.slides)}頁 文字可能溢出（需約{need:.2f}吋，框高{h:.2f}吋）：{''.join(paras)[:24]}")
        return tb

    def rr(self, s, l, t, w, h, fill, rad=0.06):
        sp = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, I(l), I(t), I(w), I(h))
        try:
            sp.adjustments[0] = rad
        except Exception:
            pass
        sp.fill.solid(); sp.fill.fore_color.rgb = fill
        sp.line.fill.background(); sp.shadow.inherit = False
        return sp

    def rect(self, s, l, t, w, h, fill):
        sp = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, I(l), I(t), I(w), I(h))
        sp.fill.solid(); sp.fill.fore_color.rgb = fill; sp.line.fill.background(); sp.shadow.inherit = False
        sp.name = "cover-mask"
        return sp

    def oval(self, s, l, t, d, fill):
        sp = s.shapes.add_shape(MSO_SHAPE.OVAL, I(l), I(t), I(d), I(d))
        sp.fill.solid(); sp.fill.fore_color.rgb = fill; sp.line.fill.background(); sp.shadow.inherit = False
        return sp

    def icon(self, s, name, x, y, color="#BF181F", size=0.30):
        key = (name, color)
        if key not in self.icons:
            self.icons[key] = icon_png(name, color, 128)
        s.shapes.add_picture(self.icons[key], I(x), I(y), width=I(size))

    def title(self, s, t, sub=None):
        self.tx(s, 2.5, 0.42, 10.2, 0.6, t, size=25, color=CHAR, bold=True, anchor=MSO_ANCHOR.MIDDLE).name = "xh-title"
        if sub:
            self.tx(s, 2.5, 1.02, 10.2, 0.32, sub, size=13, color=GREY).name = "xh-title"

    @staticmethod
    def notes(s, problem, line, ask, faq):
        tf = s.notes_slide.notes_text_frame
        tf.text = "這頁解客戶什麼問題：" + nz(problem)
        for label, val in (("話術金句：", line), ("可反問客戶：", ask), ("常見疑問應對：", faq)):
            p = tf.add_paragraph(); p.text = label + nz(val)

    # 版型 
    def shot(self, s, name, cap, x=6.30, y=1.72, w=6.50, h=4.45):
        from PIL import Image
        self.rr(s, x, y, w, h, LGREY, rad=0.03)
        img = SHOT / name
        iw, ih = Image.open(img).size
        dw = w - 0.3; dh = dw * ih / iw
        if dh > h - 0.66:
            dh = h - 0.66; dw = dh * iw / ih
        s.shapes.add_picture(str(img), I(x + (w - dw) / 2), I(y + 0.15), width=I(dw))
        self.tx(s, x + 0.15, y + h - 0.40, w - 0.3, 0.28, cap, size=12, color=GREY, align=PP_ALIGN.CENTER)

    def chips_row(self, s, x, y, w, chips, h=0.62):
        cw = (w - 0.2 * (len(chips) - 1)) / len(chips)
        for i, (ic, txt) in enumerate(chips):
            cx = x + i * (cw + 0.2)
            self.rr(s, cx, y, cw, h, RTINT, rad=0.12)
            big = h >= 0.75
            isz = 0.34 if big else 0.28
            self.icon(s, ic, cx + 0.18, y + (h - isz) / 2, size=isz)
            self.tx(s, cx + 0.18 + isz + 0.16, y, cw - isz - 0.5, h, txt, size=14.5 if big else 13, color=RED_D, bold=True,
                    anchor=MSO_ANCHOR.MIDDLE)

    def story(self, title, kicker, pain, after, punch, notes, shot=None, cap="", chips=None):
        """問題→解決三段式。直式截圖放右欄；寬扁截圖（寬高比>3）改放在文字下方整列。"""
        from PIL import Image
        s = self.add("內容"); self.title(s, title)
        wide = False
        if shot:
            iw, ih = Image.open(SHOT / shot).size
            wide = iw / ih > 2.2
        side = shot and not wide
        w = 5.45 if side else 11.9
        size = 13 if side else (14 if wide else 16)
        sp = 1.25 if side else (1.32 if wide else 1.42)
        y = 1.32
        self.tx(s, 0.68, y, w, 0.28, kicker, size=12, color=RED, bold=True)
        y += 0.40
        h1 = need_height([nz(pain)], size, w, sp) + 0.06
        self.tx(s, 0.68, y, w, h1, pain, size=size, color=SLATE, sp=sp)
        y += h1 + (0.14 if wide else 0.30)
        h2 = need_height([nz(after)], size, w, sp) + 0.06
        self.tx(s, 0.68, y, w, h2, after, size=size, color=CHAR, sp=sp)
        y += h2 + (0.22 if wide or side else 0.36)
        pw = w if side else 10.4
        ph = max(0.62, need_height([nz(punch)], 14, pw - 0.44, 1.1) + 0.2)
        if wide:
            # 截圖整列：框寬 10.4（避開右下 logo），高度用剩下的空間
            room = 6.80 - ph - 0.18 - y
            dw = min(10.1, (room - 0.52) * iw / ih)
            dh = dw * ih / iw
            fh = dh + 0.52
            self.rr(s, 0.68, y, 10.4, fh, LGREY, rad=0.04)
            s.shapes.add_picture(str(SHOT / shot), I(0.68 + (10.4 - dw) / 2), I(y + 0.12), width=I(dw))
            self.tx(s, 0.83, y + fh - 0.36, 10.1, 0.28, cap, size=12, color=GREY, align=PP_ALIGN.CENTER)
            if dw < 7.5:
                PROBLEMS.append(f"第{len(self.prs.slides)}頁 寬截圖只剩 {dw:.1f} 吋寬，文字要再精簡：{title}")
            y += fh + 0.18
        if chips:
            ch = 0.62 if side or wide else 0.78
            self.chips_row(s, 0.68, y, w if side else 10.4, chips, h=ch)
            y += ch + (0.20 if side or wide else 0.32)
        if y + ph > 6.81:
            PROBLEMS.append(f"第{len(self.prs.slides)}頁 故事頁內容太長，金句底到 {y + ph:.2f} 吋：{title}")
        self.rr(s, 0.68, y, pw, ph, RTINT, rad=0.10)
        self.tx(s, 0.90, y, pw - 0.44, ph, punch, size=14 if side else 15, color=RED_D, bold=True, anchor=MSO_ANCHOR.MIDDLE, sp=1.1)
        if side:
            self.shot(s, shot, cap)
        self.notes(s, *notes)
        return s

    def figure(self, title, sub, png, notes):
        from PIL import Image
        s = self.add("內容"); self.title(s, title, sub)
        iw, ih = Image.open(png).size
        ar = iw / ih; top = 1.55
        wa = min(12.0, (6.30 - top) * ar)            # 置中，底部不超過 6.30
        wb = min(10.95 - 0.60, (7.20 - top) * ar)    # 靠左，右緣不超過 10.95
        if wa >= wb:
            dw = wa; x = (13.333 - dw) / 2
        else:
            dw = wb; x = 0.60
        add_layers(s, Path(png).with_suffix(""), x, top, dw)     # 分層圖：放映時講到哪層出現哪層
        self.notes(s, *notes)
        return s


def build(ind_name, c):
    from pages import build_pages
    d = Deck()
    build_pages(d, ind_name, c, {"RED": RED, "RED_D": RED_D, "RTINT": RTINT, "CHAR": CHAR, "SLATE": SLATE,
                                 "GREY": GREY, "LGREY": LGREY, "WHITE": WHITE, "FIG": FIG, "DATE": DATE})
    for sl in d.prs.slides:
        for ph in list(sl.placeholders):
            ph._element.getparent().remove(ph._element)
    out = KIT / f"xConsole-SalesKit-{ind_name}.pptx"
    for sl in d.prs.slides:                          # 放映動畫：封面與 Thank You 不動，其餘依序進場
        if sl.slide_layout.name != "頁首":
            animate_slide(sl)
    d.prs.save(str(out))
    qa_pptx(out, d.prs)
    return out, len(d.prs.slides)


BAD_CHARS = re.compile(r"[\u2460-\u24FF\u3251-\u32BF\u2500-\u259F\u2600-\u27BF\U0001F300-\U0001FAFF]")


def qa_pptx(path, prs):
    bad = []
    W, H = I(13.333), I(7.5)
    for i, sl in enumerate(prs.slides, 1):
        boxes = []
        for sh in sl.shapes:
            if sh.left is None:
                continue
            l, t, w, h = sh.left, sh.top, sh.width or 0, sh.height or 0
            if l < 0 or t < 0 or l + w > W + I(0.01) or t + h > H + I(0.01):
                bad.append(f"S{i} 超出頁面：{sh.name}")
            is_fill = sh.shape_type == 1 and sh.fill.type == 1      # 自繪色塊
            if is_fill and h <= I(0.1) and w > I(1):
                bad.append(f"S{i} 細色條：{sh.name}")
            if is_fill and sh.name not in ("cover-mask",) and sl.slide_layout.name == "內容":
                # 右下 logo 區（約 x>11.15、y>6.40）不准有色塊
                if l + w > I(11.15) and t + h > I(6.40):
                    bad.append(f"S{i} 色塊壓到右下logo：{sh.name}")
            if sh.shape_type == 13 and sl.slide_layout.name == "內容":
                if l + w > I(11.15) and t + h > I(6.40):
                    bad.append(f"S{i} 圖片壓到右下logo")
            if is_fill and sh.name not in ("zebra", "highlight", "cover-mask"):
                boxes.append((sh.name, l, t, l + w, t + h))
        # 色塊彼此不可疊（表格的斑馬底與強調欄除外）
        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                A, B = boxes[a], boxes[b]
                if A[1] < B[3] and B[1] < A[3] and A[2] < B[4] and B[2] < A[4]:
                    inside = (A[1] <= B[1] and A[2] <= B[2] and A[3] >= B[3] and A[4] >= B[4]) or \
                             (B[1] <= A[1] and B[2] <= A[2] and B[3] >= A[3] and B[4] >= A[4])
                    if not inside or "Oval" not in (A[0] + B[0]):
                        bad.append(f"S{i} 色塊重疊：{A[0]} 與 {B[0]}")
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if not re.match(r"ppt/(slides|notesSlides)/\w+\.xml$", n):
                continue
            xml = z.read(n).decode("utf-8")
            if "/slides/" in n:
                if "<p:ph " in xml:
                    bad.append(f"{n} 殘留佔位框")
                for sz in re.findall(r'<a:rPr[^>]*\ssz="(\d+)"', xml):
                    if int(sz) < 1200:
                        bad.append(f"{n} 字級小於12：{int(sz)/100}")
            for x in re.findall(r"<a:t>([^<]*)</a:t>", xml):
                if BAD_CHARS.search(x):
                    bad.append(f"{n} 圈碼、框線或emoji：{x[:30]}")
                if "**" in x or "```" in x or re.match(r"^\s*(#|- |\* )", x):
                    bad.append(f"{n} markdown符號：{x[:30]}")
                if "\u96f2碩" in x:
                    bad.append(f"{n} 公司名寫錯：{x[:30]}")
                if re.search(rf"{_CJK} +[A-Za-z0-9]|[A-Za-z0-9%] +{_CJK}", x):
                    bad.append(f"{n} 中英數之間有空白：{x[:40]}")
    anim = check_timing(path)                        # 放映動畫：封面與 Thank You 以外每頁都要有
    for i, sl in enumerate(prs.slides, 1):
        if sl.slide_layout.name != "頁首" and not anim.get(i):
            bad.append(f"S{i} 沒有進場動畫")
    bad += PROBLEMS
    if bad:
        raise SystemExit(f"{path.name} 檢查未過：\n" + "\n".join(dict.fromkeys(bad)))
    print(f"檢查通過：{path.name}")


def to_pdf(pptx):
    subprocess.run([SOFFICE, "--headless", "--convert-to", "pdf", "--outdir", str(KIT), str(pptx)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return pptx.with_suffix(".pdf")


def contact_sheet(pdf, name):
    from PIL import Image, ImageDraw
    QA.mkdir(parents=True, exist_ok=True)
    tmp = QA / f"_{name}"
    tmp.mkdir(exist_ok=True)
    for f in tmp.glob("*.jpg"):
        f.unlink()
    subprocess.run(["pdftoppm", "-jpeg", "-r", "60", str(pdf), str(tmp / "p")], check=True)
    pages = sorted(tmp.glob("p-*.jpg"), key=lambda p: int(p.stem.split("-")[1]))
    ims = [Image.open(p) for p in pages]
    w, h = ims[0].size
    cols = 4; rows = math.ceil(len(ims) / cols); pad = 16
    sheet = Image.new("RGB", (cols * (w + pad) + pad, rows * (h + pad + 22) + pad), "#d9dce0")
    dr = ImageDraw.Draw(sheet)
    for k, im in enumerate(ims):
        x = pad + (k % cols) * (w + pad); y = pad + (k // cols) * (h + pad + 22)
        dr.text((x, y), f"{k + 1}", fill="#2d2d2d")
        sheet.paste(im, (x, y + 18))
    out = QA / f"xConsole-SalesKit-{name}-總覽.jpg"
    sheet.save(out, quality=88)
    for f in pages:
        f.unlink()
    tmp.rmdir()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--industry", default="all", help="all、通用、企業、政府、學校、醫院")
    ap.add_argument("--skip-diagrams", action="store_true")
    ap.add_argument("--no-pdf", action="store_true")
    a = ap.parse_args()
    names = list(INDUSTRIES) if a.industry == "all" else [a.industry]
    for n in names:
        if n not in INDUSTRIES:
            raise SystemExit(f"不認得的產業：{n}（可用：{'、'.join(INDUSTRIES)}）")
    if not a.skip_diagrams:
        subprocess.run([sys.executable, str(HERE / "diagrams.py")], check=True)
    for n in names:
        PROBLEMS.clear()
        out, count = build(n, INDUSTRIES[n])
        print(f"{out}（{count}頁）")
        if not a.no_pdf:
            pdf = to_pdf(out)
            print("  PDF：", pdf)
            print("  總覽：", contact_sheet(pdf, n))


if __name__ == "__main__":
    main()
