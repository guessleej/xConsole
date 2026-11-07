#!/usr/bin/env python3
"""產生隨附的預設推薦清單 console/catalog_default.json。

每一筆的量化大小、架構、KV、MoE 比例、授權與日期，都用代理的 hf_profile 從 Hugging Face 檔頭讀（不下載權重），
跟網頁上「從 Hugging Face 更新」走同一段程式。這裡只寫人看的欄位：顯示名稱、用途、說明、標籤、來源。
中國來源的模型照樣列（使用者拍板：這是軟體，要放入所有模型），畫面上會標「中國來源・政府案不可用」。

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
    ("unsloth/gemma-4-E2B-it-GGUF", "Gemma 4 E2B", "chat", "Google",
     "Google 最小的官方模型，8 GB 的文書筆電也能整顆放進記憶體；能看圖，入門首選", ["小模型", "看圖", "入門"]),
    ("unsloth/gemma-4-12b-it-GGUF", "Gemma 4 12B", "chat", "Google",
     "Google 官方中型稠密模型，能看圖；16 GB 筆電的甜蜜點", ["看圖"]),
    ("unsloth/gemma-3-12b-it-qat-GGUF", "Gemma 3 12B QAT", "chat", "Google",
     "量化感知訓練版：4 位元的品質接近原版，同樣大小答得比較好", ["看圖", "QAT"]),
    ("unsloth/gemma-4-31B-it-GGUF", "Gemma 4 31B", "chat", "Google",
     "Google 官方 31B 稠密模型（xVITA 31B 的底模），能看圖", ["看圖"]),
    ("unsloth/gpt-oss-20b-GGUF", "gpt-oss 20B", "chat", "OpenAI",
     "OpenAI 開源權重 MoE，16 GB 等級的機器跑得動，工具呼叫與推理能力好", ["MoE", "推理", "工具呼叫"]),
    ("unsloth/gpt-oss-120b-GGUF", "gpt-oss 120B", "chat", "OpenAI",
     "OpenAI 開源權重 MoE，工具呼叫與推理能力強，Apache 2.0", ["MoE", "推理", "工具呼叫"]),
    ("unsloth/Nemotron-3-Nano-30B-A3B-GGUF", "Nemotron 3 Nano 30B-A3B", "chat", "NVIDIA",
     "NVIDIA 混合架構 MoE，每次只動用約 3B 參數，長上下文成本低", ["MoE", "長上下文"]),
    ("unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF", "Mistral Small 3.2 24B", "chat", "Mistral AI",
     "Mistral 稠密模型，指令遵循穩定、能看圖，Apache 2.0", ["看圖"]),
    ("unsloth/Llama-3.3-70B-Instruct-GGUF", "Llama 3.3 70B", "chat", "Meta",
     "Meta 稠密 70B，通用能力完整；需要大記憶體與高頻寬", ["大模型"]),
    # 入門：4～8 GB 的文書筆電也放得下
    ("unsloth/gemma-3-1b-it-GGUF", "Gemma 3 1B", "chat", "Google",
     "Google 1B 小模型，不到 1 GB，最舊的筆電也跑得動；簡單問答、改寫用", ["小模型", "入門"]),
    ("unsloth/Llama-3.2-1B-Instruct-GGUF", "Llama 3.2 1B", "chat", "Meta",
     "Meta 1B 小模型，回應很快；記憶體最少的機器先用它試", ["小模型", "入門"]),
    ("unsloth/LFM2.5-1.2B-Instruct-GGUF", "LFM2.5 1.2B", "chat", "Liquid AI",
     "Liquid AI 為筆電與手機設計的混合架構小模型，只用 CPU 也算得快", ["小模型", "入門"]),
    ("unsloth/Llama-3.2-3B-Instruct-GGUF", "Llama 3.2 3B", "chat", "Meta",
     "Meta 3B 小模型，8 GB 筆電的好選擇，多國語言", ["小模型", "入門"]),
    ("unsloth/Ministral-3-3B-Instruct-2512-GGUF", "Ministral 3 3B", "chat", "Mistral AI",
     "Mistral 的 3B 小模型，能看圖，Apache 2.0", ["小模型", "看圖"]),
    ("unsloth/SmolLM3-3B-GGUF", "SmolLM3 3B", "chat", "Hugging Face",
     "Hugging Face 完全公開訓練資料的 3B 模型，可切換思考模式", ["小模型", "推理"]),
    ("unsloth/granite-4.1-3b-GGUF", "Granite 4.1 3B", "chat", "IBM",
     "IBM 企業用小模型，工具呼叫與 RAG 穩定，Apache 2.0", ["小模型", "工具呼叫"]),
    ("unsloth/Phi-4-mini-instruct-GGUF", "Phi-4 mini 3.8B", "chat", "Microsoft",
     "Microsoft 3.8B 小模型，數學與推理在同級裡算強，MIT 授權", ["小模型", "推理"]),
    ("unsloth/gemma-3-4b-it-GGUF", "Gemma 3 4B", "chat", "Google",
     "Google 4B，能看圖，8 GB 筆電放得下", ["小模型", "看圖"]),
    ("unsloth/NVIDIA-Nemotron-3.5-Bolt-E4B-GGUF", "Nemotron 3.5 Bolt E4B", "chat", "NVIDIA",
     "NVIDIA 的小型模型，回應快，適合邊緣裝置與筆電", ["小模型"]),
    # 中型：16～32 GB
    ("unsloth/Olmo-3-7B-Instruct-GGUF", "OLMo 3 7B", "chat", "Allen AI",
     "Allen AI 完全開源（權重、資料、訓練程式都公開）的 7B 模型，Apache 2.0", []),
    ("unsloth/Llama-3.1-8B-Instruct-GGUF", "Llama 3.1 8B", "chat", "Meta",
     "Meta 8B，最多人用的通用模型之一，相容的工具最多", []),
    ("unsloth/Ministral-3-8B-Instruct-2512-GGUF", "Ministral 3 8B", "chat", "Mistral AI",
     "Mistral 8B，能看圖，Apache 2.0", ["看圖"]),
    ("unsloth/granite-4.1-8b-GGUF", "Granite 4.1 8B", "chat", "IBM",
     "IBM 8B 企業用模型，工具呼叫與 RAG 穩定，Apache 2.0", ["工具呼叫"]),
    ("unsloth/LFM2.5-8B-A1B-GGUF", "LFM2.5 8B-A1B", "chat", "Liquid AI",
     "Liquid AI 的 MoE，每次只動用約 1B 參數，只用 CPU 也有不錯的速度", ["MoE"]),
    ("unsloth/gemma-4-12B-it-qat-GGUF", "Gemma 4 12B QAT", "chat", "Google",
     "Gemma 4 12B 的量化感知訓練版：4 位元品質接近原版，能看圖", ["看圖", "QAT"]),
    ("unsloth/Ministral-3-14B-Instruct-2512-GGUF", "Ministral 3 14B", "chat", "Mistral AI",
     "Mistral 14B，能看圖，Apache 2.0", ["看圖"]),
    ("unsloth/Phi-4-reasoning-plus-GGUF", "Phi-4 reasoning plus 14B", "chat", "Microsoft",
     "Microsoft 14B 推理模型，數學、科學、寫程式的推理強，MIT 授權", ["推理"]),
    # 大型：32 GB 以上
    ("unsloth/LFM2.5-24B-A2B-GGUF", "LFM2.5 24B-A2B", "chat", "Liquid AI",
     "Liquid AI 的 MoE，每次只動用約 2B 參數，大模型裡速度很快", ["MoE"]),
    ("unsloth/Magistral-Small-2509-GGUF", "Magistral Small 24B", "chat", "Mistral AI",
     "Mistral 的推理模型，會先想再答，能看圖，Apache 2.0", ["推理", "看圖"]),
    ("unsloth/gemma-4-26B-A4B-it-qat-GGUF", "Gemma 4 26B-A4B QAT", "chat", "Google",
     "Gemma 4 MoE 的量化感知訓練版，4 位元品質接近原版", ["MoE", "看圖", "QAT"]),
    ("unsloth/Muse-Glimmer-30B-GGUF", "Muse Glimmer 30B", "chat", "Meta",
     "Meta 30B，通用能力完整，Apache 2.0", []),
    ("unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF", "Nemotron 3.5 Lightning 30B-A3B", "chat", "NVIDIA",
     "NVIDIA 混合架構 MoE，每次只動用約 3B 參數，長上下文成本低", ["MoE", "長上下文"]),
    ("unsloth/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-GGUF", "Nemotron 3.5 Bolt 74B-A9B", "chat", "NVIDIA",
     "NVIDIA 74B MoE，每次動用約 9B 參數；64 GB 以上的機器", ["MoE", "大模型"]),
    # 中國來源：照列，畫面上標「中國來源・政府案不可用」
    ("unsloth/Qwen3.5-4B-GGUF", "Qwen3.5 4B", "chat", "阿里巴巴 Qwen",
     "Qwen 4B 小模型，中文與推理能力在同級裡強", ["小模型", "推理"]),
    ("unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3 4B Instruct 2507", "chat", "阿里巴巴 Qwen",
     "Qwen3 4B 指令版，不思考直接回答，回應快", ["小模型"]),
    ("unsloth/Qwen3.5-9B-GGUF", "Qwen3.5 9B", "chat", "阿里巴巴 Qwen",
     "Qwen 9B，中文、推理與工具呼叫都好，16 GB 筆電放得下", ["推理", "工具呼叫"]),
    ("unsloth/Qwen3.6-27B-GGUF", "Qwen3.6 27B", "chat", "阿里巴巴 Qwen",
     "Qwen 27B 稠密模型，中文與推理強", ["推理"]),
    ("unsloth/Qwen3.8-27B-GGUF", "Qwen3.8 27B", "chat", "阿里巴巴 Qwen",
     "Qwen 最新的 27B 稠密模型，能看圖", ["推理", "看圖"]),
    ("unsloth/Qwen3.6-35B-A3B-GGUF", "Qwen3.6 35B-A3B", "chat", "阿里巴巴 Qwen",
     "Qwen MoE，每次只動用約 3B 參數，速度快", ["MoE", "推理"]),
    ("unsloth/Qwen3.8-Flash-Next-GGUF", "Qwen3.8 Flash-Next", "chat", "阿里巴巴 Qwen",
     "Qwen 最新的 MoE，長上下文與速度兼顧（Strata 引擎跑的也是這一家族）", ["MoE", "長上下文"]),
    ("unsloth/GLM-4.7-Flash-GGUF", "GLM-4.7 Flash", "chat", "智譜 Z.ai",
     "智譜的 MoE，中文對話與寫程式都不錯，MIT 授權", ["MoE"]),
    ("unsloth/Qwen3.5-122B-A10B-GGUF", "Qwen3.5 122B-A10B", "chat", "阿里巴巴 Qwen",
     "Qwen 122B MoE，每次動用約 10B 參數；96 GB 以上的機器", ["MoE", "大模型", "推理"]),
    ("unsloth/Kimi-K3-GGUF", "Kimi K3", "chat", "月之暗面 Moonshot",
     "Kimi 的兆級參數 MoE，能看圖；最小的量化也要 434 GiB，只適合多卡伺服器（例：8 張 B300）", ["MoE", "大模型", "看圖", "推理"]),
    # 寫程式
    ("xCloudinfo/xGemable-12B-coder-v2-fullft-GGUF", "xGemable 12B Coder v2", "code", "云碩 xCloudinfo",
     "以 Gemma 4 12B 全參數微調的寫程式模型，給 IDE 補全與改程式用", ["寫程式", "自家"], "coder-v2"),
    ("unsloth/Devstral-Small-2-24B-Instruct-2512-GGUF", "Devstral Small 2 24B", "code", "Mistral AI",
     "Mistral 的寫程式代理模型，會自己讀檔、改檔、跑指令；接 Agent 模式用", ["寫程式", "Agent"]),
    ("xCloudinfo/gpt-oss-20b-Code-xCloud-GGUF", "gpt-oss 20B Code", "code", "云碩 xCloudinfo",
     "以 gpt-oss-20b 微調的寫程式模型，回應快，適合接 Copilot 類工具", ["寫程式", "MoE", "自家"]),
    ("unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF", "Qwen3 Coder 30B-A3B", "code", "阿里巴巴 Qwen",
     "Qwen 的寫程式 MoE，每次只動用約 3B 參數，接 Agent 模式或 IDE 用", ["寫程式", "MoE", "Agent"]),
    # 看圖與 OCR
    ("xCloudinfo/Gemma-4-E4B-xReadable-zhTW-OCR-v1", "xReadable 繁中 OCR", "vision", "云碩 xReadable",
     "以 Gemma 4 E4B 微調的繁中文件辨識，掃描公文、表格、手寫都能讀", ["看圖", "OCR", "自家"]),
    ("mradermacher/Qwen2.5-VL-7B-Instruct-GGUF", "Qwen2.5-VL 7B", "vision", "阿里巴巴 Qwen",
     "Qwen 的看圖模型，看圖說明、讀圖表與文件", ["看圖", "OCR"]),
]


def main():
    rows = []
    for repo, title, use, vendor, desc, tags, *rest in SEED:
        match = rest[0] if rest else ""                  # 同一個 repo 放了好幾個模型時，只取檔名含這段的
        code, hf = agent.hf_profile(repo, match=match)
        if code != 200:
            sys.exit(f"{repo}：{hf.get('error')}")
        row = {"repo": repo, "title": title, "use": use, "vendor": vendor, "desc": desc, "tags": tags, "match": match,
               **recommend.hf_fields(hf)}
        rows.append(row)
        p = hf["profile"]
        print(f"{repo:55s} {len(row['quants']):2d} 個量化  active {p.get('active_frac')}  {row['license']}  {row['released']}"
              + ("  中國來源" if p.get("cn_origin") else ""))
    out = os.path.join(ROOT, "console", "catalog_default.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    print(f"寫入 {out}（{len(rows)} 筆）")


if __name__ == "__main__":
    main()
