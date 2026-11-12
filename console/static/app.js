/* xConsole 主控台前端。
   規範：console-ui-architecture（頂部雙列、每頁同一副骨架、含輸入欄位的彈窗禁止點背景關閉、
   Enter 要擋中文輸入法）＋ xcloud-web-style（品牌色、無色條、無 emoji、頁面不准很長）。 */

const S = {
  fleet: null,      // 最近一次彙整結果
  view: "home",     // 頂層見 NAV；另有 node:<id>、api:<node>:<slot>、apis 等子頁
  sub: null,        // 子頁
  busy: new Set(),  // 正在動作中的欄位，避免重複點
  timer: null,
  lastOk: 0,
};

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const fmt = (n, d = 1) => (n === null || n === undefined || Number.isNaN(n)) ? "—" : Number(n).toFixed(d);
const int = (n) => (n === null || n === undefined) ? "—" : Math.round(n).toLocaleString(uiLocale());

function toast(msg, kind) {
  const t = el("div", "toast" + (kind ? " " + kind : ""), msg);
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "bad" || kind === "warn" ? 9000 : 4500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
    signal: opts.signal,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* 沒有 JSON 就算了 */ }
  if (res.status === 401 && !opts.noRelogin) { showLogin(); throw new Error("請先登入"); }
  if (!res.ok) { const e = new Error(data.error || `伺服器回 ${res.status}`); e.data = data; e.status = res.status; throw e; }
  if (typeof i18nData === "function") i18nData(data);
  return data;
}

/* ---------------------------------------------------------------- 客戶品牌
   賣給客戶時換單位名稱、系統名稱與 Logo。登入頁也要換，所以走不用登入的 /api/branding。
   頁尾的 Powered by xCloudinfo 寫死在 index.html，不在這裡處理、也不開放關掉。 */

function letterIcon(ch) {
  const esc = String(ch).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" }[c]));
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#bf181f"/>` +
    `<text x="16" y="22" font-family="sans-serif" font-size="17" font-weight="bold" fill="white" text-anchor="middle">${esc}</text></svg>`;
  return "data:image/svg+xml," + encodeURIComponent(svg);
}

// 頁面原本寫的就是預設品牌。先記下來：從「有自訂」退回「沒自訂」時（例如只上傳過 Logo 又移除）要整組還原，
// 不能直接 return，否則 Logo 還掛著、標題還是客戶的
const BRAND_DEFAULT = {
  title: document.title,
  name: $("#brand-name").textContent,
  loginTitle: $("#login-title").textContent,
  loginOrg: $("#login-org").textContent,
  mark: $("#brand-mark").textContent,
  icon: document.querySelector("link[rel=icon]")?.getAttribute("href"),
};

function resetBranding() {
  document.title = BRAND_DEFAULT.title;
  $("#brand-name").textContent = BRAND_DEFAULT.name;
  $("#login-title").textContent = BRAND_DEFAULT.loginTitle;
  $("#login-org").textContent = BRAND_DEFAULT.loginOrg;
  $("#brand-mark").textContent = BRAND_DEFAULT.mark;
  $("#brand-mark").hidden = false;
  for (const id of ["#brand-logo", "#login-logo"]) {
    const img = $(id);
    img.hidden = true;
    img.removeAttribute("src");
    img.alt = "";
  }
  const icon = document.querySelector("link[rel=icon]");
  if (icon && BRAND_DEFAULT.icon) icon.setAttribute("href", BRAND_DEFAULT.icon);
}

function applyBranding(b) {
  document.documentElement.classList.remove("brand-pending");
  if (!b) return;
  S.brand = b;
  if (!b.custom) { resetBranding(); return; }
  const first = [...b.org][0] || "x";
  document.title = `${b.product}｜${b.org}`;
  $("#brand-name").textContent = b.product;
  $("#login-title").textContent = b.product;
  $("#login-org").textContent = b.org;
  $("#brand-mark").textContent = first;
  for (const id of ["#brand-logo", "#login-logo"]) {
    const img = $(id);
    if (b.logo) {
      if (img.getAttribute("src") !== b.logo.url) img.src = b.logo.url;   // 同一張不重設，避免重新載入閃一下
      img.alt = b.org;
    }
    img.hidden = !b.logo;
  }
  $("#brand-mark").hidden = !!b.logo;
  // 橫式 Logo 縮成分頁小圖示會擠成一團：用單位名稱第一個字產生
  const icon = document.querySelector("link[rel=icon]");
  if (icon) icon.href = letterIcon(first);
}

async function loadBranding() {
  try {
    const res = await fetch("/api/branding", { credentials: "same-origin" });
    applyBranding(res.ok ? await res.json() : null);
  } catch (_) {
    applyBranding(null);                  // 拿不到就維持預設，但一定要把品牌區顯示出來
  }
}

/* ---------------------------------------------------------------- 登入 */

function showLogin() {
  $("#login").hidden = false;
  $("#app").hidden = true;
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
}

/* 登入失敗一定要讓人一眼看出來：紅底整塊、錯誤碼、還能再試幾次。
   以前密碼錯會被 api() 當成「登入逾時」改寫成灰色小字「請先登入」，使用者以為什麼事都沒發生。 */
function loginErrorText(ex) {
  if (!ex.status) return "連不到主控台，請確認網路，或防火牆有沒有放行這台電腦（錯誤碼 NET）";
  if (ex.status === 401) {
    const left = ex.data?.remaining;
    if (left === 0) return "帳號或密碼不對（錯誤碼 401）。登入已暫停，請 15 分鐘後再試";
    return "帳號或密碼不對（錯誤碼 401）" + (left ? `。再錯 ${left} 次會暫停登入 15 分鐘` : "");
  }
  return `${ex.message}（錯誤碼 ${ex.status}）`;
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#login-err"), btn = $(".login-submit"), pw = $("#pw");
  err.hidden = true;
  pw.removeAttribute("aria-invalid");
  btn.disabled = true;
  btn.textContent = "登入中…";
  try {
    await api("/api/login", { method: "POST", noRelogin: true, body: { username: $("#un").value.trim(), password: pw.value } });
    pw.value = "";
    start();
  } catch (ex) {
    err.textContent = loginErrorText(ex);
    err.hidden = false;
    pw.setAttribute("aria-invalid", "true");
    pw.select();
  } finally {
    btn.disabled = false;
    btn.textContent = "登入";
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_) { /* 失敗也要回登入頁 */ }
  showLogin();
});

/* ---------------------------------------------------------------- 彈窗 */

function modal({ title, bodyNode, confirmText, danger, onConfirm, hasInput }) {
  const host = $("#modal-host");
  const overlay = el("div", "overlay");
  const box = el("div", "modal");
  box.appendChild(el("h3", null, title));
  if (bodyNode) box.appendChild(bodyNode);
  const actions = el("div", "actions");
  const cancel = el("button", "btn", "取消");
  cancel.onclick = () => overlay.remove();
  actions.appendChild(cancel);
  if (confirmText) {
    const ok = el("button", "btn " + (danger ? "danger" : "primary"), confirmText);
    ok.onclick = async () => { overlay.remove(); await onConfirm?.(); };
    actions.appendChild(ok);
  }
  box.appendChild(actions);
  overlay.appendChild(box);
  // 含輸入欄位的彈窗一律不掛背景點擊關閉（全域鐵則：填到一半點到背景整張表單會蒸發）
  if (!hasInput) overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  const esc = (e) => { if (e.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", esc); } };
  document.addEventListener("keydown", esc);
  host.appendChild(overlay);
  return overlay;
}

/* 輸入框的 Enter 要擋中文輸入法組字（注音選字的 Enter 不能當送出） */
function imeGuard(input, onEnter, opts = {}) {
  let composing = false;
  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend", () => { composing = false; });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (composing || e.isComposing || e.keyCode === 229) return;
    if (opts.shiftNewline && e.shiftKey) return;         // 對話框：Shift+Enter 換行
    e.preventDefault();
    onEnter();
  });
}

/* ---------------------------------------------------------------- 捲動漸入 */

function reveal(root) {
  const items = root.querySelectorAll(".rv:not(.in)");
  if (!items.length) return;
  // 自動更新的重畫不播進場動畫：每 15 秒整頁從透明、下移 26px 重新淡入一次，畫面就會一直閃。
  // 動畫只留給使用者自己切頁的那一次。
  if (S.quiet) { items.forEach((n) => n.classList.remove("rv", "d1", "d2", "d3", "d4")); return; }
  const showAll = () => items.forEach((n) => n.classList.add("in"));
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } });
    }, { rootMargin: "0px 0px -8% 0px" });
    items.forEach((n) => io.observe(n));
  }
  // 三秒後「無條件」全部放行。
  // 不可以寫成「IO 沒回呼才放行」—— IO 一掛上就會對所有目標回呼一次（含不在畫面內的），
  // 旗標馬上變 true，備援永遠不會執行，沒捲到的區塊就永遠停在 opacity:0。實際踩到過。
  setTimeout(showAll, 3000);
  const vh = window.innerHeight || document.documentElement.clientHeight || 800;
  const check = () => items.forEach((n) => {
    if (n.getBoundingClientRect().top < vh * 0.95) n.classList.add("in");
  });
  check();
  window.addEventListener("scroll", check, { passive: true, capture: true });
  window.addEventListener("resize", check, { passive: true });
}

/* ---------------------------------------------------------------- 導覽 */

function groups() {
  const g = new Map();
  (S.fleet?.nodes || []).forEach((n) => {
    if (!g.has(n.group)) g.set(n.group, []);
    g.get(n.group).push(n);
  });
  return g;
}

// 頂層照使用者的動線排（參考 LocModel，見 docs/WORKFLOW.md）：看這台 → 準備模型 → 啟動 → 用（對話、Agent、畫圖、影片）
// → 管理（最後）。機群管理（機器清單、加機器、記錄）收進設定；全站共用頂列右側的「目前這台機器」。
const NAV = [["home", "概覽"], ["models", "模型"], ["run", "執行"], ["chat", "對話"], ["agent", "Agent"],
  ["image", "畫圖"], ["video", "影片"], ["settings", "設定"]];

function renderNav() {
  const nav = $("#nav");
  const sub = $("#subnav");
  nav.textContent = "";
  sub.textContent = "";
  const items = NAV;
  const v = S.view;
  const active = topOf(v);
  items.forEach(([key, label]) => {
    const b = el("button", null, label);
    if (key === active) b.setAttribute("aria-current", "true");
    b.onclick = () => { closeMobileNav(); go(key); };
    nav.appendChild(b);
  });
  renderNodePick();
  const subBtn = (label, view, on, fn) => {
    const b = el("button", null, label);
    if (on) b.setAttribute("aria-current", "true");
    b.onclick = fn || (() => go(view));
    sub.appendChild(b);
  };
  const cur = curNode();
  // 第二列：概覽在多台時可切「這台／全部機器」
  if (active === "home" && (S.fleet.nodes || []).length > 1) {
    subBtn("這台", "home", !S.homeAll, () => { S.homeAll = false; go("home"); });
    subBtn("全部機器", "home", !!S.homeAll, () => { S.homeAll = true; go("home"); });
  }
  // 第二列：執行（日常）→ 角色欄位（一台跑好幾個模型時才需要）→ API 串接（給 IDE）
  if (active === "run" && cur) {
    subBtn("執行模型", "run", v === "run");
    subBtn("角色欄位", "node:" + cur.id, v.startsWith("node:"));
    subBtn("API 串接", "apis", v === "apis" || v.startsWith("api:"));
    if (v === "apis" || v.startsWith("api:")) {
      (cur.state?.slots || []).filter(isApiSlot).forEach((s) => subBtn(s.role, `api:${cur.id}:${s.id}`, v === `api:${cur.id}:${s.id}`));
    }
  }
  // 第二列：設定頁的分頁（依管理者實際的先後；記錄與帳號在後、品牌只在交機時設一次放最後）
  if (active === "settings") {
    const tabs = settingsTabs();
    const curTab = tabs.some(([k]) => k === S.settingsTab) ? S.settingsTab : tabs[0][0];
    tabs.forEach(([key, label]) => {
      subBtn(label, "settings", (v === "logs" ? "logs" : curTab) === key,
        () => { S.settingsTab = key; go("settings"); });
    });
  }
  // 手機版：漢堡選單裡放同一組導覽
  const mnav = $("#mnav-list");
  if (mnav) {
    mnav.textContent = "";
    items.forEach(([key, label]) => {
      const b = el("button", null, label);
      if (key === active) b.setAttribute("aria-current", "true");
      b.onclick = () => { closeMobileNav(); go(key); };
      mnav.appendChild(b);
    });
  }
}

function closeMobileNav() { const m = $("#mnav"); if (m) m.hidden = true; }

function go(view) {
  if (view === "overview") view = "home";
  if (view === "lib") view = "models";
  if (view === "logs") { S.settingsTab = "logs"; view = "settings"; }
  if (view === "assist") view = "chat";
  if (view.startsWith("lib:")) { setNode(view.slice(4)); view = "models"; }
  if (view.startsWith("node:")) { setNode(view.slice(5)); S.lastNode = view.slice(5); }
  if (view.startsWith("api:")) setNode(view.split(":")[1]);
  S.view = view;
  render();
  window.scrollTo({ top: 0, behavior: "instant" });
}

/* ---------------------------------------------------------------- 共用片段 */

function poolBar(pool, slots, kind) {
  /* 一條堆疊長條：各模型佔多少 ＋ 剩餘 ＋ 底線位置。 */
  const total = pool.total_gib || 1;
  const wrap = el("div", "pool");
  const head = el("div", "pool-head");
  head.appendChild(el("span", "name", kind));
  const right = el("span", "mono small");
  right.textContent = `已用 ${fmt(pool.used_gib)} / ${fmt(total)} GiB　剩 ${fmt(pool.free_gib)}`;
  head.appendChild(right);
  wrap.appendChild(head);

  const bar = el("div", "bar");
  const colors = ["#bf181f", "#0891b2", "#7c3aed", "#d97706", "#2e7d32", "#db2777"];
  let used = 0;
  const legend = [];
  // RSS 會把共用頁重複算，可能超過池子實際用量；歸帳總和夾在 pool.used_gib 以內，
  // 否則長條會畫出比「已用」還長的彩色段。
  const cap = pool.used_gib || 0;
  slots.forEach((s, i) => {
    let v = kind === "系統記憶體" ? s.ram_gib : s.vram_gib;
    if (!v || !s.busy) return;
    v = Math.min(v, Math.max(0, cap - used));
    if (v <= 0.05) return;
    used += v;
    const seg = el("div", "seg");
    seg.style.width = (v / total * 100) + "%";
    seg.style.background = colors[i % colors.length];
    seg.title = `${s.role}：${fmt(v)} GiB`;
    bar.appendChild(seg);
    legend.push([colors[i % colors.length], `${s.role} ${fmt(v)}`]);
  });
  const other = Math.max(0, (pool.used_gib || 0) - used);
  if (other > 0.2) {
    const seg = el("div", "seg");
    seg.style.width = (other / total * 100) + "%";
    seg.style.background = "rgba(130,145,158,.5)";
    seg.title = `其他（系統與未歸帳）：${fmt(other)} GiB`;
    bar.appendChild(seg);
    legend.push(["rgba(130,145,158,.5)", `其他 ${fmt(other)}`]);
  }
  if (pool.floor_gib) {
    const f = el("div", "floor");
    f.style.left = ((total - pool.floor_gib) / total * 100) + "%";
    f.title = `底線：再往右就低於 ${pool.floor_gib} GiB，不准再載`;
    bar.appendChild(f);
  }
  wrap.appendChild(bar);

  const lg = el("div", "bar-legend");
  legend.forEach(([c, t]) => {
    const s = el("span");
    const i = el("i");
    i.style.background = c;
    s.appendChild(i);
    s.appendChild(document.createTextNode(t));
    lg.appendChild(s);
  });
  if (pool.floor_gib) {
    const s = el("span", null);
    const i = el("i");
    i.style.background = "var(--bad)";
    s.appendChild(i);
    s.appendChild(document.createTextNode(`底線 ${pool.floor_gib} GiB`));
    lg.appendChild(s);
  }
  wrap.appendChild(lg);
  return wrap;
}

function kvRow(k, v) {
  const r = el("div", "kv");
  r.appendChild(el("span", "k", k));
  r.appendChild(el("span", "v", v));
  return r;
}

/* ---------------------------------------------------------------- 欄位卡 */

function slotCard(node, s, unified) {
  if (!s.unit) {
    const c = el("div", "slot empty");
    c.appendChild(el("div", "role", `${s.role}・${s.tier || ""}`));
    const b = el("button", "btn sm", "放入");
    // 空欄位直接從模型庫挑一顆 GGUF 放進來（自動配埠、啟動）
    b.onclick = () => openSwap(node, s);
    const g = el("button", "btn sm", "設定");
    g.onclick = () => openSlotEditor(node, s);
    const acts = el("div", "slot-actions");
    acts.append(b, g);
    c.appendChild(acts);
    return c;
  }

  const c = el("div", "slot rv");
  const top = el("div", "slot-top");
  const dot = el("span", "dot" + (s.state === "active" ? " on"
    : s.state === "activating" || s.state === "deactivating" ? " warn"
      : s.state === "failed" ? " bad" : ""));
  top.appendChild(dot);
  top.appendChild(el("span", "role", `${s.role}・${s.tier || ""}`));
  top.appendChild(el("span", "spacer"));
  top.appendChild(el("span", "tier", s.state_label));
  c.appendChild(top);

  c.appendChild(el("div", "model", s.model || s.unit));
  if (s.quant) c.appendChild(el("div", "quant", s.quant));

  const chips = el("div", "chips");
  (s.caps || []).forEach((cap) => chips.appendChild(el("span", "chip", cap)));
  if (s.port) chips.appendChild(el("span", "chip brand", ":" + s.port));
  if (s.gpu_ids?.length) chips.appendChild(el("span", "chip", "卡 " + s.gpu_ids.join("、")));
  if (s.critical) chips.appendChild(el("span", "chip brand", "關鍵服務"));
  c.appendChild(chips);

  if (s.config?.max_context) {
    c.appendChild(kvRow("上下文", int(s.config.max_context) + (s.config.parallel > 1 ? ` ×${s.config.parallel}` : "")));
  }
  if (s.busy) {
    // 統一記憶體機沒有獨立顯存，列一行「顯存 0.0」只會誤導
    if (!unified) c.appendChild(kvRow("顯示記憶體" + (s.vram_exact ? "" : "（整機）"), fmt(s.vram_gib) + " GiB"));
    c.appendChild(kvRow(unified ? "記憶體" : "系統記憶體", fmt(s.ram_gib) + " GiB"));
  }
  const m = s.measure;
  if (m) {
    c.appendChild(kvRow("匯入 tok/s", `${fmt(m.prefill_min, 0)}～${fmt(m.prefill_max, 0)}`));
    c.appendChild(kvRow("生成 tok/s", `${fmt(m.decode_min, 0)}～${fmt(m.decode_max, 0)}`));
    c.appendChild(kvRow("量過最長提示", int(m.longest_prompt)));
  }

  const acts = el("div", "slot-actions");
  const mk = (label, action, cls) => {
    const b = el("button", "btn sm" + (cls ? " " + cls : ""), label);
    b.disabled = S.busy.has(node.id + "/" + s.id);
    b.onclick = () => doAction(node, s, action);
    return b;
  };
  if (s.state === "active" || s.state === "activating") {
    acts.appendChild(mk("重啟", "restart"));
    acts.appendChild(mk("停止", "stop", "danger"));
  } else {
    acts.appendChild(mk("啟動", "start", "primary"));
  }
  if (s.kind === "strata" || s.kind === "llama") {
    const sw = el("button", "btn sm", "換模型");
    sw.onclick = () => openSwap(node, s);
    acts.appendChild(sw);
  }
  if (s.kind === "llama" && s.unit) {
    const rb = el("button", "btn sm", "執行設定");
    rb.title = "上下文長度、思考模式、區網存取；看預估速度與載入時間";
    rb.onclick = () => openRunSettings(node, s);
    acts.appendChild(rb);
  }
  if (isApiSlot(s)) {
    const ab = el("button", "btn sm", "API");
    ab.title = "網址、模型名稱、上下文、各工具設定範例與實測";
    ab.onclick = () => go(`api:${node.id}:${s.id}`);
    acts.appendChild(ab);
  }
  const lg = el("button", "btn sm", "記錄");
  lg.onclick = () => { S.sub = { node: node.id, slot: s.id }; go("logs"); };
  acts.appendChild(lg);
  const cfg = el("button", "btn sm", "設定");
  cfg.onclick = () => openSlotEditor(node, s);
  acts.appendChild(cfg);
  c.appendChild(acts);
  return c;
}

/* 換模型：Strata 家族與 GGUF（llama.cpp）一起列。跨引擎時同一個埠換人接手，IDE 網址不用改；
   失敗會整組換回原本那顆。空欄位也走這裡（「放入模型」）。 */
async function openSwap(node, s, pre) {
  const empty = !s.unit;
  const body = el("div");
  body.appendChild(el("p", "small muted", "讀取這台裝了哪些模型…"));
  const ov = modal({ title: `${s.role}・${s.tier || ""} ${empty ? "放入模型" : "換模型"}`, bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide");

  let cat;
  try {
    cat = await api(`/api/node/${node.id}/catalog/${s.id}`);
  } catch (ex) {
    body.textContent = "";
    body.appendChild(el("p", "note bad", "讀不到模型清單：" + ex.message));
    return;
  }
  body.textContent = "";

  let chosen = null;
  const ctxRow = el("div", "form-row");
  ctxRow.hidden = true;
  const ctxIn = el("input");
  ctxIn.type = "number";
  const ctxHint = el("div", "assume");
  const ctxWrap = el("div");
  ctxWrap.appendChild(el("label", null, "上下文長度（token）"));
  ctxWrap.appendChild(ctxIn);
  ctxWrap.appendChild(ctxHint);
  ctxRow.appendChild(ctxWrap);

  /* 多卡（獨立顯存、兩張以上）才出現：這顆模型用哪幾張卡。預設帶建議——單張放得下就綁最空的那張，
     少了跨卡傳輸，其他卡還能跑別的模型；放不下才用最少張數一起扛。沒勾任何一張＝全部卡都用。 */
  const cards = node.state?.pools?.gpus || [];
  const multi = node.state?.pools?.memory_model === "discrete" && cards.length > 1 && node.state.pools.bindable !== false;
  const gpuRow = el("div");
  gpuRow.hidden = true;
  const gpuHint = el("div", "assume");
  const boxes = [];
  const selfIds = (s.gpu_ids || []).map(String);
  const selCards = cards.filter((c) => !selfIds.length || selfIds.includes(String(c.id)));
  const selTot = selCards.reduce((a, c) => a + c.total_gib, 0) || 1;
  const mine = s.busy ? (s.vram_gib || 0) : 0;
  // 換的時候這個欄位會先釋放：它現在佔的量攤回它現在用的卡
  const avail = (c) => c.free_gib + (selCards.includes(c) ? mine * c.total_gib / selTot : 0);
  const vfloor = node.state?.pools?.floors?.vram || 0;
  let curNeed = 0;
  let touched = selfIds.length > 0;            // 已經綁過卡的欄位維持原本的選擇，不自動改
  const recommend = (need) => {
    const by = [...cards].sort((a, b) => avail(b) - avail(a));
    for (let k = 1; k <= by.length; k++) {
      const pick = by.slice(0, k);
      const tot = pick.reduce((a, c) => a + c.total_gib, 0);
      // 跟代理的准入同一條公式：需求與底線都照卡的大小分到每張卡（不一致的話，畫面說放不下、按下去卻放行）
      if (pick.every((c) => avail(c) - need * c.total_gib / tot >= vfloor * c.total_gib / tot)) return pick.map((c) => String(c.id));
    }
    return null;
  };
  const updGpu = () => {
    if (!multi) return;
    const rec = recommend(curNeed);
    if (!touched) boxes.forEach((b) => { b.checked = rec ? rec.includes(b.value) : true; });
    const picked = boxes.filter((b) => b.checked).map((b) => b.value);
    gpuHint.textContent = (!rec ? `每種組合都放不下約 ${fmt(curNeed)} GiB，套用時會被准入擋下。`
      : rec.length === 1 ? `建議只用卡 ${rec[0]}：單張放得下，少了跨卡傳輸，其他卡可以跑別的模型。`
        : `建議用卡 ${rec.join("、")} 一起扛：要約 ${fmt(curNeed)} GiB，${rec.length - 1} 張放不下。`)
      + (picked.length ? "" : "　沒勾任何一張＝全部卡都用。");
  };
  if (multi) {
    gpuRow.appendChild(el("label", "field", "用哪幾張卡"));
    const g = el("div", "gpu-pick");
    cards.forEach((c) => {
      const l = el("label", "gpu-opt");
      const cb = el("input");
      cb.type = "checkbox";
      cb.value = String(c.id);
      cb.checked = selfIds.includes(cb.value);
      cb.onchange = () => { touched = true; updGpu(); };
      l.appendChild(cb);
      l.appendChild(document.createTextNode(` 卡 ${c.id}　可用 ${fmt(avail(c))} / ${fmt(c.total_gib)} GiB`));
      g.appendChild(l);
      boxes.push(cb);
    });
    gpuRow.appendChild(g);
    gpuRow.appendChild(gpuHint);
  }

  const row = (m, engine) => {
    const r = el("label", "pick");
    const radio = el("input");
    radio.type = "radio";
    radio.name = "model";
    radio.dataset.id = m.id || "";
    // llama.cpp 的現役模型可以再選一次：用來改上下文或改綁卡
    const blocked = (m.current && engine !== "llama") || (engine === "strata" ? !m.has_tokenizer || !m.ready : false);
    radio.disabled = blocked;
    radio.onchange = () => {
      chosen = { ...m, engine };
      okBtn.disabled = false;
      ctxRow.hidden = engine !== "llama";
      gpuRow.hidden = !(multi && engine === "llama");
      if (engine === "llama") {
        ctxIn.value = m.ctx_default;
        const par = m.parallel || 1;   // 滑動視窗每條序列各留一個視窗，所以兩部分都乘並行數
        const kvAt = (c) => ((m.kv_full_bpt || 0) * c + (m.kv_swa_bpt || 0) * Math.min(c, m.swa_window || c)) * par / 1073741824;
        const upd = () => {
          const c = Number(ctxIn.value) || m.ctx_default;
          curNeed = (m.est_vram_gib || 0) + kvAt(c);
          updGpu();
          ctxHint.textContent = `這顆最多 ${int(m.ctx_train)}；上下文 ${int(c)} 時 KV 約 ${fmt(kvAt(c))} GiB，`
            + `加權重共約 ${fmt(m.est_vram_gib + kvAt(c))} GiB。套用前還會再做一次記憶體試算。`;
        };
        ctxIn.oninput = upd;
        upd();
      }
    };
    r.appendChild(radio);
    const t = el("div", "pick-main");
    t.appendChild(el("div", "model", `${m.name}${m.quant ? " " + m.quant : ""}`));
    const meta = engine === "llama"
      ? `${m.size_gib} GiB${m.shards > 1 ? `・${m.shards} 個分片` : ""}・${m.arch}・最長 ${int(m.ctx_train)}・預估約 ${fmt(m.est_vram_gib + m.est_kv_gib)} GiB`
      : `${m.size_gib} GiB・${m.shards} 個分片`;
    t.appendChild(el("div", "small muted", meta + (m.current ? (engine === "llama" ? "・目前使用中（可改上下文或用哪幾張卡）" : "・目前使用中") : "")));
    r.appendChild(t);
    const chips = el("div", "chips");
    if (engine === "llama" && m.vision) chips.appendChild(el("span", "chip", "看圖"));
    if (m.cn_origin) chips.appendChild(el("span", "chip warn", "中國來源・政府案不可用"));
    if (engine === "llama" && m.fits_total === false) chips.appendChild(el("span", "chip bad", "這台放不下"));
    if (engine === "llama" && m.auto_cpu_moe) chips.appendChild(el("span", "chip warn", `自動分層：${m.auto_cpu_moe} 層專家放系統記憶體`));
    if (engine === "strata" && !m.has_tokenizer) chips.appendChild(el("span", "chip", "缺 tokenizer"));
    r.appendChild(chips);
    return r;
  };

  const sec = (title, why, items, engine) => {
    body.appendChild(el("h4", "pick-h", title));
    body.appendChild(el("p", "small muted", why));
    const list = el("div", "pick-list");
    items.forEach((m) => list.appendChild(row(m, engine)));
    if (!items.length) list.appendChild(el("p", "small muted", "沒有。"));
    body.appendChild(list);
  };

  if (!empty && cat.models?.length) {
    sec("Strata 引擎（Qwen3.8-Flash-Next 家族）", "這個引擎只跑 Qwen3.8-Flash-Next 的各個版本，長上下文最省記憶體。", cat.models, "strata");
  }
  if (cat.can_llama) {
    sec("llama.cpp 引擎（GGUF）", `其他模型都走這裡（Gemma、gpt-oss、Llama…）。存放區：${cat.gguf_root}`, cat.gguf || [], "llama");
    if (!cat.llama_server) body.appendChild(el("p", "note bad", "這台找不到 llama-server，GGUF 模型還不能啟動。請先編好 llama.cpp，或在登錄檔 machine.llama_server 指定路徑。"));
  }
  [...(cat.orphans || []), ...(cat.gguf_problems || [])].forEach((o) => body.appendChild(el("p", "note warn", `${o.name}：${o.missing}`)));
  body.appendChild(ctxRow);
  body.appendChild(gpuRow);

  const lib = el("button", "btn sm", "到模型庫下載或管理模型");
  lib.onclick = () => { ov.remove(); go(`lib:${node.id}`); };
  const libRow = el("div", "slot-actions");
  libRow.appendChild(lib);
  body.appendChild(libRow);
  body.appendChild(el("p", "small muted",
    (empty ? "會替這個欄位配一個沒人用的埠並啟動。" : "套用時會先備份設定、做記憶體試算，再重啟；換到另一個引擎時，舊引擎會停掉並取消開機自啟，新引擎接手同一個埠，IDE 的網址不用改。")
    + "模型要重新載入，大約一到數分鐘；載入失敗會自動換回原本的設定。"));

  const actions = ov.querySelector(".actions");
  const okBtn = el("button", "btn primary", empty ? "放入並啟動" : "套用並重啟");
  okBtn.disabled = true;
  okBtn.onclick = async () => {
    const m = chosen;
    const req = m.engine === "llama"
      ? { engine: "llama", model: m.id, ctx: Number(ctxIn.value) || m.ctx_default,
        ...(multi ? { gpus: (() => { const g = boxes.filter((b) => b.checked).map((b) => b.value); return g.length && g.length < cards.length ? g : null; })() } : {}) }
      : { pack: m.pack, native: m.native };
    const go2 = async (force) => {
      ov.remove();
      toast(`${empty ? "放入" : "換"} ${m.name} 中，模型要重新載入…`);
      S.busy.add(node.id + "/" + s.id);
      try {
        const r = await api(`/api/node/${node.id}/slot/${s.id}/model`, { method: "POST", body: { ...req, force } });
        toast(`已換成 ${r.model}${r.switched_engine ? `（引擎改為 ${r.engine === "llama" ? "llama.cpp" : "Strata"}）` : ""}`
          + (multi && r.engine === "llama" ? (r.gpus ? `，用卡 ${r.gpus.join("、")}` : "，全部卡一起用") : ""), "ok");
        S.apiNotice = S.apiNotice || {};
        S.apiNotice[node.id + "/" + s.id] = `已換成 ${r.model}。IDE 裡的模型名稱請改成 ${r.api_model_name || "下面的模型名稱"}，`
          + "上下文與輸出上限照下面填，再按「實測一次」。";
        S.busy.delete(node.id + "/" + s.id);
        await refresh();
        go(`api:${node.id}:${s.id}`);
        return;
      } catch (ex) {
        const v = ex.data?.admit;
        if (v && !force) {
          const d = el("div");
          v.reasons.forEach((x) => d.appendChild(el("p", "note bad", x)));
          if (v.kv_note) d.appendChild(el("p", "note", v.kv_note));
          d.appendChild(el("p", "small muted", "統一記憶體的機器一旦吃爆，不是服務掛掉而是整台卡死，要到現場重開。"));
          modal({ title: `擋下來了：${m.name} 放不下`, bodyNode: d, confirmText: "我知道風險，仍要套用", danger: true, onConfirm: () => go2(true) });
        } else toast((empty ? "放入失敗：" : "換模型失敗：") + ex.message, "bad");
      }
      S.busy.delete(node.id + "/" + s.id);
      await refresh();
    };
    if (s.critical && !empty) {
      const d = el("div");
      d.appendChild(el("p", "note warn", s.note || "這是標記為關鍵的服務。"));
      d.appendChild(el("p", "small muted", `換成 ${m.name} 期間這個欄位會中斷。確定要換？`));
      modal({ title: "更換關鍵服務的模型", bodyNode: d, confirmText: "確定更換", danger: true, onConfirm: () => go2(false) });
    } else go2(false);
  };
  actions.appendChild(okBtn);
  // 從模型庫按「換上」進來的：直接選好那顆（okBtn 要先建好，change 會用到它）
  const preRadio = pre && [...body.querySelectorAll("input[name=model]")].find((r) => r.dataset.id === pre && !r.disabled);
  if (preRadio) {
    preRadio.checked = true;
    preRadio.dispatchEvent(new Event("change"));
    setTimeout(() => preRadio.closest("label").scrollIntoView({ block: "nearest" }), 0);
  }
}

/* ---------------------------------------------------------------- 模型
   目前這台機器的模型（參考 LocModel 的模型頁）：推薦模型｜已安裝｜畫圖與影片模型｜搜尋 Hugging Face｜存放區，
   右上「匯入本機 GGUF」。每顆都標等級、預估速度與三色分層條；裝好的按「執行」帶到執行頁。 */
async function viewLib() {
  const v = S.view;
  const only = curNode()?.id || null;
  if (!only) { go("home"); return; }
  const scope = S.fleet.nodes.filter((n) => n.id === only);
  const main = $("#main");
  main.textContent = "";
  const wrap = el("div", "wrap");
  main.appendChild(wrap);
  const head = el("div", "page-head");
  head.appendChild(el("h1", null, "模型"));
  head.appendChild(el("span", "small muted", scope[0].name));
  head.appendChild(el("span", "spacer"));
  const imp = el("button", "btn sm", "匯入本機 GGUF");
  head.appendChild(imp);
  wrap.appendChild(head);
  loading(head.appendChild(el("div")));

  const libs = await Promise.all(scope.filter((n) => n.online).map((n) =>
    api(`/api/node/${n.id}/library`).then((lib) => ({ n, lib }), (ex) => ({ n, err: ex.message }))));
  if (S.view !== v) return;
  head.lastChild.remove();
  const ok = libs.filter((x) => x.lib);
  imp.onclick = () => {
    if (!ok.length) return;
    const d = el("div", "stack");
    d.append(libUploadCard(ok, only), libManualCard(ok));
    const ov = modal({ title: "匯入本機 GGUF", bodyNode: d, hasInput: true });
    ov.querySelector(".modal").classList.add("wide");
  };
  const down = [...scope.filter((n) => !n.online).map((n) => `${n.name}：${n.error || "連不上代理"}`),
    ...libs.filter((x) => x.err).map((x) => `${x.n.name}：${x.err}`)];
  down.forEach((t) => head.appendChild(el("p", "note bad", t)));
  if (!ok.length) return;

  const models = ok.flatMap(({ n, lib }) => [...(lib.strata || []), ...(lib.gguf || [])].map((m) => ({ ...m, node: n })));
  const body = el("div", "stack");
  const jobs = ok.map(({ n, lib }) => ({ n, jobs: lib.downloads || [] })).filter((x) => x.jobs.length);
  if (jobs.some((x) => x.jobs.some(jobActive))) {
    const jc = libJobsCard(jobs);
    wrap.appendChild(jc);
    watchLibJobs(v, jc, jobs);
  }
  ok.forEach(({ n, lib }) => { const f = foundDirsNote(n, lib); if (f) wrap.appendChild(f); });
  const tabs = el("div", "tabbar lib-tabs");
  wrap.append(tabs, body);

  // 分頁只重畫下面那一塊，不重新向各機要資料
  function paint(tab) {
    S.libTab = tab;
    tabs.textContent = "";
    [["rec", "推薦模型"], ["installed", `已安裝（${models.length}）`], ["sd", "畫圖與影片模型"], ["hf", "搜尋 Hugging Face"], ["store", "存放區"]].forEach(([k, label]) => {
      const b = el("button", null, label);
      if (k === tab) b.setAttribute("aria-current", "true");
      b.onclick = () => paint(k);
      tabs.appendChild(b);
    });
    body.textContent = "";
    if (tab === "rec") libRec(body, ok, only, v);
    else if (tab === "sd") libSd(body, ok, only, v);
    else if (tab === "installed") body.appendChild(libInstalledCard(ok, models, jobs));
    else if (tab === "hf") {
      body.appendChild(libDownloadCard(ok, only));
      // 畫圖／影片模型是好幾個檔組成的，不在這裡一個一個下載；不在推薦清單上的（含中國來源）只限內部測試手動組
      const sdNote = el("p", "small muted");
      const sdBtn = el("button", "linkbtn", "手動組一顆畫圖／影片模型（內部測試）");
      sdBtn.onclick = () => openSdManual((ok.find((x) => x.n.id === only) || ok[0]).n);
      sdNote.append("已經有 GGUF 檔案就按右上「匯入本機 GGUF」。畫圖與影片模型請到「畫圖與影片模型」分頁一鍵下載（主體、文字編碼器、VAE 一起）。", sdBtn);
      body.appendChild(sdNote);
    } else body.appendChild(libStoreCard(ok));
    reveal(body);
  }
  paint(["rec", "installed", "sd", "hf", "store"].includes(S.libTab) ? S.libTab : "rec");
  reveal(main);
}

async function libRec(body, ok, only, v) {
  const pickN = only ? ok[0] : ok.find((x) => x.n.id === S.recNode) || ok[0];
  if (ok.length > 1 && !only) {
    const sel = el("div", "tabbar rec-for");
    sel.appendChild(el("span", "small muted", "推薦給："));
    ok.forEach(({ n }) => {
      const b = el("button", null, n.name);
      if (n.id === pickN.n.id) b.setAttribute("aria-current", "true");
      b.onclick = () => { S.recNode = n.id; body.textContent = ""; libRec(body, ok, only, v); };
      sel.appendChild(b);
    });
    body.appendChild(sel);
  }
  const holder = el("div", "stack");
  body.appendChild(holder);
  loading(holder);
  let rec;
  try { rec = await getRec(pickN.n.id); } catch (ex) { errNote(holder, ex.message); return; }
  if (S.view !== v || !holder.isConnected) return;
  holder.textContent = "";
  const n = S.fleet.nodes.find((x) => x.id === pickN.n.id) || pickN.n;
  holder.append(recList(n, rec, pickN.lib, { after: viewLib }));     // 照 LocModel：硬體卡在概覽，模型頁直接是清單
  reveal(holder);
}

// 開啟位置：網頁版顯示完整路徑可複製；那台有桌面時（單機版、有人登入）可以直接用檔案總管打開
function openModelFolder(n, m) {
  const sep = m.path.includes("\\") ? "\\" : "/";
  const dir = m.path.slice(0, m.path.lastIndexOf(sep)) || m.path;
  const d = el("div");
  const pre = el("pre", "code-pre mono small");
  pre.textContent = dir;
  d.appendChild(pre);
  const row = el("div", "slot-actions");
  const cp = el("button", "btn sm", "複製路徑");
  cp.onclick = () => copyText(dir);
  const op = el("button", "btn sm primary", `在 ${n.name} 上打開資料夾`);
  const note = el("p", "small muted", "「打開資料夾」會在那台的桌面跳出檔案總管；那台沒有人登入桌面時打不開，就用上面的路徑。");
  op.onclick = async () => {
    try { await api(`/api/node/${n.id}/desktop/open`, { method: "POST", body: { path: dir } }); toast("已在那台打開", "ok"); }
    catch (ex) { note.textContent = "打不開：" + ex.message; note.className = "small warn-t"; }
  };
  row.append(cp, el("span", "spacer"), op);
  d.append(row, note);
  modal({ title: `${m.name}${m.quant ? " " + m.quant : ""} 的位置`, bodyNode: d });
}

function libInstalledCard(ok, models, jobs) {
  const ic = card("已安裝的模型", `${models.length} 顆`);
  ic.id = "lib-installed";
  const sb = el("div", "scrollbox");
  const tb = el("table", "api-table");
  const cols = ["模型", ...(ok.length > 1 ? ["機器"] : []), "大小", "這台跑起來", "架構／最長上下文", "使用中", ""];
  tb.innerHTML = "<thead><tr>" + cols.map((h) => `<th${h === "大小" ? ' class="num"' : ""}>${h}</th>`).join("") + "</tr></thead>";
  const tbody = el("tbody");
  models.forEach((m) => {
    const n = m.node;
    const tr = el("tr");
    const nm = el("td");
    nm.appendChild(el("div", null, `${m.name}${m.quant ? " " + m.quant : ""}`));
    nm.appendChild(el("div", "small muted", [m.engine === "llama" ? "llama.cpp" : "Strata", m.shards > 1 ? `${m.shards} 個分片` : null,
      m.imported ? "匯入" : null, m.external ? `其他資料夾 ${m.external}` : null,
      m.cn_origin ? "中國來源・政府案不可用" : null].filter(Boolean).join("・")));
    tr.appendChild(nm);
    if (ok.length > 1) tr.appendChild(el("td", null, n.name));
    tr.appendChild(el("td", "num", `${fmt(m.size_gib)} GiB`));
    const pc = el("td");
    pc.appendChild(planCell(m.plan));
    tr.appendChild(pc);
    tr.appendChild(el("td", "small", m.engine === "llama" ? `${m.arch}／${int(m.ctx_train)}${m.vision ? "・看圖" : ""}${m.moe ? "・MoE" : ""}` : "Qwen3.8-Flash-Next"));
    const slot = (n.state?.slots || []).find((x) => x.id === m.in_use);
    tr.appendChild(el("td", "small", m.in_use ? (slot ? `${slot.role}${slot.tier ? "・" + slot.tier : ""}${slot.busy ? "・執行中" : ""}` : m.in_use) : "—"));
    const td = el("td", "row-acts");
    const sw = el("button", "btn sm primary", m.in_use ? "執行設定" : "執行");
    sw.onclick = () => runModel(n, m);
    td.appendChild(sw);
    if (m.path) {
      const loc = el("button", "btn sm", "開啟位置");
      loc.onclick = () => openModelFolder(n, m);
      td.appendChild(loc);
    }
    if (m.engine === "llama" && !m.external) {
      const b = el("button", "btn sm danger", "刪除");
      b.disabled = !!m.in_use;
      b.title = m.in_use ? "有欄位正在用，先換成別的模型" : "";
      b.onclick = () => {
        const d = el("div");
        d.appendChild(el("p", "note bad", `會從 ${n.name} 的磁碟永久刪除 ${m.id.startsWith("./") ? "存放區根目錄" : m.folder + "/"} 裡這顆模型的檔案（約 ${fmt(m.size_gib)} GiB），無法復原。`));
        modal({ title: `刪除 ${m.name}${m.quant ? " " + m.quant : ""}`, bodyNode: d, confirmText: "永久刪除", danger: true, onConfirm: async () => {
          try { const r = await api(`/api/node/${n.id}/library/delete`, { method: "POST", body: { id: m.id } }); toast(`已刪除，釋出 ${r.freed_gib} GiB`, "ok"); } catch (ex) { toast(ex.message, "bad"); }
          if (S.rec) delete S.rec[n.id];
          viewLib();
        } });
      };
      td.appendChild(b);
    }
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
  if (!models.length) {
    const tr = el("tr");
    const td = el("td", null, "還沒有模型。到「推薦模型」挑一顆下載、到「搜尋 Hugging Face」找，或按右上「匯入本機 GGUF」。");
    td.colSpan = cols.length;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  tb.appendChild(tbody);
  sb.appendChild(tb);
  ic.appendChild(sb);
  ok.forEach(({ n, lib }) => (lib.problems || []).forEach((x) => ic.appendChild(el("p", "note warn", `${n.name}　${x.name}：${x.missing}`))));
  // 已結束的下載紀錄（失敗的原因）放在這裡看；進行中的在頁首另有一張即時更新的卡
  if (jobs.length && !jobs.some((x) => x.jobs.some(jobActive))) ic.appendChild(libJobsCard(jobs));
  return ic;
}

function libStoreCard(ok) {
  const sc = card("存放區", ok.length > 1 ? `${ok.length} 台` : "");
  sc.id = "lib-store";
  const stb = el("table", "api-table");
  stb.innerHTML = "<thead><tr><th>機器</th><th>存放區</th><th class='num'>剩餘空間</th><th>硬碟</th><th>llama.cpp</th><th></th></tr></thead>";
  const sbody = el("tbody");
  ok.forEach(({ n, lib }) => {
    const tr = el("tr");
    tr.appendChild(el("td", null, n.name));
    tr.appendChild(el("td", "mono", lib.root));
    tr.appendChild(el("td", "num", `${fmt(lib.free_gib, 0)} / ${fmt(lib.total_gib, 0)} GiB`));
    tr.appendChild(el("td", "small" + (lib.hdd ? " bad-t" : ""), lib.hdd === true ? "機械硬碟（載入慢）" : lib.hdd === false ? "固態硬碟" : "—"));
    const tdl = el("td", lib.llama_server ? "small" : "small bad-t", lib.llama_server ? "找得到" : "找不到 llama-server，GGUF 還不能啟動");
    if (!lib.llama_server) {
      const b = el("button", "btn sm primary", "一鍵安裝");
      b.onclick = () => openEngineBuild(n.id);
      tdl.appendChild(b);
    }
    tr.appendChild(tdl);
    const set = el("td", "row-acts");
    const sb = el("button", "btn sm", "更改");
    sb.onclick = () => openMachineSettings(n.id);
    set.appendChild(sb);
    tr.appendChild(set);
    sbody.appendChild(tr);
  });
  stb.appendChild(sbody);
  const ssb = el("div", "scrollbox");
  ssb.appendChild(stb);
  sc.appendChild(ssb);
  sc.appendChild(el("p", "small muted", "Strata 引擎只跑 Qwen3.8-Flash-Next 家族，由 Strata 的 setup.sh 安裝；其他模型都是 GGUF，走 llama.cpp。"));
  ok.forEach(({ n, lib }) => sc.appendChild(modelDirsBox(n, lib, ok.length > 1)));
  return sc;
}

// 其他模型資料夾（LM Studio、自己放的 C:\models）：只讀，加進來就列在「已安裝」，檔案不搬、不複製、不刪
async function modelDirAct(n, body, okText) {
  try { await api(`/api/node/${n.id}/library/dirs`, { method: "POST", body }); toast(okText, "ok"); }
  catch (ex) { toast(ex.message, "bad"); return; }
  if (S.rec) delete S.rec[n.id];
  viewLib();
}

function modelDirsBox(n, lib, many) {
  const box = el("div", "stack");
  const h = el("div", "card-head");
  h.appendChild(el("h3", null, many ? `${n.name} 的其他模型資料夾` : "其他模型資料夾"));
  h.appendChild(el("span", "spacer"));
  const add = el("button", "btn sm", "加入資料夾");
  add.onclick = () => openFolderBrowser(n, () => { if (S.rec) delete S.rec[n.id]; viewLib(); }, { kind: "models" });
  h.appendChild(add);
  box.appendChild(h);
  box.appendChild(el("p", "small muted", "別的程式（LM Studio、LocModel）下載的、或自己放的 GGUF 所在的資料夾。xConsole 只讀這些資料夾，不會搬動、複製或刪除裡面的檔案；往下最多找三層。"));
  const dirs = lib.model_dirs || [];
  if (!dirs.length) box.appendChild(el("p", "small muted", "還沒有加入其他資料夾。"));
  dirs.forEach((x) => {
    const r = el("div", "hw-row");
    r.appendChild(el("span", "mono small", x.path));
    r.appendChild(el("span", "small " + (x.exists ? "muted" : "bad-t"), x.exists ? `${x.count} 顆模型` : "資料夾不見了（拔掉的外接碟？）"));
    r.appendChild(el("span", "spacer"));
    const rm = el("button", "btn sm", "移出清單");
    rm.title = "只是不再列出，檔案不會被刪";
    rm.onclick = () => modelDirAct(n, { remove: x.path }, "已移出清單，檔案沒有動");
    r.appendChild(rm);
    box.appendChild(r);
  });
  (lib.dir_suggestions || []).forEach((x) => {
    const r = el("div", "hw-row");
    r.appendChild(el("span", "mono small", x.path));
    r.appendChild(el("span", "small muted", `找到 ${x.count} 個模型檔，共 ${fmt(x.size_gib, 0)} GiB`));
    r.appendChild(el("span", "spacer"));
    const b = el("button", "btn sm primary", "加入");
    b.onclick = () => modelDirAct(n, { add: x.path }, `已加入 ${x.path}`);
    r.appendChild(b);
    box.appendChild(r);
  });
  return box;
}

// 只給「不用再提醒」這種個人偏好用；無痕模式或封鎖儲存時讀寫會丟錯，當成沒存
function lsGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* 存不了就下次再提醒 */ } }

// 模型庫頁首：這台已經有別的程式下載的模型 → 一鍵加進來（不要叫人重新下載一份）
function foundDirsNote(n, lib) {
  const found = (lib.dir_suggestions || []).filter((x) => !lsGet(`xc-dirhide:${n.id}:${x.path}`));
  if (!found.length) return null;
  const w = el("div", "note warn");
  const total = found.reduce((a, x) => a + x.count, 0);
  w.appendChild(el("div", null, `${n.name} 上已經有 ${total} 個模型檔：${found.map((x) => `${x.path}（${x.count} 個，${fmt(x.size_gib, 0)} GiB）`).join("、")}。`
    + "加進模型庫就能直接執行，檔案不搬、不複製，也不會被刪。"));
  const row = el("div", "slot-actions");
  const b = el("button", "btn sm primary", found.length > 1 ? "全部加入" : "加入模型庫");
  b.onclick = async () => {
    b.disabled = true;
    for (const x of found) {
      try { await api(`/api/node/${n.id}/library/dirs`, { method: "POST", body: { add: x.path } }); }
      catch (ex) { toast(`${x.path}：${ex.message}`, "bad"); }
    }
    toast("已加入，到「已安裝」看", "ok");
    if (S.rec) delete S.rec[n.id];
    S.libTab = "installed";
    viewLib();
  };
  const no = el("button", "btn sm", "不用");
  no.onclick = () => { found.forEach((x) => lsSet(`xc-dirhide:${n.id}:${x.path}`, "1")); w.remove(); };
  row.append(b, no, el("span", "small muted", "之後也可以在「存放區」分頁加入或移出"));
  w.appendChild(row);
  return w;
}

function libDownloadCard(ok, only) {
  const dc = card("從 Hugging Face 下載", "每台一次下載一個；中斷了再按一次會接續");
  dc.id = "lib-dl";
  const form = el("div", "form-row");
  const fNode = el("select");
  ok.forEach(({ n, lib }) => {
    const o = el("option", null, `${n.name}（剩 ${fmt(lib.free_gib, 0)} GiB）`);
    o.value = n.id;
    fNode.appendChild(o);
  });
  if (only) fNode.value = only;
  const fRepo = el("input");
  fRepo.type = "text";
  fRepo.placeholder = "例：unsloth/gemma-4-E4B-it-GGUF";
  const fTok = el("input");
  fTok.type = "password";
  fTok.autocomplete = "off";
  fTok.placeholder = "需要授權的模型才填";
  if (ok.length > 1) {
    const w0 = el("div"); w0.appendChild(el("label", null, "下載到哪台")); w0.appendChild(fNode);
    form.appendChild(w0);
  }
  const w1 = el("div"); w1.appendChild(el("label", null, "模型名稱（作者/模型）")); w1.appendChild(fRepo);
  const w2 = el("div"); w2.appendChild(el("label", null, "Hugging Face 令牌（選填）")); w2.appendChild(fTok);
  w2.appendChild(el("div", "assume", "這裡填的只用這一次；要一直用就按「存起來」（加密保存在主控台，畫面看不到內容）"));
  w2.appendChild(hfTokenRow(fTok));
  form.appendChild(w1);
  form.appendChild(w2);
  // 不知道完整名稱時先用關鍵字找（照下載數排），點一下就帶進上面的名稱並列出量化
  const sRow = el("div", "search-row");
  const fQ = textInput("", "關鍵字（例：gemma 4、gpt-oss），或直接貼 Hugging Face 網址");
  const sBtn = el("button", "btn sm", "搜尋");
  sRow.append(fQ, sBtn);
  const sOut = el("div", "hf-results");
  sOut.hidden = true;
  dc.append(sRow, sOut, form);
  const search = async () => {
    const q = fQ.value.trim();
    if (!q) return;
    // 貼的是網址或「作者/模型」：直接列出這個 repo 的量化，不用先搜尋
    const direct = hfRepoOf(q);
    if (direct) { fRepo.value = direct; sOut.hidden = true; listBtn.click(); return; }
    sOut.hidden = false;
    loading(sOut);
    let r;
    try { r = await api(`/api/node/${fNode.value}/library/hf/search`, { method: "POST", body: { q, token: fTok.value } }); } catch (ex) { errNote(sOut, ex.message); return; }
    sOut.textContent = "";
    if (!r.results.length) { sOut.appendChild(el("p", "small muted", "找不到含這個關鍵字的 GGUF。")); return; }
    r.results.forEach((x) => {
      const b = el("button", "hf-hit");
      b.appendChild(el("span", "mono", x.repo));
      if (x.cn_origin) b.appendChild(el("span", "chip warn", "中國來源・政府案不可用"));
      b.appendChild(el("span", "small muted", `下載 ${int(x.downloads)}・${x.updated || ""}`));
      b.onclick = () => { fRepo.value = x.repo; sOut.hidden = true; listBtn.click(); };
      sOut.appendChild(b);
    });
  };
  sBtn.onclick = search;
  imeGuard(fQ, search);
  const listBtn = el("button", "btn primary", "列出可下載的量化");
  const la = el("div", "slot-actions");
  la.appendChild(listBtn);
  dc.appendChild(la);
  const choices = el("div");
  dc.appendChild(choices);
  fNode.onchange = () => { choices.textContent = ""; };
  [fRepo, fTok].forEach((i) => imeGuard(i, () => listBtn.click()));
  listBtn.onclick = async () => {
    const nodeId = fNode.value;
    choices.textContent = "";
    choices.appendChild(el("p", "small muted", "查詢中…"));
    let r;
    const stale = () => fNode.value !== nodeId;   // 查詢途中換了目標機器：舊結果丟掉，免得下載按鈕綁在上一台
    const repo = hfRepoOf(fRepo.value) || fRepo.value.trim();
    fRepo.value = repo;
    try { r = await api(`/api/node/${nodeId}/library/hf`, { method: "POST", body: { repo, token: fTok.value } }); } catch (ex) {
      if (stale()) return;
      choices.textContent = "";
      choices.appendChild(el("p", "note bad", ex.message));
      return;
    }
    if (stale()) return;
    choices.textContent = "";
    if (!r.choices.length) {
      // 原始權重（safetensors）的 repo：推理引擎只吃 GGUF，自動找別人轉好的 GGUF 版本給人挑
      choices.appendChild(el("p", "note", `${repo} 是原始權重，沒有 GGUF 檔。下面是別人轉好的 GGUF 版本（照下載數排），點一下列出量化：`));
      const alt = el("div", "hf-results");
      choices.appendChild(alt);
      loading(alt);
      const name = repo.split("/").pop().replace(/-(GGUF|gguf)$/, "");
      let found = [];
      try { found = (await api(`/api/node/${nodeId}/library/hf/search`, { method: "POST", body: { q: name.slice(0, 80), token: fTok.value } })).results; } catch (_) { found = []; }
      if (stale()) return;
      alt.textContent = "";
      if (!found.length) { alt.appendChild(el("p", "small muted", "還沒有人轉成 GGUF。可以換一個名稱搜尋，或等社群轉好再來。")); return; }
      found.forEach((x) => {
        const b = el("button", "hf-hit");
        b.appendChild(el("span", "mono", x.repo));
        if (x.cn_origin) b.appendChild(el("span", "chip warn", "中國來源・政府案不可用"));
        b.appendChild(el("span", "small muted", `下載 ${int(x.downloads)}・${x.updated || ""}`));
        b.onclick = () => { fRepo.value = x.repo; listBtn.click(); };
        alt.appendChild(b);
      });
      return;
    }
    if (r.choices.some((c) => c.cn_origin)) choices.appendChild(el("p", "note warn", "這是中國來源的模型：內部測試可以用，政府案不得使用。"));
    let mm = null;
    if (r.mmproj.length) {
      const w = el("div");
      w.appendChild(el("label", "field", "看圖用的 mmproj（選填）"));
      mm = el("select");
      [["", "不下載（只用文字）"], ...r.mmproj.map((x) => [x, x])].forEach(([val, t]) => { const o = el("option", null, t); o.value = val; mm.appendChild(o); });
      w.appendChild(mm);
      choices.appendChild(w);
    }
    const sb = el("div", "scrollbox");
    const tb = el("table", "api-table");
    tb.innerHTML = "<thead><tr><th>量化</th><th class='num'>大小</th><th>這台跑起來</th><th></th><th></th></tr></thead>";
    const tbody = el("tbody");
    r.choices.forEach((c) => {
      const tr = el("tr");
      const qn = el("td", "mono", (c.dir ? c.dir + "/" : "") + (c.aux ? c.stem : (c.quant || c.stem)));
      if (r.pick && c.stem === r.pick) qn.appendChild(el("span", "chip brand", "建議"));
      tr.appendChild(qn);
      tr.appendChild(el("td", "num", `${fmt(c.size_gib)} GiB`));
      const pc = el("td");
      if (!c.aux) pc.appendChild(planCell(c.plan));
      tr.appendChild(pc);
      tr.appendChild(el("td", "small muted", !c.fits_disk ? "磁碟不夠"
        : c.aux ? "輔助檔（推測解碼用），不是主模型" : (c.files.length > 1 ? `${c.files.length} 個分片` : "")));
      const td = el("td");
      const b = el("button", "btn sm primary", "下載");
      b.disabled = !c.fits_disk;
      b.onclick = async () => {
        if (stale()) { toast("目標機器換了，請重新列出可下載的量化", "bad"); return; }
        try {
          await api(`/api/node/${nodeId}/library/download`, { method: "POST", body: { repo: r.repo, stem: c.stem, mmproj: mm?.value || null, token: fTok.value } });
          toast(`開始下載 ${c.stem}`, "ok");
          fTok.value = "";
          viewLib();
        } catch (ex) { toast(ex.message, "bad"); }
      };
      td.appendChild(b);
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
    tb.appendChild(tbody);
    sb.appendChild(tb);
    choices.appendChild(sb);
    choices.appendChild(el("p", "assume", "等級與速度依這台的記憶體與頻寬估算；深色＝放顯示卡、淺色＝放系統記憶體、灰色＝放硬碟（最慢）。實際載入前還會再做一次記憶體試算。"));
  };
  return dc;
}

// 給別台機器連進來用的位址：單機模式的主控台用 127.0.0.1 連代理，那不是別人能連的位址
const hostOf = (n) => {
  const h = n.state?.machine?.ip || n.host;
  return /^(127\.|localhost$|::1$)/.test(h) ? "<這台的 IP>" : h;
};

// 不經 Hugging Face：自己把檔案放進存放區。醫院這類不能連外的環境就是走這條。
function libManualCard(ok) {
  const c = card("手動放入", "不能連外、或模型已經在別台機器上時");
  let cur = ok[0];
  const body = el("div");
  const draw = () => {
    body.textContent = "";
    const root = cur.lib.root;
    body.appendChild(el("p", "small", "存放區底下一顆模型一個資料夾，放好之後重新整理這頁就會出現，不用重啟。"));
    body.appendChild(el("pre", "log tree",
      `${root}/\n├── gemma-4-E4B-it/\n│   ├── gemma-4-E4B-it-Q4_K_M.gguf      同一個資料夾可放多種量化\n│   └── mmproj-F16.gguf                 看圖模型的視覺檔，檔名以 mmproj 開頭\n└── gpt-oss-120b/\n    ├── gpt-oss-120b-00001-of-00002.gguf 分片放同一個資料夾，要齊才會出現\n    └── gpt-oss-120b-00002-of-00002.gguf`));
    // 指令要能原封不動貼上執行：<資料夾> 這種佔位在 shell 裡會被當成重新導向，改成先設變數
    const user = (root.match(/^\/home\/([^/]+)\//) || [])[1] || "帳號";
    body.appendChild(copyRow("1. 在這台建資料夾", `F=gemma-4-E4B-it; mkdir -p "${root}/$F"`));
    body.appendChild(copyRow("2. 在來源機器上傳過來", `F=gemma-4-E4B-it; rsync -avP "./$F/" "${user}@${hostOf(cur.n)}:${root}/$F/"`));
    body.appendChild(el("p", "assume", "F 換成模型的資料夾名稱。第 2 行在來源機器、模型資料夾的上一層執行，從那台直接傳給這台，不要經過自己的電腦中轉。直接丟在存放區根目錄的 .gguf 不會被認到。"));
  };
  if (ok.length > 1) {
    const sel = el("select");
    ok.forEach((x, i) => { const o = el("option", null, x.n.name); o.value = String(i); sel.appendChild(o); });
    sel.onchange = () => { cur = ok[Number(sel.value)]; draw(); };
    const w = el("div", "form-row");
    const d = el("div"); d.appendChild(el("label", null, "放到哪台")); d.appendChild(sel);
    w.appendChild(d);
    c.appendChild(w);
  }
  draw();
  c.appendChild(body);
  return c;
}

// 「換上」：挑這台可以換模型的欄位。只有一格就直接打開換模型，多格先問放哪一格。
function libSwapIn(n, m) {
  // GGUF：Strata 與 llama.cpp 欄位都換得進去，空欄位也可以放；Strata 的模型只能進留有 Strata 設定的欄位
  const slots = (n.state?.slots || []).filter((s) => m.engine === "llama"
    ? s.kind === "strata" || s.kind === "llama" || !s.unit
    : s.kind === "strata" || (s.kind === "llama" && s.has_strata));
  if (!slots.length) { toast(`${n.name} 沒有可以換模型的欄位`, "bad"); return; }
  if (slots.length === 1) { openSwap(n, slots[0], m.id); return; }
  const d = el("div");
  d.appendChild(el("p", "small muted", `要把 ${m.name}${m.quant ? " " + m.quant : ""} 放進 ${n.name} 的哪一格？`));
  const list = el("div", "pick-list");
  let ov;
  slots.forEach((s) => {
    const b = el("button", "btn", `${s.role}・${s.tier || ""}　${s.unit ? (s.model || s.unit) : "空欄位"}`);
    b.onclick = () => { ov.remove(); openSwap(n, s, m.id); };
    list.appendChild(b);
  });
  d.appendChild(list);
  ov = modal({ title: "換上到哪個欄位", bodyNode: d });
}

const jobActive = (j) => j.state === "running" || j.state === "queued";

function libJobsCard(entries) {
  const jc = card("下載紀錄", "進行中的下載每 3 秒更新");
  const multi = entries.length > 1;
  entries.forEach(({ n, jobs }) => jobs.slice(0, 5).forEach((j) => {
    const g = el("div", "dl");
    const pct = j.total ? Math.floor((j.done / j.total) * 100) : 0;
    const label = { queued: "排隊中", running: `下載中 ${pct}%`, done: "完成", failed: "失敗", cancelled: "已取消", interrupted: "中斷（再下載一次會接續）" }[j.state] || j.state;
    const top = el("div", "dl-top");
    top.appendChild(el("span", "mono", `${multi ? n.name + "　" : ""}${j.sd_model ? j.title : j.repo + "　" + j.stem}`));
    top.appendChild(el("span", "spacer"));
    top.appendChild(el("span", "small " + (j.state === "failed" ? "bad-t" : "muted"), label));
    if (jobActive(j)) {
      const c = el("button", "btn sm", "取消");
      c.onclick = async () => { try { await api(`/api/node/${n.id}/library/download/${j.id}/cancel`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); } };
      top.appendChild(c);
    }
    g.appendChild(top);
    const bar = el("div", "bar");
    const fill = el("div", "seg");
    fill.style.width = (j.state === "done" ? 100 : pct) + "%";
    bar.appendChild(fill);
    g.appendChild(bar);
    g.appendChild(el("div", "small muted", `${fmt(j.done / 1073741824)} / ${fmt(j.total / 1073741824)} GiB${j.error ? "　" + j.error : ""}`));
    jc.appendChild(g);
  }));
  return jc;
}

// 下載進行中只換掉「下載紀錄」這一張卡。以前是每 3 秒整頁 viewLib()：主區塊先清空、出現載入中、
// 再全部重建，頁長塌掉又長回來，畫面每 3 秒跳一次，Hugging Face 欄位打到一半的字也被清掉（2026-10-22 中原主機實際踩到）。
// 下載結束時已安裝清單要跟著變，才整頁重畫那一次。
function watchLibJobs(view, jc, entries) {
  clearTimeout(S.libTimer);
  if (!entries.some((x) => x.jobs.some(jobActive))) return;
  S.libTimer = setTimeout(async () => {
    if (S.view !== view || !jc.isConnected) return;
    let now;
    try {
      now = await Promise.all(entries.map(async (x) => ({ n: x.n, jobs: x.jobs.some(jobActive)
        ? ((await api(`/api/node/${x.n.id}/library`)).downloads || []) : x.jobs })));
    } catch (ex) { watchLibJobs(view, jc, entries); return; }
    if (S.view !== view || !jc.isConnected) return;
    if (!now.some((x) => x.jobs.some(jobActive))) {
      if (document.querySelector(".overlay")) { watchLibJobs(view, jc, entries); return; }   // 有彈窗開著就晚點再重畫
      S.rec = {};                          // 推薦列上的「下載中」要變成「換上」
      S.sdRec = {};
      viewLib();
      return;
    }
    const fresh = libJobsCard(now);
    jc.replaceWith(fresh);
    watchLibJobs(view, fresh, now);
  }, 3000);
}


/* ================================================================ 從瀏覽器匯入 GGUF
   分塊送（每塊 32 MB）、可以續傳：中斷後再選同一個檔案，會從主機已收到的位置接著送。 */
// 跟代理的 edge_sha256 同一個算法：頭 4 MB ＋ 尾 4 MB（檔案小於 8 MB 時尾段從 4 MB 處開始）
async function edgeSha256(f) {
  const E = 4 * 1024 * 1024;
  const parts = [await f.slice(0, E).arrayBuffer()];
  if (f.size > E) parts.push(await f.slice(Math.max(E, f.size - E), f.size).arrayBuffer());
  const buf = new Uint8Array(parts.reduce((a, p) => a + p.byteLength, 0));
  let o = 0;
  parts.forEach((p) => { buf.set(new Uint8Array(p), o); o += p.byteLength; });
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", buf))].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256Hex(blob) {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()))].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function libUploadCard(ok, only) {
  const c = card("從這台電腦匯入 GGUF", "不用轉格式；檔案直接送到那台的存放區");
  const form = el("div", "form-row");
  const fNode = selectInput(ok.map(({ n, lib }) => [n.id, `${n.name}（剩 ${fmt(lib.free_gib, 0)} GiB）`]), only || ok[0]?.n.id);
  const fFile = el("input");
  fFile.type = "file";
  fFile.accept = ".gguf";
  fFile.multiple = true;
  const fFolder = textInput("", "預設用第一個檔名");
  if (ok.length > 1) form.appendChild(fieldBox("匯入到哪台", fNode));
  form.append(fieldBox("GGUF 檔案", fFile, "分片模型請一次選齊所有分片；看圖模型把 mmproj 一起選"),
    fieldBox("放進哪個資料夾", fFolder, "一顆模型一個資料夾"));
  c.appendChild(form);
  const prog = el("div");
  const go_ = el("button", "btn primary", "開始匯入");
  const stop = el("button", "btn sm", "暫停");
  stop.hidden = true;
  const row = el("div", "slot-actions");
  row.append(go_, stop);
  c.append(row, prog);
  let ctl = null;
  stop.onclick = () => ctl?.abort();
  go_.onclick = async () => {
    const files = [...fFile.files];
    if (!files.length) { errNote(prog, "先選檔案"); return; }
    if (files.some((f) => !/\.gguf$/i.test(f.name))) { errNote(prog, "只收 .gguf 檔"); return; }
    const folder = (fFolder.value.trim() || files[0].name.replace(/\.gguf$/i, "").replace(/-\d{5}-of-\d{5}$/, "")).replace(/[^\w.-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 80);
    const nid = fNode.value;
    ctl = new AbortController();
    go_.disabled = true; stop.hidden = false;
    prog.textContent = "";
    const line = el("p", "small mono", "準備中…");
    const bar = el("div", "bar thin");
    const fill = el("div", "seg");
    bar.appendChild(fill);
    prog.append(line, bar);
    const CH = 32 * 1024 * 1024;
    const total = files.reduce((a, f) => a + f.size, 0);
    let doneBytes = 0;
    const skipped = [];
    try {
      for (const f of files) {
        const name = f.name.replace(/[^\w.+-]+/g, "-");
        const st = await api(`/api/node/${nid}/library/upload/status`, { method: "POST", body: { folder, name } });
        if (st.exists) {
          // 同名檔已經在：大小與頭尾各 4 MB 的 sha256 都一樣才當成同一個（續傳時前面已送完的分片）；不一樣就停下來講清楚
          if (st.size !== f.size || st.edge_sha256 !== await edgeSha256(f)) {
            throw new Error(`${folder}/${name} 已經有一個不一樣的檔案。請換一個資料夾名稱，或先到「已安裝」刪掉舊的`);
          }
          skipped.push(name);
          doneBytes += f.size;
          continue;
        }
        let off = st.received || 0;                      // 續傳：從主機已收到的位置接著送
        while (off < f.size) {
          const chunk = f.slice(off, Math.min(off + CH, f.size));
          const res = await fetch(`/api/node/${nid}/library/upload?` + new URLSearchParams({ folder, name, offset: off, total: f.size }), {
            method: "POST", body: chunk, signal: ctl.signal, credentials: "same-origin",
            headers: { "Content-Type": "application/octet-stream", "X-XConsole-Upload": "1" } });
          const r = await res.json().catch(() => ({}));
          if (res.status === 409 && r.received !== undefined) { off = r.received; continue; }
          if (!res.ok) throw new Error(r.error || `伺服器回 ${res.status}`);
          off = r.received;
          const pct = Math.floor(((doneBytes + off) / total) * 100);
          fill.style.width = pct + "%";
          line.textContent = `${f.name}　${fmt((doneBytes + off) / 1073741824)} / ${fmt(total / 1073741824)} GiB（${pct}%）`;
        }
        doneBytes += f.size;
      }
      toast(`已匯入到 ${folder}/${skipped.length ? `（${skipped.length} 個檔案已經在、內容相同，沒有重送）` : ""}`, "ok");
      if (S.rec) delete S.rec[nid];
      S.libTab = "installed";
      viewLib();
    } catch (ex) {
      line.textContent = ex.name === "AbortError" ? "已暫停；再按一次「開始匯入」會從中斷的地方接著送" : "匯入失敗：" + ex.message;
    } finally {
      go_.disabled = false; stop.hidden = true;
    }
  };
  return c;
}

/* ================================================================ 執行設定（參考 LocModel 執行模型的那一頁）
   上下文一鍵切換（每個選項都先用准入公式試算放不放得下）、思考模式、區網存取，並顯示預估速度與載入時間。 */
async function openRunSettings(node, s) {
  const body = el("div");
  loading(body);
  const ov = modal({ title: `${s.role}・${s.model || ""}　執行設定`, bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide");
  let info;
  try { info = await api(`/api/node/${node.id}/apiinfo/${s.id}`); } catch (ex) { errNote(body, ex.message); return; }
  const run = info.run;
  body.textContent = "";
  if (!run) { body.appendChild(el("p", "note", "這個欄位不是 llama.cpp 跑的，執行設定請到 API 串接頁。")); return; }
  const stats = el("div", "tiles light");
  const tile = (k, v, sub) => { const t = el("div", "tile"); t.append(el("div", "k", k), el("div", "v", v)); if (sub) t.appendChild(el("div", "s", sub)); return t; };
  stats.append(
    tile("預估速度", run.tok_s != null ? `約 ${fmt(run.tok_s, 0)} tok/s` : "未知", run.tok_s != null ? "依記憶體頻寬估算，實際以實測為準" : "到「這台的設定」填記憶體頻寬"),
    tile("載入時間", run.load_sec != null ? `${run.load_sec} 秒` : run.load_est_sec != null ? `約 ${run.load_est_sec} 秒` : "—",
      run.load_sec != null ? "上次實際載入" : "還沒實際載入過，依磁碟速度估"),
    tile("模型", `${fmt(run.size_gib)} GiB`, run.ctx_train ? `最長上下文 ${int(run.ctx_train)}` : ""));
  body.appendChild(stats);

  let ctx = info.max_context, thinking = run.thinking, lan = run.lan;
  body.appendChild(el("h4", "pick-h", "上下文長度"));
  const ctxBar = el("div", "tabbar run-ctx");
  const ctxNote = el("p", "small muted");
  const drawCtx = () => {
    ctxBar.textContent = "";
    run.options.forEach((o) => {
      const b = el("button", null, `${o.ctx / 1024}K`);
      b.title = o.allow ? `KV 約 ${fmt(o.kv_gib)} GiB；載完最壞剩 ${fmt(o.worst_gib)} GiB` : (o.reason || "放不下");
      if (o.ctx === ctx) b.setAttribute("aria-current", "true");
      b.disabled = !o.allow && o.ctx !== info.max_context;
      b.onclick = () => { ctx = o.ctx; drawCtx(); };
      ctxBar.appendChild(b);
    });
    const o = run.options.find((x) => x.ctx === ctx);
    ctxNote.textContent = o ? `${int(ctx)} 個 token：KV 約 ${fmt(o.kv_gib)} GiB，載完最壞還剩 ${fmt(o.worst_gib)} GiB。放不下的選項會反灰（游標移上去看原因）。`
      : `目前 ${int(ctx)}（不在常用選項裡）。`;
  };
  drawCtx();
  body.append(ctxBar, ctxNote);

  body.appendChild(el("h4", "pick-h", "思考模式"));
  const thBar = el("div", "tabbar");
  const drawTh = () => {
    thBar.textContent = "";
    [["auto", "照模型預設"], ["on", "開：先想再答"], ["off", "關：直接回答"]].forEach(([k, label]) => {
      const b = el("button", null, label);
      if (k === thinking) b.setAttribute("aria-current", "true");
      b.disabled = !run.thinking_supported && k !== "auto";
      b.onclick = () => { thinking = k; drawTh(); };
      thBar.appendChild(b);
    });
  };
  drawTh();
  body.append(thBar, el("p", "small muted", run.thinking_supported ? "只對會思考的模型有用（例 gpt-oss、Granite、Nemotron）；關掉回答比較快，但難題可能答得比較差。"
    : "這支 llama.cpp 太舊，不支援切換思考模式；到「這台的設定」重新安裝推理引擎。"));

  body.appendChild(el("h4", "pick-h", "區網存取"));
  const lanBox = el("label", "check-row");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = lan;
  cb.onchange = () => { lan = cb.checked; };
  lanBox.append(cb, document.createTextNode(" 讓區網裡的其他電腦（IDE、手機）連得到這個模型"));
  body.append(lanBox, el("p", "small muted", "關掉就只有這台自己連得到（127.0.0.1）；主控台的網頁對話照樣能用。"));
  const err = el("div");
  body.appendChild(err);
  formButton(ov, "套用並重新載入", "primary", async () => {
    const want = {};
    if (ctx !== info.max_context) want.max_context = ctx;
    if (thinking !== run.thinking) want.thinking = thinking;
    if (lan !== run.lan) want.lan = lan;
    if (!Object.keys(want).length) { ov.remove(); return; }
    errNote(err, "");
    err.appendChild(el("p", "note", "重新載入模型中，大模型要幾分鐘…"));
    try {
      await api(`/api/node/${node.id}/apiinfo/${s.id}/settings`, { method: "POST", body: want });
    } catch (ex) { errNote(err, ex.message); return; }
    ov.remove();
    toast("已套用並重新載入", "ok");
    await refresh();
  });
}

/* ================================================================ 推薦模型
   參考 LocModel 的做法：先讀懂這台（晶片、記憶體、頻寬、存放區）→ 依記憶體算出每顆模型放不放得下、
   放在哪、大概多快、該下載哪個量化 → 一鍵下載 → 換上欄位。數字全由主控台 recommend.py 算，畫面不自己算。 */

// 等級的字照 LocModel：流暢／能跑／很慢（要用硬碟）／跑不動
const LEVEL = { smooth: ["流暢", "ok"], ok: ["能跑", "warn"], disk: ["很慢", "bad"], no: ["跑不動", "bad"], unknown: ["待更新", "idle"] };

async function getRec(nid, fresh) {
  S.rec = S.rec || {};
  const c = S.rec[nid];
  if (!fresh && c && Date.now() - c.at < 60000) return c.data;
  const data = await api(`/api/node/${nid}/recommend`);
  S.rec[nid] = { at: Date.now(), data };
  return data;
}

function capHeadline(cap) {
  if (!cap) return "還讀不到這台的記憶體";
  if (!cap.fast) return cap.memory_model === "discrete" && !cap.cards ? "這台沒有 GPU" : "記憶體扣掉底線後放不下推薦的模型";
  return cap.max_dense_b ? `流暢執行 ${cap.max_dense_b}B 以內的模型` : "適合跑 4B 以下的小模型";
}

function capNote(cap) {
  if (cap && !cap.speed_known && cap.fast) return "還不知道這台的記憶體頻寬，速度算不出來：這裡只照記憶體容量估。到「這台的設定」填頻寬，推薦會標出預估速度。";
  if (cap?.max_fit_b > cap?.max_dense_b) {
    return `記憶體放得下約 ${cap.max_fit_b}B 的稠密模型，但超過 ${cap.max_dense_b}B 會低於每秒 10 個 token；`
      + "MoE 模型每次只動用一部分參數，更大的也跑得順。";
  }
  if (cap?.max_moe_b && cap.max_moe_b > cap.max_dense_b) {
    return `顯示卡放不下時，MoE 模型會自動把部分專家層放到系統記憶體繼續跑——最大約 ${cap.max_moe_b}B（4 位元量化），只是比較慢。`;
  }
  return cap?.note || "";
}

// 一條橫向分層長條：越左越快。marker＝正在跑的模型占多少，標在長條上
function tierBar(cap, marker) {
  const box = el("div", "tier");
  const total = cap.tiers.reduce((a, t) => a + Math.max(t.gib, 0), 0) || 1;
  if (marker) {
    const pos = Math.min(100, (marker.gib / total) * 100);
    const m = el("div", "tier-mark");
    m.style.left = `clamp(0px, calc(${pos}% - 1px), calc(100% - 2px))`;
    m.appendChild(el("span", "tier-tag", `${marker.label}・${fmt(marker.gib, 0)} GiB`));
    box.appendChild(m);
  }
  const bar = el("div", "tier-bar");
  cap.tiers.forEach((t) => {
    const seg = el("div", "tier-seg " + t.kind);
    seg.style.flex = `${Math.max(t.gib, total * 0.08)} 1 0`;
    seg.appendChild(el("b", null, t.label));
    seg.appendChild(el("span", null, `${fmt(t.gib, 0)} GiB`));
    bar.appendChild(seg);
  });
  box.appendChild(bar);
  box.appendChild(el("div", "small muted", cap.memory_model === "discrete" && cap.slow
    ? "模型先放進顯示卡，放不下的專家層依序放到系統記憶體。越靠左越快。"
    : cap.memory_model === "unified_split" ? "模型放 GPU 那一塊；系統那一塊只給引擎，吃爆會拖死整台，載入前一律先試算。"
      : "載入前先試算載完最壞剩多少，低於保留量就擋下，不會把整台拖死。"));
  return box;
}

// 這台的硬體卡：左邊「能跑多大」＋分層長條，右邊硬體摘要
function hwCard(n, rec) {
  const st = n.state;
  const cap = rec.capacity;
  const c = el("section", "card hw rv");
  const left = el("div", "hw-main");
  left.appendChild(el("h2", null, capHeadline(cap)));
  left.appendChild(el("p", "small muted", capNote(cap)));
  if (mmUnsure(n)) {
    const w = el("div", "note warn");
    w.appendChild(document.createTextNode(`記憶體架構還沒確認：現在照「${MEM_LABEL[st.pools.memory_model]}」算，`
      + `偵測建議是「${MEM_LABEL[st.chip.suggest]}」。架構不對，推薦與載入前的試算都會不準。`));
    const b = el("button", "btn sm primary", "確認記憶體架構");
    b.onclick = () => openMachineSettings(n.id);
    w.appendChild(b);
    left.appendChild(w);
  }
  // 標正在跑的模型與實際用量。以前標的是推薦模型（xVITA 31B・30 GiB），機器明明在跑別顆，
  // 看的人會以為在跑它，數字也跟上面的標題對不起來；推薦留在「模型」頁
  // 純 CPU 的機器用量在系統記憶體；統一記憶體兩塊都算；獨顯只算顯示記憶體。
  // 欄位沒綁特定顯示卡時，顯示記憶體是整台分攤的（含其他程式），要標明是整機用量，不能說成模型本身多大
  const rs = runningSlot(n);
  const onRam = cap.cpu_only, onGpu = !onRam && cap.memory_model === "discrete";
  const used = rs && (onRam ? rs.ram_gib || 0 : (rs.vram_gib || 0) + (onGpu ? 0 : rs.ram_gib || 0));
  const whole = !onRam && rs?.vram_exact === false ? "（整機用量）" : "";
  if (cap.tiers.length) left.appendChild(tierBar(cap, used ? { label: `現在執行 ${rs.model || rs.role}${whole}`, gib: used } : null));
  c.appendChild(left);

  const right = el("div", "hw-side");
  const ch = rec.chip || {};
  const p = st.pools;
  const row = (k, v, sub, act) => {
    const r = el("div", "hw-row");
    r.appendChild(el("span", "k", k));
    const d = el("div", "v");
    d.appendChild(el("div", null, v));
    if (sub || act) {
      const sd = el("div", "small muted", sub || "");
      if (act) { sd.append("　"); sd.appendChild(act); }       // 「更改」跟說明同一行，右欄不要比左欄高一截
      d.appendChild(sd);
    }
    r.appendChild(d);
    right.appendChild(r);
  };
  const link = (t, fn) => { const a = el("button", "linkbtn", t); a.onclick = fn; return a; };
  const g = p.unified || p.vram;
  row(p.memory_model === "discrete" ? "顯示卡" : "晶片", ch.gpu || "認不出型號",
    g ? `${p.memory_model === "unified_single" ? "統一記憶體" : "顯示記憶體"} ${fmt(g.total_gib, 0)} GiB，已用 ${fmt(g.used_gib, 1)} GiB` +
      (cap.cards > 1 ? `（${cap.cards} 張）` : "") : "");
  row("處理器", ch.cpu || "—", ch.cores ? `${ch.cores} 執行緒` : "");
  if (p.memory_model !== "unified_single") row("系統記憶體", `${fmt(p.ram.total_gib, 0)} GiB`, `可用 ${fmt(p.ram.free_gib, 1)} GiB`);
  row("記憶體頻寬", ch.bandwidth_gbs ? `${ch.bandwidth_gbs} GB/s` : "未知",
    ch.bandwidth_gbs ? ch.bandwidth_src : "估不出速度，請到這台的設定填", link("更改", () => openMachineSettings(n.id)));
  if (rec.store) {
    row("模型存放區", rec.store.root, `剩 ${fmt(rec.store.free_gib, 0)} GiB${rec.store.hdd === true ? "，機械硬碟（大模型載入會很慢）" : rec.store.hdd === false ? "，固態硬碟" : ""}`,
      link("更改", () => openMachineSettings(n.id)));
  }
  const again = link("重新偵測", async () => {
    try { await getRec(n.id, true); toast("已重新讀取", "ok"); } catch (ex) { toast(ex.message, "bad"); }
    render();
  });
  right.appendChild(again);
  c.appendChild(right);
  return c;
}

// 看圖用的投影檔由主控台挑（recommend.pick_mmproj），記憶體試算、磁碟檢查與實際下載用同一個
const recMmproj = (it) => it.mmproj_pick || null;

// 一列推薦：名稱與說明 ｜ 量化（可改） ｜ 適配程度、放在哪、預估速度 ｜ 下載／換上
// 「新」：釋出四個月內（照 LocModel 的標籤）
function isNew(released) {
  const m = /^(\d{4})-(\d{2})/.exec(released || "");
  if (!m) return false;
  const now = new Date();
  return (now.getFullYear() - Number(m[1])) * 12 + (now.getMonth() + 1 - Number(m[2])) <= 4;
}

function relLabel(released) {
  const [y, mo] = (released || "").split("-");
  return y ? (mo ? `${y}年${Number(mo)}月釋出` : `${y}年`) : "";
}

// 推薦清單的一列，照 LocModel：名稱＋標籤＋釋出月／來源・說明｜量化（推薦）＋大小｜等級＋速度＋分層條｜下載
function recRow(n, it, lib, ctx) {
  const row = el("div", "rec-row");
  const draw = (stem) => {
    row.textContent = "";
    const p = it.plans.find((x) => x.stem === stem);
    const main = el("div", "rec-main");
    const t = el("div", "rec-title");
    t.appendChild(el("b", null, it.title));
    if (it.vision) t.appendChild(el("span", "chip info", "看圖"));
    if (isNew(it.released)) t.appendChild(el("span", "chip new", "新"));
    (it.tags || []).filter((x) => x === "自家" || x === "繁中").forEach((x) => t.appendChild(el("span", "chip" + (x === "自家" ? " brand" : ""), x)));
    if (it.cn_origin) t.appendChild(el("span", "chip warn", "中國來源・政府案不可用"));
    if (it.released) t.appendChild(el("span", "small muted", relLabel(it.released)));
    main.appendChild(t);
    const src = (it.repo || "").split("/")[0];
    main.appendChild(el("div", "small muted", [it.vendor, src && src !== it.vendor ? src : null, it.desc].filter(Boolean).join(" · ")));
    row.appendChild(main);

    const q = el("div", "rec-quant");
    if (!it.plans.length) {
      q.appendChild(el("span", "small muted", "還沒從 Hugging Face 讀到大小"));
    } else if (ctx.compact) {
      q.appendChild(el("b", "mono", p?.quant || "單一檔案"));
      q.appendChild(el("span", "small muted", p ? `${fmt(p.size_gib)} GB` : ""));
    } else {
      const sel = el("select");
      it.plans.forEach((x) => {
        const tag = [x.stem === it.pick && "推薦", x.installed_id && "已下載"].filter(Boolean).join("・");
        const o = el("option", null, `${x.quant || "單一檔案"}${tag ? "（" + tag + "）" : ""}`);
        o.value = x.stem;
        sel.appendChild(o);
      });
      sel.value = stem;
      sel.onchange = () => draw(sel.value);
      q.appendChild(sel);
      q.appendChild(el("span", "small muted", p ? `${fmt(p.size_gib + (it.vision ? recMmproj(it)?.gib || 0 : 0))} GB` : ""));
    }
    row.appendChild(q);

    const fit = el("div", "rec-fit");
    const [lbl, cls] = p ? levelOf(p) : LEVEL[it.level] || LEVEL.unknown;
    const line = el("div", "rec-fit-line");
    line.appendChild(el("span", "chip " + cls, lbl));
    if (p) line.appendChild(el("span", "small muted", p.tok_s != null ? `約 ${fmt(p.tok_s, p.tok_s < 10 ? 1 : 0)} tok/s` : "速度未知"));
    fit.appendChild(line);
    if (p) {
      const bar = splitBar(p.split);
      bar.title = p.place;                          // 放在哪：滑過去看（LocModel 也是只畫一條）
      fit.appendChild(bar);
    }
    row.appendChild(fit);

    const act = el("div", "rec-act");
    const libM = p?.installed_id && (lib?.gguf || []).find((m) => m.id === p.installed_id);
    if (p?.in_use) {
      act.appendChild(el("span", "chip ok", "使用中"));
    } else if (libM) {
      const b = el("button", "btn sm primary", "執行");
      b.onclick = () => runModel(n, libM);
      act.appendChild(b);
    } else if (it.job) {
      act.appendChild(el("span", "small muted", `下載中 ${it.job.total ? Math.floor((it.job.done / it.job.total) * 100) : 0}%`));
    } else if (p) {
      const mm = it.vision ? recMmproj(it) : null;
      const b = el("button", "btn sm primary", "下載");
      b.disabled = !p.fits_disk;
      b.title = !p.fits_disk ? "存放區空間不夠（另需保留 10 GiB）" : p.level === "no" ? "這台記憶體不夠，下載了也跑不動" : "";
      b.onclick = () => recDownload(n, it, p, mm, ctx);
      act.appendChild(b);
    }
    row.appendChild(act);
  };
  draw(it.installed || it.pick || it.plans[0]?.stem);
  return row;
}

function recDownload(n, it, p, mm, ctx) {
  const d = el("div");
  d.appendChild(el("p", null, `把 ${it.title} ${p.quant || ""}（${fmt(p.size_gib)} GiB${mm ? `，含看圖用的元件 ${fmt(mm.gib)} GiB` : ""}）下載到 ${n.name}？`));
  d.appendChild(el("p", "small muted", `${p.place}，預估約 ${p.tok_s != null ? fmt(p.tok_s, 0) + " tok/s" : "（速度未知）"}。下載在背景進行，中斷了再按一次會接續；完成後在清單上按「換上」。`));
  modal({ title: "下載模型", bodyNode: d, confirmText: "開始下載", onConfirm: async () => {
    try {
      await api(`/api/node/${n.id}/library/download`, { method: "POST", body: { repo: it.repo, stem: p.stem, mmproj: mm?.path || null } });
      toast("已開始下載", "ok");
    } catch (ex) { toast(ex.message, "bad"); return; }
    if (S.rec) delete S.rec[n.id];
    ctx.after?.();
  } });
}

// 推薦清單（模型庫的「推薦模型」分頁），照 LocModel：一排篩選（適合我的電腦／全部／對話／程式設計／看圖／推理），下面一列一顆。
// 「適合我的電腦」只列流暢與能跑的；「全部」連很慢、跑不動的也列（寫原因），排在後面
function recList(n, rec, lib, ctx) {
  const c = el("div", "rec-wrap rv");
  const bar = el("div", "chip-row");
  const list = el("div", "rec-list");
  const uses = [["fit", "適合我的電腦"], ["all", "全部"], ["chat", "對話"], ["code", "程式設計"], ["vision", "看圖"], ["reason", "推理"]];
  const useOk = (x, k) => k === "all" || (k === "fit" ? ["smooth", "ok"].includes(x.level)
    : k === "reason" ? (x.tags || []).includes("推理") : k === "vision" ? x.use === "vision" || x.vision : x.use === k);
  const fill = () => {
    bar.textContent = "";
    uses.forEach(([k, label]) => {
      const b = el("button", "pill", label);
      if ((S.recUse || "fit") === k) b.setAttribute("aria-current", "true");
      b.onclick = () => { S.recUse = k; fill(); };
      bar.appendChild(b);
    });
    bar.appendChild(el("span", "spacer"));
    const mg = el("button", "linkbtn small", "管理推薦清單");
    mg.onclick = openCatalog;
    bar.appendChild(mg);
    list.textContent = "";
    const rank = { smooth: 0, ok: 1, disk: 2, no: 3, unknown: 4 };
    const rows = rec.items.filter((x) => useOk(x, S.recUse || "fit"))
      .sort((a, b) => (rank[a.level] ?? 4) - (rank[b.level] ?? 4) || a.order - b.order);
    rows.forEach((it) => list.appendChild(recRow(n, it, lib, ctx)));
    if (!rows.length) list.appendChild(el("p", "small muted pad", (S.recUse || "fit") === "fit"
      ? "推薦清單裡沒有這台跑得順的模型；按「全部」看要用硬碟或跑不動的，或到「搜尋 HuggingFace」找小一點的。"
      : "這個分類裡沒有模型。"));
  };
  fill();
  c.append(bar, list);
  const a = rec.assumptions;
  c.appendChild(el("p", "small muted", `速度是估算：記憶體頻寬 × ${Math.round(a.efficiency * 100)}% ÷ 每個 token 要讀的權重（MoE 只算被選中的專家），`
    + `上下文以 ${int(a.ctx)} 計、低於 ${a.smooth_toks} tok/s 不算流暢；在這台對話過之後會依實測自動校正。標「中國來源」的模型政府案不得使用。`));
  return c;
}

// 精簡版（總覽）：硬體卡＋前三名
function recTop(n, rec, lib, ctx) {
  const c = card("為這台機器推薦");
  c.classList.add("rv");
  const head = c.querySelector(".card-head");
  head.appendChild(el("span", "spacer"));
  const all = el("button", "btn sm", "全部模型");
  all.onclick = () => { S.libTab = "rec"; go("lib:" + n.id); };
  head.appendChild(all);
  const top = rec.top.map((r) => rec.items.find((x) => x.repo === r)).filter(Boolean);
  top.forEach((it) => c.appendChild(recRow(n, it, lib, { ...ctx, compact: true })));
  if (!top.length) c.appendChild(el("p", "small muted", "推薦清單裡沒有這台放得下的模型。"));
  return c;
}

// 推薦清單的管理（管理者在網頁上增改刪；大小與架構由代理讀 Hugging Face 檔頭）
async function openCatalog() {
  const body = el("div");
  loading(body);
  const ov = modal({ title: "管理推薦清單", bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide");
  const after = () => { S.rec = {}; };
  const draw = async () => {
    let data;
    try { data = await api("/api/catalog"); } catch (ex) { errNote(body, ex.message); return; }
    body.textContent = "";
    body.appendChild(el("p", "small muted", "這份清單決定每台機器的「推薦模型」列出哪些；順序就是同等級時的排列先後。"
      + "大小、量化、架構與 MoE 比例都由機器從 Hugging Face 的檔頭讀，不必手填。中國來源的模型會自動標出來（政府案不得使用）。"));
    const sb = el("div", "scrollbox cat-box");
    const tb = el("table", "api-table");
    tb.innerHTML = "<thead><tr><th>名稱</th><th>Hugging Face</th><th>用途</th><th>量化</th><th>更新於</th><th></th></tr></thead>";
    const tbody = el("tbody");
    data.items.forEach((e, i) => {
      const tr = el("tr");
      tr.appendChild(el("td", "cat-name", e.title || e.repo));
      tr.appendChild(el("td", "mono small", e.repo));
      tr.appendChild(el("td", "small nowrap", data.uses[e.use] || "—"));
      tr.appendChild(el("td", "small nowrap", e.quants?.length ? `${e.quants.length} 種` : "還沒讀到"));
      tr.appendChild(el("td", "small nowrap", e.refreshed ? new Date(e.refreshed * 1000).toLocaleDateString(uiLocale()) : "—"));
      const td = el("td", "row-acts");
      const mv = (t, step, dis) => {
        const b = el("button", "btn sm", t);
        b.disabled = dis;
        b.onclick = async () => {
          try { await api("/api/catalog/move", { method: "POST", body: { repo: e.repo, step } }); } catch (ex) { toast(ex.message, "bad"); }
          after(); draw();
        };
        return b;
      };
      const ed = el("button", "btn sm", "編輯");
      ed.onclick = () => catalogEditor(e, data.uses, () => { after(); draw(); });
      const del = el("button", "btn sm danger", "移除");
      del.onclick = () => modal({ title: `移除 ${e.title || e.repo}`, bodyNode: el("p", null, "只從推薦清單拿掉；已經下載到各台的模型不受影響。"),
        confirmText: "移除", danger: true, onConfirm: async () => {
          try { await api("/api/catalog/delete", { method: "POST", body: { repo: e.repo } }); toast("已移除", "ok"); } catch (ex) { toast(ex.message, "bad"); }
          after(); draw();
        } });
      td.append(mv("上移", -1, i === 0), mv("下移", 1, i === data.items.length - 1), ed, del);
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
    tb.appendChild(tbody);
    sb.appendChild(tb);
    body.appendChild(sb);
    const row = el("div", "slot-actions");
    const add = el("button", "btn sm primary", "新增模型");
    add.onclick = () => catalogEditor(null, data.uses, () => { after(); draw(); });
    const all = el("button", "btn sm", "全部從 Hugging Face 重新讀取");
    all.onclick = async () => {
      all.disabled = true;
      all.textContent = "讀取中，一顆約 5 秒…";
      try {
        const r = await api("/api/catalog/refresh-all", { method: "POST", body: {} });
        toast(r.errors?.length ? `更新 ${r.updated.length} 顆；${r.errors.join("；")}` : `已更新 ${r.updated.length} 顆`, r.errors?.length ? "bad" : "ok");
      } catch (ex) { toast(ex.message, "bad"); }
      after(); draw();
    };
    row.append(add, el("span", "spacer"), all);
    body.appendChild(row);
  };
  ov.querySelector(".actions .btn").textContent = "關閉";
  ov.querySelector(".actions .btn").addEventListener("click", () => render());
  draw();
}

function catalogEditor(e, uses, done) {
  const body = el("div");
  const f = el("div", "form-row");
  const fRepo = textInput(e?.repo, "例：unsloth/gemma-4-E4B-it-GGUF");
  fRepo.disabled = !!e;
  const fTitle = textInput(e?.title, "畫面上顯示的名稱，例：Gemma 4 E4B");
  const fUse = selectInput(Object.entries(uses), e?.use || "chat");
  const fVendor = textInput(e?.vendor, "例：Google、云碩 xVITA");
  const fTags = textInput((e?.tags || []).join("、"), "例：繁中、看圖（最多 6 個）");
  const fMatch = textInput(e?.match, "選填，例：coder-v2");
  f.append(fieldBox("Hugging Face 名稱", fRepo, "GGUF 的 repo；新增時會讀它的檔頭，約 5 秒"), fieldBox("顯示名稱", fTitle),
    fieldBox("用途", fUse), fieldBox("來源", fVendor), fieldBox("標籤", fTags),
    fieldBox("檔名要含", fMatch, "同一個 repo 放了好幾個不同模型時，只列檔名含這段字的量化；改了要勾下面的重新讀取"));
  body.appendChild(f);
  const fDesc = el("textarea");
  fDesc.value = e?.desc || "";
  fDesc.placeholder = "一句話：這顆擅長什麼、適合誰用（最多 160 字）";
  body.appendChild(fieldBox("說明", fDesc));
  const again = el("label", "check-row");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = !e;
  again.append(cb, document.createTextNode(" 儲存時重新從 Hugging Face 讀大小與架構"));
  if (e) body.appendChild(again);
  const err = el("div");
  body.appendChild(err);
  [fRepo, fTitle, fVendor, fTags, fMatch].forEach((i) => imeGuard(i, () => {}));
  const ov = modal({ title: e ? `編輯 ${e.title || e.repo}` : "新增推薦模型", bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide");
  const b = formButton(ov, "儲存", "primary", async () => {
    b.textContent = "儲存中…";
    try {
      await api("/api/catalog/save", { method: "POST", body: { repo: fRepo.value.trim(), title: fTitle.value.trim(), use: fUse.value,
        vendor: fVendor.value.trim(), tags: fTags.value, desc: fDesc.value.trim(), match: fMatch.value.trim(),
        refresh: !e || cb.checked } });
    } catch (ex) { b.textContent = "儲存"; errNote(err, ex.message); return; }
    ov.remove();
    toast("已儲存", "ok");
    done();
  });
}

/* ================================================================ 畫圖與影片
   參考 LocModel 的「畫圖／影片模型」：先裝 stable-diffusion.cpp 引擎 → 照這台記憶體挑一顆、主體＋文字編碼器＋VAE
   一次下載齊 → 產圖。產圖前一律先照三種記憶體架構試算，不夠就擋；數字全由主控台 recommend.py 與代理算。 */

const SD_KIND = { image: "畫圖", video: "影片" };
const SD_ROLE = { model: "完整模型", diffusion: "擴散模型", clip_l: "CLIP-L", clip_g: "CLIP-G", t5xxl: "T5-XXL",
  llm: "文字編碼器", vae: "VAE", audio_vae: "音訊 VAE", connectors: "嵌入連接器" };
// 畫幅照 LocModel：方形、直式、橫式都有（寬高都是 16 的倍數）
const SD_SIZES = {
  image: [[1024, 1024], [768, 1024], [1024, 768], [720, 1280], [1280, 720], [512, 512], [768, 768]],
  video: [[768, 512], [512, 768], [1024, 576], [576, 1024], [1280, 720], [720, 1280], [512, 320], [320, 512]],
};
function sizeLabel(w, h, kind) {
  // 影片用 480P／720P 加橫式／直式來說（一般人看得懂的說法）
  if (kind === "video") return `${Math.min(w, h)}P ${w > h ? "橫式" : w < h ? "直式" : "方形"}（${w}×${h}）`;
  const g = (a, b) => (b ? g(b, a % b) : a);
  const d = g(w, h);
  const r = { "1:1": "方形", "3:4": "直式", "9:16": "直式", "2:3": "直式", "4:3": "橫式", "16:9": "橫式", "3:2": "橫式", "8:5": "橫式", "5:8": "直式" }[`${w / d}:${h / d}`];
  return `${w}×${h}${r ? `（${w / d}:${h / d} ${r}）` : ""}`;
}

// Hugging Face 令牌存在主控台：受限模型（Llama、部分 Gemma）下載時自動帶上；走鏡像的機器不會送出
function hfTokenRow(fTok) {
  const row = el("div", "slot-actions hf-tok");
  const st = el("span", "small muted", "已存的令牌：讀取中…");
  const save = el("button", "btn sm", "存起來");
  const clear = el("button", "btn sm", "清除已存的");
  row.append(st, el("span", "spacer"), save, clear);
  const show = (set) => { st.textContent = set ? "已存的令牌：已設定（沒填上面那格時自動用它）" : "已存的令牌：沒有"; clear.hidden = !set; };
  api("/api/settings").then((r) => show(r.hf_token_set)).catch(() => { st.textContent = ""; });
  save.onclick = async () => {
    try { await api("/api/settings/hf", { method: "POST", body: { token: fTok.value.trim() } }); } catch (ex) { toast(ex.message, "bad"); return; }
    fTok.value = "";
    show(true);
    toast("已加密存在主控台", "ok");
  };
  clear.onclick = async () => {
    try { await api("/api/settings/hf", { method: "POST", body: { clear: true } }); } catch (ex) { toast(ex.message, "bad"); return; }
    show(false);
  };
  return row;
}

async function getSdRec(nid, fresh) {
  S.sdRec = S.sdRec || {};
  const c = S.sdRec[nid];
  if (!fresh && c && Date.now() - c.at < 30000) return c.data;
  const data = await api(`/api/node/${nid}/sd/recommend`);
  S.sdRec[nid] = { at: Date.now(), data };
  return data;
}

// 畫圖／影片模型分頁，照 LocModel：說明＋「自己組一顆」、還沒裝引擎時一條「先安裝畫圖 / 影片引擎」，
// 下面分「畫圖模型」「影片模型」兩區，每一顆都列（跑不動的也列、寫原因），已下載的直接「去畫圖／去產影片」
async function libSd(body, ok, only, v) {
  const pickN = only ? ok[0] : ok.find((x) => x.n.id === S.recNode) || ok[0];
  if (ok.length > 1 && !only) {
    const sel = el("div", "chip-row");
    sel.appendChild(el("span", "small muted", "這台機器："));
    ok.forEach(({ n }) => {
      const b = el("button", "pill", n.name);
      if (n.id === pickN.n.id) b.setAttribute("aria-current", "true");
      b.onclick = () => { S.recNode = n.id; body.textContent = ""; libSd(body, ok, only, v); };
      sel.appendChild(b);
    });
    body.appendChild(sel);
  }
  const holder = el("div", "stack");
  body.appendChild(holder);
  loading(holder);
  const n = S.fleet.nodes.find((x) => x.id === pickN.n.id) || pickN.n;
  let rec;
  try { rec = await getSdRec(n.id); } catch (ex) { errNote(holder, ex.message); return; }
  if (S.view !== v || !holder.isConnected) return;
  holder.textContent = "";
  const redraw = () => { delete S.sdRec[n.id]; body.textContent = ""; libSd(body, ok, only, v); };
  const toGen = (id, kind) => { S.sdModel = id; go(kind === "video" ? "video" : "image"); };

  const intro = el("div", "sd-intro");
  const txt = el("div", "small muted");
  txt.append(el("div", null, "每顆模型包含主體、文字編碼器和 VAE，一鍵全部下載。照這台的記憶體推薦了合適的量化版本，也可以自己選。"),
    el("div", null, `存放在 ${rec.root || "—"}，剩 ${fmt(rec.free_gib, 0)} GB。`));
  const man = el("button", "btn sm", "自己組一顆");
  man.title = "不在清單上的模型：自己挑主體、文字編碼器、VAE 的檔案";
  man.onclick = () => openSdManual(n);
  intro.append(txt, el("span", "spacer"), man);
  holder.appendChild(intro);

  if (!rec.sd_cli || rec.building) {
    const eng = el("div", "sd-engine rv");
    const t = el("div");
    t.append(el("b", null, rec.building ? "正在安裝畫圖 / 影片引擎…" : "先安裝畫圖 / 影片引擎"),
      el("div", "small muted", "第一次使用要下載 stable-diffusion.cpp（依這台的顯示卡挑版本），畫圖和影片共用，以後不用再裝。"));
    const go1 = el("button", "btn primary", rec.building ? "看進度" : "一鍵安裝");
    go1.onclick = () => openEngineBuild(n.id, "sd", redraw);
    eng.append(t, el("span", "spacer"), go1);
    holder.appendChild(eng);
  }
  [["image", "畫圖模型"], ["video", "影片模型"]].forEach(([kind, label]) => {
    const items = rec.items.filter((x) => x.kind === kind).sort((a, b) => a.order - b.order);
    if (!items.length) return;
    holder.appendChild(el("h3", "sd-sec", label));
    const list = el("div", "rec-list sd-list");
    items.forEach((it) => list.appendChild(sdRow(n, rec, it, redraw, (id) => toGen(id, kind))));
    holder.appendChild(list);
  });
  const own = rec.models.filter((m) => !rec.items.some((it) => it.plans.some((p) => p.installed_id === m.id)));
  if (own.length) holder.appendChild(sdMine(n, { ...rec, models: own }, redraw));
  holder.appendChild(el("p", "small muted", "「要約」＝所有權重加上產圖時的計算緩衝（以清單的預設尺寸計）。擴散模型吃的是算力不是頻寬，速度不估算，"
    + "產過一次後顯示這台的實測。標「中國來源」的模型（含文字編碼器、VAE）政府案不得使用。"));
  reveal(holder);
}

const sdMeasured = (m) => m ? `實測：${m.w}×${m.h}${m.frames > 1 ? `、${m.frames} 格` : ""}、${m.steps} 步 ${fmt(m.secs, 0)} 秒，最高用 ${fmt(m.peak_gib)} GiB` : "";
// 畫圖／影片的等級照 LocModel：流暢／較慢／跑不動，後面一句講原因
const SD_LEVEL = { smooth: ["流暢", "ok"], ok: ["較慢", "warn"], no: ["跑不動", "bad"], unknown: ["待更新", "idle"] };
const SD_WHY = { ok: "部分放記憶體，生成較慢", no: "視訊記憶體和記憶體都不夠" };
const SD_TAG_INFO = new Set(["能改圖", "只能改圖", "圖生影片", "多圖參考", "需要上傳圖片", "首尾幀"]);

function sdRow(n, rec, it, redraw, toGen) {
  const row = el("div", "rec-row");
  const draw = (path) => {
    row.textContent = "";
    const p = it.plans.find((x) => x.path === path);
    const main = el("div", "rec-main");
    const t = el("div", "rec-title");
    t.appendChild(el("b", null, it.title));
    (it.tags || []).forEach((x) => t.appendChild(el("span", "chip " + (x === "畫圖" || x === "影片" ? "brand"
      : x === "帶聲音" ? "ok" : SD_TAG_INFO.has(x) ? "info" : "idle"), x)));
    if (it.cn_origin) t.appendChild(el("span", "chip warn", "中國來源・政府案不可用"));
    if (it.released) t.appendChild(el("span", "small muted", relLabel(it.released)));
    main.appendChild(t);
    main.appendChild(el("div", "small muted", [it.vendor, it.desc].filter(Boolean).join(" · ")));
    row.appendChild(main);
    const q = el("div", "rec-quant");
    if (!it.plans.length) q.appendChild(el("span", "small muted", "還沒從 Hugging Face 讀到檔案"));
    else {
      const sel = el("select");
      it.plans.forEach((x) => {
        const tag = [x.path === it.pick && "推薦", x.ready && "已下載"].filter(Boolean).join("・");
        const o = el("option", null, `${x.quant || "原始"}${tag ? "（" + tag + "）" : ""}`);
        o.value = x.path;
        sel.appendChild(o);
      });
      sel.value = path;
      sel.onchange = () => draw(sel.value);
      q.appendChild(sel);
      if (p) q.appendChild(el("span", "small muted", `共 ${fmt(p.size_gib)} GB`));
    }
    row.appendChild(q);
    const fit = el("div", "rec-fit");
    const lv = p?.level || it.level;
    const [lbl, cls] = SD_LEVEL[lv] || SD_LEVEL.unknown;
    const line = el("div", "rec-fit-line");
    line.appendChild(el("span", "chip " + cls, lbl));
    if (p) {
      const why = el("span", "small muted", p.measured ? sdMeasured(p.measured) : SD_WHY[lv] || p.place);
      why.title = `${p.place}，產的時候要約 ${fmt(p.need_gib, 0)} GiB`;
      line.appendChild(why);
    }
    fit.appendChild(line);
    row.appendChild(fit);
    const act = el("div", "rec-act");
    if (p?.ready) {
      const b = el("button", "btn sm primary", it.kind === "video" ? "去產影片" : "去畫圖");
      b.onclick = () => toGen(p.installed_id);
      act.appendChild(b);
    } else if (it.job) {
      act.appendChild(el("span", "small muted", `下載中 ${it.job.total ? Math.floor((it.job.done / it.job.total) * 100) : 0}%`));
    } else if (p) {
      const b = el("button", "btn sm primary", p.installed_id ? "補齊下載" : p.download_gib ? `下載 ${fmt(p.download_gib)} GB` : "加入（檔案都在了）");
      b.disabled = !p.fits_disk;
      b.title = !p.fits_disk ? "存放區空間不夠（另需保留 10 GiB）" : lv === "no" ? "這台記憶體不夠，下載了也跑不動" : "";
      b.onclick = () => sdDownload(n, it, p);
      act.appendChild(b);
    }
    row.appendChild(act);
  };
  draw(it.installed || it.pick || it.plans[0]?.path);
  return row;
}

function sdDownload(n, it, p) {
  const d = el("div");
  d.appendChild(el("p", null, `把 ${it.title} ${p.quant || ""} 下載到 ${n.name}？共 ${p.parts.length} 個檔、${fmt(p.size_gib)} GiB；`
    + (p.download_gib < p.size_gib ? `這台已經有其中幾個共用檔，實際要下載 ${fmt(p.download_gib)} GiB：` : "全部都要下載：")));
  const ul = el("ul", "small");
  p.parts.forEach((x) => ul.appendChild(el("li", null, `${SD_ROLE[x.role] || x.role}：${x.repo}/${x.path}（${fmt(x.size_gib, 2)} GiB）`)));
  d.appendChild(ul);
  d.appendChild(el("p", "small muted", `${p.place}，產圖時要約 ${fmt(p.need_gib, 0)} GiB。下載在背景進行，中斷了再按一次會接續；完成後在這一列按「去畫圖」或「去產影片」。`));
  modal({ title: "下載畫圖／影片模型", bodyNode: d, confirmText: "開始下載", onConfirm: async () => {
    try {
      const r = await api(`/api/node/${n.id}/sd/download`, { method: "POST", body: { repo: it.repo, path: p.path } });
      toast(r.id ? "已開始下載" : "需要的檔案都已經在這台了，可以直接產圖", "ok");
    } catch (ex) { toast(ex.message, "bad"); return; }
    if (S.sdRec) delete S.sdRec[n.id];
    viewLib();                            // 整頁重畫一次：上方的下載紀錄會出現並自己更新
  } });
}

function sdMine(n, rec, redraw) {
  const c = card("已下載的畫圖與影片模型", rec.models.length ? `${rec.models.length} 顆` : "");
  c.classList.add("rv");
  if (!rec.models.length) { c.appendChild(el("p", "small muted", "還沒有。到「推薦模型」挑一顆下載。")); return c; }
  const tb = el("table", "api-table");
  tb.innerHTML = "<thead><tr><th>名稱</th><th>種類</th><th class='num'>大小</th><th>狀態</th><th>實測</th><th></th></tr></thead>";
  const tbody = el("tbody");
  rec.models.forEach((m) => {
    const tr = el("tr");
    const nm = el("td");
    nm.appendChild(el("div", null, m.title));
    if (m.internal) nm.appendChild(el("span", "chip " + (m.cn_origin ? "bad" : "warn"), m.cn_origin ? "中國來源・只限內部測試" : "內部測試"));
    tr.appendChild(nm);
    tr.appendChild(el("td", "small nowrap", SD_KIND[m.kind]));
    tr.appendChild(el("td", "num", `${fmt(m.size_gib)} GiB`));
    tr.appendChild(el("td", "small", m.job ? `下載中 ${m.job.total ? Math.floor((m.job.done / m.job.total) * 100) : 0}%`
      : m.ready ? "可以用" : `缺：${m.missing.map((r) => SD_ROLE[r] || r).join("、")}（到推薦模型按「補齊下載」）`));
    tr.appendChild(el("td", "small muted", m.measured ? sdMeasured(m.measured) : "還沒產過"));
    const td = el("td", "row-acts");
    const del = el("button", "btn sm danger", "刪除");
    del.onclick = () => modal({ title: `刪除 ${m.title}`, bodyNode: el("p", null, "刪掉這顆獨用的檔（不可復原）；別顆也在用的共用檔（例：同一份文字編碼器）會留著。"),
      confirmText: "刪除", danger: true, onConfirm: async () => {
        try { const r = await api(`/api/node/${n.id}/sd/delete`, { method: "POST", body: { id: m.id } }); toast(`已刪除，空出 ${fmt(r.freed_gib)} GiB`, "ok"); } catch (ex) { toast(ex.message, "bad"); return; }
        redraw();
      } });
    td.appendChild(del);
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
  tb.appendChild(tbody);
  const sb = el("div", "scrollbox cat-box");
  sb.appendChild(tb);
  c.appendChild(sb);
  return c;
}

/* ---- 產生：左邊填，右邊看進度與結果；進度每 2 秒只換掉進度那幾個字與長條 */
function sdGen(n, rec, redraw, toRec, kind) {
  const wrap = el("div", "grid pair sd-gen");
  const ready = rec.models.filter((m) => m.ready && (!kind || m.kind === kind));
  const left = card("產生", rec.sd_cli ? "" : "還沒安裝畫圖引擎");
  left.classList.add("rv");
  const right = card("結果");
  right.classList.add("rv");
  wrap.append(left, right);
  const out = el("div", "sd-out");
  const recent = el("div", "scrollbox sd-recent");
  right.append(out, el("h4", "pick-h", "最近產出"), recent);
  if (!ready.length || !rec.sd_cli) {
    const w = el("div", "note warn");
    w.appendChild(document.createTextNode(!rec.sd_cli ? "這台還沒安裝 stable-diffusion.cpp。" : `這台還沒有下載好的${kind === "video" ? "影片" : kind === "image" ? "畫圖" : "畫圖／影片"}模型。`));
    const b = el("button", "btn sm primary", !rec.sd_cli ? "一鍵安裝" : "看推薦");
    b.onclick = !rec.sd_cli ? () => openEngineBuild(n.id, "sd", redraw) : toRec;
    w.appendChild(b);
    left.appendChild(w);
  }
  const fModel = selectInput(ready.map((m) => [m.id, `${m.title}（${SD_KIND[m.kind]}${m.internal ? "・內部測試" : ""}）`]), S.sdModel);
  const fPrompt = el("textarea");
  fPrompt.placeholder = "想要的畫面，中文英文都可以。例：雪地裡的一隻紅狐狸，清晨的光，照片風格";
  fPrompt.maxLength = 2000;
  const fNeg = el("textarea", "short");
  fNeg.placeholder = "不要出現的內容（選填）。例：blurry, text, watermark";
  fNeg.maxLength = 1000;
  const fSize = el("select");
  const fSteps = numInput(null);
  const fCfg = numInput(null);
  const fSeed = numInput(-1);
  fSeed.min = "-1";
  fSeed.step = "1";
  const fSampler = selectInput(["euler", "euler_a", "heun", "dpm2", "dpm++2s_a", "dpm++2m", "dpm++2mv2", "ipndm", "ipndm_v", "lcm", "ddim_trailing", "tcd"].map((x) => [x, x]));
  const fFrames = selectInput([17, 33, 49, 65, 97, 121].map((x) => [String(x), `${x} 格`]));
  const fFps = numInput(24);
  const fCount = selectInput([1, 2, 3, 4].map((x) => [String(x), `${x} 張`]));
  // 參考圖：畫圖＝在這張上面重繪；影片＝當第一格畫面，接著往下演
  const fInit = el("input");
  fInit.type = "file";
  fInit.accept = "image/png,image/jpeg,image/webp";
  fInit.hidden = true;
  // 縮圖用 Blob 網址顯示（不把讀進來的字串直接塞進 src）；送給代理時才組成 data URL，代理會再看檔頭
  const INIT_TYPES = ["image/png", "image/jpeg", "image/webp"];
  const initState = { b64: null, mime: null, url: null };
  const initPick = el("button", "btn sm", "選一張圖");
  const initClear = el("button", "btn sm", "拿掉");
  const initThumb = el("img", "sd-init-thumb");
  initThumb.alt = "參考圖";
  const setInit = (b64, mime) => {
    const okType = !!b64 && INIT_TYPES.includes(mime);
    if (initState.url && initState.b64 !== b64) { URL.revokeObjectURL(initState.url); initState.url = null; }
    initState.b64 = okType ? b64 : null;
    initState.mime = okType ? mime : null;
    if (okType && !initState.url) {
      initState.url = URL.createObjectURL(new Blob([Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0))], { type: mime }));
    }
    initThumb.hidden = initClear.hidden = !okType;
    if (okType) initThumb.src = initState.url; else initThumb.removeAttribute("src");
    strengthBox.hidden = !okType || model()?.kind !== "image" || model()?.defaults?.image === "ref";
  };
  initPick.onclick = () => fInit.click();
  initClear.onclick = () => setInit(null);
  fInit.onchange = async () => {
    const f = fInit.files[0];
    fInit.value = "";
    if (!f) return;
    if (f.size > 4 * 1024 * 1024) { toast("參考圖最大 4 MB", "bad"); return; }
    if (!INIT_TYPES.includes(f.type)) { toast("參考圖只收 PNG、JPEG、WebP", "bad"); return; }
    setInit(await readB64(f), f.type);
  };
  const initRow = el("div", "sd-init");
  initRow.append(fInit, initThumb, initPick, initClear);
  const fStrength = numInput(0.75);
  fStrength.min = "0.05"; fStrength.max = "1"; fStrength.step = "0.05";
  const strengthBox = fieldBox("重繪強度", fStrength, "0.3 左右只改細節，0.8 以上幾乎重畫");
  const initBox = fieldBox("參考圖（選填）", initRow, "在這張圖上面照提示詞重畫");
  const countBox = fieldBox("張數", fCount, "同一組設定一次產幾張，種子依序加一");
  const fr = el("div", "form-row");
  const framesBox = fieldBox("長度", fFrames, "越長越吃記憶體、產得越久");
  const fpsBox = fieldBox("每秒格數", fFps);
  // 長度用秒數說（LocModel 的說法），格數放括號；每秒格數改了就重算
  const relabelFrames = () => {
    const fps = Math.max(1, Number(fFps.value) || 24);
    [...fFrames.options].forEach((o) => { const v = Number(o.value); o.textContent = `約 ${((v - 1) / fps).toFixed(1)} 秒（${v} 格）`; });
  };
  fFps.oninput = relabelFrames;
  fr.append(fieldBox("畫幅", fSize), countBox, fieldBox("步數", fSteps, "清單的預設通常就是最佳值"), fieldBox("引導強度", fCfg, "FLUX 類設 1"),
    fieldBox("種子", fSeed, "-1＝隨機；同種子同設定會產出同一張"), fieldBox("取樣方法", fSampler), framesBox, fpsBox);
  const admit = el("div", "sd-admit small");
  const err = el("div");
  const go = el("button", "btn primary", "產生");
  const row = el("div", "slot-actions");
  row.append(admit, el("span", "spacer"), go);
  if (ready.length && rec.sd_cli) left.append(fieldBox("模型", fModel), fieldBox("提示詞", fPrompt), fieldBox("不要出現的內容", fNeg), initBox, strengthBox, fr, err, row);

  const model = () => ready.find((m) => m.id === fModel.value);
  const applyDefaults = (m) => {
    if (!m) return;
    const d = m.defaults || {};
    fSize.textContent = "";
    const sizes = SD_SIZES[m.kind].slice();
    if (!sizes.some(([w, h]) => w === d.width && h === d.height)) sizes.unshift([d.width, d.height]);
    sizes.forEach(([w, h]) => { const o = el("option", null, `${sizeLabel(w, h, m.kind)}${w === d.width && h === d.height ? "・預設" : ""}`); o.value = `${w}x${h}`; fSize.appendChild(o); });
    fSize.value = `${d.width}x${d.height}`;
    fSteps.value = d.steps ?? 20;
    fCfg.value = d.cfg ?? 1;
    fSampler.value = d.sampler || "euler";
    // 各家影格規則不同：LTX 8n+1、Wan 4n+1、MiniMax-H3 引擎自己對齊到 17k+5
    const step = d.frame_step || 8;
    const opts = step === 1 ? [39, 56, 73, 107] : step === 4 ? [17, 33, 49, 65, 81, 97] : [17, 33, 49, 65, 97, 121];
    fFrames.textContent = "";
    opts.forEach((x) => { const o = el("option", null, `${x} 格`); o.value = String(x); fFrames.appendChild(o); });
    const fr = String(d.frames || opts[1]);
    if (![...fFrames.options].some((o) => o.value === fr)) { const o = el("option", null, `${fr} 格`); o.value = fr; fFrames.prepend(o); }
    fFrames.value = fr;
    fFps.value = d.fps || 24;
    relabelFrames();
    framesBox.hidden = fpsBox.hidden = m.kind !== "video";
    countBox.hidden = m.kind === "video";
    // 圖片的用法照模型：改圖（Qwen-Image-Edit、FLUX.2 klein）、多圖參考（H3）走參考圖；其他是起始畫面／重繪
    const ref = d.image === "ref", must = !!d.needs_image, opt = must ? "" : "（選填）";
    initBox.querySelector("label.field").textContent = ref ? (m.kind === "video" ? `參考圖${opt}` : `要改的圖${opt}`)
      : m.kind === "video" ? `第一格畫面${opt}` : `參考圖${opt}`;
    const hint = initBox.querySelector(".assume");
    if (hint) {
      hint.textContent = ref ? (m.kind === "video" ? "影片裡會出現這張圖的人物、商品或場景" : "照提示詞改這張圖：換背景、改顏色、改圖裡的文字、加減東西")
        : m.kind === "video" ? "從這張圖開始，照提示詞接著往下演" : "在這張圖上面照提示詞重畫";
      if (must) hint.textContent += "。這顆一定要先選一張圖";
    }
    setInit(initState.b64, initState.mime);
    go.textContent = m.kind === "video" ? "產生影片" : "產生圖片";
  };
  const params = () => {
    const [w, h] = fSize.value.split("x").map(Number);
    const m = model();
    return { model: m.id, prompt: fPrompt.value.trim(), negative: fNeg.value.trim(), width: w, height: h,
      steps: Number(fSteps.value), cfg: Number(fCfg.value), seed: Number(fSeed.value), sampler: fSampler.value,
      ...(m.kind === "video" ? { frames: Number(fFrames.value), fps: Number(fFps.value) } : { count: Number(fCount.value) }),
      ...(initState.b64 ? { init_image: `data:${initState.mime};base64,${initState.b64}`,
        ...(m.kind === "image" && m.defaults?.image !== "ref" ? { strength: Number(fStrength.value) } : {}) } : {}) };
  };
  // 試算：換模型、尺寸、影格數時問代理一次（產的時候還會再算一次，這裡只是先讓人知道夠不夠）
  let admitSeq = 0;
  const checkAdmit = async () => {
    const m = model();
    if (!m) return;
    const p = params();
    const seq = ++admitSeq;
    admit.textContent = "試算記憶體中…";
    admit.className = "sd-admit small muted";
    let r;
    try { r = await api(`/api/node/${n.id}/sd/admit`, { method: "POST", body: { model: p.model, width: p.width, height: p.height, frames: p.frames } }); } catch (ex) { admit.textContent = ex.message; return; }
    if (seq !== admitSeq) return;
    const pool = r.pools.unified || r.pools.vram || r.pools.ram;
    admit.className = "sd-admit small " + (r.allow ? (r.warnings.length ? "warn-t" : "ok-t") : "bad-t");
    admit.textContent = r.allow
      ? `夠：這次要約 ${fmt(r.need_gib, 0)} GiB${r.measured ? "（依實測）" : "（估算）"}，產的時候最壞剩 ${fmt(pool.worst)} GiB（底線 ${fmt(pool.floor, 0)}）${r.warnings.length ? "。" + r.warnings.join("；") : ""}`
      : r.reasons.join("；");
    go.disabled = !r.allow;
  };
  fModel.onchange = () => { S.sdModel = fModel.value; applyDefaults(model()); checkAdmit(); };
  fSize.onchange = fFrames.onchange = checkAdmit;
  go.onclick = async () => {
    errNote(err, "");
    go.disabled = true;
    // 中文提示詞：這台有執行中的對話模型就先翻成英文（多數畫圖、影片模型看英文效果最好），翻好的放回框裡讓人看得到
    if (/[\u3400-\u9fff]/.test(fPrompt.value)) {
      const chat = (n.state?.slots || []).find((x) => (x.kind === "llama" || x.kind === "strata") && x.busy && (x.caps || ["文字"]).includes("文字"));
      if (chat) {
        const zh = fPrompt.value;
        go.textContent = "翻譯提示詞中…";
        try {
          const out = await api(`/api/node/${n.id}/chat/${chat.id}`, { method: "POST", body: { stream: false, max_tokens: 300, temperature: 0.2,
            messages: [{ role: "system", content: "You write prompts for " + (model()?.kind === "video" ? "text-to-video" : "text-to-image")
              + " models. Rewrite the user's request as ONE concise English prompt made of comma-separated descriptive phrases "
              + "(subject, setting, style, lighting, camera). Output only the prompt." }, { role: "user", content: zh }] } });
          const en = String(out.choices?.[0]?.message?.content || "").replace(/^["'「]|["'」]$/g, "").split("\n").find((x) => x.trim()) || "";
          if (en.trim().length >= 3) { fPrompt.value = en.trim(); toast("已把中文提示詞翻成英文（原文：" + zh.slice(0, 40) + "）", "ok"); }
        } catch (_) { /* 翻不了就用原文 */ }
        go.textContent = model()?.kind === "video" ? "產生影片" : "產生圖片";
      } else toast("這台沒有執行中的對話模型，提示詞照中文送出；FLUX、LTX 這類模型看英文效果比較好", "warn");
    }
    let r;
    try { r = await api(`/api/node/${n.id}/sd/generate`, { method: "POST", body: params() }); } catch (ex) {
      errNote(err, ex.data?.admit ? ex.data.admit.reasons.join("；") : ex.message);
      go.disabled = false;
      return;
    }
    watchGen(n, r.id, out, recent, go);
  };
  if (ready.length) { applyDefaults(model()); checkAdmit(); }

  // 「用同樣的設定」：模型換回去、全部參數照這件填回來（尺寸、影格數不在選單裡就補一個選項）
  const pick = (sel, v, label) => {
    if (![...sel.options].some((o) => o.value === v)) { const o = el("option", null, label); o.value = v; sel.prepend(o); }
    sel.value = v;
  };
  const form = { fModel, apply: () => applyDefaults(model()), restore: (j) => {
    fPrompt.value = j.prompt;
    fNeg.value = j.negative || "";
    fSeed.value = j.seed;
    pick(fSize, `${j.width}x${j.height}`, `${j.width}×${j.height}`);
    fSteps.value = j.steps;
    fCfg.value = j.cfg;
    fSampler.value = j.sampler;
    if (j.frames) { pick(fFrames, String(j.frames), `${j.frames} 格`); fFps.value = j.fps; relabelFrames(); }
    fCount.value = String(j.count || 1);
    if (j.strength) fStrength.value = j.strength;
    setInit(null);
    if (j.init) api(`/api/node/${n.id}/sd/jobs/${j.id}/init`).then((r) => setInit(r.b64, r.mime)).catch(() => {});
    checkAdmit();
  } };

  // 右邊：最近產出；有在產的就接著看進度
  const loadRecent = async (showFirst) => {
    let jobs = [];
    try { jobs = (await api(`/api/node/${n.id}/sd/jobs`)).jobs; } catch (ex) { errNote(recent, ex.message); return []; }
    recent.textContent = "";
    if (!jobs.length) recent.appendChild(el("p", "small muted", "還沒有產出。"));
    jobs.forEach((j) => {
      const r = el("button", "sd-job");
      r.appendChild(el("span", "small mono", new Date(j.created * 1000).toLocaleString(uiLocale(), { hour12: false }).slice(5, 16)));
      r.appendChild(el("span", "sd-job-p", j.prompt));
      r.appendChild(el("span", "small " + (j.state === "failed" ? "bad-t" : j.suspect ? "warn-t" : "muted"),
        j.suspect ? "可能壞了" : { running: "產生中", done: `${fmt(j.secs, 0)} 秒`, failed: "失敗", cancelled: "已取消", interrupted: "中斷" }[j.state] || j.state));
      r.onclick = () => (j.state === "running" ? watchGen(n, j.id, out, recent, go) : showJob(n, j, out, recent, form));
      recent.appendChild(r);
    });
    return jobs;
  };
  S.sdReload = loadRecent;
  loadRecent().then((jobs) => {
    const run = jobs.find((j) => j.state === "running");
    if (run) watchGen(n, run.id, out, recent, go);
    else if (jobs[0]) showJob(n, jobs[0], out, recent, form);
    else out.appendChild(el("p", "small muted", "左邊填好按「產生」，進度與結果會出現在這裡。"));
  });
  return wrap;
}

function watchGen(n, jid, out, recent, go) {
  clearTimeout(S.genTimer);
  out.textContent = "";
  const head = el("div", "small");
  const bar = el("div", "bar");
  const seg = el("div", "seg");
  bar.appendChild(seg);
  const meta = el("div", "small muted mono");
  const cancel = el("button", "btn sm", "取消");
  cancel.onclick = async () => { try { await api(`/api/node/${n.id}/sd/jobs/${jid}/cancel`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); } };
  const top = el("div", "slot-actions");
  top.append(head, el("span", "spacer"), cancel);
  out.append(top, bar, meta);
  go.disabled = true;
  const tick = async () => {
    if (!out.isConnected) return;
    let j;
    try { j = await api(`/api/node/${n.id}/sd/jobs/${jid}`); } catch (ex) { meta.textContent = "讀不到進度：" + ex.message; S.genTimer = setTimeout(tick, 3000); return; }
    if (!out.isConnected) return;
    const pct = j.total ? Math.floor((j.step / j.total) * 100) : 0;
    head.textContent = `${j.title}：${j.phase}${j.step ? `　${j.step}/${j.total} 步` : ""}`;
    seg.style.width = pct + "%";
    meta.textContent = `${j.width}×${j.height}${j.frames ? `、${j.frames} 格` : ""}、種子 ${j.seed}${j.s_per_it ? `、每步 ${fmt(j.s_per_it, 2)} 秒` : ""}、已經 ${Math.round(Date.now() / 1000 - j.created)} 秒`
      + (j.offload ? "　權重放系統記憶體（較慢）" : "");
    if (j.state === "running") { S.genTimer = setTimeout(tick, 2000); return; }
    go.disabled = false;
    const jobs = await S.sdReload?.();
    showJob(n, (jobs || []).find((x) => x.id === jid) || j, out, recent);
    if (j.state === "done" && S.sdRec) delete S.sdRec[n.id];       // 實測寫回模型了，推薦列要顯示
  };
  tick();
}

// 刪掉一件產出（失敗、取消、中斷的也要能刪，不然會一直留在最近產出裡）
function sdJobDelete(n, j, out, url) {
  const del = el("button", "btn sm danger", "刪除");
  del.onclick = async () => {
    try { await api(`/api/node/${n.id}/sd/jobs/${j.id}/delete`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); return; }
    [].concat(url || []).forEach((u) => URL.revokeObjectURL(u));
    out.textContent = "";
    S.sdReload?.();
  };
  return del;
}

// 引擎算壞但沒報錯（例：LTX 開 flash attention 全白、聲音滿格）：代理檢查產出後回的問題，每項一行（逐句翻譯）
function sdSuspectNote(probs) {
  const p = el("div", "note warn");
  p.appendChild(el("strong", "", "產出看起來是壞的"));
  [].concat(probs).forEach((t) => p.appendChild(el("div", "", t)));
  p.appendChild(el("div", "small", "這多半是推理引擎在這台算錯，不是提示詞的問題，換描述也不會好；請回報管理者。"));
  return p;
}

async function showJob(n, j, out, recent, form) {
  clearTimeout(S.genTimer);
  out.textContent = "";
  recent.querySelectorAll(".sd-job").forEach((b) => b.removeAttribute("aria-current"));
  const info = el("div", "small muted", `${j.title}・${j.width}×${j.height}${j.frames ? `・${j.frames} 格` : ""}${(j.count || 1) > 1 ? `・${j.count} 張` : ""}`
    + `${j.init ? (j.kind === "video" ? "・從參考圖開始" : `・參考圖重繪 ${j.strength}`) : ""}・${j.steps} 步・引導 ${j.cfg}・種子 ${j.seed}`
    + (j.secs ? `・${fmt(j.secs, 0)} 秒` : "") + (j.peak_gib ? `・最高用 ${fmt(j.peak_gib)} GiB` : ""));
  if (j.state !== "done") {
    out.appendChild(el("p", "note " + (j.state === "failed" ? "bad" : "warn"), j.error || { cancelled: "已取消", interrupted: "代理重啟過，這件沒產完" }[j.state] || j.state));
    out.appendChild(info);
    const acts = el("div", "slot-actions");
    acts.append(el("span", "spacer"), sdJobDelete(n, j, out));
    out.appendChild(acts);
  } else {
    const box = el("div", "sd-media");
    box.appendChild(el("p", "small muted", "讀取中…"));
    if (j.suspect) out.appendChild(sdSuspectNote(j.suspect));
    out.append(box, info);
    // 一次多張：第一張先出來，其他張接著抓；點縮圖換大圖，下載的是目前這張
    const fetchOut = async (i) => {
      const r = await api(`/api/node/${n.id}/sd/jobs/${j.id}/output/${i}`);
      const bin = Uint8Array.from(atob(r.b64), (ch) => ch.charCodeAt(0));
      return { ...r, url: URL.createObjectURL(new Blob([bin], { type: r.mime })) };
    };
    let first;
    try { first = await fetchOut(0); } catch (ex) { box.textContent = ex.message; return; }
    const shots = [first];
    let curShot = first;
    box.textContent = "";
    const media = el(first.mime.startsWith("video") ? "video" : "img");
    media.src = first.url;
    if (media.tagName === "VIDEO") { media.controls = true; media.loop = true; } else media.alt = j.prompt;
    box.appendChild(media);
    if ((first.count || 1) > 1) {
      const strip = el("div", "sd-strip");
      out.insertBefore(strip, info);
      const thumb = (sh, i) => {
        const b = el("button", "sd-thumb");
        b.setAttribute("aria-label", `第 ${i + 1} 張`);
        const im = el("img");
        im.src = sh.url;
        im.alt = "";
        b.appendChild(im);
        b.onclick = () => { media.src = sh.url; curShot = sh; strip.querySelectorAll(".sd-thumb").forEach((x) => x.removeAttribute("aria-current")); b.setAttribute("aria-current", "true"); };
        if (i === 0) b.setAttribute("aria-current", "true");
        strip.appendChild(b);
      };
      thumb(first, 0);
      for (let i = 1; i < first.count; i++) {
        try { const sh = await fetchOut(i); shots.push(sh); thumb(sh, i); } catch (_) { /* 少一張不影響其他張 */ }
      }
    }
    const acts = el("div", "slot-actions");
    const dl = el("button", "btn sm", "下載");
    dl.onclick = () => { const a = el("a"); a.href = curShot.url; a.download = curShot.name; document.body.appendChild(a); a.click(); a.remove(); };
    acts.appendChild(dl);
    if (form) {
      const again = el("button", "btn sm", "用同樣的設定");
      again.onclick = () => {
        if ([...form.fModel.options].some((o) => o.value === j.model)) { form.fModel.value = j.model; S.sdModel = j.model; form.apply(); }
        form.restore(j);
        toast("已帶入這件的全部設定（提示詞、畫幅、張數、步數、引導強度、種子、取樣方法、參考圖）", "ok");
      };
      acts.appendChild(again);
    }
    acts.append(el("span", "spacer"), sdJobDelete(n, j, out, shots.map((x) => x.url)));
    out.appendChild(acts);
  }
  out.appendChild(el("p", "sd-prompt small", j.prompt));
}

/* ---- 清單管理：管理者在網頁上增改刪；檔案與大小由代理讀 Hugging Face 的檔案清單 */
async function openSdCatalog(after) {
  const body = el("div");
  loading(body);
  const ov = modal({ title: "管理畫圖與影片清單", bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide", "xwide");
  const changed = () => { S.sdRec = {}; };
  const draw = async () => {
    let data;
    try { data = await api("/api/sd/catalog"); } catch (ex) { errNote(body, ex.message); return; }
    body.textContent = "";
    body.appendChild(el("p", "small muted", "這份清單決定每台機器「畫圖與影片」列出哪些模型；順序就是同等級時的排列先後。每顆寫明主體、文字編碼器、VAE 各是哪個 repo 的哪個檔，"
      + "大小由機器從 Hugging Face 讀。中國來源的模型不能加入——文字編碼器、VAE 也算。"));
    const sb = el("div", "scrollbox cat-box");
    const tb = el("table", "api-table");
    tb.innerHTML = "<thead><tr><th>名稱</th><th>主體</th><th>種類</th><th>元件</th><th>更新於</th><th></th></tr></thead>";
    const tbody = el("tbody");
    data.items.forEach((e, i) => {
      const tr = el("tr");
      tr.appendChild(el("td", "cat-name", e.title));
      tr.appendChild(el("td", "mono small", e.repo));
      tr.appendChild(el("td", "small nowrap", data.kinds[e.kind]));
      tr.appendChild(el("td", "small", e.parts.map((p) => data.roles[p.role]).join("、")));
      tr.appendChild(el("td", "small nowrap", e.refreshed ? new Date(e.refreshed * 1000).toLocaleDateString(uiLocale()) : "—"));
      const td = el("td", "row-acts");
      const mv = (t, step, dis) => {
        const b = el("button", "btn sm", t);
        b.disabled = dis;
        b.onclick = async () => {
          try { await api("/api/sd/catalog/move", { method: "POST", body: { repo: e.repo, step } }); } catch (ex) { toast(ex.message, "bad"); }
          changed(); draw();
        };
        return b;
      };
      const ed = el("button", "btn sm", "編輯");
      ed.onclick = () => sdCatalogEditor(e, data, () => { changed(); draw(); });
      const del = el("button", "btn sm danger", "移除");
      del.onclick = () => modal({ title: `移除 ${e.title}`, bodyNode: el("p", null, "只從清單拿掉；已經下載到各台的不受影響。"),
        confirmText: "移除", danger: true, onConfirm: async () => {
          try { await api("/api/sd/catalog/delete", { method: "POST", body: { repo: e.repo } }); toast("已移除", "ok"); } catch (ex) { toast(ex.message, "bad"); }
          changed(); draw();
        } });
      td.append(mv("上移", -1, i === 0), mv("下移", 1, i === data.items.length - 1), ed, del);
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
    tb.appendChild(tbody);
    sb.appendChild(tb);
    body.appendChild(sb);
    const row = el("div", "slot-actions");
    const add = el("button", "btn sm primary", "新增模型");
    add.onclick = () => sdCatalogEditor(null, data, () => { changed(); draw(); });
    const all = el("button", "btn sm", "全部從 Hugging Face 重新讀取");
    all.onclick = async () => {
      all.disabled = true;
      all.textContent = "讀取中…";
      try {
        const r = await api("/api/sd/catalog/refresh-all", { method: "POST", body: {} });
        toast(r.errors?.length ? `更新 ${r.updated.length} 顆；${r.errors.join("；")}` : `已更新 ${r.updated.length} 顆`, r.errors?.length ? "bad" : "ok");
      } catch (ex) { toast(ex.message, "bad"); }
      changed(); draw();
    };
    row.append(add, el("span", "spacer"), all);
    body.appendChild(row);
  };
  ov.querySelector(".actions .btn").textContent = "關閉";
  ov.querySelector(".actions .btn").addEventListener("click", () => after?.());
  draw();
}

// 元件列：每列一個檔（種類、repo、檔名或檔名開頭）。allowMatch＝清單用（可寫檔名開頭讓機器挑量化）；手動組裝只收完整檔名
function sdPartsEditor(parts, roles, allowMatch) {
  const box = el("div", "sd-parts" + (allowMatch ? "" : " exact"));
  const rows = [];
  const add = (p = {}) => {
    const r = el("div", "sd-part");
    const fRole = selectInput(Object.entries(roles), p.role || (rows.length ? "vae" : "diffusion"));
    const fRepo = textInput(p.repo, "作者/模型");
    const fHow = selectInput([["path", "檔名"], ["match", "檔名開頭"]], p.match ? "match" : "path");
    fHow.hidden = !allowMatch;
    const fVal = textInput(p.path || p.match, "例：vae/ae.safetensors");
    [fRepo, fVal].forEach((i) => imeGuard(i, () => {}));
    const rm = el("button", "btn sm", "移除");
    const item = { r, read: () => ({ role: fRole.value, repo: fRepo.value.trim(), [fHow.value]: fVal.value.trim() }) };
    rm.onclick = () => { rows.splice(rows.indexOf(item), 1); r.remove(); };
    r.append(fRole, fRepo, fHow, fVal, rm);
    rows.push(item);
    box.insertBefore(r, more);
  };
  const more = el("button", "btn sm", "加一個元件");
  more.onclick = () => add();
  box.appendChild(more);
  (parts.length ? parts : [{}]).forEach(add);
  return { node: box, read: () => rows.map((x) => x.read()) };
}

function sdDefaultsEditor(d, kind, samplers) {
  const f = el("div", "form-row");
  const w = numInput(d.width ?? 1024), h = numInput(d.height ?? 1024), st = numInput(d.steps ?? 20), cfg = numInput(d.cfg ?? 1);
  const sm = selectInput(samplers.map((x) => [x, x]), d.sampler || "euler");
  const fr = numInput(d.frames ?? 33), fps = numInput(d.fps ?? 24);
  const frBox = fieldBox("影格數（影片）", fr), fpsBox = fieldBox("每秒格數（影片）", fps);
  f.append(fieldBox("預設寬", w), fieldBox("預設高", h), fieldBox("步數", st), fieldBox("引導強度", cfg), fieldBox("取樣方法", sm), frBox, fpsBox);
  const setKind = (k) => { frBox.hidden = fpsBox.hidden = k !== "video"; };
  setKind(kind);
  return { node: f, setKind, read: () => ({ width: Number(w.value), height: Number(h.value), steps: Number(st.value), cfg: Number(cfg.value),
    sampler: sm.value, frames: Number(fr.value), fps: Number(fps.value) }) };
}

function sdCatalogEditor(e, meta, done) {
  const body = el("div");
  const f = el("div", "form-row");
  const fRepo = textInput(e?.repo, "主體所在的 repo，例：leejet/FLUX.1-schnell-gguf");
  fRepo.disabled = !!e;
  const fTitle = textInput(e?.title, "例：FLUX.1 schnell");
  const fKind = selectInput(Object.entries(meta.kinds), e?.kind || "image");
  const fVendor = textInput(e?.vendor, "例：Black Forest Labs");
  const fLic = textInput(e?.license, "例：apache-2.0");
  const fTags = textInput((e?.tags || []).join("、"), "例：快速、可商用（最多 6 個）");
  f.append(fieldBox("Hugging Face 名稱", fRepo), fieldBox("顯示名稱", fTitle), fieldBox("種類", fKind), fieldBox("來源", fVendor),
    fieldBox("授權", fLic), fieldBox("標籤", fTags));
  body.appendChild(f);
  const fDesc = el("textarea", "short");
  fDesc.value = e?.desc || "";
  fDesc.placeholder = "一句話：擅長什麼、適合誰用（最多 160 字）";
  body.appendChild(fieldBox("說明", fDesc));
  body.appendChild(el("h4", "pick-h", "元件"));
  body.appendChild(el("p", "small muted", "主體選「擴散模型」或「完整模型」，repo 要跟上面的 Hugging Face 名稱一樣。寫「檔名開頭」時，機器會在開頭相符的 .gguf 裡照記憶體挑量化，其他元件跟著主體的位元數。"));
  const parts = sdPartsEditor(e?.parts || [], meta.roles, true);
  body.appendChild(parts.node);
  body.appendChild(el("h4", "pick-h", "預設參數"));
  const defs = sdDefaultsEditor(e?.defaults || {}, e?.kind || "image", meta.samplers);
  fKind.onchange = () => defs.setKind(fKind.value);
  body.appendChild(defs.node);
  const err = el("div");
  body.appendChild(err);
  [fRepo, fTitle, fVendor, fLic, fTags].forEach((i) => imeGuard(i, () => {}));
  const ov = modal({ title: e ? `編輯 ${e.title}` : "新增畫圖／影片模型", bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide", "xwide");
  const b = formButton(ov, "儲存", "primary", async () => {
    b.textContent = "讀取 Hugging Face 中…";
    try {
      await api("/api/sd/catalog/save", { method: "POST", body: { repo: fRepo.value.trim(), title: fTitle.value.trim(), kind: fKind.value,
        vendor: fVendor.value.trim(), license: fLic.value.trim(), tags: fTags.value, desc: fDesc.value.trim(),
        parts: parts.read(), defaults: defs.read() } });
    } catch (ex) { b.textContent = "儲存"; errNote(err, ex.message); return; }
    ov.remove();
    toast("已儲存", "ok");
    done();
  });
}

// 內部測試：自己組一顆不在清單上的（含中國來源，例 Qwen-Image、Wan），一定要勾「內部測試」，畫面上會一直標著
async function openSdManual(n) {
  let meta;
  try { meta = await api("/api/sd/catalog"); } catch (ex) { toast(ex.message, "bad"); return; }
  const body = el("div");
  body.appendChild(el("p", "note warn", "給內部測試用：不在推薦清單上的模型（含中國來源，例 Qwen-Image、Wan）可以在這裡自己組。政府案不得使用中國來源模型，交付前要刪掉。"));
  const f = el("div", "form-row");
  const fTitle = textInput("", "例：Qwen-Image Q4（測試）");
  const fKind = selectInput(Object.entries(meta.kinds), "image");
  const fTok = el("input");
  fTok.type = "password";
  fTok.autocomplete = "off";
  fTok.placeholder = "需要授權的 repo 才填";
  f.append(fieldBox("顯示名稱", fTitle), fieldBox("種類", fKind), fieldBox("Hugging Face 權杖", fTok, "只在這次下載用，不會存下來"));
  body.appendChild(f);
  body.appendChild(el("h4", "pick-h", "元件（每列一個檔，寫完整的檔名，含子資料夾）"));
  const parts = sdPartsEditor([], meta.roles, false);
  body.appendChild(parts.node);
  body.appendChild(el("h4", "pick-h", "預設參數"));
  const defs = sdDefaultsEditor({}, "image", meta.samplers);
  fKind.onchange = () => defs.setKind(fKind.value);
  body.appendChild(defs.node);
  const ack = el("label", "check-row");
  const cb = el("input");
  cb.type = "checkbox";
  ack.append(cb, document.createTextNode(" 這顆只做內部測試，不會出現在交付給客戶的機器上"));
  body.appendChild(ack);
  const err = el("div");
  body.appendChild(err);
  imeGuard(fTitle, () => {});
  const ov = modal({ title: `手動組一顆（${n.name}）`, bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide", "xwide");
  const b = formButton(ov, "開始下載", "primary", async () => {
    if (!cb.checked) { errNote(err, "要勾「只做內部測試」才能下載"); return; }
    const title = fTitle.value.trim();
    const slug = (title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "model").slice(0, 40);
    b.textContent = "核對檔案中…";
    try {
      const r = await api(`/api/node/${n.id}/sd/download`, { method: "POST", body: { token: fTok.value, manual: {
        id: `test-${slug}-${Math.random().toString(16).slice(2, 6)}`, title, kind: fKind.value, parts: parts.read(), defaults: defs.read() } } });
      toast(r.id ? "已開始下載" : "檔案都在了", "ok");
    } catch (ex) { b.textContent = "開始下載"; errNote(err, ex.message); return; }
    ov.remove();
    if (S.sdRec) delete S.sdRec[n.id];
    S.libTab = "sd";
    S.sdSub = "mine";
    viewLib();
  });
}

async function doAction(node, s, action) {
  const key = node.id + "/" + s.id;
  const verbs = { start: "啟動", stop: "停止", restart: "重啟" };

  // 啟動與重啟前先問准入：這是整套東西最重要的一道
  if (action !== "stop") {
    let verdict;
    try { verdict = await api(`/api/node/${node.id}/admit/${s.id}`, { method: "POST" }); } catch (ex) {
      toast(ex.message, "bad"); return;
    }
    if (!verdict.allow) {
      const body = el("div");
      verdict.reasons.forEach((r) => body.appendChild(el("p", "note bad", r)));
      if (verdict.kv_note) body.appendChild(el("p", "note", verdict.kv_note));
      body.appendChild(el("p", "small muted",
        "強制執行可能讓這台機器記憶體耗盡。統一記憶體的機器一旦吃爆，不是服務掛掉而是整台卡死，要到現場重開。"));
      modal({
        title: `擋下來了：不建議${verbs[action]}「${s.role}」`,
        bodyNode: body, confirmText: "我知道風險，仍要執行", danger: true,
        onConfirm: () => run(node, s, action, true),
      });
      return;
    }
    if (verdict.warnings?.length) toast(verdict.warnings.join("；"), "");
  }

  if (action !== "start" && s.critical) {
    const body = el("div");
    body.appendChild(el("p", "note warn", s.note || "這是標記為關鍵的服務。"));
    body.appendChild(el("p", "small muted", `確定要${verbs[action]}「${s.role}・${s.tier || ""}」？`));
    modal({
      title: `${verbs[action]}關鍵服務`, bodyNode: body,
      confirmText: `確定${verbs[action]}`, danger: true,
      onConfirm: () => run(node, s, action, true),
    });
    return;
  }
  await run(node, s, action, false);
}

async function run(node, s, action, force) {
  const key = node.id + "/" + s.id;
  const verbs = { start: "啟動", stop: "停止", restart: "重啟" };
  S.busy.add(key);
  render();
  try {
    await api(`/api/node/${node.id}/slot/${s.id}/${action}`, { method: "POST", body: { force } });
    toast(`${node.name} 的「${s.role}」已${verbs[action]}`, "ok");
  } catch (ex) {
    toast(`${verbs[action]}失敗：${ex.message}`, "bad");
  } finally {
    S.busy.delete(key);
    await refresh();
  }
}

/* ---------------------------------------------------------------- API 串接
   換完模型之後，IDE（GitHub Copilot、OpenCode、Continue、Cline…）與其他服務要知道四件事：
   打哪個網址、模型名稱、上下文多長、要不要金鑰。這裡讓管理者在網頁上改、看範例、實測。 */

// 哪些欄位算「有 API」：Strata 引擎，或能力標了文字／嵌入且有埠
const isApiSlot = (s) => !!s.unit && !!s.port && (s.kind === "strata" || s.kind === "llama" || (s.caps || []).some((c) => c === "文字" || c === "嵌入"));

// 剛產生的金鑰只放在這個分頁的記憶體裡，重新整理就沒了（後端也只回這一次）
S.newKeys = S.newKeys || {};

function copyText(text) {
  const done = () => toast("已複製", "ok");
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallback());
  } else fallback();
  function fallback() {
    const t = el("textarea");
    t.value = text;
    t.style.cssText = "position:fixed;left:-9999px";
    document.body.appendChild(t);
    t.select();
    try { document.execCommand("copy"); done(); } catch (_) { toast("複製失敗，請手動選取", "bad"); }
    t.remove();
  }
}

function copyRow(k, v, opts = {}) {
  const r = el("div", "copyrow");
  r.appendChild(el("span", "k", k));
  const val = el("code", "v", v ?? "—");
  r.appendChild(val);
  if (v && !opts.noCopy) {
    const b = el("button", "btn sm", "複製");
    b.onclick = () => copyText(v);
    r.appendChild(b);
  } else r.appendChild(el("span"));
  return r;
}

function apiOutTokens(info) {
  if (info.max_tokens) return info.max_tokens;
  return Math.min(16384, Math.floor((info.max_context || 32768) / 4));
}

/* 依目前設定產生各工具的設定範例。金鑰：剛產生的就直接填；設了但看不到就放提示字；沒設就不放。 */
function apiExamples(node, info) {
  const base = info.public_url;
  const name = info.model_name || "（模型名稱）";
  const ctx = info.max_context || 32768;
  const out = apiOutTokens(info);
  const newKey = S.newKeys[node.id + "/" + info.slot];
  const key = newKey || (info.api_key_set ? "<貼上金鑰>" : "");
  const label = `${info.role}（${node.name}）`;
  const pid = ("xconsole-" + node.name.replace(/[^\w]/g, "") + "-" + info.slot).toLowerCase();
  const j = (o) => JSON.stringify(o, null, 2);
  const origin = base.replace(/\/v1\/?$/, "");
  const ex = [];

  ex.push({
    key: "copilot", label: "GitHub Copilot（VS Code）",
    note: "貼進 VS Code 的 settings.json。也可以在 Copilot 對話框的模型選單 → 管理模型 → 新增「OpenAI Compatible」，"
      + "填同樣的網址與模型名稱。網址填到 /v1；若你的版本要求完整路徑，改填 " + base + "/chat/completions。",
    code: `"github.copilot.chat.customOAIModels": ${j({
      [name]: {
        name: label, url: base, toolCalling: info.tools !== false, vision: !!info.vision, thinking: false,
        maxInputTokens: ctx - out, maxOutputTokens: out, requiresAPIKey: !!info.api_key_set,
      },
    })}`,
  });
  ex.push({
    key: "fields", label: "GitHub Copilot 桌面版／其他 IDE",
    note: "只要工具有「OpenAI 相容（OpenAI Compatible）」的自訂提供者，就填這幾欄。",
    code: [
      `提供者類型   OpenAI Compatible`,
      `Base URL     ${base}`,
      `模型名稱     ${name}`,
      `API 金鑰     ${key || "不需要（工具要求必填就隨便填一串）"}`,
      `上下文長度   ${ctx}`,
      `輸出上限     ${out}`,
      `工具呼叫     ${info.tools === false ? "不支援" : "支援（代理模式可用）"}`,
      `看圖         ${info.vision ? "支援" : "不支援"}`,
    ].join("\n"),
  });
  const ocProvider = {
    npm: "@ai-sdk/openai-compatible", name: label,
    options: { baseURL: base, ...(info.api_key_set ? { apiKey: "{env:XCONSOLE_API_KEY}" } : {}) },
    models: { [name]: { name: info.model || name, tool_call: info.tools !== false, limit: { context: ctx, output: out } } },
  };
  ex.push({
    key: "opencode", label: "OpenCode",
    note: "存成專案根目錄的 opencode.json，或 ~/.config/opencode/opencode.json。"
      + (info.api_key_set ? "金鑰放在環境變數 XCONSOLE_API_KEY，不寫進設定檔。" : ""),
    code: j({ $schema: "https://opencode.ai/config.json", provider: { [pid]: ocProvider }, model: `${pid}/${name}` }),
  });
  ex.push({
    key: "continue", label: "Continue",
    note: "加進 ~/.continue/config.yaml 的 models。",
    code: [
      "models:",
      `  - name: ${label}`,
      "    provider: openai",
      `    model: ${name}`,
      `    apiBase: ${base}`,
      ...(key ? [`    apiKey: ${key}`] : []),
      "    roles: [chat, edit, apply]",
      ...(info.tools !== false ? ["    capabilities: [tool_use]"] : []),
      "    defaultCompletionOptions:",
      `      contextLength: ${ctx}`,
      `      maxTokens: ${out}`,
    ].join("\n"),
  });
  ex.push({
    key: "cline", label: "Cline／Roo Code",
    note: "設定 → API Provider 選「OpenAI Compatible」，填這幾欄。",
    code: [
      `Base URL         ${base}`,
      `API Key          ${key || "隨便填一串（伺服器沒設金鑰）"}`,
      `Model ID         ${name}`,
      `Context Window   ${ctx}`,
      `Max Output       ${out}`,
    ].join("\n"),
  });
  if ((info.dialects || []).includes("anthropic")) {
    ex.push({
      key: "claude", label: "Claude Code（Anthropic 介面）",
      note: "這個引擎也講 Anthropic 的 /v1/messages，Claude Code 可以直接接。",
      code: [
        `export ANTHROPIC_BASE_URL=${origin}`,
        `export ANTHROPIC_AUTH_TOKEN=${key || "none"}`,
        `export ANTHROPIC_MODEL=${name}`,
        "claude",
      ].join("\n"),
    });
  }
  ex.push({
    key: "curl", label: "curl",
    note: "在任何一台連得到的電腦上貼上執行，回「連線成功」就通了。",
    code: [
      `curl ${base}/chat/completions \\`,
      `  -H "Content-Type: application/json" \\`,
      ...(key ? [`  -H "Authorization: Bearer ${key}" \\`] : []),
      `  -d '${JSON.stringify({ model: name, messages: [{ role: "user", content: "請只回答：連線成功" }] })}'`,
    ].join("\n"),
  });
  ex.push({
    key: "python", label: "Python",
    note: "用官方 openai 套件（pip install openai）。串到自己的服務時照這樣寫。",
    code: [
      "from openai import OpenAI",
      "",
      `client = OpenAI(base_url="${base}", api_key=${key ? `"${key}"` : '"none"'})`,
      "r = client.chat.completions.create(",
      `    model="${name}",`,
      '    messages=[{"role": "user", "content": "請只回答：連線成功"}],',
      ")",
      "print(r.choices[0].message.content)",
    ].join("\n"),
  });
  return ex;
}

function apiSteps(info) {
  const t = info.last_test;
  const steps = [
    { t: "確認服務在跑", why: "IDE 打過來要有人接；沒在跑就先到機器頁啟動或換模型",
      done: !!info.online, need: "（服務沒有回應）" },
    { t: "確認網址、模型名稱與上下文", why: "這三個要原封不動填進 IDE；上下文不夠，IDE 一輪的提示就送不出去",
      done: !!info.online && !!info.model_name && (info.served || []).some((m) => m.id === info.model_name) },
    { t: "決定要不要金鑰", why: "有金鑰才不會讓內網任何人都能用；開了之後每個工具都要補上金鑰",
      done: !!info.api_key_set, optional: true },
    { t: "實測一次", why: "從這台本機與主控台經網路各打一次，連工具呼叫一起驗，確認 IDE 真的連得上",
      done: !!(t && t.ok) },
  ];
  const firstOpen = steps.findIndex((s) => !s.done && !s.optional);
  steps.forEach((s, i) => {
    if (s.done) s.state = "done";
    else if (s.optional && (firstOpen === -1 || i < firstOpen)) s.state = "skip";
    else if (i === firstOpen) s.state = "now";
    else s.state = "blocked";
  });
  return steps;
}

/* 全部機器的 API 一覽：要串 IDE 或其他服務時，從這裡挑一個 */
function viewApis() {
  const main = $("#main");
  main.textContent = "";
  const wrap = el("div", "wrap");
  main.appendChild(wrap);
  const c = card("API 串接", "換完模型之後，IDE 與其他服務要打的網址、模型名稱與上下文都在這裡");
  const list = [];
  (S.fleet.nodes || []).forEach((n) => (n.state?.slots || []).filter(isApiSlot).forEach((s) => list.push([n, s])));
  if (!list.length) {
    c.appendChild(el("p", "note", "目前沒有提供 API 的模型欄位。先到機器頁啟動或換上一顆文字模型。"));
    wrap.appendChild(c);
    return;
  }
  const sb = el("div", "scrollbox");
  const tb = el("table", "api-table");
  const th = el("thead");
  const hr = el("tr");
  ["機器", "欄位", "模型名稱（填進 IDE）", "網址", "上下文", "金鑰", "上次實測", ""].forEach((h) => hr.appendChild(el("th", null, h)));
  th.appendChild(hr);
  tb.appendChild(th);
  const body = el("tbody");
  list.forEach(([n, s]) => {
    const tr = el("tr");
    const cells = [n.name, `${s.role}・${s.tier || ""}`, "讀取中…", "", int(s.config?.max_context), "", "", ""].map((v) => el("td", null, v));
    cells.forEach((td) => tr.appendChild(td));
    cells[2].className = "mono";
    cells[3].className = "mono";
    const b = el("button", "btn sm primary", "設定與實測");
    b.onclick = () => go(`api:${n.id}:${s.id}`);
    cells[7].textContent = "";
    cells[7].appendChild(b);
    tr.style.cursor = "pointer";
    tr.onclick = (e) => { if (e.target.tagName !== "BUTTON") go(`api:${n.id}:${s.id}`); };
    body.appendChild(tr);
    api(`/api/node/${n.id}/apiinfo/${s.id}`).then((info) => {
      cells[2].textContent = info.model_name || "—";
      cells[3].textContent = info.public_url;
      cells[4].textContent = int(info.max_context);
      cells[5].textContent = info.api_key_set ? "有" : "沒有";
      cells[6].textContent = info.last_test
        ? `${info.last_test.ok ? "通過" : "失敗"}・${new Date(info.last_test.at * 1000).toLocaleString(uiLocale(), { hour12: false })}` : "還沒測";
    }).catch((ex) => { cells[2].textContent = ex.message; });
  });
  tb.appendChild(body);
  sb.appendChild(tb);
  c.appendChild(sb);
  c.appendChild(el("p", "small muted", "網址是 http：這些模型服務在內網直接對外，沒有經過主控台。設了金鑰的話，金鑰會以明文在內網傳輸。"));
  wrap.appendChild(c);
}

async function viewApi() {
  const [, nodeId, slotId] = S.view.split(":");
  const node = S.fleet.nodes.find((x) => x.id === nodeId);
  const main = $("#main");
  main.textContent = "";
  const wrap = el("div", "wrap");
  main.appendChild(wrap);
  if (!node) { go("apis"); return; }
  const slot = (node.state?.slots || []).find((x) => x.id === slotId) || { role: slotId };

  const head = card(`API 串接：${node.name} ${slot.role}・${slot.tier || ""}`,
    [slot.model, slot.quant].filter(Boolean).join(" ") || "");
  wrap.appendChild(head);
  loading(head.appendChild(el("div")));
  let info;
  try {
    info = await api(`/api/node/${nodeId}/apiinfo/${slotId}`);
  } catch (ex) {
    head.lastChild.textContent = "";
    head.lastChild.appendChild(el("p", "note bad", ex.message));
    return;
  }
  if (S.view !== `api:${nodeId}:${slotId}`) return;     // 讀取期間使用者已經切走
  head.lastChild.remove();
  if (S.apiNotice?.[nodeId + "/" + slotId]) {
    head.appendChild(el("p", "note warn", S.apiNotice[nodeId + "/" + slotId]));
    delete S.apiNotice[nodeId + "/" + slotId];
  }
  head.appendChild(renderSteps(apiSteps(info)));

  /* ---- 連線資訊 ｜ 設定 */
  const pair = el("div", "grid pair");
  wrap.appendChild(pair);
  const ci = card("連線資訊", info.online ? "服務有回應" : "服務沒有回應");
  const newKey = S.newKeys[nodeId + "/" + slotId];
  ci.appendChild(copyRow("網址（Base URL）", info.public_url));
  ci.appendChild(copyRow("模型名稱", info.model_name));
  ci.appendChild(copyRow("上下文長度", info.max_context ? String(info.max_context) : null));
  ci.appendChild(copyRow("單次輸出上限", info.max_tokens ? String(info.max_tokens) : `引擎預設（範例填 ${apiOutTokens(info)}）`, { noCopy: !info.max_tokens }));
  ci.appendChild(copyRow("API 金鑰", newKey || (info.api_key_set ? "已設定（只在產生當下顯示一次）" : "沒有設定"), { noCopy: !newKey }));
  ci.appendChild(copyRow("支援的介面", (info.dialects || []).map((d) => ({ openai: "OpenAI", anthropic: "Anthropic", responses: "Responses" }[d])).join("、"), { noCopy: true }));
  ci.appendChild(copyRow("工具呼叫／看圖", `${info.tools === false ? "不支援" : info.tools ? "支援" : "未知"}／${info.vision ? "支援" : "不支援"}`, { noCopy: true }));
  if ((info.served || []).length && !(info.served || []).some((m) => m.id === info.model_name)) {
    ci.appendChild(el("p", "note warn", `服務實際回報的模型名稱是 ${info.served.map((m) => m.id).join("、")}，跟設定檔不一致，IDE 請填實際的那個。`));
  }
  if (!info.online) ci.appendChild(el("p", "note bad", `本機打 :${info.port}/v1/models 沒有回應（${info.status || "連不上"}）。`));
  pair.appendChild(ci);

  const fc = card("設定", info.editable ? "改完會重啟這個模型並用新設定實際打一次，打不通自動換回" : "這個服務的設定檔不由 xConsole 管理，只能改對外網址");
  const form = el("div", "form-row");
  const field = (label, input, hint) => {
    const w = el("div");
    w.appendChild(el("label", null, label));
    w.appendChild(input);
    if (hint) w.appendChild(el("div", "assume", hint));
    form.appendChild(w);
    return input;
  };
  const mk = (type, val, ph) => { const i = el("input"); i.type = type; i.value = val ?? ""; if (ph) i.placeholder = ph; return i; };
  const fUrl = field("對外網址", mk("text", info.public_url_custom ? info.public_url : "", info.default_url),
    "IDE 所在的電腦要打的位址。留空＝這台的 IP 與埠；前面有反向代理或內部網域時改成那個");
  const fName = field("模型名稱", mk("text", info.model_name), "IDE 裡填的就是這個；自己取過的名稱，換模型時會沿用");
  const fCtx = field("上下文長度（token）", mk("number", info.max_context), "調大前會先做記憶體試算，不夠會擋下");
  const fOut = field("單次輸出上限（token）", mk("number", info.max_tokens || "", "留空＝引擎預設"), "要小於上下文長度");
  const fKey = el("select");
  [["keep", info.api_key_set ? "維持現在的金鑰" : "維持不使用金鑰"], ["new", info.api_key_set ? "重新產生金鑰（舊的立即失效）" : "產生金鑰"],
    ...(info.api_key_set ? [["none", "不使用金鑰"]] : [])].forEach(([v, t]) => { const o = el("option", null, t); o.value = v; fKey.appendChild(o); });
  field("API 金鑰", fKey, "金鑰只在產生的當下顯示一次；之後忘了就重新產生");
  [fName, fCtx, fOut].forEach((i) => { if (!info.editable) i.disabled = true; });
  if (!info.editable) fKey.disabled = true;
  fc.appendChild(form);
  const save = el("button", "btn primary", "儲存");
  const acts = el("div", "slot-actions");
  acts.appendChild(save);
  fc.appendChild(acts);
  [fUrl, fName, fCtx, fOut].forEach((i) => imeGuard(i, () => save.click()));
  pair.appendChild(fc);

  save.onclick = () => {
    const body = {};
    const url = fUrl.value.trim();
    if (url !== (info.public_url_custom ? info.public_url : "")) body.public_url = url;
    if (info.editable) {
      if (fName.value.trim() !== (info.model_name || "")) body.model_name = fName.value.trim();
      if (Number(fCtx.value) !== Number(info.max_context)) body.max_context = Number(fCtx.value);
      if ((fOut.value || "") !== String(info.max_tokens || "")) body.max_tokens = fOut.value ? Number(fOut.value) : null;
      if (fKey.value !== "keep") body.key = fKey.value;
    }
    if (!Object.keys(body).length) { toast("沒有變更"); return; }
    const restart = ["model_name", "max_context", "max_tokens", "key"].some((k) => k in body);
    const send = async (force) => {
      save.disabled = true;
      save.textContent = restart ? "套用中，模型重新載入約一到兩分鐘…" : "儲存中…";
      try {
        const r = await api(`/api/node/${nodeId}/apiinfo/${slotId}/settings`, { method: "POST", body: { ...body, force } });
        if (r.api_key) {
          S.newKeys[nodeId + "/" + slotId] = r.api_key;
          showNewKey(r.api_key);
        } else if (body.key === "none") delete S.newKeys[nodeId + "/" + slotId];
        toast(r.message || "已儲存", "ok");
      } catch (ex) {
        const v = ex.data?.admit;
        if (v && !force) {
          const d = el("div");
          v.reasons.forEach((x) => d.appendChild(el("p", "note bad", x)));
          modal({ title: "上下文調大之後記憶體不夠", bodyNode: d, confirmText: "我知道風險，仍要套用", danger: true, onConfirm: () => send(true) });
        } else toast(ex.message, "bad");
      }
      viewApi();
    };
    if (!restart) { send(false); return; }
    const d = el("div");
    d.appendChild(el("p", "small muted", "會改寫這個模型的設定檔（先自動備份）並重啟；重啟期間這個模型的 API 會中斷約一到兩分鐘。"
      + "套用後會用新設定實際打一次，打不通就自動換回原本的設定。"));
    if (slot.critical) d.appendChild(el("p", "note warn", slot.note || "這是標記為關鍵的服務。"));
    if ("model_name" in body) d.appendChild(el("p", "note warn", `模型名稱改成 ${body.model_name} 之後，所有已串接的工具都要改成新名稱。`));
    if (body.key === "new") d.appendChild(el("p", "note warn", info.api_key_set ? "舊金鑰會立即失效，所有已串接的工具都要換上新金鑰。" : "開了金鑰之後，所有已串接的工具都要補上金鑰才連得上。"));
    if (body.key === "none") d.appendChild(el("p", "note warn", "拿掉金鑰之後，內網任何連得到這台的人都能使用這個模型。"));
    modal({ title: "套用並重啟", bodyNode: d, confirmText: "套用並重啟", danger: !!slot.critical, onConfirm: () => send(false) });
  };

  /* ---- 實測 ｜ 工具設定範例 */
  const pair2 = el("div", "grid pair");
  wrap.appendChild(pair2);
  const tc = card("實測", info.last_test ? `上次：${info.last_test.ok ? "通過" : "失敗"}・${new Date(info.last_test.at * 1000).toLocaleString(uiLocale(), { hour12: false })}（台北時間）` : "還沒測過");
  tc.appendChild(el("p", "small muted", "在這台本機打模型清單、一般對話與工具呼叫；再從主控台經網路打對外網址，證明別台電腦連得到。"));
  let fTestKey = null;
  if (info.api_key_set && !newKey) {
    const w = el("div");
    w.appendChild(el("label", "field", "金鑰（選填，只用這一次、不保存；填了會連經網路的那一段一起完整驗）"));
    fTestKey = el("input");
    fTestKey.type = "password";
    fTestKey.autocomplete = "off";
    w.appendChild(fTestKey);
    tc.appendChild(w);
  }
  const res = el("div");
  const tb = el("button", "btn primary", "實測一次");
  if (fTestKey) imeGuard(fTestKey, () => tb.click());
  tb.disabled = !info.online;
  tb.onclick = async () => {
    tb.disabled = true;
    tb.textContent = "測試中…";
    res.textContent = "";
    try {
      const r = await api(`/api/node/${nodeId}/apiinfo/${slotId}/test`, { method: "POST", body: { key: newKey || fTestKey?.value || "" } });
      const rows = [...(r.checks || []), ...(r.network ? [{ name: "從主控台經網路連對外網址", ok: r.network.ok, ms: r.network.ms, detail: r.network.detail }] : [])];
      rows.forEach((c) => {
        const g = el("div", "res-group " + (c.ok ? "ok" : "bad"));
        const h4 = el("h4", null, `${c.ok ? "通過" : "失敗"}　${c.name}`);
        g.appendChild(h4);
        g.appendChild(el("div", "small", `${c.detail || ""}${c.ms ? `（${int(c.ms)} 毫秒）` : ""}`));
        res.appendChild(g);
      });
      toast(r.ok ? "全部通過，可以貼到 IDE 了" : "有項目沒通過，看下面的說明", r.ok ? "ok" : "bad");
    } catch (ex) { res.appendChild(el("p", "note bad", ex.message)); }
    tb.disabled = false;
    tb.textContent = "再測一次";
  };
  const ta = el("div", "slot-actions");
  ta.appendChild(tb);
  tc.appendChild(ta);
  tc.appendChild(res);
  pair2.appendChild(tc);

  const ec = card("工具設定範例", "依上面的設定即時產生");
  const exs = apiExamples(node, info);
  const tabs = el("div", "tabbar");
  const note = el("p", "small muted");
  const pre = el("pre", "code");
  const cp = el("button", "btn sm", "複製這段");
  let cur = exs.find((x) => x.key === S.apiTab) || exs[0];
  const show = (x) => {
    cur = x;
    S.apiTab = x.key;
    [...tabs.children].forEach((b) => b.setAttribute("aria-current", b.dataset.k === x.key ? "true" : "false"));
    note.textContent = x.note;
    pre.textContent = x.code;
  };
  exs.forEach((x) => {
    const b = el("button", null, x.label);
    b.dataset.k = x.key;
    b.onclick = () => show(x);
    tabs.appendChild(b);
  });
  cp.onclick = () => copyText(cur.code);
  ec.appendChild(tabs);
  ec.appendChild(note);
  ec.appendChild(pre);
  const ea = el("div", "slot-actions");
  ea.appendChild(cp);
  ec.appendChild(ea);
  show(cur);
  pair2.appendChild(ec);
}

function showNewKey(k) {
  const d = el("div");
  d.appendChild(el("p", "note warn", "這把金鑰只顯示這一次。關掉之後主控台與節點都不會再顯示它；忘了就重新產生。"));
  const r = copyRow("金鑰", k);
  d.appendChild(r);
  d.appendChild(el("p", "small muted", "在這個分頁重新整理之前，下面的設定範例會直接帶入這把金鑰。"));
  modal({ title: "新的 API 金鑰", bodyNode: d });
}

/* ---------------------------------------------------------------- 畫面 */

function viewOverview() {
  const main = $("#main");
  const nodes = S.fleet.nodes;
  const online = nodes.filter((n) => n.online);
  let slotsOn = 0, slotsTotal = 0, vram = 0, vramTotal = 0;
  online.forEach((n) => {
    const st = n.state;
    slotsOn += st.slots.filter((s) => s.busy).length;
    slotsTotal += st.slots.filter((s) => s.unit).length;
    const p = st.pools.vram || st.pools.unified;
    if (p) { vram += p.used_gib || 0; vramTotal += p.total_gib || 0; }
  });

  const hero = el("section", "hero");
  hero.appendChild(el("div", "hero-glow"));
  const hw = el("div", "wrap");
  hw.appendChild(el("h1", null, "機群模型主控台"));
  hw.appendChild(el("p", null,
    "哪台機器上開了哪個模型、還剩多少記憶體、能不能再開 —— 不用再自己記。"));
  const tiles = el("div", "tiles");
  const tile = (k, v, s) => {
    const t = el("div", "tile");
    t.appendChild(el("div", "k", k));
    t.appendChild(el("div", "v", v));
    if (s) t.appendChild(el("div", "s", s));
    return t;
  };
  tiles.appendChild(tile("機器在線", `${online.length} / ${nodes.length}`,
    nodes.length === online.length ? "全部連得上" : "有機器連不上代理"));
  tiles.appendChild(tile("模型運作中", `${slotsOn} / ${slotsTotal}`, "已登錄的欄位"));
  tiles.appendChild(tile("顯示記憶體使用", `${fmt(vram, 0)} GiB`, `共 ${fmt(vramTotal, 0)} GiB`));
  tiles.appendChild(tile("更新於", new Date(S.fleet.ts * 1000).toLocaleTimeString(uiLocale()), "每 15 秒自動更新"));
  hw.appendChild(tiles);
  hero.appendChild(hw);

  const wrap = el("div", "wrap");
  // 從零到跑起來的三步（LocModel 的開場）：全部完成就收起來，已經設好的機群不佔版面
  const steps = fleetSteps();
  if (steps.some((x) => !x.done)) {
    const sc = el("section", "card rv");
    sc.appendChild(renderSteps(steps));
    wrap.appendChild(sc);
    const b = online.find((n) => n.state.engine?.building);
    if (b) watchBuild(sc, b);
  }
  // 只有一台（單機版交機的情況）：直接在總覽讀懂這台、看推薦
  if (online.length === 1) {
    const n = online[0];
    const cached = S.rec?.[n.id];
    if (cached) {
      wrap.appendChild(hwCard(n, cached.data));
      wrap.appendChild(recTop(n, cached.data, cached.lib, { after: () => { S.libTab = "rec"; go("lib:" + n.id); } }));
    }
    if (!cached || Date.now() - cached.at > 60000) {
      Promise.all([getRec(n.id, true), api(`/api/node/${n.id}/library`).catch(() => null)])
        .then(([, lib]) => { S.rec[n.id].lib = lib; if (S.view === "home") quietRender(); }).catch(() => {});
    }
  }
  for (const [gname, gnodes] of groups()) {
    const card = el("section", "card rv");
    const head = el("div", "card-head");
    head.appendChild(el("h2", null, gname));
    head.appendChild(el("span", "sub", `${gnodes.length} 台`));
    card.appendChild(head);
    const grid = el("div", "grid");
    gnodes.forEach((n) => grid.appendChild(nodeSummary(n)));
    card.appendChild(grid);
    wrap.appendChild(card);
  }

  main.textContent = "";
  main.appendChild(hero);
  main.appendChild(wrap);
  reveal(main);
}

// 記憶體架構沒確認、又跟偵測建議不同：推薦與准入都照錯的架構在算
const mmUnsure = (n) => !n.state.machine.confirmed && n.state.chip?.suggest && n.state.chip.suggest !== n.state.pools.memory_model;

function fleetSteps() {
  const nodes = S.fleet.nodes;
  const on = nodes.filter((n) => n.online);
  const unsure = on.find(mmUnsure);
  const noEng = on.find((n) => n.state.engine && !n.state.engine.llama_server);
  const building = on.find((n) => n.state.engine?.building);
  const running = on.some((n) => n.state.slots.some((s) => s.busy && (s.kind === "llama" || s.kind === "strata")));
  const chipOf = (n) => n.state.chip?.gpu || n.name;
  const steps = [
    { t: "檢測硬體", done: on.length > 0 && !unsure,
      why: unsure ? `${unsure.name} 偵測到 ${chipOf(unsure)}，建議記憶體架構是「${MEM_LABEL[unsure.state.chip.suggest]}」；確認後推薦與試算才準`
        : on.length ? `${on.map(chipOf).slice(0, 3).join("、")}${on.length > 3 ? ` 等 ${on.length} 台` : ""}：顯示卡、記憶體、頻寬與存放區都讀到了`
          : "加一台機器，主控台會讀出它的顯示卡、記憶體、頻寬與磁碟，算出能跑多大的模型",
      go: unsure ? ["確認記憶體架構", () => openMachineSettings(unsure.id)] : on.length ? null : ["加一台機器", openAddMachine] },
    { t: "安裝推理引擎", done: on.length > 0 && !noEng && !building,
      why: building ? `${building.name} 正在安裝 llama.cpp…` : noEng ? `${noEng.name} 還沒有 llama.cpp：依這台的顯示卡自動選加速方式一鍵安裝（macOS、Windows 下載官方預編譯版，Linux 在這台編譯）`
        : "每台都有可用的 llama.cpp",
      go: building ? ["看進度", () => openEngineBuild(building.id)] : noEng ? ["一鍵安裝", () => openEngineBuild(noEng.id)] : null },
    { t: "下載一個模型", done: running,
      why: "照這台的記憶體推薦、標好速度與放不放得下；點一下下載，再換上欄位",
      go: ["看推薦", () => { S.libTab = "rec"; go(on.length === 1 ? "lib:" + on[0].id : "lib"); }] },
  ];
  const first = steps.findIndex((x) => !x.done);
  steps.forEach((x, i) => { x.state = x.done ? "done" : i === first ? "now" : "blocked"; });
  // 第一步沒做完以外，引擎與模型兩步彼此不互卡：模型可以先下載，引擎編好再換上
  if (steps[0].done) steps.forEach((x) => { if (x.state === "blocked") x.state = "now"; });
  return steps;
}

// 引擎編譯中：只更新順序軸第二步裡的那一行進度（cmake 的 [ 42%]），不整頁重畫
function watchBuild(sc, n) {
  clearTimeout(S.buildTimer);
  const stepEl = sc.querySelectorAll(".step")[1];
  if (!stepEl) return;
  const line = el("div", "small mono", "讀取進度…");
  stepEl.appendChild(line);
  const tick = async () => {
    if (!line.isConnected) return;
    try {
      const r = await api(`/api/node/${n.id}/llama/build/progress`);
      const pct = [...(r.log_tail || [])].reverse().map((x) => /\[\s*(\d+)%\]/.exec(x)).find(Boolean);
      line.textContent = r.running ? (pct ? `進行中 ${pct[1]}%` : "準備中…") : "已結束，按「看進度」看結果";
      if (!r.running) { refresh(); return; }
    } catch (ex) { line.textContent = "讀不到進度：" + ex.message; }
    S.buildTimer = setTimeout(tick, 3000);
  };
  tick();
}

function nodeSummary(n) {
  const c = el("div", "slot");
  const top = el("div", "slot-top");
  top.appendChild(el("span", "dot" + (n.online ? " on" : " bad")));
  top.appendChild(el("span", "model", n.name));
  c.appendChild(top);
  c.appendChild(el("div", "quant", n.host));

  if (!n.online) {
    c.appendChild(el("p", "note bad", n.error || "連不上代理"));
  } else {
    const st = n.state;
    const p = st.pools.unified || st.pools.vram;
    const r = st.pools.ram;
    c.appendChild(kvRow(st.pools.memory_model === "unified_single" ? "統一記憶體" : "顯示記憶體",
      `${fmt(p.used_gib)} / ${fmt(p.total_gib)} GiB`));
    if (st.pools.memory_model !== "unified_single") {
      const low = r.free_gib < r.floor_gib + 4;
      const row = kvRow("系統記憶體剩", fmt(r.free_gib) + " GiB");
      if (low) row.querySelector(".v").style.color = "var(--warn)";
      c.appendChild(row);
    }
    const on = st.slots.filter((s) => s.busy).map((s) => s.role);
    c.appendChild(kvRow("運作中", on.length ? on.join("、") : "無"));
    if (n.capacity) c.appendChild(kvRow("能跑多大", n.capacity.max_dense_b ? `流暢 ${n.capacity.max_dense_b}B 以內` : "小模型"));
    if (st.machine.profile) {
      const prof = st.profiles?.[st.machine.profile];
      c.appendChild(kvRow("情境", prof?.name || st.machine.profile));
    }
  }
  const acts = el("div", "slot-actions");
  const b = el("button", "btn sm primary", "進入");
  b.onclick = () => go("node:" + n.id);
  acts.appendChild(b);
  c.appendChild(acts);
  return c;
}

function viewNode() {
  const main = $("#main");
  const n = S.fleet.nodes.find((x) => x.id === S.view.slice(5));
  main.textContent = "";
  if (!n) { go("overview"); return; }

  const wrap = el("div", "wrap");
  const head = el("section", "card");
  const h = el("div", "card-head");
  h.appendChild(el("h2", null, n.name));
  h.appendChild(el("span", "sub", n.host));
  head.appendChild(h);

  if (!n.online) {
    head.appendChild(el("p", "note bad", n.error || "連不上這台的代理"));
    wrap.appendChild(head);
    main.appendChild(wrap);
    return;
  }
  const st = n.state;
  const setBtn = el("button", "btn sm", "這台的設定");
  setBtn.onclick = () => openMachineSettings(n.id);
  const libBtn = el("button", "btn sm", "這台的模型庫");
  libBtn.onclick = () => go("lib:" + n.id);
  h.appendChild(el("span", "spacer"));
  h.append(setBtn, libBtn);
  if (st.machine.note) head.appendChild(el("p", "note", st.machine.note));
  // 新機器從零到能用的順序軸：全部完成就不顯示（已經設好的機器不佔版面）
  const info = S.minfo?.[n.id];
  if (info) {
    const steps = machineSteps(n, info);
    if (steps.some((x) => !x.done && !x.optional)) head.appendChild(renderSteps(steps));
  } else if (!S.minfoAsked?.[n.id]) {
    S.minfoAsked = { ...(S.minfoAsked || {}), [n.id]: true };
    machineInfo(n.id).then(() => { if (S.view === "node:" + n.id) quietRender(); }).catch(() => {});
  }

  // 情境切換
  const profs = Object.entries(st.profiles || {});
  const hasStrata = st.slots.some((x) => x.kind === "strata");
  if (profs.length || hasStrata || st.slots.some((x) => x.unit)) {
    const row = el("div", "slot-actions");
    if (profs.length) row.appendChild(el("span", "small muted", "情境："));
    profs.forEach(([key, p]) => {
      const b = el("button", "btn sm" + (st.machine.profile === key ? " primary" : ""), p.name);
      b.title = p.why || "";
      b.onclick = () => modal({
        title: `切換到「${p.name}」`,
        bodyNode: (() => {
          const d = el("div");
          if (p.why) d.appendChild(el("p", "note", p.why));
          d.appendChild(el("p", "small muted",
            "會改寫設定檔並重啟對應服務（先自動備份，失敗會還原回上一版）。重啟期間這台的模型會短暫中斷。"));
          return d;
        })(),
        confirmText: "套用並重啟", danger: true,
        onConfirm: async () => {
          toast("套用中，模型要重新載入，約一到兩分鐘…");
          try {
            await api(`/api/node/${n.id}/profile/${key}`, { method: "POST", body: { force: true } });
            toast(`已切換到「${p.name}」`, "ok");
          } catch (ex) { toast("切換失敗：" + ex.message, "bad"); }
          await refresh();
        },
      });
      row.appendChild(b);
    });
    const stopAll = el("button", "btn sm danger", "全部停止");
    stopAll.onclick = () => modal({
      title: "停止這台所有模型",
      bodyNode: (() => {
        const d = el("div");
        d.appendChild(el("p", "note bad",
          "會停掉這台上所有已登錄的欄位。標記為關鍵的服務預設會跳過，除非你在下面確認。"));
        return d;
      })(),
      confirmText: "全部停止（跳過關鍵服務）", danger: true,
      onConfirm: async () => {
        try {
          const r = await api(`/api/node/${n.id}/all/stop`, { method: "POST", body: { force: false } });
          const skipped = r.results.filter((x) => x.code === 409).length;
          toast(`已送出；跳過 ${skipped} 個關鍵服務`, "ok");
        } catch (ex) { toast(ex.message, "bad"); }
        await refresh();
      },
    });
    if (hasStrata) {
      // 情境只調 Strata 欄位的參數（上下文、並行數、自動縮輸出上限），沒有 Strata 欄位的機器不顯示
      const mp = el("button", "btn sm", profs.length ? "管理情境" : "新增情境");
      mp.title = "一組參數一鍵切換，例如寫程式要長上下文、抓料要多並行";
      mp.onclick = () => openProfiles(n);
      row.appendChild(mp);
    }
    row.appendChild(el("span", "spacer"));
    if (st.slots.some((x) => x.unit)) row.appendChild(stopAll);
    head.appendChild(row);
  }
  wrap.appendChild(head);

  // 角色欄位
  const slotsCard = el("section", "card");
  const sh = el("div", "card-head");
  sh.appendChild(el("h2", null, "角色欄位"));
  sh.appendChild(el("span", "sub",
    `${st.slots.filter((s) => s.busy).length} 個運作中，共 ${st.slots.length} 格`));
  const addSlot = el("button", "btn sm", "新增欄位");
  addSlot.onclick = () => openSlotEditor(n, null);
  sh.appendChild(addSlot);
  slotsCard.appendChild(sh);
  if (!st.slots.length) slotsCard.appendChild(el("p", "note", "這台還沒有欄位。按「新增欄位」建一格空欄位，再到模型庫挑模型放進來。"));
  const grid = el("div", "grid slots");
  const unified = st.pools.memory_model === "unified_single";
  st.slots.filter((s) => s.unit).forEach((s) => grid.appendChild(slotCard(n, s, unified)));
  slotsCard.appendChild(grid);
  // 空欄位壓成一排矮卡：要讓人看出還缺什麼，但不該佔掉整頁高度
  const empties = st.slots.filter((s) => !s.unit);
  if (empties.length) {
    slotsCard.appendChild(el("p", "small muted", `還空著 ${empties.length} 格`));
    const eg = el("div", "empties");
    empties.forEach((s) => eg.appendChild(slotCard(n, s, unified)));
    slotsCard.appendChild(eg);
  }
  wrap.appendChild(slotsCard);

  // 記憶體
  const memCard = el("section", "card rv");  // 與硬體並排
  const mh = el("div", "card-head");
  mh.appendChild(el("h2", null, "記憶體"));
  const nCards = (st.pools.gpus || []).length;
  mh.appendChild(el("span", "sub", st.pools.memory_model !== "discrete" ? (st.pools.model_note || "")
    : nCards ? `${nCards} 張卡，顯示記憶體與系統記憶體各一塊` : "沒有偵測到 GPU，只算系統記憶體"));
  memCard.appendChild(mh);
  const cards = st.pools.gpus || [];
  if (st.pools.memory_model === "unified_single") {
    memCard.appendChild(poolBar(st.pools.unified, st.slots, "統一記憶體"));
  } else {
    // 沒有 GPU 的機器不畫顯存條（總量 0 會被畫成一條 1 GiB 的假長條）
    if (cards.length) memCard.appendChild(poolBar(st.pools.vram, st.slots, cards.length > 1 ? `顯示記憶體（${cards.length} 張卡合計）` : "顯示記憶體"));
    // 多卡時每張卡一條：總量夠不代表每張都夠，llama.cpp 把模型按層拆到各張卡上，先滿的那張會先出事
    if (cards.length > 1) {
      const per = el("div", "gpu-list");
      cards.forEach((g) => {
        const r = el("div", "gpu-row");
        const on = st.slots.filter((x) => x.busy && (x.gpu_ids || []).map(String).includes(String(g.id))).map((x) => x.role);
        r.appendChild(el("span", "small", `卡 ${g.id}${g.name ? "　" + g.name.replace(/^NVIDIA /, "") : ""}${on.length ? "・" + on.join("、") : ""}`));
        const bar = el("div", "bar thin");
        const seg = el("div", "seg");
        seg.style.width = (g.total_gib ? Math.min(100, g.used_gib / g.total_gib * 100) : 0) + "%";
        bar.appendChild(seg);
        r.appendChild(bar);
        r.appendChild(el("span", "mono small", `${fmt(g.used_gib)} / ${fmt(g.total_gib)} GiB`));
        per.appendChild(r);
      });
      memCard.appendChild(per);
    }
    memCard.appendChild(poolBar(st.pools.ram, st.slots, "系統記憶體"));
  }
  const sw = st.pools.swap;
  if (sw?.total_gib) {
    memCard.appendChild(el("p", "small muted",
      `置換空間：已用 ${fmt(sw.used_gib)} / ${fmt(sw.total_gib)} GiB`));
  }
  const ramPool = st.pools.ram;
  if (ramPool && ramPool.free_gib < ramPool.floor_gib) {
    memCard.appendChild(el("p", "note bad",
      `系統記憶體只剩 ${fmt(ramPool.free_gib)} GiB，已低於 ${ramPool.floor_gib} GiB 的底線。` +
      (st.pools.memory_model === "discrete" ? "再載東西進去，系統會開始用置換空間，整台變慢。"
        : "這台是統一記憶體機，再載東西進去有機會把整台拖死。")));
  }
  // 硬體與溫度
  const hwCard = el("section", "card rv d1");
  const hh = el("div", "card-head");
  hh.appendChild(el("h2", null, "硬體"));
  const src = st.hw_source === "self" ? "本代理直接量測" : "機群監控代理";
  hh.appendChild(el("span", "sub", "資料來源：" + src));
  hwCard.appendChild(hh);
  const hgrid = el("div", "hw-grid");
  const box = (title, rows) => {
    const d = el("div");
    d.appendChild(el("div", "role", title));
    rows.forEach(([k, v]) => d.appendChild(kvRow(k, v)));
    return d;
  };
  hgrid.appendChild(box("GPU", [
    ["使用率", st.gpu.use === null || st.gpu.use === undefined ? "—" : fmt(st.gpu.use, 0) + " %"],
    ["溫度", st.gpu.temp ? fmt(st.gpu.temp, 0) + " °C" : "—"],
    ["耗電", st.gpu.power ? fmt(st.gpu.power, 0) + " W" : "—"],
    ["時脈", st.gpu.clock ? `${int(st.gpu.clock.current_mhz)} / ${int(st.gpu.clock.max_mhz)} MHz` : "—"],
  ]));
  hgrid.appendChild(box("CPU 與系統", [
    ["負載", fmt(st.cpu.load, 2)],
    ["核心", int(st.cpu.cores)],
    ["已開機", st.uptime_sec ? fmt(st.uptime_sec / 86400, 1) + " 天" : "—"],
  ]));
  const t = (st.temps || []).slice(0, 5).map((x) => [x.label || x.source, fmt(x.celsius, 0) + " °C"]);
  if (t.length) hgrid.appendChild(box("溫度", t));
  const d = (st.disks || [])[0];
  if (d) {
    hgrid.appendChild(box("磁碟", [
      ["掛載", d.mount],
      ["已用", `${fmt(d.used / 1073741824, 0)} / ${fmt(d.total / 1073741824, 0)} GiB`],
      ["使用率", fmt(d.pct, 0) + " %"],
    ]));
  }
  hwCard.appendChild(hgrid);
  const twoCol = el("div", "grid pair");
  twoCol.appendChild(memCard);
  twoCol.appendChild(hwCard);
  wrap.appendChild(twoCol);

  // 埠口（固定高度在區塊內捲，不讓頁面一路變長）
  const portCard = el("section", "card rv d2");
  const ph = el("div", "card-head");
  ph.appendChild(el("h2", null, "監控中的埠"));
  ph.appendChild(el("span", "sub", `共 ${st.ports.length} 個，對外 ${st.ports.filter((x) => x.external).length} 個`));
  portCard.appendChild(ph);
  const sb = el("div", "scrollbox");
  const tb = el("table");
  tb.innerHTML = "<thead><tr><th class='num'>埠</th><th>類別</th><th>是什麼</th><th>範圍</th></tr></thead>";
  const body = el("tbody");
  st.ports.forEach((p) => {
    const tr = el("tr");
    tr.appendChild(Object.assign(el("td", "num mono"), { textContent: p.port }));
    tr.appendChild(el("td", null, p.kind));
    tr.appendChild(el("td", null, p.label));
    tr.appendChild(el("td", null, p.external ? "對外" : "僅本機"));
    body.appendChild(tr);
  });
  tb.appendChild(body);
  sb.appendChild(tb);
  portCard.appendChild(sb);
  wrap.appendChild(portCard);

  main.appendChild(wrap);
  reveal(main);
}

function viewLogs() {
  const main = $("#main");
  main.textContent = "";
  const wrap = el("div", "wrap");
  const card = el("section", "card");
  const h = el("div", "card-head");
  h.appendChild(el("h2", null, "記錄"));
  card.appendChild(h);

  const picker = el("div", "slot-actions");
  const nodeSel = el("select");
  S.fleet.nodes.filter((n) => n.online).forEach((n) => {
    const o = el("option", null, n.name);
    o.value = n.id;
    nodeSel.appendChild(o);
  });
  // 帶進來的機器或服務已離線、不在這台：退回第一個選項，不要把「選不到」當成「沒有」
  const pick = (sel, want) => { sel.value = want || ""; if (sel.selectedIndex < 0) sel.selectedIndex = 0; };
  pick(nodeSel, S.sub?.node);
  const slotSel = el("select");
  const fillSlots = () => {
    slotSel.textContent = "";
    const n = S.fleet.nodes.find((x) => x.id === nodeSel.value);
    (n?.state.slots || []).filter((s) => s.unit).forEach((s) => {
      const o = el("option", null, `${s.role}・${s.tier || ""}（${s.unit}）`);
      o.value = s.id;
      slotSel.appendChild(o);
    });
    if (!slotSel.options.length) slotSel.appendChild(el("option", null, "這台沒有模型服務")).value = "";
    pick(slotSel, S.sub?.slot);
  };
  fillSlots();
  nodeSel.onchange = () => { fillSlots(); load(); };
  slotSel.onchange = () => load();
  const nw = el("div"); nw.style.flex = "1"; nw.appendChild(nodeSel);
  const sw2 = el("div"); sw2.style.flex = "2"; sw2.appendChild(slotSel);
  picker.appendChild(nw);
  picker.appendChild(sw2);
  const rb = el("button", "btn sm", "重新讀取");
  picker.appendChild(rb);
  card.appendChild(picker);

  const pre = el("pre", "log", "讀取中…");
  const none = el("div", "note");          // 沒有服務可看時取代黑底記錄框，不去讀一個空的路徑
  card.append(pre, none);
  const load = async () => {
    const empty = !nodeSel.value || !slotSel.value;
    pre.hidden = empty;
    none.hidden = !empty;
    slotSel.disabled = !slotSel.options[0]?.value;
    if (empty) {
      none.textContent = "";
      if (!nodeSel.value) { none.append("目前沒有連得上的機器，沒有記錄可看。"); return; }
      none.append("這台還沒有模型服務，沒有記錄可看。到模型庫下載模型並按「換上」，服務的記錄就會出現在這裡。　");
      const b = el("button", "btn sm primary", "到模型庫");
      b.onclick = () => go("lib:" + nodeSel.value);
      none.appendChild(b);
      return;
    }
    pre.textContent = "讀取中…";
    try {
      const d = await api(`/api/node/${nodeSel.value}/logs/${slotSel.value}`);
      pre.textContent = d.text || "（沒有內容）";
      pre.scrollTop = pre.scrollHeight;
    } catch (ex) { pre.textContent = "讀不到：" + ex.message; }
  };
  rb.onclick = load;
  load();

  wrap.appendChild(card);
  main.appendChild(wrap);
}

/* ================================================================ 設定頁（五個分頁）
   分頁順序照管理者實際的先後：先確認機器都連得上 → 處理憑證 → 看使用報告 → 帳號；
   品牌只在交機時設一次，放最後。 */

// 使用者貼的 Hugging Face 網址或名稱 → 「作者/模型」；不是這兩種回 null
function hfRepoOf(text) {
  const t = String(text || "").trim();
  const u = t.match(/^(?:https?:\/\/)?(?:www\.)?(?:huggingface\.co|hf\.co)\/([A-Za-z0-9][\w.-]{0,95})\/([A-Za-z0-9][\w.-]{0,95})/);
  if (u) return `${u[1]}/${u[2]}`;
  return /^[A-Za-z0-9][\w.-]{0,95}\/[A-Za-z0-9][\w.-]{0,95}$/.test(t) ? t : null;
}

const SETTINGS_TABS = [
  ["machines", "機器"], ["groups", "群組"], ["engine", "推理引擎"], ["providers", "第三方 API"], ["tls", "HTTPS 與憑證"], ["report", "使用報告"],
  ["logs", "記錄"], ["account", "帳號與安全"], ["update", "版本與更新"], ["brand", "品牌與外觀"],
];
// 桌面版（一般人單機用）：只留會用到的，照先後排；機群、使用報告、品牌是管理一群機器才用的。
// HTTPS 與憑證只在打開區網存取（別台電腦要連進來）時才有意義
const DESKTOP_TABS = ["account", "engine", "update", "providers", "logs"];

function settingsTabs() {
  if (!S.appWindow) return SETTINGS_TABS;
  const keys = DESKTOP_TABS.concat(S.lanOn ? ["tls"] : []);
  return keys.map((k) => SETTINGS_TABS.find(([key]) => key === k));
}

function viewSettings() {
  const tabs = settingsTabs();
  const tab = tabs.some(([k]) => k === S.settingsTab) ? S.settingsTab : tabs[0][0];
  if (tab === "logs") return viewLogs();
  const main = $("#main");
  main.textContent = "";
  const wrap = el("div", "wrap");
  main.appendChild(wrap);
  ({ machines: settingsMachines, groups: settingsGroups, engine: settingsEngine, providers: settingsProviders, tls: settingsTls, report: settingsReport,
     account: settingsAccount, update: settingsUpdate, brand: settingsBrand }[tab] || settingsMachines)(wrap);
}

/* ---------------------------------------------------------------- 版本與更新
   主控台：檢查 GitHub 上的新版 → 一鍵更新（核對 sha256、先備份、失敗自動還原）→ 主控台重新啟動後自動重新整理。
   不能連外的機器：在別台下載安裝包，這裡手動上傳並貼上 sha256。可以退回上一版。
   各機代理：跟主控台隨附的那一份比，不一樣就可以在這裡推過去。 */
async function settingsUpdate(wrap) {
  const v = S.view;
  const c = card("版本與更新", "主控台與各機代理的版本；更新前一律核對 sha256 並先備份");
  wrap.appendChild(c);
  const body = el("div");
  c.appendChild(body);
  loading(body);
  let ov;
  try { ov = await api("/api/update"); } catch (ex) { errNote(body, ex.message); return; }
  if (S.view !== v) return;
  body.textContent = "";
  body.appendChild(el("div", "tiles light", null));
  const tiles = body.lastChild;
  tiles.append(tileOf("主控台版本", ov.current || "未知", `來源：github.com/${ov.repo}`),
    tileOf("各機代理", `${ov.nodes.filter((n) => n.current).length}/${ov.nodes.length} 台是最新`, "跟主控台隨附的那一份比"));
  const jobBox = el("div");
  body.appendChild(jobBox);
  const drawJob = (job) => {
    jobBox.textContent = "";
    if (!job?.state) return;
    const msg = { checking: "檢查更新包…", downloading: `下載中…${job.got ? `（${fmt(job.got / 1048576, 1)} MB）` : ""}`, installing: "安裝相依套件…",
      applying: "備份並換上新版…", restarting: `換好了，主控台重新啟動中（備份 ${job.backup || ""}）…`, failed: `更新沒有成功：${job.error}` }[job.state] || job.state;
    jobBox.appendChild(el("p", "note " + (job.state === "failed" ? "bad" : "warn"), msg));
  };
  drawJob(ov.job);
  const watchJob = async () => {
    for (;;) {
      await new Promise((ok) => setTimeout(ok, 2000));
      let r;
      try { r = await api("/api/update"); } catch (_) { r = null; }
      if (!r) { jobBox.textContent = ""; jobBox.appendChild(el("p", "note warn", "主控台重新啟動中，等它回來…")); continue; }
      if (r.job?.state === "restarting") { drawJob(r.job); continue; }
      if (r.current !== ov.current || (!r.job?.state && ov.job?.state === "restarting")) { toast(`已更新到 ${r.current}，重新載入頁面`, "ok"); setTimeout(() => location.reload(), 1200); return; }
      drawJob(r.job);
      if (!["checking", "downloading", "installing", "applying"].includes(r.job?.state)) return;
    }
  };

  // ---- 檢查新版
  body.appendChild(el("h4", "pick-h", "主控台"));
  const chk = el("div");
  const chkBtn = el("button", "btn primary", "檢查新版");
  body.append(chkBtn, chk);
  chkBtn.onclick = async () => {
    chk.textContent = "";
    loading(chk);
    let r;
    try { r = await api("/api/update/check", { method: "POST", body: {} }); } catch (ex) { chk.textContent = ""; errNote(chk, ex.message); return; }
    chk.textContent = "";
    chk.appendChild(el("p", "note " + (r.newer ? "warn" : ""), r.newer ? `有新版 ${r.latest}（目前 ${r.current}）${r.published ? "，" + new Date(r.published).toLocaleDateString(uiLocale()) + " 發布" : ""}` : `目前就是最新版（${r.current}）`));
    if (r.notes) { const pre = el("pre", "code-pre small upd-notes"); pre.textContent = r.notes; chk.appendChild(pre); }
    if (r.newer && r.asset) {
      chk.appendChild(el("p", "small muted", `安裝包 ${r.asset.name}（${fmt((r.asset.size || 0) / 1048576, 1)} MB），sha256 ${r.asset.sha256 ? r.asset.sha256.slice(0, 16) + "…" : "GitHub 沒有提供，不能自動更新"}`));
      const go = el("button", "btn primary", `更新到 ${r.latest}`);
      go.disabled = !r.asset.sha256;
      go.onclick = () => modal({ title: `更新到 ${r.latest}`, bodyNode: el("p", null, "會先備份目前的檔，換上新版後主控台重新啟動（大約十幾秒，頁面會自動重新載入）。正在跑的模型不受影響。"),
        confirmText: "開始更新", onConfirm: async () => {
          try { await api("/api/update/apply", { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); return; }
          drawJob({ state: "checking" });
          watchJob();
        } });
      chk.appendChild(go);
    }
  };

  // ---- 手動上傳
  const up = el("details", "upd-manual");
  up.appendChild(el("summary", null, "不能連外：手動上傳更新包"));
  const fFile = el("input");
  fFile.type = "file";
  fFile.accept = ".gz,.tgz,application/gzip";
  const fSha = textInput("", "從 GitHub 版本頁抄 sha256（64 碼）");
  fSha.maxLength = 64;
  const upBtn = el("button", "btn sm primary", "上傳並更新");
  const upErr = el("div");
  up.append(el("p", "small muted", `在能上網的電腦到 github.com/${ov.repo}/releases 下載 xconsole-版本.tar.gz，並抄下它的 sha256；兩樣都對了才會更新。`),
    fieldBox("更新包", fFile), fieldBox("sha256", fSha), upBtn, upErr);
  body.appendChild(up);
  upBtn.onclick = async () => {
    errNote(upErr, "");
    const f = fFile.files[0];
    if (!f) { errNote(upErr, "先選更新包"); return; }
    if (!/^[0-9a-f]{64}$/i.test(fSha.value.trim())) { errNote(upErr, "sha256 要是 64 碼的十六進位"); return; }
    if (f.size > 64 * 1024 * 1024) { errNote(upErr, "更新包要在 64 MB 以內"); return; }
    upBtn.disabled = true;
    try {
      const res = await fetch(`/api/update/upload?sha256=${fSha.value.trim().toLowerCase()}`, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/octet-stream", "X-XConsole-Upload": "1" }, body: f });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || `伺服器回 ${res.status}`);
    } catch (ex) { errNote(upErr, ex.message); upBtn.disabled = false; return; }
    drawJob({ state: "checking" });
    watchJob();
  };

  // ---- 退回上一版
  if (ov.backups?.length) {
    const b = ov.backups[0];
    const rb = el("button", "btn sm", `退回上一版（${b.version}，${new Date(b.at * 1000).toLocaleString(uiLocale(), { hour12: false })} 備份）`);
    rb.onclick = () => modal({ title: "退回上一版", bodyNode: el("p", null, `把主控台換回 ${b.version}，然後重新啟動。各機代理不會跟著退，需要的話在下面個別處理。`),
      confirmText: "退回", danger: true, onConfirm: async () => {
        try { await api("/api/update/rollback", { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); return; }
        drawJob({ state: "restarting" });
        watchJob();
      } });
    body.appendChild(rb);
  }

  // ---- 各機代理
  body.appendChild(el("h4", "pick-h", "各機代理"));
  const tb = el("table", "api-table");
  tb.innerHTML = "<thead><tr><th>機器</th><th>狀態</th><th></th></tr></thead>";
  const tbody = el("tbody");
  ov.nodes.forEach((n) => {
    const tr = el("tr");
    tr.appendChild(el("td", null, n.name));
    const stTd = el("td", "small " + (n.current ? "ok-t" : "warn-t"), !n.online ? (n.old_agent ? "代理太舊（沒有線上更新），請用安裝程式或部署腳本更新一次" : "連不上")
      : n.current ? "最新" : `跟主控台隨附的不同（${(n.sha256 || "").slice(0, 8)}）`);
    tr.appendChild(stTd);
    const td = el("td", "row-acts");
    if (n.online && !n.current) {
      const b = el("button", "btn sm primary", "更新這台代理");
      b.onclick = async () => {
        b.disabled = true;
        try { await api(`/api/update/agent/${n.id}`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); b.disabled = false; return; }
        stTd.textContent = "已送出新版，代理重新啟動中…";
        // 只更新這一列：等代理回來再問一次它的版本
        for (let i = 0; i < 20; i++) {
          await new Promise((ok) => setTimeout(ok, 3000));
          let r;
          try { r = await api("/api/update"); } catch (_) { continue; }
          const me = r.nodes.find((x) => x.id === n.id);
          if (me?.online) {
            stTd.textContent = me.current ? "最新" : "已重新啟動，但版本還是不同";
            stTd.className = "small " + (me.current ? "ok-t" : "warn-t");
            b.remove();
            return;
          }
        }
        stTd.textContent = "代理一分鐘內沒有回來，請到那台看服務狀態";
      };
      td.appendChild(b);
    }
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
  tb.appendChild(tbody);
  body.appendChild(tb);
  if (["checking", "downloading", "installing", "applying", "restarting"].includes(ov.job?.state)) watchJob();
}

function card(title, sub) {
  const c = el("section", "card");
  const h = el("div", "card-head");
  h.appendChild(el("h2", null, title));
  if (sub) h.appendChild(el("span", "sub", sub));
  c.appendChild(h);
  return c;
}

function readB64(file) {
  return new Promise((ok, bad) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result).split(",")[1] || "");
    r.onerror = () => bad(new Error(`讀不到 ${file.name}`));
    r.readAsDataURL(file);
  });
}

function download(name, text, type) {
  const a = el("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

function loading(node) {
  node.textContent = "";
  node.appendChild(el("p", "small muted", "讀取中…"));
}

/* 頂部到期提醒：剩 7 天內或已過期，每一頁都看得到 */
async function checkTlsBanner() {
  const b = $("#tls-banner");
  try {
    const me = await api("/api/me");
    S.appWindow = !!me.app_window;
    S.lanOn = !!me.lan_access;
    const lo = $("#logout");
    if (lo) lo.hidden = S.appWindow;           // 應用程式的視窗不用登入，也就不放登出（別台電腦連進來的照樣有）
    const t = me.tls || {};
    if (t.level === "urgent" || t.level === "expired") {
      b.textContent = t.daysLeft < 0
        ? `主控台憑證已在 ${t.validToTaipei}（台北時間）過期，使用者連線會看到錯誤。請到「設定 → HTTPS 與憑證」匯入新憑證。`
        : `主控台憑證剩 ${t.daysLeft} 天，${t.validToTaipei}（台北時間）到期。請盡快到「設定 → HTTPS 與憑證」匯入新憑證。`;
      b.hidden = false;
    } else b.hidden = true;
  } catch (_) { b.hidden = true; }
}

/* ---------------------------------------------------------------- 1 機器 */

/* ---------------------------------------------------------------- 群組 */

const GROUP_LOCK_NAMES = { models: "要哪些模型", profile: "執行參數", reserve_user_gib: "保留記憶體", allowed_models: "只准用的模型", gpu_power_limit: "顯卡功耗上限" };
const GROUP_SRC_NAMES = { group: "同群組互傳", mirror: "內部鏡像", huggingface: "Hugging Face" };
const GROUP_APPLY_NAMES = { now: "立即", boot: "下次開機", at: "指定時間" };

function groupField(form, label, node, wide) {
  const f = el("label", "field" + (wide ? " wide" : ""), label);
  f.appendChild(node);
  form.appendChild(f);
  return node;
}

function groupInputBox(type, value, attrs) {
  const i = el("input");
  i.type = type;
  if (value !== null && value !== undefined) i.value = value;
  Object.assign(i, attrs || {});
  return i;
}

async function settingsGroups(wrap) {
  let d;
  const head = card("群組", "同一群組的機器用同一份設定；設定一改，版本號就加一");
  wrap.appendChild(head);
  try { d = await api("/api/groups"); } catch (ex) { head.appendChild(el("p", "note warn", ex.message)); return; }
  head.appendChild(el("p", "small muted",
    "先建群組（例如一間教室、一個部門），再到「機器」把機器放進來，最後在這裡填群組設定。存檔後每台機器會先「準備」（下載並核對模型），到了套用時機才「套用」（換上模型、生效鎖定），上課或上班中改設定不會提早換掉正在用的模型。"));
  const add = el("div", "slot-actions");
  const fName = groupInputBox("text", "", { placeholder: "新群組名稱，例：電腦教室 A" });
  fName.style.maxWidth = "280px";
  const fKind = el("select");
  d.kinds.forEach((k) => { const o = el("option", null, k); o.value = k; fKind.appendChild(o); });
  fKind.style.maxWidth = "120px";
  const ab = el("button", "btn sm primary", "新增群組");
  ab.onclick = async () => {
    try {
      const r = await api("/api/groups", { method: "POST", body: { name: fName.value.trim(), kind: fKind.value } });
      S.groupEdit = r.group.id;
      toast("已新增群組", "ok");
      render();
    } catch (ex) { toast(ex.message, "bad"); }
  };
  [fName, fKind, ab].forEach((x) => add.appendChild(x));
  head.appendChild(add);

  const list = card("全部群組", `${d.groups.length} 個`);
  const tbl = el("table", "api-table");
  const hr = el("tr");
  ["名稱", "種類", "機器", "設定版本", "就緒", "已套用", "更新時間（台北時間）", ""].forEach((h) => hr.appendChild(el("th", null, h)));
  tbl.appendChild(hr);
  d.groups.forEach((g) => {
    const tr = el("tr");
    tr.appendChild(el("td", null, g.name));
    tr.appendChild(el("td", null, g.kind));
    tr.appendChild(el("td", null, `${g.members} 台`));
    tr.appendChild(el("td", "mono", `v${g.version}`));
    tr.appendChild(el("td", "nowrap", g.members ? `${g.ready}/${g.members}` : "—"));
    tr.appendChild(el("td", "nowrap", g.members ? `${g.done}/${g.members}` : "—"));
    tr.appendChild(el("td", "nowrap", new Date(g.updated * 1000).toLocaleString(uiLocale(), { hour12: false, timeZone: "Asia/Taipei" })));
    const td = el("td", "nowrap");
    const eb = el("button", "btn sm" + (S.groupEdit === g.id ? " primary" : ""), "編輯設定");
    eb.onclick = () => { S.groupEdit = S.groupEdit === g.id ? null : g.id; render(); };
    td.appendChild(eb);
    const jb = el("button", "btn sm", "產生加入碼");
    jb.onclick = () => openAddMachine(g.name);
    td.appendChild(jb);
    if (g.name !== "未分類") {
      const db = el("button", "btn sm", "刪除");
      db.onclick = () => modal({
        title: `刪除群組「${g.name}」`, hasInput: false, danger: true, confirmText: "刪除",
        bodyNode: el("p", "small muted", g.members
          ? `這個群組有 ${g.members} 台機器。刪除後它們移到「未分類」，機器本身不受影響。`
          : "這個群組沒有機器。"),
        onConfirm: async () => {
          try { const r = await api(`/api/groups/${g.id}/delete`, { method: "POST" }); toast((r.moved ? `已刪除，${r.moved} 台移到「未分類」` : "已刪除") + (r.join_codes_revoked ? `；這個群組還能用的 ${r.join_codes_revoked} 個加入碼一起作廢` : ""), "ok"); }
          catch (ex) { toast(ex.message, "bad"); }
          if (S.groupEdit === g.id) S.groupEdit = null;
          render();
        },
      });
      td.appendChild(db);
    }
    tr.appendChild(td);
    tbl.appendChild(tr);
  });
  const tw = el("div", "md-table-wrap");
  tw.appendChild(tbl);
  list.appendChild(tw);
  wrap.appendChild(list);

  const g = d.groups.find((x) => x.id === S.groupEdit);
  if (g) {
    wrap.appendChild(groupEditor(g, d));
    wrap.appendChild(groupMembers(g));
  }
  wrap.appendChild(await joinCodesCard());
}

const GROUP_PHASE = { queued: "收到，排隊中", preparing: "準備中", waiting_boot: "已準備，等下次開機套用", waiting_time: "已準備，等指定時間套用",
  applying: "套用中", done: "已套用", failed: "失敗", idle: "—" };

function groupMembers(g) {
  const c = card("各台進度", `設定 v${g.version}；就緒 ${g.ready}/${g.members}、已套用 ${g.done}/${g.members}`);
  const rb = el("button", "btn sm", "重新整理");
  rb.onclick = () => render();
  const ra = el("div", "slot-actions");
  ra.appendChild(rb);
  c.appendChild(ra);
  if (!g.nodes.length) {
    c.appendChild(el("p", "small muted", "這個群組還沒有機器。到「機器」把機器的群組改成這一個。"));
    return c;
  }
  const tbl = el("table", "api-table");
  const hr = el("tr");
  ["機器", "已準備", "已套用", "狀態", "最後一步"].forEach((h) => hr.appendChild(el("th", null, h)));
  tbl.appendChild(hr);
  g.nodes.forEach((m) => {
    const tr = el("tr");
    tr.appendChild(el("td", null, m.name || m.id));
    tr.appendChild(el("td", "mono", m.prepared ? `v${m.prepared}` : "—"));
    tr.appendChild(el("td", "mono", m.applied ? `v${m.applied}` : "—"));
    const status = !m.checked ? "還沒連到（主動連回的機器要在線才送）"
      : m.error && !m.phase ? m.error : GROUP_PHASE[m.phase] || m.phase || "—";
    tr.appendChild(el("td", m.phase === "failed" || (m.error && !m.phase) ? "bad" : null, status));
    const last = m.steps[m.steps.length - 1];
    tr.appendChild(el("td", "small", last ? last.step : "—"));
    tbl.appendChild(tr);
  });
  const tw = el("div", "md-table-wrap");
  tw.appendChild(tbl);
  c.appendChild(tw);
  return c;
}

function groupEditor(g, d) {
  const st = g.settings || d.defaults;
  const p = st.profile || {};
  const c = card(`「${g.name}」的設定`, `目前版本 v${g.version}；內容有變才會加一`);
  const form = el("div", "grp-form");
  const fName = groupField(form, "群組名稱", groupInputBox("text", g.name, { disabled: g.name === "未分類" }));
  const fKind = el("select");
  d.kinds.forEach((k) => { const o = el("option", null, k); o.value = k; o.selected = k === g.kind; fKind.appendChild(o); });
  groupField(form, "種類（只是標籤）", fKind);
  const lines = (a) => (a || []).join("\n");
  const fModels = groupField(form, "要哪些模型（一行一個；寫 auto 由主控台依每台硬體挑版本）", el("textarea"), true);
  fModels.value = lines(st.models);
  const fAllowed = groupField(form, "只准用的模型（一行一個；空的就是不限）", el("textarea"), true);
  fAllowed.value = lines(st.allowed_models);
  const num = (v) => (v === null || v === undefined ? "" : v);
  const fCtx = groupField(form, "上下文長度（token，空的就是不指定）", groupInputBox("number", num(p.ctx), { min: 512, step: 512 }));
  const fPar = groupField(form, "並行數", groupInputBox("number", num(p.parallel), { min: 1, max: 64 }));
  const fThink = el("select");
  [["", "不指定"], ["on", "開"], ["off", "關"]].forEach(([v, t]) => {
    const o = el("option", null, t); o.value = v; o.selected = v === (p.thinking === true ? "on" : p.thinking === false ? "off" : ""); fThink.appendChild(o);
  });
  groupField(form, "思考", fThink);
  const fTemp = groupField(form, "溫度（0～2）", groupInputBox("number", num(p.temperature), { min: 0, max: 2, step: 0.05 }));
  const fTopP = groupField(form, "top_p（0～1）", groupInputBox("number", num(p.top_p), { min: 0, max: 1, step: 0.05 }));
  const fRes = groupField(form, "保留給使用者其他程式的記憶體（GiB）", groupInputBox("number", num(st.reserve_user_gib), { min: 0, step: 1 }));
  const fPow = groupField(form, "顯卡功耗上限（瓦，空的就是不限）", groupInputBox("number", num(st.gpu_power_limit), { min: 30, step: 10 }));
  const fRoot = groupField(form, "模型存放位置（完整路徑；還原卡要指到不還原的磁碟；空的用預設）", groupInputBox("text", st.storage_root || "", { placeholder: "/data/xconsole 或 D:\\xconsole" }), true);

  const checks = (names, on) => {
    const box = el("div", "grp-checks");
    const cbs = {};
    Object.entries(names).forEach(([k, t]) => {
      const l = el("label");
      const cb = el("input");
      cb.type = "checkbox"; cb.checked = on.includes(k);
      l.appendChild(cb);
      l.appendChild(document.createTextNode(t));
      box.appendChild(l);
      cbs[k] = cb;
    });
    return [box, cbs];
  };
  const [srcBox, srcCb] = checks(GROUP_SRC_NAMES, st.sources || []);
  groupField(form, "模型可以從哪裡取得（至少選一個）", srcBox, true);
  const [lockBox, lockCb] = checks(GROUP_LOCK_NAMES, st.locked || []);
  groupField(form, "鎖定（使用者在自己那台不能改）", lockBox, true);

  const fApply = el("select");
  Object.entries(GROUP_APPLY_NAMES).forEach(([k, t]) => { const o = el("option", null, t); o.value = k; o.selected = k === (st.apply_at?.mode || "now"); fApply.appendChild(o); });
  groupField(form, "套用時機", fApply);
  const at = st.apply_at?.at ? new Date(st.apply_at.at * 1000) : null;
  // datetime-local 用瀏覽器本地時間；送出前換成時間戳，主控台以自己的時鐘為準
  const local = at ? new Date(at.getTime() - at.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "";
  const fAt = groupField(form, "指定時間（選「指定時間」才用）", groupInputBox("datetime-local", local));
  c.appendChild(form);

  const act = el("div", "slot-actions");
  const sb = el("button", "btn primary", "儲存設定");
  sb.onclick = async () => {
    const n = (i) => (i.value.trim() === "" ? null : Number(i.value));
    const ls = (t) => t.value.split(/\n+/).map((x) => x.trim()).filter(Boolean);
    const profile = {};
    if (n(fCtx) !== null) profile.ctx = n(fCtx);
    if (n(fPar) !== null) profile.parallel = n(fPar);
    if (fThink.value) profile.thinking = fThink.value === "on";
    if (n(fTemp) !== null) profile.temperature = n(fTemp);
    if (n(fTopP) !== null) profile.top_p = n(fTopP);
    const apply = { mode: fApply.value };
    if (apply.mode === "at") {
      if (!fAt.value) { toast("選了「指定時間」就要填時間", "bad"); return; }
      apply.at = Math.floor(new Date(fAt.value).getTime() / 1000);
    }
    const settings = {
      models: ls(fModels), allowed_models: ls(fAllowed), profile,
      reserve_user_gib: n(fRes), gpu_power_limit: n(fPow), storage_root: fRoot.value.trim(),
      sources: Object.keys(srcCb).filter((k) => srcCb[k].checked),
      locked: Object.keys(lockCb).filter((k) => lockCb[k].checked), apply_at: apply,
    };
    try {
      const r = await api(`/api/groups/${g.id}/update`, { method: "POST", body: { name: fName.value.trim(), kind: fKind.value, settings } });
      toast(r.changed ? `已儲存，設定版本升到 v${r.group.version}` : "已儲存（設定內容沒變，版本不動）", "ok");
      render();
    } catch (ex) { toast(ex.message, "bad"); }
  };
  act.appendChild(sb);
  c.appendChild(act);
  return c;
}

async function settingsMachines(wrap) {
  const c = card("納管的機器", `共 ${S.fleet.nodes.length} 台`);
  const add = el("button", "btn sm primary", "加一台機器");
  add.onclick = openAddMachine;
  c.querySelector(".card-head").append(el("span", "spacer"), add);
  c.appendChild(el("p", "small muted",
    "機器怎麼來的：按「加一台機器」產生一行安裝指令，到那台貼上執行，就會自動出現在這裡；單機版安裝時會先把本機放進來。"
    + "主控台連各機代理一律走 HTTPS，只接受加入時收到的那張代理憑證，不會退回明文或關掉驗證。"));
  const box = el("div");
  loading(box);
  c.appendChild(box);
  wrap.appendChild(c);

  let nodes;
  try { nodes = (await api("/api/nodes")).nodes; } catch (ex) { box.textContent = ex.message; return; }
  if (!nodes.length) {
    box.textContent = "";
    box.appendChild(el("p", "note", "還沒有納管任何機器。按右上角「加一台機器」開始。"));
    return;
  }
  const MM = { unified_split: "統一記憶體（切兩塊）", unified_single: "統一記憶體（單一池）", discrete: "獨立顯示記憶體" };
  const sb = el("div", "scrollbox");
  const tb = el("table");
  tb.innerHTML = "<thead><tr><th>機器</th><th>群組</th><th>位址</th><th>記憶體架構</th><th>連線</th><th>代理憑證</th><th></th></tr></thead>";
  const body = el("tbody");
  nodes.forEach((n) => {
    const live = S.fleet.nodes.find((x) => x.id === n.id);
    const tr = el("tr");
    tr.appendChild(el("td", "nowrap", n.name));
    tr.appendChild(el("td", "nowrap", n.group));
    const push = n.link === "push";   // 工作站：防火牆擋連入，由那台主動連回主控台
    tr.appendChild(Object.assign(el("td", "mono"), { textContent: push ? `${n.host}（由這台連回）` : `${n.host}:${n.agent_port || 9401}` }));
    tr.appendChild(el("td", "nowrap", live?.online ? (MM[live.state.pools.memory_model] || "—") : "—"));
    tr.appendChild(el("td", "nowrap " + (live?.online ? "" : "bad-t"), live?.online ? "在線" : (push ? "沒有連回" : "連不上")));
    tr.appendChild(Object.assign(el("td", "mono small"),
      { textContent: push ? "不需要（連回時驗權杖）" : n.trusted ? (n.trust_fingerprint || "").slice(0, 23) + "…" : "尚未信任" }));
    const td = el("td", "row-acts");
    const e = el("button", "btn sm", "修改");
    e.onclick = () => openMachineSettings(n.id);
    const r = el("button", "btn sm danger", "移除");
    r.onclick = () => removeMachine(n);
    td.append(e, r);
    tr.appendChild(td);
    body.appendChild(tr);
  });
  tb.appendChild(body);
  sb.appendChild(tb);
  box.textContent = "";
  box.appendChild(sb);
}

function importTrust(n) {
  const d = el("div");
  d.appendChild(el("p", "small muted",
    `選 ${n.name} 代理的憑證檔（在該機的 ~/.xconsole/agent-cert.pem）。主控台之後只接受這張憑證，其他人冒充會被拒絕。`));
  const inp = el("input");
  inp.type = "file";
  inp.accept = ".pem,.crt,.cer";
  d.appendChild(inp);
  modal({
    title: `匯入 ${n.name} 的代理憑證`, bodyNode: d, confirmText: "匯入", hasInput: true,
    onConfirm: async () => {
      if (!inp.files[0]) { toast("沒有選檔案", "bad"); return; }
      try {
        const r = await api(`/api/node/${n.id}/trusted-ca`, { method: "POST", body: { file: await readB64(inp.files[0]) } });
        toast(`已信任 ${n.name}：${r.summary.fingerprint256.slice(0, 23)}…`, "ok");
        render();
      } catch (ex) { toast(ex.message, "bad"); }
    },
  });
}

/* ---------------------------------------------------------------- 2 HTTPS 與憑證 */

function tlsSteps(st, ui) {
  const cur = st.current || {};
  const imported = cur.source === "imported";
  const steps = [
    { t: "確認使用者會打的網址", why: "憑證必須涵蓋這些網址與 IP，不然使用者會看到憑證錯誤",
      done: (st.hostnames || []).length > 0 },
    { t: "取得憑證", why: "已有憑證就直接上傳；沒有就在下面產生 CSR 交給憑證廠商",
      done: imported || !!st.csr || !!ui.inspected },
    { t: "上傳並檢查", why: "先看懂哪裡有問題再決定套不套，不會直接換上去",
      done: imported || !!(ui.inspected && ui.inspected.ok) },
    { t: "套用", why: "不必重開機，新連線立刻生效；失敗會自動換回原本那張", done: imported },
    { t: "開啟 HSTS", why: "讓瀏覽器以後一律走 HTTPS，不會被降級成 http",
      done: !!cur.hsts, blocked: !st.hsts_allowed, need: "（要先換成正式憑證）" },
  ];
  let firstOpen = steps.findIndex((s) => !s.done);
  steps.forEach((s, i) => {
    if (s.done) s.state = "done";
    else if (s.blocked && i !== firstOpen) s.state = "blocked";
    else if (i === firstOpen) s.state = s.blocked ? "blocked" : "now";
    else s.state = "blocked";
  });
  return steps;
}

function renderSteps(steps) {
  const box = el("div");
  const done = steps.filter((s) => s.done).length;
  const next = steps.findIndex((s) => s.state === "now");
  const head = el("div", "steps-head");
  head.appendChild(el("span", "count", `${done}/${steps.length} 步完成`));
  const required = steps.filter((s) => !s.optional);
  head.appendChild(el("span", "next", next >= 0 ? `下一步：${next + 1}. ${steps[next].t}`
    : required.every((s) => s.done) ? (done === steps.length ? "全部完成" : "必要步驟都完成了，其餘可略過")
      : "還有步驟要先完成前面的才能做"));
  box.appendChild(head);
  const g = el("div", "steps");
  steps.forEach((s, i) => {
    const d = el("div", "step " + s.state);
    const t = el("div", "t");
    t.appendChild(el("span", "n", String(i + 1)));
    t.appendChild(document.createTextNode(s.t));
    d.appendChild(t);
    d.appendChild(el("div", "why", s.why));
    const lbl = { done: "已完成", now: "現在做這步", blocked: "還不能做", skip: "可略過" }[s.state];
    const need = s.state === "blocked"
      ? (s.blocked && s.need ? s.need : `（要先完成第 ${steps.findIndex((x) => !x.done && !x.optional) + 1} 步）`) : "";
    d.appendChild(el("span", "st", lbl + need));
    if (s.go && s.state !== "done" && s.state !== "blocked") {
      const b = el("button", "btn sm" + (s.state === "now" ? " primary" : ""), s.go[0]);
      b.onclick = s.go[1];
      d.appendChild(b);
    }
    g.appendChild(d);
  });
  box.appendChild(g);
  // 手機上順序軸是橫向滑動的：把「現在做這步」捲到眼前，不要讓人以為只有第一步
  setTimeout(() => {
    const now = g.querySelector(".step.now");
    if (now && g.scrollWidth > g.clientWidth) g.scrollLeft = now.offsetLeft - g.offsetLeft;
  }, 0);
  return box;
}

async function settingsTls(wrap) {
  const ui = S.tlsUi || (S.tlsUi = {});
  let st;
  const top = card("HTTPS 與憑證", "換憑證不必找工程師、不必重開機");
  loading(top);
  wrap.appendChild(top);
  try { st = await api("/api/tls/status"); } catch (ex) { top.textContent = ex.message; return; }
  const cur = st.current || {};

  // ---- 順序軸
  top.textContent = "";
  const h = el("div", "card-head");
  h.appendChild(el("h2", null, "HTTPS 與憑證"));
  h.appendChild(el("span", "sub", "換憑證不必找工程師、不必重開機"));
  top.appendChild(h);
  top.appendChild(renderSteps(tlsSteps(st, ui)));

  // ---- 目前憑證
  const cc = card("目前使用的憑證", cur.source === "imported" ? "已匯入" : "暫用自簽");
  const lvl = cur.level === "ok" ? "" : cur.level === "warn" ? "warn" : "bad";
  if (cur.selfSigned) cc.appendChild(el("p", "note warn",
    "目前是系統自己產生的暫用自簽憑證，使用者第一次連線會看到安全警告。正式上線請匯入憑證廠商或貴單位 CA 簽發的憑證。"));
  const rows = [
    ["涵蓋的網址", [...(cur.dns || []), ...(cur.ips || [])].join("、") || "—"],
    ["發行者", cur.issuer || "—"],
    ["到期", `${cur.validToTaipei}（台北時間），剩 ${cur.daysLeft} 天`],
    ["金鑰", cur.key || "—"],
    ["憑證鏈", `${cur.chainLength || 1} 張`],
    ["指紋（SHA-256 前 16 碼）", (cur.fingerprint256 || "").slice(0, 47)],
    ["套用時間", cur.appliedAtTaipei ? cur.appliedAtTaipei + "（台北時間）" : "—"],
  ];
  const kvb = el("div");
  kvb.style.maxWidth = "640px";
  rows.forEach(([k, v]) => {
    const r = kvRow(k, v);
    if (k === "到期" && lvl) r.querySelector(".v").style.color = lvl === "warn" ? "var(--warn)" : "var(--bad)";
    kvb.appendChild(r);
  });
  cc.appendChild(kvb);
  const ccAct = el("div", "slot-actions");
  const rb = el("button", "btn sm", "還原上一張");
  rb.disabled = !(st.history || []).length;
  rb.title = (st.history || [])[0] ? `上一張：${st.history[0].subject}，${st.history[0].validToTaipei} 到期` : "沒有上一張";
  rb.onclick = () => modal({
    title: "還原上一張憑證", hasInput: false, danger: true, confirmText: "還原",
    bodyNode: el("p", "small muted", `會換回 ${st.history[0]?.subject || "上一張"}（${st.history[0]?.validToTaipei || ""} 到期）。新連線立刻生效，不必重開。`),
    onConfirm: async () => {
      try { const r = await api("/api/tls/rollback", { method: "POST" }); toast(r.message, "ok"); } catch (ex) { toast(ex.message, "bad"); }
      checkTlsBanner(); render();
    },
  });
  ccAct.appendChild(rb);
  const bs = el("button", "btn sm", "依目前網址重產暫用自簽");
  bs.onclick = () => modal({
    title: "重新產生暫用自簽", danger: cur.source === "imported", confirmText: "重新產生",
    bodyNode: el("p", "small muted", cur.source === "imported"
      ? "目前是正式憑證。換回暫用自簽之後，使用者連線會看到安全警告，確定嗎？"
      : "會依「使用者會打的網址」重新產一張暫用自簽，瀏覽器需要再確認一次。"),
    onConfirm: async () => {
      try { const r = await api("/api/tls/bootstrap", { method: "POST", body: { confirm: true } }); toast(r.message, "ok"); } catch (ex) { toast(ex.message, "bad"); }
      render();
    },
  });
  ccAct.appendChild(bs);
  cc.appendChild(ccAct);
  wrap.appendChild(cc);

  // ---- 第 1 步：網址
  const hc = card("1. 使用者會打的網址", "一行一個，網域或 IP 都可以");
  const ta = el("textarea");
  ta.value = (st.hostnames || []).join("\n");
  hc.appendChild(ta);
  const hb = el("button", "btn sm", "儲存網址");
  hb.onclick = async () => {
    const list = ta.value.split(/\s+/).map((x) => x.trim()).filter(Boolean);
    try { await api("/api/tls/settings", { method: "POST", body: { hostnames: list } }); toast("網址已儲存", "ok"); render(); }
    catch (ex) { toast(ex.message, "bad"); }
  };
  const hbr = el("div", "slot-actions");
  hbr.appendChild(hb);
  hc.appendChild(hbr);
  wrap.appendChild(hc);

  // ---- 第 2 步：CSR
  const csrc = card("2. 還沒有憑證：產生 CSR 交給憑證廠商", "私鑰在這台伺服器上產生並加密保存，不會離開伺服器");
  if (st.csr) {
    csrc.appendChild(el("p", "note", `已有一份待配對的 CSR（${st.csr.cn}，${st.csr.createdTaipei}（台北時間）產生）。`
      + "廠商簽回憑證後，在下面第 3 步只要上傳憑證，系統會自動對上這把私鑰。"));
    const dl = el("button", "btn sm primary", "下載 CSR");
    dl.onclick = async () => {
      try {
        const res = await fetch("/api/tls/csr", { credentials: "same-origin" });
        if (!res.ok) throw new Error("下載失敗");
        download(`${st.csr.cn}.csr`, await res.text(), "application/pkcs10");
      } catch (ex) { toast(ex.message, "bad"); }
    };
    const r = el("div", "slot-actions"); r.appendChild(dl); csrc.appendChild(r);
  }
  const fr = el("div", "form-row");
  const mk = (label, node) => { const d = el("div"); d.appendChild(el("label", null, label)); d.appendChild(node); fr.appendChild(d); return node; };
  // 不預填：憑證廠商要的是完整網域名稱，預填主機名稱（例 HP-Z2-Mini-G1a）會被直接退件
  const cnI = mk("主要網址（CN）", Object.assign(el("input"), { type: "text", value: "", placeholder: "例：xconsole.貴單位網域.tw" }));
  const orgI = mk("單位名稱（選填）", Object.assign(el("input"), { type: "text", value: "" }));
  const ktI = mk("金鑰類型", el("select"));
  [["rsa2048", "RSA 2048（相容性最好）"], ["ec256", "ECC P-256（較新、較快）"]].forEach(([v, t]) => { const o = el("option", null, t); o.value = v; ktI.appendChild(o); });
  csrc.appendChild(fr);
  csrc.appendChild(el("p", "small muted", "其他網址會自動放進 CSR 的主體別名（SAN）：" + (st.hostnames || []).join("、")));
  const gb = el("button", "btn sm", st.csr ? "重新產生 CSR（舊的會作廢）" : "產生 CSR");
  gb.onclick = async () => {
    try {
      const r = await api("/api/tls/csr", { method: "POST",
        body: { cn: cnI.value.trim(), org: orgI.value.trim(), key_type: ktI.value, hostnames: st.hostnames } });
      download(`${cnI.value.trim()}.csr`, r.csr, "application/pkcs10");
      toast("CSR 已產生並下載，請交給憑證廠商", "ok");
      render();
    } catch (ex) { toast(ex.message, "bad"); }
  };
  const gr = el("div", "slot-actions"); gr.appendChild(gb); csrc.appendChild(gr);
  wrap.appendChild(csrc);

  // ---- 第 3、4 步：上傳、檢查、套用
  const up = card("3. 上傳並檢查　4. 套用", "PFX／P12、PEM、CER、CRT、KEY、P7B 都可以，順序不拘");
  const dz = el("div", "dropzone");
  dz.innerHTML = "<strong>把憑證檔拖到這裡，或點這裡選檔</strong>可以一次選多個檔案（例如憑證、私鑰、中繼憑證三個檔）";
  const fi = el("input");
  fi.type = "file"; fi.multiple = true; fi.hidden = true;
  fi.accept = ".pfx,.p12,.pem,.cer,.crt,.key,.p7b,.p7c,.der";
  dz.appendChild(fi);
  const fl = el("div", "filelist");
  const pwBox = el("div", "form-row");
  pwBox.style.marginTop = "10px";
  const pfxPw = Object.assign(el("input"), { type: "password", autocomplete: "off", placeholder: "PFX 匯出時設定的密碼" });
  const keyPw = Object.assign(el("input"), { type: "password", autocomplete: "off", placeholder: "私鑰的密碼" });
  const pfxWrap = el("div"); pfxWrap.appendChild(el("label", null, "PFX／P12 密碼")); pfxWrap.appendChild(pfxPw);
  const keyWrap = el("div"); keyWrap.appendChild(el("label", null, "私鑰密碼")); keyWrap.appendChild(keyPw);
  pfxWrap.hidden = true; keyWrap.hidden = true;
  pwBox.appendChild(pfxWrap); pwBox.appendChild(keyWrap);
  const res = el("div");
  const act = el("div", "slot-actions");
  const ib = el("button", "btn sm primary", "檢查");
  const ab = el("button", "btn sm", "套用這張憑證");
  ab.disabled = true;
  act.appendChild(ib); act.appendChild(ab);
  ui.files = ui.files || [];

  const showFiles = async () => {
    fl.textContent = "";
    let needPfx = false, needKey = false;
    for (const f of ui.files) {
      fl.appendChild(el("span", "chip", f.name));
      if (/\.(pfx|p12)$/i.test(f.name)) needPfx = true;
      if (/\.(pem|key)$/i.test(f.name) && f.size < 200000) {
        const txt = await f.text();
        if (/ENCRYPTED/.test(txt)) needKey = true;
      }
    }
    // 偵測到 PFX 才出現密碼欄、偵測到加密私鑰才出現私鑰密碼欄
    pfxWrap.hidden = !needPfx && !ui.inspected?.need_password;
    keyWrap.hidden = !needKey && !ui.inspected?.need_key_password;
  };
  const addFiles = (list) => {
    for (const f of list) if (!ui.files.some((x) => x.name === f.name && x.size === f.size)) ui.files.push(f);
    ui.inspected = null; ab.disabled = true; res.textContent = "";
    showFiles();
  };
  dz.onclick = () => fi.click();
  fi.onchange = () => addFiles(fi.files);
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); addFiles(e.dataTransfer.files); });
  [pfxPw, keyPw].forEach((x) => imeGuard(x, () => ib.click()));

  const showResult = (r) => {
    res.textContent = "";
    const grp = (cls, title, items) => {
      if (!items?.length) return;
      const g = el("div", "res-group " + cls);
      g.appendChild(el("h4", null, title));
      const ul = el("ul");
      items.forEach((t) => ul.appendChild(el("li", null, t)));
      g.appendChild(ul);
      res.appendChild(g);
    };
    grp("bad", "不能套用", r.errors);
    grp("warn", "要注意", r.warnings);
    grp("ok", "可以套用", r.passes);
    if (r.summary) {
      const s = r.summary;
      res.appendChild(el("p", "small muted", `這張憑證：${s.subject}，發行者 ${s.issuer}，${s.validToTaipei}（台北時間）到期，`
        + `涵蓋 ${[...s.dns, ...s.ips].join("、")}，指紋 ${s.fingerprint256.slice(0, 23)}…`));
    }
  };
  ib.onclick = async () => {
    if (!ui.files.length) { toast("請先選擇檔案", "bad"); return; }
    ib.disabled = true;
    try {
      const files = [];
      for (const f of ui.files) files.push({ name: f.name, data: await readB64(f) });
      const r = await api("/api/tls/inspect", { method: "POST", body: { files, password: pfxPw.value, key_password: keyPw.value } });
      ui.inspected = r;
      showResult(r);
      ab.disabled = !r.ok;
      if (r.need_password) pfxWrap.hidden = false;
      if (r.need_key_password) keyWrap.hidden = false;
    } catch (ex) { toast(ex.message, "bad"); }
    ib.disabled = false;
  };
  ab.onclick = () => {
    const s = ui.inspected.summary;
    modal({
      title: "套用這張憑證", confirmText: "套用",
      bodyNode: el("p", "small muted", `換成 ${s.subject}（${s.issuer}，${s.validToTaipei} 到期）。`
        + "新連線立刻使用，已連上的不會中斷；系統會實際連自己一次核對，不對就自動換回原本那張。"),
      onConfirm: async () => {
        try {
          const r = await api("/api/tls/apply", { method: "POST", body: { token: ui.inspected.token } });
          toast(r.message, "ok");
          ui.files = []; ui.inspected = null;
        } catch (ex) { toast(ex.message, "bad"); }
        checkTlsBanner(); render();
      },
    });
  };
  up.appendChild(dz); up.appendChild(fl); up.appendChild(pwBox); up.appendChild(act); up.appendChild(res);
  wrap.appendChild(up);
  if (ui.files.length) showFiles();
  if (ui.inspected) { showResult(ui.inspected); ab.disabled = !ui.inspected.ok; }

  // ---- 第 5 步：HSTS
  const hs = card("5. HSTS", "讓瀏覽器以後一律走 HTTPS");
  const lab = el("label", "switch" + (st.hsts_allowed ? "" : " disabled"));
  const cb = el("input");
  cb.type = "checkbox"; cb.checked = !!cur.hsts; cb.disabled = !st.hsts_allowed;
  lab.appendChild(cb);
  lab.appendChild(document.createTextNode(cur.hsts ? "已開啟" : "未開啟"));
  hs.appendChild(lab);
  hs.appendChild(el("p", "small muted", st.hsts_allowed
    ? "開啟後一年內，瀏覽器不會再用 http 連這台。之後憑證出問題時，使用者連「略過警告」都沒有，所以只在正式憑證驗證過後才開。"
    : "目前是暫用自簽或憑證已過期，不能開啟——開了之後憑證一出問題，使用者連「略過警告」都沒有。"));
  cb.onchange = async () => {
    try { await api("/api/tls/settings", { method: "POST", body: { hsts: cb.checked } }); toast(cb.checked ? "HSTS 已開啟" : "HSTS 已關閉", "ok"); }
    catch (ex) { toast(ex.message, "bad"); }
    render();
  };
  wrap.appendChild(hs);
  await settingsLinkCard(wrap);
}

// 工作站主動連回用的通道：另一個埠、另一張憑證。換上面那張管理網頁的憑證不會影響它
async function settingsLinkCard(wrap) {
  let li;
  try { li = await api("/api/link"); } catch (ex) { return; }
  const day = (t) => t ? new Date(t * 1000).toLocaleString(uiLocale(), { hour12: false, timeZone: "Asia/Taipei" }).slice(0, 10) : "—";
  const ring = li.ring || {};
  const c = card("工作站連回通道", `主動連回的機器 ${li.nodes} 台；這裡的憑證和上面的管理網頁憑證分開，換那張不會讓工作站斷線`);
  const kv = el("div");
  kv.style.maxWidth = "640px";
  kv.appendChild(kvRow("通道埠", li.listen && li.listen.ok ? `${li.port}（有在聽）` : (li.listen && li.listen.error) || `${li.port}（沒有在聽）`));
  const name = { current: "使用中", next: "輪替中（發給各台）", retired: "退役（保留到期就刪）" };
  (ring.keys || []).forEach((k) => kv.appendChild(kvRow(`通道憑證・${name[k.state] || k.state}`,
    `${k.fingerprint256.slice(0, 16)}…，${day(k.created)} 產生` + (k.retire_at ? `，${day(k.retire_at)} 刪除（台北時間）` : ""))));
  c.appendChild(kv);
  if (li.listen && !li.listen.ok) c.appendChild(el("p", "note warn", "通道埠沒有在聽：主動連回的工作站都連不上。請確認這個埠沒被別的程式占用，再重開主控台。"));
  if (ring.rotating) c.appendChild(el("p", "note", ring.waiting.length
    ? `新的通道憑證正在發給各台，在線的都收到後自動換上。還在等：${ring.waiting.slice(0, 20).join("、")}${ring.waiting.length > 20 ? ` 等 ${ring.waiting.length} 台` : ""}`
    : "新的通道憑證已發出，正在換上。"));
  c.appendChild(el("p", "small muted",
    `換通道憑證時，舊的會再保留 ${ring.retain_days || 30} 天：這段時間關機或沒帶回來的機器，一連上就自動拿到新的；超過 ${ring.retain_days || 30} 天都沒上線的要重新加入。`));
  const act = el("div", "slot-actions");
  const rb = el("button", "btn sm", "換通道憑證");
  rb.disabled = !!ring.rotating;
  rb.title = ring.rotating ? "上一次還沒完成" : "";
  rb.onclick = () => modal({
    title: "換通道憑證", hasInput: false, confirmText: "換",
    bodyNode: el("p", "small muted", "會產生一張新的通道憑證，經現有的通道發給每台主動連回的機器。在線的都收到後才換上，工作站不會斷線。"),
    onConfirm: async () => {
      try { const r = await api("/api/link/rotate", { method: "POST" }); toast(r.message, "ok"); } catch (ex) { toast(ex.message, "bad"); }
      render();
    },
  });
  act.appendChild(rb);
  c.appendChild(act);

  c.appendChild(el("h3", null, "對外網址"));
  c.appendChild(el("p", "small muted",
    "筆電帶回家或在外面時，從這些位址連回來（一行一個，寫成「主機:埠」，例如 vpn.example.com:9402）。對外只要開通道埠，不必開管理網頁的埠。各台在辦公室裡照樣走內部位址，連不到才改走這些。"));
  const ta = el("textarea");
  ta.value = (li.public || []).join("\n");
  c.appendChild(ta);
  c.appendChild(el("h3", null, "代理伺服器"));
  c.appendChild(el("p", "small muted",
    "公司網路要經代理伺服器才能連到主控台時填，寫成 http://主機:埠。留空就用各台自己的系統設定；都不通時各台會改直連試試。需要帳密的代理伺服器請設在各台的系統設定，主控台不保存代理伺服器的密碼。"));
  const px = el("input");
  px.type = "text";
  px.value = li.proxy || "";
  px.placeholder = "http://proxy.example.com:3128";
  c.appendChild(px);
  const sb = el("button", "btn sm", "儲存並發給各台");
  sb.onclick = async () => {
    const pub = ta.value.split(/\s+/).map((x) => x.trim()).filter(Boolean);
    try {
      await api("/api/link/settings", { method: "POST", body: { public: pub, proxy: px.value.trim() } });
      toast("已儲存，在線的工作站幾秒內就會收到", "ok");
      render();
    } catch (ex) { toast(ex.message, "bad"); }
  };
  const sr = el("div", "slot-actions");
  sr.appendChild(sb);
  c.appendChild(sr);
  wrap.appendChild(c);
}

/* ---------------------------------------------------------------- 3 使用報告 */

async function settingsReport(wrap) {
  const days = S.reportDays || 30;
  const head = card("服務使用報告", "數字全由系統的取樣與操作紀錄算出");
  const per = el("div", "slot-actions");
  per.appendChild(el("span", "small muted", "期間："));
  [7, 30, 90].forEach((d) => {
    const b = el("button", "btn sm" + (d === days ? " primary" : ""), `近 ${d} 天`);
    b.onclick = () => { S.reportDays = d; render(); };
    per.appendChild(b);
  });
  per.appendChild(el("span", "spacer"));
  const dj = el("button", "btn sm", "下載 JSON");
  per.appendChild(dj);
  head.appendChild(per);
  const box = el("div");
  loading(box);
  head.appendChild(box);
  wrap.appendChild(head);

  let r;
  try { r = await api(`/api/admin/usage-report?days=${days}`); } catch (ex) { box.textContent = ex.message; return; }
  dj.onclick = () => download(`xConsole-使用報告-近${days}天.json`, JSON.stringify(r, null, 1), "application/json");
  box.textContent = "";
  box.appendChild(el("p", "small muted", `${r.period.from} 至 ${r.period.to}（台北時間）`));
  if (r.period.note) box.appendChild(el("p", "note warn", r.period.note));

  const fmtN = (v) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString(uiLocale());
  const t = r.build_vs_buy.local;
  const tiles = el("div", "tiles light");
  const tile = (k, v, s) => { const d = el("div", "tile"); d.appendChild(el("div", "k", k)); d.appendChild(el("div", "v", v)); if (s) d.appendChild(el("div", "s", s)); return d; };
  tiles.appendChild(tile("登入次數", fmtN(r.people.logins_ok), `失敗 ${fmtN(r.people.logins_failed)} 次`));
  tiles.appendChild(tile("操作次數", fmtN(r.people.operations), `${fmtN(r.people.distinct_users)} 個帳號`));
  tiles.appendChild(tile("地端處理請求", fmtN(t.requests), `匯入 ${fmtN(t.prompt)} token`));
  tiles.appendChild(tile("快取命中", r.build_vs_buy.cache_hit_ratio === null ? "—" : `${(r.build_vs_buy.cache_hit_ratio * 100).toFixed(1)}%`, "重用的提示占比"));
  tiles.appendChild(tile("GPU 用電", `${r.spend.gpu_energy_kwh} 度`, r.spend.est_ntd ? `約 NT$ ${fmtN(r.spend.est_ntd)}` : "未設電價"));
  tiles.appendChild(tile("待處理", String(r.health.items.length), r.health.summary));
  box.appendChild(tiles);

  // 設備
  const ac = card("設備有沒有用到");
  const sb = el("div", "scrollbox");
  const tb = el("table");
  tb.innerHTML = "<thead><tr><th>機器</th><th>在線比例</th><th>池子平均／峰值</th><th>系統記憶體最低</th><th>低於底線</th><th>運作中欄位</th></tr></thead>";
  const body = el("tbody");
  r.assets.nodes.forEach((n) => {
    const tr = el("tr");
    [n.node, n.online_ratio === null ? "—" : `${(n.online_ratio * 100).toFixed(1)}%`,
     `${fmt(n.pool_used_avg_gib)}／${fmt(n.pool_used_peak_gib)} GiB（共 ${fmt(n.pool_total_gib, 0)}）`,
     n.memory_model === "unified_single" ? "單一池" : `${fmt(n.ram_free_min_gib)} GiB`,
     `${n.below_floor_samples} 次`, `${fmt(n.slots_busy_avg, 1)}／${n.slots_total}`].forEach((v) => tr.appendChild(el("td", null, v)));
    body.appendChild(tr);
  });
  if (!r.assets.nodes.length) { const tr = el("tr"); const td = el("td", null, "本期還沒有取樣資料"); td.colSpan = 6; tr.appendChild(td); body.appendChild(tr); }
  tb.appendChild(body); sb.appendChild(tb); ac.appendChild(sb);
  wrap.appendChild(ac);

  // 健康
  const hc = card("系統健康與治理", r.health.summary);
  if (!r.health.items.length) hc.appendChild(el("p", "note", "本期無異常。"));
  r.health.items.forEach((it) => {
    const g = el("div", "res-group " + (it.level === "緊急" ? "bad" : "warn"));
    g.appendChild(el("h4", null, `${it.level}：${it.what}`));
    g.appendChild(el("div", "small", it.fix));
    hc.appendChild(g);
  });
  wrap.appendChild(hc);

  // 估算假設
  const sc = card("估算用的假設", "沒有設定就不換算金額，報告裡會寫明");
  sc.appendChild(el("p", "assume", r.spend.assumption));
  sc.appendChild(el("p", "assume", r.build_vs_buy.assumption || r.build_vs_buy.cloud_equivalent?.assumption || ""));
  let cfg;
  try { cfg = (await api("/api/settings")).report || {}; } catch (_) { cfg = {}; }
  // 填單價是罕用的管理動作，平常收起來；假設的文字說明上面照常顯示
  const det = el("details");
  const sumy = el("summary", "small", "修改估算假設（電價、雲端參考單價）");
  sumy.style.cursor = "pointer";
  det.appendChild(sumy);
  const fr = el("div", "form-row");
  fr.style.marginTop = "10px";
  const mk = (label, v, ph) => { const d = el("div"); d.appendChild(el("label", null, label)); const i = Object.assign(el("input"), { type: "text", value: v ?? "", placeholder: ph || "" }); d.appendChild(i); fr.appendChild(d); return i; };
  const ref = cfg.cloud_ref || {};
  const pI = mk("電價（NT$／度）", cfg.price_ntd_per_kwh, "例：貴單位電費單上的平均單價");
  const nI = mk("雲端參考模型名稱", ref.name, "例：某雲端模型");
  const iI = mk("每百萬輸入 token（US$）", ref.usd_per_mtok_in, "照該模型官方定價填");
  const oI = mk("每百萬輸出 token（US$）", ref.usd_per_mtok_out, "照該模型官方定價填");
  const fI = mk("匯率（NT$／US$）", ref.ntd_per_usd, "選填");
  det.appendChild(fr);
  sc.appendChild(det);
  const sv = el("button", "btn sm", "儲存假設");
  sv.onclick = async () => {
    try {
      await api("/api/settings", { method: "POST", body: { report: { price_ntd_per_kwh: pI.value,
        cloud_ref: { name: nI.value, usd_per_mtok_in: iI.value, usd_per_mtok_out: oI.value, ntd_per_usd: fI.value } } } });
      toast("已儲存，報告會依新的假設重算", "ok"); render();
    } catch (ex) { toast(ex.message, "bad"); }
  };
  const svr = el("div", "slot-actions"); svr.appendChild(sv); det.appendChild(svr);
  sc.appendChild(el("p", "small muted", "要產出給主管看的 Word 月報，在有 xConsole 原始碼的電腦上執行："
    + "python3 scripts/report/build_usage_report.py --days 30"));
  wrap.appendChild(sc);
}

/* ---------------------------------------------------------------- 4 帳號與安全 */

// 桌面版：區網存取開關（照 LocModel：預設只有這台自己用；打開才讓別台電腦連進來，走 HTTPS＋密碼）
function settingsLan(wrap, me) {
  const c = card("區網存取", me.lan_access ? `別台電腦可以用 https://這台的位址:${me.https_port} 連進來（要登入）`
    : "目前只有這台電腦自己能用");
  const row = el("div", "slot-actions");
  const b = el("button", "btn sm" + (me.lan_access ? "" : " primary"), me.lan_access ? "關閉區網存取" : "打開區網存取");
  b.onclick = async () => {
    b.disabled = true;
    try {
      await api("/api/settings/lan", { method: "POST", body: { on: !me.lan_access } });
      toast(me.lan_access ? "已關閉，主控台重新啟動中" : "已打開，主控台重新啟動中", "ok");
      setTimeout(() => location.reload(), 4000);
    } catch (ex) { toast(ex.message, "bad"); b.disabled = false; }
  };
  row.appendChild(b);
  c.appendChild(row);
  if (me.password_unset) c.appendChild(el("p", "small muted", "要先在下面設一組密碼，才能打開區網存取。"));
  wrap.appendChild(c);
}

async function settingsAccount(wrap) {
  let me = {};
  try { me = await api("/api/settings"); } catch (_) { /* 沒拿到就顯示預設 */ }
  if (me.desktop) { S.lanOn = !!me.lan_access; settingsLan(wrap, me); }
  const first = me.app_window && me.password_unset;
  const c = card(first ? "設定密碼" : "變更密碼", first ? "這台的視窗不用密碼；別台電腦連進來（區網存取）時要用這組密碼登入"
    : `目前登入：${me.username || "admin"}`);
  const fr = el("div", "form-row");
  const mk = (label) => { const d = el("div"); d.appendChild(el("label", null, label)); const i = Object.assign(el("input"), { type: "password", autocomplete: "new-password" }); d.appendChild(i); fr.appendChild(d); return i; };
  const o = first ? { value: "" } : mk("目前的密碼");
  const n1 = mk("新密碼（至少 8 碼）");
  const n2 = mk("再輸入一次新密碼");
  c.appendChild(fr);
  const b = el("button", "btn sm primary", first ? "設定密碼" : "變更密碼");
  const go2 = async () => {
    if (n1.value !== n2.value) { toast("兩次輸入的新密碼不一樣", "bad"); return; }
    try {
      const r = await api("/api/account/password", { method: "POST", body: { old: o.value, new: n1.value } });
      toast(r.message, "ok");
      if (me.app_window) { n1.value = n2.value = ""; render(); } else setTimeout(showLogin, 800);
    } catch (ex) { toast(ex.message, "bad"); }
  };
  b.onclick = go2;
  [o, n1, n2].filter((x) => x.tagName).forEach((x) => imeGuard(x, go2));
  const br = el("div", "slot-actions"); br.appendChild(b); c.appendChild(br);
  c.appendChild(el("p", "small muted", "密碼以 PBKDF2-SHA256（20 萬輪）加鹽雜湊後存檔，不存明文。變更後所有已登入的裝置都會被登出。"));
  wrap.appendChild(c);

  const ac = card("最近的操作紀錄", "只記操作與結果，不記密碼、私鑰與權杖");
  const box = el("div");
  loading(box);
  ac.appendChild(box);
  wrap.appendChild(ac);
  try {
    const items = (await api("/api/admin/audit?limit=200")).items;
    const LABEL = { "auth.login": "登入", "slot.start": "啟動", "slot.stop": "停止", "slot.restart": "重啟",
      "model.swap": "換模型", "profile.apply": "切換情境", "all.stop": "全部停止", "tls.inspect": "檢查憑證",
      "tls.apply": "套用憑證", "tls.rollback": "還原憑證", "tls.csr": "產生 CSR", "tls.hsts": "HSTS 開關",
      "tls.hostnames": "修改網址", "tls.bootstrap": "重產自簽", "tls.expiry_notice": "憑證到期提醒",
      "trust.import": "信任代理憑證", "account.password": "變更密碼", "settings.report": "修改報告假設",
      "branding.update": "修改品牌名稱", "branding.logo": "更換 Logo" };
    const sb = el("div", "scrollbox");
    const tb = el("table");
    tb.innerHTML = "<thead><tr><th>時間（台北）</th><th>帳號</th><th>來源</th><th>動作</th><th>對象</th><th>結果</th></tr></thead>";
    const body = el("tbody");
    items.forEach((a) => {
      const tr = el("tr");
      [new Date(a.ts * 1000).toLocaleString(uiLocale(), { timeZone: "Asia/Taipei", hour12: false }),
       a.user || "—", a.ip || "—", LABEL[a.action] || a.action, a.target || "—",
       a.ok ? "成功" : ((a.detail && a.detail.blocked) ? "被准入擋下" : "失敗")].forEach((v) => tr.appendChild(el("td", null, v)));
      body.appendChild(tr);
    });
    if (!items.length) { const tr = el("tr"); const td = el("td", null, "還沒有紀錄"); td.colSpan = 6; tr.appendChild(td); body.appendChild(tr); }
    tb.appendChild(body); sb.appendChild(tb);
    box.textContent = ""; box.appendChild(sb);
  } catch (ex) { box.textContent = ex.message; }
}

/* ---------------------------------------------------------------- 5 品牌與外觀 */

function settingsBrand(wrap) {
  const b = S.brand || { org: "", product: "", logo: null, custom: false };

  const c = card("品牌與外觀", "賣給客戶時換成客戶的名稱與 Logo；頁尾固定顯示 Powered by xCloudinfo 云碩科技");
  const pv = el("div", "brand-preview");
  const pvImg = el("img");
  const pvMark = el("span", "brand-mark");
  const pvName = el("span");
  pv.append(pvMark, pvImg, pvName);
  const showPreview = (org, product, logo) => {
    pvName.textContent = product || "（系統名稱）";
    pvMark.textContent = [...(org || "x")][0];
    pvImg.hidden = !logo;
    pvMark.hidden = !!logo;
    if (logo && pvImg.getAttribute("src") !== logo.url) { pvImg.src = logo.url; pvImg.alt = org; }
  };
  c.appendChild(el("p", "small muted", "預覽（頁首會長這樣）"));
  c.appendChild(pv);

  const fr = el("div", "form-row");
  const mk = (label, value, hint) => {
    const d = el("div");
    d.appendChild(el("label", null, label));
    const i = Object.assign(el("input"), { type: "text", maxLength: 40, value });
    d.appendChild(i);
    if (hint) d.appendChild(el("p", "small muted", hint));
    fr.appendChild(d);
    return i;
  };
  const org = mk("單位名稱", b.custom ? b.org : "", "例：中原大學。用在 Logo 的替代文字、分頁標題與登入頁");
  const product = mk("系統名稱", b.custom ? b.product : "", "例：AI 模型主控台。顯示在頁首與登入頁大標");
  c.appendChild(fr);
  const live = () => showPreview(org.value.trim(), product.value.trim(), S.brand && S.brand.logo);
  org.addEventListener("input", live);
  product.addEventListener("input", live);
  live();

  const save = async () => {
    try {
      const r = await api("/api/branding", { method: "POST", body: { org: org.value.trim(), product: product.value.trim() } });
      applyBranding(r.branding);
      live();
      toast("已套用，所有頁面與登入頁都換成新的名稱", "ok");
    } catch (ex) { toast(ex.message, "bad"); }
  };
  [org, product].forEach((x) => imeGuard(x, save));
  const sr = el("div", "slot-actions");
  const sb = el("button", "btn sm primary", "儲存名稱");
  sb.onclick = save;
  sr.appendChild(sb);
  c.appendChild(sr);
  wrap.appendChild(c);

  const lc = card("Logo", "PNG、JPEG、WebP，1 MB 以內；不收 SVG");
  const now = el("div", "small muted");
  const paint = () => {
    now.textContent = S.brand && S.brand.logo
      ? `目前使用 ${S.brand.logo.width}×${S.brand.logo.height} 像素的 Logo`
      : "還沒有上傳 Logo，頁首用單位名稱的第一個字代替";
    rm.hidden = !(S.brand && S.brand.logo);
  };
  const file = Object.assign(el("input"), { type: "file", accept: "image/png,image/jpeg,image/webp" });
  file.hidden = true;
  const pick = el("button", "btn sm primary", "選擇圖檔上傳");
  pick.onclick = () => file.click();
  const rm = el("button", "btn sm", "移除 Logo");
  file.onchange = async () => {
    const f = file.files && file.files[0];
    file.value = "";
    if (!f) return;
    if (f.size > 1024 * 1024) { toast("Logo 要在 1 MB 以內", "bad"); return; }
    try {
      const r = await api("/api/branding/logo", { method: "POST", body: { data: await readB64(f) } });
      applyBranding(r.branding); live(); paint();
      toast("Logo 已更換", "ok");
    } catch (ex) { toast(ex.message, "bad"); }
  };
  rm.onclick = async () => {
    try {
      const r = await api("/api/branding/logo", { method: "POST", body: { remove: true } });
      applyBranding(r.branding); live(); paint();
      toast("已移除 Logo", "ok");
    } catch (ex) { toast(ex.message, "bad"); }
  };
  lc.appendChild(now);
  const lr = el("div", "slot-actions");
  lr.append(pick, rm, file);
  lc.appendChild(lr);
  lc.appendChild(el("p", "small muted", "建議用橫式、透明背景、高度 64 像素以上的圖。Logo 一律放在白底上顯示，深色模式也看得清楚。瀏覽器分頁的小圖示用單位名稱第一個字產生。"));
  paint();
  wrap.appendChild(lc);
}

function render() {
  if (!S.fleet) return;
  S.live = null;                 // 換頁就清掉上一頁的局部更新
  renderNav();
  const v = S.view;
  if (v === "home" || v === "overview") viewHome();
  else if (v === "models") viewLib();
  else if (v === "run") viewRun();
  else if (v.startsWith("node:")) viewNode();
  else if (v === "apis") viewApis();
  else if (v.startsWith("api:")) viewApi();
  else if (v === "chat") viewChat();
  else if (v === "agent") viewAgent();
  else if (v === "image") viewCreate("image");
  else if (v === "video") viewCreate("video");
  else if (v === "settings") viewSettings();
  else viewHome();
}


/* ================================================================ 機器的納管與設定：全部在網頁上做完
   加一台機器（一次性安裝指令）→ 這台的設定（記憶體架構、底線、存放區、llama-server）→ 新增欄位 → 放入模型。
   以前這些都要工程師跑部署腳本或手改 registry.json／fleet.json，管理者看得到機器卻不知道它怎麼來的。 */

const MEM_LABEL = {
  unified_split: "統一記憶體・切兩塊（AMD AI 395 這類）",
  unified_single: "統一記憶體・單一池（DGX Spark、Jetson）",
  discrete: "獨立顯示記憶體（一般 GPU 伺服器）",
};

function fieldBox(label, input, hint) {
  const d = el("div");
  d.appendChild(el("label", "field", label));
  d.appendChild(input);
  if (hint) d.appendChild(el("div", "assume", hint));
  return d;
}
function textInput(v, ph) { const i = el("input"); i.type = "text"; i.value = v ?? ""; if (ph) i.placeholder = ph; return i; }
function numInput(v, ph) { const i = el("input"); i.type = "number"; i.step = "any"; i.min = "0"; i.value = v ?? ""; if (ph) i.placeholder = ph; return i; }
function selectInput(opts, v) {
  const s = el("select");
  opts.forEach(([val, t]) => { const o = el("option", null, t); o.value = val; s.appendChild(o); });
  if (v !== undefined && opts.some(([val]) => val === v)) s.value = v;
  return s;
}
function groupInput(v) {
  const i = textInput(v, "例：NVIDIA 伺服器");
  const id = "grp-" + Math.random().toString(36).slice(2, 8);
  const dl = el("datalist");
  dl.id = id;
  [...new Set((S.fleet?.nodes || []).map((n) => n.group).filter(Boolean))].forEach((g) => { const o = el("option"); o.value = g; dl.appendChild(o); });
  i.setAttribute("list", id);
  return [i, dl];
}
// 送出失敗時彈窗留著、錯誤顯示在窗內（modal() 的確定鈕會先關窗，填了半天的表單會蒸發）
function formButton(ov, text, cls, fn) {
  const b = el("button", "btn " + cls, text);
  b.onclick = async () => { b.disabled = true; try { await fn(); } finally { b.disabled = false; } };
  ov.querySelector(".actions").appendChild(b);
  return b;
}
function errNote(box, msg) {
  box.textContent = "";
  if (msg) box.appendChild(el("p", "note bad", msg));
}

async function machineInfo(nid, fresh) {
  S.minfo = S.minfo || {};
  if (!fresh && S.minfo[nid]) return S.minfo[nid];
  S.minfo[nid] = await api(`/api/node/${nid}/machine`);
  return S.minfo[nid];
}

function openAddMachine(group) {
  const body = el("div");
  body.appendChild(el("p", "small muted",
    "主控台會產生一行安裝指令。到要加入的機器上貼上執行，它會裝好節點代理、自動出現在這裡。一間教室可以共用同一行指令：設好可用次數與名稱樣板，每台貼上同一行就好。"));
  const form = el("div", "form-row");
  const fName = textInput("", "選填，例：A{01..20}；不填就用各台的主機名稱");
  const [fGroup, dl] = groupInput(group || "");
  const fHost = textInput(location.host);
  const fUses = numInput(1);
  fUses.step = "1"; fUses.min = "1"; fUses.max = "1000";
  const fMin = numInput(30);
  fMin.step = "1"; fMin.min = "5"; fMin.max = "10080";
  const fCidr = textInput("", "選填，例：198.51.100.0/24");
  const fReplace = selectInput([["", "不是，這是新的一台"], ...(S.fleet?.nodes || []).map((x) => [x.id, `${x.name}（${x.host}）`])], "");
  const replaceBox = fieldBox("這是哪一台重新安裝？", fReplace, "代理重裝或換了 IP 時選它，加入後會更新那一筆，名稱與群組不變；新機器不用選");
  fUses.oninput = () => { replaceBox.hidden = Number(fUses.value) > 1; if (replaceBox.hidden) fReplace.value = ""; };
  form.append(fieldBox("名稱", fName, "多台時用樣板：A{01..20} 依加入順序給 A01、A02…"), fieldBox("群組", fGroup),
    fieldBox("可用次數", fUses, "這行指令最多能加入幾台"), fieldBox("有效時間（分鐘）", fMin, "5 分鐘到 7 天（10080 分鐘）"),
    fieldBox("只收這些網段", fCidr, "選填；填了之後，網段以外的機器拿到指令也加入不了"),
    fieldBox("主控台位址", fHost, "要加入的那台機器連得到的位址（IP:埠）"), replaceBox);
  body.append(form, dl);
  const out = el("div");
  body.appendChild(out);
  const ov = modal({ title: "加一台機器", bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide");
  let run = 0;
  const gen = formButton(ov, "產生安裝指令", "primary", async () => {
    let r;
    try {
      r = await api("/api/nodes/join-code", { method: "POST", body: { name: fName.value.trim(), group: fGroup.value.trim(), console: fHost.value.trim(), replace: fReplace.value,
        uses: Math.round(Number(fUses.value) || 1), minutes: Math.round(Number(fMin.value) || 30), cidrs: fCidr.value.trim() } });
    } catch (ex) { errNote(out, ex.message); return; }
    const me = ++run;                       // 重新產生時，舊指令的等待就不再更新畫面
    gen.textContent = "重新產生";
    out.textContent = "";
    // 依那台的作業系統給指令：Linux 與 macOS 同一行 curl；Windows 是 PowerShell
    const osBar = el("div", "tabbar");
    const osBox = el("div");
    const showOs = (k) => {
      S.joinOs = k;
      osBar.querySelectorAll("button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.k === k)));
      osBox.textContent = "";
      const need = el("ul", "small muted need-list");
      if (k === "win") {
        osBox.appendChild(el("h4", "pick-h", "在那台開「Windows PowerShell」，貼上執行（建議按右鍵「以系統管理員身分執行」）"));
        osBox.appendChild(copyRow("安裝指令", r.command_windows));
        const det = el("details", "small");
        det.appendChild(el("summary", null, "這行指令實際做什麼（解碼後的原文）"));
        det.appendChild(el("pre", "log", r.command_windows_text));
        osBox.appendChild(det);
        ["以系統管理員身分執行：裝成開機自動啟動（不必有人登入），並在防火牆開 9401（主控台連代理）與 8090～8289（模型 API）；"
          + "一般身分執行：只在這個帳號登入後才跑，防火牆要另外請管理員開",
        "那台要有 Python 3.9 以上；沒有的話會問要不要用 winget 自動安裝",
        "llama.cpp 不用自己編：加入後在「這台的設定」按一鍵安裝，會依顯示卡下載官方預編譯版（NVIDIA 用 CUDA、AMD／Intel 用 Vulkan）",
        "下載時只接受主控台這一張憑證（比對整張憑證的 SHA-256），不是關掉驗證"].forEach((t) => need.appendChild(el("li", null, t)));
      } else {
        osBox.appendChild(el("h4", "pick-h", "到那台機器上，用一般帳號（不要用 root）開終端機貼上執行"));
        osBox.appendChild(copyRow("安裝指令", r.command));
        ["Ubuntu：要有 python3、curl、openssl（預設都有）；會建立 systemd 使用者服務並設成開機自啟",
          "macOS（Apple Silicon）：只要有 python3（沒有的話先執行 xcode-select --install）；會建立 launchd 服務，登入後自動啟動。"
            + "第一次會問是否允許 python3 接受傳入連線，請按允許",
          `那台要連得到 ${fHost.value.trim()}；主控台要連得到那台的 9401 埠，有防火牆要先開`,
          "最後把代理的憑證與權杖送回主控台；下載時釘住主控台憑證的公鑰，不是關掉驗證"].forEach((t) => need.appendChild(el("li", null, t)));
      }
      osBox.appendChild(need);
    };
    [["unix", "Linux／macOS"], ["win", "Windows"]].forEach(([k, label]) => {
      const b = el("button", null, label);
      b.dataset.k = k;
      b.onclick = () => showOs(k);
      osBar.appendChild(b);
    });
    out.append(osBar, osBox);
    showOs(S.joinOs || "unix");
    const until = new Date(r.expires_at * 1000).toLocaleString(uiLocale(), { hour12: false, timeZone: "Asia/Taipei" });
    out.appendChild(el("p", "small muted", r.uses > 1
      ? `這行指令可以加入 ${r.uses} 台，${until}（台北時間）前有效。${r.names.length ? `名稱依加入順序：${r.names[0]}～${r.names[r.names.length - 1]}。` : ""}指令只顯示這一次，關掉視窗後請到「設定／群組」的加入碼清單查進度或作廢。`
      : `這行指令只能加入 1 台，${until}（台北時間）前有效。指令只顯示這一次。`));
    const st = el("p", "note", "等待那台執行…（這個視窗開著會自動更新）");
    out.appendChild(st);
    let seen = 0;
    const poll = async () => {
      if (!ov.isConnected || me !== run) return;
      let j;
      try { j = await api(`/api/nodes/join-code/${r.id}`); } catch (_) { setTimeout(poll, 3000); return; }
      if (!ov.isConnected || me !== run) return;
      if (j.uses > 1 && j.state === "waiting") {
        st.className = j.error ? "note bad" : "note";
        st.textContent = `已加入 ${j.used}／${j.uses} 台${j.nodes.length ? "：" + j.nodes.map((x) => x.name).join("、") : ""}` + (j.error ? `。最近一次失敗：${j.error}` : "。其他台貼上同一行就好（這個視窗開著會自動更新）");
        if (j.used > seen) { seen = j.used; refresh(); }   // 有新的一台加入才重抓清單，不每 3 秒重畫
        setTimeout(poll, 3000);
        return;
      }
      if (j.state === "joined" && j.uses > 1) {
        st.className = "note ok";
        st.textContent = `${j.uses} 台都加入了：${j.nodes.map((x) => x.name).join("、")}`;
        await refresh();
        gen.remove();
        return;
      }
      if (j.state === "revoked") { st.className = "note bad"; st.textContent = "這個加入碼已作廢"; return; }
      if (j.state === "joined") {
        st.className = "note ok";
        st.textContent = "已加入。" + (j.linger === false ? "提醒：那台還沒開 linger，帳號登出後代理會停，請照安裝畫面最後一行處理。" : "");
        await refresh();
        const next = el("button", "btn primary", "到這台設定記憶體與欄位");
        next.onclick = () => { ov.remove(); go("node:" + j.node_id); openMachineSettings(j.node_id); };
        ov.querySelector(".actions").appendChild(next);
        gen.remove();
        return;
      }
      if (j.state === "expired") { st.className = "note bad"; st.textContent = "加入碼過期了，請按「重新產生」"; return; }
      st.className = j.error ? "note bad" : "note";
      st.textContent = j.error ? `那台執行了，但還沒加入成功：${j.error}` : "等待那台執行…（這個視窗開著會自動更新）";
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  });
  [fName, fGroup, fHost, fCidr].forEach((i) => imeGuard(i, () => gen.click()));
}

const JOIN_STATE = { waiting: "可用", joined: "已用完", expired: "已過期", revoked: "已作廢" };

async function joinCodesCard() {
  const c = card("加入碼", "看每個加入碼加入了哪些機器；還沒用完的可以作廢");
  const add = el("div", "slot-actions");
  const nb = el("button", "btn sm primary", "產生加入碼");
  nb.onclick = () => openAddMachine();
  add.appendChild(nb);
  c.appendChild(add);
  let d;
  try { d = await api("/api/nodes/join-codes"); } catch (ex) { c.appendChild(el("p", "note warn", ex.message)); return c; }
  if (!d.codes.length) { c.appendChild(el("p", "small muted", "還沒有加入碼。產生後指令只顯示一次，這裡只看得到進度，看不到碼本身。")); return c; }
  const tbl = el("table", "api-table");
  const hr = el("tr");
  ["產生時間（台北時間）", "群組", "名稱", "已用", "到期（台北時間）", "狀態", "加入的機器", ""].forEach((h) => hr.appendChild(el("th", null, h)));
  tbl.appendChild(hr);
  const t = (x) => new Date(x * 1000).toLocaleString(uiLocale(), { hour12: false, timeZone: "Asia/Taipei" });
  d.codes.forEach((j) => {
    const tr = el("tr");
    tr.appendChild(el("td", "nowrap", t(j.created)));
    tr.appendChild(el("td", null, j.group || "未分類"));
    tr.appendChild(el("td", "mono", j.template || j.name || "主機名稱"));
    tr.appendChild(el("td", "nowrap", `${j.used}／${j.uses}`));
    tr.appendChild(el("td", "nowrap", t(j.expires_at)));
    const sc = el("td", "nowrap", JOIN_STATE[j.state] || j.state);
    if (j.cidrs.length) sc.appendChild(el("div", "small muted", "限 " + j.cidrs.join("、")));
    if (j.error && j.state === "waiting") sc.appendChild(el("div", "small bad-t", j.error));
    tr.appendChild(sc);
    tr.appendChild(el("td", "small", j.nodes.map((x) => x.name).join("、") || "—"));
    const td = el("td", "nowrap");
    if (j.state === "waiting") {
      const rb = el("button", "btn sm", "作廢");
      rb.onclick = () => modal({
        title: "作廢加入碼", hasInput: false, danger: true, confirmText: "作廢",
        bodyNode: el("p", "small muted", `作廢後，還沒執行這行指令的機器就加入不了；已加入的 ${j.used} 台不受影響。`),
        onConfirm: async () => {
          try { await api(`/api/nodes/join-code/${j.id}/revoke`, { method: "POST" }); toast("已作廢", "ok"); } catch (ex) { toast(ex.message, "bad"); }
          render();
        },
      });
      td.appendChild(rb);
    }
    tr.appendChild(td);
    tbl.appendChild(tr);
  });
  const tw = el("div", "md-table-wrap");
  tw.appendChild(tbl);
  c.appendChild(tw);
  return c;
}

async function openMachineSettings(nid) {
  const n = S.fleet.nodes.find((x) => x.id === nid);
  if (!n) return;
  const body = el("div");
  loading(body);
  const ov = modal({ title: `${n.name} 的設定`, bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide");
  let rec, info = null, infoErr = "";
  try { rec = (await api("/api/nodes")).nodes.find((x) => x.id === nid); } catch (ex) { errNote(body, ex.message); return; }
  if (n.online) { try { info = await machineInfo(nid, true); } catch (ex) { infoErr = ex.message; } }
  body.textContent = "";

  // 桌面版（一般人單機用）：位址、埠、代理憑證是管理一群機器才會動的，收起來（值照樣送回，不影響儲存）
  const basic = el("div");
  basic.hidden = !!S.appWindow;
  body.appendChild(basic);
  basic.appendChild(el("h4", "pick-h", "基本"));
  const f1 = el("div", "form-row");
  const fName = textInput(rec.name);
  const [fGroup, dl] = groupInput(rec.group);
  const fHost = textInput(rec.host);
  const fPort = numInput(rec.agent_port || 9401);
  f1.append(fieldBox("名稱", fName), fieldBox("群組", fGroup), fieldBox("位址", fHost, "主控台連這台代理用的 IP 或主機名稱；改了會先實際連一次，連不上就不改"),
    fieldBox("代理的埠", fPort));
  basic.append(f1, dl);
  const tr = el("div", "slot-actions");
  tr.appendChild(el("span", "small muted", rec.trusted ? `信任的代理憑證：${(rec.trust_fingerprint || "").slice(0, 23)}…` : "還沒信任這台的代理憑證"));
  tr.appendChild(el("span", "spacer"));
  const tb = el("button", "btn sm", "匯入新憑證（代理重裝後用）");
  tb.onclick = () => importTrust(rec);
  tr.appendChild(tb);
  basic.appendChild(tr);

  let fMM, fRam, fVram, fBw, fRbw, fRoot, fRootC, fLlama, fLlamaC, fSd, fSdC, fNote, fAgent, fGpuMode, fGpuName, fGpuVendor, fGpuVram, fHfMirror, fGhProxy, fDesk;
  if (!info) {
    body.appendChild(el("p", "note warn", `這台現在連不上，只能改上面的基本資料；記憶體與模型存放等連上之後再設定。${infoErr}`));
  } else {
    const st = info.settings;
    body.appendChild(el("h4", "pick-h", "記憶體"));
    const f2 = el("div", "form-row");
    fMM = selectInput(Object.entries(MEM_LABEL), st.memory_model);
    fRam = numInput(st.ram_floor_gib);
    fVram = numInput(st.vram_floor_gib);
    const ph = () => {
      const d = info.defaults[fMM.value] || {};
      fRam.placeholder = `預設 ${d.ram ?? "—"}`;
      fVram.placeholder = `預設 ${d.vram ?? "—"}`;
    };
    fMM.onchange = ph;
    ph();
    const det = info.detect;
    f2.append(fieldBox("記憶體架構", fMM, `偵測建議：${MEM_LABEL[det.memory_model]}。${det.why}`),
      fieldBox("系統記憶體底線（GiB）", fRam, "載入後系統記憶體至少要剩這麼多；空白＝預設"),
      fieldBox("顯示記憶體底線（GiB）", fVram, "多張卡時照卡的大小分到每張；空白＝預設"));
    body.appendChild(f2);
    if (st.memory_model !== det.memory_model) {
      const b = el("button", "btn sm", "改成偵測建議");
      b.onclick = () => { fMM.value = det.memory_model; ph(); };
      body.appendChild(b);
    }
    body.appendChild(el("p", "small muted",
      `這台：${det.gpus.length ? det.gpus.map((g) => `卡 ${g.id} ${g.name || ""}${g.vram_gib ? " " + g.vram_gib + " GiB" : ""}`).join("、") : "沒有偵測到 GPU"}；系統記憶體 ${det.ram_gib} GiB`));
    // 手動指定顯示卡：偵測不到（例：Windows 的內顯被當成沒有）或顯存讀錯時用
    const ovr = st.gpu_override;
    const fg = el("div", "form-row");
    fGpuMode = selectInput([["auto", "自動偵測（建議）"], ["cpu", "不用 GPU，只用 CPU"], ["manual", "手動指定一張卡"]],
      ovr === "cpu" ? "cpu" : ovr ? "manual" : "auto");
    fGpuName = textInput(ovr?.name || "", "例：AMD Radeon 880M");
    fGpuName.maxLength = 80;
    fGpuVendor = selectInput([["amd", "AMD"], ["intel", "Intel"], ["nvidia", "NVIDIA"], ["apple", "Apple"]], ovr?.vendor || "amd");
    fGpuVram = numInput(ovr?.vram_gib ?? "", "GiB");
    fGpuVram.min = "0.5";
    const manualBoxes = [fieldBox("顯示卡名稱", fGpuName), fieldBox("廠牌", fGpuVendor, "決定裝哪一版推理引擎（AMD／Intel 走 Vulkan）"),
      fieldBox("顯存（GiB）", fGpuVram, "內顯填專用記憶體；Windows 的內顯另外會借用一半系統記憶體，不必加進來")];
    const syncGpu = () => manualBoxes.forEach((b) => { b.hidden = fGpuMode.value !== "manual"; });
    fGpuMode.onchange = syncGpu;
    fg.append(fieldBox("顯示卡", fGpuMode, "偵測錯了才改。改了之後推薦、記憶體試算與推理引擎版本都照這裡算"), ...manualBoxes);
    syncGpu();
    body.appendChild(fg);

    // 推薦清單估速度用：每產生一個 token 都要把權重從記憶體讀一遍，速度上限看頻寬
    const ch = info.chip || {};
    const f2b = el("div", "form-row");
    fBw = numInput(st.bandwidth_gbs, ch.bandwidth_spec_gbs ? `規格書 ${ch.bandwidth_spec_gbs}` : "認不出晶片，請填");
    fRbw = numInput(st.ram_bandwidth_gbs, "沒填時假設 50");
    f2b.append(fieldBox("記憶體頻寬（GB/s）", fBw,
      `${ch.gpu ? "偵測到：" + ch.gpu + "。" : ""}推薦模型的預估速度照這個算；空白＝用規格書標稱值`),
    fieldBox("系統記憶體頻寬（GB/s）", fRbw, "只有獨立顯示卡機會用到：放不下的 MoE 專家搬到系統記憶體時，那部分照這個算"));
    body.appendChild(f2b);

    body.appendChild(el("h4", "pick-h", "模型存放"));
    const f3 = el("div", "form-row");
    const disk = (x) => `${x.hdd ? "機械硬碟，大模型載入會很慢" : "SSD"}・剩 ${fmt(x.free_gib, 0)} GiB`;
    const defDisk = info.storage.find((x) => x.path === info.effective.gguf_root);
    const rootOpts = [["", `預設：${info.effective.gguf_root}${defDisk ? "（" + disk(defDisk) + "）" : ""}`],
      ...info.storage.filter((x) => x !== defDisk).map((x) => [x.path, `${x.path}（${disk(x)}）`]),
      ["__custom", "自訂路徑…"]];
    const curRoot = st.gguf_root || "";
    fRoot = selectInput(rootOpts, rootOpts.some(([v]) => v === curRoot) ? curRoot : "__custom");
    fRootC = textInput(rootOpts.some(([v]) => v === curRoot) ? "" : curRoot, "例：/media/data/models/gguf");
    fRootC.hidden = fRoot.value !== "__custom";
    fRoot.onchange = () => { fRootC.hidden = fRoot.value !== "__custom"; };
    const rootBox = fieldBox("存放區", fRoot, "一顆模型一個資料夾；建議放 SSD。換了位置，舊位置的模型不會自動搬過去");
    rootBox.appendChild(fRootC);
    const llOpts = [["", `自動挑選（目前：${info.effective.llama_server || "找不到"}）`],
      ...info.llama.map((x) => [x.path, `${x.path}（${x.build ? "build " + x.build + "・" : ""}${x.backend}${x.ok ? "" : "・跑不起來"}）`]),
      ["__custom", "自訂路徑…"]];
    const curL = st.llama_server || "";
    fLlama = selectInput(llOpts, llOpts.some(([v]) => v === curL) ? curL : "__custom");
    fLlamaC = textInput(llOpts.some(([v]) => v === curL) ? "" : curL, "例：/opt/llama.cpp/build/bin/llama-server");
    fLlamaC.hidden = fLlama.value !== "__custom";
    fLlama.onchange = () => { fLlamaC.hidden = fLlama.value !== "__custom"; };
    const llBox = fieldBox("llama-server", fLlama, "GGUF 模型靠它跑；自動挑選會用家目錄下版本最新、有 GPU 加速的那支");
    llBox.appendChild(fLlamaC);
    const sdOpts = [["", `自動挑選（目前：${info.effective.sd_cli || "還沒安裝"}）`],
      ...(info.sd || []).map((x) => [x.path, `${x.path}${x.build ? "（master-" + x.build + "）" : ""}`]), ["__custom", "自訂路徑…"]];
    const curS = st.sd_cli || "";
    fSd = selectInput(sdOpts, sdOpts.some(([v]) => v === curS) ? curS : "__custom");
    fSdC = textInput(sdOpts.some(([v]) => v === curS) ? "" : curS, "例：/opt/stable-diffusion.cpp/build/bin/sd-cli");
    fSdC.hidden = fSd.value !== "__custom";
    fSd.onchange = () => { fSdC.hidden = fSd.value !== "__custom"; };
    const sdBox = fieldBox("sd-cli（畫圖與影片）", fSd, `畫圖模型放在 ${info.effective.sd_root}（跟存放區同一層）；在模型庫的「畫圖與影片」可以一鍵安裝`);
    sdBox.appendChild(fSdC);
    f3.append(rootBox, llBox, sdBox);
    body.appendChild(f3);
    const bb = el("div", "slot-actions");
    if (!info.llama.length && !st.llama_server) {
      bb.appendChild(el("span", "small bad-t", "這台還沒有安裝推理引擎（llama.cpp），模型還不能啟動。"));
    }
    bb.appendChild(el("span", "spacer"));
    // Windows／macOS 是下載官方預編譯版、Linux 才在這台編譯：按鈕不說「編譯」，交給安裝視窗依系統說明
    const bbtn = el("button", "btn sm" + (info.llama.length ? "" : " primary"), info.llama.length ? "更新推理引擎" : "安裝推理引擎");
    bbtn.onclick = () => openEngineBuild(nid);
    bb.appendChild(bbtn);
    body.appendChild(bb);
    // 下載來源：連不上 Hugging Face 或 GitHub 的網路環境用（例：公司防火牆、對岸）
    body.appendChild(el("h4", "pick-h", "下載來源"));
    const fd = el("div", "form-row");
    fHfMirror = textInput(st.hf_mirror || "", "空白＝官方 https://huggingface.co");
    fGhProxy = textInput(st.github_proxy || "", "空白＝直連 GitHub");
    fd.append(fieldBox("Hugging Face 鏡像", fHfMirror, "例：https://hf-mirror.com。走鏡像時不會送出 Hugging Face 令牌（鏡像是第三方）"),
      fieldBox("GitHub 加速字首", fGhProxy, "例：https://ghfast.top/。只用在下載推理引擎；版本與 sha256 仍直接問 GitHub，對不上就不裝"));
    body.appendChild(fd);
    // Agent：會在這台建檔、跑指令，預設關閉；打開後只能動 ~/xconsole-agent/ 底下，每條指令要人按允許
    body.appendChild(el("h4", "pick-h", "Agent"));
    const agRow = el("label", "check-row");
    fAgent = el("input");
    fAgent.type = "checkbox";
    fAgent.checked = !!(st.agent_enabled ?? info.agent_enabled);
    agRow.append(fAgent, document.createTextNode(" 允許 Agent 在這台工作（建檔、寫程式、跑指令）"));
    body.append(agRow, el("p", "small muted", "檔案只能動工作資料夾裡面：~/xconsole-agent/ 底下的，或在 Agent 頁連結進來的資料夾（系統資料夾、家目錄本身、.ssh 這類設定夾不能連）。指令以代理的身分執行，每條都要人在網頁上按允許，並寫進記錄。不需要就保持關閉。"));
    body.appendChild(el("h4", "pick-h", "操作電腦"));
    const pcRow = el("label", "check-row");
    fDesk = el("input");
    fDesk.type = "checkbox";
    fDesk.checked = !!st.desktop_enabled;
    pcRow.append(fDesk, document.createTextNode(" 讓模型操作這台的桌面（看螢幕、控制滑鼠鍵盤）"));
    const dk = info.desktop || {};
    body.append(pcRow, el("p", "small " + (dk.how ? "muted" : "warn-t"), dk.how
      ? `${dk.how === "helper" ? "由桌面小幫手代為操作（使用者登入後才有桌面）" : "代理直接操作這台的桌面"}。對話頁按「操作電腦」使用；每一步預設都要人按允許，每個動作寫進記錄。不需要就保持關閉。`
      : `這台現在不能操作桌面：${dk.why || "沒有圖形桌面"}`));
    body.appendChild(el("h4", "pick-h", "備註"));
    fNote = el("textarea");
    fNote.value = st.note || "";
    fNote.placeholder = "給接手的人看：這台是做什麼的、有什麼要小心";
    body.appendChild(fNote);
  }
  const err = el("div");
  body.appendChild(err);
  [fName, fGroup, fHost, fPort].forEach((i) => imeGuard(i, () => {}));
  formButton(ov, "儲存", "primary", async () => {
    try {
      await api(`/api/nodes/${nid}/update`, { method: "POST", body: { name: fName.value.trim(), group: fGroup.value.trim(), host: fHost.value.trim(), agent_port: Number(fPort.value) } });
      if (info) {
        await api(`/api/node/${nid}/machine`, { method: "POST", body: {
          memory_model: fMM.value, ram_floor_gib: fRam.value, vram_floor_gib: fVram.value, note: fNote.value,
          bandwidth_gbs: fBw.value, ram_bandwidth_gbs: fRbw.value,
          gguf_root: fRoot.value === "__custom" ? fRootC.value.trim() : fRoot.value,
          llama_server: fLlama.value === "__custom" ? fLlamaC.value.trim() : fLlama.value,
          sd_cli: fSd.value === "__custom" ? fSdC.value.trim() : fSd.value, agent_enabled: fAgent.checked,
          gpu_override: fGpuMode.value === "manual" ? { name: fGpuName.value.trim(), vendor: fGpuVendor.value, vram_gib: Number(fGpuVram.value) } : fGpuMode.value,
          hf_mirror: fHfMirror.value.trim(), github_proxy: fGhProxy.value.trim(), desktop_enabled: fDesk.checked } });
      }
    } catch (ex) { errNote(err, ex.message); return; }
    ov.remove();
    toast("已儲存", "ok");
    if (S.minfo) delete S.minfo[nid];
    if (S.rec) delete S.rec[nid];          // 架構、頻寬、存放區都會改變推薦結果
    await refresh();
    render();
  });
}

function removeMachine(rec) {
  const d = el("div");
  d.appendChild(el("p", "note warn", `${rec.name} 會從主控台移除，之後不再顯示、不再取樣。那台機器上的代理、模型與欄位都不會被動。`));
  d.appendChild(el("p", "small muted", "之後要再加回來，按「加一台機器」在那台重跑一次安裝指令即可，欄位與模型都還在。"));
  modal({ title: `移除 ${rec.name}`, bodyNode: d, confirmText: "移除", danger: true, onConfirm: async () => {
    try { await api(`/api/nodes/${rec.id}/remove`, { method: "POST", body: {} }); toast(`已移除 ${rec.name}`, "ok"); } catch (ex) { toast(ex.message, "bad"); }
    await refresh();
    render();
  } });
}

// 新增或修改一個欄位。欄位只收白名單欄位：記錄檔與設定檔的路徑不能從網頁改（改得到就能讀任意檔案）
async function openSlotEditor(n, s) {
  const isNew = !s;
  const engine = s && (s.kind === "strata" || s.kind === "llama");
  const body = el("div");
  loading(body);
  const ov = modal({ title: isNew ? `${n.name} 新增欄位` : `${s.role}・${s.tier || ""} 的設定`, bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide");
  let units = [];
  if (isNew) { try { units = (await machineInfo(n.id, true)).units || []; } catch (ex) { errNote(body, ex.message); return; } }
  body.textContent = "";

  let type = "empty";
  const svcRow = el("div", "form-row");
  const fUnit = selectInput(units.map((u) => [u, u]));
  if (isNew) {
    const pick = el("div", "pick-list");
    const opt = (val, title, why, disabled) => {
      const l = el("label", "pick");
      const r = el("input");
      r.type = "radio";
      r.name = "slot-type";
      r.checked = val === type;
      r.disabled = disabled;
      r.onchange = () => { type = val; svcRow.hidden = val !== "service"; portBox.hidden = val !== "service"; modelBox.hidden = val !== "service"; };
      l.appendChild(r);
      const t = el("div", "pick-main");
      t.appendChild(el("div", "model", title));
      t.appendChild(el("div", "small muted", why));
      l.appendChild(t);
      pick.appendChild(l);
    };
    opt("empty", "空欄位", "之後到模型庫挑一顆 GGUF 放進來，xConsole 會替它建服務、配埠、算記憶體");
    opt("service", "登記這台已經在跑的服務", units.length ? "例如瀏覽器、語音、代理程式這類 systemd 使用者服務；登記後可以在這裡啟停、看記錄、算進記憶體"
      : "這台沒有可以登記的使用者服務（已登記的、xConsole 自己的都不列）", !units.length);
    body.appendChild(pick);
    svcRow.appendChild(fieldBox("服務", fUnit, "systemctl --user 看得到的服務"));
    svcRow.hidden = true;
    body.appendChild(svcRow);
  }
  const f = el("div", "form-row");
  const fRole = textInput(s?.role, "例：主腦、嵌入、語音辨識");
  const fTier = textInput(s?.tier, "例：高、低、眼睛");
  const fModel = textInput(s?.model, "畫面上顯示的名稱");
  const fPort = numInput(s?.port);
  const fCaps = textInput((s?.caps || []).join("、"), "例：文字、看圖（用頓號分開）");
  const fV = numInput(s?.est_vram_gib, "0");
  const fR = numInput(s?.est_ram_gib, "0");
  const modelBox = fieldBox("顯示名稱", fModel);
  const portBox = fieldBox("埠", fPort, "這個服務聽的埠（選填）");
  modelBox.hidden = portBox.hidden = engine || (isNew && type !== "service") || (!isNew && !s.unit);
  f.append(fieldBox("角色", fRole), fieldBox("等級", fTier, "選填"), modelBox, portBox, fieldBox("能力標籤", fCaps, "選填，最多 6 個"),
    fieldBox("預估顯示記憶體（GiB）", fV, engine ? "放入模型時會自動算，通常不用改" : "准入試算用，選填"),
    fieldBox("預估系統記憶體（GiB）", fR, "准入試算用，選填"));
  body.appendChild(f);
  if (engine) body.appendChild(el("p", "small muted", "這是模型引擎的欄位：埠、上下文、金鑰在「API 串接」頁改，模型用「換模型」換。"));
  const crit = el("label", "gpu-opt check-row");
  const fCrit = el("input");
  fCrit.type = "checkbox";
  fCrit.checked = !!s?.critical;
  crit.append(fCrit, document.createTextNode(" 關鍵服務：停止、重啟、換模型前要多確認一次；「全部停止」會略過它"));
  body.appendChild(crit);
  const fNote = el("textarea");
  fNote.value = s?.note || "";
  fNote.placeholder = "給接手的人看：這格是做什麼的、停掉會影響誰";
  body.appendChild(fieldBox("備註", fNote));
  const err = el("div");
  body.appendChild(err);
  [fRole, fTier, fModel, fCaps].forEach((i) => imeGuard(i, () => {}));

  if (!isNew) {
    const del = el("button", "btn danger", "移除這個欄位");
    const running = s.state === "active" || s.state === "activating";
    del.disabled = running;
    del.title = running ? "服務還在跑，先停止再移除" : "";
    del.onclick = () => {
      const d = el("div");
      d.appendChild(el("p", "note warn", s.kind === "llama"
        ? "會一起刪掉 xConsole 替這格建的 llama.cpp 服務與它的設定、金鑰檔。模型檔留在存放區，不會被刪。"
        : s.unit ? `只是取消登記，${s.unit} 這個服務本身不會被動。` : "這是空欄位，移除後就不會再出現。"));
      // 疊在欄位設定窗上面的確認窗：不給點背景關（點偏了會連底下填到一半的設定窗一起誤會）
      modal({ title: `移除 ${s.role}・${s.tier || ""}`, bodyNode: d, confirmText: "移除", danger: true, hasInput: true, onConfirm: async () => {
        try { await api(`/api/node/${n.id}/slot/${s.id}/delete`, { method: "POST", body: {} }); toast("已移除", "ok"); ov.remove(); } catch (ex) { toast(ex.message, "bad"); }
        await refresh();
      } });
    };
    ov.querySelector(".actions").prepend(del);
  }
  formButton(ov, isNew ? "新增" : "儲存", "primary", async () => {
    const payload = {
      role: fRole.value.trim(), tier: fTier.value.trim(), note: fNote.value, critical: fCrit.checked,
      caps: fCaps.value.split(/[、,，\s]+/).map((x) => x.trim()).filter(Boolean),
      est_vram_gib: fV.value, est_ram_gib: fR.value,
    };
    if (!modelBox.hidden) payload.model = fModel.value.trim();
    if (!portBox.hidden) payload.port = fPort.value;
    try {
      if (isNew) await api(`/api/node/${n.id}/slots/add`, { method: "POST", body: { type, unit: type === "service" ? fUnit.value : undefined, ...payload } });
      else await api(`/api/node/${n.id}/slot/${s.id}/edit`, { method: "POST", body: payload });
    } catch (ex) { errNote(err, ex.message); return; }
    ov.remove();
    toast(isNew ? "已新增欄位" : "已儲存", "ok");
    await refresh();
  });
}


// 情境：一組 Strata 參數一鍵切換。網頁上只開放上下文長度、並行數、自動縮輸出上限三項
function openProfiles(n) {
  const st = n.state;
  const body = el("div");
  body.appendChild(el("p", "small muted", "情境是一組參數，按一下就把 Strata 欄位切過去（會改設定並重啟，失敗自動還原）。例如寫程式要長上下文、抓料要多並行。"));
  const list = el("div", "pick-list");
  const profs = Object.entries(st.profiles || {});
  if (!profs.length) list.appendChild(el("p", "note", "這台還沒有情境。"));
  let ov;
  profs.forEach(([key, p]) => {
    const r = el("div", "pick");
    const t = el("div", "pick-main");
    t.appendChild(el("div", "model", p.name + (st.machine.profile === key ? "（目前）" : "")));
    t.appendChild(el("div", "small muted", p.why || ""));
    r.appendChild(t);
    const e = el("button", "btn sm", "修改");
    e.onclick = () => { ov.remove(); openProfileEditor(n, key, p); };
    const d = el("button", "btn sm danger", "刪除");
    d.onclick = () => modal({ title: `刪除情境「${p.name}」`, bodyNode: el("p", "small muted", "只刪掉這個情境，服務目前的設定不會變。"),
      confirmText: "刪除", danger: true, hasInput: true, onConfirm: async () => {
        try { await api(`/api/node/${n.id}/profiles/${key}/delete`, { method: "POST", body: {} }); toast("已刪除", "ok"); ov.remove(); } catch (ex) { toast(ex.message, "bad"); }
        await refresh();
      } });
    const acts = el("div", "row-acts");
    acts.append(e, d);
    r.appendChild(acts);
    list.appendChild(r);
  });
  body.appendChild(list);
  ov = modal({ title: `${n.name} 的情境`, bodyNode: body });
  ov.querySelector(".modal").classList.add("wide");
  formButton(ov, "新增情境", "primary", async () => { ov.remove(); openProfileEditor(n, null, null); });
}

function openProfileEditor(n, key, p) {
  const st = n.state;
  const strata = st.slots.filter((x) => x.kind === "strata");
  const body = el("div");
  const f = el("div", "form-row");
  const fName = textInput(p?.name, "例：寫程式後端");
  const fWhy = textInput(p?.why, "例：Copilot 一輪會送四萬 token，上下文要夠長");
  f.append(fieldBox("名稱", fName), fieldBox("為什麼要切到這個情境", fWhy, "給接手的人看"));
  body.appendChild(f);
  const rows = [];
  strata.forEach((s) => {
    const ch = (p?.changes || []).find((c) => c.slot === s.id) || {};
    body.appendChild(el("h4", "pick-h", `${s.role}・${s.tier || ""}（${s.model || s.unit}）`));
    const g = el("div", "form-row");
    const fCtx = numInput(ch.args?.["--max-context"], `不改（目前 ${int(s.config?.max_context)}）`);
    const fPar = numInput(ch.config?.parallel, `不改（目前 ${s.config?.parallel || 1}）`);
    const cur = ch.config?.fit_max_tokens;
    const fFit = selectInput([["", "不改"], ["true", "開：太長時自動縮輸出上限"], ["false", "關"]], cur === undefined ? "" : String(cur));
    g.append(fieldBox("上下文長度（token）", fCtx, "2,048～1,048,576；套用前會做記憶體試算"), fieldBox("並行數", fPar, "1～16；並行越多，每條能用的上下文越少"),
      fieldBox("自動縮輸出上限", fFit));
    body.appendChild(g);
    rows.push({ slot: s.id, fCtx, fPar, fFit });
  });
  const err = el("div");
  body.appendChild(err);
  [fName, fWhy].forEach((i) => imeGuard(i, () => {}));
  const ov = modal({ title: key ? `修改情境「${p.name}」` : "新增情境", bodyNode: body, hasInput: true });
  ov.querySelector(".modal").classList.add("wide");
  formButton(ov, "儲存", "primary", async () => {
    const changes = rows.map((r) => ({ slot: r.slot, max_context: r.fCtx.value, parallel: r.fPar.value,
      fit_max_tokens: r.fFit.value === "" ? null : r.fFit.value === "true" }));
    try { await api(`/api/node/${n.id}/profiles/save`, { method: "POST", body: { key: key || "", name: fName.value.trim(), why: fWhy.value.trim(), changes } }); }
    catch (ex) { errNote(err, ex.message); return; }
    ov.remove();
    toast("已儲存情境，按情境按鈕就會套用", "ok");
    await refresh();
  });
}


// 在網頁上編譯 llama.cpp：系統套件要 IT 用 sudo 裝，這裡只檢查並列出指令；下載、設定、編譯由代理在背景做
// 在網頁上編譯推理引擎：kind＝llama（文字模型用的 llama-server）或 sd（畫圖與影片用的 sd-cli）
const ENGINE = { llama: { name: "llama.cpp", exe: "llama-server" }, sd: { name: "stable-diffusion.cpp", exe: "sd-cli" } };
async function openEngineBuild(nid, kind = "llama", onDone) {
  const n = S.fleet.nodes.find((x) => x.id === nid);
  const eng = ENGINE[kind];
  const body = el("div");
  loading(body);
  const ov = modal({ title: `${n?.name || ""} 安裝 ${eng.name}`, bodyNode: body });
  ov.querySelector(".modal").classList.add("wide");
  let b;
  try { b = await api(`/api/node/${nid}/${kind}/build`); } catch (ex) { errNote(body, ex.message); return; }
  body.textContent = "";
  const pre = b.prereqs;
  // macOS／Windows 下載官方預編譯版（幾分鐘）；Linux 在這台編譯（CUDA／ROCm 跟著這台的驅動走）
  const dl = pre.mode === "prebuilt";
  const verb = dl ? "安裝" : "編譯";
  if (!dl) {
    body.appendChild(el("p", "small muted", `會下載 ${eng.name} 最新的正式版、在這台編譯 ${eng.exe}。加速方式：${{ cuda: "NVIDIA CUDA", hip: "AMD ROCm", vulkan: "Vulkan（Intel／AMD）", cpu: "只用 CPU" }[pre.backend] || pre.backend}。`));
  }
  if (b.other) body.appendChild(el("p", "note warn", `這台正在安裝 ${ENGINE[b.other].name}，等它裝完才能裝這個（同時編兩個會把記憶體吃光）。`));
  pre.notes.forEach((t) => body.appendChild(el("p", "note", t)));
  if (!dl) body.appendChild(el("p", "small", "編譯工具：" + Object.entries(pre.tools).map(([k, v]) => `${k} ${v ? "有" : "缺"}`).join("、")));
  if (pre.missing.length) {
    body.appendChild(el("p", "note bad", "這台缺少編譯工具，要先請 IT 用有 sudo 權限的帳號裝好（網頁不代裝系統套件）："));
    body.appendChild(copyRow("請 IT 執行", pre.apt));
  }
  if (b.installed.length) body.appendChild(el("p", "small muted", `已經有的 ${eng.exe}：` + b.installed.map((x) => `${x.path}（${x.build ? "build " + x.build + "・" : ""}${x.backend}）`).join("、")));
  const prog = el("div");
  body.appendChild(prog);
  const head = el("p", "note");
  const log = el("pre", "log build-log");
  const closeBtn = ov.querySelector(".actions .btn");          // 視窗本身的「取消」：裝好之後改成「完成」
  const draw = (j, tail, running) => {
    prog.textContent = "";
    // 按鈕的顯示在最前面處理：還沒裝過（沒有狀態）時也不能出現「取消安裝」
    startBtn.hidden = running;
    startBtn.className = "btn " + (j.state === "done" ? "" : "primary");
    cancelBtn.hidden = !running;
    imp.hidden = running || j.state === "done";
    if (closeBtn) {
      closeBtn.textContent = j.state === "done" ? "完成" : "取消";
      closeBtn.className = "btn" + (j.state === "done" ? " primary" : "");
    }
    if (!j.state) return;
    const v = j.mode === "import" ? "匯入" : verb;
    const label = { running: `${v}中：${j.phase}`, done: `裝好了：${j.exe}`, failed: `失敗：${j.error || ""}`, cancelled: "已取消",
      paused: j.error, interrupted: j.error }[j.state] || j.state;
    head.className = "note " + (j.state === "done" ? "ok" : j.state === "running" ? "" : j.state === "paused" ? "warn" : "bad");
    head.textContent = `${j.tag || ""}　${label}`;
    prog.appendChild(head);
    if (j.state === "done") prog.appendChild(el("p", "small muted", kind === "sd" ? "裝好了。回模型庫的「畫圖與影片」下載一顆模型就能產圖。"
      : "裝好了，按「完成」回去，接著到「模型」下載一顆推薦的模型。"));
    log.textContent = (tail || []).join("\n") || "（還沒有輸出）";
    // 裝好之後下載記錄收進「詳細記錄」，畫面只留結果
    if (j.state === "done") {
      const det = el("details", "build-detail");
      det.appendChild(el("summary", "small muted", "詳細記錄"));
      det.appendChild(log);
      prog.appendChild(det);
    } else prog.appendChild(log);
    log.scrollTop = log.scrollHeight;
    // 預編譯版下載中可以暫停；暫停或下載到一半失敗，再按會從斷掉的位置接著下載
    const dlNow = running && j.mode === "prebuilt" && j.phase === "下載";
    cancelBtn.textContent = dlNow ? "暫停下載" : `取消${v}`;
    if (dl) startBtn.textContent = j.state === "paused" || (j.state === "failed" && j.resumable) ? "繼續下載"
      : j.state === "done" ? "更新到最新版" : startLabel;
  };
  const startLabel = dl ? "一鍵安裝（下載幾十到幾百 MB）" : "開始編譯（約 10～30 分鐘）";
  const startBtn = formButton(ov, startLabel, "primary", async () => {
    try { await api(`/api/node/${nid}/${kind}/build`, { method: "POST", body: {} }); } catch (ex) { errNote(prog, ex.message); return; }
    toast(`開始${verb}，可以關掉這個視窗，之後再回來看進度`, "ok");
    poll();
  });
  startBtn.disabled = pre.missing.length > 0 || !!b.other;
  startBtn.title = pre.missing.length ? "先請 IT 裝好編譯工具" : dl ? "" : "編譯會用掉一半的 CPU 核心，正在跑的模型可能變慢";
  const cancelBtn = formButton(ov, `取消${verb}`, "danger", async () => {
    try { await api(`/api/node/${nid}/${kind}/build/cancel`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); }
  });
  const imp = engineImportBox(nid, kind, eng, () => poll());
  body.appendChild(imp);
  draw(b.job, b.log_tail, b.running);
  // 進行中每 3 秒只換掉狀態與記錄的文字，不重畫整個視窗
  async function poll() {
    if (!ov.isConnected) return;
    let r;
    try { r = await api(`/api/node/${nid}/${kind}/build/progress`); } catch (_) { setTimeout(poll, 3000); return; }
    if (!ov.isConnected) return;
    draw(r.job, r.log_tail, r.running);
    if (r.running) setTimeout(poll, 3000);
    else {
      if (S.minfo) delete S.minfo[nid];
      onDone?.();
    }
  }
  if (b.running) setTimeout(poll, 3000);
}

/* 手動匯入安裝包：不能連外的機器，在別台電腦下載官方的壓縮檔，從這裡送上去裝。分塊送、可以暫停與續傳。 */
function engineImportBox(nid, kind, eng, onStart) {
  const box = el("details", "eng-import");
  box.appendChild(el("summary", null, "這台不能連外？改成手動匯入安裝包"));
  box.appendChild(el("p", "small muted", `在能上網的電腦到 ${eng.name} 官方 GitHub 的版本頁，下載這台系統與顯示卡用的壓縮檔`
    + `（.zip 或 .tar.gz），在這裡選檔送上去。檔案旁邊的 sha256 一併貼上，送齊後會核對，對不上就不裝。`));
  const fFile = el("input");
  fFile.type = "file";
  fFile.accept = ".zip,.gz,.tgz";
  const fSha = textInput("", "選填，64 個字元");
  const form = el("div", "form-row");
  form.append(fieldBox("安裝包", fFile), fieldBox("官方的 sha256", fSha, "不確定就留空，裝完會把算出來的值寫在記錄裡，請自己核對"));
  const line = el("p", "small mono");
  const bar = el("div", "bar thin");
  const fill = el("div", "seg");
  bar.appendChild(fill);
  bar.hidden = true;
  const goB = el("button", "btn primary", "送上去並安裝");
  const stopB = el("button", "btn sm", "暫停");
  stopB.hidden = true;
  const row = el("div", "slot-actions");
  row.append(goB, stopB);
  box.append(form, row, bar, line);
  let ctl = null;
  stopB.onclick = () => ctl?.abort();
  goB.onclick = async () => {
    const f = fFile.files[0];
    if (!f) { line.textContent = "先選安裝包"; return; }
    if (!/\.(zip|tar\.gz|tgz)$/i.test(f.name)) { line.textContent = "只收 .zip、.tar.gz、.tgz"; return; }
    const sha = fSha.value.trim().toLowerCase();
    if (sha && !/^[0-9a-f]{64}$/.test(sha)) { line.textContent = "sha256 要是 64 個十六進位字元"; return; }
    const name = f.name.replace(/[^\w.+-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "");
    ctl = new AbortController();
    goB.disabled = true; stopB.hidden = false; bar.hidden = false;
    try {
      // 伺服器上同名的舊包（上次裝失敗留下的）一律換掉；送到一半的，開頭一樣才接著送，不一樣就從頭送
      const st = await api(`/api/node/${nid}/${kind}/import/status`, { method: "POST", body: { name } });
      let off = st.received || 0;
      if (st.exists || (off && (off > f.size || st.head_sha256 !== await sha256Hex(f.slice(0, Math.min(off, 4 * 1024 * 1024)))))) {
        await api(`/api/node/${nid}/${kind}/import/cancel`, { method: "POST", body: { name } });
        off = 0;
      }
      const CH = 32 * 1024 * 1024;
      while (off < f.size) {
        const res = await fetch(`/api/node/${nid}/${kind}/import?` + new URLSearchParams({ name, offset: off, total: f.size }), {
          method: "POST", body: f.slice(off, Math.min(off + CH, f.size)), signal: ctl.signal, credentials: "same-origin",
          headers: { "Content-Type": "application/octet-stream", "X-XConsole-Upload": "1" } });
        const r = await res.json().catch(() => ({}));
        if (res.status === 409 && r.received !== undefined) { off = r.received; continue; }
        if (!res.ok) throw new Error(r.error || `伺服器回 ${res.status}`);
        off = r.received;
        const pct = Math.floor((off / f.size) * 100);
        fill.style.width = pct + "%";
        line.textContent = `${f.name}　${fmt(off / 1048576, 0)} / ${fmt(f.size / 1048576, 0)} MB（${pct}%）`;
      }
      await api(`/api/node/${nid}/${kind}/import/install`, { method: "POST", body: { name, sha256: sha } });
      line.textContent = "送齊了，正在核對、解壓縮、試跑；進度看上面";
      onStart?.();
    } catch (ex) {
      line.textContent = ex.name === "AbortError" ? "已暫停；再按一次會從斷掉的地方接著送" : "匯入失敗：" + ex.message;
    } finally {
      goB.disabled = false; stopB.hidden = true;
    }
  };
  return box;
}

function machineSteps(n, info) {
  const st = n.state;
  const steps = [
    { t: "確認記憶體架構", why: "准入試算照這個算；選錯會放行一個會把整台拖死的載入",
      done: info.settings.confirmed || info.settings.memory_model === info.detect.memory_model,
      go: ["這台的設定", () => openMachineSettings(n.id)] },
    { t: "新增欄位", why: "一格放一個模型或服務；欄位決定角色、埠與記憶體預估",
      done: st.slots.length > 0, go: ["新增欄位", () => openSlotEditor(n, null)] },
    { t: "確認模型存放與 llama.cpp", why: "GGUF 模型放在存放區、靠 llama.cpp 跑；要找得到編好的 llama-server",
      done: !!info.effective.llama_server,
      go: info.effective.llama_server ? ["這台的設定", () => openMachineSettings(n.id)] : ["編譯 llama.cpp", () => openEngineBuild(n.id)] },
    { t: "放入模型", why: "到模型庫下載或手動放入，再到空欄位按「放入」",
      done: st.slots.some((x) => x.unit), go: ["到模型庫", () => go("lib:" + n.id)] },
    { t: "到 API 串接實測", why: "換完模型會自動帶過去；按「實測一次」確認 IDE 連得上", done: false, optional: true,
      go: ["到 API 串接", () => go("apis")] },
  ];
  const first = steps.findIndex((x) => !x.done && !x.optional);
  steps.forEach((x, i) => { x.state = x.done ? "done" : x.optional ? "skip" : i === first ? "now" : "blocked"; });
  return steps;
}

/* ---------------------------------------------------------------- 主迴圈 */

// 資料更新造成的重畫：不播動畫、捲動位置不動。使用者按導覽切頁才走 render()。
function quietRender() {
  const y = window.scrollY;
  S.quiet = true;
  try { render(); } finally { S.quiet = false; }
  if (window.scrollY !== y) window.scrollTo(0, y);
}

async function refresh() {
  try {
    S.fleet = await api("/api/fleet");
    S.lastOk = Date.now();
    $("#live").classList.remove("stale");
    $("#live-text").textContent = "即時";
    // 設定頁與記錄頁有表單或捲到一半的內容，自動更新時不重畫，否則選好的檔案、打到一半的密碼會蒸發
    // API 串接頁也一樣：有表單、有實測結果、剛產生的金鑰提示
    // 執行、Agent、畫圖、影片也一樣：有選到一半的設定、進行中的載入進度或任務，各自只更新自己那一塊
    if (["settings", "apis", "chat", "models", "run", "agent", "image", "video"].includes(S.view) || S.view.startsWith("api:")) {
      renderNav();
      S.live?.();                // 有登記局部更新的頁（執行頁的狀態列）只換那一塊
    } else quietRender();
  } catch (ex) {
    $("#live").classList.add("stale");
    $("#live-text").textContent = "更新失敗";
  }
}

function start() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  checkTlsBanner();
  refresh();
  if (S.timer) clearInterval(S.timer);
  S.timer = setInterval(() => { if (!S.busy.size) refresh(); }, 15000);
}

loadBranding();

// 手動按重新整理：執行與模型頁沒有打到一半的字，直接重畫整頁拿最新狀態；其他頁照自動更新的規則
$("#refresh").addEventListener("click", async () => {
  await refresh();
  if (S.view === "run" || (S.view === "models" && S.libTab !== "hf")) render();
});

$("#theme").addEventListener("click", () => {
  const now = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", now);
  try { localStorage.setItem("xconsole-theme", now); } catch (_) { /* 無痕模式會丟錯，不影響畫面 */ }
});

try {
  const saved = localStorage.getItem("xconsole-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  else if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
    document.documentElement.setAttribute("data-theme", "dark");
  }
} catch (_) { /* 讀不到就用預設淺色 */ }

// 頂列玻璃化：捲動超過 20px 就加深
window.addEventListener("scroll", () => {
  const sc = window.scrollY > 20;
  const tb = $("#topbar");
  if (sc) tb.setAttribute("data-scrolled", ""); else tb.removeAttribute("data-scrolled");
}, { passive: true });

(async function boot() {
  await i18nInit();                          // 選了別的語言就先載字典，畫面一出來就是那個語言
  ["langpick-top", "langpick-login", "langpick-m"].forEach((id) => { const h = document.getElementById(id); if (h) h.appendChild(i18nPicker()); });
  // xConsole 應用程式開的視窗：鑰匙放在網址 # 後面（不會送到伺服器），換成登入後馬上從網址拿掉
  const dk = location.hash.match(/^#desktop=([\w-]{32,128})$/);
  if (dk) {
    history.replaceState(null, "", location.pathname);
    try { await api("/api/desktop/enter", { method: "POST", body: { key: dk[1] } }); } catch (_) { /* 失敗就走一般登入 */ }
  }
  try {
    await api("/api/me");
    start();
  } catch (_) {
    showLogin();
  }
  i18nStart();
})();
