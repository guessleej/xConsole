/* xConsole 主要頁面：概覽、執行、對話、Agent、畫圖、影片，以及設定裡的推理引擎與第三方 API。
   動線照 docs/WORKFLOW.md：這台機器從零到能用是一條路（概覽 → 模型 → 執行 → 對話／Agent／畫圖／影片），
   機群管理退到設定。全站共用「目前這台機器」（S.node），只有一台時不用選。
   共用的小工具（el、api、card、modal、imeGuard…）在 app.js。 */

/* ---------------------------------------------------------------- 目前這台機器 */

try { S.node = localStorage.getItem("xconsole-node") || null; } catch (_) { S.node = null; }

function curNode() {
  const ns = S.fleet?.nodes || [];
  return ns.find((n) => n.id === S.node) || ns.find((n) => n.online) || ns[0] || null;
}

function setNode(id) {
  S.node = id;
  try { localStorage.setItem("xconsole-node", id); } catch (_) { /* 無痕模式：只記在這個分頁 */ }
}

// 頂列右側的機器切換：只有一台時只顯示名稱
function renderNodePick() {
  const box = $("#nodepick");
  if (!box) return;
  const ns = S.fleet?.nodes || [];
  const n = curNode();
  box.textContent = "";
  if (!n) return;
  if (ns.length < 2) {
    box.appendChild(el("span", "nodepick-one", n.name + (n.online ? "" : "（連不上）")));
    return;
  }
  const sel = el("select", "nodepick-sel");
  sel.setAttribute("aria-label", "目前這台機器");
  for (const [gname, gnodes] of groups()) {
    const og = el("optgroup");
    og.label = gname;
    gnodes.forEach((x) => {
      const o = el("option", null, x.name + (x.online ? "" : "（連不上）"));
      o.value = x.id;
      og.appendChild(o);
    });
    sel.appendChild(og);
  }
  sel.value = n.id;
  sel.onchange = () => {
    setNode(sel.value);
    // 停在某台的子頁時，換機器就回到同一區的第一頁
    const top = topOf(S.view);
    go({ home: "home", models: "models", run: "run", chat: "chat", agent: "agent", image: "image", video: "video" }[top] || S.view);
  };
  box.appendChild(sel);
}

// 某個內部頁屬於頂層哪一格
function topOf(v) {
  if (v === "home" || v === "overview") return "home";
  if (v === "models" || v === "lib" || v.startsWith("lib:")) return "models";
  if (v === "run" || v === "apis" || v.startsWith("api:") || v.startsWith("node:")) return "run";
  if (v === "chat" || v === "assist") return "chat";
  if (v === "logs") return "settings";
  return v;
}

/* ---------------------------------------------------------------- 共用：等級與三色分層條 */

// 深色＝放顯示卡（最快）、淺色＝放系統記憶體、灰色＝放硬碟（最慢）
function splitBar(split, opts = {}) {
  const box = el("div", "split" + (opts.big ? " big" : ""));
  const s = split || {};
  const tot = (s.gpu || 0) + (s.ram || 0) + (s.disk || 0);
  if (!tot) return box;
  [["gpu", "放顯示卡"], ["ram", "放系統記憶體"], ["disk", "放硬碟"]].forEach(([k, label]) => {
    if (!s[k]) return;
    const seg = el("i", "split-" + k);
    seg.style.flexGrow = String(s[k]);
    seg.title = `${label} ${fmt(s[k])} GiB`;
    box.appendChild(seg);
  });
  box.title = "深色＝放顯示卡（最快）、淺色＝放系統記憶體、灰色＝放硬碟（最慢）";
  return box;
}

// 等級的字：頻寬不知道時速度算不出來，放得下就說「放得下」，不要說成「勉強」
function levelOf(p) {
  if (p?.level === "ok" && p.tok_s == null && !p.cpu_moe) return ["放得下", "ok"];
  return LEVEL[p?.level] || LEVEL.unknown;
}

// 一格「等級＋預估速度＋分層條」：模型清單、搜尋結果、執行頁共用
function planCell(p) {
  const cell = el("div", "plan-cell");
  if (!p) { cell.appendChild(el("span", "chip idle", "待更新")); return cell; }
  const [lbl, cls] = levelOf(p);
  const top = el("div", "plan-top");
  top.appendChild(el("span", "chip " + cls, lbl));
  top.appendChild(el("span", "small mono", p.tok_s != null ? (p.tok_s < 1 ? "不到 1 tok/s" : `約 ${fmt(p.tok_s, 0)} tok/s`) : "速度未知"));
  if (p.guess) top.appendChild(el("span", "small muted", "（估算）"));
  cell.appendChild(top);
  cell.appendChild(splitBar(p.split));
  return cell;
}

// 從任何清單按「執行」：帶到執行頁並選好這顆
function runModel(n, m) {
  setNode(n.id);
  S.runPick = m.id;
  go("run");
}

const tileOf = (k, v, s) => {
  const t = el("div", "tile");
  t.append(el("div", "k", k), el("div", "v", v));
  if (s) t.appendChild(el("div", "s", s));
  return t;
};

// 這台現在跑著的語言模型欄位（第一個）
const llmSlots = (n) => (n?.state?.slots || []).filter((s) => s.kind === "llama" || s.kind === "strata" || (!s.unit && !s.kind));
const runningSlot = (n) => llmSlots(n).find((s) => s.busy && s.unit);

/* ================================================================ 概覽
   這台：三步設定順序 → 能跑多大 ＋ 硬體 → 為這台推薦；多台時第二列可切到「全部機器」（原本的機群總覽）。 */

function viewHome() {
  if (S.homeAll) return viewOverview();
  const main = $("#main");
  const n = curNode();
  if (!n) {
    main.textContent = "";
    const wrap = el("div", "wrap");
    const c = card("還沒有機器", "先把一台機器加進來，主控台會讀出它的顯示卡、記憶體與磁碟，算出能跑多大的模型");
    const b = el("button", "btn primary", "加一台機器");
    b.onclick = openAddMachine;
    c.appendChild(b);
    wrap.appendChild(c);
    main.appendChild(wrap);
    return;
  }
  const st = n.state || {};
  const rs = runningSlot(n);
  const hero = el("section", "hero");
  hero.appendChild(el("div", "hero-glow"));
  const hw = el("div", "wrap");
  hw.appendChild(el("h1", null, n.name));
  hw.appendChild(el("p", null, n.online ? [st.chip?.gpu, st.chip?.cpu].filter(Boolean).join("・") || "已連線"
    : "連不上這台的代理：確認機器有開、網路通，或到設定看這台的位址"));
  const tiles = el("div", "tiles");
  const pool = st.pools?.unified || st.pools?.vram;
  tiles.append(
    tileOf("現在執行", rs ? (rs.model || rs.role) : "沒有", rs ? `${rs.role}・埠 ${rs.port || "—"}` : "到「執行」選一顆啟動"),
    tileOf(st.pools?.memory_model === "discrete" ? "顯示記憶體可用" : "記憶體可用", pool ? `${fmt(pool.free_gib, 0)} GiB` : "—",
      pool ? `共 ${fmt(pool.total_gib, 0)} GiB` : ""),
    tileOf("推理引擎", st.engine?.llama_server ? "llama.cpp" : st.engine?.building ? "安裝中" : "還沒安裝",
      st.engine?.llama_server ? "已安裝" : "第二步一鍵安裝"),
    tileOf("更新於", new Date(S.fleet.ts * 1000).toLocaleTimeString("zh-TW"), "每 15 秒自動更新"));
  hw.appendChild(tiles);
  hero.appendChild(hw);

  const wrap = el("div", "wrap");
  if (n.online) {
    const steps = homeSteps(n);
    const sc = el("section", "card rv");
    sc.appendChild(renderSteps(steps));
    if (steps.every((x) => x.done)) {
      const g = sc.querySelector(".steps");
      g.hidden = true;
      const t = el("button", "linkbtn", "看步驟");
      t.onclick = () => { g.hidden = !g.hidden; t.textContent = g.hidden ? "看步驟" : "收起步驟"; };
      sc.querySelector(".steps-head").appendChild(t);
    }
    wrap.appendChild(sc);
    if (st.engine?.building) watchBuild(sc, n);
    if (rs) wrap.appendChild(statusBar(n, rs));
    const cached = S.rec?.[n.id];
    if (cached) {
      wrap.appendChild(hwCard(n, cached.data));
      wrap.appendChild(recTop(n, cached.data, cached.lib, { after: () => { S.libTab = "rec"; go("models"); } }));
    } else {
      const h = el("section", "card");
      loading(h);
      wrap.appendChild(h);
    }
    if (!cached || Date.now() - cached.at > 60000) {
      Promise.all([getRec(n.id, true), api(`/api/node/${n.id}/library`).catch(() => null)])
        .then(([, lib]) => { S.rec[n.id].lib = lib; if (S.view === "home") quietRender(); }).catch(() => {});
    }
  }
  main.textContent = "";
  main.append(hero, wrap);
  reveal(main);
}

