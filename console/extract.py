#!/usr/bin/env python3
"""對話附件抽文字：PDF、Word、Excel、PowerPoint、純文字與程式碼。

主控台用獨立行程跑這支（限時 30 秒；Linux／macOS 另外限記憶體），檔案從標準輸入進來、結果以 JSON 從標準輸出回去。
上傳的文件是不可信的：壓縮炸彈、會讓解析器跑不完的 PDF 都只會讓這個行程被砍掉，主控台本身不受影響。
Office 檔用標準函式庫的 zipfile 讀 XML，不另外裝套件；PDF 用 pypdf。

    python extract.py <檔名>   < 檔案內容
"""
import io
import json
import re
import sys
import zipfile

MAX_CHARS = 200_000          # 抽出來的字最多留這麼多（約 5～10 萬 token），超過截斷並標出來
MAX_MEMBER = 64 << 20        # Office 檔裡單一 XML 解開後的上限：擋壓縮炸彈
MAX_PAGES = 500
TEXT_EXT = {"txt", "md", "markdown", "csv", "tsv", "json", "jsonl", "log", "xml", "yaml", "yml", "html", "htm", "ini", "toml",
            "py", "js", "ts", "tsx", "jsx", "sh", "ps1", "bat", "sql", "c", "h", "cpp", "hpp", "cs", "java", "go", "rs", "rb",
            "php", "css", "scss", "vue", "kt", "swift", "r", "m", "tex", "srt", "vtt"}
OFFICE = {"docx", "xlsx", "pptx"}


def decode_text(data):
    """純文字：UTF-8（含 BOM）、UTF-16（有 BOM）、台灣常見的 Big5（cp950）依序試。"""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp950"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _member(z, name):
    info = z.getinfo(name)
    if info.file_size > MAX_MEMBER:
        raise ValueError(f"檔案裡的 {name} 解開後超過 64 MB，不處理")
    with z.open(info) as fh:
        return fh.read(MAX_MEMBER + 1).decode("utf-8", "replace")


def _unxml(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))


def _runs(xml, tag):
    """XML 裡 <tag>文字</tag> 的內容（w:t、a:t、t）。"""
    return [_unxml(x) for x in re.findall(rf"<{tag}(?:\s[^>]*)?>([^<]*)</{tag}>", xml)]


def docx_text(z):
    names = ["word/document.xml"] + sorted(n for n in z.namelist() if re.fullmatch(r"word/(header|footer|footnotes)\d*\.xml", n))
    out = []
    for n in names:
        if n not in z.namelist():
            continue
        xml = _member(z, n)
        for para in re.split(r"</w:p>", xml):
            line = "".join(_runs(para, "w:t"))
            if line.strip():
                out.append(line)
    return "\n".join(out)


def _rels(z, path):
    """關聯檔（_rels/*.rels）：Id → 目標檔在壓縮檔裡的完整路徑。"""
    if path not in z.namelist():
        return {}
    base = path.split("_rels/")[0]
    out = {}
    for attrs in re.findall(r"<Relationship\s([^>]*?)/?>", _member(z, path)):
        rid = re.search(r'\bId="([^"]*)"', attrs)
        tgt = re.search(r'\bTarget="([^"]*)"', attrs)
        if rid and tgt:
            t = tgt.group(1)
            out[rid.group(1)] = t.lstrip("/") if t.startswith("/") else _norm(base + t)
    return out


def _norm(path):
    parts = []
    for p in path.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p and p != ".":
            parts.append(p)
    return "/".join(parts)


def _col(ref):
    """儲存格位置（例 D12）→ 欄的序號（0 起算）。"""
    m = re.match(r"([A-Z]+)", ref or "")
    n = 0
    for ch in m.group(1) if m else "":
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_text(z):
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in re.findall(r"<si>(.*?)</si>", _member(z, "xl/sharedStrings.xml"), re.S):
            shared.append("".join(_runs(si, "t")))
    # 工作表照活頁簿裡分頁的順序，名稱經 r:id 對到實際的 sheetN.xml（分頁搬過順序時兩者不一致）
    rels = _rels(z, "xl/_rels/workbook.xml.rels")
    sheets, titles = [], []
    if "xl/workbook.xml" in names:
        for attrs in re.findall(r"<sheet\s([^>]*?)/?>", _member(z, "xl/workbook.xml")):
            nm = re.search(r'\bname="([^"]*)"', attrs)
            rid = re.search(r'\br:id="([^"]*)"', attrs)
            titles.append(_unxml(nm.group(1)) if nm else "")
            target = rels.get(rid.group(1)) if rid else None
            if target in names:
                sheets.append((titles[-1] or target, target))
    if not sheets:   # 沒有關聯檔（少見）：照檔名編號，名稱照順序配
        files = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
        sheets = [(titles[i] if i < len(titles) and titles[i] else n, n) for i, n in enumerate(files)]
    out = []
    for title, n in sheets:
        out.append(f"## 工作表：{title}")
        for row in re.findall(r"<row[^>]*>(.*?)</row>", _member(z, n), re.S):
            cells = {}
            for attrs, body in re.findall(r"<c([^>]*?)(?:/>|>(.*?)</c>)", row, re.S):
                ref = re.search(r'\br="([A-Z]+\d+)"', attrs)
                t = re.search(r'\bt="(\w+)"', attrs)
                v = re.search(r"<v>([^<]*)</v>", body or "")
                if t and t.group(1) == "s" and v and v.group(1).isdigit() and int(v.group(1)) < len(shared):
                    val = shared[int(v.group(1))]
                elif t and t.group(1) == "inlineStr":
                    val = "".join(_runs(body or "", "t"))
                else:
                    val = _unxml(v.group(1)) if v else ""
                # 空白格常被省略、只靠 r 屬性記位置：照欄位放，中間補空格，欄位才不會往前擠
                col = _col(ref.group(1)) if ref else (max(cells) + 1 if cells else 0)
                cells[col] = val
            if any(str(c).strip() for c in cells.values()):
                out.append("\t".join(cells.get(i, "") for i in range(min(max(cells) + 1, 512))))   # 512 欄以後不補空格
    return "\n".join(out)


