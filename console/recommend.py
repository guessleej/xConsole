"""推薦清單：依每台機器的記憶體與頻寬，算出每顆模型「放不放得下、放在哪、大概多快、該下載哪個量化」。

推薦清單本身（要推哪些模型）存在主控台，管理者在網頁上增改刪；每一筆的大小、架構、KV、MoE 比例
都由代理從 Hugging Face 的檔頭讀來（hf_profile），不是人手抄的。

速度是估算，不是實測：每產生一個 token 都要把用到的權重從記憶體讀一遍，
所以 tok/s ≈ 記憶體頻寬 × 效率 ÷ 每個 token 讀的位元組。效率取 EFFICIENCY，畫面上一併寫出假設。
"""
import json
import math
import os
import re
import threading
import time

GIB = 1024 ** 3
EFFICIENCY = 0.6          # llama.cpp 解碼實際吃到的頻寬比例，經驗值；畫面上寫明是估算
DEFAULT_RAM_BW = 50       # 獨立顯卡機的系統記憶體頻寬沒填時的假設（GB/s），只用在專家搬到系統記憶體的情況
SMOOTH_TOKS = 10          # 低於這個速度不算「流暢」
LAYER_MS = 0.1            # 每層固定開銷（毫秒）：MoE 每個 token 只讀幾 GB，光算頻寬會高估，加上啟動核心的固定成本
CTX = 32768               # 推薦時假設的上下文
ENGINE_OVERHEAD = 1.0     # 計算緩衝，與代理 llama_est 一致
Q4_GIB_PER_B = 0.57       # Q4_K_M 每十億參數約 0.57 GiB，用來換算「流暢執行 XB 以內」
NICE_B = (1, 3, 4, 8, 12, 14, 24, 27, 32, 49, 70, 120, 235, 405, 671)
USES = {"chat": "對話與知識", "code": "寫程式", "vision": "看圖與 OCR"}
CN_ORIGIN = ("qwen", "deepseek", "glm", "chatglm", "kimi", "moonshot", "minimax", "hunyuan",
             "internlm", "baichuan", "doubao", "ernie", "seed-oss")
REPO_RX = re.compile(r"[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}")


def quant_bits(q):
    """量化名稱 → 大約幾位元。F16／BF16 回 16。檔名看不出量化（repo 只放一個檔）當 4 位元那一級。"""
    q = (q or "").upper()
    if not q:
        return 4
    if "F16" in q or "F32" in q:
        return 16
    if "MXFP4" in q:
        return 4
    m = re.search(r"I?Q(\d)", q)
    return int(m.group(1)) if m else None


def quant_pref(q):
    """同一個位元數裡的偏好：K 系列（含 unsloth 動態 UD-…_K_XL）優先，舊式 Q4_0／Q4_1 最後。"""
    q = (q or "").upper()
    if q.endswith(("_K_XL", "_K_M", "_K_L")) or "MXFP4" in q or not q:
        return 2
    if q.endswith("_K_S") or q.startswith(("IQ", "UD-IQ")) or q.endswith("_K"):
        return 1
    return 0


def kv_gib(prof, ctx, parallel=1):
    """與代理 llama_kv_gib 同一條公式（test_recommend 會拿兩邊對照）。"""
    swa = prof.get("swa_window") or ctx
    return (prof.get("kv_full_bpt", 0) * ctx * parallel
            + prof.get("kv_swa_bpt", 0) * min(ctx, swa) * parallel) / GIB


def repo_folder(repo):
    """代理下載時放的資料夾名（同 xconsole_agent.start_download 的 _slug 規則），用來認出已下載。"""
    owner, name = repo.split("/", 1)
    name = name.removesuffix("-GGUF").removesuffix("-gguf")
    return re.sub(r"[^a-z0-9._-]+", "-", f"{owner}--{name}".lower()).strip("-")[:80] or "model"


def _nice(b):
    return max((x for x in NICE_B if x <= b), default=0)


