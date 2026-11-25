/* ComfyUI串接：畫圖、影片頁的另一個來源。
   管理員：1 連上ComfyUI → 2 指定ComfyUI資料夾（可略過）→ 3 匯入工作流 → 4 產生；使用者只看到產生。
   進度每 1.5 秒只換掉那幾個字與長條，不重畫整塊。 */

const COMFY_KIND = { image: "圖片", video: "影片" };

async function comfyPane(holder, kind) {
  loading(holder);
  let st;
  try { st = await api("/api/comfy"); } catch (ex) { errNote(holder, ex.message); return; }
  if (!holder.isConnected) return;
  holder.textContent = "";
  const admin = isAdmin();
  const top = el("div", "comfy-top");
  top.appendChild(comfyStatusLine(st));
  const tabs = el("div", "tabbar");
  const body = el("div");
  holder.append(top, ...(admin ? [tabs] : []), body);
  const reload = (tab) => { if (tab) S.comfyTab = tab; comfyPane(holder, kind); };
  const steps = comfySteps(st, kind, (t) => paint(t));
  const paint = (tab) => {
    S.comfyTab = tab;
    tabs.textContent = "";
    // 分頁照設定順序編號：沒做完的那一步點一顆小圓點
    [["conn", "1 連線與資料夾", !st.online], ["wf", `2 工作流（${st.workflows.length}）`, !st.workflows.length], ["gen", "3 產生", false]].forEach(([k, label, dot]) => {
      const b = el("button", null, label);
      if (dot) b.appendChild(el("span", "tab-dot"));
      if (k === tab) b.setAttribute("aria-current", "true");
      b.onclick = () => paint(k);
      tabs.appendChild(b);
    });
    body.textContent = "";
    if (admin && (tab !== "gen" || steps.some((s) => !s.done && !s.optional))) body.appendChild(renderSteps(steps));
    if (tab === "conn" && admin) comfyConn(body, st, reload);
    else if (tab === "wf" && admin) comfyWorkflows(body, st, kind, reload);
    else body.appendChild(comfyGen(st, kind, () => paint(admin ? (st.online ? "wf" : "conn") : "gen")));
    reveal(body);
  };
  const want = admin && ["conn", "wf", "gen"].includes(S.comfyTab) ? S.comfyTab : "gen";
  paint(admin && !st.configured ? "conn" : want);
}

function comfyStatusLine(st) {
  const d = el("div", "comfy-status small");
  if (!st.configured) { d.appendChild(el("span", "chip", "還沒串接ComfyUI")); return d; }
  d.appendChild(el("span", "chip " + (st.online ? "ok" : "bad"), st.online ? "已連線" : "連不上"));
  if (st.version) d.appendChild(el("span", null, `ComfyUI ${st.version}`));
  if (st.url) d.appendChild(el("span", "mono", st.url));
  const dev = (st.devices || [])[0];
  if (dev?.name) d.appendChild(el("span", "muted", dev.name + (dev.vram_gib ? `・${fmt(dev.vram_gib)} GiB` : "")));
  if (!st.online && st.error) d.appendChild(el("span", "bad-t", st.error));
  return d;
}

function comfySteps(st, kind, paint) {
  const steps = [
    { t: "連上ComfyUI", why: "填ComfyUI的網址（或讓系統找本機的），存起來會先驗證對方真的是ComfyUI", done: !!st.online,
      go: ["去設定", () => paint("conn")] },
    { t: "指定ComfyUI資料夾", why: "選了才看得到它有哪些模型，缺模型時也會講清楚要放進哪個資料夾", done: !!st.folder, optional: true,
      go: ["去指定", () => paint("conn")] },
    { t: "匯入工作流", why: "在ComfyUI存好的工作流（.json）匯進來，系統會檢查缺哪些節點與模型", done: st.workflows.length > 0,
      go: ["去匯入", () => paint("wf")] },
    { t: `產生${COMFY_KIND[kind]}`, why: "挑一個工作流、寫提示詞、按產生；進度與結果在右邊",
      done: !!st.online && st.workflows.some((w) => w.kind === kind && !w.warnings.length), go: ["去產生", () => paint("gen")] },
  ];
  const firstOpen = steps.findIndex((s) => !s.done && !s.optional);
  steps.forEach((s, i) => {
    if (s.done) s.state = "done";
    else if (s.optional) s.state = steps[0].done ? "skip" : "blocked";
    else s.state = i === firstOpen ? "now" : "blocked";
  });
  return steps;
}

