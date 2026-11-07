#!/usr/bin/env python3
"""其他模型資料夾（LM Studio、自己放的 C:\\models）的自我檢查：
  1. 存放區根目錄直接放的 .gguf 也認得（不必先建一層資料夾）
  2. 自動偵測：~/models、~/.lmstudio/models 這種有 GGUF 的資料夾列成建議；小檔（詞表）不算；
     預設存放區 ~/models/gguf 在 ~/models 底下，不會被算兩次
  3. 加入：要存在、不能是系統或隱藏資料夾（LM Studio 例外）、不能是自己的存放區；重複加入不重複；有上限
  4. 掃描：往下三層、巢狀加入的資料夾不重複、標出 external；推薦清單認得同名量化＝已安裝
  5. 刪除：外部資料夾的檔一律不刪；存放區根目錄的散檔可以刪，但不刪存放區本身
  6. 移出：有欄位在用就不准；移出後檔案還在
跑法：python3 test_model_dirs.py
"""
import io
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = tempfile.mkdtemp(prefix="xc-dirs-")
os.environ["XCONSOLE_HOME"] = HOME
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "console"))
import xconsole_agent as A  # noqa: E402
import recommend  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def gguf(path, mb=60, name="Test-8B"):
    """有效的 GGUF 檔頭，後面補成 mb MB 的稀疏檔（不真的寫那麼多）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    s = lambda t: struct.pack("<Q", len(t)) + t.encode()  # noqa: E731
    kv = {"general.architecture": "t", "general.name": name, "t.block_count": 32, "t.context_length": 8192,
          "t.attention.head_count": 8, "t.attention.head_count_kv": 2, "t.embedding_length": 1024}
    tensors = [("token_embd.weight", 0), ("output.weight", 4096), ("blk.0.attn_q.weight", 8192)]
    b = io.BytesIO()
    b.write(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensors), len(kv)))
    for k, v in kv.items():
        b.write(s(k) + (struct.pack("<I", 8) + s(v) if isinstance(v, str) else struct.pack("<I", 4) + struct.pack("<I", v)))
    for n, off in tensors:
        b.write(s(n) + struct.pack("<I", 1) + struct.pack("<Q", 1) + struct.pack("<I", 0) + struct.pack("<Q", off))
    with open(path, "wb") as fh:
        fh.write(b.getvalue())
        fh.truncate(mb << 20)


H = A.HOME
root = A.gguf_root()
gguf(os.path.join(root, "own", "own-Q4_K_M.gguf"))
gguf(os.path.join(root, "loose-Q8_0.gguf"))                                    # 1. 根目錄散放
ext = os.path.join(H, "models")                                               # ~/models：預設存放區的上一層
gguf(os.path.join(ext, "granite-4.2-30b-Q4_K_M.gguf"))
gguf(os.path.join(ext, "vocab-only.gguf"), mb=2)                              # 小檔不算
gguf(os.path.join(ext, "imatrix_unsloth.gguf"))                              # 量化校正資料不是模型
lms = os.path.join(H, ".lmstudio", "models")
gguf(os.path.join(lms, "lmstudio-community", "gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q4_K_M.gguf"))
gguf(os.path.join(lms, "a", "b", "c", "d", "too-deep-Q4_K_M.gguf"))           # 第四層以下不找

_, ms, _ = A.scan_gguf()
ids = {m["id"] for m in ms}
case("1. 存放區根目錄的散檔認得", "./loose-Q8_0" in ids, ids)
case("   存放區子資料夾照舊", "own/own-Q4_K_M" in ids, ids)
case("   還沒加入的外部資料夾不列", not any(m.get("external") for m in ms))

sug = {x["path"]: x for x in A.model_dir_suggestions()}
r_ext, r_lms = os.path.realpath(ext), os.path.realpath(lms)
case("2. 偵測到 ~/models", r_ext in sug, sug)
case("   ~/models 只算外面那 1 顆（存放區裡的不重算、詞表小檔不算）", sug.get(r_ext, {}).get("count") == 1, sug.get(r_ext))
case("   偵測到 LM Studio 的隱藏資料夾，往下三層", sug.get(r_lms, {}).get("count") == 1, sug.get(r_lms))

case("3. 不存在的資料夾不能加", A.set_model_dirs({"add": os.path.join(H, "nope")})[0] == 400)
case("   自己的存放區不能加", A.set_model_dirs({"add": root})[0] == 400)
os.makedirs(os.path.join(H, ".ssh"), exist_ok=True)
case("   隱藏資料夾不能加（LM Studio 以外）", A.set_model_dirs({"add": os.path.join(H, ".ssh")})[0] == 400)
case("   要帶 add 或 remove", A.set_model_dirs({})[0] == 400)
c1, _ = A.set_model_dirs({"add": ext})
c2, _ = A.set_model_dirs({"add": lms})
c3, out = A.set_model_dirs({"add": ext + os.sep})
case("   加入 ~/models 與 LM Studio", c1 == 200 and c2 == 200, (c1, c2))
case("   重複加入不重複", c3 == 200 and len(out["dirs"]) == 2, out)
A._SUGGEST["at"] = 0
case("   加入後不再列成建議", not A.model_dir_suggestions())

_, ms, _ = A.scan_gguf()
exts = [m for m in ms if m.get("external")]
case("4. 掃到兩顆外部模型（太深的不算），名稱用檔名（不用檔頭裡的訓練代號）",
     sorted(m["name"] for m in exts) == ["gemma-4-E4B-it", "granite-4.2-30b"], [m["name"] for m in exts])
case("   存放區的模型照舊用檔頭名稱", next(m["name"] for m in ms if m["id"] == "own/own-Q4_K_M") == "Test-8B")
case("   存放區裡的模型沒有被當成外部的再列一次", sum(1 for m in ms if m["path"].endswith("own-Q4_K_M.gguf")) == 1)
case("   外部模型的 id 帶資料夾代號", all(m["id"].startswith(A.dir_tag(m["external"]) + "/") for m in exts), [m["id"] for m in exts])
sub = os.path.join(lms, "lmstudio-community")
A.set_model_dirs({"add": sub})
_, ms2, _ = A.scan_gguf()
case("   巢狀加入的資料夾不會重複列", sum(1 for m in ms2 if m["path"].endswith("gemma-4-E4B-it-Q4_K_M.gguf")) == 1)
A.set_model_dirs({"remove": sub})
lib = {"gguf": ms, "downloads": []}
cat = [{"repo": "unsloth/granite-4.2-30b-GGUF", "title": "g", "use": "chat",
        "profile": {"arch": "t", "layers": 32, "kv_full_bpt": 1000, "active_frac": 1.0, "ctx_train": 8192},
        "quants": [{"stem": "granite-4.2-30b-Q4_K_M", "quant": "Q4_K_M",
                    "size_gib": next(m["size_gib"] for m in exts if "granite" in m["path"])}]}]
st = {"pools": {"memory_model": "unified_single", "unified": {"total_gib": 64, "used_gib": 4}, "ram": {"total_gib": 64, "free_gib": 60}},
      "machine": {"memory_model": "unified_single"}, "chip": {"bandwidth_gbs": 120}, "slots": []}
rec = recommend.recommend(st, lib, cat)
case("   推薦清單認得外部資料夾裡同名的量化＝已安裝", rec["items"][0].get("installed") == "granite-4.2-30b-Q4_K_M"
     or any(p.get("installed_id") for p in rec["items"][0]["plans"]), rec["items"][0].get("installed"))

e = exts[0]
code, out = A.delete_model(e["id"])
case("5. 外部資料夾的檔不刪", code == 400 and os.path.exists(e["path"]), (code, out))
code, out = A.delete_model("./loose-Q8_0")
case("   存放區根目錄的散檔可以刪，存放區本身還在", code == 200 and not os.path.exists(os.path.join(root, "loose-Q8_0.gguf"))
     and os.path.isdir(root), (code, out))

reg = A.registry()
cfg = os.path.join(H, "slot.json")
reg["slots"].append({"id": "s1", "kind": "llama", "config": cfg})
A.save_json(A.REGISTRY, reg)
orig = A.slot_config
A.slot_config = lambda sl: {"_native": e["path"]} if sl.get("id") == "s1" else orig(sl)
code, out = A.set_model_dirs({"remove": e["external"]})
case("6. 有欄位在用的資料夾不准移出", code == 409, (code, out))
A.slot_config = orig
reg = A.registry()
reg["slots"] = [s for s in reg["slots"] if s["id"] != "s1"]
A.save_json(A.REGISTRY, reg)
code, out = A.set_model_dirs({"remove": e["external"]})
case("   移出後檔案還在", code == 200 and os.path.exists(e["path"]), (code, out))
case("   移出不在清單的回 404", A.set_model_dirs({"remove": os.path.join(H, "zzz")})[0] == 404)
case("   瀏覽器：LM Studio 資料夾可以選、.ssh 不行",
     A.model_dir_browse(lms)[1]["ok"] and not A.model_dir_browse(os.path.join(H, ".ssh"))[1]["ok"])

# ---- Codex 審查補的幾種情況
flat = os.path.join(H, "AI-Models")                                           # 好幾顆平放、只有一個投影檔
gguf(os.path.join(flat, "Muse-Glimmer-30B-UD-Q4_K_XL.gguf"))
gguf(os.path.join(flat, "Llama-3.2-3B-Instruct-Q4_K_M.gguf"))
gguf(os.path.join(flat, "mmproj-Muse-Glimmer-30B-BF16.gguf"), mb=1)
A.set_model_dirs({"add": flat})
_, ms3, _ = A.scan_gguf()
mm = {os.path.basename(m["path"]): os.path.basename(m["mmproj"] or "") for m in ms3 if m.get("external") == os.path.realpath(flat)}
case("7. 平放的資料夾：投影檔只配給檔名對得上的那顆", mm == {"Muse-Glimmer-30B-UD-Q4_K_XL.gguf": "mmproj-Muse-Glimmer-30B-BF16.gguf",
                                                     "Llama-3.2-3B-Instruct-Q4_K_M.gguf": ""}, mm)
one = os.path.join(lms, "solo")                                               # 資料夾裡只有一顆：投影檔就是它的
gguf(os.path.join(one, "gemma-3-4b-it-Q4_K_M.gguf"))
gguf(os.path.join(one, "mmproj-F16.gguf"), mb=1)
_, ms3, _ = A.scan_gguf()
case("   資料夾裡只有一顆模型：投影檔照配", any(m["path"].endswith("gemma-3-4b-it-Q4_K_M.gguf") and m["mmproj"] for m in ms3))

same = os.path.join(H, "same")                                                # 同名不同顆：只看檔名會配錯
gguf(os.path.join(same, "model-Q4_K_M.gguf"), mb=2048)
A.set_model_dirs({"add": same})
gguf(os.path.join(root, "rootonly-Q6_K.gguf"), mb=1024)                       # 存放區根目錄的散檔
_, ms4, _ = A.scan_gguf()
prof = {"arch": "t", "layers": 32, "kv_full_bpt": 1000, "active_frac": 1.0, "ctx_train": 8192}
cat2 = [{"repo": f"x/{n}-GGUF", "title": n, "use": "chat", "profile": prof,
         "quants": [{"stem": "model-Q4_K_M", "quant": "Q4_K_M", "size_gib": g}]} for n, g in (("a", 3.0), ("b", 2.0), ("c", 5.0))]
cat2.append({"repo": "x/rootonly-GGUF", "title": "r", "use": "chat", "profile": prof,
             "quants": [{"stem": "rootonly-Q6_K", "quant": "Q6_K", "size_gib": 1.0}]})
rec2 = recommend.recommend(st, {"gguf": ms4, "downloads": []}, cat2)
got = {it["repo"]: any(p["installed_id"] for p in it["plans"]) for it in rec2["items"]}
case("   同名不同顆：只有大小對得上的那顆算已安裝", got == {"x/a-GGUF": False, "x/b-GGUF": True, "x/c-GGUF": False, "x/rootonly-GGUF": True}, got)

up = os.path.join(root, "upper")
gguf(os.path.join(up, "SHOUT-Q4_K_M.GGUF"))
code, out = A.delete_model("upper/SHOUT-Q4_K_M")
case("8. 大寫副檔名 .GGUF：刪得掉", code == 200 and not os.path.exists(os.path.join(up, "SHOUT-Q4_K_M.GGUF")), (code, out))

outside = tempfile.mkdtemp(prefix="xc-mnt-")                                  # 像 Linux 的 /models、另外掛的資料碟
orig_bases = A.data_bases
A.data_bases = lambda: [H, outside]
os.makedirs(os.path.join(outside, "llm"), exist_ok=True)
code, out = A.model_dir_browse(outside)
case("9. 瀏覽器：check_model_dir 收的位置都瀏覽得到", code == 200 and any(d["name"] == "llm" for d in out["dirs"]), (code, out))
code, _ = A.agent_browse(outside) if A.agent_enabled() else (400, None)
case("   Agent 資料夾的瀏覽範圍沒跟著放寬", code != 200)
A.data_bases = orig_bases

print("全部通過" if OK else "有項目失敗")
sys.exit(0 if OK else 1)