def capacity(state):
    """這台的「快速層」與「備援層」有多大（扣掉底線與引擎本身），以及能流暢跑多大的模型。
    用整台的容量，不是現在剩多少：推薦是「這台適合跑什麼」，真正換上時另有准入照當下剩餘量擋。"""
    pools = state.get("pools") or {}
    mm = pools.get("memory_model", "discrete")
    fl = pools.get("floors") or {}
    chip = state.get("chip") or {}
    out = {"memory_model": mm, "bandwidth_gbs": chip.get("bandwidth_gbs"), "bandwidth_src": chip.get("bandwidth_src"),
           "ram_bandwidth_gbs": chip.get("ram_bandwidth_gbs") or DEFAULT_RAM_BW,
           "ram_bw_assumed": not chip.get("ram_bandwidth_gbs"), "cards": len(pools.get("gpus") or [])}
    if mm == "unified_single":
        tot = (pools.get("unified") or {}).get("total_gib") or 0
        out.update(fast=max(0.0, tot - fl.get("ram", 12) - 1.5), slow=0.0,
                   tiers=[{"kind": "gpu", "label": "統一記憶體", "gib": round(tot - fl.get("ram", 12), 1)},
                          {"kind": "floor", "label": "保留給系統", "gib": fl.get("ram", 12)}],
                   note="GPU 與系統共用同一塊記憶體；載入前會先試算，不夠就擋下，不會把整台拖死")
    elif mm == "unified_split":
        v = (pools.get("vram") or {}).get("total_gib") or 0
        r = (pools.get("ram") or {}).get("total_gib") or 0
        out.update(fast=max(0.0, v - fl.get("vram", 4)), slow=0.0,
                   tiers=[{"kind": "gpu", "label": "GPU 那一塊", "gib": round(v - fl.get("vram", 4), 1)},
                          {"kind": "floor", "label": "系統那一塊", "gib": round(r, 1)}],
                   note="BIOS 把記憶體切成兩塊：模型放 GPU 那塊；系統那塊只給引擎，吃爆會把整台拖死，所以另外盯")
    else:
        v = sum(c.get("total_gib") or 0 for c in pools.get("gpus") or [])
        r = (pools.get("ram") or {}).get("total_gib") or 0
        fast = max(0.0, v - fl.get("vram", 2)) if v else 0.0
        slow = max(0.0, r - fl.get("ram", 8) - 2.5)
        out.update(fast=fast, slow=slow,
                   tiers=([{"kind": "gpu", "label": f"顯示卡 × {out['cards']}" if out["cards"] > 1 else "顯示卡",
                            "gib": round(fast, 1)}] if v else [])
                   + [{"kind": "ram", "label": "系統記憶體", "gib": round(slow, 1)}],
                   note=("顯示卡放不下的 MoE 模型，會自動把部分專家層放到系統記憶體繼續跑，只是比較慢"
                         if v else "這台沒有偵測到 GPU"))
    fast = out["fast"]
    out["fast"] = round(fast, 1)
    out["slow"] = round(out["slow"], 1)
    # 「放得下」只看記憶體；「流暢」還要夠快：稠密模型每個 token 讀整顆，頻寬 × 效率 ÷ 10 tok/s 就是流暢的大小上限
    fit_b = (fast - ENGINE_OVERHEAD - 4) / Q4_GIB_PER_B if fast > 6 else 0
    bw = out["bandwidth_gbs"]
    speed_b = EFFICIENCY * bw * 1e9 / SMOOTH_TOKS / GIB / Q4_GIB_PER_B if bw else None
    out["max_fit_b"] = _nice(fit_b)
    out["max_dense_b"] = _nice(min(fit_b, speed_b)) if speed_b is not None else _nice(fit_b)
    out["speed_known"] = speed_b is not None
    out["max_moe_b"] = _nice((fast + out["slow"] - ENGINE_OVERHEAD - 4) / Q4_GIB_PER_B) if out["slow"] and fast > 6 else 0
    return out