/* ---------------------------------------------------------------- 1 連線與資料夾 */

function comfyConn(body, st, reload) {
  const c = card("連線", "ComfyUI可以在這台，也可以在別台；在別台要用 --listen 啟動");
  c.classList.add("rv");
  const fUrl = textInput(st.url || "", "例：http://127.0.0.1:8188");
  const fKey = el("input");
  fKey.type = "password";
  fKey.autocomplete = "off";
  fKey.placeholder = st.key_set ? "已設定；空白＝保留原金鑰" : "選填：ComfyUI前面有反向代理或登入外掛才需要";
  const clearKey = el("label", "small");
  const ck = el("input");
  ck.type = "checkbox";
  clearKey.append(ck, document.createTextNode(" 清掉已存的金鑰"));
  clearKey.hidden = !st.key_set;
  const fCa = el("textarea", "short mono");
  fCa.placeholder = "-----BEGIN CERTIFICATE-----\n（選填）對方是https且用自建CA或自簽憑證時，貼上對方的CA憑證";
  const caBox = fieldBox("對方的CA憑證", fCa, st.ca_set ? "已匯入一張；貼新的會換掉，空白＝保留" : "不提供關閉憑證驗證的選項：匯入對方的CA才是正確做法");
  const find = el("button", "btn sm", "找本機的ComfyUI");
  const found = el("div", "comfy-found");
  find.onclick = async () => {
    find.disabled = true;
    found.textContent = "找找看 8188、8000、8189 這幾個埠，以及納管的每台機器…";
    let r;
    try { r = await api("/api/comfy/detect"); } catch (ex) { found.textContent = ex.message; find.disabled = false; return; }
    find.disabled = false;
    found.textContent = "";
    if (!r.found.length) { found.appendChild(el("p", "small muted", "沒找到正在執行的ComfyUI。請確認它有啟動；在別台的話要加 --listen，然後把網址填在上面。")); return; }
    r.found.forEach((f) => {
      const b = el("button", "btn sm", `用這個：${f.url}${f.version ? `（ComfyUI ${f.version}）` : ""}`);
      b.onclick = () => { fUrl.value = f.url; fUrl.focus(); };
      found.appendChild(b);
    });
  };
  const err = el("div");
  const result = el("div");
  const save = el("button", "btn primary", "儲存並測試");
  const test = el("button", "btn", "重新測試");
  test.hidden = !st.configured;
  const disconnect = el("button", "btn danger", "中斷串接");
  disconnect.hidden = !st.configured;
  const showResult = (r) => {
    result.textContent = "";
    result.appendChild(el("p", "note " + (r.online ? "ok" : "bad"), r.online
      ? `連上了：ComfyUI ${r.version || "（沒有回報版本）"}${(r.devices || [])[0]?.name ? `，${r.devices[0].name}` : ""}`
      : `沒連上：${r.error}`));
  };
  save.onclick = async () => {
    errNote(err, "");
    save.disabled = true;
    let r;
    try {
      r = await api("/api/comfy/settings", { method: "POST", body: { url: fUrl.value.trim(), key: fKey.value.trim(), clear_key: ck.checked,
        ...(fCa.value.trim() ? { ca_pem: fCa.value.trim() } : {}) } });
    } catch (ex) { errNote(err, ex.message); save.disabled = false; return; }
    save.disabled = false;
    fKey.value = "";
    if (r.online) { toast(`已連上 ComfyUI ${r.version || ""}`, "ok"); reload("conn"); } else showResult(r);
  };
  test.onclick = async () => {
    test.disabled = true;
    try { showResult(await api("/api/comfy/test", { method: "POST", body: {} })); } catch (ex) { errNote(err, ex.message); }
    test.disabled = false;
  };
  disconnect.onclick = () => modal({ title: "中斷ComfyUI串接", danger: true, confirmText: "中斷",
    bodyNode: el("p", null, "清掉網址、金鑰與CA憑證；匯入的工作流與已產出的圖片、影片都留著，之後重新連上就能再用。"),
    onConfirm: async () => {
      try { await api("/api/comfy/settings", { method: "POST", body: { url: "" } }); } catch (ex) { toast(ex.message, "bad"); return; }
      reload("conn");
    } });
  const acts = el("div", "slot-actions");
  acts.append(find, el("span", "spacer"), disconnect, test, save);
  c.append(fieldBox("網址", fUrl, "ComfyUI預設是 8188 埠；ComfyUI桌面版是 8000。網際網路上的ComfyUI一律用https"),
    found, fieldBox("金鑰", fKey, "只放在 Authorization 標頭送出，不會出現在網址；存的時候加密"), clearKey, caBox, err, result, acts);
  body.appendChild(c);
  body.appendChild(comfyFolder(st, reload));
}

