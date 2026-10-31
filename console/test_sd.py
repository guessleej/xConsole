#!/usr/bin/env python3
"""畫圖與影片的自我檢查：推薦（放不放得下、挑哪個量化、元件跟著主體挑）、清單欄位與中國來源過濾、
產圖前的准入（三種記憶體架構）、sd-cli 參數、進度解析、共用檔刪除、產圖途中的記憶體看門狗。
這支壞掉，畫面會推薦一顆產圖時把統一記憶體吃爆的模型，或讓中國來源的文字編碼器混進政府案。
跑法：python3 test_sd.py（不需要任何套件、不連網路）
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HOME"] = tempfile.mkdtemp()           # 代理匯入時會建權杖檔，不要碰到真的 ~/.xconsole
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "agent")]
import recommend as R  # noqa: E402
import xconsole_agent as A  # noqa: E402

GIB = R.GIB
OK = True


def case(name, got, want):
    global OK
    good = got == want
    OK &= good
    print(f"[{'通過' if good else '失敗'}] {name}（得到 {got}，預期 {want}）")


def state(mm, **pools):
    return {"pools": {"memory_model": mm, **pools}, "chip": {"bandwidth_gbs": 256}}


AI395 = state("unified_split", floors={"ram": 15, "vram": 4}, vram={"total_gib": 96}, ram={"total_gib": 31},
              gpus=[{"id": "0", "total_gib": 96}])
SPARK = state("unified_single", floors={"ram": 12}, unified={"total_gib": 120}, gpus=[])
JETSON16 = state("unified_single", floors={"ram": 4}, unified={"total_gib": 16}, gpus=[])
ADA2 = state("discrete", floors={"ram": 8, "vram": 2}, ram={"total_gib": 212},
             gpus=[{"id": "0", "total_gib": 48}, {"id": "1", "total_gib": 24}])
SMALLCARD = state("discrete", floors={"ram": 8, "vram": 1}, ram={"total_gib": 64}, gpus=[{"id": "0", "total_gib": 8}])
G = GIB
FLUX = {"repo": "leejet/FLUX.1-schnell-gguf", "title": "FLUX.1 schnell", "kind": "image",
        "parts": [{"role": "diffusion", "repo": "leejet/FLUX.1-schnell-gguf", "match": "flux1-schnell-"},
                  {"role": "clip_l", "repo": "comfyanonymous/flux_text_encoders", "path": "clip_l.safetensors"},
                  {"role": "t5xxl", "repo": "city96/t5-v1_1-xxl-encoder-gguf", "match": "t5-v1_1-xxl-encoder-"},
                  {"role": "vae", "repo": "diffusers/FLUX.1-vae", "path": "diffusion_pytorch_model.safetensors"}],
        "defaults": {"width": 1024, "height": 1024, "steps": 4, "cfg": 1.0, "sampler": "euler"},
        "files": {"leejet/FLUX.1-schnell-gguf": {"flux1-schnell-q2_k.gguf": 3.8 * G, "flux1-schnell-q4_k.gguf": 6.4 * G,
                                                 "flux1-schnell-q8_0.gguf": 11.9 * G},
                  "comfyanonymous/flux_text_encoders": {"clip_l.safetensors": 0.23 * G},
                  "city96/t5-v1_1-xxl-encoder-gguf": {"t5-v1_1-xxl-encoder-Q4_K_M.gguf": 2.7 * G,
                                                      "t5-v1_1-xxl-encoder-Q8_0.gguf": 4.7 * G,
                                                      "t5-v1_1-xxl-encoder-f16.gguf": 8.9 * G},
                  "diffusers/FLUX.1-vae": {"diffusion_pytorch_model.safetensors": 0.16 * G}}}


def main():
    # ---- 檔名 → 量化、位元數
    case("量化：小寫 q4_k", R.sd_quant("flux1-schnell-q4_k.gguf"), "Q4_K")
    case("量化：unsloth 動態 UD-Q4_K_M", R.sd_quant("distilled-1.1/ltx-2.3-22b-distilled-1.1-UD-Q4_K_M.gguf"), "UD-Q4_K_M")
    case("量化：safetensors 的 fp16", R.sd_quant("t5xxl_fp16.safetensors"), "FP16")
    case("量化：看不出來回空字串", R.sd_quant("clip_l.safetensors"), "")
    case("位元：空白當 16 位元、FP8 是 8", (R.sd_bits(""), R.sd_bits("FP8_E4M3FN"), R.sd_bits("Q8_0")), (16, 8, 8))
    # ---- 元件挑檔
    files = {"Chroma1-HD/Chroma1-HD-Q8_0.gguf": 9 * G, "Chroma1-HD/Chroma1-HD-dc/Chroma1-HD-dc-Q8_0.gguf": 9 * G,
             "Chroma1-HD/mmproj-x.gguf": 1}
    case("檔名開頭：不往下一層資料夾挑、不挑 mmproj",
         [o["path"] for o in R.sd_options({"match": "Chroma1-HD/Chroma1-HD-"}, files)], ["Chroma1-HD/Chroma1-HD-Q8_0.gguf"])
    t5 = R.sd_options(FLUX["parts"][2], FLUX["files"]["city96/t5-v1_1-xxl-encoder-gguf"])
    case("文字編碼器跟著主體：主體 Q8 配 Q8", R.sd_follow(t5, 8)["quant"], "Q8_0")
    case("文字編碼器跟著主體：主體 Q2 也至少配 Q4", R.sd_follow(t5, 2)["quant"], "Q4_K_M")
    combos = R.sd_resolve(FLUX)
    case("FLUX：三種主體量化各湊成一組四個檔", [len(c["parts"]) for c in combos], [4, 4, 4])
    case("缺一個元件的檔就湊不出", R.sd_resolve({**FLUX, "files": {**FLUX["files"], "diffusers/FLUX.1-vae": {}}}), [])
    spec = R.sd_spec(FLUX, combos[1])
    case("下載規格：代號只有英數連字號、帶量化", spec["id"], "flux-1-schnell-gguf-q4_k")
    case("下載規格：代理那邊檢查得過", A.sd_check_spec(spec)[1], None)
    # ---- 放不放得下
    cap = R.sd_capacity(ADA2)
    case("獨立顯卡：sd-cli 只用一張，快速層取最大那張扣底線", cap["fast"], 46.0)
    plans = [R.sd_plan(R.sd_capacity(AI395), "image", FLUX["defaults"], c) for c in combos]
    case("AI 395：三種都流暢，放 GPU 那一塊", [(p["level"], p["place"]) for p in plans], [("smooth", "全部放進 GPU 那一塊")] * 3)
    case("AI 395：建議 Q8（擴散模型 4～8 位元取最高）", R.sd_pick(plans)["quant"], "Q8_0")
    plans = [R.sd_plan(R.sd_capacity(JETSON16), "image", FLUX["defaults"], c) for c in combos]
    case("16 GB Jetson：只有 Q2 放得下，Q4／Q8 放不下", [p["level"] for p in plans], ["smooth", "no", "no"])
    case("16 GB Jetson：流暢的只有 Q2（低於 4 位元），退而求其次還是挑它", R.sd_pick(plans)["quant"], "Q2_K")
    plans = [R.sd_plan(R.sd_capacity(SMALLCARD), "image", FLUX["defaults"], c) for c in combos]
    case("8 GB 顯示卡：Q2 權重放系統記憶體輪流搬（能跑）；Q4 最大一個檔加計算緩衝就超過卡",
         [p["level"] for p in plans], ["ok", "no", "no"])
    case("8 GB 顯示卡：都不流暢挑能跑的最小一個", R.sd_pick(plans)["quant"], "Q2_K")
    # ---- 推薦結果：已下載、下載中
    lib = {"free_gib": 500, "models": [{"id": "flux-1-schnell-gguf-q4_k", "ready": True, "measured": {"secs": 12}}],
           "downloads": [{"id": "ab12cd34", "state": "running", "sd_model": "flux-1-schnell-gguf-q8_0", "done": 1, "total": 4}]}
    rec = R.recommend_sd(SPARK, lib, [FLUX])
    it = rec["items"][0]
    case("推薦：已下載的那個量化排第一個顯示", it["installed"], "flux1-schnell-q4_k.gguf")
    case("推薦：下載中的工作掛在這一列", it["job"]["id"], "ab12cd34")
    case("推薦：實測值帶到該量化", next(p for p in it["plans"] if p["ready"])["measured"], {"secs": 12})
    # ---- 清單欄位與中國來源
    flux_in = {k: FLUX[k] for k in ("repo", "title", "kind", "parts", "defaults")}
    case("清單欄位：FLUX 收", R.check_sd_fields(flux_in)[1], None)
    klein = {**flux_in, "repo": "leejet/FLUX.2-klein-4B-GGUF", "parts": [
        {"role": "diffusion", "repo": "leejet/FLUX.2-klein-4B-GGUF", "match": "flux-2-klein-4b-"},
        {"role": "llm", "repo": "unsloth/Qwen3-4B-GGUF", "match": "Qwen3-4B-"},
        {"role": "vae", "repo": "black-forest-labs/FLUX.2-dev", "path": "vae/diffusion_pytorch_model.safetensors"}]}
    case("清單欄位：主體非中國、文字編碼器是 Qwen 也不收", "中國來源" in (R.check_sd_fields(klein)[1] or ""), True)
    krea = {**flux_in, "parts": flux_in["parts"][:1] + [
        {"role": "vae", "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged", "path": "split_files/vae/wan_2.1_vae.safetensors"}]}
    case("清單欄位：Wan 的 VAE 也不收", "中國來源" in (R.check_sd_fields(krea)[1] or ""), True)
    case("中國來源比對：swan、wander 不算 Wan", bool(R.SD_CN_RX.search("swan-lake wander")), False)
    two = {**flux_in, "parts": flux_in["parts"] + [{"role": "model", "repo": FLUX["repo"], "path": "x.gguf"}]}
    case("清單欄位：兩個主體不收", R.check_sd_fields(two)[1], "要有、而且只能有一個主體（完整模型或擴散模型）")
    bad = {**flux_in, "parts": [{"role": "diffusion", "repo": FLUX["repo"], "path": "../../etc/passwd.gguf"}]}
    case("清單欄位：檔名有 .. 不收", "要填" in (R.check_sd_fields(bad)[1] or ""), True)
    case("清單欄位：影片影格數要 8n+1", R.check_sd_defaults({"frames": 32}, "video")[1], "影格數 1～241 且是 8 的倍數加 1，每秒格數 1～60")
    # ---- 與代理同一份常數
    case("中國來源比對：主控台與代理同一條", R.SD_CN_RX.pattern, A.SD_CN_RX.pattern)
    case("計算緩衝、sd-cli 自用量：兩邊一致", (R.SD_COMPUTE, R.SD_RAM_SELF), (A.SD_COMPUTE, A.SD_RAM_SELF))
    case("元件種類與取樣方法：兩邊一致", (set(R.SD_ROLES), R.SD_SAMPLERS), (set(A.SD_ROLES), A.SD_SAMPLERS))
    # ---- 隨附的預設清單
    rows = json.load(open(os.path.join(HERE, "sd_catalog_default.json"), encoding="utf-8"))
    case("預設清單：每一筆都過得了欄位檢查（含中國來源過濾）",
         [r["repo"] for r in rows if R.check_sd_fields(r)[1]], [])
    case("預設清單：每一筆都湊得出至少一組完整的檔", [r["repo"] for r in rows if not R.sd_resolve(r)], [])
    case("預設清單：有畫圖也有影片", sorted({r["kind"] for r in rows}), ["image", "video"])

    # ---- 代理：macOS／Windows 裝官方預編譯版（檔名照 master-1030 實際的 release）
    dg = {"digest": "sha256:" + "a" * 64, "browser_download_url": "https://x"}
    rels = [{"tag_name": "master-fbd42b6", "assets": []},
            {"tag_name": "master-1030-0a06712", "assets": [
                {"name": "sd-master-0a06712-bin-Darwin-macOS-26.6.2-arm64.zip", **dg},
                {"name": "sd-master-0a06712-bin-win-cuda12-x64.zip", **dg},
                {"name": "cudart-sd-bin-win-cu12-x64.zip", **dg},
                {"name": "sd-master-0a06712-bin-win-vulkan-x64.zip", "browser_download_url": "https://x"}]}]
    t, a, x = A.pick_release(rels, "macos-arm64", None, "sd")
    case("預編譯：macOS 的檔名帶系統版本也認得", (t, a["name"]), ("master-1030-0a06712", "sd-master-0a06712-bin-Darwin-macOS-26.6.2-arm64.zip"))
    t, a, x = A.pick_release(rels, "win-cuda12-x64", "cudart-sd-bin-win-cu12-x64.zip", "sd")
    case("預編譯：Windows CUDA 連執行階段一起", (a["name"], x["name"]), ("sd-master-0a06712-bin-win-cuda12-x64.zip", "cudart-sd-bin-win-cu12-x64.zip"))
    case("預編譯：官方沒公布 sha256 的檔不裝", A.pick_release(rels, "win-vulkan-x64", None, "sd")[0], None)

    # ---- 代理：規格檢查
    case("代理：絕對路徑不收", "檔名不合法" in (A.sd_check_spec({**spec, "parts": [{"role": "diffusion", "repo": "a/b", "path": "/etc/x.gguf"}]})[1] or ""), True)
    case("代理：不是模型檔的副檔名不收", "檔名不合法" in (A.sd_check_spec({**spec, "parts": [{"role": "diffusion", "repo": "a/b", "path": "x.sh"}]})[1] or ""), True)
    case("代理：中國來源沒勾內部測試，連 Hugging Face 都不去問就擋",
         A.sd_download({**spec, "parts": [{"role": "diffusion", "repo": "Qwen/Qwen-Image", "path": "q.gguf"}]})[0], 400)
    # ---- 代理：准入（三種記憶體架構）
    m = {"kind": "image", "parts": [{"size": 6 * G}, {"size": 3 * G}, {"size": 1 * G}]}

    def hw(gpus):
        return {"gpu": {"gpus": gpus, "vram_total": sum(g["vram_total"] for g in gpus),
                        "vram_used": sum(g["vram_used"] for g in gpus)}}

    def reg(mm):
        return {"machine": {"id": "t", "memory_model": mm}, "slots": [], "profiles": {}, "measure": {}}

    A.has_nvidia = lambda: True                    # 不要在測試機上真的跑 nvidia-smi
    A.ram_pool = lambda: {"total_gib": 120.0, "free_gib": 30.0, "used_gib": 90.0}
    A.swap_pool = lambda: {"total_gib": 0.0, "used_gib": 0.0}
    need, _ = A.sd_need(m, 1024, 1024)
    case("產圖要多少：權重 10＋緩衝 1＋每百萬像素 2", need, round(10 + 1 + 2 * 1.048576, 1))
    v = A.sd_admit(m, 1024, 1024, reg=reg("unified_single"), hw=hw([]))
    case("DGX Spark 剩 30：要 13.1＋自用 2，剩約 15 高於底線 12，放行", (v["allow"], v["pools"]["unified"]["worst"]), (True, 14.9))
    case("  sd-cli 的上限＝剩餘扣底線與自用（它自己分配也不會越線）", v["plan"]["max_vram"], 16.0)
    A.ram_pool = lambda: {"total_gib": 120.0, "free_gib": 20.0, "used_gib": 100.0}
    v = A.sd_admit(m, 1024, 1024, reg=reg("unified_single"), hw=hw([]))
    case("DGX Spark 剩 20：會跌破 12 底線，擋下並講統一記憶體會拖死整台", (v["allow"], "把整台拖死" in v["reasons"][0]), (False, True))
    A.ram_pool = lambda: {"total_gib": 31.0, "free_gib": 16.0, "used_gib": 15.0}
    v = A.sd_admit(m, 1024, 1024, reg=reg("unified_split"),
                   hw=hw([{"id": "0", "vram_total": 96 * G, "vram_used": 30 * G}]))
    case("AI 395：GPU 那塊夠，但系統那塊只剩 16、底線 15，擋下", (v["allow"], "系統記憶體不足" in v["reasons"][0]), (False, True))
    A.ram_pool = lambda: {"total_gib": 64.0, "free_gib": 50.0, "used_gib": 14.0}
    v = A.sd_admit(m, 1024, 1024, reg=reg("discrete"),
                   hw=hw([{"id": "0", "vram_total": 8 * G, "vram_used": 0}, {"id": "1", "vram_total": 12 * G, "vram_used": 0}]))
    case("獨立顯卡：挑最空的卡；放不下全部時權重放系統記憶體", (v["allow"], v["plan"]["gpu"], v["plan"]["offload"]), (True, "1", True))
    v = A.sd_admit(m, 1024, 1024, reg=reg("discrete"), hw=hw([{"id": "0", "vram_total": 4 * G, "vram_used": 0}]))
    case("獨立顯卡：連最大一個檔都放不下就擋", v["allow"], False)
    m2 = {**m, "measured": {"peak_gib": 20.0, "w": 1024, "h": 1024}}
    case("產過一次：改用實測的最高用量（比估算大時）", A.sd_need(m2, 1024, 1024)[0], 20.0)
    case("產過一次：尺寸更大照比例加上去", A.sd_need(m2, 2048, 1024)[0], round(20 + 2 * 1.048576, 1))
    # ---- sd-cli 參數
    mm = {"kind": "video", "parts": [{"role": "diffusion", "folder": "a", "file": "x.gguf"}, {"role": "llm", "folder": "b", "file": "g.gguf"}]}
    root = os.path.join(os.environ["HOME"], "sdroot")
    argv = A.sd_argv("/bin/sd-cli", mm, root, {"prompt": "a cat", "negative": "", "width": 768, "height": 512, "steps": 8, "cfg": 1.0,
                                               "sampler": "euler", "seed": 7, "frames": 33, "fps": 24}, "/o.webm",
                     {"offload": False, "max_vram": 20.5, "fa_off": True})
    case("影片：-M vid_gen、元件旗標、影格、上限、關 flash attention、大尺寸分塊解碼",
         [x in argv for x in ("vid_gen", "--diffusion-model", "--llm", "--video-frames", "--max-vram", "--vae-tiling")]
         + [argv[argv.index("--flash-attn") + 1]], [True] * 6 + ["off"])
    case("提示詞是一個獨立參數（不經過 shell）", argv[argv.index("-p") + 1], "a cat")
    # ---- 進度
    log = ("|####| 196/196 -   1.13GB/s\r|==>  | 1/4 -   2.10s/it\r|====>| 3/4 -   1.64s/it\n"
           "[I] loading tensors completed\r|##| 504/776 - 2.12GB/s")
    case("進度：只認總數等於步數的那幾行", A.parse_sd_progress(log, 4), {"step": 3, "total": 4, "s_per_it": 1.64})
    case("進度：it/s 換成每步秒數", A.parse_sd_progress("| 2/20 - 4.00it/s", 20)["s_per_it"], 0.25)
    # ---- 刪除：共用的檔留著
    A.registry()
    sroot = A.sd_root()
    for f, size in (("shared/t5.gguf", 5), ("a/flux.gguf", 7), ("b/chroma.gguf", 9)):
        os.makedirs(os.path.dirname(os.path.join(sroot, f)), exist_ok=True)
        open(os.path.join(sroot, f), "wb").write(b"x" * size)
    part = lambda folder, file, size: {"role": "x", "repo": "a/b", "path": file, "folder": folder, "file": file, "size": size}  # noqa: E731
    A._sd_save({"flux": {"title": "f", "kind": "image", "parts": [part("a", "flux.gguf", 7), part("shared", "t5.gguf", 5)]},
                "chroma": {"title": "c", "kind": "image", "parts": [part("b", "chroma.gguf", 9), part("shared", "t5.gguf", 5)]}})
    case("已下載：檔案齊就是可以用", [x["ready"] for x in A.sd_models()], [True, True])
    code, out = A.sd_delete("flux")
    case("刪除：只刪獨用的，共用的 t5 留著", (code, out["removed"], os.path.exists(os.path.join(sroot, "shared/t5.gguf")),
                                    os.path.exists(os.path.join(sroot, "a"))), (200, ["flux.gguf"], True, False))
    # ---- 看門狗：產的時候系統記憶體跌破底線就中止
    A.SD_JOBS_FILE = os.path.join(os.environ["HOME"], "jobs.json")
    A._GEN["jobs"] = []
    os.makedirs(A.GEN_DIR, exist_ok=True)
    slow = [sys.executable, "-c", "import time,sys\nfor i in range(1,5):\n print(f'| {i}/4 - 1.00s/it', flush=True); time.sleep(0.6)\n"
            f"open({os.path.join(A.GEN_DIR, 'w1.png')!r},'wb').write(b'png')"]
    j = {"id": "aaaa0001", "model": "chroma", "kind": "image", "steps": 4, "width": 512, "height": 512, "out": "w1.png",
         "state": "running", "phase": "載入模型中", "_cancel": False}
    A._GEN["jobs"].append(j)
    A._gen_sample = lambda: (20.0, {"0": 1.0})
    A._gen_run(j, slow, {}, "unified_single", {"ram": 12})
    case("正常產完：狀態完成、進度讀到 4/4", (j["state"], j.get("step")), ("done", 4))
    j2 = {**j, "id": "aaaa0002", "out": "w2.png", "state": "running"}
    A._GEN["jobs"].append(j2)
    A._gen_sample = lambda: (5.0, {})
    t0 = time.time()
    A._gen_run(j2, slow, {}, "unified_single", {"ram": 12})
    case("系統記憶體只剩 5（底線 12）：中止、講清楚原因，不等它跑完",
         (j2["state"], "已中止" in (j2["error"] or ""), time.time() - t0 < 2.5), ("failed", True, True))

    j3 = {**j, "id": "aaaa0003", "out": "w3.png", "state": "running"}
    A._GEN["jobs"].append(j3)
    A._gen_run(j3, ["/nonexistent/sd-cli"], {}, "unified_single", {"ram": 12})
    case("sd-cli 啟動不了：這件標失敗，不會一直卡在產生中擋住後面", (j3["state"], "啟動失敗" in j3["error"]), ("failed", True))

    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
