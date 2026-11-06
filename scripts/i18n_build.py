#!/usr/bin/env python3
"""把翻譯好的字串組成前端字典 console/static/i18n-{en,ja,ko}.json。

翻譯的真本是 scripts/i18n_translations.json：{原文: {"en": …, "ja": …, "ko": …}}。
新增介面字串之後：
  1. python3 scripts/i18n_extract.py         重新抽出所有中文字串（scripts/i18n_source.json）
  2. 把還沒翻的補進 i18n_translations.json  （python3 scripts/i18n_build.py --missing 列出還沒翻的）
  3. python3 scripts/i18n_build.py           重新產生三份字典（只收佔位符對得上的）
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "scripts", "i18n_source.json")
TR = os.path.join(ROOT, "scripts", "i18n_translations.json")
LANGS = ("en", "ja", "ko")
PH = re.compile(r"\{\d+\}")


def main():
    src = [r["zh"] for r in json.load(open(SRC, encoding="utf-8"))]
    tr = json.load(open(TR, encoding="utf-8")) if os.path.exists(TR) else {}
    if "--missing" in sys.argv:
        miss = [z for z in src if not all((tr.get(z) or {}).get(l) for l in LANGS)]
        json.dump(miss, sys.stdout, ensure_ascii=False, indent=0)
        print(f"\n還沒翻：{len(miss)} 筆", file=sys.stderr)
        return
    bad = 0
    for lang in LANGS:
        out = {}
        for z in src:
            t = (tr.get(z) or {}).get(lang)
            if not t:
                continue
            if sorted(PH.findall(z)) != sorted(PH.findall(t)):
                bad += 1
                continue
            if t != z:
                out[z] = t
        p = os.path.join(ROOT, "console", "static", f"i18n-{lang}.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        print(f"{lang}: {len(out)} 筆", file=sys.stderr)
    cov = sum(1 for z in src if all((tr.get(z) or {}).get(l) for l in LANGS)) / max(1, len(src))
    print(f"涵蓋率 {cov:.1%}；佔位符對不上而略過 {bad} 筆", file=sys.stderr)


if __name__ == "__main__":
    main()
