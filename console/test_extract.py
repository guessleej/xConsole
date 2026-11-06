#!/usr/bin/env python3
"""對話附件抽文字的自我檢查（console/extract.py 與主控台的 extract_doc）。
  1. Word、Excel（共用字串、行內字串、數字）、PowerPoint、PDF 抽得出字，標出頁或工作表
  2. 純文字：UTF-8、UTF-8 BOM、UTF-16、Big5 都讀得對；二進位檔、舊版 Office、不支援的格式講清楚
  3. 壓縮炸彈（Office 檔裡的 XML 解開超過 64 MB）不處理；副檔名是 docx 但內容不是 zip 也擋
  4. 掃描型 PDF（抽不出字）講清楚要用看圖模型或 OCR；太長的截斷並標出來
  5. 主控台：獨立行程跑、base64 不對、超過 15 MB、逾時，都回人話
跑法：python3 test_extract.py（需要 requirements.txt 的套件）
"""
import base64
import io
import os
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-extract-")
sys.path.insert(0, HERE)
import extract as E  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def ozip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, t in files.items():
            z.writestr(n, t)
    return b.getvalue()


def bad(name, data, want):
    try:
        E.extract(name, data)
        case(f"{name}：要擋（{want}）", False)
    except ValueError as ex:
        case(f"{name}：要擋（{want}）", want in str(ex), str(ex))


def pdf(text_ops):
    """最小的 PDF：一頁，內容串流照給的運算子（空字串＝沒有文字的頁）。xref 照實算。"""
    stream = text_ops.encode("latin-1")
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    x = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for o in offs:
        out.write(b"%010d 00000 n \n" % o)
    out.write(b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x))
    return out.getvalue()


# ---- 1. Office 與 PDF
docx = ozip({"word/document.xml": '<w:document><w:body><w:p><w:r><w:t>第一段</w:t></w:r><w:r><w:t xml:space="preserve"> 接著</w:t></w:r></w:p>'
                                  '<w:p><w:r><w:t>A &amp; B &lt;C&gt;</w:t></w:r></w:p></w:body></w:document>',
             "word/header1.xml": "<w:hdr><w:p><w:r><w:t>頁首</w:t></w:r></w:p></w:hdr>"})
r = E.extract("報告.docx", docx)
case("Word：段落照順序、XML 跳脫還原、頁首也收", r["text"] == "第一段 接著\nA & B <C>\n頁首", r["text"])