// 這台從零到能用的三步（LocModel 的開場）：狀態全由代理回報的真實資料算
function homeSteps(n) {
  const st = n.state;
  const unsure = mmUnsure(n);
  const building = st.engine?.building;
  const noEng = st.engine && !st.engine.llama_server;
  const running = !!runningSlot(n);
  const steps = [
    { t: "檢測硬體", done: !unsure,
      why: unsure ? `偵測到 ${st.chip?.gpu || "這台的晶片"}，建議記憶體架構是「${MEM_LABEL[st.chip.suggest]}」；確認後推薦與試算才準`
        : "顯示卡、記憶體、頻寬與存放區都讀到了",
      go: unsure ? ["確認記憶體架構", () => openMachineSettings(n.id)] : null },
    { t: "安裝推理引擎", done: !noEng && !building,
      why: building ? "正在安裝 llama.cpp…" : noEng ? "依這台的顯示卡自動選加速方式一鍵安裝（macOS、Windows 下載官方預編譯版，Linux 在這台編譯）"
        : "llama.cpp 已就緒",
      go: building ? ["看進度", () => openEngineBuild(n.id)] : noEng ? ["一鍵安裝", () => openEngineBuild(n.id)] : null },
    { t: "下載並啟動一個模型", done: running,
      why: running ? "模型執行中，可以到「對話」直接用，或把 API 網址給 IDE" : "照這台的記憶體推薦、標好速度與放不放得下；下載完到「執行」按啟動",
      go: running ? ["去對話", () => go("chat")] : ["看推薦", () => { S.libTab = "rec"; go("models"); }] },
  ];
  const first = steps.findIndex((x) => !x.done);
  steps.forEach((x, i) => { x.state = x.done ? "done" : i === first ? "now" : "blocked"; });
  if (steps[0].done) steps.forEach((x) => { if (x.state === "blocked") x.state = "now"; });
  return steps;
}

// 執行中的狀態列：模型、生成速度、記憶體、API 網址（複製）、去對話、停止
function statusBar(n, s) {
  const bar = el("section", "card status-bar rv");
  const dot = el("span", "run-dot");
  const name = el("div", "sb-name");
  name.append(dot, el("b", null, s.model || s.role), el("span", "small muted", `${s.role}・埠 ${s.port || "—"}`));
  const pool = n.state.pools?.unified || n.state.pools?.vram;
  const ram = n.state.pools?.ram;
  const stats = el("div", "sb-stats");
  const stat = (k, v) => { const d = el("div"); d.append(el("span", "k", k), el("b", "mono", v)); stats.appendChild(d); };
  const sp = s.measure?.decode_max ?? s.measure?.decode_min;
  if (sp) stat("生成速度", `${fmt(sp, 1)} tok/s`);
  if (pool) stat(n.state.pools.memory_model === "discrete" ? "顯示記憶體" : "記憶體", `${fmt(pool.used_gib, 1)} / ${fmt(pool.total_gib, 0)} GiB`);
  if (ram && n.state.pools.memory_model !== "unified_single") stat("系統記憶體", `${fmt(ram.used_gib, 1)} / ${fmt(ram.total_gib, 0)} GiB`);
  const url = `http://${hostOf(n)}:${s.port}/v1`;
  const urlBox = el("div", "sb-url");
  urlBox.appendChild(el("code", "mono", url));
  const cp = el("button", "btn sm", "複製");
  cp.onclick = () => copyText(url);
  urlBox.appendChild(cp);
  const acts = el("div", "sb-acts");
  const chat = el("button", "btn sm primary", "去對話");
  chat.onclick = () => go("chat");
  const apiB = el("button", "btn sm", "API 串接");
  apiB.onclick = () => go(`api:${n.id}:${s.id}`);
  const stop = el("button", "btn sm danger", "停止模型");
  stop.onclick = () => doAction(n, s, "stop");
  acts.append(chat, apiB, stop);
  bar.append(name, stats, urlBox, acts);
  return bar;
}

/* ================================================================ 執行
   選一顆已安裝的模型 → 自動分層規劃（顯示卡／系統記憶體／硬碟）→ 上下文、思考、區網 → 預估速度與載入時間
   → 啟動 → 載入進度 → 執行中狀態列。欄位（role slot）是進階概念：這台還沒有任何語言模型欄位時，
   啟動時自動建一格「主模型」，使用者不必先懂欄位。 */

const RUN_CTX_OPTS = [8192, 16384, 32768, 65536, 131072];

async function viewRun() {
  const main = $("#main");
  const v = S.view;
  const n = curNode();
  main.textContent = "";
  const wrap = el("div", "wrap");
  main.appendChild(wrap);
  if (!n || !n.online) {
    wrap.appendChild(el("p", "note warn", n ? `${n.name} 連不上，換一台或到設定看這台的位址。` : "還沒有機器。"));
    return;
  }
  const holder = el("div", "stack");
  wrap.appendChild(holder);
  loading(holder);
  let lib;
  try { lib = await api(`/api/node/${n.id}/library`); } catch (ex) { errNote(holder, ex.message); return; }
  if (S.view !== v || !holder.isConnected) return;
  holder.textContent = "";
  S.runLib = lib;
  const models = (lib.gguf || []).concat(lib.strata || []);
  const slots = llmSlots(n);
  const rs = runningSlot(n);

  // ---- 上方：選模型
  const top = el("section", "card run-top rv");
  const th = el("div", "run-top-row");
  th.appendChild(el("span", "small muted", rs ? "執行中" : "準備執行"));
  const sel = el("select", "run-model");
  if (!models.length) {
    const o = el("option", null, "還沒有已安裝的模型");
    o.value = "";
    sel.appendChild(o);
  }
  models.forEach((m) => {
    const o = el("option", null, `${m.name}${m.quant ? "・" + m.quant : ""}・${fmt(m.size_gib)} GiB${m.in_use ? "（執行中）" : ""}`);
    o.value = m.id;
    sel.appendChild(o);
  });
  const want = S.runPick || models.find((m) => m.in_use)?.id || models[0]?.id || "";
  sel.value = models.some((m) => m.id === want) ? want : (models[0]?.id || "");
  S.runPick = null;
  const manage = el("button", "btn sm", "管理模型");
  manage.onclick = () => { S.libTab = "installed"; go("models"); };
  th.append(sel, el("span", "spacer"), manage);
  // 有好幾格語言模型欄位時才問放哪一格；只有一格或沒有就不打擾
  let slotSel = null;
  if (slots.length > 1) {
    slotSel = selectInput(slots.map((s) => [s.id, `${s.role}${s.tier ? "・" + s.tier : ""}　${s.unit ? (s.model || "") : "空欄位"}`]),
      S.runSlot || rs?.id || slots[0].id);
    slotSel.className = "run-slot";
    th.insertBefore(el("span", "small muted", "放在"), manage);
    th.insertBefore(slotSel, manage);
  }
  top.appendChild(th);
  holder.appendChild(top);

  if (!models.length) {
    const e = el("div", "note");
    e.appendChild(document.createTextNode("這台還沒有模型。先到「模型」挑一顆推薦的下載，或匯入你已經有的 GGUF。"));
    const b = el("button", "btn sm primary", "看推薦");
    b.onclick = () => { S.libTab = "rec"; go("models"); };
    e.appendChild(b);
    top.appendChild(e);
    if (rs) holder.appendChild(statusBar(n, rs));
    reveal(holder);
    return;
  }

  const grid = el("div", "grid pair run-grid");
  const left = card("自動分層規劃", "模型先放進最快的那一層，放不下的依序往下放");
  const right = card("執行設定");
  left.classList.add("rv");
  right.classList.add("rv");
  grid.append(left, right);
  // 載入進度放在最上面（LocModel 也是）：按下啟動後眼睛不用往下找
  const prog = el("div");
  holder.append(prog, grid);
  const statusHolder = el("div");
  holder.appendChild(statusHolder);
  if (rs) statusHolder.appendChild(statusBar(n, rs));

  const st = { ctx: null, thinking: "auto", lan: true, disk: false };
  const draw = () => {
    const m = models.find((x) => x.id === sel.value);
    const slotId = slotSel?.value || rs?.id || slots[0]?.id || null;
    const slot = slots.find((s) => s.id === slotId);
    const p = m.plan || null;
    const running = !!(m.in_use && slot && m.in_use === slot.id && slot.busy);
    drawPlan(left, n, m, p);
    drawSettings(right, n, m, p, slot, running, st, prog, statusHolder);
  };
  sel.onchange = () => { st.ctx = null; st.disk = false; draw(); };
  if (slotSel) slotSel.onchange = () => { S.runSlot = slotSel.value; draw(); };
  draw();
  reveal(holder);
}

