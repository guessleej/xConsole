#!/usr/bin/env python3
"""准入公式的自我檢查。這支壞掉代表主控台會放行一個會把機器弄死的載入，
所以它比其他測試都重要。跑法：python3 test_admit.py（不需要任何套件）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("HOME", tempfile.mkdtemp())  # 不要碰到真的 ~/.xconsole

import xconsole_agent as A  # noqa: E402

GIB = A.GIB


def fake_hw(vram_total_gib, vram_used_gib, gpus=None):
    gpus = gpus or [{"id": "0", "name": "t", "vram_total": vram_total_gib * GIB,
                     "vram_used": vram_used_gib * GIB}]
    return {"gpu": {"count": len(gpus), "gpus": gpus,
                    "vram_total": sum(g["vram_total"] for g in gpus),
                    "vram_used": sum(g["vram_used"] for g in gpus)},
            "cpu": {}, "ram": {}}


def reg_for(model, slots, ram_total_gib, ram_free_gib):
    # 把 ram_pool 換成固定值，才能測到公式本身而不是測這台 Mac 的記憶體
    A.ram_pool = lambda: {"total_gib": ram_total_gib, "free_gib": ram_free_gib,
                          "used_gib": ram_total_gib - ram_free_gib}
    A.swap_pool = lambda: {"total_gib": 0.0, "used_gib": 0.0}
    return {"machine": {"id": "t", "memory_model": model}, "slots": slots,
            "profiles": {}, "measure": {}}


def case(name, got, want):
    mark = "通過" if got == want else "失敗"
    print(f"[{mark}] {name}（得到 {got}，預期 {want}）")
    return got == want


def main():
    ok = True

    # 1. Strix Halo：VRAM 很鬆，但系統 RAM 只剩 16 GiB，再吃 14 就跌破 15 的底線 → 必須擋
    slots = [{"id": "brain", "role": "主腦", "unit": "x.service",
              "est_vram_gib": 20, "est_ram_gib": 14}]
    r = A.admit("brain", reg_for("unified_split", slots, 31, 16), fake_hw(96, 40))
    ok &= case("Strix Halo：VRAM 夠但系統 RAM 會跌破底線 → 擋", r["allow"], False)
    ok &= case("  擋的理由講的是系統記憶體",
               any("系統記憶體不足" in x for x in r["reasons"]), True)

    # 2. 同一台，系統 RAM 充裕 → 放行
    r = A.admit("brain", reg_for("unified_split", slots, 31, 30), fake_hw(96, 40))
    ok &= case("Strix Halo：兩個池子都夠 → 放行", r["allow"], True)

    # 3. 統一記憶體（DGX）：VRAM 與 RAM 是同一塊，不可以分開算成「各自都夠」
    slots2 = [{"id": "big", "role": "主腦", "unit": "x.service",
               "est_vram_gib": 60, "est_ram_gib": 20}]
    r = A.admit("big", reg_for("unified_single", slots2, 122, 70), fake_hw(122, 52))
    ok &= case("DGX：60+20 要 80，只剩 70 → 擋", r["allow"], False)
    r = A.admit("big", reg_for("unified_single", slots2, 122, 110), fake_hw(122, 12))
    ok &= case("DGX：剩 110 夠放 80 → 放行", r["allow"], True)

    # 4. 多卡獨立：兩個池子分開算
    slots3 = [{"id": "farm", "role": "推論", "unit": "x.service",
               "est_vram_gib": 28, "est_ram_gib": 6}]
    r = A.admit("farm", reg_for("discrete", slots3, 220, 100), fake_hw(256, 200))
    ok &= case("多卡：顯存只剩 56、要 28 → 放行", r["allow"], True)
    r = A.admit("farm", reg_for("discrete", slots3, 220, 100), fake_hw(256, 240))
    ok &= case("多卡：顯存只剩 16、要 28 → 擋", r["allow"], False)

    # 5. KV 的大小是「每 token 幾位元組 × 上下文 × 並行」，不同引擎差很多。
    #    這顆 Qwen3.8-Flash-Next 是 GDN 混合注意力，int8 下 1,056 B/token，
    #    128K 全開也只有 0.13 GiB —— 實測佐證：.27 把 max-context 由 32,768 改成
    #    131,072，載入後剩餘 VRAM 由 33,026/33,416 MiB 變成 33,094 MiB，等於沒變。
    cfg = {"max_context": 131072, "kv": "int8", "kv_grow": False, "parallel": 1}
    kv = A.kv_worst_gib(cfg)
    ok &= case("131072×1 的 int8 KV 約 0.13 GiB", 0.1 <= kv <= 0.2, True)

    # 傳統全注意力模型的 KV 才是會吃掉顯存的那種，欄位可以自己指定每 token 位元組數
    cfg_big = {"max_context": 131072, "kv": "int8", "kv_grow": False, "parallel": 2,
               "kv_bytes_per_cell": 262144}
    kv_big = A.kv_worst_gib(cfg_big)
    ok &= case("全注意力 256 KiB/token、128K×2 → 約 64 GiB", 60 <= kv_big <= 68, True)

    tmp = tempfile.mkdtemp()
    cfgpath = os.path.join(tmp, "strata.json")
    base = {"args": ["--max-context", "131072", "--kv", "int8"], "parallel": 1, "port": 8081}
    json.dump(base, open(cfgpath, "w"))
    slots4 = [{"id": "s", "role": "主腦", "unit": "x.service", "kind": "strata",
               "config": cfgpath, "est_vram_gib": 64, "est_ram_gib": 14}]
    r = A.admit("s", reg_for("unified_split", slots4, 31, 30), fake_hw(96, 0))
    ok &= case("GDN 模型的 131072 → 放行（64 + 0.13 放得進 96）", r["allow"], True)

    # 同樣 131072，但換成全注意力的 KV 成本 → 必須擋
    slots5 = [{"id": "s2", "role": "主腦", "unit": "x.service", "kind": "strata",
               "config": cfgpath, "est_vram_gib": 64, "est_ram_gib": 14,
               "kv_bytes_per_cell": 262144}]
    r = A.admit("s2", reg_for("unified_split", slots5, 31, 30), fake_hw(96, 0))
    ok &= case("全注意力的 131072 → 擋（64 + 32 放不進 96 還要留底線）", r["allow"], False)

    # 6. 邊界：剛好踩在底線上要擋，底線加一點點要放行
    slots6 = [{"id": "edge", "role": "測", "unit": "x.service",
               "est_vram_gib": 0, "est_ram_gib": 15}]
    r = A.admit("edge", reg_for("unified_split", slots6, 31, 30.0), fake_hw(96, 0))
    ok &= case("載完剛好剩 15.0（等於底線）→ 放行", r["allow"], True)
    r = A.admit("edge", reg_for("unified_split", slots6, 31, 29.9), fake_hw(96, 0))
    ok &= case("載完剩 14.9（低於底線）→ 擋", r["allow"], False)

    # 7. 換模型後畫面上的名稱：取權重檔名，不是資料夾名
    ok &= case("權重檔名 → 模型名稱與量化",
               A.pretty_model("/m/unsloth-UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf"),
               ("Qwen3.8-Flash-Next", "UD-IQ4_XS"))

    print("\n全部通過" if ok else "\n有項目失敗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
