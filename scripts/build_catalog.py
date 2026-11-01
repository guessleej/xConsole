#!/usr/bin/env python3
"""產生隨附的預設推薦清單 console/catalog_default.json。

每一筆的量化大小、架構、KV、MoE 比例、授權與日期，都用代理的 hf_profile 從 Hugging Face 檔頭讀（不下載權重），
跟網頁上「從 Hugging Face 更新」走同一段程式。這裡只寫人看的欄位：顯示名稱、用途、說明、標籤、來源。
政府案不得使用中國來源模型，這份清單一筆都不放。

用法（要能連 Hugging Face）：python3 -I scripts/build_catalog.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["HOME"] = tempfile.mkdtemp()           # 代理匯入時會建權杖檔，別寫進這台的家目錄
sys.path[:0] = [os.path.join(ROOT, "agent"), os.path.join(ROOT, "console")]
import recommend  # noqa: E402
import xconsole_agent as agent  # noqa: E402

SEED = [
    # 對話與知識
    ("xCloudinfo/Gemma-4-31B-xVITA-zhTW-GGUF", "xVITA 31B 繁中", "chat", "云碩 xVITA",
     "以 Google Gemma 4 31B 微調的繁體中文模型，台灣用語，能看圖；記憶體夠的機器首選", ["繁中", "看圖", "自家"]),
    ("xCloudinfo/Gemma-4-26B-A4B-TAIDE-zhTW-GGUF", "Gemma 4 26B-A4B TAIDE 繁中", "chat", "云碩 xCloudinfo",
     "以 TAIDE 繁中語料微調的 Gemma 4 MoE，每次只動用約 4B 參數，速度快", ["繁中", "MoE", "自家"]),
    ("xCloudinfo/Granite-4.2-30B-xVITA-zhTW-GGUF", "Granite 4.2 30B xVITA 繁中", "chat", "云碩 xVITA",
     "以 IBM Granite 4.2 30B 微調的繁體中文模型，非中國來源，適合政府案", ["繁中", "自家"]),
    ("xCloudinfo/gpt-oss-120b-TAIDE-zhTW-GGUF", "gpt-oss 120B TAIDE 繁中", "chat", "云碩 xCloudinfo",
     "OpenAI gpt-oss-120b 以 TAIDE 語料微調，推理能力強；需要 64 GB 以上記憶體", ["繁中", "MoE", "推理", "自家"]),
    ("xCloudinfo/gpt-oss-20b-TAIDE-zhTW-GGUF", "gpt-oss 20B TAIDE 繁中", "chat", "云碩 xCloudinfo",
     "OpenAI gpt-oss-20b 以 TAIDE 語料微調，16 GB 等級的機器也跑得動", ["繁中", "MoE", "推理", "自家"]),
    ("unsloth/gemma-4-26B-A4B-it-GGUF", "Gemma 4 26B-A4B", "chat", "Google",
     "Google 官方 MoE，每次只動用約 4B 參數，能看圖、支援長上下文", ["MoE", "看圖"]),
    ("unsloth/gemma-4-E4B-it-GGUF", "Gemma 4 E4B", "chat", "Google",
     "Google 官方小模型，能看圖；小機器、邊緣裝置、或要很快回應時用", ["小模型", "看圖"]),
    ("unsloth/gpt-oss-120b-GGUF", "gpt-oss 120B", "chat", "OpenAI",
     "OpenAI 開源權重 MoE，工具呼叫與推理能力強，Apache 2.0", ["MoE", "推理", "工具呼叫"]),
    ("unsloth/Nemotron-3-Nano-30B-A3B-GGUF", "Nemotron 3 Nano 30B-A3B", "chat", "NVIDIA",
     "NVIDIA 混合架構 MoE，每次只動用約 3B 參數，長上下文成本低", ["MoE", "長上下文"]),
    ("unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF", "Mistral Small 3.2 24B", "chat", "Mistral AI",
     "Mistral 稠密模型，指令遵循穩定、能看圖，Apache 2.0", ["看圖"]),
    ("unsloth/Llama-3.3-70B-Instruct-GGUF", "Llama 3.3 70B", "chat", "Meta",
     "Meta 稠密 70B，通用能力完整；需要大記憶體與高頻寬", ["大模型"]),
    # 寫程式
    ("xCloudinfo/xGemable-12B-coder-v2-fullft-GGUF", "xGemable 12B Coder v2", "code", "云碩 xCloudinfo",
     "以 Gemma 4 12B 全參數微調的寫程式模型，給 IDE 補全與改程式用", ["寫程式", "自家"], "coder-v2"),
    ("xCloudinfo/gpt-oss-20b-Code-xCloud-GGUF", "gpt-oss 20B Code", "code", "云碩 xCloudinfo",
     "以 gpt-oss-20b 微調的寫程式模型，回應快，適合接 Copilot 類工具", ["寫程式", "MoE", "自家"]),
    # 看圖與 OCR
    ("xCloudinfo/Gemma-4-E4B-xReadable-zhTW-OCR-v1", "xReadable 繁中 OCR", "vision", "云碩 xReadable",
     "以 Gemma 4 E4B 微調的繁中文件辨識，掃描公文、表格、手寫都能讀", ["看圖", "OCR", "自家"]),
]


def main():
    rows = []
    for repo, title, use, vendor, desc, tags, *rest in SEED:
        match = rest[0] if rest else ""                  # 同一個 repo 放了好幾個模型時，只取檔名含這段的
        code, hf = agent.hf_profile(repo, match=match)
        if code != 200:
            sys.exit(f"{repo}：{hf.get('error')}")
        if hf["profile"].get("cn_origin"):
            sys.exit(f"{repo}：檔頭判定為中國來源，不能放進推薦清單")
        row = {"repo": repo, "title": title, "use": use, "vendor": vendor, "desc": desc, "tags": tags, "match": match,
               **recommend.hf_fields(hf)}
        rows.append(row)
        p = hf["profile"]
        print(f"{repo:55s} {len(row['quants']):2d} 個量化  active {p.get('active_frac')}  {row['license']}  {row['released']}")
    out = os.path.join(ROOT, "console", "catalog_default.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    print(f"寫入 {out}（{len(rows)} 筆）")


if __name__ == "__main__":
    main()
