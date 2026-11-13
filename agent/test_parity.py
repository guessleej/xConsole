#!/usr/bin/env python3
"""與 LocModel 2.1.0 對齊那一批的代理端自我檢查（docs/LOCMODEL_PARITY.md 第 11～23 項）。
  1. 自訂啟動引數：白名單、不准路徑與 shell 字元；自己寫分層或上下文時自動參數讓位；跟著模型存
  2. 完整啟動指令：金鑰不在指令裡
  3. MoE 用硬碟執行依張量種類分層：注意力與共用放顯卡、專家留在映射記憶體、溢出從 SSD 讀
  4. Windows 的內建顯示晶片當成一個池（專用＋一半系統記憶體）
跑法：python3 test_parity.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-parity-")
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def eq(name, got, want):
    case(name, got == want, f"得到 {got}，預期 {want}")


def main():
    # ---- 1. 自訂啟動引數
    toks, err = A.parse_llama_args('-ot "blk\\.(\\d+)\\.ffn_.*_exps\\.=CPU" -fa on -t 8 --temp 0.7')
    eq("合法的調校參數照收（含 -ot 正規式）", (err, toks[:2]), (None, ["-ot", "blk\\.(\\d+)\\.ffn_.*_exps\\.=CPU"]))
    for bad, why in (("-m /etc/passwd", "換模型"), ("--host 0.0.0.0", "改監聽位址"), ("--port 1", "改埠"),
                     ("--api-key x", "金鑰"), ("--log-file x", "寫檔"), ("--chat-template-file t", "讀檔"),
                     ("-ctk ../../x", "路徑"), ("-t 8 ; rm -rf ~", "shell 字元"), ('-t "8', "引號不成對"),
                     ("--lora a.gguf", "載入外掛權重"), ("-ngl ~/x", "家目錄路徑")):
        _, e = A.parse_llama_args(bad)
        case(f"擋下：{why}（{bad}）", e is not None)
    _, e = A.parse_llama_args(" ".join(["-t 8"] * 21))
    case("最多 40 個", e is not None)

    reg = A.registry()
    raw = {"exe": "/opt/llama/llama-server", "model": "/m/a/model.gguf", "port": 8090, "alias": "a", "ctx": 8192,
           "parallel": 1, "ngl": 999, "cpu_moe": 12, "mmap": True, "api_key_file": "/nope/key"}
    argv = A.llama_argv(raw, reg)
    case("沒有自訂引數：自動帶 -c、-ngl、--n-cpu-moe", all(x in argv for x in ("-c", "-ngl", "--n-cpu-moe")))
    A.set_custom_args(raw["model"], ["-ngl", "20", "-c", "4096", "-fa", "on"])
    reg = A.registry()
    argv = A.llama_argv(raw, reg)
    eq("自己寫了 -ngl 與 -c：自動的 -ngl、--n-cpu-moe、-c 都不加，用自己的", (argv.count("-ngl"), argv.count("-c"), "--n-cpu-moe" in argv,
       argv[-6:]), (1, 1, False, ["-ngl", "20", "-c", "4096", "-fa", "on"]))
    eq("自訂引數跟著模型存（換別顆模型不會帶過去）", A.llama_custom_args({**raw, "model": "/m/b/other.gguf"}, reg), [])
    A.set_custom_args(raw["model"], [])
    eq("清空自訂引數", A.llama_custom_args(raw), [])

    # ---- 2. 完整啟動指令
    A.write_llama_key("s1", "xc-SECRETKEY")
    raw2 = {**raw, "api_key_file": A.engine_file("s1", ".key")}
    cmd = A.llama_cmd_text(raw2)
    case("顯示的啟動指令：以 llama-server 開頭、有金鑰檔位置、沒有金鑰本身",
         cmd.startswith("llama-server") and "--api-key-file" in cmd and "SECRETKEY" not in cmd, cmd)

    # ---- 3. MoE 依張量分層
    moe = {"size_gib": 120.0, "mmproj_gib": 0.0, "moe": True, "expert_layers": 48, "expert_layer_frac": 0.018,
           "layers": 48, "kv_full_bpt": 49152, "kv_swa_bpt": 0, "swa_window": 0, "active_frac": 0.06}
    dense_part = 120 - 120 * 0.018 * 48
    disc = {"memory_model": "discrete", "floors": {"vram": 2, "ram": 8}}
    cards = [{"id": "0", "free_gib": 24.0, "total_gib": 24.0}]
    lay = A.disk_layout(moe, disc, cards, None, 32768, 1, 0.0, ram_free_gib=48.0)
    case("獨立顯卡：MoE 用硬碟執行走依張量分層（-ngl 999 ＋ --n-cpu-moe）",
         lay["tiered"] and lay["ngl"] == 999 and 0 < lay["cpu_moe"] <= 48 and lay["mmap"], lay)
    case("  顯卡上放了注意力與共用權重（約 %.1f GiB）再加放得下的專家" % dense_part,
         lay["gpu_weights_gib"] >= round(dense_part, 1) and lay["est_vram"] <= 24 - 2, lay)
    case("  專家分到記憶體（頁快取）與 SSD，KV 全在顯卡", lay["split"]["ram"] > 0 and lay["split"]["disk"] > 0 and lay["kv_ram_frac"] == 0, lay["split"])
    tiny = [{"id": "0", "free_gib": 6.0, "total_gib": 6.0}]
    lay2 = A.disk_layout(moe, disc, tiny, None, 32768, 1, 0.0, ram_free_gib=48.0)
    case("  顯卡連注意力＋KV 都放不下：退回整層切的放法", not lay2.get("tiered") and lay2["mode"] == "disk", lay2)
    dense_m = {**moe, "moe": False, "expert_layers": 0, "expert_layer_frac": None}
    case("  稠密模型照舊整層切", not A.disk_layout(dense_m, disc, cards, None, 32768, 1, 0.0, 48.0).get("tiered"))
    single = {"memory_model": "unified_single", "floors": {"ram": 12}}
    real_os = A.OS
    try:
        A.OS = "linux"
        lay3 = A.disk_layout(moe, single, [], None, 32768, 1, 0.0, ram_free_gib=100.0)
        case("DGX Spark／Jetson：專家全部留映射記憶體，只有共用部分常駐", lay3.get("tiered") and lay3["cpu_moe"] == 48, lay3)
        A.OS = "darwin"
        case("Apple：照舊整層映射（Metal 直接用映射的記憶體）", not A.disk_layout(moe, single, [], None, 32768, 1, 0.0, 100.0).get("tiered"))
    finally:
        A.OS = real_os
    lay4 = A.disk_layout(moe, {"memory_model": "unified_split", "floors": {}}, cards, None, 32768, 1, 0.0, 48.0)
    case("AI 395（BIOS 切兩塊）也照張量分層：GPU 那塊放注意力與共用，專家留在映射", bool(lay4 and lay4.get("tiered") and lay4["ngl"] == 999), lay4)

    # ---- 4. Windows 內顯
    real = (A.OS, A.has_nvidia, A._apu_like)
    try:
        A.OS = "windows"
        A.has_nvidia = lambda: False
        A._apu_like = lambda: True
        sug, why = A.suggest_memory_model([{"id": "0", "name": "AMD Radeon(TM) 880M Graphics", "vendor": "amd", "vram_total": 4 * A.GIB}])
        eq("Windows 的 AMD APU：建議當成一個池（共用 GPU 記憶體）", sug, "unified_single")
        A.OS = "linux"
        sug, _ = A.suggest_memory_model([{"id": "0", "name": "AMD Radeon 8060S", "vendor": "amd", "vram_total": 96 * A.GIB}])
        eq("Linux 的 AI 395 照舊是 BIOS 切兩塊", sug, "unified_split")
    finally:
        A.OS, A.has_nvidia, A._apu_like = real
    eq("880M／890M 有頻寬規格（不再是未知）", (A.spec_bandwidth("Radeon 880M"), A.spec_bandwidth("Radeon 890M")), (89.6, 89.6))

    # ---- 5. 手動指定顯示卡
    A.save_machine({"memory_model": "discrete", "gpu_override": {"name": "AMD Radeon 880M", "vendor": "amd", "vram_gib": 4}})
    eq("手動指定一張卡：偵測結果換成那張", [(g["name"], g["vram_total"] / A.GIB) for g in A.probe_gpus()], [("AMD Radeon 880M", 4.0)])
    A.save_machine({"memory_model": "discrete", "gpu_override": "cpu"})
    eq("指定只用 CPU：沒有卡", A.probe_gpus(), [])
    for bad in ({"name": "", "vendor": "amd", "vram_gib": 4}, {"name": "x", "vendor": "evil", "vram_gib": 4},
                {"name": "x", "vendor": "amd", "vram_gib": 99999}, "gpu0"):
        c, _ = A.save_machine({"memory_model": "discrete", "gpu_override": bad})
        case(f"擋下不合法的手動指定：{bad}", c == 400)
    A.save_machine({"memory_model": "discrete", "gpu_override": "auto"})
    case("改回自動偵測", A.gpu_override() is None)

    # ---- 6. 鏡像、令牌、GitHub 加速
    for bad in ("http://hf-mirror.com", "https://u:p@x.com", "https://x.com/?a=1", "ftp://x"):
        _, e = A.check_mirror(bad, "hf_mirror")
        case(f"鏡像只收 https、不帶帳密與查詢字串：{bad}", e is not None)
    eq("鏡像去掉結尾斜線、加速字首補上斜線", (A.check_mirror("https://hf-mirror.com/", "hf_mirror")[0],
                                                A.check_mirror("https://ghfast.top", "github_proxy")[0]), ("https://hf-mirror.com", "https://ghfast.top/"))
    A.save_machine({"memory_model": "discrete", "hf_mirror": "https://hf-mirror.com", "github_proxy": "https://ghfast.top/"})
    eq("設了鏡像：Hugging Face 的網址改走鏡像", A._hf_file_url("unsloth/x-GGUF", "a.gguf"), "https://hf-mirror.com/unsloth/x-GGUF/resolve/main/a.gguf")
    seen = {}

    class FakeOpener:
        def open(self, req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            raise OSError("不真的連")
    real_op = A._HF_OPENER
    A._HF_OPENER = FakeOpener()
    try:
        for url, want in (("https://hf-mirror.com/api/models/x", None), ("https://huggingface.co/api/models/x", "Bearer hf_secret"),
                          ("https://cdn-lfs.hf.co/x", "Bearer hf_secret"), ("https://evil.example/x", None)):
            try:
                A._hf_get(url, "hf_secret")
            except OSError:
                pass
            eq(f"令牌只送給 Hugging Face 自己：{url.split('/')[2]}", seen.get("auth"), want)
    finally:
        A._HF_OPENER = real_op
    A.save_machine({"memory_model": "discrete", "hf_mirror": "", "github_proxy": ""})
    eq("清空鏡像：回到官方", A.hf_base(), A.HF_BASE)

    # ---- 7. Agent 連結資料夾
    A.save_machine({"memory_model": "discrete", "agent_enabled": True})
    proj = os.path.join(A.HOME, "proj-a")
    os.makedirs(proj, exist_ok=True)
    for bad, why in (("/", "根目錄"), ("/etc", "系統資料夾"), (A.HOME, "家目錄本身"), (os.path.dirname(A.HOME), "家目錄的上層"),
                     (A.STATE_DIR, "xConsole 自己的狀態"), (os.path.join(A.HOME, ".ssh"), "隱藏設定夾")):
        os.makedirs(bad, exist_ok=True) if bad.startswith(A.HOME) else None
        case(f"不能連結：{why}", A.agent_link_denied(bad) is not None, bad)
    c, out = A.agent_link(proj)
    case("連結家目錄底下的專案資料夾", c == 200 and out["name"] == "proj-a", out)
    eq("  Agent 用得到它（路徑檢查照舊套用）", A.agent_folder("proj-a"), os.path.realpath(proj))
    c, out = A.agent_write("proj-a", "../escape.txt", "x")
    case("  寫到資料夾外面照樣擋下", c == 400)
    c, out = A.agent_write("proj-a", "ok.txt", "hi")
    case("  資料夾裡面照樣能寫", c == 200 and os.path.exists(os.path.join(proj, "ok.txt")))
    c, out = A.agent_browse(A.HOME)
    case("資料夾瀏覽器：只列子資料夾、隱藏的不列、標出能不能選", c == 200 and "proj-a" in [d["name"] for d in out["dirs"]]
         and not any(d["name"].startswith(".") for d in out["dirs"]))
    bob = os.path.join(os.path.dirname(A.HOME), "bob-" + os.path.basename(A.HOME), "Documents")
    os.makedirs(bob, exist_ok=True)
    case("別人家目錄底下的資料夾也不能連結（不只家目錄本身）", A.agent_link_denied(bob) is not None, bob)
    open(os.path.join(A.STATE_DIR, "desktop-user.txt"), "w", encoding="utf-8").write("Jeff")
    real_env = os.environ.get("USERNAME")
    os.environ["USERNAME"] = "HP$"
    try:
        own = A.win_own_users()
        case("Windows 能碰的使用者資料夾：桌面使用者與 Public，不含 SYSTEM 的機器帳號", "jeff" in own and "public" in own and "hp$" not in own, own)
    finally:
        os.environ.pop("USERNAME") if real_env is None else os.environ.__setitem__("USERNAME", real_env)
    c, _ = A.agent_unlink("proj-a")
    case("取消連結：資料夾本身還在", c == 200 and os.path.isdir(proj) and "proj-a" not in A.agent_links())

    # ---- 8. 實測速度校正
    mdir = os.path.join(A.gguf_root(), "m")
    os.makedirs(mdir, exist_ok=True)
    mp = os.path.join(mdir, "m.gguf")
    with open(mp, "wb") as fh:
        fh.truncate(4 * A.GIB)
    reg = A.registry()
    cfgp = os.path.join(A.ENGINE_DIR, "s9-llama.json")
    os.makedirs(A.ENGINE_DIR, exist_ok=True)
    A.save_json(cfgp, {"model": mp, "ngl": 999, "exe": "/x/llama-server"})
    reg["slots"] = [{"id": "s9", "kind": "llama", "config": cfgp, "unit": "u"}]
    reg["machine"]["bandwidth_gbs"] = 100
    A.save_json(A.REGISTRY, reg)
    real_prof = A._profile_cached
    A._profile_cached = lambda p: {"active_frac": 1.0, "layers": 32}
    try:
        est = A.estimate_decode({"active_frac": 1.0, "layers": 32}, 4.0, A.registry()["machine"])
        A.record_speed("s9", est * 0.5, 200)
        case("載入後第一個回答不算（暖機）", "speed" not in A.registry() or os.path.abspath(mp) not in A.registry().get("speed", {}))
        A.record_speed("s9", est * 2, 200)
        r = A.registry()
        eq("實測記在這顆模型上", r["speed"][os.path.abspath(mp)]["tok_s"], round(est * 2, 2))
        eq("一般放法：校正倍數＝實測 ÷ 估的", round(r["machine"]["speed_factor"], 2), 2.0)
        A._SPEED_LAST.clear()
        A.record_speed("s9", est * 2, 10)
        eq("太短的回答（不到 32 個 token）不算", A.registry()["speed"][os.path.abspath(mp)]["n"], 1)
        eq("校正倍數出現在晶片資訊（推薦清單會用）", A.chip()["speed_factor"], 2.0)
        sp = A.registry()["speed"][os.path.abspath(mp)]
        case("一般放法另記擬合用的點（讀多少、幾層、速度）", sp.get("layers") == 32 and sp.get("read_gib") and sp.get("fit_tok_s"))
        case("只量過一顆：不擬合，留單一倍數", "speed_fit" not in A.registry()["machine"])
    finally:
        A._profile_cached = real_prof

    # ---- 8a. 改頻寬：舊的校正倍數與擬合作廢
    r0 = A.registry()
    r0["machine"].update(speed_factor=1.5, speed_factor_n=3, speed_fit={"eff": 0.8, "layer_ms": 0.2, "n": 2, "bw": 100})
    A.save_json(A.REGISTRY, r0)
    real_root, real_llama, real_sd = A.check_root, A.check_llama, A.check_sd_cli
    A.check_root = A.check_llama = A.check_sd_cli = lambda v: (None, None)
    try:
        mm0 = r0["machine"].get("memory_model") or "discrete"
        A.save_machine({"memory_model": mm0, "bandwidth_gbs": 100})
        case("頻寬沒變：校正保留", A.registry()["machine"].get("speed_factor") == 1.5)
        A.save_machine({"memory_model": mm0, "bandwidth_gbs": 120})
        m0 = A.registry()["machine"]
        case("頻寬改了：校正倍數與擬合都作廢", not any(k in m0 for k in ("speed_factor", "speed_factor_n", "speed_fit")))
    finally:
        A.check_root, A.check_llama, A.check_sd_cli = real_root, real_llama, real_sd

    # ---- 8b. 擬合頻寬效率與每層開銷（HP 筆電：E2B 38.6、12B 6.9 tok/s，89.6 GB/s）
    def tok(read, layers, eff=0.83, lms=0.28, bw=89.6):
        return 1 / (read * A.GIB / (eff * bw * 1e9) + layers * lms / 1000)
    f = A.fit_speed([(1.125, 35, tok(1.125, 35)), (9.11, 48, tok(9.11, 48))], 89.6)
    eq("擬合：頻寬效率", f["eff"], 0.83)
    eq("擬合：每層開銷（毫秒）", f["layer_ms"], 0.28)
    eq("擬合記下當時的頻寬（改了頻寬就不再用）", f["bw"], 89.6)
    case("只有一顆不擬合", A.fit_speed([(1.125, 35, 38.6)], 89.6) is None)
    case("讀的量差不到兩倍不擬合（分不開兩項）", A.fit_speed([(4, 32, 10), (6, 40, 7)], 89.6) is None)
    eq("開銷算出負的就全部算在頻寬上", A.fit_speed([(1, 30, 200), (8, 30, 10)], 89.6)["layer_ms"], 0.0)

    # ---- 9. 桌面小幫手的權杖不出這台
    import json as _j
    real_route = A.desktop_route
    A.desktop_route = lambda: ("helper", (54321, "HELPER-SECRET-TOKEN"))
    try:
        info = _j.dumps(A.machine_info(), ensure_ascii=False)
        case("機器資訊不含桌面小幫手的埠與權杖", "HELPER-SECRET-TOKEN" not in info and "54321" not in info)
        eq("  只回怎麼操作", A.machine_info()["desktop"], {"how": "helper", "why": None})
    finally:
        A.desktop_route = real_route
    case("save_machine 在登錄檔鎖底下（不會蓋掉同時進來的更新）", hasattr(A.save_machine, "__wrapped__")
         and not hasattr(A.check_mirror, "__wrapped__"))
    saved = (A.OS, A.gpu_override, A.has_nvidia, A.platform_machine, A.sh)
    try:
        A.OS, A.has_nvidia, A.platform_machine = "windows", (lambda: False), (lambda: "AMD64")
        A.sh = lambda argv, timeout=20: (1, "", "")
        A.gpu_override = lambda: {"vendor": "nvidia", "name": "RTX 4090"}
        case("Windows 手動指定 NVIDIA（偵測失敗）：裝 CUDA 版", (A.prebuilt_variant() or ("",))[0].startswith("win-cuda-"), A.prebuilt_variant())
        case("  stable-diffusion 也裝 CUDA 版", A.prebuilt_variant("sd")[0] == "win-cuda12-x64", A.prebuilt_variant("sd"))
        A.gpu_override = lambda: "cpu"
        eq("  指定不用 GPU：stable-diffusion 也只裝 CPU 版", A.prebuilt_variant("sd")[0], "win-cpu-x64")
    finally:
        A.OS, A.gpu_override, A.has_nvidia, A.platform_machine, A.sh = saved
    saved = (A.OS, A._linux_display_env, os.environ.get("DISPLAY"), os.environ.get("WAYLAND_DISPLAY"))
    try:
        A.OS, A._linux_display_env = "linux", (lambda: {})
        os.environ.pop("DISPLAY", None)
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        how, why = A.desktop_route()
        case("Linux 純 Wayland：截得到圖但點不了，照實說不能用", how is None and "Wayland" in why, (how, why))
    finally:
        A.OS, A._linux_display_env = saved[0], saved[1]
        for k, v in (("DISPLAY", saved[2]), ("WAYLAND_DISPLAY", saved[3])):
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    real_sug = A.suggest_memory_model
    try:
        A.suggest_memory_model = lambda gpus=None: ("unified_single", "測試")
        r = A.registry(); r["machine"].pop("confirmed", None); r["machine"]["memory_model"] = "discrete"; A.save_json(A.REGISTRY, r)
        A.desktop_first_run()
        m = A.registry()["machine"]
        case("桌面版第一次啟動：直接套用偵測到的記憶體架構（一般人不必懂）", (m["memory_model"], m.get("confirmed")) == ("unified_single", True), m)
        r = A.registry(); r["machine"]["memory_model"] = "discrete"; A.save_json(A.REGISTRY, r)
        A.desktop_first_run()
        eq("  使用者確認過的不再覆蓋", A.registry()["machine"]["memory_model"], "discrete")
    finally:
        A.suggest_memory_model = real_sug
    case("桌面版：區網存取只沿用使用者自己打開的（舊版留下的 0.0.0.0 不算）",
         "elif DESKTOP_AGENT and not prev_raw.get(\"lan_chosen\")" in open(A.__file__, encoding="utf-8").read())
    eq("顯示卡名稱帶 (TM) 也對得到頻寬", A.spec_bandwidth("AMD Radeon(TM) 890M Graphics"), 89.6)
    real_rp = A.ram_pool
    try:
        A.ram_pool = lambda: {"total_gib": 31.8, "free_gib": 20.0, "used_gib": 11.8}
        eq("32 GB 筆電（單一池）：底線照比例留（不是 DGX 的 12 GB）", A.floors({"memory_model": "unified_single"})["ram"], 5.7)
        A.ram_pool = lambda: {"total_gib": 120.0, "free_gib": 100.0, "used_gib": 20.0}
        eq("120 GB 的 DGX Spark：照舊 12 GB", A.floors({"memory_model": "unified_single"})["ram"], 12.0)
        eq("AI 395 切兩塊：照舊 15 GB（實測值，不縮）", A.floors({"memory_model": "unified_split"})["ram"], 15.0)
        eq("管理者自己填的底線優先", A.floors({"memory_model": "unified_single", "ram_floor_gib": 9})["ram"], 9.0)
    finally:
        A.ram_pool = real_rp

    print("全部通過" if OK else "有項目失敗")
    sys.exit(0 if OK else 1)


if __name__ == "__main__":
    main()
