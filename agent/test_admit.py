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

    # 8. llama.cpp 的 KV：滑動視窗層每條序列各留一個視窗，要乘並行數
    got = A.llama_kv_gib({"kv_full_bpt": 1000, "kv_swa_bpt": 2000, "swa_window": 4096}, 32768, 4)
    ok &= case("滑動視窗 KV × 並行（4 條 32K、視窗 4K）",
               round(got, 4), round((1000 * 32768 * 4 + 2000 * 4096 * 4) / A.GIB, 4))

    # 9. 多卡（例：中原 2× 48 GB）：總量夠不代表每張都夠
    two = lambda u0, u1: fake_hw(96, u0 + u1, gpus=[
        {"id": "0", "name": "t", "vram_total": 48 * GIB, "vram_used": u0 * GIB},
        {"id": "1", "name": "t", "vram_total": 48 * GIB, "vram_used": u1 * GIB}])
    # 卡 0 已被別的欄位吃掉 40，卡 1 幾乎是空的；總共剩 54。沒綁卡的 40 GiB 模型會攤成每張 20 → 卡 0 放不下
    m9 = [{"id": "m", "role": "主腦", "unit": "x.service", "est_vram_gib": 40}]
    r = A.admit("m", reg_for("discrete", m9, 123, 100), two(40, 2))
    ok &= case("兩張卡、沒綁卡：總量夠但卡 0 會爆 → 擋", r["allow"], False)
    ok &= case("  擋的理由點名卡 0", any(x.startswith("卡 0") for x in r["reasons"]), True)
    ok &= case("  卡 1 沒有被列成理由", any(x.startswith("卡 1") for x in r["reasons"]), False)
    m9b = [{"id": "m", "role": "主腦", "unit": "x.service", "est_vram_gib": 40, "gpu_ids": ["1"]}]
    r = A.admit("m", reg_for("discrete", m9b, 123, 100), two(40, 2))
    ok &= case("同一顆綁到空的卡 1 → 放行", r["allow"], True)
    ok &= case("  逐張卡的明細只算卡 1", [c["id"] for c in r["pools"]["cards"]], ["1"])
    # 128B 那種要兩張一起扛的：攤成每張 46，各剩 48 → 放行；再大一點就擋
    big = [{"id": "b", "role": "主腦", "unit": "x.service", "est_vram_gib": 92}]
    r = A.admit("b", reg_for("discrete", big, 123, 100), two(0, 0))
    ok &= case("92 GiB 攤到兩張 48 → 放行", r["allow"], True)
    big[0]["est_vram_gib"] = 96
    r = A.admit("b", reg_for("discrete", big, 123, 100), two(0, 0))
    ok &= case("96 GiB 攤到兩張 48 → 每張剩 0，低於分到的 0.5 底線 → 擋", r["allow"], False)
    # 中原實況：Mistral 128B 跨兩張 48 GB 已經在跑（卡 0 剩 0.8、卡 1 剩 1.7），重啟它不能被擋。
    # 底線 1 GiB 是總量要留的，分到兩張各 0.5；每張都要留滿 1 GiB 就會擋（2026-10-25 實際踩到）
    cy = [{"id": "brain", "role": "主腦", "unit": "x.service", "est_vram_gib": 92.8}]
    real_state, A.unit_state = A.unit_state, lambda u: "active"
    cycu = fake_hw(95.6, 93.1, gpus=[      # 中原 nvidia-smi 實測：每張 47.8 GiB，已用 47.0／46.1
        {"id": "0", "name": "t", "vram_total": 47.8 * GIB, "vram_used": 47.0 * GIB},
        {"id": "1", "name": "t", "vram_total": 47.8 * GIB, "vram_used": 46.1 * GIB}])
    r = A.admit("brain", reg_for("discrete", cy, 123, 117), cycu)
    A.unit_state = real_state
    ok &= case("中原 Mistral 128B 跨兩張卡在跑，重啟 → 放行", r["allow"], True)
    # 換綁卡：舊模型在卡 0 佔 40，要搬到卡 1；釋放量要還給卡 0，不是卡 1
    r = A.admit("m", reg_for("discrete", m9b, 123, 100), two(46, 46), reclaim_cards={"0": 40.0})
    ok &= case("舊模型在卡 0、改綁卡 1：卡 1 沒空間 → 擋（不能把卡 0 釋放的量算給卡 1）", r["allow"], False)
    # 單卡機器照舊走總量
    r = A.admit("m", reg_for("discrete", m9, 123, 100), fake_hw(48, 2))
    ok &= case("單卡機器不出現逐張卡明細", "cards" in r["pools"], False)

    # 10. 選卡檢查
    cards = [{"id": "0", "total_gib": 48}, {"id": "1", "total_gib": 48}]
    ok &= case("沒帶＝自動", A.pick_gpus(None, cards), (None, None))
    ok &= case("全選也當自動（登錄檔不留多餘的綁定）", A.pick_gpus(["1", "0"], cards), (None, None))
    ok &= case("只用卡 1", A.pick_gpus([1], cards), (["1"], None))
    ok &= case("沒有的卡要拒絕", A.pick_gpus(["2"], cards)[1] is not None, True)
    ok &= case("攤分照卡的大小", A.card_split(30, None, [{"id": "0", "total_gib": 16}, {"id": "1", "total_gib": 32}]),
               {"0": 10.0, "1": 20.0})

    # 11. 綁卡的環境變數
    ok &= case("NVIDIA：照 PCI 匯流排排序再指定卡", A.visible_env(["1"], nvidia=True),
               {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "1"})
    ok &= case("沒綁卡不設任何變數", A.visible_env(None, nvidia=True), {})
    # AMD：drm card 號碼與 HIP 序號不一定相同（KFD 節點 1 是 CPU、card1 才是第一張 GPU）
    root = tempfile.mkdtemp()
    kfd, drm = os.path.join(root, "kfd"), os.path.join(root, "drm")
    for n, minor, simd in ((0, 0, 0), (1, 129, 240), (2, 128, 240)):
        os.makedirs(os.path.join(kfd, str(n)))
        open(os.path.join(kfd, str(n), "properties"), "w").write(f"simd_count {simd}\ndrm_render_minor {minor}\n")
    for card, minor in (("card0", 128), ("card1", 129)):
        os.makedirs(os.path.join(drm, card, "device", "drm", f"renderD{minor}"))
    ok &= case("AMD：drm card → HIP 序號照 KFD 節點順序", A.amd_hip_order(drm, kfd), {"1": 0, "0": 1})
    A.DRM_DIR, A.KFD_NODES = drm, kfd
    orig = A.amd_hip_order
    A.amd_hip_order = lambda d=drm, k=kfd: orig(d, k)
    ok &= case("AMD：綁 card0 → HIP_VISIBLE_DEVICES=1", A.visible_env(["0"], nvidia=False), {"HIP_VISIBLE_DEVICES": "1"})
    try:
        A.visible_env(["5"], nvidia=False)
        ok &= case("AMD：對不到的卡要拒絕", False, True)
    except ValueError:
        ok &= case("AMD：對不到的卡要拒絕", True, True)
    A.amd_hip_order = orig
    # 啟動時真的有套上：設定檔的 gpus 變成 llama-server 的環境變數，蓋過登錄檔 llama_env 裡的同名設定
    real_which = A.shutil.which
    A.shutil.which = lambda x: "/usr/bin/nvidia-smi" if x == "nvidia-smi" else real_which(x)
    env = A.llama_launch_env("/opt/llama/llama-server", {"machine": {"llama_env": {"CUDA_VISIBLE_DEVICES": "0,1"}}}, ["1"])
    ok &= case("啟動環境：只看得到卡 1", (env.get("CUDA_VISIBLE_DEVICES"), env.get("CUDA_DEVICE_ORDER")), ("1", "PCI_BUS_ID"))
    env = A.llama_launch_env("/opt/llama/llama-server", {"machine": {}}, None)
    ok &= case("啟動環境：沒綁卡不動 CUDA_VISIBLE_DEVICES", env.get("CUDA_VISIBLE_DEVICES"), os.environ.get("CUDA_VISIBLE_DEVICES"))
    A.shutil.which = real_which

    # 12. 指令一律不經過 shell（防命令注入）：sh() 不收字串、原始碼裡不准有 shell=True／os.system
    try:
        A.sh("echo hi; id")
        ok &= case("sh() 收到整條字串要拒絕", False, True)
    except TypeError:
        ok &= case("sh() 收到整條字串要拒絕", True, True)
    rc, out, _ = A.sh(["echo", "a; id"])
    ok &= case("參數裡的分號只是文字，不會被當成第二個指令", out, "a; id")
    src = open(A.__file__, encoding="utf-8").read()
    ok &= case("代理原始碼沒有 shell=True、os.system、os.popen",
               [x for x in ("shell=True", "os.system(", "os.popen(") if x in src], [])

    print("\n全部通過" if ok else "\n有項目失敗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
