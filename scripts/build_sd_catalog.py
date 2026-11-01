#!/usr/bin/env python3
"""產生隨附的畫圖與影片推薦清單 console/sd_catalog_default.json。

每個元件的檔案與大小用代理的 hf_tree 從 Hugging Face 的檔案清單讀（不下載），跟網頁上「從 Hugging Face 重新讀取」
走同一段程式。這裡只寫人看的欄位與元件組成。政府案不得使用中國來源模型——連文字編碼器、VAE 也算
（FLUX.2 klein 的 Qwen3、Krea2 的 Wan VAE 都是），這份清單一筆都不放。

用法（要能連 Hugging Face）：python3 -I scripts/build_sd_catalog.py
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

FLUX_TE = [{"role": "clip_l", "repo": "comfyanonymous/flux_text_encoders", "path": "clip_l.safetensors"},
           {"role": "t5xxl", "repo": "city96/t5-v1_1-xxl-encoder-gguf", "match": "t5-v1_1-xxl-encoder-"},
           {"role": "vae", "repo": "diffusers/FLUX.1-vae", "path": "diffusion_pytorch_model.safetensors"}]
SEED = [
    {"repo": "leejet/FLUX.1-schnell-gguf", "title": "FLUX.1 schnell", "kind": "image", "vendor": "Black Forest Labs",
     "license": "apache-2.0", "tags": ["快速", "可商用"],
     "desc": "四步就出圖，看得懂長句提示詞、能在圖裡寫英文字；Apache 2.0 可商用，第一顆先裝它",
     "parts": [{"role": "diffusion", "repo": "leejet/FLUX.1-schnell-gguf", "match": "flux1-schnell-"}] + FLUX_TE,
     "defaults": {"width": 1024, "height": 1024, "steps": 4, "cfg": 1.0, "sampler": "euler"}},
    {"repo": "silveroxides/Chroma-GGUF", "title": "Chroma1 HD", "kind": "image", "vendor": "Lodestone Rock",
     "license": "apache-2.0", "tags": ["高畫質", "可商用"],
     "desc": "以 FLUX.1 schnell 為底重新訓練的高畫質模型，細節與寫實感更好，步數多、比較慢",
     "parts": [{"role": "diffusion", "repo": "silveroxides/Chroma-GGUF", "match": "Chroma1-HD/Chroma1-HD-"}] + FLUX_TE[1:],
     "defaults": {"width": 1024, "height": 1024, "steps": 26, "cfg": 4.0, "sampler": "euler"}},
    {"repo": "second-state/stable-diffusion-3.5-medium-GGUF", "title": "Stable Diffusion 3.5 Medium", "kind": "image",
     "vendor": "Stability AI", "license": "stabilityai-community", "tags": ["均衡"],
     "desc": "Stability AI 的中型模型，畫質與速度均衡；年營收一百萬美元以下可免費商用",
     "parts": [{"role": "model", "repo": "second-state/stable-diffusion-3.5-medium-GGUF", "match": "sd3.5_medium-"},
               {"role": "clip_l", "repo": "second-state/stable-diffusion-3.5-medium-GGUF", "match": "clip_l-"},
               {"role": "clip_g", "repo": "second-state/stable-diffusion-3.5-medium-GGUF", "match": "clip_g-"},
               {"role": "t5xxl", "repo": "second-state/stable-diffusion-3.5-medium-GGUF", "match": "t5xxl-"}],
     "defaults": {"width": 1024, "height": 1024, "steps": 28, "cfg": 4.5, "sampler": "euler"}},
    {"repo": "second-state/stable-diffusion-v1-5-GGUF", "title": "Stable Diffusion 1.5", "kind": "image",
     "vendor": "Runway／Stability AI", "license": "creativeml-openrail-m", "tags": ["小模型"],
     "desc": "最經典的小模型，檔案 2 GB 以內、產一張約要 4 GB；記憶體小的機器也跑得動，畫質普通、適合試流程",
     "parts": [{"role": "model", "repo": "second-state/stable-diffusion-v1-5-GGUF", "match": "stable-diffusion-v1-5-pruned-emaonly-"}],
     "defaults": {"width": 512, "height": 512, "steps": 20, "cfg": 7.0, "sampler": "euler_a"}},
    {"repo": "unsloth/LTX-2.3-GGUF", "title": "LTX-2.3 影片（蒸餾版）", "kind": "video", "vendor": "Lightricks",
     "license": "ltx-2-community", "tags": ["影片", "有聲"],
     "desc": "文字產生一段有聲影片；蒸餾版 8 步就好。文字編碼器用 Google Gemma 3 12B，需要 32 GB 以上記憶體",
     "parts": [{"role": "diffusion", "repo": "unsloth/LTX-2.3-GGUF", "match": "distilled-1.1/ltx-2.3-22b-distilled-1.1-"},
               {"role": "llm", "repo": "unsloth/gemma-3-12b-it-GGUF", "match": "gemma-3-12b-it-"},
               {"role": "connectors", "repo": "unsloth/LTX-2.3-GGUF",
                "path": "text_encoders/ltx-2.3-22b-distilled_embeddings_connectors.safetensors"},
               {"role": "vae", "repo": "unsloth/LTX-2.3-GGUF", "path": "vae/ltx-2.3-22b-distilled_video_vae.safetensors"},
               {"role": "audio_vae", "repo": "unsloth/LTX-2.3-GGUF", "path": "vae/ltx-2.3-22b-distilled_audio_vae.safetensors"}],
     "defaults": {"width": 768, "height": 512, "steps": 8, "cfg": 1.0, "sampler": "euler", "frames": 33, "fps": 24}},
]


def main():
    rows = []
    for e in SEED:
        fields, err = recommend.check_sd_fields(e)
        assert not err, f"{e['repo']}：{err}"
        trees = {}
        for repo in {p["repo"] for p in fields["parts"]}:
            code, t = agent.hf_tree(repo)
            assert code == 200, f"{repo}：{t}"
            trees[repo] = t["files"]
        fields["files"] = recommend.sd_files(fields["parts"], trees)
        combos = recommend.sd_resolve(fields)
        assert combos, f"{e['repo']}：一組完整的檔都湊不出來"
        print(f"{fields['title']}：{len(combos)} 種量化，" + "、".join(
            f"{c['main']['quant'] or '原始'} {sum(p['size_gib'] for p in c['parts']):.1f} GiB" for c in combos))
        fields["refreshed"] = int(__import__("time").time())
        rows.append(fields)
    out = os.path.join(ROOT, "console", "sd_catalog_default.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    print("寫入", out)


if __name__ == "__main__":
    main()
