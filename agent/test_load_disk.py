#!/usr/bin/env python3
"""載入進度與「用硬碟執行」（allow_disk）的自我檢查。
  1. 進度解析：prepare → read（百分比、剩餘時間）→ alloc → warmup → done；失敗時回報卡在哪一步
  2. 用硬碟執行的分層：BIOS 切兩塊一律不給；DGX Spark 這類 -ngl 0、Apple -ngl 999；獨立顯卡照放得下的層數
  3. 准入只算不是檔案映射的部分，KV 照層數比例分到兩個池，底線照樣守住
  4. 實際換模型（假的 llama-server，代理自己看管）：沒帶 allow_disk 照擋；帶了以 mmap 啟動；
     思考模式與區網存取同一次啟動就套用，下一次沒帶就沿用；換模型進行中問得到進度
跑法：python3 test_load_disk.py
"""
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-loaddisk-")
os.environ["XCONSOLE_SUPERVISOR"] = "1"          # Linux 上也走代理自己看管的那條路
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

GIB = A.GIB
OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def eq(name, got, want):
    case(name, got == want, f"得到 {got}，預期 {want}")


def gguf_bytes(layers=40, heads=8, kv_heads=2, emb=1024, ctx=8192):
    b = io.BytesIO()
    s = lambda t: struct.pack("<Q", len(t)) + t.encode()  # noqa: E731
    kv = {"general.architecture": "t", "general.name": "Test-Dense-8B", "t.block_count": layers, "t.context_length": ctx,
          "t.attention.head_count": heads, "t.attention.head_count_kv": kv_heads, "t.embedding_length": emb}
    tensors = [("token_embd.weight", 0), ("output.weight", 4096), ("blk.0.attn_q.weight", 8192)]
    b.write(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensors), len(kv)))
    for k, v in kv.items():
        b.write(s(k) + (struct.pack("<I", 8) + s(v) if isinstance(v, str) else struct.pack("<I", 4) + struct.pack("<I", v)))
    for name, off in tensors:
        b.write(s(name) + struct.pack("<I", 1) + struct.pack("<Q", 1) + struct.pack("<I", 0) + struct.pack("<Q", off))
    return b.getvalue()


FAKE = r'''#!{py}
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
a = sys.argv
if "--help" in a:
    print("--reasoning [on|off|auto]  --load-mode MODE  --no-mmap"); sys.exit(0)
if "--version" in a:
    print("version: 1 (fake)"); sys.exit(0)
port = int(a[a.index("--port") + 1]); alias = a[a.index("--alias") + 1]
open(sys.argv[0] + ".argv.json", "w").write(json.dumps(a))
print("load_tensors: loading model tensors", flush=True)
time.sleep(2.5)
print("llama_context: n_ctx = 8192", flush=True)
print("common_init_from_params: warming up the model with an empty run", flush=True)
time.sleep(1.0)
class H(BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def do_GET(self):
        b = json.dumps({{"data": [{{"id": alias}}]}} if self.path.startswith("/v1/models") else {{"status": "ok"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
class S(HTTPServer):
    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", port
srv = S(("127.0.0.1", port), H)
print("main: server is listening on http://127.0.0.1:%d" % port, flush=True)
srv.serve_forever()
'''