function drawPlan(c, n, m, p) {
  c.querySelectorAll(":scope > :not(.card-head)").forEach((x) => x.remove());
  const pools = n.state.pools || {};
  const mm = pools.memory_model;
  const sp = p?.split || {};
  const rows = el("div", "plan-rows");
  const row = (kind, label, cap, used, note) => {
    const r = el("div", "plan-row " + kind);
    const h = el("div", "plan-row-head");
    h.append(el("b", null, label), el("span", "mono small", cap != null ? `${fmt(used || 0)} GiB / ${fmt(cap, 0)} GiB` : `${fmt(used || 0)} GiB`));
    const bar = el("div", "plan-bar");
    const fill = el("i");
    fill.style.width = cap ? `${Math.min(100, ((used || 0) / cap) * 100)}%` : (used ? "100%" : "0");
    bar.appendChild(fill);
    r.append(h, bar, el("div", "small muted", note));
    rows.appendChild(r);
  };
  const g = pools.unified || pools.vram;
  const gpuName = mm === "unified_single" ? "統一記憶體" : mm === "unified_split" ? "GPU 那一塊" : (n.state.chip?.gpu ? `顯示卡・${n.state.chip.gpu}` : "顯示卡");
  if (g || (pools.gpus || []).length) {
    row("gpu", gpuName, g?.total_gib, sp.gpu, sp.gpu ? (mm === "discrete" ? "模型層放顯示卡，最快" : "模型放在這一塊，最快") : "沒有放進這一層");
  }
  if (mm !== "unified_single") {
    row("ram", mm === "unified_split" ? "系統那一塊" : "系統記憶體", pools.ram?.total_gib, sp.ram,
      mm === "unified_split" ? "只給引擎用；吃爆會把整台拖死，所以模型不放這裡" : sp.ram ? "放不下顯示卡的專家層放這裡，比較慢" : `剩下的保留給系統（至少留 ${fmt(pools.floors?.ram, 0)} GiB）`);
  }
  row("disk", "硬碟・按需讀取", null, sp.disk,
    sp.disk ? "記憶體放不下的部分從硬碟映射，用到才讀；速度取決於硬碟，通常不到 1 tok/s" : "不需要");
  c.appendChild(rows);
  if (!p) {
    c.appendChild(el("p", "note", "還算不出這顆的分層（讀不到模型結構或這台的記憶體），載入前仍會做記憶體試算。"));
    return;
  }
  const msg = { smooth: `全部放進最快的一層，預估約 ${fmt(p.tok_s, 0)} tok/s，很順。`,
    ok: p.cpu_moe ? `${p.cpu_moe} 層專家放系統記憶體，能跑但比較慢${p.tok_s != null ? `（約 ${fmt(p.tok_s, 0)} tok/s）` : ""}。`
      : p.tok_s == null ? "放得下。這台的記憶體頻寬還沒填，算不出速度；到「這台的設定」填了就會標出來。" : `放得下，但速度偏慢（約 ${fmt(p.tok_s, 0)} tok/s）。`,
    disk: sp.disk ? `這台的記憶體放不下這顆，約 ${fmt(sp.disk, 0)} GiB 要從硬碟按需讀取；能跑，但很慢${p.tok_s != null ? `（約 ${p.tok_s < 1 ? "不到 1" : fmt(p.tok_s, 0)} tok/s）` : ""}，適合測試。`
      : "顯示卡放不下，部分層改放系統記憶體、權重用記憶體映射載入；能跑但比較慢。",
    no: p.place || "這台放不下這顆。" }[p.level] || "";
  c.appendChild(el("p", "note" + (p.level === "disk" ? " warn" : p.level === "no" ? " bad" : ""), msg));
}

function drawSettings(c, n, m, p, slot, running, st, prog, statusHolder) {
  c.querySelectorAll(":scope > :not(.card-head)").forEach((x) => x.remove());
  const isLlama = m.engine === "llama";
  // 上下文：自動＝模型預設；每個選項標 KV 要多少（跟換模型視窗同一條公式）
  const par = m.parallel || 1;
  const kvAt = (ctx) => ((m.kv_full_bpt || 0) * ctx + (m.kv_swa_bpt || 0) * Math.min(ctx, m.swa_window || ctx)) * par / 1073741824;
  if (isLlama) {
    c.appendChild(el("h4", "pick-h", `上下文長度・目前 ${st.ctx ? `${st.ctx / 1024}K` : "自動"}`));
    const bar = el("div", "tabbar run-ctx");
    const opts = [null, ...RUN_CTX_OPTS.filter((x) => !m.ctx_train || x <= m.ctx_train)];
    opts.forEach((ctx) => {
      const b = el("button", null, ctx ? `${ctx / 1024}K` : "自動");
      b.title = ctx ? `KV 約 ${fmt(kvAt(ctx))} GiB` : "用模型的預設";
      if (ctx === st.ctx) b.setAttribute("aria-current", "true");
      b.onclick = () => { st.ctx = ctx; drawSettings(c, n, m, p, slot, running, st, prog, statusHolder); };
      bar.appendChild(b);
    });
    c.appendChild(bar);
    const eff = st.ctx || m.ctx_default || Math.min(m.ctx_train || 8192, 32768);
    c.appendChild(el("p", "small muted", `越長能讀越長的文件，但佔記憶體；${int(eff)} 個 token 時 KV 約 ${fmt(kvAt(eff))} GiB。`
      + `「自動」用模型預設${m.ctx_train ? `，這顆最長 ${int(m.ctx_train)}` : ""}。`));
  }
  const toggle = (label, sub, on, set, disabled) => {
    const r = el("label", "switch-row");
    const t = el("div");
    t.append(el("b", null, label), el("div", "small muted", sub));
    const cb = el("input");
    cb.type = "checkbox";
    cb.className = "switch";
    cb.checked = on;
    cb.disabled = !!disabled;
    cb.onchange = () => set(cb.checked);
    r.append(t, cb);
    c.appendChild(r);
  };
  if (isLlama) {
    toggle("思考模式", "先想再答，難題答得比較好但比較慢（只對會思考的模型有用）", st.thinking === "on",
      (v) => { st.thinking = v ? "on" : "auto"; });
    toggle("區網存取", "讓區網裡的其他電腦（IDE、手機）連得到這個模型；關掉只有這台自己連得到", st.lan, (v) => { st.lan = v; });
  }

  const est = el("div", "tiles light run-est");
  est.append(
    tileOf("預估速度", p?.tok_s != null ? (p.tok_s < 1 ? "不到 1 tok/s" : `約 ${fmt(p.tok_s, 0)} tok/s`) : "未知",
      p?.tok_s != null ? "依記憶體頻寬估算" : "到這台的設定填記憶體頻寬"),
    tileOf("第一次載入", p?.load_sec != null ? `${fmt(p.load_sec, 0)} 秒` : p?.load_est_sec != null ? `約 ${fmtDur(p.load_est_sec)}` : "—",
      p?.load_sec != null ? "上次實際載入" : "依磁碟速度估"));
  c.appendChild(est);

  // 要用硬碟：管理者勾選確認才放行；BIOS 切兩塊的機器一律不行
  const diskNeed = p?.level === "disk";
  const err = el("div");
  if (diskNeed) {
    if (p.disk_allowed === false || n.state.pools?.memory_model === "unified_split") {
      c.appendChild(el("p", "note bad", "這台的記憶體是 BIOS 切成兩塊的（例 AI 395）：系統那塊被擠爆會把整台拖死，所以不允許用硬碟補。請換小一點的量化。"));
    } else {
      const r = el("label", "check-row");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = st.disk;
      cb.onchange = () => { st.disk = cb.checked; btn.disabled = !st.disk; };
      r.append(cb, document.createTextNode(p.split?.disk ? " 我了解：放不進記憶體的部分會從硬碟按需讀取，速度通常不到 1 tok/s"
        : " 我了解：這顆要分到顯示卡與系統記憶體、用記憶體映射載入，速度比較慢"));
      c.appendChild(r);
    }
  }
  const btn = el("button", "btn primary run-go", running ? "套用設定並重新載入" : "啟動模型");
  btn.disabled = p?.level === "no" || (diskNeed && !st.disk);
  btn.title = p?.level === "no" ? "這台放不下這顆" : "";
  c.append(btn, err);
  if (running && slot) {
    const stopB = el("button", "btn danger run-go", "停止模型");
    stopB.onclick = () => doAction(n, slot, "stop");
    c.appendChild(stopB);
  }
  btn.onclick = () => startModel(n, m, slot, st, btn, err, prog, statusHolder);
}

const fmtDur = (sec) => sec >= 90 ? `${Math.floor(sec / 60)} 分 ${Math.round(sec % 60)} 秒` : `${Math.round(sec)} 秒`;

