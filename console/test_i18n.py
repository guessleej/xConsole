#!/usr/bin/env python3
"""介面語言的自我檢查：
- 三份字典（i18n-en／ja／ko.json）存在、是合法 JSON、佔位符 {n} 跟原文一致
- 涵蓋率：目前抽得到的介面字串，三種語言都翻到 95% 以上（新加的字串沒翻會讓這項掉下來）
- i18n.js 在 app.js 之前載入；不翻的區塊（模型回答、程式碼、輸入框）有列在跳過清單
- 翻譯函式（有 node 時實際跑）：整句、帶變數的樣板、拼起來的片段都換得掉，沒有中文的字不動
跑法：python3 test_i18n.py
"""
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ST = os.path.join(HERE, "static")
OK = True
PH = re.compile(r"\{\d+\}")


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def main():
    # 現抽一次：新加的介面字串沒翻，涵蓋率就會掉（不靠提交進來的 i18n_source.json）
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import i18n_extract as X
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".i18n_check.json")
    real = X.os.path.join
    X_out = []
    for rel, fn in [(p, X.js_strings) for p in X.JS] + [(p, X.html_strings) for p in X.HTML] + [(p, X.py_strings) for p in X.PY]:
        for s0, pat in fn(open(os.path.join(ROOT, rel), encoding="utf-8").read()):
            pieces = [s0] + ([x for x in re.split(r"\{\d+\}", s0) if x.strip()] if pat else []) \
                + ([x.strip() for x in re.findall(r">([^<>]+)<", s0)] if "<" in s0 else [])
            for i, t in enumerate(pieces):
                t = t if i == 0 else t.strip(" ，、：；。（）()・")
                if X.keep(t):
                    X_out.append(t)
    src = list(dict.fromkeys(X_out))
    del tmp, real
    dicts = {}
    for lang in ("en", "ja", "ko"):
        p = os.path.join(ST, f"i18n-{lang}.json")
        try:
            dicts[lang] = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError) as ex:
            check(f"{lang} 字典讀得到", False, str(ex))
            continue
        d = dicts[lang]
        bad = [k for k, v in d.items() if sorted(PH.findall(k)) != sorted(PH.findall(v))]
        check(f"{lang}：佔位符跟原文一致", not bad, bad[:3])
        cov = sum(1 for z in src if z in d) / max(1, len(src))
        check(f"{lang}：涵蓋率 {cov:.1%}（至少 95%）", cov >= 0.95)
        han = [v for v in d.values() if lang != "ja" and re.search(r"[一-鿿]", v)]
        check(f"{lang}：譯文沒有殘留中文", len(han) <= len(d) * 0.01, han[:3])
    html = open(os.path.join(ST, "index.html"), encoding="utf-8").read()
    check("i18n.js 在 app.js 之前載入", 0 <= html.find('src="i18n.js"') < html.find('src="app.js"'))
    js = open(os.path.join(ST, "i18n.js"), encoding="utf-8").read()
    skip = re.search(r'I18N_SKIP = "([^"]+)"', js).group(1)
    for sel in (".msg-text", "pre", "code", "textarea", "input", ".mono", "[data-i18n-skip]"):
        check(f"不翻的區塊包含 {sel}", sel in skip)

    node = shutil.which("node")
    if not node or "en" not in dicts:
        print("（沒有 node 或沒有英文字典，略過翻譯函式的實跑）")
    else:
        sample = {"重新整理": "Refresh", "已啟動 {0}": "Started {0}", "約 {0} tok/s": "about {0} tok/s",
                  "記憶體不夠": "Not enough memory", "請換小一點的量化": "Use a smaller quantization"}
        prog = js + """
const out = [];
globalThis.__run = async () => {
  I18N.lang = "en"; I18N.exact = new Map(); I18N.patterns = [];
  const dict = %s;
  const pieces = [];
  for (const [zh, tr] of Object.entries(dict)) {
    if (/\\{\\d+\\}/.test(zh)) { const names = []; const rx = new RegExp("^" + reEsc(zh).replace(/\\\\\\{(\\d+)\\\\\\}/g, (_, d) => { names.push(Number(d)); return "([\\\\s\\\\S]+?)"; }) + "$"); I18N.patterns.push({ rx, names, tr, len: zh.length }); }
    else { I18N.exact.set(zh, tr); pieces.push(zh); }
  }
  pieces.sort((a, b) => b.length - a.length);
  I18N.re = new RegExp(pieces.map(reEsc).join("|"), "g");
  console.log(JSON.stringify([i18nText("重新整理"), i18nText("  重新整理 "), i18nText("已啟動 gemma-4"), i18nText("約 12 tok/s"),
    i18nText("記憶體不夠，請換小一點的量化"), i18nText("gemma-4-E2B"), i18nText("已啟動 約 3 tok/s")]));
  i18nData({ nodes: [{ id: "n1", name: "重新整理", slots: [{ role: "記憶體不夠" }] }], error: "請換小一點的量化" });
  console.log(JSON.stringify([i18nText("重新整理"), i18nText("已啟動 記憶體不夠"), i18nText("請換小一點的量化")]));
};
__run();
""" % json.dumps(sample, ensure_ascii=False)
        stub = "globalThis.navigator={languages:['zh-TW']};globalThis.localStorage={getItem:()=>null,setItem(){}};" \
               "globalThis.document={documentElement:{}};globalThis.location={reload(){}};"
        r = subprocess.run([node, "-e", stub + prog], capture_output=True, text=True, timeout=30)
        try:
            got = json.loads(r.stdout.strip().splitlines()[-2])
            data = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            got = None
            check("翻譯函式跑得起來", False, r.stderr[-300:])
        if got:
            check("整句換掉", got[0] == "Refresh", got[0])
            check("  前後空白保留", got[1] == "  Refresh ", repr(got[1]))
            check("帶變數的樣板：變數原樣帶過去", got[2] == "Started gemma-4", got[2])
            check("  數字變數", got[3] == "about 12 tok/s", got[3])
            check("拼起來的句子：認得的片段換掉", "Not enough memory" in got[4] and "Use a smaller quantization" in got[4], got[4])
            check("沒有中文的字不動（模型名稱）", got[5] == "gemma-4-E2B", got[5])
            check("樣板的變數本身也會翻（巢狀）", got[6] == "Started about 3 tok/s", got[6])
            check("使用者資料（機器名）跟介面文字一樣也不翻", data[0] == "重新整理", data[0])
            check("  句子裡夾著的資料（角色名）原樣保留，其他照翻", data[1] == "Started 記憶體不夠", data[1])
            check("  錯誤訊息不是資料，照翻", data[2] == "Use a smaller quantization", data[2])
    print("全部通過" if OK else "有項目失敗")
    sys.exit(0 if OK else 1)


if __name__ == "__main__":
    main()
