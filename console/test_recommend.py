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
    case("2 張 48G：流暢稠密模型上限是好記的整數", c["max_dense_b"] in R.NICE_B, True)
    case("沒有 GPU：快速層 0、標題講清楚", (R.capacity(NOGPU)["fast"], R.capacity(NOGPU)["cards"]), (0.0, 0))

    # ---- 單一量化的規劃
    p = R.plan_quant(R.capacity(AI395), DENSE, q("Q4_K_M", 17.4))
    case("31B 稠密在 AI 395：放得下", p["place"], "全部放進 GPU 那一塊")
    case("31B 稠密在 AI 395：256 GB/s 跑不到 10 tok/s，降為「能跑」不算流暢", p["level"], "ok")
    p = R.plan_quant(R.capacity(SPARK), DENSE, q("Q8_0", 130))
    case("放不下就說放不下", p["level"], "no")
    p = R.plan_quant(R.capacity(ADA2), MOE, q("MXFP4", 120))
    case("2 張 48G 放不下的 MoE：專家搬到系統記憶體、等級為能跑", (p["level"], p["cpu_moe"] > 0), ("ok", True))
    case("搬到系統記憶體的速度比全放卡上慢", p["tok_s"] < R.plan_quant(R.capacity(ADA2), MOE, q("Q2_K", 60))["tok_s"], True)
    p = R.plan_quant(R.capacity(ADA2), DENSE, q("Q8_0", 130))
    case("稠密模型放不下就不搬（整層搬出去太慢，不推薦）", p["level"], "no")
    p = R.plan_quant(R.capacity(NOGPU), MOE, q("Q4_K_M", 10))
    case("沒有 GPU：任何模型都不推薦", p["level"], "no")
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
    case("全放不下：不推薦任何量化", R.pick([R.plan_quant(R.capacity(SPARK), DENSE, q("Q4_K_M", 200))]), None)

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

    # ---- 管理者送來的欄位
    case("中國來源不能加入推薦清單", R.check_fields({"repo": "unsloth/Qwen3-8B-GGUF", "title": "x"})[1] is not None, True)
    case("名稱格式要對", R.check_fields({"repo": "../../etc", "title": "x"})[1] is not None, True)
    f, err = R.check_fields({"repo": "a/b-GGUF", "title": "名稱", "tags": "繁中、看圖", "use": "code"})
    case("標籤可用頓號分隔", (err, f["tags"]), (None, ["繁中", "看圖"]))
    with open(os.path.join(HERE, "catalog_default.json"), encoding="utf-8") as fh:
        default = json.load(fh)
    case("隨附的推薦清單沒有中國來源", [e["repo"] for e in default if any(c in e["repo"].lower() for c in R.CN_ORIGIN)
                                       or (e.get("profile") or {}).get("cn_origin")], [])
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
    case("清單檔權限 0600", oct(os.stat(os.path.join(d, "c.json")).st_mode & 0o777), "0o600")

    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
