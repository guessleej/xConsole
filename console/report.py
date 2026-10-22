"""服務使用報告：把稽核紀錄與取樣算成六塊數字（skill service-usage-report）。

原則：數字全由程式算；估算值與假設一起回；沒設定的單價不換算金額、直接講沒設定；
報表要揭露系統自己的問題（health 一塊）。
"""
import datetime as dt
import os
import time

TPE = dt.timezone(dt.timedelta(hours=8))
SAMPLE_GAP = 900          # 兩筆取樣相隔超過 15 分鐘就當中間沒資料，不把空窗算成耗電


def _t(ts):
    return dt.datetime.fromtimestamp(ts, TPE).strftime("%Y/%m/%d %H:%M")


def _group(samples):
    by = {}
    for s in samples:
        by.setdefault(s["node"], []).append(s)
    return by


def _counter_deltas(rows, since):
    """各欄位的 token 計數器是累計值；兩筆之間的差就是那段時間處理的量。
    計數器變小代表代理重啟或記錄輪替，那一筆就當成從零開始累計。"""
    total, last = {}, {}
    for s in rows:
        for slot, c in (s["counters"] or {}).items():
            prev = last.get(slot)
            if prev is not None and s["ts"] >= since:
                agg = total.setdefault(slot, {"requests": 0, "prompt": 0, "reused": 0, "generated": 0})
                for k in agg:
                    cur, old = c.get(k, 0), prev.get(k, 0)
                    agg[k] += cur - old if cur >= old else cur
            last[slot] = c
    return total


def _energy_kwh(rows, since):
    kwh = 0.0
    for a, b in zip(rows, rows[1:]):
        if b["ts"] < since or not (a["online"] and b["online"]):
            continue
        dt_s = b["ts"] - a["ts"]
        if dt_s <= 0 or dt_s > SAMPLE_GAP:
            continue
        pa, pb = a["gpu_power"], b["gpu_power"]
        if pa is None or pb is None:
            continue
        kwh += (pa + pb) / 2 * dt_s / 3600 / 1000
    return kwh