// 啟動：沒有語言模型欄位就先建一格「主模型」；換模型的請求進行中，每秒問一次載入進度，只更新進度卡
async function startModel(n, m, slot, st, btn, err, prog, statusHolder, force = false) {
  errNote(err, "");
  btn.disabled = true;
  let s = slot;
  try {
    if (!s) {
      const r = await api(`/api/node/${n.id}/slots/add`, { method: "POST", body: { type: "empty", role: "主模型", tier: "", caps: ["文字"] } });
      s = r.slot;
    }
  } catch (ex) { errNote(err, ex.message); btn.disabled = false; return; }
  const key = n.id + "/" + s.id;
  S.busy.add(key);
  const req = m.engine === "llama"
    ? { engine: "llama", model: m.id, ...(st.ctx ? { ctx: st.ctx } : {}), thinking: st.thinking, lan: st.lan, ...(st.disk ? { allow_disk: true } : {}) }
    : { pack: m.pack, native: m.native };
  const pc = progressCard(m);
  prog.textContent = "";
  prog.appendChild(pc.el);
  let done = false;
  const poll = async () => {
    if (done || !pc.el.isConnected) return;
    // 進度讀不到（舊版代理）也不影響載入本身：只更新經過時間
    try { pc.update(await api(`/api/node/${n.id}/slot/${s.id}/progress`)); } catch (_) { pc.update({ stage: "read", pct: null, message: "這台的代理不回報細部進度，載入完成會自動跳到下一步" }); }
    if (!done) setTimeout(poll, 1000);
  };
  setTimeout(poll, 600);
  try {
    const r = await api(`/api/node/${n.id}/slot/${s.id}/model`, { method: "POST", body: { ...req, force } });
    done = true;
    pc.update({ state: "ready", stage: "done", pct: 100 });
    toast(`已啟動 ${r.model}`, "ok");
  } catch (ex) {
    done = true;
    pc.el.remove();
    const v = ex.data?.admit;
    if (v && !force) {
      const d = el("div");
      v.reasons.forEach((x) => d.appendChild(el("p", "note bad", x)));
      if (v.kv_note) d.appendChild(el("p", "note", v.kv_note));
      d.appendChild(el("p", "small muted", "統一記憶體的機器一旦吃爆，不是服務掛掉而是整台卡死，要到現場重開。可以先把上下文調短再試。"));
      modal({ title: `擋下來了：${m.name} 放不下`, bodyNode: d, confirmText: "我知道風險，仍要啟動", danger: true,
        onConfirm: () => startModel(n, m, s, st, btn, err, prog, statusHolder, true) });
    } else errNote(err, "啟動失敗：" + ex.message);
  }
  S.busy.delete(key);
  btn.disabled = false;
  await refresh();
  if (S.view === "run") viewRun();
}

const STAGES = [["prepare", "準備"], ["read", "讀取模型"], ["alloc", "分配快取"], ["warmup", "預熱"], ["done", "完成"]];

function progressCard(m) {
  const c = card(`正在啟動 ${m.name}`);
  c.classList.add("load-card");
  const pctEl = el("b", "load-pct mono", "0%");
  const h2 = c.querySelector("h2");
  c.querySelector(".card-head").append(el("span", "spacer"), pctEl);
  const sub = el("div", "small muted", "讀取模型資訊…");
  const track = el("div", "stage-track");
  const marks = STAGES.map(([, label]) => { const s = el("div", "stage", label); track.appendChild(s); return s; });
  const bar = el("div", "plan-bar load-bar");
  const fill = el("i");
  bar.appendChild(fill);
  const msg = el("div", "small mono muted load-msg");
  c.append(sub, track, bar, msg);
  const t0 = Date.now();
  return {
    el: c,
    update(r) {
      const i = Math.max(0, STAGES.findIndex(([k]) => k === r.stage));
      marks.forEach((x, j) => { x.className = "stage" + (j < i ? " done" : j === i ? " now" : ""); });
      // 讀權重的百分比只代表「讀取模型」那一段；整體進度依階段推進
      const pct = r.stage === "done" ? 100 : r.pct != null && r.stage === "read" ? Math.round(10 + r.pct * 0.7) : [3, 10, 82, 92, 100][i];
      pctEl.textContent = `${pct}%`;
      fill.style.width = `${pct}%`;
      const sec = r.elapsed_sec ?? Math.round((Date.now() - t0) / 1000);
      sub.textContent = [`已用 ${fmtDur(sec)}`, r.read_gib != null && r.total_gib ? `讀了 ${fmt(r.read_gib)} / ${fmt(r.total_gib)} GiB` : null,
        r.eta_sec != null ? `約再 ${fmtDur(r.eta_sec)}` : null].filter(Boolean).join("・");
      msg.textContent = r.state === "failed" ? (r.message || "載入失敗") : (r.message || "");
      if (r.stage === "done") h2.textContent = `已啟動 ${m.name}`;
    },
  };
}

/* ================================================================ 對話
   本機執行中的模型，或管理者在設定裡加的第三方 API。模型寫的程式碼區塊可以複製、下載，HTML 可以直接執行預覽
   （預覽頁走主控台另一個沙盒網址，拿不到登入 cookie）。對話紀錄只存在這個瀏覽器分頁（sessionStorage）。 */

const CHAT2 = { sessions: [], cur: null };
const TOOL_LABEL = { list_machines: "列出機器", machine_detail: "看這台的狀態", recommend_models: "查推薦模型", installed_models: "查已下載的模型" };
try { CHAT2.sessions = JSON.parse(sessionStorage.getItem("xconsole-chats") || "[]"); } catch (_) { CHAT2.sessions = []; }
const saveChats = () => {
  try {
    sessionStorage.setItem("xconsole-chats", JSON.stringify(CHAT2.sessions.slice(0, 30).map((s) => ({
      ...s, msgs: s.msgs.filter((m) => !m.pending).map(({ role, content, reasoning, stat, error }) => ({ role, content, reasoning, stat, error })) }))));
  } catch (_) { /* 空間不夠或無痕：只留在記憶體 */ }
};

function chatTargets2() {
  const out = [];
  (S.fleet?.nodes || []).filter((n) => n.online).forEach((n) => (n.state.slots || [])
    .filter((s) => isApiSlot(s) && s.busy && ((s.caps || []).includes("文字") || s.kind === "llama" || s.kind === "strata"))
    .forEach((s) => out.push({ key: `slot:${n.id}:${s.id}`, group: "本機模型", label: `${s.model || s.unit}（${n.name}）`, n, s,
      vision: (s.caps || []).includes("看圖"), local: true, url: `/api/node/${n.id}/chat/${s.id}` })));
  (S.providers || []).filter((p) => p.key_set).forEach((p) => out.push({ key: `prov:${p.id}`, group: "第三方 API",
    label: `${p.name}・${p.default_model || ""}`, prov: p, local: false, url: `/api/providers/${p.id}/chat`, cn: p.cn_origin }));
  // 主控台助理：用第一顆本機模型查機器狀態、推薦與已下載的模型（只能查，不會動作）
  const first = out.find((t) => t.local);
  if (first) out.push({ ...first, key: "assist", group: "主控台助理", label: `查機器狀態與推薦（用 ${first.s.model || first.s.role}）`, assist: true, vision: false });
  return out;
}

