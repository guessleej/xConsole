/* xConsole 主控台前端。
   規範：console-ui-architecture（頂部雙列、每頁同一副骨架、含輸入欄位的彈窗禁止點背景關閉、
   Enter 要擋中文輸入法）＋ xcloud-web-style（品牌色、無色條、無 emoji、頁面不准很長）。 */

const S = {
  fleet: null,      // 最近一次彙整結果
  view: "overview", // overview | node:<id> | logs | settings
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
const int = (n) => (n === null || n === undefined) ? "—" : Math.round(n).toLocaleString("zh-TW");

function toast(msg, kind) {
  const t = el("div", "toast" + (kind ? " " + kind : ""), msg);
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "bad" ? 9000 : 4500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* 沒有 JSON 就算了 */ }
  if (res.status === 401) { showLogin(); throw new Error("請先登入"); }
  if (!res.ok) { const e = new Error(data.error || `伺服器回 ${res.status}`); e.data = data; throw e; }
  return data;
}

/* ---------------------------------------------------------------- 登入 */

function showLogin() {
  $("#login").hidden = false;
  $("#app").hidden = true;
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#login-err");
  err.hidden = true;
  try {
    await api("/api/login", { method: "POST", body: { username: $("#un").value.trim(), password: $("#pw").value } });
    $("#pw").value = "";
    start();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
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
function imeGuard(input, onEnter) {
  let composing = false;
  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend", () => { composing = false; });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (composing || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    onEnter();
  });
}

/* ---------------------------------------------------------------- 捲動漸入 */