def build(store, certs, cfg, days=30, now=None):
    now = now or time.time()
    since = now - days * 86400
    first = store.first_sample_ts()
    data_since = max(since, first) if first else None
    audits = store.audit_list(limit=100000, since=since)
    samples = store.samples(since - SAMPLE_GAP)
    by_node = _group(samples)
    nodes_cfg = {n["id"]: n for n in cfg.get("nodes", [])}
    rep_cfg = cfg.get("report") or {}

    # ---------------- 1. 多少人在用
    logins = [a for a in audits if a["action"] == "auth.login"]
    ops = [a for a in audits if a["action"].startswith(("slot.", "profile.", "model.", "tls.", "account.",
                                                        "trust.", "all."))]
    by_type = {}
    for a in ops:
        by_type[a["action"]] = by_type.get(a["action"], 0) + 1
    users = {}
    for a in ops:
        users[a["user"] or "（未知）"] = users.get(a["user"] or "（未知）", 0) + 1
    people = {
        "logins_ok": sum(1 for a in logins if a["ok"]),
        "logins_failed": sum(1 for a in logins if not a["ok"]),
        "distinct_users": len({a["user"] for a in logins if a["ok"]}),
        "operations": len(ops),
        "operations_by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
        "operators": [{"user": u, "operations": n} for u, n in sorted(users.items(), key=lambda x: -x[1])],
    }

    # ---------------- 2. 花了多少（只算得出 GPU 晶片的電，其餘講清楚沒算）
    per_node_kwh = {nid: _energy_kwh(rows, since) for nid, rows in by_node.items()}
    total_kwh = sum(per_node_kwh.values())
    price = rep_cfg.get("price_ntd_per_kwh")
    spend = {
        "gpu_energy_kwh": round(total_kwh, 2),
        "per_node": [{"node": nodes_cfg.get(n, {}).get("name", n), "kwh": round(k, 2)}
                     for n, k in sorted(per_node_kwh.items())],
        "price_ntd_per_kwh": price,
        "est_ntd": round(total_kwh * price, 0) if price else None,
        "assumption": ("只含 GPU 晶片回報的功耗，不含 CPU、記憶體、硬碟、電源轉換損失與冷氣，"
                       "整機實際用電會明顯較高。"
                       + (f"電價以每度 NT$ {price} 計算。" if price else "尚未設定電價，不換算金額。")),
    }

    # ---------------- 3. 自建與外購（地端模型處理了多少，換算成雲端要多少錢要先設參考單價）
    tok = {"requests": 0, "prompt": 0, "reused": 0, "generated": 0}
    per_slot = []
    for nid, rows in by_node.items():
        for slot, c in _counter_deltas(rows, since).items():
            for k in tok:
                tok[k] += c[k]
            if c["requests"]:
                per_slot.append({"node": nodes_cfg.get(nid, {}).get("name", nid), "slot": slot, **c})
    ref = rep_cfg.get("cloud_ref") or {}
    cloud = None
    if ref.get("usd_per_mtok_in") is not None and ref.get("usd_per_mtok_out") is not None:
        usd = ((tok["prompt"] - tok["reused"]) * ref["usd_per_mtok_in"]
               + tok["generated"] * ref["usd_per_mtok_out"]) / 1e6
        fx = ref.get("ntd_per_usd")
        cloud = {"model": ref.get("name", "（未命名）"), "usd": round(usd, 2),
                 "ntd": round(usd * fx, 0) if fx else None,
                 "assumption": (f"以 {ref.get('name', '參考模型')} 每百萬輸入 token US$ {ref['usd_per_mtok_in']}、"
                                f"輸出 US$ {ref['usd_per_mtok_out']} 計；重用快取的 token 不計費"
                                + (f"；匯率 {fx}" if fx else ""))}
    build_vs_buy = {
        "local": tok,
        "cache_hit_ratio": round(tok["reused"] / tok["prompt"], 3) if tok["prompt"] else None,
        "per_slot": sorted(per_slot, key=lambda r: -r["prompt"])[:20],
        "cloud_equivalent": cloud,
        "assumption": None if cloud else "尚未設定雲端參考單價，只報地端處理量，不估算省下的費用。",
    }

    # ---------------- 4. 設備有沒有用到
    assets_nodes = []
    for nid, rows in sorted(by_node.items()):
        rows = [r for r in rows if r["ts"] >= since]
        if not rows:
            continue
        on = [r for r in rows if r["online"]]
        vu = [r["vram_used"] for r in on if r["vram_used"] is not None]
        rf = [r["ram_free"] for r in on if r["ram_free"] is not None]
        gu = [r["gpu_use"] for r in on if r["gpu_use"] is not None]
        floor = next((r["ram_floor"] for r in on if r["ram_floor"] is not None), None)
        below = sum(1 for r in on if r["ram_free"] is not None and floor is not None
                    and r["mm"] != "unified_single" and r["ram_free"] < floor)
        assets_nodes.append({
            "node": nodes_cfg.get(nid, {}).get("name", nid),
            "memory_model": next((r["mm"] for r in on if r["mm"]), None),
            "samples": len(rows),
            "online_ratio": round(len(on) / len(rows), 4) if rows else None,
            "pool_total_gib": next((r["vram_total"] for r in on if r["vram_total"]), None),
            "pool_used_avg_gib": round(sum(vu) / len(vu), 1) if vu else None,
            "pool_used_peak_gib": round(max(vu), 1) if vu else None,
            "ram_free_min_gib": round(min(rf), 1) if rf else None,
            "ram_floor_gib": floor,
            "below_floor_samples": below,
            "gpu_use_avg_pct": round(sum(gu) / len(gu), 1) if gu else None,
            "slots_busy_avg": round(sum(r["busy"] or 0 for r in on) / len(on), 2) if on else None,
            "slots_total": max((r["slots"] or 0) for r in rows),
        })

    # ---------------- 5. 對外串接安不安全
    cur = certs.current() if certs else None
    trust_dir = certs.p.trust if certs else None
    agents = []
    for n in cfg.get("nodes", []):
        tf = os.path.join(trust_dir, f"{n['id']}.pem") if trust_dir else None
        agents.append({"node": n["name"], "tls": True, "trusted": bool(tf and os.path.exists(tf))})
    adm = [a for a in audits if a["action"] in ("slot.start", "slot.restart", "model.swap", "profile.apply")]
    integrations = {
        "tls": ({k: cur[k] for k in ("source", "selfSigned", "daysLeft", "validToTaipei", "issuer", "hsts")}
                if cur else None),
        "agents": agents,
        "admission": {
            "attempts": len(adm),
            "blocked": sum(1 for a in adm if (a["detail"] or {}).get("blocked")),
            "forced": sum(1 for a in adm if (a["detail"] or {}).get("force")),
        },
        "unauthorized": people["logins_failed"],
    }

    # ---------------- 6. 系統健康與治理
    items = []
    for a in assets_nodes:
        if a["online_ratio"] is not None and a["online_ratio"] < 0.99:
            items.append({"level": "注意", "what": f"{a['node']} 有 {round((1 - a['online_ratio']) * 100, 1)}% 的取樣連不上",
                          "fix": "查網路或代理服務是否中斷"})
        if a["below_floor_samples"]:
            items.append({"level": "注意", "what": f"{a['node']} 系統記憶體低於底線 {a['below_floor_samples']} 次",
                          "fix": "這台曾因系統記憶體耗盡整台卡死；檢查是否有服務長期吃記憶體"})
    if cur and cur["selfSigned"]:
        items.append({"level": "待改善", "what": "主控台使用暫用自簽憑證",
                      "fix": "在「設定 → HTTPS 與憑證」產生 CSR 或上傳正式憑證"})
    if cur and cur["daysLeft"] <= 30:
        items.append({"level": "注意" if cur["daysLeft"] >= 0 else "緊急",
                      "what": f"主控台憑證剩 {cur['daysLeft']} 天，{cur['validToTaipei']}（台北時間）到期",
                      "fix": "向憑證廠商續約並在網頁上匯入新憑證"})
    for ag in agents:
        if not ag["trusted"]:
            items.append({"level": "緊急", "what": f"{ag['node']} 的代理憑證尚未信任，主控台連不上",
                          "fix": "重新執行 deploy_console.sh，或在設定頁匯入該機憑證"})
    if integrations["admission"]["forced"]:
        items.append({"level": "注意", "what": f"有 {integrations['admission']['forced']} 次強制略過記憶體准入試算",
                      "fix": "確認當時操作者是否知情；強制載入有機會把統一記憶體機拖死"})
    if people["logins_failed"] >= 10:
        items.append({"level": "注意", "what": f"登入失敗 {people['logins_failed']} 次",
                      "fix": "確認是否有人在猜密碼"})
    last = {}
    for s in samples:
        last[s["node"]] = s
    for nid, s in last.items():
        for slot, stt in (s["states"] or {}).items():
            if stt in ("failed", "activating"):
                items.append({"level": "注意",
                              "what": f"{nodes_cfg.get(nid, {}).get('name', nid)} 的 {slot} 最後狀態為"
                                      f"{'失敗' if stt == 'failed' else '一直停在啟動中'}",
                              "fix": "到「記錄」頁查該服務的記錄"})

    return {
        "generated_at": _t(now),
        "period": {"days": days, "from": _t(since), "to": _t(now),
                   "data_since": _t(data_since) if data_since else None,
                   "note": (None if first and first <= since else
                            "取樣自 " + (_t(first) if first else "（尚無）") + " 起才開始收集，本期之前沒有資料")},
        "people": people,
        "spend": spend,
        "build_vs_buy": build_vs_buy,
        "assets": {"nodes": assets_nodes},
        "integrations": integrations,
        "health": {"items": items, "summary": "本期無異常" if not items else f"共 {len(items)} 項待處理"},
    }