xlsx = ozip({"xl/workbook.xml": '<workbook><sheets><sheet name="預算" sheetId="1"/><sheet name="人力" sheetId="2"/></sheets></workbook>',
             "xl/sharedStrings.xml": "<sst><si><t>項目</t></si><si><t>金額</t></si><si><r><t>伺</t></r><r><t>服器</t></r></si></sst>",
             "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                                         '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>120000</v></c></row></sheetData></worksheet>',
             "xl/worksheets/sheet2.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>工程師</t></is></c><c r="B1"><v>3</v></c></row></sheetData></worksheet>'})
r = E.extract("預算.xlsx", xlsx)
case("Excel：共用字串、行內字串、數字、每張工作表標名稱",
     r["text"] == "## 工作表：預算\n項目\t金額\n伺服器\t120000\n## 工作表：人力\n工程師\t3", r["text"])
# 分頁搬過順序：活頁簿裡第一頁「乙」其實是 sheet2.xml；空白格被省略，靠 r 屬性定位
xlsx2 = ozip({"xl/workbook.xml": '<workbook><sheets><sheet name="乙" sheetId="2" r:id="rId2"/><sheet name="甲" sheetId="1" r:id="rId1"/></sheets></workbook>',
              "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
              "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>甲的資料</t></is></c></row></sheetData></worksheet>',
              "xl/worksheets/sheet2.xml": '<worksheet><sheetData><row r="1"><c r="B1"><v>1</v></c><c r="D1"><v>2</v></c></row></sheetData></worksheet>'})
r = E.extract("排序.xlsx", xlsx2)
case("Excel：工作表照分頁順序、名稱對到正確的檔；省略的空白格補回來",
     r["text"] == "## 工作表：乙\n\t1\t\t2\n## 工作表：甲\n甲的資料", r["text"])

pptx = ozip({"ppt/slides/slide2.xml": "<p:sld><a:p><a:r><a:t>第二頁</a:t></a:r></a:p></p:sld>",
             "ppt/slides/slide1.xml": "<p:sld><a:p><a:r><a:t>標題</a:t></a:r></a:p><a:p><a:r><a:t>內文</a:t></a:r></a:p></p:sld>",
             "ppt/slides/slide10.xml": "<p:sld><a:p><a:r><a:t>第十頁</a:t></a:r></a:p></p:sld>"})
r = E.extract("簡報.pptx", pptx)
case("PowerPoint：沒有 presentation.xml 時照檔名編號（10 排在 2 後面）", r["text"] == "## 第 1 頁\n標題\n內文\n## 第 2 頁\n第二頁\n## 第 3 頁\n第十頁", r["text"])
pptx2 = ozip({**{n: z for n, z in [("ppt/slides/slide1.xml", "<p:sld><a:p><a:r><a:t>先建立的</a:t></a:r></a:p></p:sld>"),
                                   ("ppt/slides/slide2.xml", "<p:sld><a:p><a:r><a:t>搬到第一頁</a:t></a:r></a:p></p:sld>")]},
              "ppt/presentation.xml": '<p:presentation><p:sldIdLst><p:sldId id="257" r:id="rId3"/><p:sldId id="256" r:id="rId2"/></p:sldIdLst></p:presentation>',
              "ppt/_rels/presentation.xml.rels": '<Relationships><Relationship Id="rId2" Target="slides/slide1.xml"/><Relationship Id="rId3" Target="slides/slide2.xml"/></Relationships>'})
r = E.extract("搬過.pptx", pptx2)
case("PowerPoint：照簡報裡實際的順序，頁碼也照這個順序", r["text"] == "## 第 1 頁\n搬到第一頁\n## 第 2 頁\n先建立的", r["text"])

r = E.extract("a.pdf", pdf("BT /F1 12 Tf 20 100 Td (Hello PDF world) Tj ET"))
case("PDF：抽得出字、標頁碼、回總頁數", "Hello PDF world" in r["text"] and r["text"].startswith("## 第 1 頁") and r["pages"] == 1, r)
bad("scan.pdf", pdf(""), "抽不出文字")
bad("broken.pdf", b"%PDF-1.4\n garbage", "損毀")
from pypdf import PdfWriter  # noqa: E402
w = PdfWriter()
w.add_blank_page(200, 200)
w.encrypt(user_password="secret", owner_password="owner")
buf = io.BytesIO()
w.write(buf)
bad("locked.pdf", buf.getvalue(), "密碼保護")

# ---- 2. 純文字
case("純文字：UTF-8", E.extract("a.txt", "你好，世界".encode())["text"] == "你好，世界")
case("純文字：UTF-8 BOM 拿掉", E.extract("a.csv", "﻿欄,值".encode("utf-8"))["text"] == "欄,值")
case("純文字：UTF-16", E.extract("a.txt", "繁體".encode("utf-16"))["text"] == "繁體")
case("純文字：Big5（cp950）", E.extract("舊檔.txt", "臺灣正體中文".encode("cp950"))["text"] == "臺灣正體中文")
case("程式碼：.py 照純文字讀", E.extract("a.py", b"print(1)\r\n")["text"] == "print(1)")
bad("a.txt", b"\x00\x01\x02binary", "二進位")
bad("a.doc", b"\xd0\xcf\x11\xe0", "舊版 Office")
bad("a.exe", b"MZ", "不支援")
bad("空.txt", b"   \n", "沒有文字")

# ---- 3. 壓縮炸彈與偽裝
bomb = ozip({"word/document.xml": "<w:p><w:t>" + "a" * (65 << 20) + "</w:t></w:p>"})
case("壓縮炸彈：壓縮後很小", len(bomb) < 1 << 20, len(bomb))
bad("bomb.docx", bomb, "超過 64 MB")
bad("fake.docx", b"just text", "不是 Office")

# ---- 4. 截斷
r = E.extract("long.txt", ("字" * (E.MAX_CHARS + 50)).encode())
case("太長：截斷並標出原本多長", r["truncated"] and len(r["text"]) == E.MAX_CHARS and r["chars"] == E.MAX_CHARS + 50, (r["truncated"], r["chars"]))

# ---- 5. 主控台這一端（獨立行程）
try:
    import xconsole_server as X
except ImportError as ex:
    print(f"（略過主控台那一段：{ex}；請用裝好 requirements.txt 的 Python 跑）")
    X = None
if X:
    code, out = X.extract_doc("報告.docx", base64.b64encode(docx).decode())
    case("主控台：獨立行程抽得出字", code == 200 and out["text"].startswith("第一段"), (code, out))
    code, out = X.extract_doc("a.exe", base64.b64encode(b"MZ").decode())
    case("主控台：不支援的格式回 422 與人話", code == 422 and "不支援" in out["error"], (code, out))
    code, out = X.extract_doc("a.txt", "!!not base64!!")
    case("主控台：base64 不對回 400", code == 400, (code, out))
    code, out = X.extract_doc("a.txt", base64.b64encode(b"x" * (X.EXTRACT_MAX + 1)).decode())
    case("主控台：超過 15 MB 回 413", code == 413, code)
    code, out = X.extract_doc("", base64.b64encode(b"x").decode())
    case("主控台：沒有檔名回 400", code == 400, code)
    real = subprocess.run

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 30)

    subprocess.run = slow
    code, out = X.extract_doc("a.txt", base64.b64encode(b"hi").decode())
    subprocess.run = real
    case("主控台：逾時回 422 並說明", code == 422 and "30 秒" in out["error"], (code, out))

    def crash(*a, **k):
        return subprocess.CompletedProcess(a[0], -9, b"", b"MemoryError")

    subprocess.run = crash
    code, out = X.extract_doc("a.pdf", base64.b64encode(b"%PDF-").decode())
    subprocess.run = real
    case("主控台：解析行程被砍（記憶體上限）回 422", code == 422 and "解析器中止" in out["error"], (code, out))

print("全部通過" if OK else "有項目失敗")
sys.exit(0 if OK else 1)
