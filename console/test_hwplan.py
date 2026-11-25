#!/usr/bin/env python3
"""混合機型自動挑版本（清單 #14）：主控台依每台的硬體分機型，用推薦清單與准入同一條公式替每種機型挑量化。
驗：機型分得開、沒寫量化或寫 auto 才挑、挑出來的跟推薦清單一致、放不下給替代方案、清單外的講清楚、
中國來源照標、依機型覆寫只影響那一種、讀不到硬體不送設定、檢視帳號看得到機型表。
跑法：python3 test_hwplan.py（不開網路、不碰正式環境）
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-hwplan-")
sys.path.insert(0, HERE)
import groups  # noqa: E402
import xconsole_server as X  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


def state(mm, bw, gpu, **pools):
    return {"pools": {"memory_model": mm, **pools}, "chip": {"bandwidth_gbs": bw, "gpu": gpu}}


AI395 = state("unified_split", 256, "Radeon 8060S", floors={"ram": 15, "vram": 4}, vram={"total_gib": 96}, ram={"total_gib": 31},
              gpus=[{"id": "0", "total_gib": 96}])
ADA2 = state("discrete", 960, "RTX 6000 Ada", floors={"ram": 8, "vram": 2}, ram={"total_gib": 212},
             gpus=[{"id": "0", "total_gib": 48}, {"id": "1", "total_gib": 48}])
LAPTOP = state("discrete", None, None, floors={"ram": 8, "vram": 2}, ram={"total_gib": 16}, gpus=[])
LAPTOP["chip"]["cpu"] = "Core i5"
HW = {"lab-1": AI395, "lab-2": AI395, "ws-1": ADA2, "nb-1": LAPTOP}
X.node_hw = lambda nid: HW.get(nid)

keys = {k: X.hw_class(v) for k, v in (("ai", AI395), ("ada", ADA2), ("nb", LAPTOP))}
check("機型分得開：記憶體架構、顯示卡或 CPU、可放模型的記憶體", len({k for k, _ in keys.values()}) == 3, keys)
check("  名稱講人話", "Radeon 8060S" in keys["ai"][1] and "只有 CPU" in keys["nb"][1] and "GiB" in keys["ada"][1], keys)
check("寫明量化就不挑", not X.needs_pick({"models": ["a/b:Q4_K_M"]}))
check("  寫 auto、沒寫量化或有依機型覆寫才挑",
      X.needs_pick({"models": ["auto"]}) and X.needs_pick({"models": ["a/b"]}) and X.needs_pick({"models": ["a/b:Q4_K_M"], "overrides": {"x": ["c/d:Q8_0"]}}))

REC = {"items": [{"repo": "a/b", "cn_origin": False, "pick": "m-Q4_K_M",
                  "plans": [{"quant": "Q8_0", "stem": "m-Q8_0", "level": "no", "tok_s": None, "place": "放不下"},
                            {"quant": "Q4_K_M", "stem": "m-Q4_K_M", "level": "smooth", "tok_s": 31.5, "place": "全部放進顯示卡"}]},
                 {"repo": "Qwen/Qwen3-8B-GGUF", "cn_origin": True, "pick": "m-Q4_K_M",
                  "plans": [{"quant": "Q4_K_M", "stem": "m-Q4_K_M", "level": "ok", "tok_s": 12.0, "place": "x"}]}],
       "featured": "a/b", "top": ["a/b"]}
r = X.resolve_spec("a/b", REC)
check("沒寫量化：照推薦清單替這種機型挑", r["spec"] == "a/b:Q4_K_M" and r["level"] == "smooth" and r["tok_s"] == 31.5, r)
r = X.resolve_spec("auto", REC)
check("  auto：挑這種機型最適合的那顆", r["spec"] == "a/b:Q4_K_M", r)
r = X.resolve_spec("a/b:Q8_0", REC)
check("  寫明的量化放不下：照用、標放不下，給這種機型放得下的替代", r["spec"] == "a/b:Q8_0" and r["level"] == "no"
      and r["alt"] == "a/b:Q4_K_M", r)
r = X.resolve_spec("x/not-in-catalog", REC)
check("  推薦清單外又沒寫量化：講清楚要怎麼寫", r["spec"] is None and "寫明量化" in r["why"], r)
check("  中國來源照標（只標示）", X.resolve_spec("Qwen/Qwen3-8B-GGUF", REC)["cn"] is True)
check("  推薦清單裡沒有跑得動的：auto 講清楚", X.resolve_spec("auto", {"items": [], "featured": None, "top": []})["spec"] is None)

big = next(e["repo"] for e in X.CATALOG.load() if e["repo"].endswith("Gemma-4-31B-xVITA-zhTW-GGUF"))
nodes = [{"id": i, "name": i, "group": "教室"} for i in ("lab-1", "lab-2", "ws-1", "nb-1", "off-1")]
g = {"id": "g1", "name": "教室", "settings": groups.check({"models": [big]})}
a, _ = X.node_models(g, nodes[0])
b, _ = X.node_models(g, nodes[2])
c, _ = X.node_models(g, nodes[3])
check("真的推薦清單：同一顆模型，不同機型挑到不同量化", a and b and c and len({a[0], b[0], c[0]}) >= 2 and all(":" in x[0] for x in (a, b, c)),
      (a, b, c))
check("  同機型的兩台挑到一樣的（同群組互傳才對得上）", X.node_models(g, nodes[1])[0] == a)
models, why = X.node_models(g, nodes[4])
check("讀不到硬體：先不送設定，講原因", models is None and "讀不到這台的硬體" in why, why)
g2 = {**g, "settings": groups.check({"models": [big], "overrides": {keys["nb"][0]: [f"{big}:Q2_K"]}})}
check("依機型覆寫：只改那一種機型", X.node_models(g2, nodes[3])[0] == [f"{big}:Q2_K"] and X.node_models(g2, nodes[2])[0] == b)
p = X.group_plan(g2, nodes)
check("群組設定頁的機型表：三種機型、每種幾台、讀不到硬體的另外列",
      len(p["classes"]) == 3 and sorted(len(c["nodes"]) for c in p["classes"]) == [1, 1, 2] and p["unknown"] == ["off-1"], p["unknown"])
nb = next(c for c in p["classes"] if c["key"] == keys["nb"][0])
check("  覆寫的那種機型顯示覆寫內容與它的估算", nb["override"] == [f"{big}:Q2_K"] and nb["rows"][0]["model"] == f"{big}:Q2_K"
      and "level_name" in nb["rows"][0])
check("  寫明速度是估算", "估算" in p["note"])

try:
    groups.check({"overrides": {"x|y|1": ["../etc"]}})
    bad = False
except ValueError:
    bad = True
check("覆寫的模型名稱照樣檢查", bad)
check("  覆寫空的就照群組設定，不存", groups.check({"overrides": {"x|y|1": []}})["overrides"] == {})
check("檢視帳號看得到機型表、使用者看不到", X.user_may("GET", "/api/groups/abcdef12/plan", "viewer")
      and not X.user_may("GET", "/api/groups/abcdef12/plan", "user"))

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
