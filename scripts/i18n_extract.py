#!/usr/bin/env python3
"""抽出介面上會出現的中文字串，給翻譯用（console/static/i18n/*.json 的來源）。

前端（JS、HTML）：字串常值與樣板字串；樣板裡的 ${...} 換成 {0}、{1}…，翻譯時照原樣保留。
後端（主控台、代理）：回給畫面的錯誤與說明（Python 字串常值與 f-string），一樣換成佔位。
輸出 scripts/i18n_source.json：[{"zh": 原文, "pattern": true/false}]，依出現順序、去重。
跑法：python3 scripts/i18n_extract.py
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CJK = re.compile(r"[㐀-鿿豈-﫿]")
JS = ["console/static/app.js", "console/static/pages.js", "console/static/chat.js"]
HTML = ["console/static/index.html"]
PY = ["console/xconsole_server.py", "console/chats.py", "console/computer.py", "console/recommend.py",
      "console/extract.py", "console/tls.py", "console/updater.py", "agent/xconsole_agent.py"]


def js_strings(src):
    """簡單的 JS 斷詞：跳過註解與正規式，取出 "…" '…' `…`（樣板的 ${} 換成 {n}）。"""
    out, i, n = [], 0, len(src)
    prev = ""
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            i = src.find("\n", i)
            i = n if i < 0 else i
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and prev in "(,=:[!&|?{};+-*%<>~^" + "\n":
            j, cls = i + 1, False
            while j < n and (src[j] != "/" or cls) and src[j] != "\n":
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    cls = True
                elif src[j] == "]":
                    cls = False
                j += 1
            i = j + 1
            prev = "x"
            continue
        if c in "\"'":
            j, buf = i + 1, []
            while j < n and src[j] != c and src[j] != "\n":
                if src[j] == "\\" and j + 1 < n:
                    buf.append({"n": "\n", "t": "\t"}.get(src[j + 1], src[j + 1]))
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            out.append(("".join(buf), False))
            i = j + 1
            prev = "x"
            continue
        if c == "`":
            j, buf, k, depth = i + 1, [], 0, 0
            while j < n and src[j] != "`":
                if src[j] == "\\" and j + 1 < n:
                    buf.append({"n": "\n", "t": "\t"}.get(src[j + 1], src[j + 1]))
                    j += 2
                    continue
                if src.startswith("${", j):
                    depth, j = 1, j + 2
                    start = j
                    while j < n and depth:
                        if src[j] == "{":
                            depth += 1
                        elif src[j] == "}":
                            depth -= 1
                        elif src[j] == "`":       # 巢狀樣板：先把裡面的字串也收進來
                            sub_end = src.find("`", j + 1)
                            out.extend(js_strings(src[j:sub_end + 1]))
                            j = sub_end
                        j += 1
                    out.extend(js_strings(src[start:j - 1]))
                    buf.append("{%d}" % k)
                    k += 1
                    continue
                buf.append(src[j])
                j += 1
            out.append(("".join(buf), k > 0))
            i = j + 1
            prev = "x"
            continue
        if not c.isspace():
            prev = c
        i += 1
    return out


def py_strings(src):
    out = []
    tree = ast.parse(src)
    doc = {id(n.body[0].value) for n in ast.walk(tree)
           if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
           and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
    for node in ast.walk(tree):
        if id(node) in doc:
            continue                                   # 說明文字（docstring）不會出現在畫面上
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.value, False))
        elif isinstance(node, ast.JoinedStr):
            parts, k = [], 0
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    parts.append("{%d}" % k)
                    k += 1
            out.append(("".join(parts), k > 0))
    return out


def html_strings(src):
    out = [(m.group(1).strip(), False) for m in re.finditer(r">([^<>]+)<", src)]
    out += [(m.group(2), False) for m in re.finditer(r'(placeholder|title|aria-label|content)="([^"]+)"', src)]
    return out


def keep(s):
    if not CJK.search(s) or len(s) > 600:
        return False
    if s.lstrip().startswith(("#", "//")) and "\n" in s:
        return False
    return True


def main():
    seen, rows = set(), []
    for rel, fn in [(p, js_strings) for p in JS] + [(p, html_strings) for p in HTML] + [(p, py_strings) for p in PY]:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for s, pat in fn(src):
            # 樣板裡被 {n} 切開的每一段中文本身也收（畫面上常是好幾段拼起來的）
            pieces = [s] + ([x for x in re.split(r"\{\d+\}", s) if x.strip()] if pat else []) \
                + ([x.strip() for x in re.findall(r">([^<>]+)<", s)] if "<" in s else [])   # innerHTML 字串裡標籤之間的字
            for i, t in enumerate(pieces):
                t = t if i == 0 else t.strip(" ，、：；。（）()・")
                if keep(t) and t not in seen:
                    seen.add(t)
                    rows.append({"zh": t, "pattern": bool(re.search(r"\{\d+\}", t))})
    json.dump(rows, open(os.path.join(ROOT, "scripts", "i18n_source.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print(f"{len(rows)} 筆（樣板 {sum(r['pattern'] for r in rows)}）", file=sys.stderr)


if __name__ == "__main__":
    main()