function reveal(root) {
  const items = root.querySelectorAll(".rv:not(.in)");
  if (!items.length) return;
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

function renderNav() {
  const nav = $("#nav");
  const sub = $("#subnav");
  nav.textContent = "";
  sub.textContent = "";

  const items = [["overview", "總覽"]];
  for (const [name] of groups()) items.push(["group:" + name, name]);
  items.push(["apis", "API 串接"], ["logs", "記錄"], ["settings", "設定"]);

  const active = S.view.startsWith("node:")
    ? "group:" + (S.fleet.nodes.find((n) => n.id === S.view.slice(5))?.group || "")
    : S.view.startsWith("api:") ? "apis" : S.view;

  items.forEach(([key, label]) => {
    const b = el("button", null, label);
    if (key === active) b.setAttribute("aria-current", "true");
    b.onclick = () => {
      if (key.startsWith("group:")) {
        const first = groups().get(key.slice(6))?.[0];
        go(first ? "node:" + first.id : "overview");
      } else go(key);
    };
    nav.appendChild(b);
  });

  // 第二列：設定頁的分頁（依管理者實際的先後：先看機器、再處理憑證、再看報告、最後才是帳號）
  if (S.view === "settings") {
    SETTINGS_TABS.forEach(([key, label]) => {
      const b = el("button", null, label);
      if ((S.settingsTab || "machines") === key) b.setAttribute("aria-current", "true");
      b.onclick = () => { S.settingsTab = key; render(); };
      sub.appendChild(b);
    });
  }

  // 第二列：API 串接的各個端點
  if (S.view === "apis" || S.view.startsWith("api:")) {
    const all = el("button", null, "全部");
    if (S.view === "apis") all.setAttribute("aria-current", "true");
    all.onclick = () => go("apis");
    sub.appendChild(all);
    (S.fleet.nodes || []).forEach((n) => (n.state?.slots || []).filter(isApiSlot).forEach((s) => {
      const b = el("button", null, `${n.name} ${s.role}`);
      if (S.view === `api:${n.id}:${s.id}`) b.setAttribute("aria-current", "true");
      b.onclick = () => go(`api:${n.id}:${s.id}`);
      sub.appendChild(b);
    }));
  }

  // 第二列：所在區的各台機器
  if (S.view.startsWith("node:")) {
    const me = S.fleet.nodes.find((n) => n.id === S.view.slice(5));
    (groups().get(me?.group) || []).forEach((n) => {
      const b = el("button", null, n.name + (n.online ? "" : "（連不上）"));
      if (n.id === me.id) b.setAttribute("aria-current", "true");
      b.onclick = () => go("node:" + n.id);
      sub.appendChild(b);
    });
  }
}

function go(view) {
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
    b.onclick = () => modal({
      title: `${s.role}・${s.tier || ""} 要放什麼`,
      bodyNode: (() => {
        const d = el("div");
        d.appendChild(el("p", "small muted",
          "欄位內容寫在各機的 ~/.xconsole/registry.json。加一個欄位要填：" +
          "對應的 systemd 服務名、預估顯存與系統記憶體（准入控制要用）、能力標籤。" +
          "填好後主控台會自動把它納入記憶體試算與啟停。"));
        return d;
      })(),
    });
    c.appendChild(b);
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
  if (s.critical) chips.appendChild(el("span", "chip brand", "關鍵服務"));
  c.appendChild(chips);

  if (s.config?.max_context) {
    c.appendChild(kvRow("上下文", int(s.config.max_context) + (s.config.parallel > 1 ? ` ×${s.config.parallel}` : "")));
  }
  if (s.busy) {
    // 統一記憶體機沒有獨立顯存，列一行「顯存 0.0」只會誤導
    if (!unified) c.appendChild(kvRow("顯存" + (s.vram_exact ? "" : "（整機）"), fmt(s.vram_gib) + " GiB"));
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
  if (s.kind === "strata") {
    const sw = el("button", "btn sm", "換模型");
    sw.onclick = () => openSwap(node, s);
    acts.appendChild(sw);
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
  c.appendChild(acts);
  return c;
}

/* 換模型：列出這台裝了哪些、選一個、套用。失敗會自動還原成原本那顆。 */
async function openSwap(node, s) {
  const body = el("div");
  body.appendChild(el("p", "small muted", "讀取這台裝了哪些模型…"));
  const ov = modal({ title: `${s.role}・${s.tier || ""} 換模型`, bodyNode: body, hasInput: true });

  let cat;
  try {
    cat = await api(`/api/node/${node.id}/catalog/${s.id}`);
  } catch (ex) {
    body.textContent = "";
    body.appendChild(el("p", "note bad", "讀不到模型清單：" + ex.message));
    return;
  }

  body.textContent = "";
  if (cat.error) body.appendChild(el("p", "note bad", cat.error));
  if (!cat.models?.length) {
    body.appendChild(el("p", "note warn", "這台沒有掃到可用的模型。"));
  } else if (cat.models.length === 1 && cat.models[0].current) {
    body.appendChild(el("p", "note",
      `這台目前只裝了一顆模型（${cat.models[0].name}），沒有別的可以換。`));
  }

  let chosen = null;
  const list = el("div");
  cat.models?.forEach((m) => {
    const row = el("label", "slot");
    row.style.cssText = "flex-direction:row;align-items:center;gap:10px;min-height:0;padding:10px 12px;cursor:pointer";
    const r = el("input");
    r.type = "radio";
    r.name = "model";
    r.style.width = "auto";
    r.disabled = !!m.current;
    r.onchange = () => { chosen = m; okBtn.disabled = false; };
    row.appendChild(r);
    const txt = el("div");
    txt.style.flex = "1";
    txt.appendChild(el("div", "model", m.name));
    txt.appendChild(el("div", "small muted",
      `${m.size_gib} GiB・${m.shards} 個分片${m.current ? "・目前使用中" : ""}`));
    row.appendChild(txt);
    if (!m.has_tokenizer) {
      r.disabled = true;
      row.appendChild(el("span", "chip", "缺 tokenizer"));
    }
    list.appendChild(row);
  });
  body.appendChild(list);

  (cat.orphans || []).forEach((o) => {
    body.appendChild(el("p", "note warn", `${o.name}：${o.missing}`));
  });
  body.appendChild(el("p", "small muted",
    "套用時會先備份設定檔、做一次記憶體試算，然後重啟。模型要重新載入，大約一到兩分鐘；" +
    "期間這個欄位的服務會中斷。載入失敗會自動還原成原本那顆。"));

  const actions = ov.querySelector(".actions");
  const okBtn = el("button", "btn primary", "套用並重啟");
  okBtn.disabled = true;
  okBtn.onclick = async () => {
    ov.remove();
    toast("換模型中，模型要重新載入…");
    try {
      const r = await api(`/api/node/${node.id}/slot/${s.id}/model`,
        { method: "POST", body: { pack: chosen.pack, native: chosen.native } });
      toast(`已換成 ${chosen.name}`, "ok");
      // 換完模型，下一步就是把 IDE 改到新模型：直接帶去 API 串接頁，實測一次再貼設定
      S.apiNotice = S.apiNotice || {};
      S.apiNotice[node.id + "/" + s.id] = `已換成 ${chosen.name}。IDE 裡的模型名稱請改成 ${r.api_model_name || "下面的模型名稱"}，`
        + "上下文與輸出上限照下面填，再按「實測一次」。";
      await refresh();
      go(`api:${node.id}:${s.id}`);
      return;
    } catch (ex) {
      const v = ex.data?.admit;
      toast(v ? `擋下來了：${v.reasons.join("；")}` : ("換模型失敗：" + ex.message), "bad");
    }
    await refresh();
  };
  actions.appendChild(okBtn);
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
const isApiSlot = (s) => !!s.unit && !!s.port && (s.kind === "strata" || (s.caps || []).some((c) => c === "文字" || c === "嵌入"));

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
        ? `${info.last_test.ok ? "通過" : "失敗"}・${new Date(info.last_test.at * 1000).toLocaleString("zh-TW", { hour12: false })}` : "還沒測";
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
  const tc = card("實測", info.last_test ? `上次：${info.last_test.ok ? "通過" : "失敗"}・${new Date(info.last_test.at * 1000).toLocaleString("zh-TW", { hour12: false })}（台北時間）` : "還沒測過");
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
  tiles.appendChild(tile("顯存使用", `${fmt(vram, 0)} GiB`, `共 ${fmt(vramTotal, 0)} GiB`));
  tiles.appendChild(tile("更新於", new Date(S.fleet.ts * 1000).toLocaleTimeString("zh-TW"), "每 15 秒自動更新"));
  hw.appendChild(tiles);
  hero.appendChild(hw);

  const wrap = el("div", "wrap");
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
    c.appendChild(kvRow(st.pools.memory_model === "unified_single" ? "統一記憶體" : "顯存",
      `${fmt(p.used_gib)} / ${fmt(p.total_gib)} GiB`));
    if (st.pools.memory_model !== "unified_single") {
      const low = r.free_gib < r.floor_gib + 4;
      const row = kvRow("系統記憶體剩", fmt(r.free_gib) + " GiB");
      if (low) row.querySelector(".v").style.color = "var(--warn)";
      c.appendChild(row);
    }
    const on = st.slots.filter((s) => s.busy).map((s) => s.role);
    c.appendChild(kvRow("運作中", on.length ? on.join("、") : "無"));
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
  if (st.machine.note) head.appendChild(el("p", "note", st.machine.note));

  // 情境切換
  const profs = Object.entries(st.profiles || {});
  if (profs.length) {
    const row = el("div", "slot-actions");
    row.appendChild(el("span", "small muted", "情境："));
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
    row.appendChild(el("span", "spacer"));
    row.appendChild(stopAll);
    head.appendChild(row);
  }
  wrap.appendChild(head);

  // 角色欄位
  const slotsCard = el("section", "card");
  const sh = el("div", "card-head");
  sh.appendChild(el("h2", null, "角色欄位"));
  sh.appendChild(el("span", "sub",
    `${st.slots.filter((s) => s.busy).length} 個運作中，共 ${st.slots.length} 格`));
  slotsCard.appendChild(sh);
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
  mh.appendChild(el("span", "sub", st.pools.model_note || ""));
  memCard.appendChild(mh);
  if (st.pools.memory_model === "unified_single") {
    memCard.appendChild(poolBar(st.pools.unified, st.slots, "統一記憶體"));
  } else {
    memCard.appendChild(poolBar(st.pools.vram, st.slots, "顯存"));
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
      "這台是統一記憶體機，再載東西進去有機會把整台拖死。"));
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
  tb.innerHTML = "<thead><tr><th>埠</th><th>類別</th><th>是什麼</th><th>範圍</th></tr></thead>";
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
  if (S.sub?.node) nodeSel.value = S.sub.node;
  const slotSel = el("select");
  const fillSlots = () => {
    slotSel.textContent = "";
    const n = S.fleet.nodes.find((x) => x.id === nodeSel.value);
    (n?.state.slots || []).filter((s) => s.unit).forEach((s) => {
      const o = el("option", null, `${s.role}・${s.tier || ""}（${s.unit}）`);
      o.value = s.id;
      slotSel.appendChild(o);
    });
    if (S.sub?.slot) slotSel.value = S.sub.slot;
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
  card.appendChild(pre);
  const load = async () => {
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

/* ================================================================ 設定頁（四個分頁）
   分頁順序照管理者實際的先後：先確認機器都連得上 → 處理憑證 → 看使用報告 → 帳號。 */

const SETTINGS_TABS = [
  ["machines", "1 機器"], ["tls", "2 HTTPS 與憑證"], ["report", "3 使用報告"], ["account", "4 帳號與安全"],
];

function viewSettings() {
  const tab = S.settingsTab || "machines";
  const main = $("#main");
  main.textContent = "";
  const wrap = el("div", "wrap");
  main.appendChild(wrap);
  ({ machines: settingsMachines, tls: settingsTls, report: settingsReport, account: settingsAccount }[tab])(wrap);
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

async function settingsMachines(wrap) {
  const c = card("納管的機器", `共 ${S.fleet.nodes.length} 台`);
  const box = el("div");
  loading(box);
  c.appendChild(box);
  c.appendChild(el("p", "small muted",
    "主控台連各機代理一律走 HTTPS，並用部署時經 SSH 取回的該機憑證驗證對方；"
    + "憑證不符就拒絕連線，不會退回明文或關掉驗證。代理重裝之後，重新執行 deploy_console.sh，或在下面匯入新憑證。"));
  wrap.appendChild(c);

  let nodes;
  try { nodes = (await api("/api/nodes")).nodes; } catch (ex) { box.textContent = ex.message; return; }
  const MM = { unified_split: "統一記憶體（切兩塊）", unified_single: "統一記憶體（單一池）", discrete: "獨立顯存" };
  const sb = el("div", "scrollbox");
  const tb = el("table");
  tb.innerHTML = "<thead><tr><th>機器</th><th>群組</th><th>位址</th><th>記憶體架構</th><th>連線</th><th>信任的代理憑證</th><th></th></tr></thead>";
  const body = el("tbody");
  nodes.forEach((n) => {
    const live = S.fleet.nodes.find((x) => x.id === n.id);
    const tr = el("tr");
    tr.appendChild(el("td", null, n.name));
    tr.appendChild(el("td", null, n.group));
    tr.appendChild(Object.assign(el("td", "mono"), { textContent: n.host }));
    tr.appendChild(el("td", null, live?.online ? (MM[live.state.pools.memory_model] || "—") : "—"));
    tr.appendChild(el("td", null, live?.online ? "在線" : "連不上"));
    tr.appendChild(Object.assign(el("td", "mono small"),
      { textContent: n.trusted ? (n.trust_fingerprint || "").slice(0, 23) + "…" : "尚未信任" }));
    const td = el("td");
    const btn = el("button", "btn sm", "匯入憑證");
    btn.onclick = () => importTrust(n);
    td.appendChild(btn);
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

  const fmtN = (v) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("zh-TW");
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

async function settingsAccount(wrap) {
  let me = {};
  try { me = await api("/api/settings"); } catch (_) { /* 沒拿到就顯示預設 */ }
  const c = card("變更密碼", `目前登入：${me.username || "admin"}`);
  const fr = el("div", "form-row");
  const mk = (label) => { const d = el("div"); d.appendChild(el("label", null, label)); const i = Object.assign(el("input"), { type: "password", autocomplete: "new-password" }); d.appendChild(i); fr.appendChild(d); return i; };
  const o = mk("目前的密碼");
  const n1 = mk("新密碼（至少 8 碼）");
  const n2 = mk("再輸入一次新密碼");
  c.appendChild(fr);
  const b = el("button", "btn sm primary", "變更密碼");
  const go2 = async () => {
    if (n1.value !== n2.value) { toast("兩次輸入的新密碼不一樣", "bad"); return; }
    try {
      const r = await api("/api/account/password", { method: "POST", body: { old: o.value, new: n1.value } });
      toast(r.message, "ok");
      setTimeout(showLogin, 800);
    } catch (ex) { toast(ex.message, "bad"); }
  };
  b.onclick = go2;
  [o, n1, n2].forEach((x) => imeGuard(x, go2));
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
      "trust.import": "信任代理憑證", "account.password": "變更密碼", "settings.report": "修改報告假設" };
    const sb = el("div", "scrollbox");
    const tb = el("table");
    tb.innerHTML = "<thead><tr><th>時間（台北）</th><th>帳號</th><th>來源</th><th>動作</th><th>對象</th><th>結果</th></tr></thead>";
    const body = el("tbody");
    items.forEach((a) => {
      const tr = el("tr");
      [new Date(a.ts * 1000).toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false }),
       a.user || "—", a.ip || "—", LABEL[a.action] || a.action, a.target || "—",
       a.ok ? "成功" : ((a.detail && a.detail.blocked) ? "被准入擋下" : "失敗")].forEach((v) => tr.appendChild(el("td", null, v)));
      body.appendChild(tr);
    });
    if (!items.length) { const tr = el("tr"); const td = el("td", null, "還沒有紀錄"); td.colSpan = 6; tr.appendChild(td); body.appendChild(tr); }
    tb.appendChild(body); sb.appendChild(tb);
    box.textContent = ""; box.appendChild(sb);
  } catch (ex) { box.textContent = ex.message; }
}

function render() {
  if (!S.fleet) return;
  renderNav();
  if (S.view === "overview") viewOverview();
  else if (S.view.startsWith("node:")) viewNode();
  else if (S.view === "apis") viewApis();
  else if (S.view.startsWith("api:")) viewApi();
  else if (S.view === "logs") viewLogs();
  else if (S.view === "settings") viewSettings();
}

/* ---------------------------------------------------------------- 主迴圈 */

async function refresh() {
  try {
    S.fleet = await api("/api/fleet");
    S.lastOk = Date.now();
    $("#live").classList.remove("stale");
    $("#live-text").textContent = "即時";
    // 設定頁與記錄頁有表單或捲到一半的內容，自動更新時不重畫，否則選好的檔案、打到一半的密碼會蒸發
    // API 串接頁也一樣：有表單、有實測結果、剛產生的金鑰提示
    if (S.view === "settings" || S.view === "logs" || S.view === "apis" || S.view.startsWith("api:")) renderNav(); else render();
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

$("#refresh").addEventListener("click", refresh);

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
  try {
    await api("/api/me");
    start();
  } catch (_) {
    showLogin();
  }
})();