function comfyFolder(st, reload) {
  const c = card("ComfyUI資料夾", "選填：選了才列得出它有哪些模型，缺模型時講得出要放哪");
  c.classList.add("rv");
  const nodes = (S.fleet?.nodes || []).filter((n) => n.online);
  const f = st.folder;
  const fNode = selectInput(nodes.map((n) => [n.id, n.name]), f?.node);
  const path = el("div", "mono small", f ? f.path : "還沒指定");
  const pick = el("button", "btn sm", f ? "換一個資料夾" : "選資料夾");
  const rescan = el("button", "btn sm", "重新掃描");
  const clear = el("button", "btn sm", "不指定");
  rescan.hidden = clear.hidden = !f;
  const list = el("div");
  const err = el("div");
  const show = (r) => {
    list.textContent = "";
    const total = Object.values(r.folders).reduce((a, x) => a + x.length, 0);
    list.appendChild(el("p", "small muted", `${r.models}：共 ${total} 個模型檔${r.truncated ? "（太多了，只列前 2000 個）" : ""}。這些檔案歸ComfyUI管，xConsole只讀清單，不會刪除或搬動；要刪請在ComfyUI或檔案總管裡刪。`));
    const box = el("div", "scrollbox comfy-models");
    Object.entries(r.folders).forEach(([sub, rows]) => {
      box.appendChild(el("h4", "pick-h", `${sub}（${rows.length}）`));
      if (!rows.length) box.appendChild(el("p", "small muted", "沒有模型檔"));
      rows.forEach((m) => {
        const row = el("div", "comfy-model small");
        row.append(el("span", "mono", m.name), el("span", "muted", `${fmt(m.gib, 2)} GiB`));
        box.appendChild(row);
      });
    });
    list.appendChild(box);
  };
  const save = async (p) => {
    errNote(err, "");
    let r;
    try { r = await api("/api/comfy/folder", { method: "POST", body: { node: fNode.value, path: p } }); } catch (ex) { errNote(err, ex.message); return; }
    toast(p ? "已指定ComfyUI資料夾" : "已改成不指定", "ok");
    if (p) S.comfyModels = r;
    reload("conn");
  };
  pick.onclick = () => {
    const n = nodes.find((x) => x.id === fNode.value);
    if (n) openFolderBrowser(n, save, { kind: "comfy" });
  };
  clear.onclick = () => save("");
  rescan.onclick = async () => {
    rescan.disabled = true;
    try { S.comfyModels = await api("/api/comfy/models"); show(S.comfyModels); } catch (ex) { errNote(err, ex.message); }
    rescan.disabled = false;
  };
  if (!nodes.length) c.appendChild(el("p", "note warn", "沒有連得上的機器：ComfyUI資料夾要在納管的機器上（代理會幫忙列出模型）。"));
  const row = el("div", "slot-actions");
  row.append(path, el("span", "spacer"), clear, rescan, pick);
  c.append(fieldBox("在哪台機器", fNode, "ComfyUI跟xConsole在同一台就選這台"), row, err, list);
  if (f && S.comfyModels) show(S.comfyModels);
  else if (f) api("/api/comfy/models").then((r) => { S.comfyModels = r; if (list.isConnected) show(r); }).catch((ex) => errNote(err, ex.message));
  return c;
}