def free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def pure_tests():
    rec = {"state": "loading", "t0": 100.0, "total": 10 * GIB, "message": ""}
    p = A.load_progress(rec, pid=None, now=101)
    eq("進度：行程還沒起來 → prepare、百分比未知", (p["stage"], p["pct"]), ("prepare", None))
    p = A.load_progress(rec, pid=123, read_bytes=int(2.5 * GIB), now=110)
    eq("進度：讀權重 → read、25%、剩餘時間照速度推", (p["stage"], p["pct"], p["read_gib"], p["eta_sec"]), ("read", 25.0, 2.5, 30))
    p = A.load_progress(rec, pid=123, read_bytes=12 * GIB, now=110)
    eq("進度：讀到的比總大小多（含其他檔）→ 封頂 99%", p["pct"], 99.0)
    p = A.load_progress(rec, pid=123, rss_gib=5.0, now=110)
    eq("進度：不是 Linux 用常駐記憶體估", (p["stage"], p["pct"]), ("read", 50.0))
    p = A.load_progress(rec, pid=123, read_bytes=None, rss_gib=None, now=110)
    eq("進度：讀數都拿不到 → pct 給 None", p["pct"], None)
    p = A.load_progress(rec, pid=123, read_bytes=GIB, log_text="llama_context: n_ctx = 32768\n", now=120)
    eq("進度：記錄檔出現 KV／context 配置 → alloc、讀取算完成", (p["stage"], p["pct"]), ("alloc", 99.0))
    p = A.load_progress(rec, pid=123, read_bytes=GIB, log_text="llama_context: x\nwarming up the model", now=120)
    eq("進度：出現 warming up → warmup", p["stage"], "warmup")
    p = A.load_progress(rec, pid=123, read_bytes=GIB, log_text="", ready=True, now=120)
    eq("進度：/health 回 200 → done、100%", (p["stage"], p["pct"]), ("done", 100.0))
    eq("進度：n_ctx_train 不算 KV 配置（讀權重前就會印）",
         A.load_progress(rec, pid=1, read_bytes=GIB, log_text="print_info: n_ctx_train = 131072", now=120)["stage"], "read")
    p = A.load_progress({**rec, "state": "ready", "t1": 130.0}, now=200)
    eq("進度：已完成 → state ready、經過秒數記到完成那一刻", (p["state"], p["stage"], p["elapsed_sec"]), ("ready", "done", 30.0))
    p = A.load_progress({**rec, "state": "failed", "t1": 130.0, "stage": "alloc", "message": "載不起來"}, now=200)
    eq("進度：失敗 → 回報卡在哪一步與原因", (p["state"], p["stage"], p["message"]), ("failed", "alloc", "載不起來"))
    if A.OS == "linux":
        case("Linux：讀得到自己的 /proc/<pid>/io", (A.proc_read_bytes(os.getpid()) or 0) > 0)
    else:
        case("不是 Linux：/proc 讀數回 None（改用 RSS 估）", A.proc_read_bytes(os.getpid()) is None)

    # ---- 用硬碟執行的看門狗
    wd_dir = tempfile.mkdtemp(prefix="wd-")
    disk_cfg, norm_cfg = os.path.join(wd_dir, "d.json"), os.path.join(wd_dir, "n.json")
    A.save_json(disk_cfg, {"disk": True, "mmap": True})
    A.save_json(norm_cfg, {"disk": False})
    wd_reg = {"machine": {"memory_model": "unified_split"},
              "slots": [{"id": "big", "kind": "llama", "unit": "u-big", "config": disk_cfg, "model": "Big-Q8"},
                        {"id": "ok", "kind": "llama", "unit": "u-ok", "config": norm_cfg, "model": "Small"}]}
    real_alive = A._alive
    A._alive = lambda sl: True
    stopped_units, hits = [], {}
    line = A.watchdog_line(wd_reg["machine"])
    eq("看門狗：AI 395 的危險線＝底線 15 的一半", line, 7.5)
    A.watchdog_clear("big")
    eq("看門狗：記憶體夠，不動", A.watchdog_check(wd_reg, 20.0, hits, stop=stopped_units.append), [])
    A.watchdog_check(wd_reg, 5.0, hits, stop=stopped_units.append)
    A.watchdog_check(wd_reg, 5.0, hits, stop=stopped_units.append)
    eq("看門狗：只低兩次（尖峰）不停", stopped_units, [])
    A.watchdog_check(wd_reg, 20.0, hits, stop=stopped_units.append)
    A.watchdog_check(wd_reg, 5.0, hits, stop=stopped_units.append)
    A.watchdog_check(wd_reg, 5.0, hits, stop=stopped_units.append)
    eq("看門狗：中間回升就重新算", stopped_units, [])
    got = A.watchdog_check(wd_reg, 5.0, hits, stop=stopped_units.append)
    eq("看門狗：連續三次低於危險線，停掉用硬碟執行的那個", (got, stopped_units), (["big"], ["u-big"]))
    note = A.watchdog_notes().get("big") or {}
    case("  留下原因給畫面（剩多少、危險線、怎麼辦），並記下已取消開機自啟",
         "5.0 GiB" in note.get("message", "") and note.get("line") == 7.5 and note.get("disabled") is True, note)
    case("  一般放法的模型不停（准入已經算死，不會跑到一半多吃）", "u-ok" not in stopped_units)
    A.watchdog_clear("big")
    eq("  重新啟動前清掉原因", A.watchdog_notes().get("big"), None)
    A._alive = lambda sl: False
    hits = {}
    for _ in range(4):
        A.watchdog_check(wd_reg, 1.0, hits, stop=stopped_units.append)
    eq("看門狗：沒在跑的不理", stopped_units, ["u-big"])
    # 兩個都用硬碟執行：一次只停一個，停完重新量
    disk2 = os.path.join(wd_dir, "d2.json")
    A.save_json(disk2, {"disk": True})
    wd_reg2 = {"machine": wd_reg["machine"], "slots": [wd_reg["slots"][0], {"id": "big2", "kind": "llama", "unit": "u-big2", "config": disk2}]}
    A._alive = lambda sl: True
    stopped_units, hits = [], {}
    for _ in range(3):
        A.watchdog_check(wd_reg2, 5.0, hits, stop=stopped_units.append)
    eq("看門狗：兩個都用硬碟執行，一次只停一個", stopped_units, ["u-big"])
    A.watchdog_check(wd_reg2, 12.0, hits, stop=stopped_units.append)
    eq("  停完記憶體回升，另一個不停", stopped_units, ["u-big"])
    A._alive = real_alive
    A.watchdog_clear("big")
    calls = []
    real_sysctl = A.sysctl
    A.sysctl = lambda unit, verb, timeout=200: (calls.append(verb), (0, "", ""))[1]
    A.watchdog_stop("u-x")
    A.sysctl = real_sysctl
    eq("看門狗停服務：停掉並取消開機自啟（重開機不會自己再起來）", calls, ["stop", "disable"])

    # 用硬碟執行時，會關掉檔案映射或鎖住記憶體的自訂引數一律不套用
    eq("拿掉 --no-mmap／--mlock／--load-mode 與它的值",
       A.strip_no_mmap(["--no-mmap", "-t", "8", "--load-mode", "none", "--mlock", "--load-mode=none", "--temp", "0.7"]),
       ["-t", "8", "--temp", "0.7"])
    mp = os.path.join(wd_dir, "m.gguf")
    reg_args = {"machine": {"memory_model": "unified_split", "llama_args": ["--mlock"]},
                "llama_custom_args": {os.path.abspath(mp): ["--no-mmap", "-t", "8"]}}
    raw_disk = {"exe": "/x/llama-server", "model": mp, "port": 8290, "alias": "a", "disk": True, "mmap": True, "ngl": 999}
    argv = A.llama_argv(raw_disk, reg_args)
    case("  用硬碟執行：指令裡沒有 --no-mmap、--mlock，其他自訂引數照樣套用",
         "--no-mmap" not in argv and "--mlock" not in argv and "-t" in argv, argv)
    argv = A.llama_argv({**raw_disk, "disk": False, "mmap": False}, reg_args)
    case("  一般放法：自訂的 --no-mmap、--mlock 照舊套用", "--no-mmap" in argv and "--mlock" in argv, argv)

    # ---- 用硬碟執行的分層
    m = {"size_gib": 100.0, "mmproj_gib": 0.0, "layers": 50, "kv_full_bpt": 131072, "kv_swa_bpt": 0, "swa_window": 0}
    # BIOS 切兩塊（AI 395）：GPU 那塊當一張卡，放法跟獨立顯卡一樣；系統那塊只當頁快取
    apu = [{"id": "1", "total_gib": 96, "free_gib": 90}]
    d = A.disk_layout(m, {"memory_model": "unified_split"}, apu, None, 8192, 1, 0, ram_free_gib=25)
    kv0 = A.llama_kv_gib(m, 8192)
    n0 = int((90 - A.floors({"memory_model": "unified_split"})["vram"] - 1.0) // (100 / 50 + kv0 / 50))
    eq("AI 395：可以用硬碟執行，-ngl 照 GPU 那塊放得下的層數", (d["mode"], d["ngl"], d["mmap"]), ("disk", n0, True))
    cache0 = 25 - A.floors({"memory_model": "unified_split"})["ram"] - 2.5 - kv0 * d["kv_ram_frac"]
    eq("  系統那塊只當頁快取：其餘從 SSD 讀", d["split"]["disk"], round(100 - 100 * n0 / 50 - cache0, 1))
    eq("  准入只算丟不掉的部分（引擎 2.5 GiB）", d["est_ram"], 2.5)
    moe_m = {**m, "moe": True, "expert_layer_frac": 0.017, "expert_layers": 50}
    dm = A.disk_layout(moe_m, {"memory_model": "unified_split"}, apu, None, 8192, 1, 0, ram_free_gib=25)
    eq("AI 395 的 MoE：照張量分層（-ngl 999 ＋ --n-cpu-moe）", (dm.get("tiered"), dm["ngl"], dm["cpu_moe"] > 0), (True, 999, True))
    cards = [{"id": "0", "total_gib": 24, "free_gib": 24}, {"id": "1", "total_gib": 24, "free_gib": 24}]
    d = A.disk_layout(m, {"memory_model": "discrete"}, cards, None, 8192, 1, 0, ram_free_gib=64)
    kv = A.llama_kv_gib(m, 8192)
    room = 48 - A.floors({"memory_model": "discrete"})["vram"] - 1.0
    want_n = int(room // (100 / 50 + kv / 50))
    eq("獨立顯卡：-ngl 照顯存放得下的層數、以 mmap 啟動", (d["mode"], d["ngl"], d["mmap"]), ("disk", want_n, True))
    eq("  沒放 GPU 的層，KV 在系統記憶體（比例）", d["kv_ram_frac"], round(1 - want_n / 50, 4))
    eq("  顯存只算放進去的層＋計算緩衝", d["est_vram"], round(100 * want_n / 50 + 1.0, 1))
    rest = 100 - 100 * want_n / 50
    eq("  頁快取放不下的量算成溢出到硬碟",
         d["split"]["disk"], round(max(0.0, rest - (64 - A.floors({"memory_model": "discrete"})["ram"] - 2.5 - kv * d["kv_ram_frac"])), 1))
    real_os = A.OS
    A.OS = "linux"
    d = A.disk_layout(m, {"memory_model": "unified_single"}, [], None, 8192, 1, 0, ram_free_gib=110)
    eq("DGX Spark／Jetson（非 Apple 的統一記憶體）：-ngl 0（CUDA 會把卸載的層複製成不可換頁的記憶體）", d["ngl"], 0)
    A.OS = "darwin"
    d = A.disk_layout(m, {"memory_model": "unified_single"}, [], None, 8192, 1, 0, ram_free_gib=110)
    eq("Apple：-ngl 999（Metal 直接用映射的記憶體）", d["ngl"], 999)
    A.OS = real_os
    eq("  統一記憶體：權重不算進准入（只算計算緩衝與引擎）", (d["est_vram"], d["est_ram"]), (1.0, 1.5))

    # ---- 准入：KV 照比例分到兩個池
    A.ram_pool = lambda: {"total_gib": 64.0, "free_gib": 30.0, "used_gib": 34.0}
    A.swap_pool = lambda: {"total_gib": 0.0, "used_gib": 0.0}
    hw = {"gpu": {"gpus": [{"id": "0", "name": "t", "vram_total": 24 * GIB, "vram_used": 0}], "vram_total": 24 * GIB, "vram_used": 0}}
    reg = {"machine": {"memory_model": "discrete"}, "slots": [{"id": "s", "role": "r", "est_vram_gib": 10, "est_ram_gib": 2.5}],
           "profiles": {}, "measure": {}}
    base = {"max_context": 65536, "kv": "f16", "kv_grow": False, "parallel": 1, "kv_bytes_per_cell": 262144}
    v0 = A.admit("s", reg, hw, cfg_override=base)
    v1 = A.admit("s", reg, hw, cfg_override={**base, "kv_ram_frac": 0.75, "disk_mode": True})
    eq("准入：一般放法 KV 全算顯存", (v0["pools"]["vram"]["need"], v0["pools"]["ram"]["need"]), (26.0, 2.5))
    eq("准入：用硬碟執行時 KV 照沒上 GPU 的層數比例算到系統記憶體",
         (v1["pools"]["vram"]["need"], v1["pools"]["ram"]["need"]), (14.0, 14.5))
    v2 = A.admit("s", reg, hw, cfg_override={**base, "kv_ram_frac": 1.0, "kv_bytes_per_cell": 2 * 262144, "disk_mode": True})
    eq("准入：用硬碟執行照樣守住系統記憶體底線", (v2["allow"], any("系統記憶體不足" in r for r in v2["reasons"])), (False, True))


def swap_tests():
    if os.name == "nt":
        print("[略過] Windows 不跑假的 llama-server 實際換模型（其他項目照跑）")
        return
    home = os.environ["XCONSOLE_HOME"]
    root = os.path.join(home, "models", "gguf")
    os.makedirs(os.path.join(root, "test--dense"), exist_ok=True)
    path = os.path.join(root, "test--dense", "Test-Dense-8B-Q4_K_M.gguf")
    with open(path, "wb") as fh:
        fh.write(gguf_bytes())
        fh.truncate(3 * GIB)                       # 稀疏檔：看起來 3 GiB，不真的占磁碟
    exe = os.path.join(home, "llama.cpp-bfake", "llama-server")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with open(exe, "w") as fh:
        fh.write(FAKE.format(py=sys.executable))
    os.chmod(exe, 0o700)
    A.ram_pool = lambda: {"total_gib": 16.0, "free_gib": 4.0, "used_gib": 12.0}
    A.swap_pool = lambda: {"total_gib": 0.0, "used_gib": 0.0}
    A.hardware = lambda: {"gpu": {"gpus": [], "vram_total": 0, "vram_used": 0}, "cpu": {}, "ram": {}}
    port = free_port()
    reg = {"machine": {"id": "t", "memory_model": "unified_single", "ram_floor_gib": 1.0, "gguf_root": root,
                       "llama_server": exe}, "slots": [{"id": "s1", "role": "對話", "port": port}],
           "profiles": {}, "measure": {}}
    A.save_json(A.REGISTRY, reg)
    mid = "test--dense/Test-Dense-8B-Q4_K_M"
    try:
        code, out = A.set_llama_model("s1", mid, 8192)
        case("沒帶 allow_disk：記憶體不夠照擋（行為不變）", code == 409 and "admit" in out, out.get("error"))
        st = A.slot_progress("s1")[1]
        case("  擋下之後進度回 failed 並寫原因", st["state"] == "failed" and "記憶體不夠" in st["message"], st)

        seen = []
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                seen.append(A.slot_progress("s1")[1].get("stage"))
                time.sleep(0.3)
        th = threading.Thread(target=poll, daemon=True)
        th.start()
        code, out = A.set_llama_model("s1", mid, 8192, allow_disk=True, thinking="off", lan=False)
        stop.set()
        th.join()
        case("帶 allow_disk：以檔案映射啟動成功", code == 200 and out.get("disk_mode") is True, out)
        raw = A.load_json(A.engine_file("s1", "-llama.json"), {})
        argv = json.load(open(exe + ".argv.json"))
        eq("  設定檔：mmap、disk、-ngl 依平台（Apple 999／其他 0）",
             (raw.get("mmap"), raw.get("disk"), raw.get("ngl")), (True, True, 999 if A.OS == "darwin" else 0))
        case("  啟動參數沒有 --no-mmap／--load-mode none", "--no-mmap" not in argv and "--load-mode" not in argv, argv)
        eq("  思考模式與區網存取在同一次啟動就套用",
             (raw.get("reasoning"), raw.get("host"), "--reasoning" in argv, argv[argv.index("--host") + 1]),
             ("off", "127.0.0.1", True, "127.0.0.1"))
        case("  換模型進行中問得到進度（至少經過讀取後的階段）", any(x in ("alloc", "warmup", "done") for x in seen), seen)
        st = A.slot_progress("s1")[1]
        eq("  完成後進度回 ready／done", (st["state"], st["stage"]), ("ready", "done"))
        reg2 = A.registry()
        sl = next(x for x in reg2["slots"] if x["id"] == "s1")
        case("  准入用的估計值不含權重（檔案映射）", sl["est_vram_gib"] <= 1.5, sl)
        opts = A.run_options(sl, raw)
        case("執行設定回 split、disk_allowed、disk_needed",
             opts.get("split") is not None and opts["disk_allowed"] is True and opts["disk_needed"] is True and opts["disk_mode"], opts)

        code, out = A.set_llama_model("s1", mid, 8192, allow_disk=True)
        raw = A.load_json(A.engine_file("s1", "-llama.json"), {})
        eq("下一次沒帶思考模式與區網：沿用上一次的設定", (code, raw.get("reasoning"), raw.get("host")), (200, "off", "127.0.0.1"))
        code, out = A.set_llama_model("s1", mid, 8192, allow_disk=True, thinking="bad")
        eq("思考模式不是 auto／on／off → 400", code, 400)

        r = A.registry()
        r["machine"]["memory_model"] = "unified_split"
        A.save_json(A.REGISTRY, r)
        code, out = A.set_llama_model("s1", mid, 8192, allow_disk=True)
        case("BIOS 切兩塊的機器也可以帶 allow_disk（不再回 400）", code != 400 or "BIOS" not in out.get("error", ""), out)
        sl = next(x for x in A.registry()["slots"] if x["id"] == "s1")
        opts = A.run_options(sl, A.load_json(A.engine_file("s1", "-llama.json"), {}))
        case("  執行設定 disk_allowed=true，並附上這種機器的說明", opts["disk_allowed"] is True and "看門狗" in (opts["disk_note"] or ""), opts.get("disk_note"))

        # 看門狗停掉之後：換模型失敗要留著原因；按「啟動」要恢復開機自啟、成功才清掉原因
        A.save_json(A.WATCHDOG_FILE, {"s1": {"message": "看門狗停掉了", "disabled": True}})
        code, out = A.set_llama_model("s1", "沒有這顆/x", 8192, allow_disk=True)
        case("換模型失敗（沒有這顆）：看門狗留下的原因還在", code == 400 and A.watchdog_notes().get("s1"), (code, out))
        verbs = []
        real_sysctl = A.sysctl
        A.sysctl = lambda unit, verb, timeout=200: (verbs.append(verb), (0, "", ""))[1]
        try:
            code, out = A.do_control("s1", "start", force=True)
        finally:
            A.sysctl = real_sysctl
        case("按啟動：先恢復開機自啟再啟動，成功後清掉原因",
             code == 200 and "enable" in verbs and verbs.index("enable") < verbs.index("start") and not A.watchdog_notes().get("s1"), (code, verbs))
    finally:
        for u in list(A.SUP.procs):
            A.SUP.ctl(u, "stop")


def main():
    pure_tests()
    swap_tests()
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