def plan_quant(cap, prof, q, mmproj_gib=0.0):
    """一個量化放在這台：等級（流暢／能跑／放不下）、放在哪、要多少、估計速度、自動分層幾層。"""
    size = q["size_gib"]
    ctx = min(CTX, prof.get("ctx_train") or CTX)
    kv = kv_gib(prof, ctx)
    need = size + mmproj_gib + ENGINE_OVERHEAD + kv
    act = prof.get("active_frac") or 1.0
    bw, rbw = cap.get("bandwidth_gbs"), cap["ram_bandwidth_gbs"]
    out = {"stem": q["stem"], "quant": q["quant"], "size_gib": size, "need_gib": round(need, 1),
           "ctx": ctx, "kv_gib": round(kv, 1), "cpu_moe": 0, "bits": quant_bits(q["quant"])}
    read = size * act * GIB
    fixed = (prof.get("layers") or 0) * LAYER_MS / 1000
    if need <= cap["fast"]:
        place = {"unified_single": "全部放進統一記憶體", "unified_split": "全部放進 GPU 那一塊"}.get(
            cap["memory_model"], f"分到 {cap['cards']} 張卡" if cap["cards"] > 1 else "全部放進顯示卡")
        # 多張卡是一層接一層輪流算（llama.cpp 預設依層切），頻寬不會相加，照單張卡算
        tok = 1 / (read / (EFFICIENCY * bw * 1e9) + fixed) if bw else None
        # 速度不知道就不說流暢（頻寬沒填的機器），先標「能跑」
        out.update(level="smooth" if tok is not None else "ok", place=place, tok_s=tok)
    elif cap["memory_model"] == "discrete" and cap["fast"] and prof.get("moe") and prof.get("expert_layer_frac"):
        per = prof["expert_layer_frac"] * size
        n = math.ceil((need - cap["fast"]) / per)
        moved = n * per
        if n <= prof.get("expert_layers", 0) and moved <= cap["slow"]:
            # 搬出去的專家每個 token 只讀被選中的那幾個；其餘照 GPU 頻寬
            cpu_read = moved * (prof.get("expert_used_frac") or act) * GIB
            tok = 1 / (((read - cpu_read) / (bw * 1e9) + cpu_read / (rbw * 1e9)) / EFFICIENCY + fixed) if bw else None
            out.update(level="ok", place=f"{n} 層專家放系統記憶體", tok_s=tok, cpu_moe=n)
        else:
            out.update(level="no", place=f"要約 {need:.0f} GiB，這台放不下", tok_s=None)
    else:
        out.update(level="no", place=f"要約 {need:.0f} GiB，這台最多 {cap['fast']:.0f} GiB", tok_s=None)
    if out["tok_s"] is not None:
        out["tok_s"] = round(out["tok_s"], 1)
        if out["level"] == "smooth" and out["tok_s"] < SMOOTH_TOKS:
            out["level"] = "ok"            # 放得下但太慢，不算流暢
    return out


def pick(plans):
    """建議下載哪個量化：
    1. 4～6 位元裡流暢的，挑位元數最高、同位元偏好 K 系列（Q6 以上品質差異已很小，卻要多一倍下載與慢一倍）；
    2. 這個 repo 根本沒有 4～6 位元時，才考慮流暢的 8 位元；
    3. 都不流暢，退到放得下的 4 位元裡最小的（求快）；
    4. 再不行挑放得下的最大一個。全放不下回 None。"""
    usable = [p for p in plans if p["level"] != "no" and p["bits"] and p["bits"] <= 8]
    key = lambda p: (p["bits"], quant_pref(p["quant"]), p["size_gib"])  # noqa: E731
    mid = [p for p in usable if p["level"] == "smooth" and 4 <= p["bits"] <= 6]
    if mid:
        return max(mid, key=key)
    if not any(p["bits"] and 4 <= p["bits"] <= 6 for p in plans):
        eight = [p for p in usable if p["level"] == "smooth" and p["bits"] == 8]
        if eight:
            return max(eight, key=key)
    four = [p for p in usable if p["bits"] == 4]
    if four:
        return min(four, key=lambda p: (-quant_pref(p["quant"]), p["size_gib"]))
    if usable:
        return max(usable, key=lambda p: p["size_gib"])
    # repo 只放了 F16／BF16（沒有任何量化）：放得下就推它，不要整顆標成放不下
    wide = [p for p in plans if p["level"] != "no"]
    return min(wide, key=lambda p: p["size_gib"]) if wide else None


