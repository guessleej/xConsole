#!/usr/bin/env python3
"""2026-11-12 在 .27 實機抓到的三個問題的自我檢查。
  1. 生字探測：/v1/models 回得來但生不出字要抓得到；逾時、忙線、載入中不算壞；連續兩次才判；成功一次就恢復
  2. 載入失敗的原因：不引 llama.cpp 的 W 級警告，只引 E 級；等不到回應時講等了多久、卡在哪一段
  3. 用硬碟執行：逾時照「搬進 GPU」的量放寬；讀完檔案後進入「搬進 GPU」階段並給剩餘時間
跑法：python3 test_health.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-health-")
os.environ["XCONSOLE_SUPERVISOR"] = "1"
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


def gen_tests():
    ok = {"choices": [{"message": {"content": "2"}}]}
    dead = {"error": {"type": "invalid_request_error",
                      "message": "CUDA: unspecified launch failure at cudaStreamSynchronize(I.st)"}}
    eq("生得出字＝好", A.gen_verdict(200, ok), True)
    eq(".27 實際的錯誤（400＋CUDA launch failure）＝壞", A.gen_verdict(400, dead), False)
    eq("500＝壞", A.gen_verdict(500, None), False)
    for st, why in ((0, "逾時或連不上"), (503, "還在載入"), (429, "忙線"), (401, "金鑰問題")):
        eq(f"{why}不算壞（{st}）", A.gen_verdict(st, None), None)
    eq("200 但沒有 choices 不算好", A.gen_verdict(200, {}), None)
    eq("錯誤文字取 error.message", A.gen_error_text(400, dead)[:31], "CUDA: unspecified launch failur")

    A._GEN.clear()
    r = A.gen_record("brain", False, "x", now=100)
    eq("失敗一次還不判壞", r["ok"], None)
    eq("失敗一次 gen_state 還是沒有判定", A.gen_state("brain"), None)
    A.gen_record("brain", None, now=200)
    r = A.gen_record("brain", False, "CUDA 壞了", now=300)
    eq("量不到的那次不打斷連續失敗：兩次就判壞", r["ok"], False)
    eq("判壞時帶錯誤與時間", A.gen_state("brain"), {"ok": False, "at": 300, "error": "CUDA 壞了"})
    r = A.gen_record("brain", True, now=400)
    eq("成功一次就恢復", (r["ok"], r["fails"], r["error"]), (True, 0, ""))
    A.gen_forget("brain")
    eq("重啟或換模型後清掉判定", A.gen_state("brain"), None)

    # 探一輪：只探跑著的語言模型欄位；正在換模型的跳過；不跑了的清掉
    reg = {"slots": [{"id": "brain", "kind": "strata", "unit": "a.service"},
                     {"id": "eyes", "unit": "camofox.service"},
                     {"id": "b2", "kind": "llama", "unit": "b.service"},
                     {"id": "b3", "kind": "llama", "unit": "c.service"},
                     {"id": "empty", "kind": "llama"}]}
    states = {"a.service": "active", "camofox.service": "active", "b.service": "active", "c.service": "inactive"}
    orig = A.unit_state
    A.unit_state = lambda u: states.get(u, "inactive")
    probed = []
    A._GEN.clear()
    A._GEN["b3"] = {"ok": False, "fails": 2, "error": "舊的"}
    A._LOAD["b2"] = {"state": "loading"}
    try:
        for _ in range(2):
            A.gen_check(reg, probe=lambda sl: (probed.append(sl["id"]), (False, "CUDA 壞了"))[1])
    finally:
        A.unit_state = orig
        A._LOAD.pop("b2", None)
    eq("只探跑著的 Strata／llama.cpp 欄位，換模型中的不探", sorted(set(probed)), ["brain"])
    eq("探兩輪都失敗：主腦標成生不出字", (A.gen_state("brain") or {}).get("ok"), False)
    eq("服務沒在跑：舊判定清掉", A.gen_state("b3"), None)

    # 畫面拿得到：slot_state 帶 gen，沒在跑就不帶
    eq("沒在跑的欄位不帶 gen", A.slot_state({"id": "x", "role": "r", "unit": None}, {"slots": [], "measure": {}}, {})["gen"], None)


def hint_tests():
    log = "\n".join([
        "0.00.050.802 I common_init_result: fitting params to device memory ...",
        "0.00.749.633 W common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to 999, abort",
        "0.00.982.463 W load: special_eos_id is not in special_eog_ids",
    ])
    eq(".27 實際的記錄：只有 W 級，不引任何一行", A.llama_fail_hint(log), "")
    log2 = log + "\n1.02.000.000 E llama_model_load: error loading model: unknown model architecture: 'xyz'"
    case("有 E 級就引 E 級那行", "unknown model architecture" in A.llama_fail_hint(log2))
    eq("沒有等級標記的舊版照關鍵字找", A.llama_fail_hint("loading\nerror: out of memory\n"), "error: out of memory")

    orig = A.slot_progress
    A.slot_progress = lambda sid: (200, {"stage": "upload"})
    try:
        eq("等不到回應：講等了多久、卡在哪", A.slow_load_reason("b2", 2040), "等了 34 分鐘還沒載完，卡在「把權重搬進 GPU」")
    finally:
        A.slot_progress = orig


def disk_tests():
    apu = {"memory_model": "unified_split"}
    eq("一般放法維持 15 分鐘", A.load_timeout({"mode": "normal", "est_vram": 90}, apu), 900)
    t = A.load_timeout({"mode": "disk", "est_vram": 88}, apu)
    want = int(600 + 88 * GIB / 50e6 * 1.5)
    eq("AI 395 用硬碟執行放 88 GiB：照每秒 50 MB 放寬", t, want)
    case("88 GiB 給到 50 分鐘以上（實測 34 分鐘還沒完的那種要等得到）", t > 50 * 60, t)
    eq("小的照樣至少 15 分鐘", A.load_timeout({"mode": "disk", "est_vram": 2}, apu), 900)
    eq("獨立顯卡沒實測值：維持原本的 15 分鐘", A.load_timeout({"mode": "disk", "est_vram": 40}, {"memory_model": "discrete"}), 900)
    eq("最多 3 小時", A.load_timeout({"mode": "disk", "est_vram": 900}, apu), 3 * 3600)

    total = 106.7 * GIB
    rec = {"state": "loading", "t0": 1000, "total": total, "upload_gib": 88, "upload_bps": 50e6}
    p = A.load_progress(rec, pid=1, read_bytes=total, now=1100)
    eq("檔案讀完、記錄檔還沒到配置：進入「搬進 GPU」", p["stage"], "upload")
    case("剛進這段：剩餘時間約 31 分鐘", abs(p["eta_sec"] - 88 * GIB / 50e6) < 2, p["eta_sec"])
    case("訊息講清楚在搬什麼", "搬進 GPU" in p["message"] and "50 MB" in p["message"], p["message"])
    p = A.load_progress({**rec, "upload_t0": 1100}, pid=1, read_bytes=total, now=1100 + 950)
    case("搬到一半：估計進度約五成", 45 < p["upload_pct"] < 55, p["upload_pct"])
    p = A.load_progress({**rec, "upload_t0": 1100, "timeout": 3434}, pid=1, read_bytes=total, now=1100 + 4000)
    case("比預估慢：講還會等到幾分鐘、超過會換回原設定", "57 分鐘" in p["message"] and "換回" in p["message"], p["message"])
    eq("比預估慢時剩餘時間是 0 不是負的", p["eta_sec"], 0)
    p = A.load_progress({**rec, "upload_t0": 1100}, pid=1, read_bytes=total,
                        log_text="llama_context: n_ctx = 8192", now=3000)
    eq("記錄檔到了配置就離開這段", p["stage"], "alloc")
    p = A.load_progress({"state": "loading", "t0": 1000, "total": total}, pid=1, read_bytes=total, now=1100)
    eq("一般載入（沒有搬進 GPU 這段）照舊停在讀取", p["stage"], "read")


def main():
    gen_tests()
    hint_tests()
    disk_tests()
    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