/* ---------------------------------------------------------------- 2 工作流 */

function comfyWorkflows(body, st, kind, reload) {
  const imp = card("匯入工作流", "ComfyUI存的 .json 或「匯出（API）」的 .json 都可以");
  imp.classList.add("rv");
  if (!st.online) imp.appendChild(el("p", "note warn", "要先連上ComfyUI才能匯入：系統要讀ComfyUI的節點定義，才看得懂工作流、檢查缺什麼。"));
  const file = el("input");
  file.type = "file";
  file.accept = ".json,application/json";
  file.hidden = true;
  const pick = el("button", "btn sm", "選 .json 檔");
  const picked = el("span", "small muted", "還沒選檔案");
  const fName = textInput("", "例：Z-Image 文生圖");
  fName.maxLength = 60;
  const fKind = selectInput([["auto", "自動判斷（看輸出節點）"], ["image", "圖片"], ["video", "影片"]], "auto");
  const err = el("div");
  const go = el("button", "btn primary", "匯入");
  go.disabled = !st.online;
  let text = null;
  pick.onclick = () => file.click();
  file.onchange = async () => {
    const f = file.files[0];
    file.value = "";
    if (!f) return;
    if (f.size > 5 * 1024 * 1024) { toast("工作流檔最大 5 MB", "bad"); return; }
    text = await f.text();
    picked.textContent = `${f.name}（${fmt(f.size / 1024, 0)} KB）`;
    if (!fName.value.trim()) fName.value = f.name.replace(/\.json$/i, "").slice(0, 60);
  };
  go.onclick = async () => {
    errNote(err, "");
    if (!text) { errNote(err, "先選一個 .json 檔"); return; }
    go.disabled = true;
    let r;
    try { r = await api("/api/comfy/workflows/import", { method: "POST", body: { name: fName.value.trim(), kind: fKind.value, workflow: text } }); } catch (ex) {
      go.disabled = false;
      const p = el("div", "note bad");
      p.appendChild(el("strong", null, "沒有匯入"));
      (ex.data?.problems || [ex.message]).forEach((t) => p.appendChild(el("div", null, t)));
      (ex.data?.warnings || []).forEach((t) => p.appendChild(el("div", "small", t)));
      err.appendChild(p);
      return;
    }
    go.disabled = false;
    toast(`已匯入「${r.workflow.name}」（${COMFY_KIND[r.workflow.kind]}）`, "ok");
    if (r.warnings.length) S.comfyImportWarn = r.warnings;
    reload("wf");
  };
  const acts = el("div", "slot-actions");
  acts.append(pick, picked, el("span", "spacer"), go);
  const fr = el("div", "form-row");
  fr.append(fieldBox("名稱", fName), fieldBox("分類", fKind, "影片＝輸出節點是儲存影片這類"));
  imp.append(file, fr, acts, err);
  if (S.comfyImportWarn) {
    const w = el("div", "note warn");
    w.appendChild(el("strong", null, "匯入了，但還不能產生"));
    S.comfyImportWarn.forEach((t) => w.appendChild(el("div", null, t)));
    imp.appendChild(w);
    S.comfyImportWarn = null;
  }
  imp.appendChild(el("p", "small muted", "缺節點時：在ComfyUI打開這個工作流，用ComfyUI Manager的「安裝缺失節點」裝好、重啟ComfyUI，再回來匯入。"));
  body.appendChild(imp);

  const lc = card("已匯入的工作流", `${st.workflows.length} 個`);
  lc.classList.add("rv");
  if (!st.workflows.length) lc.appendChild(el("p", "small muted", "還沒有。"));
  else {
    const tb = el("table", "api-table");
    tb.innerHTML = "<thead><tr><th>名稱</th><th>分類</th><th>可調整的欄位</th><th>狀態</th><th></th></tr></thead>";
    const tbody = el("tbody");
    st.workflows.forEach((w) => {
      const tr = el("tr");
      tr.appendChild(el("td", null, w.name));
      const k = selectInput([["image", "圖片"], ["video", "影片"]], w.kind);
      k.onchange = async () => {
        try { await api(`/api/comfy/workflows/${w.id}/update`, { method: "POST", body: { name: w.name, kind: k.value } }); toast("已改分類", "ok"); w.kind = k.value; } catch (ex) { toast(ex.message, "bad"); k.value = w.kind; }
      };
      const tk = el("td");
      tk.appendChild(k);
      tr.appendChild(tk);
      tr.appendChild(el("td", "small", w.fields.map((f) => f.label).join("、") || "沒有（照原樣跑）"));
      tr.appendChild(el("td", "small " + (w.warnings.length ? "warn-t" : "ok-t"), w.warnings.length ? `${w.warnings.length} 個問題` : "可以用"));
      if (w.warnings.length) tr.lastChild.title = w.warnings.join("\n");
      const td = el("td", "row-acts");
      const ren = el("button", "btn sm", "改名");
      ren.onclick = () => {
        const i = textInput(w.name);
        i.maxLength = 60;
        const ov = modal({ title: "工作流改名", bodyNode: fieldBox("名稱", i), hasInput: true });
        const e2 = el("div");
        ov.querySelector(".modal").insertBefore(e2, ov.querySelector(".actions"));
        formButton(ov, "儲存", "primary", async () => {
          try { await api(`/api/comfy/workflows/${w.id}/update`, { method: "POST", body: { name: i.value.trim(), kind: w.kind } }); } catch (ex) { errNote(e2, ex.message); return; }
          ov.remove();
          reload("wf");
        });
        imeGuard(i, () => ov.querySelector(".actions .primary").click());
        i.focus();
      };
      const del = el("button", "btn sm danger", "刪除");
      del.onclick = () => modal({ title: `刪除「${w.name}」`, danger: true, confirmText: "刪除",
        bodyNode: el("p", null, "只刪xConsole裡的這份工作流；ComfyUI那邊的檔案與已經產出的圖片、影片都不動。"),
        onConfirm: async () => {
          try { await api(`/api/comfy/workflows/${w.id}/delete`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); return; }
          reload("wf");
        } });
      td.append(ren, del);
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
    tb.appendChild(tbody);
    const sb = el("div", "scrollbox comfy-wf-list");
    sb.appendChild(tb);
    lc.appendChild(sb);
  }
  body.appendChild(lc);
}

/* ---------------------------------------------------------------- 3 產生 */

function comfyGen(st, kind, toSetup) {
  const wrap = el("div", "grid pair sd-gen");
  const wfs = st.workflows.filter((w) => w.kind === kind);
  const left = card("產生", "用ComfyUI的工作流");
  const right = card("結果");
  left.classList.add("rv");
  right.classList.add("rv");
  wrap.append(left, right);
  const out = el("div", "sd-out");
  const recent = el("div", "scrollbox sd-recent");
  right.append(out, el("h4", "pick-h", "最近產出"), recent);
  const blocked = !st.configured ? "還沒串接ComfyUI。" : !wfs.length ? `還沒有${COMFY_KIND[kind]}類的工作流。` : !st.online ? `ComfyUI連不上：${st.error}` : "";
  if (blocked) {
    const w = el("div", "note warn");
    w.appendChild(document.createTextNode(blocked));
    if (isAdmin()) {
      const b = el("button", "btn sm primary", !st.online ? "去設定連線" : "去匯入工作流");
      b.onclick = toSetup;
      w.appendChild(b);
    } else w.appendChild(document.createTextNode("請找管理員設定。"));
    left.appendChild(w);
  }
  const usable = wfs.find((w) => !w.warnings.length);
  const fWf = selectInput(wfs.map((w) => [w.id, w.name + (w.warnings.length ? "（有問題）" : "")]),
    wfs.some((w) => w.id === S.comfyWf) ? S.comfyWf : usable?.id);
  const fields = el("div", "comfy-fields");
  const err = el("div");
  const go = el("button", "btn primary", kind === "video" ? "產生影片" : "產生圖片");
  const row = el("div", "slot-actions");
  row.append(el("span", "spacer"), go);
  if (wfs.length && st.configured) left.append(fieldBox("工作流", fWf), fields, err, row);
  let form = null;
  const wf = () => wfs.find((w) => w.id === fWf.value);
  const build = () => {
    fields.textContent = "";
    const w = wf();
    if (!w) return;
    if (w.warnings.length) {
      const p = el("div", "note warn");
      w.warnings.forEach((t) => p.appendChild(el("div", null, t)));
      fields.appendChild(p);
    }
    form = comfyForm(w);
    fields.appendChild(form.node);
  };
  fWf.onchange = () => { S.comfyWf = fWf.value; build(); };
  build();
  go.onclick = async () => {
    errNote(err, "");
    const w = wf();
    if (!w || !form) return;
    go.disabled = true;
    let r;
    try { r = await api("/api/comfy/generate", { method: "POST", body: { workflow: w.id, values: form.values(), images: form.images() } }); } catch (ex) {
      errNote(err, (ex.data?.problems || [ex.message]).join("；"));
      go.disabled = false;
      return;
    }
    comfyWatch(r.id, out, recent, go, kind);
  };

  const ctx = { restore: (j) => {
    if (![...fWf.options].some((o) => o.value === j.wf)) return false;
    fWf.value = j.wf;
    S.comfyWf = j.wf;
    build();
    form.restore(j.values || {});
    return true;
  } };
  const loadRecent = async () => {
    let jobs = [];
    try { jobs = (await api("/api/comfy/jobs")).jobs.filter((j) => j.kind === kind); } catch (ex) { errNote(recent, ex.message); return []; }
    recent.textContent = "";
    if (!jobs.length) recent.appendChild(el("p", "small muted", "還沒有產出。"));
    jobs.forEach((j) => {
      const b = el("button", "sd-job");
      b.dataset.id = j.id;
      b.appendChild(el("span", "small mono", new Date(j.created * 1000).toLocaleString(uiLocale(), { hour12: false }).slice(5, 16)));
      b.appendChild(el("span", "sd-job-p", j.summary || j.wf_name));
      b.appendChild(el("span", "small " + (j.state === "failed" ? "bad-t" : "muted"),
        { running: "產生中", done: `${fmt(j.secs, 0)} 秒`, failed: "失敗", cancelled: "已取消", interrupted: "中斷" }[j.state] || j.state));
      b.onclick = () => (j.state === "running" ? comfyWatch(j.id, out, recent, go, kind) : comfyShow(j, out, recent, ctx));
      recent.appendChild(b);
    });
    return jobs;
  };
  S.comfyReload = loadRecent;
  S.comfyCtx = ctx;
  loadRecent().then((jobs) => {
    const run = jobs.find((j) => j.state === "running");
    if (run) comfyWatch(run.id, out, recent, go, kind);
    else if (jobs[0]) comfyShow(jobs[0], out, recent, ctx);
    else out.appendChild(el("p", "small muted", "左邊填好按「產生」，進度與結果會出現在這裡。"));
  });
  return wrap;
}

// 工作流可以調整的欄位：提示詞、不要出現的內容、輸入圖片、尺寸與步數這類數字、種子
function comfyForm(w) {
  const node = el("div");
  const inputs = {};
  const imgs = {};
  const nums = el("div", "form-row");
  w.fields.forEach((f) => {
    if (f.kind === "text") {
      const t = el("textarea", f.role === "negative" ? "short" : "");
      t.value = f.value || "";
      t.maxLength = 20000;
      t.placeholder = f.role === "negative" ? "不要出現的內容（選填）" : "想要的畫面";
      inputs[f.id] = t;
      node.appendChild(fieldBox(f.label, t));
    } else if (f.kind === "image") {
      const state = { b64: null, mime: null, url: null };
      const fi = el("input");
      fi.type = "file";
      fi.accept = "image/png,image/jpeg,image/webp";
      fi.hidden = true;
      const thumb = el("img", "sd-init-thumb");
      thumb.alt = f.label;
      thumb.hidden = true;
      const pick = el("button", "btn sm", "選一張圖");
      const clear = el("button", "btn sm", "拿掉");
      clear.hidden = true;
      const set = (b64, mime) => {
        if (state.url) URL.revokeObjectURL(state.url);
        state.b64 = b64; state.mime = mime; state.url = null;
        if (b64) state.url = URL.createObjectURL(new Blob([Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0))], { type: mime }));
        thumb.hidden = clear.hidden = !b64;
        if (b64) thumb.src = state.url; else thumb.removeAttribute("src");
      };
      pick.onclick = () => fi.click();
      clear.onclick = () => set(null, null);
      fi.onchange = async () => {
        const file = fi.files[0];
        fi.value = "";
        if (!file) return;
        if (file.size > 4 * 1024 * 1024) { toast("圖片最大 4 MB", "bad"); return; }
        if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) { toast("只收 PNG、JPEG、WebP", "bad"); return; }
        set(await readB64(file), file.type);
      };
      imgs[f.id] = state;
      const r = el("div", "sd-init");
      r.append(fi, thumb, pick, clear);
      node.appendChild(fieldBox(f.label + (f.required ? "" : "（選填）"), r, f.required ? "這個工作流一定要先選一張圖" : "不選就用工作流裡原本那張"));
    } else {
      const i = numInput(f.kind === "seed" ? -1 : f.value);
      if (f.kind === "seed") { i.min = "-1"; i.step = "1"; } else {
        i.step = f.int ? String(f.step || 1) : "any";
        if (f.min !== undefined) i.min = String(f.min); else i.removeAttribute("min");
        if (f.max !== undefined) i.max = String(f.max);
      }
      inputs[f.id] = i;
      nums.appendChild(fieldBox(f.label, i, f.kind === "seed" ? "-1＝隨機；同種子同設定會產出同一張" : f.min !== undefined && f.max !== undefined ? `${f.min}～${f.max}` : ""));
    }
  });
  if (nums.childElementCount) node.appendChild(nums);
  if (!w.fields.length) node.appendChild(el("p", "small muted", "這個工作流沒有可以在這裡調整的欄位，會照ComfyUI裡存的設定跑。"));
  return {
    node,
    values: () => Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.type === "number" ? (i.value === "" ? "" : Number(i.value)) : i.value])),
    images: () => Object.fromEntries(Object.entries(imgs).filter(([, s]) => s.b64).map(([k, s]) => [k, `data:${s.mime};base64,${s.b64}`])),
    restore: (vals) => Object.entries(vals).forEach(([k, v]) => { if (inputs[k]) inputs[k].value = v; }),
  };
}