async function viewChat() {
  const main = $("#main");
  const v = S.view;
  if (!S.providers) {
    try { S.providers = (await api("/api/providers")).items || []; } catch (_) { S.providers = []; }
    if (S.view !== v) return;
  }
  main.textContent = "";
  const wrap = el("div", "wrap chat-wrap");
  main.appendChild(wrap);
  const targets = chatTargets2();
  const c = el("section", "card chat-card");
  wrap.appendChild(c);
  if (!targets.length) {
    c.appendChild(el("h2", null, "還沒有可以對話的模型"));
    c.appendChild(el("p", "small muted", "到「執行」啟動一顆模型，或請管理者在「設定 › 第三方 API」加一個服務。"));
    const b = el("button", "btn primary", "去執行");
    b.onclick = () => go("run");
    c.appendChild(b);
    return;
  }
  let cur = CHAT2.sessions.find((x) => x.id === CHAT2.cur);
  if (!cur) cur = newSession(targets[0].key);
  if (!targets.some((t) => t.key === cur.target)) cur.target = targets[0].key;

  // ---- 頂列：模型、狀態、新對話、對話紀錄
  const bar = el("div", "chat-bar");
  const sel = el("select", "chat-target");
  const grp = {};
  targets.forEach((t) => {
    if (!grp[t.group]) { grp[t.group] = el("optgroup"); grp[t.group].label = t.group; sel.appendChild(grp[t.group]); }
    const o = el("option", null, t.label + (t.cn ? "（中國來源）" : ""));
    o.value = t.key;
    grp[t.group].appendChild(o);
  });
  sel.value = cur.target;
  const status = el("span", "small muted chat-status");
  const newB = el("button", "btn sm", "新對話");
  const histB = el("button", "btn sm", `對話紀錄（${CHAT2.sessions.length}）`);
  bar.append(sel, status, el("span", "spacer"), newB, histB);
  c.appendChild(bar);
  const tgt = () => targets.find((t) => t.key === sel.value);
  const syncStatus = () => {
    const t = tgt();
    status.textContent = t.local ? `本機執行・對話不離開這台${cur.lastSpeed ? `・${fmt(cur.lastSpeed, 1)} tok/s` : ""}`
      : `第三方 API・對話內容會送到 ${t.prov.name}${t.cn ? "・中國來源，政府案不得使用" : ""}`;
    status.classList.toggle("warn-t", !t.local);
  };

  const box = el("div", "chat-box");
  c.appendChild(box);
  const drawAll = () => {
    box.textContent = "";
    if (!cur.msgs.length) {
      const e = el("div", "chat-empty");
      e.append(el("h3", null, "有什麼可以幫你？"), el("p", "small muted", tgt().local ? "模型跑在你自己的機器上，對話不會上傳。" : "這個模型在外部服務，對話會送出去。"));
      const ex = el("div", "chips chat-ex");
      ["用 HTML 做一個會動的時鐘", "幫我把這段話改得更正式", "解釋什麼是 MoE 模型"].forEach((x) => {
        const b = el("button", "chip-btn", x);
        b.onclick = () => { ta.value = x; ta.focus(); };
        ex.appendChild(b);
      });
      e.appendChild(ex);
      box.appendChild(e);
    }
    cur.msgs.forEach((m) => box.appendChild(chatBubble(m)));
    box.scrollTop = box.scrollHeight;
  };

  const inputRow = el("div", "chat-input");
  const ta = el("textarea");
  ta.rows = 2;
  ta.placeholder = "問點什麼（Enter 送出，Shift+Enter 換行）";
  ta.value = cur.draft || "";
  ta.oninput = () => { cur.draft = ta.value; };
  const tools = el("div", "chat-tools");
  const attach = el("input");
  attach.type = "file";
  attach.accept = "image/png,image/jpeg,image/webp";
  attach.hidden = true;
  const attBtn = el("button", "btn sm", "附圖");
  attBtn.onclick = () => attach.click();
  attach.onchange = async () => {
    const f = attach.files[0];
    if (!f) return;
    if (f.size > 4 * 1024 * 1024) { toast("圖片最大 4 MB", "bad"); return; }
    cur.image = "data:" + f.type + ";base64," + await readB64(f);
    attBtn.textContent = "已附圖（再按換一張）";
  };
  const send = el("button", "btn primary", "送出");
  const stopB = el("button", "btn sm danger", "停止");
  stopB.hidden = true;
  tools.append(attach, attBtn, el("span", "spacer"), stopB, send);
  inputRow.append(ta, tools);
  c.appendChild(inputRow);
  const syncVision = () => { attBtn.hidden = !tgt()?.vision; };
  sel.onchange = () => { cur.target = sel.value; saveChats(); syncVision(); syncStatus(); };
  syncVision();
  syncStatus();
  drawAll();
  newB.onclick = () => { if (cur.busy) return; cur = newSession(sel.value); drawAll(); histB.textContent = `對話紀錄（${CHAT2.sessions.length}）`; ta.focus(); };
  histB.onclick = () => openChatHistory((id) => { CHAT2.cur = id; viewChat(); });

  const submit = async () => {
    const text = ta.value.trim();
    if (!text || cur.busy) return;
    const t = tgt();
    const user = { role: "user", content: text, image: cur.image || null };
    if (!cur.msgs.length) cur.title = text.slice(0, 24);
    cur.msgs.push(user);
    const reply = { role: "assistant", content: "", pending: true };
    cur.msgs.push(reply);
    ta.value = ""; cur.draft = ""; cur.image = null; attBtn.textContent = "附圖";
    if (box.querySelector(".chat-empty")) box.textContent = "";
    box.append(chatBubble(user), chatBubble(reply));
    box.scrollTop = box.scrollHeight;
    cur.busy = true; send.disabled = true; stopB.hidden = false;
    const sent = cur.msgs.filter((m) => !m.pending && !m.error);
    const lastImg = sent.map((m) => !!m.image).lastIndexOf(true);
    const history = sent.map((m, i) => ({ role: m.role,
      content: m.image && i === lastImg ? [{ type: "text", text: m.content }, { type: "image_url", image_url: { url: m.image } }]
        : m.image ? `（先前附了一張圖）${m.content}` : m.content }));
    const t0 = performance.now();
    try {
      cur.ctl = new AbortController();
      if (t.assist) {
        const r = await api("/api/assistant", { method: "POST", signal: cur.ctl.signal, body: { node: t.n.id, slot: t.s.id, messages: history } });
        Object.assign(reply, { content: r.content, reasoning: r.reasoning, pending: false,
          stat: [r.steps?.length ? `查了：${r.steps.map((x) => TOOL_LABEL[x.tool] || x.tool).join("、")}` : null,
            `${fmt((performance.now() - t0) / 1000, 1)} 秒`].filter(Boolean).join("・") });
        return;
      }
      const body = { messages: history, stream: true, max_tokens: 8192, ...(t.prov ? { model: t.prov.default_model } : {}) };
      const res = await fetch(t.url, { method: "POST", credentials: "same-origin", signal: cur.ctl.signal,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.error || `伺服器回 ${res.status}`); }
      const rd = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "", timings = null, usage = null, last = 0;
      reply.pending = false;
      for (;;) {
        const { value, done } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const ln = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (!ln.startsWith("data:")) continue;
          const d = ln.slice(5).trim();
          if (d === "[DONE]") continue;
          let j;
          try { j = JSON.parse(d); } catch (_) { continue; }
          if (j.timings) timings = j.timings;
          if (j.usage) usage = j.usage;
          const delta = j.choices?.[0]?.delta || {};
          if (delta.reasoning_content) reply.reasoning = (reply.reasoning || "") + delta.reasoning_content;
          if (delta.content) reply.content += delta.content;
        }
        // 只重畫這一則（最多每 120 毫秒一次），不重畫整個對話；使用者往上捲在看舊訊息時不把他拉回底部
        if (performance.now() - last > 120) { last = performance.now(); paintReply(reply, box); }
      }
      const sp = timings?.predicted_per_second;
      if (sp) cur.lastSpeed = sp;
      reply.stat = [sp ? `${fmt(sp, 1)} tok/s（實測）` : null, usage?.completion_tokens ? `${usage.completion_tokens} tokens` : null,
        `${fmt((performance.now() - t0) / 1000, 1)} 秒`].filter(Boolean).join("・");
    } catch (ex) {
      reply.pending = false;
      if (ex.name === "AbortError") reply.stat = "已停止";
      else reply.error = ex.message;
    } finally {
      cur.busy = false; send.disabled = false; stopB.hidden = true; cur.ctl = null;
      if (!reply.content && !reply.error && reply.stat !== "已停止") reply.content = "（沒有回答）";
      paintReply(reply, box, true);
      syncStatus();
      saveChats();
    }
  };
  send.onclick = submit;
  stopB.onclick = () => cur.ctl?.abort();
  imeGuard(ta, submit, { shiftNewline: true });
  ta.focus();
}

function newSession(target) {
  const s = { id: Math.random().toString(36).slice(2, 10), title: "新對話", target, msgs: [], at: Date.now() };
  CHAT2.sessions.unshift(s);
  CHAT2.cur = s.id;
  saveChats();
  return s;
}

function openChatHistory(pick) {
  const d = el("div", "pick-list chat-hist");
  let ov;
  if (!CHAT2.sessions.length) d.appendChild(el("p", "small muted", "還沒有對話。"));
  CHAT2.sessions.forEach((s) => {
    const r = el("div", "hist-row");
    const b = el("button", "btn", `${s.title || "新對話"}　${new Date(s.at).toLocaleString("zh-TW", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`);
    b.onclick = () => { ov.remove(); pick(s.id); };
    const del = el("button", "btn sm danger", "刪除");
    del.onclick = () => {
      CHAT2.sessions = CHAT2.sessions.filter((x) => x.id !== s.id);
      if (CHAT2.cur === s.id) CHAT2.cur = null;
      saveChats();
      r.remove();
    };
    r.append(b, del);
    d.appendChild(r);
  });
  d.appendChild(el("p", "small muted", "對話只存在這個瀏覽器分頁，關掉分頁就沒了；不會存到主控台。"));
  ov = modal({ title: "對話紀錄", bodyNode: d });
}

// 一則訊息：文字與程式碼區塊分開畫，程式碼區塊附複製、下載，HTML 另有「執行預覽」
function chatBubble(m) {
  const b = el("div", "msg " + m.role);
  m._el = b;
  paintMsg(m);
  return b;
}