def pptx_text(z):
    names = z.namelist()
    # 投影片照簡報裡的實際順序（presentation.xml 的 sldIdLst），不是照建立時的檔名編號
    rels = _rels(z, "ppt/_rels/presentation.xml.rels")
    slides = []
    if "ppt/presentation.xml" in names:
        for rid in re.findall(r'<p:sldId\b[^>]*\br:id="([^"]*)"', _member(z, "ppt/presentation.xml")):
            if rels.get(rid) in names:
                slides.append(rels[rid])
    if not slides:
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
    out = []
    for i, n in enumerate(slides, 1):
        xml = _member(z, n)
        lines = [t for t in ("".join(_runs(p, "a:t")) for p in re.split(r"</a:p>", xml)) if t.strip()]
        if lines:
            out.append(f"## 第 {i} 頁")
            out.extend(lines)
    return "\n".join(out)


def pdf_text(data):
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
    try:
        r = PdfReader(io.BytesIO(data))
    except PyPdfError:
        raise ValueError("這份 PDF 損毀或不完整，讀不出來") from None
    if r.is_encrypted:
        # 有些 PDF 只有「擁有者密碼」（限制列印），用空白密碼就打得開；真的要密碼才看得到的，decrypt 回 0（不一定丟例外）
        try:
            ok = r.decrypt("")
        except Exception:  # noqa: BLE001
            ok = 0
        if not ok:
            raise ValueError("這份 PDF 有密碼保護，請先解除再附上")
    out = []
    pages = r.pages
    n = len(pages)
    for i in range(min(n, MAX_PAGES)):
        t = (pages[i].extract_text() or "").strip()
        if t:
            out.append(f"## 第 {i + 1} 頁\n{t}")
        if sum(len(x) for x in out) > MAX_CHARS:
            break
    if not out:
        raise ValueError("這份 PDF 抽不出文字（可能是掃描的圖片）。請改附圖片給看得懂圖的模型，或先做 OCR")
    return "\n\n".join(out), n


def extract(name, data):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    pages = None
    if ext == "pdf" or data.startswith(b"%PDF-"):
        text, pages = pdf_text(data)
    elif ext in OFFICE:
        if not data.startswith(b"PK"):
            raise ValueError("檔案內容不是 Office 格式（副檔名對不上）")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            text = {"docx": docx_text, "xlsx": xlsx_text, "pptx": pptx_text}[ext](z)
    elif ext in ("doc", "xls", "ppt"):
        raise ValueError("舊版 Office 格式（.doc／.xls／.ppt）讀不了，請另存成 .docx／.xlsx／.pptx")
    elif ext in TEXT_EXT or not ext:
        if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise ValueError("這看起來是二進位檔，不是文字")
        text = decode_text(data)
    else:
        raise ValueError(f"不支援 .{ext}：可以附 PDF、Word、Excel、PowerPoint、純文字與程式碼")
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n")).strip()
    if not text:
        raise ValueError("檔案裡沒有文字")
    cut = len(text) > MAX_CHARS
    return {"text": text[:MAX_CHARS], "chars": len(text), "truncated": cut, "pages": pages}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    data = sys.stdin.buffer.read()
    try:
        out = extract(name, data)
    except ValueError as ex:
        out = {"error": str(ex)}
    except Exception as ex:  # noqa: BLE001
        out = {"error": f"讀不出這個檔案（{type(ex).__name__}），檔案可能損毀"}
    # 直接寫 UTF-8 位元組：Windows 的標準輸出預設是 cp1252，寫中文會丟 UnicodeEncodeError
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