function comfyWatch(jid, out, recent, go, kind) {
  clearTimeout(S.genTimer);
  out.textContent = "";
  const head = el("div", "small");
  const bar = el("div", "bar");
  const seg = el("div", "seg");
  bar.appendChild(seg);
  const meta = el("div", "small muted mono");
  const cancel = el("button", "btn sm", "取消");
  cancel.onclick = async () => {
    cancel.disabled = true;
    try { await api(`/api/comfy/jobs/${jid}/cancel`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); cancel.disabled = false; }
  };
  const top = el("div", "slot-actions");
  top.append(head, el("span", "spacer"), cancel);
  out.append(top, bar, meta);
  go.disabled = true;
  recent.querySelectorAll(".sd-job").forEach((b) => b.toggleAttribute("aria-current", b.dataset.id === jid));
  const tick = async () => {
    if (!out.isConnected) return;
    let j;
    try { j = await api(`/api/comfy/jobs/${jid}`); } catch (ex) { meta.textContent = "讀不到進度：" + ex.message; S.genTimer = setTimeout(tick, 3000); return; }
    if (!out.isConnected) return;
    const nodePct = j.total_nodes ? (j.done_nodes || 0) / j.total_nodes : 0;
    const stepPct = j.steps ? j.step / j.steps : 0;
    seg.style.width = Math.floor(Math.max(nodePct, stepPct) * 100) + "%";
    head.textContent = `${j.wf_name}：${j.phase || ""}${j.steps ? `　${j.step}/${j.steps} 步` : ""}`;
    meta.textContent = (j.queue ? `前面還有 ${j.queue} 件排隊・` : "") + `已經 ${Math.round(Date.now() / 1000 - j.created)} 秒`
      + (j.live === false ? "・這台ComfyUI沒有即時進度，跑完會自動顯示" : "");
    if (j.state === "running") { S.genTimer = setTimeout(tick, 1500); return; }
    go.disabled = false;
    const jobs = await S.comfyReload?.();
    comfyShow((jobs || []).find((x) => x.id === jid) || j, out, recent, S.comfyCtx);
  };
  tick();
}

