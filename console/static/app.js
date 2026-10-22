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
    await api("/api/login", { method: "POST", body: { password: $("#pw").value } });
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
  items.push(["logs", "記錄"], ["settings", "設定"]);

  const active = S.view.startsWith("node:")
    ? "group:" + (S.fleet.nodes.find((n) => n.id === S.view.slice(5))?.group || "")
    : S.view;

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
      await api(`/api/node/${node.id}/slot/${s.id}/model`,
        { method: "POST", body: { pack: chosen.pack, native: chosen.native } });
      toast(`已換成 ${chosen.name}`, "ok");
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
  const twoCol = el("div", "grid");
  twoCol.style.marginTop = "16px";
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

function viewSettings() {
  const main = $("#main");
  main.textContent = "";
  const wrap = el("div", "wrap");

  const c1 = el("section", "card");
  const h1 = el("div", "card-head");
  h1.appendChild(el("h2", null, "納管的機器"));
  h1.appendChild(el("span", "sub", `共 ${S.fleet.nodes.length} 台`));
  c1.appendChild(h1);
  const sb = el("div", "scrollbox");
  const tb = el("table");
  tb.innerHTML = "<thead><tr><th>機器</th><th>群組</th><th>位址</th><th>記憶體架構</th><th>狀態</th></tr></thead>";
  const body = el("tbody");
  const MM = { unified_split: "統一記憶體（切兩塊）", unified_single: "統一記憶體（單一池）", discrete: "獨立顯存" };
  S.fleet.nodes.forEach((n) => {
    const tr = el("tr");
    tr.appendChild(el("td", null, n.name));
    tr.appendChild(el("td", null, n.group));
    tr.appendChild(Object.assign(el("td", "mono"), { textContent: n.host }));
    tr.appendChild(el("td", null, n.online ? (MM[n.state.pools.memory_model] || "—") : "—"));
    tr.appendChild(el("td", null, n.online ? "在線" : "連不上"));
    body.appendChild(tr);
  });
  tb.appendChild(body);
  sb.appendChild(tb);
  c1.appendChild(sb);
  c1.appendChild(el("p", "small muted",
    "要加機器：在該機部署節點代理（deploy_agent.sh），再把它的位址與權杖加進主控台的 ~/.xconsole/fleet.json。"));
  wrap.appendChild(c1);

  const c2 = el("section", "card rv");
  const h2 = el("div", "card-head");
  h2.appendChild(el("h2", null, "安全"));
  c2.appendChild(h2);
  [
    ["連線", "主控台以 HTTPS 提供服務。第一次啟動若沒有正式憑證會自動產生暫用自簽；要換成正式憑證，把 cert.pem 與 key.pem 放進主控台機器的 ~/.xconsole/tls/ 再重啟即可。"],
    ["各機代理權杖", "存在每台機器的 ~/.xconsole/token（權限 0600），只有主控台伺服器讀得到。瀏覽器拿到的只是一個簽章過的登入票證，看不到任何代理權杖。"],
    ["登入密碼", "以 PBKDF2-SHA256（20 萬輪）加鹽雜湊後存檔，不存明文。要改密碼在主控台機器上跑 xconsole_server.py --set-password。"],
    ["准入底線", "統一記憶體機的系統記憶體底線預設 15 GiB、DGX 與 Jetson 預設 12 GiB，可在各機 registry.json 以 ram_floor_gib 調整。"],
  ].forEach(([k, v]) => {
    const d = el("div");
    d.appendChild(el("div", "role", k));
    d.appendChild(el("p", "small muted", v));
    c2.appendChild(d);
  });
  wrap.appendChild(c2);

  main.appendChild(wrap);
  reveal(main);
}

function render() {
  if (!S.fleet) return;
  renderNav();
  if (S.view === "overview") viewOverview();
  else if (S.view.startsWith("node:")) viewNode();
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
    render();
  } catch (ex) {
    $("#live").classList.add("stale");
    $("#live-text").textContent = "更新失敗";
  }
}

function start() {
  $("#login").hidden = true;
  $("#app").hidden = false;
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