def pick_mmproj(mmproj):
    """看圖要下載哪一個投影檔：有 F16 就用 F16（品質與大小的折衷，各家都提供），沒有才挑最小的。
    推薦算記憶體、磁碟檢查、實際下載都用這一個，數字才對得起來。"""
    rows = mmproj or []
    return next((x for x in rows if re.search(r"f16", x["path"], re.I) and not re.search(r"bf16", x["path"], re.I)), None) \
        or (min(rows, key=lambda x: x["gib"]) if rows else None)


def recommend(state, library, catalog):
    """這台的推薦結果。library 是代理 /library 的回應（認出已下載、使用中、下載中）。"""
    cap = capacity(state)
    lib = library or {}
    installed = {m["id"]: m for m in lib.get("gguf") or []}
    jobs = lib.get("downloads") or []
    items = []
    for i, e in enumerate(catalog):
        prof = e.get("profile") or {}
        if not prof or not e.get("quants"):
            items.append({**_public(e), "order": i, "plans": [], "pick": None, "level": "unknown"})
            continue
        proj = pick_mmproj(e.get("mmproj"))
        mm = proj["gib"] if proj else 0.0
        folder = repo_folder(e["repo"])
        plans = []
        for q in e["quants"]:
            p = plan_quant(cap, prof, q, mm)
            got = installed.get(f"{folder}/{q['stem']}")
            p["installed_id"] = got["id"] if got else None
            p["in_use"] = got.get("in_use") if got else None
            p["fits_disk"] = lib.get("free_gib") is None or q["size_gib"] + mm + 10 < lib["free_gib"]
            plans.append(p)
        best = pick(plans)
        mine = next((p for p in plans if p["installed_id"]), None)
        job = next((j for j in jobs if j.get("repo") == e["repo"] and j.get("state") in ("running", "queued")), None)
        # 等級：裝好的那個量化放得下就照它；裝了一個放不下的大量化，不能把另一個放得下的小量化一起藏起來
        lv = mine["level"] if mine and mine["level"] != "no" else (best or mine or {"level": "no"})["level"]
        items.append({**_public(e), "order": i, "plans": plans, "pick": best["stem"] if best else None,
                      "installed": mine["stem"] if mine else None, "mmproj_pick": proj,
                      "level": lv,
                      "job": {"id": job["id"], "done": job.get("done"), "total": job.get("total"),
                              "stem": job.get("stem")} if job else None})
    rank = {"smooth": 0, "ok": 1, "no": 2, "unknown": 3}
    order = sorted(items, key=lambda x: (rank[x["level"]], x["order"]))
    featured = next((x for x in order if x["level"] == "smooth"), None)
    return {"capacity": cap, "items": items, "top": [x["repo"] for x in order if x["level"] in ("smooth", "ok")][:3],
            "featured": featured["repo"] if featured else None,
            "assumptions": {"efficiency": EFFICIENCY, "ctx": CTX, "smooth_toks": SMOOTH_TOKS}}


def _public(e):
    return {k: e.get(k) for k in ("repo", "title", "use", "desc", "tags", "vendor", "license", "released",
                                  "mmproj", "refreshed", "match")} | {"vision": bool(e.get("mmproj")),
                                                             "profile": e.get("profile")}


# ---------------------------------------------------------------- 推薦清單的存放