function paintReply(m, box, final) {
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  paintMsg(m, final);
  if (near) box.scrollTop = box.scrollHeight;
}

function paintMsg(m, final) {
  const b = m._el;
  if (!b) return;
  b.textContent = "";
  if (m.image) { const im = el("img", "msg-img"); im.src = m.image; im.alt = "附加的圖片"; b.appendChild(im); }
  if (m.reasoning) {
    const d = el("details", "msg-think");
    d.open = !m.content && !final;
    d.appendChild(el("summary", null, m.content ? "思考過程" : "思考中…"));
    d.appendChild(el("div", "small muted", m.reasoning));
    b.appendChild(d);
  }
  if (m.role === "assistant") renderRich(b, m.content || (m.pending ? "…" : ""), !final && !m.stat && !m.error);
  else b.appendChild(el("div", "msg-text", m.content));
  if (m.stat) b.appendChild(el("div", "msg-stat small muted", m.stat));
  if (m.error) b.appendChild(el("p", "note bad", m.error));
}

// 把 ``` 圍起來的程式碼切出來；串流中最後一段沒收尾也照程式碼畫
function renderRich(box, text, streaming) {
  const parts = text.split(/^```/m);
  parts.forEach((seg, i) => {
    if (i % 2 === 0) {
      if (seg.trim()) box.appendChild(el("div", "msg-text", seg.replace(/^\n+|\n+$/g, "")));
      return;
    }
    const nl = seg.indexOf("\n");
    const lang = (nl >= 0 ? seg.slice(0, nl) : seg).trim().toLowerCase();
    const code = nl >= 0 ? seg.slice(nl + 1).replace(/\n$/, "") : "";
    box.appendChild(codeBlock(lang, code, streaming && i === parts.length - 1));
  });
}

function codeBlock(lang, code, open) {
  const w = el("div", "code-block");
  const h = el("div", "code-head");
  const isHtml = lang === "html" || /^\s*<!doctype html|^\s*<html/i.test(code);
  h.appendChild(el("span", "mono small", lang || "程式碼"));
  h.appendChild(el("span", "spacer"));
  if (!open) {
    const cp = el("button", "btn sm", "複製");
    cp.onclick = () => copyText(code);
    const dl = el("button", "btn sm", isHtml ? "下載 .html" : "下載");
    dl.onclick = () => download(isHtml ? "page.html" : `code.${extOf(lang)}`, code, (isHtml ? "text/html" : "text/plain") + ";charset=utf-8");
    h.append(cp, dl);
    if (isHtml) {
      const pv = el("button", "btn sm primary", "執行預覽");
      pv.onclick = () => openPreview(code);
      h.appendChild(pv);
    }
  } else h.appendChild(el("span", "small muted", "產生中…"));
  const pre = el("pre", "mono");
  pre.textContent = code;
  w.append(h, pre);
  return w;
}

const extOf = (lang) => ({ python: "py", py: "py", javascript: "js", js: "js", typescript: "ts", ts: "ts", bash: "sh", sh: "sh",
  shell: "sh", css: "css", json: "json", yaml: "yml", sql: "sql", go: "go", rust: "rs", java: "java", c: "c", cpp: "cpp", markdown: "md" }[lang] || "txt");

// 預覽：HTML 交給主控台存一份，回一個沙盒網址（不透明來源、30 分鐘過期），用全螢幕框架開
async function openPreview(html) {
  let r;
  try { r = await api("/api/preview", { method: "POST", body: { html } }); } catch (ex) { toast("預覽失敗：" + ex.message, "bad"); return; }
  const ov = el("div", "preview-ov");
  const bar = el("div", "preview-bar");
  bar.appendChild(el("span", "small", "執行預覽・在沙盒裡執行，拿不到主控台的登入；按 Esc 關閉"));
  bar.appendChild(el("span", "spacer"));
  const tab = el("a", "btn sm", "開新分頁");
  tab.href = r.url;
  tab.target = "_blank";
  tab.rel = "noopener";
  const close = el("button", "btn sm primary", "關閉");
  bar.append(tab, close);
  const fr = el("iframe", "preview-frame");
  fr.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-popups");
  fr.src = r.url;
  ov.append(bar, fr);
  const done = () => { ov.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") done(); };
  close.onclick = done;
  document.addEventListener("keydown", esc);
  document.body.appendChild(ov);
}

/* ================================================================ Agent
   交給 Agent 一件事：它在這台機器的工作資料夾裡規劃、建檔、寫程式、跑指令。每條指令都要人按允許
   （可以勾「這個任務之後不再問」）。檔案只能動工作資料夾裡的；指令在這台以代理的身分執行。 */

async function viewAgent() {
  const main = $("#main");
  const v = S.view;
  const n = curNode();
  main.textContent = "";
  const wrap = el("div", "wrap");
  main.appendChild(wrap);
  if (!n || !n.online) { wrap.appendChild(el("p", "note warn", n ? `${n.name} 連不上。` : "還沒有機器。")); return; }
  if (!S.providers) { try { S.providers = (await api("/api/providers")).items || []; } catch (_) { S.providers = []; } }
  let folders;
  try { folders = await api(`/api/node/${n.id}/agent/folders`); } catch (ex) {
    if (S.view !== v) return;
    const c = card("Agent 還沒在這台開啟");
    c.appendChild(el("p", "small muted", "Agent 會在這台機器上建檔、跑指令。為了安全，每台預設關閉，要管理者在「這台的設定」打開；"
      + "打開後檔案只能動 ~/xconsole-agent/ 底下的工作資料夾，每條指令都要人按允許。"));
    c.appendChild(el("p", "note", ex.message));
    const b = el("button", "btn primary", "到這台的設定開啟");
    b.onclick = () => openMachineSettings(n.id);
    c.appendChild(b);
    wrap.appendChild(c);
    return;
  }
  if (S.view !== v) return;
  let tasks = [];
  try { tasks = (await api("/api/agent/tasks")).items || []; } catch (_) { /* 沒有就從頭開始 */ }
  if (S.view !== v) return;

  const models = [];
  llmSlots(n).filter((s) => s.busy && s.unit && s.port).forEach((s) => models.push([`slot:${n.id}:${s.id}`, `${s.model || s.role}（本機）`]));
  (S.providers || []).filter((p) => p.key_set).forEach((p) => models.push([`prov:${p.id}`, `${p.name}・${p.default_model || ""}${p.cn_origin ? "（中國來源）" : ""}`]));

  const grid = el("div", "agent-grid");
  const mainC = el("section", "card agent-main");
  const side = el("aside", "card agent-side");
  grid.append(mainC, side);
  wrap.appendChild(grid);

  const bar = el("div", "chat-bar");
  const mSel = selectInput(models.length ? models : [["", "沒有可用的模型"]], S.agentModel || models[0]?.[0]);
  const fSel = selectInput([["", "選擇工作資料夾…"], ...(folders.folders || []).map((f) => [f.name, `${f.name}（${f.files} 個檔案）`])], S.agentFolder || "");
  const newF = el("button", "btn sm", "新增資料夾");
  const taskSel = selectInput([["", "新任務"], ...tasks.map((t) => [t.id, `${(t.prompt || t.id).slice(0, 20)}（${TASK_STATE[t.state] || t.state}）`])], S.agentTask || "");
  bar.append(mSel, fSel, newF, el("span", "spacer"), taskSel);
  mainC.appendChild(bar);
  mSel.onchange = () => { S.agentModel = mSel.value; };
  fSel.onchange = () => { S.agentFolder = fSel.value; drawSide(); };
  taskSel.onchange = () => { S.agentTask = taskSel.value; viewAgent(); };
  newF.onclick = () => {
    const d = el("div");
    const inp = textInput("");
    inp.placeholder = "例：port-morrow-game";
    d.appendChild(fieldBox("資料夾名稱", inp, "英數、連字號、底線；會建在 ~/xconsole-agent/ 底下"));
    const er = el("div");
    d.appendChild(er);
    const ov = modal({ title: "新增工作資料夾", bodyNode: d, hasInput: true });
    formButton(ov, "建立", "primary", async () => {
      try { await api(`/api/node/${n.id}/agent/folders/create`, { method: "POST", body: { name: inp.value.trim() } }); }
      catch (ex) { errNote(er, ex.message); return; }
      ov.remove();
      S.agentFolder = inp.value.trim();
      viewAgent();
    });
  };

  const logBox = el("div", "agent-log");
  mainC.appendChild(logBox);
  const inputRow = el("div", "chat-input");
  const ta = el("textarea");
  ta.rows = 2;
  ta.placeholder = "描述要做的事，越具體越好（Enter 送出，Shift+Enter 換行）";
  const stopB = el("button", "btn sm danger", "停止");
  stopB.hidden = true;
  const send = el("button", "btn primary", "交給 Agent");
  const tools = el("div", "chat-tools");
  tools.append(el("span", "small muted", "檔案只能動工作資料夾裡的；每條指令都會先問你"), el("span", "spacer"), stopB, send);
  inputRow.append(ta, tools);
  mainC.appendChild(inputRow);

  const planBox = el("div");
  const filesBox = el("div");
  const treeBox = el("div");
  side.append(el("h4", "pick-h", "計畫"), planBox, el("h4", "pick-h", "改動的檔案"), filesBox, el("h4", "pick-h", "工作資料夾"), treeBox);
  const drawSide = (t) => {
    planBox.textContent = "";
    filesBox.textContent = "";
    treeBox.textContent = "";
    const plan = t?.plan || [];
    if (!plan.length) planBox.appendChild(el("p", "small muted", "Agent 開始做事後，計畫會顯示在這裡"));
    plan.forEach((x) => planBox.appendChild(el("div", "plan-item " + (x.status || ""), x.text)));
    const files = t?.files || [];
    if (!files.length) filesBox.appendChild(el("p", "small muted", "還沒有改動"));
    files.forEach((f) => {
      const r = el("div", "file-row");
      r.append(el("span", "chip " + (f.action === "created" ? "ok" : "warn"), f.action === "created" ? "新建" : "修改"), el("span", "mono small", f.path));
      filesBox.appendChild(r);
    });
    const tree = t?.tree || [];
    treeBox.appendChild(el("p", "small muted", fSel.value ? `~/xconsole-agent/${fSel.value}` : "還沒選資料夾"));
    if (tree.length) {
      const pre = el("pre", "mono small agent-tree");
      pre.textContent = tree.map((x) => (x.dir ? x.path + "/" : `${x.path}  (${x.size != null ? fmtBytes(x.size) : ""})`)).join("\n");
      treeBox.appendChild(pre);
    }
  };

  const empty = () => {
    logBox.textContent = "";
    const e = el("div", "chat-empty");
    e.append(el("h3", null, "交給 Agent 一個任務"),
      el("p", "small muted", "它會在工作資料夾裡自己規劃、建檔、寫程式、跑測試，直到完成。"));
    const ex = el("div", "chips chat-ex");
    ["做一個番茄鐘網頁，打開 index.html 就能用", "寫一個 Python 腳本，把資料夾裡的圖片批次縮到 1080p", "讀一下這個專案，寫一份 README 說明怎麼執行", "整理這個資料夾：照檔案類型分到不同子資料夾"]
      .forEach((x) => { const b = el("button", "chip-btn", x); b.onclick = () => { ta.value = x; ta.focus(); }; ex.appendChild(b); });
    e.appendChild(ex);
    logBox.appendChild(e);
  };

  let shown = 0;
  const paint = (t) => {
    if (!shown) logBox.textContent = "";
    const head = shown === 0 && t.prompt ? [{ type: "user", text: t.prompt }] : [];
    // 計畫與完成在右側面板與最後的總結看得到，不在步驟裡重複列
    head.concat((t.log || []).slice(shown)).filter((x) => !["update_plan", "finish"].includes(x.tool))
      .forEach((x) => logBox.appendChild(agentEntry(x)));
    shown = (t.log || []).length;
    logBox.scrollTop = logBox.scrollHeight;
    drawSide(t);
    const live = t.state === "running" || t.state === "waiting_approval";
    stopB.hidden = !live;
    send.disabled = live;
  };

  const watch = async (id) => {
    let asked = null;
    const tick = async () => {
      if (!logBox.isConnected) return;
      let t;
      try { t = await api(`/api/agent/tasks/${id}`); } catch (ex) { toast(ex.message, "bad"); return; }
      paint(t);
      if (t.state === "waiting_approval" && t.pending && asked !== t.pending.id) {
        asked = t.pending.id;
        askApprove(t);
      }
      if (t.state === "running" || t.state === "waiting_approval") setTimeout(tick, 1500);
      else if (t.state === "done") { toast("Agent 完成了", "ok"); if (t.summary && !logBox.querySelector(".agent-summary")) { const b = el("div", "msg assistant agent-summary"); renderRich(b, t.summary, false); logBox.appendChild(b); logBox.scrollTop = logBox.scrollHeight; } }
      else if (t.state === "failed") toast("Agent 停在錯誤：" + (t.error || ""), "bad");
    };
    tick();
  };

  const askApprove = (t) => {
    const d = el("div");
    d.appendChild(el("p", "small", `Agent 想在 ~/xconsole-agent/${t.folder} 裡執行這條指令，請確認它是安全的再允許：`));
    const pre = el("pre", "mono code-pre");
    pre.textContent = t.pending.command;
    d.appendChild(pre);
    if (t.pending.why) d.appendChild(el("p", "small muted", t.pending.why));
    const r = el("label", "check-row");
    const cb = el("input");
    cb.type = "checkbox";
    r.append(cb, document.createTextNode(" 這個任務之後的指令不再詢問"));
    d.appendChild(r);
    const ov = modal({ title: "Agent 想執行一條指令", bodyNode: d, hasInput: true });
    const answer = async (allow) => {
      ov.remove();
      try { await api(`/api/agent/tasks/${t.id}/approve`, { method: "POST", body: { allow, always: allow && cb.checked } }); }
      catch (ex) { toast(ex.message, "bad"); }
    };
    const actions = ov.querySelector(".actions");
    actions.textContent = "";
    const no = el("button", "btn", "拒絕");
    no.onclick = () => answer(false);
    const yes = el("button", "btn primary", "允許");
    yes.onclick = () => answer(true);
    actions.append(no, yes);
  };

  send.onclick = async () => {
    const text = ta.value.trim();
    if (!text) return;
    if (!mSel.value) { toast("先到「執行」啟動一顆模型，或在設定加第三方 API", "bad"); return; }
    if (!fSel.value) { toast("先選一個工作資料夾（沒有就按「新增資料夾」）", "bad"); return; }
    const [kind, a, b] = mSel.value.split(":");
    const model = kind === "slot" ? { kind: "local", node: a, slot: b } : { kind: "provider", id: a };
    let r;
    try { r = await api("/api/agent/tasks", { method: "POST", body: { node: n.id, folder: fSel.value, model, prompt: text } }); }
    catch (ex) { toast(ex.message, "bad"); return; }
    ta.value = "";
    S.agentTask = r.id;
    shown = 0;
    watch(r.id);
  };
  stopB.onclick = async () => { try { await api(`/api/agent/tasks/${S.agentTask}/stop`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); } };
  imeGuard(ta, () => send.click(), { shiftNewline: true });

  if (S.agentTask && tasks.some((t) => t.id === S.agentTask)) watch(S.agentTask); else { empty(); drawSide(null); }
}

const TASK_STATE = { running: "進行中", waiting_approval: "等你允許", done: "完成", failed: "失敗", stopped: "已停止" };
const fmtBytes = (b) => b >= 1048576 ? `${fmt(b / 1048576)} MB` : b >= 1024 ? `${fmt(b / 1024, 0)} KB` : `${b} B`;
const TOOL_ZH = { list_files: "列出檔案", read_file: "讀檔", write_file: "寫檔", run_command: "執行指令", update_plan: "更新計畫", finish: "完成" };

function agentEntry(x) {
  if (x.type === "user") return el("div", "msg user", x.text);
  if (x.type === "text") { const b = el("div", "msg assistant"); renderRich(b, x.text || "", false); return b; }
  if (x.type === "error") return el("p", "note bad", x.text || x.error || "錯誤");
  const r = el("div", "agent-step" + (x.type === "result" ? " result" : ""));
  if (x.type === "tool") {
    const a = x.args || {};
    r.append(el("span", "chip idle", TOOL_ZH[x.tool] || x.tool),
      el("span", "mono small", a.command || a.path || (x.tool === "update_plan" ? "" : x.tool === "finish" ? "" : JSON.stringify(a).slice(0, 160))));
  } else {
    r.append(el("span", "chip " + (x.ok === false ? "bad" : "ok"), x.ok === false ? "失敗" : "完成"), el("span", "small muted", TOOL_ZH[x.tool] || x.tool || ""));
    if (x.text) {
      let out = x.text;
      try {
        const j = JSON.parse(x.text);
        out = j.error ? j.error : [j.stdout, j.stderr, j.content, j.files && j.files.join("\n"), j.code != null ? `結束碼 ${j.code}${j.timed_out ? "（逾時）" : ""}` : null]
          .filter((v) => v !== undefined && v !== null && v !== "").join("\n") || x.text;
      } catch (_) { /* 不是 JSON 就照原文 */ }
      const d = el("details");
      d.appendChild(el("summary", "small", "結果"));
      const pre = el("pre", "mono small");
      pre.textContent = out;
      d.appendChild(pre);
      r.appendChild(d);
    }
  }
  return r;
}

/* ================================================================ 畫圖、影片
   寫一句描述就產出；模型與引擎沒準備好時帶去模型頁的「畫圖與影片模型」。 */

async function viewCreate(kind) {
  const main = $("#main");
  const v = S.view;
  const n = curNode();
  main.textContent = "";
  const wrap = el("div", "wrap");
  main.appendChild(wrap);
  if (!n || !n.online) { wrap.appendChild(el("p", "note warn", n ? `${n.name} 連不上。` : "還沒有機器。")); return; }
  const head = el("div", "page-head");
  head.appendChild(el("h1", null, kind === "image" ? "畫圖" : "生成影片"));
  head.appendChild(el("span", "spacer"));
  const dl = el("button", "btn sm", kind === "image" ? "下載畫圖模型" : "下載影片模型");
  dl.onclick = () => { S.libTab = "sd"; S.sdSub = "rec"; go("models"); };
  head.appendChild(dl);
  wrap.appendChild(head);
  const holder = el("div");
  wrap.appendChild(holder);
  loading(holder);
  let rec;
  try { rec = await getSdRec(n.id); } catch (ex) { errNote(holder, ex.message); return; }
  if (S.view !== v || !holder.isConnected) return;
  holder.textContent = "";
  const redraw = () => { delete S.sdRec[n.id]; viewCreate(kind); };
  holder.appendChild(sdGen(n, rec, redraw, () => { S.libTab = "sd"; S.sdSub = "rec"; go("models"); }, kind));
  reveal(holder);
}

/* ================================================================ 設定：推理引擎、第三方 API */

function settingsEngine(wrap) {
  const c = card("推理引擎", "每台機器的 llama.cpp 與畫圖引擎（stable-diffusion.cpp）；依顯示卡自動選加速方式，一鍵安裝或更新");
  const tb = el("table", "api-table");
  tb.innerHTML = "<thead><tr><th>機器</th><th>llama.cpp</th><th>畫圖引擎</th><th></th></tr></thead>";
  const tbody = el("tbody");
  (S.fleet.nodes || []).forEach((n) => {
    const e = n.state?.engine || {};
    const tr = el("tr");
    tr.appendChild(el("td", null, n.name + (n.online ? "" : "（連不上）")));
    tr.appendChild(el("td", "small", e.building ? "安裝中…" : e.llama_server ? "已安裝" : "還沒安裝"));
    tr.appendChild(el("td", "small", e.sd_building ? "安裝中…" : e.sd_cli ? "已安裝" : "還沒安裝"));
    const td = el("td", "row-acts");
    const a = el("button", "btn sm" + (e.llama_server ? "" : " primary"), e.llama_server ? "更新 llama.cpp" : "安裝 llama.cpp");
    a.disabled = !n.online;
    a.onclick = () => openEngineBuild(n.id);
    const b = el("button", "btn sm", e.sd_cli ? "更新畫圖引擎" : "安裝畫圖引擎");
    b.disabled = !n.online;
    b.onclick = () => openEngineBuild(n.id, "sd");
    td.append(a, b);
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
  tb.appendChild(tbody);
  c.appendChild(tb);
  c.appendChild(el("p", "small muted", "已經有自己編好的 llama-server，到「機器 › 這台的設定」指定路徑即可。"));
  wrap.appendChild(c);
}

async function settingsProviders(wrap) {
  const c = card("第三方 API", "本機模型放不下、或要用更強的模型時，接 OpenAI 相容的雲端服務；對話與 Agent 可以在頂部切換本機或這些服務");
  wrap.appendChild(c);
  const head = c.querySelector(".card-head");
  head.appendChild(el("span", "spacer"));
  const add = el("button", "btn sm primary", "新增 API");
  head.appendChild(add);
  const body = el("div");
  c.appendChild(body);
  loading(body);
  let r;
  try { r = await api("/api/providers"); } catch (ex) { errNote(body, ex.message); return; }
  S.providers = r.items || [];
  body.textContent = "";
  body.appendChild(el("p", "note warn", "對話內容會送到該服務的伺服器。政府案與含個資的對話請用本機模型；中國來源的服務政府案不得使用。"));
  if (!S.providers.length) body.appendChild(el("p", "small muted", "還沒有第三方 API。"));
  S.providers.forEach((p) => {
    const row = el("div", "prov-row");
    const t = el("div");
    const name = el("b", null, p.name);
    t.appendChild(name);
    if (p.cn_origin) t.appendChild(el("span", "chip bad", "中國來源・政府案不可用"));
    t.appendChild(el("div", "small muted mono", `${p.base_url}・${p.default_model || "未設預設模型"}・金鑰${p.key_set ? "已設定" : "未設定"}`));
    const ed = el("button", "btn sm", "編輯");
    ed.onclick = () => openProvider(r.presets || [], p);
    const del = el("button", "btn sm danger", "刪除");
    del.onclick = () => modal({ title: `刪除 ${p.name}`, bodyNode: el("p", null, "金鑰會一起刪除，對話與 Agent 就不能再選這個服務。"),
      confirmText: "刪除", danger: true, onConfirm: async () => {
        try { await api(`/api/providers/${p.id}/delete`, { method: "POST", body: {} }); toast("已刪除", "ok"); } catch (ex) { toast(ex.message, "bad"); }
        viewSettings();
      } });
    const acts = el("div", "row-acts");
    acts.append(ed, del);
    row.append(t, acts);
    body.appendChild(row);
  });
  body.appendChild(el("p", "small muted", "金鑰只存在主控台這台，以 AES-256-GCM 加密；畫面與記錄永遠不會顯示金鑰內容。"));
  add.onclick = () => openProvider(r.presets || [], null);
}

function openProvider(presets, p) {
  const d = el("div");
  const fPre = selectInput(presets.map((x) => [x.key, x.name]), presets[0]?.key);
  const fName = textInput(p?.name || "");
  const fUrl = textInput(p?.base_url || "");
  fUrl.placeholder = "https://…/v1";
  const fKey = el("input");
  fKey.type = "password";
  fKey.autocomplete = "off";
  fKey.placeholder = p?.key_set ? "已設定；留空＝不更改" : "貼上 API Key";
  const fModel = textInput(p?.default_model || "");
  const listB = el("button", "btn sm", "取得模型清單");
  const hint = el("p", "small muted");
  const applyPreset = () => {
    const x = presets.find((y) => y.key === fPre.value);
    if (!x || p) return;
    fName.value = x.name;
    fUrl.value = x.base_url || "";
    fModel.value = x.default_model || "";
    hint.textContent = x.note || "";
  };
  fPre.onchange = () => { applyPreset(); };
  if (!p) applyPreset();
  const mRow = el("div", "prov-model-row");
  const mBox = fieldBox("預設模型", fModel);
  mRow.append(mBox, listB);
  d.append(...(p ? [] : [fieldBox("服務商", fPre)]), fieldBox("名稱", fName), fieldBox("網址（OpenAI 相容）", fUrl, "要 https；只有內網位址可以用 http"),
    fieldBox("API Key", fKey), mRow, hint);
  const err = el("div");
  d.appendChild(err);
  const ov = modal({ title: p ? `編輯 ${p.name}` : "新增第三方 API", bodyNode: d, hasInput: true });
  const save = async () => {
    const r = await api("/api/providers/save", { method: "POST", body: { ...(p ? { id: p.id } : {}), name: fName.value.trim(),
      base_url: fUrl.value.trim(), api_key: fKey.value, default_model: fModel.value.trim() } });
    fKey.value = "";
    p = r.provider || p;
    return r;
  };
  listB.onclick = async () => {
    errNote(err, "");
    try {
      const r = await save();
      const id = (r.provider || p).id;
      const m = await api(`/api/providers/${id}/models`, { method: "POST", body: {} });
      const dl = el("datalist");
      dl.id = "prov-models";
      (m.models || []).forEach((x) => { const o = el("option"); o.value = x.id; if (x.cn_origin) o.label = "中國來源"; dl.appendChild(o); });
      d.appendChild(dl);
      fModel.setAttribute("list", "prov-models");
      if (!fModel.value && m.models?.length) fModel.value = m.models[0].id;
      hint.textContent = `金鑰可用（${m.ms} 毫秒），有 ${(m.models || []).length} 個模型可選；點「預設模型」欄就會列出來。`;
    } catch (ex) { errNote(err, ex.message); }
  };
  formButton(ov, "儲存", "primary", async () => {
    try { await save(); } catch (ex) { errNote(err, ex.message); return; }
    ov.remove();
    toast("已儲存", "ok");
    S.providers = null;
    viewSettings();
  });
}

/* ---------------------------------------------------------------- 手機版漢堡選單 */
$("#menubtn")?.addEventListener("click", () => { $("#mnav").hidden = false; });
$("#mnav-close")?.addEventListener("click", closeMobileNav);
$("#mnav")?.addEventListener("click", (e) => { if (e.target.id === "mnav") closeMobileNav(); });
