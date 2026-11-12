#!/usr/bin/env python3
"""推薦清單的自我檢查：放不放得下、放在哪、多快、挑哪個量化，以及跟代理同一條公式。
這支壞掉，畫面會推薦一顆載不起來的模型，或把能跑的說成放不下。跑法：python3 test_recommend.py
"""
import io
import json
import os
import struct
import sys
import tempfile

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


def state(mm, bw, **pools):
    return {"pools": {"memory_model": mm, **pools}, "chip": {"bandwidth_gbs": bw}}


AI395 = state("unified_split", 256, floors={"ram": 15, "vram": 4}, vram={"total_gib": 96}, ram={"total_gib": 31},
              gpus=[{"id": "0", "total_gib": 96}])
SPARK = state("unified_single", 273, floors={"ram": 12}, unified={"total_gib": 120}, gpus=[])
ADA2 = state("discrete", 960, floors={"ram": 8, "vram": 2}, ram={"total_gib": 212},
             gpus=[{"id": "0", "total_gib": 48}, {"id": "1", "total_gib": 48}])
NOGPU = state("discrete", None, floors={"ram": 8, "vram": 2}, ram={"total_gib": 64}, gpus=[])

DENSE = {"ctx_train": 131072, "kv_full_bpt": 131072, "kv_swa_bpt": 0, "swa_window": 0, "layers": 60,
         "active_frac": 1.0, "moe": False}
MOE = {"ctx_train": 131072, "kv_full_bpt": 49152, "kv_swa_bpt": 0, "swa_window": 0, "layers": 36,
       "active_frac": 0.05, "moe": True, "expert_layers": 36, "expert_layer_frac": 0.026, "expert_used_frac": 0.03125}


def q(quant, size):
    return {"stem": f"m-{quant}", "quant": quant, "size_gib": size}


