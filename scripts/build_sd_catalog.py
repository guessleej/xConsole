#!/usr/bin/env python3
"""產生隨附的畫圖與影片推薦清單 console/sd_catalog_default.json。

每個元件的檔案與大小用代理的 hf_tree 從 Hugging Face 的檔案清單讀（不下載），跟網頁上「從 Hugging Face 重新讀取」
走同一段程式。這裡只寫人看的欄位與元件組成。照 LocModel 放入所有模型；中國來源（含文字編碼器、VAE，
例 FLUX.2 klein 的 Qwen3）照列，畫面標「中國來源・政府案不可用」。

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
UMT5 = {"role": "t5xxl", "repo": "city96/umt5-xxl-encoder-gguf", "match": "umt5-xxl-encoder-"}
WAN21_VAE = {"role": "vae", "repo": "Comfy-Org/Wan-2.1", "path": "split_files/vae/wan_2.1_vae.safetensors"}
QWEN_VAE = {"role": "vae", "repo": "QuantStack/Qwen-Image-GGUF", "path": "VAE/Qwen_Image-VAE.safetensors"}
QWEN_VL = {"role": "llm", "repo": "mradermacher/Qwen2.5-VL-7B-Instruct-GGUF", "path": "Qwen2.5-VL-7B-Instruct.Q4_K_M.gguf"}
H3_PARTS = [{"role": "llm", "repo": "unsloth/MiniMax-H3-GGUF", "path": "qwen3vl_32b_minimax_h3-Q4_K_M.gguf"},
            {"role": "vae", "repo": "unsloth/MiniMax-H3-GGUF", "path": "vae/minimax_h3_video_vae_fp16.safetensors"},
            {"role": "audio_vae", "repo": "unsloth/MiniMax-H3-GGUF", "path": "vae/minimax_h3_audio_vae_fp32.safetensors"}]
# 順序、說法照 LocModel 的「畫圖 / 影片模型」頁（畫圖 5 顆＋影片 6 顆），再加原本就有的三顆
# 參數照 stable-diffusion.cpp 的 docs/（z_image、qwen_image、qwen_image_edit、flux2、wan、minimax_h3、ltx2）
SEED = [
    {"repo": "leejet/Z-Image-Turbo-GGUF", "title": "Z-Image Turbo", "kind": "image", "vendor": "阿里通義",
     "license": "apache-2.0", "released": "2025-11", "tags": ["畫圖"],
     "desc": "速度快，8 步出圖，中文提示詞友好，4 GB 視訊記憶體也能跑",
     "parts": [{"role": "diffusion", "repo": "leejet/Z-Image-Turbo-GGUF", "match": "z_image_turbo-"},
               {"role": "llm", "repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF", "path": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"},
               {"role": "vae", "repo": "Comfy-Org/Z-Image-Turbo", "path": "split_files/vae/ae.safetensors"}],
     "defaults": {"width": 1024, "height": 1024, "steps": 8, "cfg": 1.0, "sampler": "euler"}},
    {"repo": "QuantStack/Qwen-Image-GGUF", "title": "Qwen-Image", "kind": "image", "vendor": "阿里 Qwen",
     "license": "apache-2.0", "released": "2025-08", "tags": ["畫圖"],
     "desc": "畫面裡的中文、英文文字最準，適合海報、封面、帶字的圖",
     "parts": [{"role": "diffusion", "repo": "QuantStack/Qwen-Image-GGUF", "match": "Qwen_Image-"}, QWEN_VAE, QWEN_VL],
     "defaults": {"width": 1024, "height": 1024, "steps": 20, "cfg": 2.5, "sampler": "euler", "flow_shift": 3}},
    {"repo": "leejet/FLUX.1-schnell-gguf", "title": "FLUX.1 schnell", "kind": "image", "vendor": "Black Forest Labs",
     "license": "apache-2.0", "released": "2024-08", "tags": ["畫圖"],
     "desc": "畫質標竿，4 步出圖，寫實照片和插畫都很強（英文提示詞效果最好）",
     "parts": [{"role": "diffusion", "repo": "leejet/FLUX.1-schnell-gguf", "match": "flux1-schnell-"}] + FLUX_TE,
     "defaults": {"width": 1024, "height": 1024, "steps": 4, "cfg": 1.0, "sampler": "euler"}},
    {"repo": "leejet/FLUX.2-klein-4B-GGUF", "title": "FLUX.2 klein 4B", "kind": "image", "vendor": "Black Forest Labs",
     "license": "apache-2.0", "released": "2026-01", "tags": ["畫圖", "能改圖"],
     "desc": "又小又快，4 步出圖；還能按一句話改圖：換背景、改顏色、加減東西，8 GB 視訊記憶體可用",
     "parts": [{"role": "diffusion", "repo": "leejet/FLUX.2-klein-4B-GGUF", "match": "flux-2-klein-4b-"},
               {"role": "llm", "repo": "unsloth/Qwen3-4B-GGUF", "path": "Qwen3-4B-Q4_K_M.gguf"},
               {"role": "vae", "repo": "Comfy-Org/Flux-2-Klein-4B", "path": "split_files/vae/flux2-vae.safetensors"}],
     "defaults": {"width": 1024, "height": 1024, "steps": 4, "cfg": 1.0, "sampler": "euler", "image": "ref"}},
    {"repo": "unsloth/Qwen-Image-Edit-2511-GGUF", "title": "Qwen-Image-Edit 改圖", "kind": "image", "vendor": "阿里 Qwen",
     "license": "apache-2.0", "released": "2025-12", "tags": ["畫圖", "只能改圖"],
     "desc": "專門改圖：一句話換背景、換衣服、改圖裡的文字，人物長相基本不變，中文指令最準（2511 版）",
     "parts": [{"role": "diffusion", "repo": "unsloth/Qwen-Image-Edit-2511-GGUF", "match": "qwen-image-edit-2511-"}, QWEN_VAE, QWEN_VL,
               {"role": "llm_vision", "repo": "mradermacher/Qwen2.5-VL-7B-Instruct-GGUF", "path": "Qwen2.5-VL-7B-Instruct.mmproj-Q8_0.gguf"}],
     "defaults": {"width": 1024, "height": 1024, "steps": 20, "cfg": 2.5, "sampler": "euler", "flow_shift": 3,
                  "model_args": "qwen_image_zero_cond_t=true", "image": "ref", "needs_image": True}},
    {"repo": "silveroxides/Chroma-GGUF", "title": "Chroma1 HD", "kind": "image", "vendor": "Lodestone Rock",
     "license": "apache-2.0", "released": "2025-08", "tags": ["畫圖", "高畫質"],
     "desc": "以 FLUX.1 schnell 為底重新訓練的高畫質模型，細節與寫實感更好，步數多、比較慢",
     "parts": [{"role": "diffusion", "repo": "silveroxides/Chroma-GGUF", "match": "Chroma1-HD/Chroma1-HD-"}] + FLUX_TE[1:],
     "defaults": {"width": 1024, "height": 1024, "steps": 26, "cfg": 4.0, "sampler": "euler"}},
    {"repo": "second-state/stable-diffusion-3.5-medium-GGUF", "title": "Stable Diffusion 3.5 Medium", "kind": "image",
     "vendor": "Stability AI", "license": "stabilityai-community", "released": "2024-10", "tags": ["畫圖"],
     "desc": "Stability AI 的中型模型，畫質與速度均衡；年營收一百萬美元以下可免費商用",
     "parts": [{"role": "model", "repo": "second-state/stable-diffusion-3.5-medium-GGUF", "match": "sd3.5_medium-"},
               {"role": "clip_l", "repo": "second-state/stable-diffusion-3.5-medium-GGUF", "match": "clip_l-"},
               {"role": "clip_g", "repo": "second-state/stable-diffusion-3.5-medium-GGUF", "match": "clip_g-"},
               {"role": "t5xxl", "repo": "second-state/stable-diffusion-3.5-medium-GGUF", "match": "t5xxl-"}],
     "defaults": {"width": 1024, "height": 1024, "steps": 28, "cfg": 4.5, "sampler": "euler"}},
    {"repo": "second-state/stable-diffusion-v1-5-GGUF", "title": "Stable Diffusion 1.5", "kind": "image",
     "vendor": "Runway／Stability AI", "license": "creativeml-openrail-m", "released": "2022-10", "tags": ["畫圖", "小模型"],
     "desc": "最經典的小模型，檔案 2 GB 以內、產一張約要 4 GB；記憶體小的機器也跑得動，畫質普通、適合試流程",
     "parts": [{"role": "model", "repo": "second-state/stable-diffusion-v1-5-GGUF", "match": "stable-diffusion-v1-5-pruned-emaonly-"}],
     "defaults": {"width": 512, "height": 512, "steps": 20, "cfg": 7.0, "sampler": "euler_a"}},
    # ---- 影片
    {"repo": "Comfy-Org/Wan-2.1", "title": "Wan2.1 文生影片 1.3B", "kind": "video", "vendor": "阿里通義萬相",
     "license": "apache-2.0", "released": "2025-02", "tags": ["影片"],
     "desc": "最輕量的影片模型，8 GB 視訊記憶體就能生成 480P 短影片，適合先體驗",
     "parts": [{"role": "diffusion", "repo": "Comfy-Org/Wan-2.1", "path": "split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors"},
               UMT5, WAN21_VAE],
     "defaults": {"width": 832, "height": 480, "steps": 20, "cfg": 6.0, "sampler": "euler", "frames": 33, "fps": 16,
                  "frame_step": 4, "flow_shift": 3}},
    {"repo": "QuantStack/Wan2.2-TI2V-5B-GGUF", "title": "Wan2.2 影片 5B", "kind": "video", "vendor": "阿里通義萬相",
     "license": "apache-2.0", "released": "2025-07", "tags": ["影片", "圖生影片", "720P"],
     "desc": "畫質明顯更好，支援文字生成影片，也能上傳一張圖片讓它動起來",
     "parts": [{"role": "diffusion", "repo": "QuantStack/Wan2.2-TI2V-5B-GGUF", "match": "Wan2.2-TI2V-5B-"}, UMT5,
               {"role": "vae", "repo": "QuantStack/Wan2.2-TI2V-5B-GGUF", "path": "VAE/Wan2.2_VAE.safetensors"}],
     "defaults": {"width": 832, "height": 480, "steps": 20, "cfg": 6.0, "sampler": "euler", "frames": 33, "fps": 24,
                  "frame_step": 4, "flow_shift": 3}},
    {"repo": "unsloth/LTX-2.3-GGUF", "title": "LTX-2.3 有聲影片", "kind": "video", "vendor": "Lightricks",
     "license": "ltx-2-community", "released": "2026", "tags": ["影片", "圖生影片", "帶聲音", "720P"],
     "desc": "影片和聲音一起生成（對白、音效、背景音樂），支援 720P 和圖片動起來；蒸餾版 8 步出片，速度快",
     "parts": [{"role": "diffusion", "repo": "unsloth/LTX-2.3-GGUF", "match": "distilled-1.1/ltx-2.3-22b-distilled-1.1-"},
               {"role": "llm", "repo": "unsloth/gemma-3-12b-it-GGUF", "match": "gemma-3-12b-it-"},
               {"role": "connectors", "repo": "unsloth/LTX-2.3-GGUF",
                "path": "text_encoders/ltx-2.3-22b-distilled_embeddings_connectors.safetensors"},
               {"role": "vae", "repo": "unsloth/LTX-2.3-GGUF", "path": "vae/ltx-2.3-22b-distilled_video_vae.safetensors"},
               {"role": "audio_vae", "repo": "unsloth/LTX-2.3-GGUF", "path": "vae/ltx-2.3-22b-distilled_audio_vae.safetensors"}],
     "defaults": {"width": 768, "height": 512, "steps": 8, "cfg": 1.0, "sampler": "euler", "frames": 33, "fps": 24}},
    {"repo": "unsloth/MiniMax-H3-GGUF", "title": "MiniMax-H3 有聲影片", "kind": "video", "vendor": "MiniMax 海螺",
     "license": "other", "released": "2026", "tags": ["影片", "圖生影片", "首尾幀", "帶聲音"],
     "desc": "畫面＋立體聲一起生成，人物動作和鏡頭運動自然，可以上傳圖片當第一幀；模型很大，建議 24 GB 視訊記憶體＋64 GB 記憶體",
     "parts": [{"role": "diffusion", "repo": "unsloth/MiniMax-H3-GGUF", "match": "minimax_h3_fl2va_pruned-"}] + H3_PARTS,
     "defaults": {"width": 864, "height": 480, "steps": 20, "cfg": 1.0, "sampler": "euler", "frames": 56, "fps": 24, "frame_step": 1}},
    {"repo": "leejet/MiniMax-H3-GGUF", "title": "MiniMax-H3 多圖參考影片", "kind": "video", "vendor": "MiniMax 海螺",
     "license": "other", "released": "2026", "tags": ["影片", "多圖參考", "帶聲音", "需要上傳圖片"],
     "desc": "上傳參考圖（人物、商品、場景），生成的影片裡會出現它們，同時生成聲音；和「MiniMax-H3 有聲影片」共用文字編碼器和 VAE，已經下過只需再下主體",
     "parts": [{"role": "diffusion", "repo": "leejet/MiniMax-H3-GGUF", "match": "minimax_h3_ref2va_pruned-"}] + H3_PARTS,
     "defaults": {"width": 864, "height": 480, "steps": 20, "cfg": 1.0, "sampler": "euler", "frames": 56, "fps": 24, "frame_step": 1,
                  "image": "ref", "needs_image": True}},
    {"repo": "QuantStack/Wan2.2-I2V-A14B-GGUF", "title": "Wan2.2 圖生影片 A14B", "kind": "video", "vendor": "阿里通義萬相",
     "license": "apache-2.0", "released": "2025-07", "tags": ["影片", "需要上傳圖片"],
     "desc": "開源圖生影片畫質天花板：上傳一張圖讓它動起來，動作幅度大、細節穩定；兩個 14B 專家輪流工作，較慢",
     "parts": [{"role": "diffusion", "repo": "QuantStack/Wan2.2-I2V-A14B-GGUF", "match": "LowNoise/Wan2.2-I2V-A14B-LowNoise-"},
               {"role": "high_noise", "repo": "QuantStack/Wan2.2-I2V-A14B-GGUF", "match": "HighNoise/Wan2.2-I2V-A14B-HighNoise-"},
               UMT5, {"role": "vae", "repo": "QuantStack/Wan2.2-I2V-A14B-GGUF", "path": "VAE/Wan2.1_VAE.safetensors"}],
     "defaults": {"width": 832, "height": 480, "steps": 10, "cfg": 3.5, "sampler": "euler", "frames": 33, "fps": 16,
                  "frame_step": 4, "flow_shift": 3, "high_steps": 8, "high_cfg": 3.5, "needs_image": True}},
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
