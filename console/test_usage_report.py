#!/usr/bin/env python3
"""服務使用報告（GET /api/admin/usage-report）的測試。

用假造的取樣與操作紀錄跑 report.build()，斷言六塊都在、數字算得對，
特別是三個容易算錯的地方：計數器歸零（代理重啟）、取樣空窗不算耗電、低於底線的次數。
跑法：python3 test_usage_report.py（需要 cryptography；在 .27 上跑）
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report  # noqa: E402
import tls  # noqa: E402
from store import Store, redact  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail and not cond else ""))
    OK = OK and bool(cond)


def state(vram, ram_free, power, slots_busy=1, mm="unified_split"):
    pools = {"memory_model": mm, "ram": {"free_gib": ram_free, "total_gib": 31.1, "floor_gib": 15.0}}
    if mm == "unified_single":
        pools["unified"] = {"used_gib": vram, "total_gib": 122.0}
    else:
        pools["vram"] = {"used_gib": vram, "total_gib": 96.0}
    return {"pools": pools, "gpu": {"power": power, "use": 10},
            "slots": [{"id": "brain", "unit": "strata.service", "busy": slots_busy > 0, "state": "active"},
                      {"id": "eyes", "unit": "camofox.service", "busy": False, "state": "activating"}]}


def main():
    d = tempfile.mkdtemp(prefix="xconsole-report-test-")
    st = Store(os.path.join(d, "t.db"))
    cm = tls.CertManager(d, 1)
    cm.ensure(["localhost"])
    now = time.time()
    t0 = now - 3600 * 3

    # .26：三小時、每 5 分鐘一筆；第 20 筆起代理重啟、計數器歸零
    ctr = 0
    for i in range(36):
        ts = t0 + i * 300
        if i == 20:
            ctr = 0                    # 代理重啟
        ctr += 10
        st.sample("26", True, state(63.9, 14.0 if i in (5, 6) else 17.0, 40.0),
                  {"brain": {"requests": ctr, "prompt": ctr * 1000, "reused": ctr * 600, "generated": ctr * 50}})
        st.db.execute("UPDATE samples SET ts=? WHERE id=(SELECT MAX(id) FROM samples)", (ts,))
    # .20：前一小時有、中間空了一小時（不該算耗電）、後一小時有
    for i in list(range(12)) + list(range(24, 36)):
        st.sample("20", True, state(6.4, 116.0, 100.0, mm="unified_single"), {})
        st.db.execute("UPDATE samples SET ts=? WHERE id=(SELECT MAX(id) FROM samples)", (t0 + i * 300,))

    st.audit("admin", "192.168.9.188", "auth.login", "admin", True)
    st.audit("admin", "192.168.9.188", "auth.login", "admin", True)
    st.audit("", "10.0.0.9", "auth.login", "root", False)
    st.audit("admin", "192.168.9.188", "slot.restart", "26/brain", True, {"force": False, "blocked": False})
    st.audit("admin", "192.168.9.188", "model.swap", "27/brain", False, {"force": False, "blocked": True})
    st.audit("admin", "192.168.9.188", "slot.start", "26/brain2", True, {"force": True, "blocked": False})
    st.audit("admin", "192.168.9.188", "tls.apply", "x", True, {"password": "不能出現", "fingerprint256": "AA:BB"})

    cfg = {"nodes": [{"id": "26", "name": ".26", "host": "h"}, {"id": "20", "name": ".20", "host": "h"}],
           "report": {"price_ntd_per_kwh": 4.0}}
    r = report.build(st, cm, cfg, days=1, now=now)

    # ---- 六塊都要在
    for k in ("generated_at", "period", "people", "spend", "build_vs_buy", "assets", "integrations", "health"):
        check(f"報告有「{k}」這一塊", k in r and r[k] is not None)

    # ---- 1 人
    check("登入成功 2 次、失敗 1 次", r["people"]["logins_ok"] == 2 and r["people"]["logins_failed"] == 1,
          r["people"])
    check("操作次數 4（登入不算操作）", r["people"]["operations"] == 4, r["people"]["operations"])

    # ---- 2 錢：.26 每筆 40W × 35 段 × 5 分鐘；.20 100W × 22 段（中間空窗那段不算）
    want = (40 * 35 * 300 + 100 * 22 * 300) / 3600 / 1000
    check("GPU 用電照實算、取樣空窗不計", abs(r["spend"]["gpu_energy_kwh"] - round(want, 2)) < 0.011,
          f"得到 {r['spend']['gpu_energy_kwh']}，預期 {want:.3f}")
    check("有電價才換算金額，且假設寫在旁邊",
          r["spend"]["est_ntd"] is not None and "NT$ 4.0" in r["spend"]["assumption"])

    # ---- 3 地端處理量：0～19 筆遞增 10，第 20 筆歸零後又遞增
    # 第一筆沒有前值不算 → 前段 19 次增量各 10＝190；第 20 筆歸零後值為 10，當成新增 10；之後 15 次各 10＝150
    want_req = 190 + 10 + 150
    got = r["build_vs_buy"]["local"]["requests"]
    check("計數器歸零（代理重啟）不會算成負數或漏算", got == want_req, f"得到 {got}，預期 {want_req}")
    check("快取命中率＝重用／匯入", r["build_vs_buy"]["cache_hit_ratio"] == 0.6, r["build_vs_buy"]["cache_hit_ratio"])
    check("沒設雲端參考單價 → 不估算並寫明", r["build_vs_buy"]["cloud_equivalent"] is None
          and "尚未設定" in r["build_vs_buy"]["assumption"])

    # ---- 4 設備
    n26 = next(x for x in r["assets"]["nodes"] if x["node"] == ".26")
    n20 = next(x for x in r["assets"]["nodes"] if x["node"] == ".20")
    check(".26 系統記憶體低於底線 2 次", n26["below_floor_samples"] == 2, n26["below_floor_samples"])
    check(".20 是單一池，不計系統記憶體底線", n20["below_floor_samples"] == 0)

    # ---- 5 安全
    adm = r["integrations"]["admission"]
    check("准入：擋下 1 次、強制略過 1 次", adm["blocked"] == 1 and adm["forced"] == 1, adm)
    check("暫用自簽有被報出來", r["integrations"]["tls"]["selfSigned"] is True)

    # ---- 6 健康（報表要揭露系統自己的問題）
    whats = " ".join(x["what"] for x in r["health"]["items"])
    check("健康：列出暫用自簽", "暫用自簽" in whats)
    check("健康：列出低於底線", "低於底線" in whats)
    check("健康：列出強制略過准入", "強制略過" in whats)
    check("健康：列出卡在啟動中的服務", "啟動中" in whats)
    check("健康：沒信任的代理列為緊急", any(x["level"] == "緊急" and "尚未信任" in x["what"] for x in r["health"]["items"]))

    # ---- 稽核不落地機密
    rows = st.audit_list(10)
    tls_row = next(x for x in rows if x["action"] == "tls.apply")
    check("稽核紀錄遮罩密碼、保留指紋", tls_row["detail"].get("password") == "[已遮罩]"
          and tls_row["detail"].get("fingerprint256") == "AA:BB", tls_row["detail"])
    check("遮罩函式：私鑰字串整段換掉", redact({"x": "-----BEGIN PRIVATE KEY-----abc"})["x"] == "[已遮罩]")

    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