def main():
    # ---- 容量
    c = R.capacity(AI395)
    case("AI 395：快速層＝GPU 那塊扣顯存底線", c["fast"], 92.0)
    case("AI 395：統一記憶體沒有備援層", c["slow"], 0.0)
    case("DGX Spark：單一池扣系統底線與引擎", R.capacity(SPARK)["fast"], 106.5)
    c = R.capacity(ADA2)
    case("2 張 48G：快速層＝兩張加總扣底線", c["fast"], 94.0)
    case("2 張 48G：備援層＝系統記憶體扣底線與引擎", c["slow"], 201.5)
    case("2 張 48G：流暢稠密模型上限照估算往下取整（100B 以上取到 10B）", c["max_dense_b"], 94)
    # 兩張 RTX PRO 5000（1344 GB/s）實測跑 128B 稠密模型約 15 tok/s：標題不能寫成比它小的 120B
    c5000 = R.capacity(state("discrete", 1344, floors={"ram": 8, "vram": 2}, ram={"total_gib": 123},
                             gpus=[{"id": "0", "total_gib": 48}, {"id": "1", "total_gib": 48}]))
    case("2 張 RTX PRO 5000：流暢上限 130B，涵蓋正在跑的 128B", c5000["max_dense_b"], 130)
    c = R.capacity(NOGPU)
    case("沒有 GPU：快速層＝系統記憶體扣底線與引擎、標成 CPU 執行", (c["fast"], c["cards"], c.get("cpu_only")), (53.5, 0, True))
    case("  頻寬沒填用假設的系統記憶體頻寬，並標明是假設", (c["bandwidth_gbs"], c["bandwidth_src"]), (R.DEFAULT_RAM_BW, "假設值"))

    # ---- 單一量化的規劃
    p = R.plan_quant(R.capacity(AI395), DENSE, q("Q4_K_M", 17.4))
    case("31B 稠密在 AI 395：放得下", p["place"], "全部放進 GPU 那一塊")
    case("31B 稠密在 AI 395：256 GB/s 跑不到 10 tok/s，降為「能跑」不算流暢", p["level"], "ok")
    p = R.plan_quant(R.capacity(SPARK), DENSE, q("Q8_0", 130))
    case("DGX Spark 放不進統一記憶體：改列「要用硬碟」，不是直接說放不下", p["level"], "disk")
    case("  溢出到硬碟的量＝權重減掉記憶體放得下的", p["split"]["disk"], round(130 - (106.5 - 1 - 1), 1))   # 要用硬碟時上下文縮到 8K（KV 1 GiB）
    p = R.plan_quant(R.capacity(AI395), DENSE, q("Q8_0", 130))
    case("AI 395（BIOS 切兩塊）放不下：也改列「要用硬碟」（GPU 那塊 90、系統那塊頁快取 31-15-2.5、其餘從 SSD）",
         (p["level"], p["split"]["gpu"], p["split"]["ram"], p["split"]["disk"]), ("disk", 92.0, 13.5, 26.5))
    case("  說明寫 GPU 那一塊與系統那塊的頁快取", "放不進記憶體" in p["place"], True)
    p = R.plan_quant(R.capacity(ADA2), MOE, q("MXFP4", 120))
    case("2 張 48G 放不下的 MoE：專家搬到系統記憶體、等級為能跑", (p["level"], p["cpu_moe"] > 0), ("ok", True))
    case("搬到系統記憶體的速度比全放卡上慢", p["tok_s"] < R.plan_quant(R.capacity(ADA2), MOE, q("Q2_K", 60))["tok_s"], True)
    p = R.plan_quant(R.capacity(ADA2), DENSE, q("Q8_0", 130))
    case("稠密模型放不下顯示卡：不搬專家，改列「要用硬碟」（檔案映射，上下文縮到 8K）", p["level"], "disk")
    case("  分層：顯示卡放滿、其餘在系統記憶體當頁快取、沒有溢出到硬碟",
         (p["split"]["gpu"], p["split"]["ram"], p["split"]["disk"]), (94.0, round(130 - (94 - 1 - 1), 1), 0.0))
    p = R.plan_quant(R.capacity(NOGPU), MOE, q("Q4_K_M", 10))
    case("沒有 GPU：MoE 小模型用 CPU 跑得動，放系統記憶體", (p["level"] in ("smooth", "ok"), p["place"], p["split"]["gpu"]),
         (True, "全部放進系統記憶體（CPU 執行）", 0.0))
    case("頻寬未知：速度回 None、不硬掰數字",
         R.plan_quant(R.capacity(state("unified_single", None, floors={"ram": 12}, unified={"total_gib": 120})), DENSE,
                      q("Q4_K_M", 17))["tok_s"], None)

    # ---- 與代理同一條公式
    prof = {"kv_full_bpt": 81920, "kv_swa_bpt": 1638400, "swa_window": 1024}
    case("KV 公式與代理 llama_kv_gib 一致", round(R.kv_gib(prof, 32768), 6), round(A.llama_kv_gib(prof, 32768), 6))
    m = {"size_gib": 120, "mmproj_gib": 0, "moe": True, "expert_layers": 36, "expert_layer_frac": 0.026, **MOE}
    cards = [{"id": "0", "total_gib": 48, "free_gib": 48}, {"id": "1", "total_gib": 48, "free_gib": 48}]
    agent_n = A.auto_cpu_moe(m, {"memory_model": "discrete", "vram_floor_gib": 2}, cards, None, 32768, 1, 0.0)
    rec_n = R.plan_quant(R.capacity(ADA2), MOE, q("MXFP4", 120))["cpu_moe"]
    case("自動分層層數：推薦畫面與代理實際換模型算出一樣", rec_n, agent_n)
    case("統一記憶體機不做自動分層", A.auto_cpu_moe(m, {"memory_model": "unified_single"}, cards, None, 32768, 1, 0.0), 0)
    est = A.llama_est({**m, "size_gib": 120}, {"memory_model": "discrete"}, 32768, 1, agent_n)
    case("搬出去的量從顯存扣掉、加到系統記憶體", round(est["vram"] + est["ram"], 1), round(120 + 1 + 2.5, 1))
    raw = {"exe": "/x/llama-server", "model": "/m.gguf", "port": 8090, "alias": "a", "cpu_moe": 5, "mmap": True}
    case("自動分層寫進 llama-server 參數", "--n-cpu-moe" in A.llama_argv(raw, {"machine": {}}), True)
    repo = "unsloth/gemma-4-E4B-it-GGUF"
    case("已下載的資料夾名與代理下載時一致", R.repo_folder(repo), A._slug("unsloth--gemma-4-E4B-it"))

    # ---- 挑量化
    plans = [R.plan_quant(R.capacity(ADA2), DENSE, x) for x in
             (q("Q4_0", 16), q("Q4_1", 17.5), q("Q4_K_M", 17.4), q("Q6_K", 23.5), q("Q8_0", 30.4), q("BF16", 58))]
    case("夠快又放得下：挑 6 位元，不挑 8 位元與 BF16", R.pick(plans)["quant"], "Q6_K")
    plans = [R.plan_quant(R.capacity(AI395), DENSE, x) for x in (q("Q4_0", 16), q("Q4_1", 17.5), q("Q4_K_M", 17.4), q("Q6_K", 23.5))]
    case("都不夠快：退到 4 位元，K 系列優先於舊式 Q4_0／Q4_1", R.pick(plans)["quant"], "Q4_K_M")
    plans = [R.plan_quant(R.capacity(ADA2), DENSE, x) for x in (q("Q8_0", 30), q("BF16", 58))]
    case("repo 只有 8 位元以上：挑 8 位元", R.pick(plans)["quant"], "Q8_0")
    case("只有一個檔、看不出量化：當 4 位元那一級", R.quant_bits(None), 4)
    only = R.pick([R.plan_quant(R.capacity(AI395), DENSE, q("Q4_K_M", 200))])
    case("AI 395 只有放不下的量化：照樣給一條能跑的路（用硬碟）", (only or {}).get("level"), "disk")

    lap8 = state("discrete", 25, floors={"ram": 4}, ram={"total_gib": 7.7}, gpus=[])
    small = R.plan_quant(R.capacity(lap8), DENSE, q("Q4_K_M", 1.0))
    case("8 GB 文書筆電（只有 CPU）：小模型放得進記憶體，必要時縮上下文", (small["level"] in ("smooth", "ok"), small["ctx"] <= 32768), (True, True))

    # ---- 推薦結果：已下載、排序
    cat = [{"repo": "a/big-GGUF", "title": "大", "use": "chat", "profile": DENSE, "quants": [q("Q4_K_M", 200)]},
           {"repo": "b/small-GGUF", "title": "小", "use": "chat", "profile": DENSE, "quants": [q("Q4_K_M", 5)]},
           {"repo": "c/none-GGUF", "title": "還沒讀"}]
    lib = {"free_gib": 500, "gguf": [{"id": "b--small/m-Q4_K_M", "in_use": None}], "downloads": []}
    r = R.recommend(ADA2, lib, cat)
    case("放得下的排前面、放不下的不進前三", r["top"], ["b/small-GGUF"])
    case("認得出已下載", r["items"][1]["installed"], "m-Q4_K_M")
    case("還沒讀到大小的標「待更新」", r["items"][2]["level"], "unknown")
    case("磁碟不夠時下載鈕要擋", R.recommend(ADA2, {"free_gib": 8}, cat)["items"][1]["plans"][0]["fits_disk"], False)

    # ---- GGUF 張量：從檔頭算出每個 token 讀多少
    def gguf(tensors, kv, total):
        b = io.BytesIO()
        s = lambda t: struct.pack("<Q", len(t)) + t.encode()  # noqa: E731
        b.write(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensors), len(kv)))
        for k, v in kv.items():
            b.write(s(k) + (struct.pack("<I", 8) + s(v) if isinstance(v, str) else struct.pack("<I", 4) + struct.pack("<I", v)))
        for name, off in tensors:
            b.write(s(name) + struct.pack("<I", 1) + struct.pack("<Q", 1) + struct.pack("<I", 0) + struct.pack("<Q", off))
        return b.getvalue(), total
    kv = {"general.architecture": "t", "t.expert_count": 8, "t.expert_used_count": 2, "t.block_count": 2}
    tens = [("token_embd.weight", 0), ("output.weight", 100), ("blk.0.attn_q.weight", 200),
            ("blk.0.ffn_up_exps.weight", 300), ("blk.1.ffn_up_exps.weight", 700)]
    raw, total = gguf(tens, kv, 1100)
    meta = A.gguf_meta(io.BytesIO(raw), tensors=True)
    wp = A.weight_profile([(meta, meta["_data_start"] + 1100)])     # 權重資料區共 1100 位元組
    # 張量：embd 100、output 100、attn 100、兩層專家各 400 → 總 1100；每 token 讀 output＋attn＋專家 1/4
    case("MoE 每個 token 讀的比例（不算查表的 embd、專家只讀選中的）", wp["active_frac"], round((100 + 100 + 800 / 4) / 1100, 4))
    case("專家層數與每層比例", (wp["expert_layers"], wp["expert_layer_frac"]), (2, round(400 / 1100, 6)))
    case("檔頭被截斷：回 None 讓呼叫端抓更多，不是給錯數字", A.gguf_meta(io.BytesIO(raw[:len(raw) - 5]), tensors=True), None)
    case("分片路徑展開", A.shard_paths("/m/x-00001-of-00003.gguf")[-1], "/m/x-00003-of-00003.gguf")

    # ---- Codex 審查抓到的情況（PR #20）
    case("頻寬未知：放得下也不標流暢", R.plan_quant(R.capacity(state("unified_single", None, floors={"ram": 12}, unified={"total_gib": 120})),
                                          DENSE, q("Q4_K_M", 17))["level"], "ok")
    c = R.capacity(AI395)
    case("流暢上限把頻寬算進去：AI 395 放得下 150B，但流暢只到 25B", (c["max_fit_b"], c["max_dense_b"]), (150, 25))
    case("頻寬未知：上限只照容量、標明速度未知", R.capacity(state("unified_single", None, floors={"ram": 12}, unified={"total_gib": 120}))["speed_known"], False)
    only16 = [R.plan_quant(R.capacity(ADA2), DENSE, x) for x in (q("BF16", 16), q("F32", 32))]
    case("repo 只有 16 位元：放得下就推它", (R.pick(only16) or {}).get("quant"), "BF16")
    cat2 = [{"repo": "d/two-GGUF", "title": "兩種", "use": "chat", "profile": DENSE,
             "quants": [q("Q4_K_M", 17), q("Q8_0", 200)]}]
    r2 = R.recommend(ADA2, {"free_gib": 900, "gguf": [{"id": "d--two/m-Q8_0", "in_use": None}]}, cat2)
    case("裝了放不下的大量化，不會把放得下的小量化一起藏起來", r2["items"][0]["level"], "smooth")
    mm = [{"path": "mmproj-F32.gguf", "gib": 2.1}, {"path": "mmproj-BF16.gguf", "gib": 1.1}, {"path": "x-mmproj-f16.gguf", "gib": 1.1},
          {"path": "mmproj-Q8_0.gguf", "gib": 0.6}]
    case("投影檔：有 F16 用 F16（不是 BF16、也不是最小的）", R.pick_mmproj(mm)["path"], "x-mmproj-f16.gguf")
    case("投影檔：沒有 F16 才挑最小的", R.pick_mmproj(mm[:2] + mm[3:])["path"], "mmproj-Q8_0.gguf")
    vis = [{"repo": "v/vis-GGUF", "title": "看圖", "use": "vision", "profile": DENSE, "quants": [q("Q4_K_M", 5)], "mmproj": mm}]
    rv = R.recommend(ADA2, {"free_gib": 900}, vis)["items"][0]
    case("推薦算記憶體用的就是要下載的那個投影檔", (rv["mmproj_pick"]["path"], rv["plans"][0]["need_gib"] > 5 + 1.1), ("x-mmproj-f16.gguf", True))
    case("檔名後面才有 mmproj 也認得是投影檔", (A.is_mmproj("X-OCR-v1-mmproj-f16.gguf"), A.is_mmproj("model-Q4_K_M.gguf")), (True, False))
    case("MI210／MI250 認不出是哪一張：頻寬回未知", (A.spec_bandwidth("AMD Instinct MI210／MI250"), A.spec_bandwidth("AMD Instinct MI50／MI60")), (None, 1024))
    case("「檔名要含」只收安全字元", R.check_fields({"repo": "a/b-GGUF", "title": "x", "match": "../x"})[1] is not None, True)
    d2 = tempfile.mkdtemp()
    first = os.path.join(d2, "m-00001-of-00002.gguf")
    second = os.path.join(d2, "m-00002-of-00002.gguf")
    for pth in (first, second):
        open(pth, "wb").write(b"not gguf")
    A._profile_cached(first)
    before = len(A._META_CACHE)
    open(second, "wb").write(b"changed, longer")
    A._profile_cached(first)
    case("分片模型：後面的分片換過，快取跟著失效", len(A._META_CACHE), before + 1)

    # ---- 用硬碟執行（disk）與分層（split）
    p = R.plan_quant(R.capacity(ADA2), DENSE, q("Q4_K_M", 17.4))
    case("全放顯示卡：分層全部算在顯示卡", (p["split"]["gpu"], p["split"]["ram"], p["split"]["disk"]), (p["need_gib"], 0.0, 0.0))
    p = R.plan_quant(R.capacity(ADA2), MOE, q("MXFP4", 120))
    case("專家搬到系統記憶體：顯示卡＋系統記憶體＝總需求", round(p["split"]["gpu"] + p["split"]["ram"], 1), p["need_gib"])
    big = q("Q8_0", 400)
    fast_disk = R.plan_quant(R.capacity(ADA2), DENSE, big, hdd=False)
    slow_disk = R.plan_quant(R.capacity(ADA2), DENSE, big, hdd=True)
    case("放不進記憶體：等級 disk，溢出量＝權重扣掉顯示卡與系統記憶體放得下的",
         (fast_disk["level"], fast_disk["split"]["disk"]), ("disk", round(400 - (94 - 1 - 1) - 201.5, 1)))
    case("  速度用硬碟讀取頻寬估，機械硬碟更慢", 0 < slow_disk["tok_s"] < fast_disk["tok_s"] < 1, True)
    moe_disk = R.plan_quant(R.capacity(SPARK), MOE, big)
    dense_disk = R.plan_quant(R.capacity(SPARK), DENSE, big)
    case("  MoE 只算被選中的專家：同樣大小比稠密快", (moe_disk["level"], moe_disk["tok_s"] > dense_disk["tok_s"]), ("disk", True))
    case("  DGX Spark 用硬碟：快速層之外的權重全部算溢出", dense_disk["split"]["disk"], round(400 - (106.5 - 1 - 1), 1))
    case("AI 395（BIOS 切兩塊）放不下的一律給 disk",
         {R.plan_quant(R.capacity(AI395), DENSE, q("Q8_0", n))["level"] for n in (100, 200, 400)}, {"disk"})
    moe395 = R.plan_quant(R.capacity(AI395), MOE, q("Q4_K_M", 200))
    case("AI 395 的 MoE：注意力與共用放 GPU 那塊，專家留在映射、溢出從 SSD 讀",
         (moe395["level"], moe395.get("tiered"), moe395["split"]["ram"] <= 13.5, "GPU 那一塊" in moe395["place"]), ("disk", True, True, True))
    p = R.plan_quant(R.capacity(NOGPU), DENSE, big)
    case("沒有 GPU 的機器：放不下就用硬碟執行（權重留在系統記憶體當頁快取、其餘從 SSD 讀）",
         (p["level"], p["split"]["gpu"], p["split"]["ram"] > 0, p["split"]["disk"] > 0), ("disk", 0.0, True, True))
    # ---- MoE 依張量種類分層（注意力與共用放顯卡、專家留在映射記憶體、溢出從 SSD 讀），跟代理 moe_disk_layout 同一個放法
    small_card = state("discrete", 960, floors={"ram": 8, "vram": 2}, ram={"total_gib": 64}, gpus=[{"id": "0", "total_gib": 24}])
    p = R.plan_quant(R.capacity(small_card), MOE, q("Q4_K_M", 200))
    case("MoE 放不下：注意力與共用放顯卡、專家分到記憶體與 SSD", (p["level"], p.get("tiered"), p["split"]["disk"] > 0), ("disk", True, True))
    flat = R._disk_plan(R.capacity(small_card), 200, 1 + R.kv_gib(MOE, 32768) + 0, MOE["active_frac"], None, MOE["layers"] * R.LAYER_MS / 1000)
    case("  依張量分層比整層切快（每 token 共用全讀、專家只讀被選中的）", p["tok_s"] > flat["tok_s"], True)
    p2 = R.plan_quant(R.capacity(SPARK), MOE, q("Q4_K_M", 300))
    case("  DGX Spark：專家全部留映射記憶體，只有共用部分常駐", (p2.get("tiered"), p2["cpu_moe"]), (True, MOE["expert_layers"]))
    tiny_vram = state("discrete", 400, floors={"ram": 8, "vram": 2}, ram={"total_gib": 64}, gpus=[{"id": "0", "total_gib": 6}])
    pt = R.plan_quant(R.capacity(tiny_vram), DENSE, big)
    case("32K 的 KV 放不進顯示卡：先把上下文縮到 8K，再用硬碟跑（跟 LocModel 一樣，不直接說不行）", (pt["level"], pt["ctx"]), ("disk", 8192))
    no_vram = state("discrete", 400, floors={"ram": 8, "vram": 2}, ram={"total_gib": 64}, gpus=[{"id": "0", "total_gib": 2.5}])
    case("  連 8K 的 KV 與計算緩衝都放不進顯示卡：才說放不下", R.plan_quant(R.capacity(no_vram), DENSE, big)["level"], "no")
    case("只能用硬碟跑時，建議下載最快的那個量化（一般人的電腦也要跑得起來）", R.pick([slow_disk, fast_disk])["stem"], fast_disk["stem"])
    case("每個量化都帶第一次載入的估計秒數", fast_disk["load_est_sec"], R.load_seconds(400, False))
    rd = R.recommend(SPARK, {"free_gib": 900}, [{"repo": "z/huge-GGUF", "title": "巨大", "use": "chat", "profile": DENSE,
                                                 "quants": [q("Q4_K_M", 300)]}])
    case("推薦清單：這台沒有放得進記憶體的，用硬碟跑的照樣推薦（標 disk）", (rd["items"][0]["level"], rd["top"]), ("disk", ["z/huge-GGUF"]))

    # ---- Hugging Face 量化清單附等級、已安裝清單附等級
    info = {"repo": "a/b-GGUF", "choices": [{"stem": "m-Q4_K_M", "quant": "Q4_K_M", "size_gib": 4.6, "aux": False},
                                            {"stem": "m-Q8_0", "quant": "Q8_0", "size_gib": 8.5, "aux": False},
                                            {"stem": "mtp", "quant": None, "size_gib": 0.1, "aux": True}],
            "mmproj_sizes": {"mmproj-F16.gguf": 0.8}}
    h = R.plan_hf(ADA2, json.loads(json.dumps(info)))
    case("拿不到結構：用稠密假設、每個量化都標 guess", bool(all(c["plan"]["guess"] for c in h["choices"]) and h["guess"]), True)
    case("  plan 的欄位照合約", set(h["choices"][0]["plan"]) >= {"level", "tok_s", "place", "need_gib", "split", "guess"}, True)
    case("  整體附建議的量化（4～6 位元裡流暢的、不挑輔助檔）", h["pick"], "m-Q4_K_M")
    h2 = R.plan_hf(ADA2, {**json.loads(json.dumps(info)), "profile": DENSE})
    case("讀得到檔頭：不標 guess", any(c["plan"]["guess"] for c in h2["choices"]), False)
    h3 = R.plan_hf(ADA2, {**json.loads(json.dumps(info)), "profile": DENSE}, True)
    case("存放區是機械硬碟：第一次載入估得比較久", h3["choices"][0]["plan"]["load_est_sec"] > h2["choices"][0]["plan"]["load_est_sec"], True)
    case("  算記憶體有算進要下載的投影檔", h2["choices"][0]["plan"]["need_gib"] > R.plan_quant(R.capacity(ADA2), DENSE, q("Q4_K_M", 4.6))["need_gib"], True)
    lib = {"hdd": True, "gguf": [{"id": "f/m-Q4_K_M", "quant": "Q4_K_M", "size_gib": 17.4, "mmproj_gib": 0, "load_sec": 41, **DENSE}]}
    R.plan_installed(ADA2, lib)
    pl = lib["gguf"][0]["plan"]
    case("已安裝：附 plan、機械硬碟的載入估計、實測載入秒數",
         (pl["level"], pl["load_est_sec"], pl["load_sec"], pl["guess"]), ("smooth", R.load_seconds(17.4, True), 41, False))

    # ---- 管理者送來的欄位
    case("中國來源也能加入推薦清單（照列、畫面標示）", R.check_fields({"repo": "unsloth/Qwen3-8B-GGUF", "title": "x"})[1], None)
    case("中國來源在推薦結果裡標 cn_origin", (R.is_cn({"repo": "unsloth/Qwen3-8B-GGUF"}), R.is_cn({"repo": "unsloth/gemma-4-E4B-it-GGUF"}),
                                            R.is_cn({"repo": "x/y", "profile": {"cn_origin": True}})), (True, False, True))
    case("名稱格式要對", R.check_fields({"repo": "../../etc", "title": "x"})[1] is not None, True)
    f, err = R.check_fields({"repo": "a/b-GGUF", "title": "名稱", "tags": "繁中、看圖", "use": "code"})
    case("標籤可用頓號分隔", (err, f["tags"]), (None, ["繁中", "看圖"]))
    with open(os.path.join(HERE, "catalog_default.json"), encoding="utf-8") as fh:
        default = json.load(fh)
    # 這是給一般人用的軟體，推薦清單放進所有熱門模型（含中國來源，只標示不遮蔽）；但非中國來源的選擇要比 LocModel 多
    cn = [e["repo"] for e in default if R.is_cn(e)]
    case("隨附的推薦清單含中國來源，且檔頭或名稱都判得出來",
         bool(cn) and all((e.get("profile") or {}).get("cn_origin") for e in default if e["repo"] in cn), True)
    case("隨附的推薦清單：非中國來源至少 40 顆，每個用途都有", (len(default) - len(cn) >= 40,
         {u for u in R.USES} <= {e["use"] for e in default if not R.is_cn(e)}), (True, True))
    case("隨附的推薦清單：入門小模型（2 GiB 以下的量化）至少 5 顆",
         sum(1 for e in default if min((q["size_gib"] for q in e["quants"]), default=99) <= 2) >= 5, True)
    case("隨附的推薦清單每一筆都讀到了大小與架構", [e["repo"] for e in default if not e.get("quants") or not e.get("profile")], [])

    # ---- 推薦清單的增改刪
    d = tempfile.mkdtemp()
    cat = R.Catalog(os.path.join(d, "c.json"), os.path.join(HERE, "catalog_default.json"))
    n0 = len(cat.load())
    cat.upsert({"repo": "x/y-GGUF", "title": "新的", "use": "chat"})
    case("新增後多一筆、排在最後", (len(cat.load()), cat.load()[-1]["title"]), (n0 + 1, "新的"))
    cat.move("x/y-GGUF", -1)
    case("上移一格", cat.load()[-2]["repo"], "x/y-GGUF")
    cat.upsert({"repo": "x/y-GGUF", "title": "改名"})
    case("編輯不會重複新增", (len(cat.load()), cat.load()[-2]["title"]), (n0 + 1, "改名"))
    case("移除", (cat.delete("x/y-GGUF"), len(cat.load())), (True, n0))
    if os.name != "nt":                    # Windows 靠 ACL（安裝程式限縮資料夾權限），沒有 Unix 的權限位元
        case("清單檔權限 0600", oct(os.stat(os.path.join(d, "c.json")).st_mode & 0o777), "0o600")

    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