function comfyShow(j, out, recent, ctx) {
  clearTimeout(S.genTimer);
  out.textContent = "";
  recent.querySelectorAll(".sd-job").forEach((b) => b.toggleAttribute("aria-current", b.dataset.id === j.id));
  const info = el("div", "small muted", `${j.wf_name}・ComfyUI` + (j.secs ? `・${fmt(j.secs, 0)} 秒` : "") + (j.by ? `・${j.by}` : ""));
  const acts = el("div", "slot-actions");
  const del = el("button", "btn sm danger", "刪除");
  del.onclick = async () => {
    try { await api(`/api/comfy/jobs/${j.id}/delete`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); return; }
    out.textContent = "";
    S.comfyReload?.();
  };
  if (j.state !== "done") {
    out.appendChild(el("p", "note " + (j.state === "failed" ? "bad" : "warn"), j.error || { cancelled: "已取消", interrupted: "中斷了" }[j.state] || j.state));
  } else {
    const box = el("div", "sd-media");
    const url = (i) => `/api/comfy/jobs/${j.id}/output/${i}`;
    const media = (o, i) => {
      const m = el(o.mime.startsWith("video") ? "video" : o.mime.startsWith("audio") ? "audio" : "img");
      m.src = url(i);
      if (m.tagName === "IMG") m.alt = j.summary || j.wf_name; else { m.controls = true; if (m.tagName === "VIDEO") m.loop = true; }
      return m;
    };
    let cur = j.outputs[0];
    box.appendChild(media(cur, 0));
    out.appendChild(box);
    if (j.outputs.length > 1) {
      const strip = el("div", "sd-strip");
      j.outputs.forEach((o, i) => {
        const b = el("button", "sd-thumb" + (o.mime.startsWith("image") ? "" : " comfy-thumb-file"));
        b.setAttribute("aria-label", `第 ${i + 1} 個`);
        if (o.mime.startsWith("image")) { const im = el("img"); im.src = url(i); im.alt = ""; b.appendChild(im); } else b.textContent = String(i + 1);
        b.toggleAttribute("aria-current", i === 0);
        b.onclick = () => {
          box.textContent = "";
          box.appendChild(media(o, i));
          cur = o;
          strip.querySelectorAll(".sd-thumb").forEach((x) => x.toggleAttribute("aria-current", x === b));
        };
        strip.appendChild(b);
      });
      out.appendChild(strip);
    }
    const dl = el("button", "btn sm", "下載");
    dl.onclick = () => { const a = el("a"); a.href = url(j.outputs.indexOf(cur)); a.download = cur.name; document.body.appendChild(a); a.click(); a.remove(); };
    acts.appendChild(dl);
  }
  if (ctx && j.values) {
    const again = el("button", "btn sm", "用同樣的設定");
    again.onclick = () => { if (ctx.restore(j)) toast("已帶入這件的設定（輸入圖片要重新選）", "ok"); else toast("這個工作流已經被刪掉了", "warn"); };
    acts.appendChild(again);
  }
  acts.append(el("span", "spacer"), del);
  out.append(info, acts);
  if (j.summary) out.appendChild(el("p", "sd-prompt small", j.summary));
}