class Catalog:
    """推薦清單：存在主控台（~/.xconsole/catalog.json），第一次用時從隨附的預設清單複製。"""

    def __init__(self, path, default_path):
        self.path, self.default_path = path, default_path
        self.lock = threading.Lock()

    def load(self):
        for p in (self.path, self.default_path):
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    return data
            except (OSError, ValueError):
                continue
        return []

    def _save(self, rows):
        tmp = self.path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def upsert(self, fields, hf=None):
        """fields：管理者在網頁上填的（名稱、用途、說明、標籤、廠商）。hf：代理 hf_profile 的回應，有就一起更新。"""
        repo = fields["repo"]
        with self.lock:
            rows = self.load()
            cur = next((r for r in rows if r["repo"] == repo), None)
            if cur is None:
                cur = {"repo": repo}
                rows.append(cur)
            for k in ("title", "use", "desc", "tags", "vendor", "match"):
                if k in fields:
                    cur[k] = fields[k]
            if hf:
                cur.update(hf_fields(hf))
            self._save(rows)
            return cur

    def delete(self, repo):
        with self.lock:
            rows = self.load()
            left = [r for r in rows if r["repo"] != repo]
            if len(left) == len(rows):
                return False
            self._save(left)
            return True

    def move(self, repo, step):
        with self.lock:
            rows = self.load()
            i = next((k for k, r in enumerate(rows) if r["repo"] == repo), None)
            if i is None:
                return False
            j = max(0, min(len(rows) - 1, i + step))
            rows.insert(j, rows.pop(i))
            self._save(rows)
            return True


def hf_fields(hf):
    """代理 hf_profile 的回應 → 推薦清單裡要存的欄位。只留主模型檔（MTP 草稿頭、子資料夾的不算）。"""
    card = hf.get("card") or {}
    return {"profile": hf["profile"], "profiled": hf.get("profiled"),
            "quants": [{"stem": c["stem"], "quant": c["quant"], "size_gib": c["size_gib"]}
                       for c in hf["choices"] if not c["aux"]],
            "mmproj": [{"path": p, "gib": g} for p, g in sorted((hf.get("mmproj_sizes") or {}).items(), key=lambda x: x[1])],
            "license": card.get("license"), "released": card.get("released"), "refreshed": int(time.time())}


def check_fields(body):
    """管理者送來的欄位檢查。回 (欄位, 錯誤)。"""
    repo = str(body.get("repo") or "").strip()
    if not REPO_RX.fullmatch(repo):
        return None, "Hugging Face 名稱格式是「作者/模型」，例：unsloth/gemma-4-E4B-it-GGUF"
    if any(c in repo.lower() for c in CN_ORIGIN):
        return None, "政府案不得使用中國來源模型，不列入推薦清單（內部測試可以直接到「從 Hugging Face 下載」）"
    out = {"repo": repo}
    title = str(body.get("title") or "").strip()
    if not 1 <= len(title) <= 60:
        return None, "顯示名稱要 1～60 字"
    out["title"] = title
    use = body.get("use") or "chat"
    if use not in USES:
        return None, "用途只能是：" + "、".join(USES.values())
    out["use"] = use
    for k, lim in (("desc", 160), ("vendor", 40)):
        v = str(body.get(k) or "").strip()
        if len(v) > lim or re.search(r"[\x00-\x1f]", v):
            return None, f"{'說明' if k == 'desc' else '來源'}最多 {lim} 字，不能有控制字元"
        out[k] = v
    tags = body.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,，、\s]+", tags) if t]
    if not isinstance(tags, list) or len(tags) > 6 or any(not isinstance(t, str) or len(t) > 12 for t in tags):
        return None, "標籤最多 6 個、每個 12 字以內"
    out["tags"] = tags
    match = str(body.get("match") or "").strip()
    if len(match) > 60 or not re.fullmatch(r"[\w.+-]*", match):
        return None, "「檔名要含」只能填英數、底線、點、加號與連字號，最多 60 字"
    out["match"] = match
    return out, None
