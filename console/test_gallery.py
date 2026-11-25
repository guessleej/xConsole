#!/usr/bin/env python3
"""畫圖、影片頁每天會用到的五件事的自我檢查：
1. 作品資料夾：代理的「開啟位置」放行作品資料夾本身，「..」、捷徑、旁邊同名開頭的資料夾都跳不出去；
   主控台只在桌面版、代理在這台、看網頁的人也在這台時才真的打開，其餘只給路徑
2. 畫圖模型標能力（能畫也能改圖／只能改圖／只能畫圖），清單標籤跟著參考圖用法走；只能改圖沒給圖時產生鈕先停用
3. 表單記在瀏覽器：每台、畫圖影片分開，localStorage壞掉也照常開頁，參考圖不記
4. 作品牆：代理保留300件、縮圖用標準函式庫縮好快取；畫面一批固定件數、換批整批取代不往下接
5. 全螢幕檢視器：左右鍵切換、Esc關閉並拿掉鍵盤監聽、點背景關閉
跑法：python3 test_gallery.py（隔離HOME，不連外；第2～5項的畫面邏輯用node實跑）
"""
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

HOME = tempfile.mkdtemp(prefix="xc-gallery-")
os.environ["HOME"] = HOME
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "agent")]
import recommend as R  # noqa: E402
import xconsole_agent as A  # noqa: E402
import xconsole_server as X  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def mk_png(path, w, h, px, ct=2, depth=8):
    """寫一張PNG：px(x, y) 回這個像素的通道值；每列輪流用五種濾鏡，確認解碼每一種都對。"""
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}[ct]
    rows, prev = [], bytes(w * bpp)
    for y in range(h):
        raw = bytes(v for x in range(w) for v in px(x, y))
        f = y % 5
        out = bytearray()
        for i, v in enumerate(raw):
            a = raw[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pred = (0, a, b, (a + b) >> 1, a if abs(p - a) <= abs(p - b) and abs(p - a) <= abs(p - c) else b if abs(p - b) <= abs(p - c) else c)[f]
            out.append((v - pred) & 255)
        rows.append(bytes([f]) + bytes(out))
        prev = raw

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, ct, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def decode(path):
    with open(path, "rb") as fh:
        w, h, ct, bpp, rows = A._png_decode(fh.read())
    return w, h, ct, [bytes(r) for r in rows]


# ================================================================ 1. 作品資料夾
A.registry()
gen = os.path.realpath(A.GEN_DIR)
os.makedirs(gen, exist_ok=True)
models = A.gguf_root()
os.makedirs(os.path.join(models, "qwen"), exist_ok=True)
check("作品資料夾本身打得開", A.desktop_open_target(A.GEN_DIR) == gen)
check("  寫成帶尾斜線或「.」也認得（先正規化再比）", A.desktop_open_target(A.GEN_DIR + "/./") == gen)
check("  「..」往上跳到 ~/.xconsole：擋", A.desktop_open_target(os.path.join(A.GEN_DIR, "..")) is None)
check("  「..」跳到家目錄：擋", A.desktop_open_target(os.path.join(A.GEN_DIR, "..", "..")) is None)
os.makedirs(os.path.join(gen, "sub"), exist_ok=True)
check("  作品資料夾底下的子資料夾：擋（只放行那一個資料夾）", A.desktop_open_target(os.path.join(gen, "sub")) is None)
os.makedirs(gen + "2", exist_ok=True)
check("  名稱開頭一樣的旁邊資料夾（gen2）：擋", A.desktop_open_target(gen + "2") is None)
link = os.path.join(models, "qwen", "escape")
os.symlink(os.path.dirname(gen), link)
check("  模型存放區裡指向外面的捷徑：還原成真正位置後擋", A.desktop_open_target(link) is None)
check("  空字串、含NUL：擋", A.desktop_open_target("") is None and A.desktop_open_target(gen + "\x00") is None)
check("  原本的模型存放區底下照樣打得開", A.desktop_open_target(os.path.join(models, "qwen")) == os.path.realpath(os.path.join(models, "qwen")))
src = open(A.__file__, encoding="utf-8").read()
handler = src[src.index('if path == "/desktop/open":'):][:600]
check("  代理的 /desktop/open走同一道檢查", "desktop_open_target(body.get(\"path\"))" in handler)
check("  /sd/jobs回報作品資料夾位置", '{"jobs": sd_jobs_list(), "dir": GEN_DIR}' in src)


class FakeReq:
    def __init__(self, ip, loopback=False):
        self.ip, self.loopback = ip, loopback


def folder(desktop, node_host, viewer_ip, agent_out=None):
    X.DESKTOP = desktop
    X.update_fleet(lambda c: c.__setitem__("nodes", [{"id": "n1", "name": "這台", "host": node_host, "token": ""}]))
    real = X.call_node
    X.call_node = lambda nid, path, *a, **k: (200, agent_out if agent_out is not None else {"jobs": [], "dir": gen})
    try:
        return X.Handler._sd_folder(FakeReq(viewer_ip), "n1")
    finally:
        X.call_node = real


code, out = folder(True, "127.0.0.1", "127.0.0.1")
check("桌面版、代理在這台、人在這台：可以直接打開", (code, out.get("can_open"), out.get("path")) == (200, True, gen), out)
check("  網頁版（不是桌面版）：只給路徑", folder(False, "127.0.0.1", "127.0.0.1")[1].get("can_open") is False)
check("  代理在別台（192.0.2.10）：只給路徑", folder(True, "192.0.2.10", "127.0.0.1")[1].get("can_open") is False)
check("  看網頁的人從別台連進來：只給路徑（不在他看不到的螢幕上開視窗）",
      folder(True, "127.0.0.1", "192.0.2.50")[1].get("can_open") is False)
check("  舊代理不回報資料夾：講清楚要更新，不亂猜路徑", folder(True, "127.0.0.1", "127.0.0.1", {"jobs": []})[0] == 409)
check("節點位址判斷：127.0.0.1是這台、文件範例位址不是", X.node_on_this_machine("127.0.0.1") and not X.node_on_this_machine("192.0.2.10"))
X.DESKTOP = False
ssrc = open(X.__file__, encoding="utf-8").read()
opener = ssrc[ssrc.index('m = re.fullmatch(r"/api/node/([\\w.-]+)/sd/folder/open", path)'):][:700]
check("  主控台的打開只送代理回報的作品資料夾，不收網頁帶來的路徑",
      'if not info["can_open"]' in opener and '{"path": info["path"]}' in opener and "body.get" not in opener)

# ================================================================ 2. 畫圖模型標能力
check("改圖能力：ref＋needs_image＝只能改圖、ref＝能畫也能改圖、其他＝只能畫圖、影片不標",
      [R.sd_edit_cap("image", {"image": "ref", "needs_image": True}), R.sd_edit_cap("image", {"image": "ref"}),
       R.sd_edit_cap("image", {}), R.sd_edit_cap("video", {"image": "ref"})] == ["only", "both", "draw", None])
cat = json.load(open(os.path.join(HERE, "sd_catalog_default.json"), encoding="utf-8"))


def cap_tags(e):
    want = R.SD_EDIT_TAGS.get(R.sd_edit_cap("image", e["defaults"]))
    return {t for t in e["tags"] if t in R.SD_EDIT_TAGS.values()} == ({want} if want else set())


bad = [e["title"] for e in cat if e["kind"] == "image" and not cap_tags(e)]
check("內建清單：每顆畫圖模型的「能改圖／只能改圖」標籤都跟參考圖用法一致", not bad, bad)
caps = sorted({R.sd_edit_cap("image", e["defaults"]) for e in cat if e["kind"] == "image"})
check("  內建清單三種能力都有（才測得到三種標記）", caps == ["both", "draw", "only"], caps)
body = {"repo": "a/b", "title": "改圖", "kind": "image", "tags": ["畫圖", "能改圖"],
        "parts": [{"role": "diffusion", "repo": "a/b", "path": "m.gguf"}],
        "defaults": {"width": 1024, "height": 1024, "steps": 8, "cfg": 1, "image": "ref", "needs_image": True}}
e, err = R.check_sd_fields(body)
check("管理者把用法改成只能改圖：標籤自動換成「只能改圖」", err is None and e["tags"] == ["畫圖", "只能改圖"], (err, e and e["tags"]))
e, err = R.check_sd_fields({**body, "defaults": {**body["defaults"], "image": "init", "needs_image": False}})
check("  改回一般畫圖：「能改圖」標籤拿掉", err is None and e["tags"] == ["畫圖"], (err, e and e["tags"]))
e, err = R.check_sd_fields({**body, "tags": ["畫圖", "a", "b", "c", "d", "e"]})
check("  加上自動標籤後超過6個：擋下並說明", e is None and "6" in err)
rec = R.recommend_sd({"pools": {"memory_model": "discrete"}}, {"models": [
    {"id": "q", "kind": "image", "defaults": {"image": "ref", "needs_image": True}}, {"id": "v", "kind": "video", "defaults": {}}]}, [])
check("推薦結果的已下載模型帶edit欄位，網頁下拉直接用", [m.get("edit") for m in rec["models"]] == ["only", None])

# ================================================================ 4. 作品保留與縮圖
check("代理保留至少300件作品（原本只留30件）", A.SD_KEEP >= 300, A.SD_KEEP)
A.SD_JOBS_FILE = os.path.join(HOME, "jobs.json")
jobs = []
for k in range(A.SD_KEEP + 5):
    jid = f"{k:08x}"
    for f in (f"{jid}.png", f"{jid}-t0.png"):
        open(os.path.join(gen, f), "wb").write(b"x")
    jobs.append({"id": jid, "kind": "image", "state": "done", "out": f"{jid}.png"})
A._GEN["jobs"] = jobs
with A._GEN_LOCK:
    A._sd_trim_locked()
gone = [f"{k:08x}" for k in range(A.SD_KEEP, A.SD_KEEP + 5)]
check("超過保留件數：最舊的連產出與縮圖一起刪，新的都在",
      len(A._GEN["jobs"]) == A.SD_KEEP and not any(os.path.exists(os.path.join(gen, f"{j}{s}")) for j in gone for s in (".png", "-t0.png"))
      and os.path.exists(os.path.join(gen, "00000000.png")))

rnd = random.Random(7)
big = os.path.join(gen, "aaaa0001.png")
mk_png(big, 400, 200, lambda x, y: (rnd.randrange(256), (x * 3) & 255, (y * 5) & 255))
_, _, _, src_rows = decode(big)
tdst = os.path.join(gen, "t.png")
check("縮圖：PNG縮成長邊384、保留原比例", A.png_thumb(big, tdst) and decode(tdst)[:3] == (384, 192, 2), decode(tdst)[:3])
tw, th, _, t_rows = decode(tdst)
k = 400 / 384
same = all(t_rows[ty][tx * 3:tx * 3 + 3] == src_rows[min(199, int(ty * k))][min(399, int(tx * k)) * 3:min(399, int(tx * k)) * 3 + 3]
           for ty in range(0, th, 17) for tx in range(0, tw, 23))
check("  縮圖像素取自原圖對應位置（五種掃描線濾鏡都解對）", same)
mk_png(os.path.join(gen, "g.png"), 20, 10, lambda x, y: (x * 10,), ct=0)
A.png_thumb(os.path.join(gen, "g.png"), tdst)
check("  灰階轉成RGB、小圖不放大", decode(tdst)[:3] == (20, 10, 2) and decode(tdst)[3][0][3:6] == bytes([10, 10, 10]))
mk_png(os.path.join(gen, "a.png"), 8, 8, lambda x, y: (255, 0, 0, x * 30), ct=6)
A.png_thumb(os.path.join(gen, "a.png"), tdst)
check("  透明度保留（RGBA）", decode(tdst)[2] == 6 and decode(tdst)[3][0][4:8] == bytes([255, 0, 0, 30]))
mk_png(os.path.join(gen, "d16.png"), 4, 4, lambda x, y: (1, 2, 3), depth=16)
check("  看不懂的格式（16位元）回False，不丟例外", A.png_thumb(os.path.join(gen, "d16.png"), tdst) is False)
check("  原本的單色檢查照樣算得對（共用同一段解碼）", tuple(round(v) for v in A.png_stats(os.path.join(gen, "a.png"))) == (85, 0))

A._GEN["jobs"] = [{"id": "aaaa0001", "kind": "image", "state": "done", "out": "aaaa0001.png"},
                  {"id": "bbbb0002", "kind": "video", "state": "done", "out": "bbbb0002.webm"},
                  {"id": "cccc0003", "kind": "image", "state": "running", "out": "cccc0003.png"}]
open(os.path.join(gen, "bbbb0002.webm"), "wb").write(b"\x1a\x45\xdf\xa3webm")
code, out = A.sd_thumb("aaaa0001", 0)
cached = os.path.join(gen, "aaaa0001-t0.png")
check("作品牆縮圖：第一次現縮並存成快取", code == 200 and out.get("thumb") and os.path.exists(cached))
os.utime(cached, (1, 1))
A.sd_thumb("aaaa0001", 0)
check("  第二次直接讀快取，不重縮", os.stat(cached).st_mtime == 1)
code, out = A.sd_thumb("bbbb0002", 0)
check("  影片：回原檔（網頁拿來當第一格畫面）", code == 200 and out.get("mime") == "video/webm" and not out.get("thumb"))
check("  還在產的、沒有這一張：404", A.sd_thumb("cccc0003", 0)[0] == 404 and A.sd_thumb("aaaa0001", 3)[0] == 404)
A._sd_files_rm(A._GEN["jobs"][0])
check("  刪作品時縮圖一起刪", not os.path.exists(cached))
check("  產好就先縮好（作品牆第一次打開不必等）", 'if j["state"] == "done" and j["kind"] == "image":' in src
      and "sd_thumb(j[\"id\"], i)" in src[src.index("def _gen_run"):src.index("def tail_text")])
check("主控台：縮圖路由一般使用者也能用、只看得到自己的",
      any(m == "GET" and re.fullmatch(rx, "/api/node/n1/sd/jobs/aaaa0001/thumb/0") for m, rx in X.USER_ROUTES)
      and "/thumb/[0-3]" in ssrc[ssrc.index('sd/jobs(/[0-9a-f]{8}(?:/output'):][:120])

# ================================================================ 2～5. 畫面邏輯（node實跑）
APP = open(os.path.join(HERE, "static", "app.js"), encoding="utf-8").read()
PAGES = open(os.path.join(HERE, "static", "pages.js"), encoding="utf-8").read()
CHAT = open(os.path.join(HERE, "static", "chat.js"), encoding="utf-8").read()
gen_fn = APP[APP.index("function sdGen("):APP.index("function sdOutIdle(")]
check("產生鈕：這顆一定要圖、還沒選圖時停用並寫出原因",
      "needs_image && !initState.b64" in gen_fn and "go.disabled = gate.busy || !gate.admit || need" in gen_fn and "先選一張要修改的圖片" in gen_fn)
check("  選了圖、拿掉圖都會重新判斷", "syncGo();\n  };\n  initPick" in gen_fn)
check("表單：輸入就記、進頁面先套模型預設再蓋上記住的值", 'left.addEventListener("input", remember)' in gen_fn
      and gen_fn.index("applyDefaults(model());\n    if (saved) restoreSaved(saved);") > 0)
check("  參考圖不進localStorage", "init" not in APP[APP.index("const SD_FORM_FIELDS"):APP.index("const sdFormKey")])
check("作品資料夾按鈕：畫圖、影片頁首與對話作品都有", "head.appendChild(sdFolderButton(n.id))" in PAGES and "sdFolderButton(g.node" in CHAT)
wall_fn = APP[APP.index("function sdWall("):APP.index("function sdViewer(")]
check("作品牆換批整批取代：只用replaceChildren，沒有把格子接在後面", "grid.replaceChildren(" in wall_fn
      and "grid.appendChild(tile" not in wall_fn and "載入更多" not in APP)


def block(start, end):
    return APP[APP.index(start):APP.index(end)]


JS = r"""
const assert = (name, cond, detail) => { console.log((cond ? "[通過] " : "[失敗] ") + name + (cond ? "" : "：" + JSON.stringify(detail))); if (!cond) process.exitCode = 1; };
class Node {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {}; this.dataset = {}; this.style = { setProperty() {} };
    this._text = ""; this.listeners = {}; this.parent = null; this.hidden = false; this.disabled = false;
    const cls = new Set(); this.classList = { add: (...c) => c.forEach((x) => cls.add(x)), remove: (c) => cls.delete(c),
      toggle: (c, on) => { if (on ?? !cls.has(c)) cls.add(c); else cls.delete(c); }, contains: (c) => cls.has(c) }; }
  append(...k) { k.forEach((c) => { if (typeof c === "string") c = new Text(c); c.parent = this; this.children.push(c); }); }
  appendChild(c) { this.append(c); return c; }
  replaceChildren(...k) { this.children = []; this.append(...k); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); this.parent = null; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === HOST_ROOT; }
  set textContent(v) { this.children = []; this._text = String(v); } get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); } removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  click() { (this.listeners.click || []).forEach((f) => f({ target: this })); this.onclick?.(); }
  focus() { global.document.activeElement = this; }
  find(fn) { if (fn(this)) return this; for (const c of this.children) { const r = c.find?.(fn); if (r) return r; } return null; }
}
class Text { constructor(v) { this.textContent = v; } }
const HOST_ROOT = new Node("body");
const HOST = new Node("div"); HOST_ROOT.append(HOST);
const docL = {};
global.document = { activeElement: null, addEventListener: (t, f, c) => { (docL[t] = docL[t] || []).push(f); },
  removeEventListener: (t, f) => { docL[t] = (docL[t] || []).filter((x) => x !== f); }, createElement: (t) => new Node(t),
  documentElement: new Node("html") };
const key = (k, target) => (docL.keydown || []).slice().forEach((f) => f({ key: k, target: target || {}, preventDefault() {}, stopPropagation() {} }));
global.el = (t, cls, text) => { const n = new Node(t); if (cls) cls.split(" ").forEach((c) => n.classList.add(c)); if (text != null) n.textContent = text; return n; };
global.$ = () => HOST;
global.copyText = () => {}; global.toast = () => {};
global.sdShot = (nid, jid, i) => Promise.resolve({ mime: jid.startsWith("v") ? "video/webm" : "image/png", url: `blob:${jid}/${i}`, name: `${jid}.png` });
global.sdDownload = () => {};
let store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
eval(SRC + "; Object.assign(global, { sdViewer, sdWallSlice, sdWallPageList, sdFormLoad, sdFormSave, sdModelLabel, SD_WALL_PAGE });");
const tick = () => new Promise((r) => setTimeout(r, 0));

(async () => {
  // ---- 2. 下拉標能力
  const lab = ["both", "only", "draw"].map((e) => sdModelLabel({ title: "M", kind: "image", edit: e }));
  assert("下拉：能畫也能改圖／只能改圖／只能畫圖", lab[0] === "M（能畫也能改圖）" && lab[1] === "M（只能改圖）" && lab[2] === "M（只能畫圖）", lab);
  assert("  影片模型照舊標影片、中國來源照舊標", sdModelLabel({ title: "W", kind: "video", cn_origin: true }) === "W（影片・中國來源）");

  // ---- 3. 表單記憶
  sdFormSave("n1", "image", { model: "flux", prompt: "貓", steps: "4", init_image: "data:..." });
  sdFormSave("n1", "video", { model: "ltx", prompt: "海" });
  sdFormSave("n2", "image", { model: "sd", prompt: "狗" });
  const a = sdFormLoad("n1", "image");
  assert("表單：每台、畫圖影片各記一份", a.prompt === "貓" && sdFormLoad("n1", "video").prompt === "海" && sdFormLoad("n2", "image").prompt === "狗");
  assert("  只讀回認得的欄位（參考圖不會被帶回來）", !("init_image" in a) && a.steps === "4", a);
  store["xconsole-sdform:n1:image"] = "{壞掉";
  assert("  存的內容壞了：當作沒有，用預設", sdFormLoad("n1", "image") === null);
  global.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("quota"); } };
  let threw = false;
  try { sdFormSave("n1", "image", { prompt: "x" }); assert("  瀏覽器擋localStorage：讀回null、寫入不丟錯", sdFormLoad("n1", "image") === null); } catch (_) { threw = true; }
  assert("  瀏覽器擋localStorage：頁面照常", !threw);

  // ---- 4. 作品牆分批
  const now = Date.UTC(2026, 10, 25);
  const jobs = [];
  for (let k = 0; k < 30; k++) jobs.push({ id: `i${k}`, kind: "image", state: "done", prompt: k % 3 ? "cat" : "dog", created: now / 1000 - k * 3600 * 20 });
  for (let k = 0; k < 5; k++) jobs.push({ id: `v${k}`, kind: "video", state: "done", prompt: "sea", created: now / 1000 });
  const p0 = sdWallSlice(jobs, { tab: "image", page: 0 }, now), p1 = sdWallSlice(jobs, { tab: "image", page: 1 }, now);
  assert("作品牆：一批固定件數", p0.items.length === SD_WALL_PAGE && p1.items.length === SD_WALL_PAGE && p0.pages === 3, [p0.items.length, p0.pages]);
  assert("  換批是整批換掉、不是接在後面", p1.items[0].id === `i${SD_WALL_PAGE}` && !p1.items.some((j) => p0.items.includes(j)));
  assert("  畫圖、影片分開", sdWallSlice(jobs, { tab: "video" }, now).hits.length === 5 && p0.hits.every((j) => j.kind === "image"));
  assert("  找提示詞（不分大小寫）", sdWallSlice(jobs, { tab: "image", q: "DOG" }, now).hits.length === 10);
  assert("  時間範圍：今天", sdWallSlice(jobs, { tab: "image", range: "1" }, now).hits.length === 2);
  assert("  頁碼超出範圍：停在最後一批", sdWallSlice(jobs, { tab: "image", page: 99 }, now).page === 2);
  assert("  批次按鈕太多時中間省略", JSON.stringify(sdWallPageList(25, 10)) === JSON.stringify([0, null, 9, 10, 11, null, 24]) && sdWallPageList(3, 0).length === 3);

  // ---- 5. 全螢幕檢視器
  const items = [{ job: { id: "i1", kind: "image", prompt: "p1" }, i: 0 }, { job: { id: "i2", kind: "image", prompt: "p2" }, i: 0 },
                 { job: { id: "v1", kind: "video", prompt: "p3" }, i: 0 }];
  const opener = new Node("button"); HOST.append(opener); opener.focus();
  sdViewer({ id: "n1" }, items, 0, {});
  await tick(); await tick();
  const ov = HOST.children.find((c) => c.classList.contains("sd-viewer"));
  const pos = () => ov.find((x) => x.classList?.contains("sd-viewer-pos")).textContent;
  const media = () => ov.find((x) => x.tagName === "IMG" || x.tagName === "VIDEO");
  assert("檢視器：開在點的那一張、顯示第幾張", ov && pos() === "1／3" && media().src === "blob:i1/0");
  assert("  開著時背後頁面不捲", document.documentElement.classList.contains("viewer-open"));
  key("ArrowRight"); await tick(); await tick();
  assert("  右鍵下一張", pos() === "2／3" && media().src === "blob:i2/0");
  key("ArrowRight"); await tick(); await tick();
  assert("  影片開大播放器（有控制列）", media().tagName === "VIDEO" && media().controls === true);
  key("ArrowRight"); await tick();
  assert("  最後一張再按右鍵不動", pos() === "3／3");
  key("ArrowLeft", { tagName: "VIDEO" }); await tick();
  assert("  焦點在影片上時左右鍵留給影片快轉", pos() === "3／3");
  key("ArrowLeft"); await tick(); await tick();
  assert("  左鍵上一張", pos() === "2／3");
  const one = ov.find((x) => x.textContent === "1:1原尺寸");
  one.click();
  assert("  1:1原尺寸切換", ov.find((x) => x.classList?.contains("sd-viewer-stage")).classList.contains("actual"));
  key("Escape");
  assert("  Esc關閉、拿掉鍵盤監聽、焦點回到原來的按鈕、頁面恢復捲動", !ov.parent && !(docL.keydown || []).length
    && document.activeElement === opener && !document.documentElement.classList.contains("viewer-open"));
  sdViewer({ id: "n1" }, items, 1, {});
  await tick();
  const ov2 = HOST.children.find((c) => c.classList.contains("sd-viewer"));
  (ov2.listeners.click || []).forEach((f) => f({ target: ov2 }));
  assert("  點背景關閉（檢視器沒有輸入欄位）", !ov2.parent && !(docL.keydown || []).length);
})();
"""
node = shutil.which("node")
if node:
    src_js = "\n".join([block("const SD_EDIT_LABEL", "function sdGen("),
                        block("const SD_WALL_PAGE", "function sdWall("),
                        block("function sdViewer(", "/* ---- 清單管理")])
    r = subprocess.run([node, "-e", "const SRC = " + json.dumps(src_js) + ";\nconst SD_KIND = { image: \"畫圖\", video: \"影片\" };\n" + JS],
                       capture_output=True, text=True, timeout=60)
    print(r.stdout.rstrip())
    if r.returncode or r.stderr.strip():
        check("畫面邏輯用node實跑", False, (r.stderr or "").strip()[-600:])
elif os.environ.get("CI"):
    check("CI上要有node才能實跑畫面邏輯", False)
else:
    print("[略過] 這台沒有node：畫面邏輯只做了原始碼檢查")

print("\n全部通過" if OK else "\n有項目失敗")
sys.exit(0 if OK else 1)
