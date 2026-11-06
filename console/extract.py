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


def xlsx_text(z):
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in re.findall(r"<si>(.*?)</si>", _member(z, "xl/sharedStrings.xml"), re.S):
            shared.append("".join(_runs(si, "t")))
    sheet_names = re.findall(r'<sheet [^>]*name="([^"]*)"', _member(z, "xl/workbook.xml")) if "xl/workbook.xml" in names else []
    sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
    out = []
    for i, n in enumerate(sheets):
        out.append(f"## 工作表：{_unxml(sheet_names[i]) if i < len(sheet_names) else n}")
        for row in re.findall(r"<row[^>]*>(.*?)</row>", _member(z, n), re.S):
            cells = []
            for attrs, body in re.findall(r"<c([^>]*?)(?:/>|>(.*?)</c>)", row, re.S):
                t = re.search(r'\bt="(\w+)"', attrs)
                v = re.search(r"<v>([^<]*)</v>", body or "")
                if t and t.group(1) == "s" and v and v.group(1).isdigit() and int(v.group(1)) < len(shared):
                    cells.append(shared[int(v.group(1))])
                elif t and t.group(1) == "inlineStr":
                    cells.append("".join(_runs(body or "", "t")))
                else:
                    cells.append(_unxml(v.group(1)) if v else "")
            if any(c.strip() for c in cells):
                out.append("\t".join(cells))
    return "\n".join(out)


def pptx_text(z):
    slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
    out = []
    for n in slides:
        i = int(re.findall(r"\d+", n)[-1])
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
        try:
            r.decrypt("")
        except Exception:  # noqa: BLE001
            raise ValueError("這份 PDF 有密碼保護，請先解除再附上") from None
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
