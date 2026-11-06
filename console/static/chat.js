/* ================================================================ 對話
   本機執行中的模型，或管理者在設定裡加的第三方 API。
   - 對話紀錄存在主控台（加密落地，/api/chats*），換瀏覽器、重新整理都在；左邊可以搜尋、改名、刪除。
   - 每則回答：複製、重新生成、調整回答（更簡潔…改用英文）、編輯、刪除；重新生成與調整回答會留下各版本可切換。
   - 對話設定：系統提示詞與取樣參數（第三方 API 只送它支援的標準參數，由主控台把關）。
   - 說「畫一張…」「生成一段…的影片」直接產出；附圖說「把…改成…」改圖。輸入列也有「畫圖」「影片」鈕。
   - 快超過上下文時，把較早的對話摘要起來再送（畫面上原文照留），標示「已壓縮前 N 則」。
   - 「工具」打開時，本機模型可以呼叫主控台的查詢工具（機器狀態、推薦、已下載模型；只查不動）。
   共用的 renderRich、codeBlock、openPreview 在 pages.js。 */

const CHAT2 = { list: null, cur: null, q: "", blob: {}, media: {}, ctx: {}, migrated: false, sideOpen: false };
const TOOL_LABEL = { list_machines: "列出機器", machine_detail: "看這台的狀態", recommend_models: "查推薦模型", installed_models: "查已下載的模型" };
const CHAT_DEF_KEY = "xconsole-chat-defaults";
const REWRITE = [
  ["shorter", "更簡潔", "把你上一個回答改寫得更簡潔，保留所有重點，只輸出改寫後的內容。"],
  ["longer", "更詳細", "把你上一個回答改寫得更詳細，補充說明與例子，只輸出改寫後的內容。"],
  ["formal", "更正式", "把你上一個回答改寫成正式的書面語氣，只輸出改寫後的內容。"],
  ["casual", "更口語", "把你上一個回答改寫得更口語、更好懂，只輸出改寫後的內容。"],
  ["rephrase", "換個說法", "用不同的說法重寫你上一個回答，意思不變，只輸出改寫後的內容。"],
  ["english", "改用英文", "Rewrite your previous answer in English. Output only the rewritten answer."],
];
// 對話設定欄位：鍵、標籤、說明、範圍（跟主控台 chats.SETTINGS 同一組）
const CHAT_PARAMS = [
  ["temperature", "溫度", "越高越有創意、越低越穩定，常用 0.6～1.0", 0, 2, 0.05],
  ["top_p", "Top P", "只從累計機率前 P 的詞裡挑，常用 0.8～0.95", 0, 1, 0.01],
  ["top_k", "Top K", "只從機率最高的 K 個詞裡挑，常用 20～40", 0, 1000, 1],
  ["min_p", "Min P", "過濾機率太低的詞，常用 0～0.05", 0, 1, 0.01],
  ["repeat_penalty", "重複懲罰", "大於 1 減少重複用詞，常用 1.0～1.1", 0.5, 2, 0.01],
  ["presence_penalty", "新話題", "大於 0 鼓勵聊新的內容，常用 0～1.5", -2, 2, 0.1],
  ["max_tokens", "最大回覆長度", "單次回答最多幾個 token；空白＝不限", 1, 131072, 1],
  ["seed", "隨機種子", "固定種子，同樣的問題會得到一樣的回答；空白＝隨機", -1, 2147483647, 1],
];
const STD_PARAMS = ["temperature", "top_p", "presence_penalty", "max_tokens", "seed"];

const chatDefaults = () => { try { return JSON.parse(localStorage.getItem(CHAT_DEF_KEY) || "{}") || {}; } catch (_) { return {}; } };

function chatTargets2() {
  const out = [];
  (S.fleet?.nodes || []).filter((n) => n.online).forEach((n) => (n.state.slots || [])
    .filter((s) => isApiSlot(s) && s.busy && ((s.caps || []).includes("文字") || s.kind === "llama" || s.kind === "strata"))
    .forEach((s) => out.push({ key: `slot:${n.id}:${s.id}`, group: "本機模型", label: `${s.model || s.unit}（${n.name}）`, n, s,
      vision: (s.caps || []).includes("看圖"), local: true, url: `/api/node/${n.id}/chat/${s.id}`,
      ctx: s.config?.max_context || null })));
  (S.providers || []).filter((p) => p.key_set).forEach((p) => out.push({ key: `prov:${p.id}`, group: "第三方 API",
    label: `${p.name}・${p.default_model || ""}`, prov: p, local: false, url: `/api/providers/${p.id}/chat`, cn: p.cn_origin, ctx: 32768 }));
  return out;
}

function newChat(target) {
  return { id: null, title: "新對話", target, msgs: [], settings: { ...chatDefaults() }, summary: null, mode: "chat",
    draft: "", image: null, docs: [], busy: false };
}

/* ---- 存取主控台 */

async function loadChatList() {
  try { CHAT2.list = (await api("/api/chats" + (CHAT2.q ? "?q=" + encodeURIComponent(CHAT2.q) : ""))).items; } catch (_) { CHAT2.list = CHAT2.list || []; }
  return CHAT2.list;
}

// 以前存在瀏覽器分頁的對話，第一次進來時搬到主控台（搬完清掉分頁裡那份）
async function migrateOldChats() {
  if (CHAT2.migrated) return;
  CHAT2.migrated = true;
  let old = [];
  try { old = JSON.parse(sessionStorage.getItem("xconsole-chats") || "[]"); } catch (_) { old = []; }
  if (!Array.isArray(old) || !old.length) return;
  let n = 0;
  const left = [];
  for (const s of old) {
    const msgs = (s.msgs || []).filter((m) => m && (m.role === "user" || m.role === "assistant"));
    if (!msgs.length) continue;
    try { await api("/api/chats/save", { method: "POST", body: { title: s.title || "新對話", target: s.target || "", msgs: msgs.map(packMsg), settings: {} } }); n += 1; }
    catch (_) { left.push(s); }                  // 沒搬成功的留著，下次進來再試，不能直接丟掉
  }
  try { if (left.length) sessionStorage.setItem("xconsole-chats", JSON.stringify(left)); else sessionStorage.removeItem("xconsole-chats"); } catch (_) { /* 無痕 */ }
  if (n) toast(`已把這個分頁裡的 ${n} 則對話存到主控台`, "ok");
  if (left.length) toast(`還有 ${left.length} 則舊對話沒搬成功，下次打開對話頁會再試`, "bad");
}

function packMsg(m) {
  const o = { role: m.role, content: m.content || "" };
  ["reasoning", "stat", "error"].forEach((k) => { if (m[k]) o[k] = m[k]; });
  if (m.image) o.image = m.image;
  if (m.image_lost) o.image_lost = true;
  if (m.docs?.length) o.docs = m.docs.map(({ name, text, chars, truncated, lost }) => ({ name, ...(text ? { text } : {}), ...(chars != null ? { chars } : {}), ...(truncated ? { truncated } : {}), ...(lost ? { lost } : {}) }));
  if (m.alts?.length > 1) { o.alts = m.alts.map(({ content, reasoning, stat }) => ({ content: content || "", ...(reasoning ? { reasoning } : {}), ...(stat ? { stat } : {}) })); o.alt = m.alt ?? m.alts.length - 1; }
  if (m.gen) {
    const { node, job, kind, prompt, model, title, state, error, asked, count, edit } = m.gen;
    o.gen = { node, kind, count: count || 1, ...(job ? { job } : {}), ...(prompt ? { prompt } : {}), ...(model ? { model } : {}), ...(title ? { title } : {}),
      ...(state ? { state } : {}), ...(error ? { error } : {}), ...(asked ? { asked } : {}), ...(edit ? { edit } : {}) };
  }
  return o;
}

// 存檔排隊：同一則對話不會同時送兩次（後一次等前一次拿到 id）
function saveChat(cur) {
  const run = async () => {
    const kept = cur.msgs.filter((m) => !m.pending);
    if (!kept.length) return;
    const r = await api("/api/chats/save", { method: "POST", body: { id: cur.id, title: cur.title, target: cur.target,
      msgs: kept.map(packMsg), settings: cur.settings, summary: cur.summary } });
    const first = !cur.id;
    cur.id = r.id;
    Object.entries(r.images || {}).forEach(([i, ref]) => { const m = kept[Number(i)]; if (m) { CHAT2.blob[ref] = m.image; m.image = ref; } });
    if (first || CHAT2.list) { await loadChatList(); CHAT2.drawSide?.(); }
  };
  cur._save = (cur._save || Promise.resolve()).then(run).catch((ex) => toast("對話沒存進主控台：" + ex.message, "bad"));
  return cur._save;
}

async function openChat(id) {
  const r = await api("/api/chats/" + id);
  const cur = newChat(r.target);
  Object.assign(cur, { id: r.id, title: r.title, settings: r.settings || {}, summary: r.summary || null,
    msgs: (r.msgs || []).map((m) => ({ ...m })) });
  return cur;
}

const imgSrc = (ref) => (ref?.startsWith("blob:") ? `/api/chats/blob/${ref.slice(5)}` : ref);
async function imgDataUrl(ref) {
  if (!ref) return null;
  if (ref.startsWith("data:")) return ref;
  if (CHAT2.blob[ref]) return CHAT2.blob[ref];
  const res = await fetch(imgSrc(ref), { credentials: "same-origin" });
  if (!res.ok) throw new Error("讀不到附圖");
  const blob = await res.blob();
  const url = await new Promise((ok, bad) => { const fr = new FileReader(); fr.onload = () => ok(fr.result); fr.onerror = bad; fr.readAsDataURL(blob); });
  CHAT2.blob[ref] = url;
  return url;
}

/* ---- 送給模型的內容 */

const estTokens = (s) => Math.ceil(new TextEncoder().encode(s || "").length / 3);
const withDocs = (m) => (m.docs || []).map((d) => (d.text ? `【附件：${d.name}】\n${d.text}\n【附件結束】\n\n`
  : `（先前附了「${d.name}」，內容已經看不到了）\n`)).join("") + (m.content || "");
const genText = (g) => (g.state === "done" ? `（已產生${g.kind === "video" ? "一段影片" : ` ${g.count || 1} 張圖`}，提示詞：${g.prompt || g.asked || ""}）`
  : `（${g.kind === "video" ? "影片" : "圖片"}沒有產生成功）`);

async function buildHistory(cur, msgs, t) {
  const start = cur.summary?.upto || 0;
  const sent = msgs.slice(start).filter((m) => !m.pending && !m.error);
  const lastImg = t.vision ? sent.map((m) => !!m.image && !m.gen).lastIndexOf(true) : -1;
  const out = [];
  const sys = [cur.settings?.system, cur.summary?.text ? `以下是這段對話較早部分的摘要，請當作已知的前情：\n${cur.summary.text}` : null].filter(Boolean).join("\n\n");
  if (sys) out.push({ role: "system", content: sys });
  for (let i = 0; i < sent.length; i++) {
    const m = sent[i];
    const text = m.gen ? genText(m.gen) : withDocs(m);
    if (m.image && i === lastImg) {
      let url = null;
      try { url = await imgDataUrl(m.image); } catch (_) { url = null; }
      out.push({ role: m.role, content: url ? [{ type: "text", text }, { type: "image_url", image_url: { url } }] : `（先前附了一張圖，已經讀不到了）${text}` });
    } else out.push({ role: m.role, content: m.image && !m.gen ? `（先前附了一張圖）${text}` : m.image_lost ? `（先前附了一張圖，已經看不到了）${text}` : text });
  }
  return out;
}

function sampleBody(cur, t) {
  const st = cur.settings || {};
  const out = {};
  (t.local ? CHAT_PARAMS.map((p) => p[0]) : STD_PARAMS).forEach((k) => { if (st[k] !== undefined && st[k] !== null && st[k] !== "") out[k] = st[k]; });
  // 空白＝不限（交給模型服務自己的預設）；有填才送，而且不超過上下文的一半
  if (out.max_tokens !== undefined && t.ctx && out.max_tokens > t.ctx / 2) out.max_tokens = Math.max(256, Math.floor(t.ctx / 2));
  return out;
}

// 一次性（不串流）的請求：摘要、改寫畫圖提示詞用
async function askOnce(t, messages, maxTokens) {
  const body = { messages, stream: false, max_tokens: maxTokens || 1024, temperature: 0.2, ...(t.prov ? { model: t.prov.default_model } : {}) };
  const r = await api(t.url, { method: "POST", body });
  const msg = r.choices?.[0]?.message || {};
  return (msg.content || "").trim();
}

// 長對話壓縮：估計超過上下文 75% 時，把較早的（保留最後 4 則）摘要起來
async function maybeCompress(cur, t, msgs, note) {
  const ctx = t.ctx || 32768;
  const hist = await buildHistory(cur, msgs, t);
  const used = hist.reduce((a, m) => a + estTokens(typeof m.content === "string" ? m.content : m.content[0].text) + (Array.isArray(m.content) ? 800 : 0), 0);
  const reserve = Math.min(sampleBody(cur, t).max_tokens || 4096, Math.floor(ctx / 4));
  const from = cur.summary?.upto || 0;
  if (used + reserve <= ctx * 0.75 || msgs.length - from <= 6) return hist;
  let upto = msgs.length - 4;
  while (upto > from && msgs[upto]?.role !== "user") upto--;
  if (upto <= from) return hist;
  note("對話太長，正在把較早的部分整理成摘要…");
  const part = msgs.slice(from, upto).filter((m) => !m.pending && !m.error)
    .map((m) => `${m.role === "user" ? "使用者" : "助理"}：${(m.gen ? genText(m.gen) : withDocs(m)).slice(0, 6000)}`).join("\n\n");
  let text = "";
  try {
    text = await askOnce(t, [
      { role: "system", content: "你負責把對話整理成摘要，讓之後的對話可以接著進行。用繁體中文（台灣用語）條列，保留人名、數字、日期、做過的決定、使用者的偏好與還沒完成的事；不要加評論。800 字以內。" },
      { role: "user", content: (cur.summary?.text ? `先前的摘要：\n${cur.summary.text}\n\n接著的對話：\n` : "") + part },
    ], 1500);
  } catch (_) { text = ""; }
  if (!text) { note(""); return hist; }
  cur.summary = { text, upto };
  note("");
  return buildHistory(cur, msgs, t);
}

/* ---- 串流一則回答 */

async function streamInto(t, body, reply, paint, signal) {
  const res = await fetch(t.url, { method: "POST", credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.error || `伺服器回 ${res.status}`); }
  if (!(res.headers.get("Content-Type") || "").includes("event-stream")) {
    const j = await res.json();
    const m = j.choices?.[0]?.message || {};
    reply.content = m.content || ""; if (m.reasoning_content) reply.reasoning = m.reasoning_content;
    return { timings: j.timings, usage: j.usage };
  }
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
    // 只重畫這一則（最多每 120 毫秒一次）；使用者往上捲在看舊訊息時不把他拉回底部
    if (performance.now() - last > 120) { last = performance.now(); paint(); }
  }
  return { timings, usage };
}

/* ---- 在對話裡畫圖、做影片 */

const CN_NUM = { 一: 1, 兩: 2, 二: 2, 三: 3, 四: 4, 1: 1, 2: 2, 3: 3, 4: 4 };
function detectGen(text, hasImage, mode) {
  const s = text.trim();
  const count = Math.min(4, CN_NUM[(s.match(/([一兩二三四1-4])\s*[張幅]/) || [])[1]] || 1);
  const edit = hasImage && /(把|將|幫我把|請把).{0,80}(改成|換成|變成|改為|改得|換掉|去掉|拿掉|加上|移除)/.test(s);
  if (mode === "video") return { kind: "video", count: 1, edit: false };
  if (mode === "image") return { kind: "image", count, edit: hasImage };
  if (/(生成|產生|做|拍|製作)\s*(一|1)?\s*(段|支|部|個)?.{0,60}(影片|動畫|短片|視頻)/.test(s)) return { kind: "video", count: 1, edit: false };
  if (edit) return { kind: "image", count: 1, edit: true };
  if (/^(請|幫我|幫忙|麻煩|可以)?\s*(你)?\s*畫/.test(s) || /(生成|產生|做|來)\s*([一兩二三四1-4])?\s*[張幅]\s*.{0,60}(圖|照片|插畫|海報|插圖|桌布|頭像)/.test(s)
    || /(生成|產生).{0,30}(的圖|圖片|插圖|照片)$/.test(s)) return { kind: "image", count, edit: false };
  return null;
}

// 中文描述交給對話模型改寫成英文提示詞（多數畫圖模型英文效果最好）；沒有本機文字模型或改寫失敗就用原文
async function genPrompt(t, text, kind, edit) {
  if (!t?.local) return text;
  try {
    const out = await askOnce(t, [
      { role: "system", content: "You write prompts for " + (kind === "video" ? "text-to-video" : "text-to-image") + " models. Rewrite the user's request as ONE concise English prompt "
        + "made of comma-separated descriptive phrases (subject, setting, style, lighting, camera). "
        + (edit ? "The user is editing an existing picture: describe how the FINAL picture should look after the change. " : "")
        + "Output only the prompt, no quotes, no explanation." },
      { role: "user", content: text },
    ], 300);
    const p = out.replace(/^["'「]|["'」]$/g, "").split("\n").find((x) => x.trim()) || "";
    return p.length >= 3 && p.length <= 1500 ? p.trim() : text;
  } catch (_) { return text; }
}

function genNodeOf(t) {
  if (t?.local) return t.n;
  return curNode();
}

async function runGen(cur, t, reply, text, intent, initImage, paint) {
  const n = genNodeOf(t);
  const g = reply.gen = { node: n?.id || "-", kind: intent.kind, count: intent.count || 1, asked: text, edit: !!intent.edit, state: "prepare" };
  reply.pending = false;
  if (!n || !n.online) { Object.assign(g, { state: "failed", error: "沒有連得上的機器可以產生" }); return; }
  paint();
  let rec;
  try { rec = await getSdRec(n.id, true); } catch (ex) { Object.assign(g, { state: "failed", error: ex.message }); return; }
  if (!rec.sd_cli) { Object.assign(g, { state: "need_engine", error: `${n.name} 還沒安裝畫圖引擎（stable-diffusion.cpp）` }); return; }
  const ready = rec.models.filter((m) => m.ready && m.kind === intent.kind);
  if (!ready.length) { Object.assign(g, { state: "need_model", error: `${n.name} 還沒有下載好的${intent.kind === "video" ? "影片" : "畫圖"}模型` }); return; }
  const model = ready.find((m) => m.id === S.sdModel) || ready[0];
  g.model = model.id; g.title = model.title;
  g.state = "prompt"; paint();
  g.prompt = await genPrompt(t, text, intent.kind, intent.edit);
  if (cur._genStop) { Object.assign(g, { state: "failed", error: "已停止" }); return; }
  const d = model.defaults || {};
  const body = { model: model.id, prompt: g.prompt, negative: "", width: d.width || 1024, height: d.height || 1024,
    steps: d.steps ?? 20, cfg: d.cfg ?? 1, seed: -1, sampler: d.sampler || "euler",
    ...(intent.kind === "video" ? { frames: d.frames || 33, fps: d.fps || 24 } : { count: g.count }),
    ...(initImage ? { init_image: initImage, ...(intent.kind === "image" ? { strength: 0.65 } : {}) } : {}) };
  let r;
  try { r = await api(`/api/node/${n.id}/sd/generate`, { method: "POST", body }); } catch (ex) {
    Object.assign(g, { state: "failed", error: ex.data?.admit ? ex.data.admit.reasons.join("；") : ex.message }); return;
  }
  Object.assign(g, { job: r.id, state: "running", _watch: true });
  if (cur._genStop) { api(`/api/node/${n.id}/sd/jobs/${r.id}/cancel`, { method: "POST", body: {} }).catch(() => {}); }
  await saveChat(cur);
  await watchChatGen(g, paint);
}

async function watchChatGen(g, paint) {
  for (;;) {
    let j;
    try { j = await api(`/api/node/${g.node}/sd/jobs/${g.job}`); } catch (ex) {
      if (/404|沒有這/.test(ex.message)) { Object.assign(g, { state: "gone" }); paint(); return; }
      await new Promise((ok) => setTimeout(ok, 3000)); continue;
    }
    g._p = j.total ? `${j.phase}　${j.step}/${j.total} 步` : j.phase;
    g._pct = j.total ? Math.floor((j.step / j.total) * 100) : 0;
    if (j.state === "running") { paint(); await new Promise((ok) => setTimeout(ok, 2000)); continue; }
    g.state = j.state === "done" ? "done" : "failed";
    if (j.state !== "done") g.error = j.error || { cancelled: "已取消", interrupted: "代理重啟過，這件沒產完" }[j.state] || j.state;
    paint();
    return;
  }
}

async function genMedia(g) {
  const key = `${g.node}/${g.job}`;
  if (CHAT2.media[key]) return CHAT2.media[key];
  const out = [];
  for (let i = 0; i < (g.count || 1); i++) {
    try {
      const r = await api(`/api/node/${g.node}/sd/jobs/${g.job}/output/${i}`);
      const bin = Uint8Array.from(atob(r.b64), (ch) => ch.charCodeAt(0));
      out.push({ ...r, url: URL.createObjectURL(new Blob([bin], { type: r.mime })), b64: r.b64 });
    } catch (ex) { if (!i) throw ex; }
  }
  CHAT2.media[key] = out;
  return out;
}

/* ---- 頁面 */

async function viewChat() {
  const main = $("#main");
  const v = S.view;
  if (!S.providers) {
    try { S.providers = (await api("/api/providers")).items || []; } catch (_) { S.providers = []; }
  }
  await migrateOldChats();
  if (!CHAT2.list) await loadChatList();
  if (S.view !== v) return;
  main.textContent = "";
  const wrap = el("div", "wrap chat-wrap");
  main.appendChild(wrap);
  const targets = chatTargets2();
  const shell = el("section", "card chat-shell");
  wrap.appendChild(shell);
  if (!targets.length && !CHAT2.list.length) {
    shell.classList.add("chat-none");
    shell.appendChild(el("h2", null, "還沒有可以對話的模型"));
    shell.appendChild(el("p", "small muted", "到「執行」啟動一顆模型，或請管理者在「設定 › 第三方 API」加一個服務。"));
    const b = el("button", "btn primary", "去執行");
    b.onclick = () => go("run");
    shell.appendChild(b);
    return;
  }
  let cur = CHAT2.cur && (CHAT2.cur.id || CHAT2.cur.msgs.length || CHAT2.cur.draft) ? CHAT2.cur : null;
  if (!cur) cur = CHAT2.cur = newChat(targets[0]?.key || "");
  if (targets.length && !targets.some((t) => t.key === cur.target)) cur.target = targets[0].key;

  // ---- 左：對話紀錄（可收合；窄螢幕變成抽屜）
  const side = el("aside", "chat-side");
  side.classList.toggle("open", CHAT2.sideOpen);
  const newB = el("button", "btn primary", "＋ 新對話");
  const search = el("input", "chat-search");
  search.type = "search";
  search.placeholder = "搜尋對話（標題與內容）";
  search.value = CHAT2.q;
  const listBox = el("div", "chat-list");
  const sideX = el("button", "btn sm chat-side-x", "收起");
  sideX.onclick = () => { CHAT2.sideOpen = false; side.classList.remove("open"); };
  const sideTop = el("div", "chat-side-top");
  sideTop.append(newB, sideX);
  side.append(sideTop, search, listBox);
  const mainCol = el("div", "chat-main");
  shell.append(side, mainCol);

  // 換對話、刪對話是使用者自己按的，整頁換掉；搜尋與存檔後只重畫左邊清單（drawSide）
  const pickChat = async (it) => {
    if (cur.busy) { toast("回答還在產生，先按停止再換對話", "bad"); return; }
    try { CHAT2.cur = await openChat(it.id); } catch (ex) { toast(ex.message, "bad"); return; }
    CHAT2.sideOpen = false;
    viewChat();
  };
  const dropChat = (it) => modal({ title: "刪除這則對話", bodyNode: el("p", null, `「${it.title}」連同附圖會從主控台刪掉，不能復原。`), confirmText: "刪除", danger: true,
    onConfirm: async () => {
      try { await api(`/api/chats/${it.id}/delete`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); return; }
      if (cur.id === it.id) CHAT2.cur = null;
      await loadChatList();
      viewChat();
    } });
  const drawSide = () => {
    listBox.textContent = "";
    if (!CHAT2.list.length) listBox.appendChild(el("p", "small muted", CHAT2.q ? "找不到符合的對話。" : "還沒有存下來的對話。"));
    CHAT2.list.forEach((it) => {
      const row = el("div", "chat-item");
      if (cur.id === it.id) row.setAttribute("aria-current", "true");
      const b = el("button", "chat-item-main");
      b.appendChild(el("span", "chat-item-t", it.title || "新對話"));
      b.appendChild(el("span", "small muted", it.snippet || new Date(it.updated * 1000).toLocaleString("zh-TW", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })));
      b.onclick = () => pickChat(it);
      const ren = el("button", "icon-btn", "改名");
      ren.onclick = () => renameChat(it, () => { if (cur.id === it.id) cur.title = it.title; drawSide(); });
      const del = el("button", "icon-btn danger-t", "刪除");
      del.onclick = () => dropChat(it);
      row.append(b, ren, del);
      listBox.appendChild(row);
    });
  };
  CHAT2.drawSide = drawSide;
  drawSide();
  let qT;
  search.oninput = () => { clearTimeout(qT); qT = setTimeout(async () => { CHAT2.q = search.value.trim(); await loadChatList(); drawSide(); }, 250); };
  newB.onclick = () => {
    if (cur.busy) return;
    CHAT2.cur = newChat(sel.value || targets[0]?.key || "");
    CHAT2.sideOpen = false;
    viewChat();
  };

  // ---- 右：頂列
  const bar = el("div", "chat-bar");
  const sideT = el("button", "btn sm chat-side-t", "對話紀錄");
  sideT.onclick = () => { CHAT2.sideOpen = !CHAT2.sideOpen; side.classList.toggle("open", CHAT2.sideOpen); };
  const sel = el("select", "chat-target");
  const grp = {};
  targets.forEach((t) => {
    if (!grp[t.group]) { grp[t.group] = el("optgroup"); grp[t.group].label = t.group; sel.appendChild(grp[t.group]); }
    const o = el("option", null, t.label + (t.cn ? "（中國來源）" : ""));
    o.value = t.key;
    grp[t.group].appendChild(o);
  });
  if (!targets.length) { const o = el("option", null, "（目前沒有執行中的模型，只能看紀錄）"); o.value = ""; sel.appendChild(o); sel.disabled = true; }
  sel.value = cur.target;
  const status = el("span", "small muted chat-status");
  const setB = el("button", "btn sm", "對話設定");
  const expB = el("button", "btn sm", "匯出");
  bar.append(sideT, sel, status, setB, expB);
  mainCol.appendChild(bar);
  const tgt = () => targets.find((t) => t.key === sel.value);
  const syncStatus = () => {
    const t = tgt();
    if (!t) { status.textContent = "這則對話的模型目前沒有在執行"; return; }
    const st = cur.settings || {};
    const tuned = CHAT_PARAMS.some(([k]) => st[k] !== undefined && st[k] !== "") || st.system;
    status.textContent = (t.local ? `本機執行・對話不離開這台${cur.lastSpeed ? `・${fmt(cur.lastSpeed, 1)} tok/s` : ""}`
      : `第三方 API・對話內容會送到 ${t.prov.name}${t.cn ? "・中國來源，政府案不得使用" : ""}`)
      + (t.ctx ? `・上下文 ${int(t.ctx)}` : "") + (tuned ? "・已自訂設定" : "") + (cur.summary ? `・已壓縮前 ${cur.summary.upto} 則` : "");
    status.classList.toggle("warn-t", !t.local);
  };
  setB.onclick = () => openChatSettings(cur, tgt(), () => { syncStatus(); if (cur.id) saveChat(cur); });
  expB.onclick = () => exportChat(cur);

  const box = el("div", "chat-box");
  mainCol.appendChild(box);
  const noteRow = el("div", "small muted chat-note");
  noteRow.hidden = true;
  const setNote = (s) => { noteRow.textContent = s; noteRow.hidden = !s; };

  const ctl = { busy: () => cur.busy };
  const drawAll = () => {
    box.textContent = "";
    if (!cur.msgs.length) {
      const e = el("div", "chat-empty");
      e.append(el("h3", null, "有什麼可以幫你？"), el("p", "small muted", tgt()?.local === false ? "這個模型在外部服務，對話會送出去。" : "模型跑在你自己的機器上，對話不會上傳。"));
      const ex = el("div", "chips chat-ex");
      ["用 HTML 做一個會動的時鐘", "幫我把這段話改得更正式", "畫一張雪地裡的紅色狐狸", "解釋什麼是 MoE 模型"].forEach((x) => {
        const b = el("button", "chip-btn", x);
        b.onclick = () => { ta.value = x; cur.draft = x; ta.focus(); };
        ex.appendChild(b);
      });
      e.appendChild(ex);
      box.appendChild(e);
    }
    if (cur.summary?.upto) {
      const d = el("details", "chat-sum small");
      d.appendChild(el("summary", null, `前 ${cur.summary.upto} 則已壓縮成摘要送給模型（原文照留在下面）`));
      d.appendChild(el("div", "muted", cur.summary.text));
      box.appendChild(d);
    }
    cur.msgs.forEach((m, i) => box.appendChild(chatBubble(m, i, acts)));
    box.scrollTop = box.scrollHeight;
  };

  // ---- 輸入列
  const inputRow = el("div", "chat-input");
  const ta = el("textarea");
  ta.rows = 2;
  ta.value = cur.draft || "";
  ta.oninput = () => { cur.draft = ta.value; };
  const tools = el("div", "chat-tools");
  const attach = el("input");
  attach.type = "file";
  attach.multiple = true;
  attach.hidden = true;
  const attBtn = el("button", "btn sm", "附件");
  const toolB = el("button", "btn sm toggle", "工具");
  const imgB = el("button", "btn sm toggle", "畫圖");
  const vidB = el("button", "btn sm toggle", "影片");
  const docsRow = el("div", "chat-docs");
  const syncMode = () => {
    imgB.setAttribute("aria-pressed", String(cur.mode === "image"));
    vidB.setAttribute("aria-pressed", String(cur.mode === "video"));
    toolB.setAttribute("aria-pressed", String(!!cur.settings?.tools));
    const t = tgt();
    toolB.disabled = !t?.local;
    toolB.title = t?.local ? "讓模型呼叫主控台的查詢工具：機器狀態、推薦模型、已下載的模型（只查，不會動作）" : "第三方 API 不開放主控台的工具";
    ta.placeholder = cur.mode === "image" ? "描述想要的畫面（附一張圖就是改圖）。Enter 送出" : cur.mode === "video" ? "描述影片內容（附一張圖當第一格）。Enter 送出"
      : "問點什麼，或說「畫一張…」「生成一段…的影片」（Enter 送出，Shift+Enter 換行）";
  };
  imgB.onclick = () => { cur.mode = cur.mode === "image" ? "chat" : "image"; syncMode(); ta.focus(); };
  vidB.onclick = () => { cur.mode = cur.mode === "video" ? "chat" : "video"; syncMode(); ta.focus(); };
  toolB.onclick = () => { cur.settings = { ...(cur.settings || {}), tools: !cur.settings?.tools }; if (!cur.settings.tools) delete cur.settings.tools; syncMode(); };
  const drawDocs = () => {
    docsRow.textContent = "";
    if (cur.image) {
      const c = el("span", "chat-doc", "圖片");
      const x = el("button", null, "×");
      x.setAttribute("aria-label", "拿掉圖片");
      x.onclick = () => { cur.image = null; drawDocs(); };
      c.appendChild(x);
      docsRow.appendChild(c);
    }
    cur.docs.forEach((d, i) => {
      const c = el("span", "chat-doc", d.busy ? `${d.name}（讀取中…）` : `${d.name}・${int(d.chars)} 字${d.truncated ? "（太長，只取前段）" : ""}`);
      if (!d.busy) {
        const x = el("button", null, "×");
        x.setAttribute("aria-label", `拿掉 ${d.name}`);
        x.onclick = () => { cur.docs.splice(i, 1); drawDocs(); };
        c.appendChild(x);
      }
      docsRow.appendChild(c);
    });
    docsRow.hidden = !cur.image && !cur.docs.length;
  };
  attBtn.onclick = () => attach.click();
  attach.accept = ".pdf,.docx,.xlsx,.pptx,.txt,.md,.csv,.json,.log,.html,.xml,.yaml,.yml,.py,.js,.ts,.sh,.sql,image/png,image/jpeg,image/webp";
  attBtn.title = "附文件（PDF、Word、Excel、PowerPoint、純文字、程式碼）或圖片（看圖、改圖、影片第一格）";
  attach.onchange = async () => {
    const files = [...attach.files];
    attach.value = "";
    for (const f of files) {
      if (/^image\//.test(f.type)) {
        if (f.size > 4 * 1024 * 1024) { toast("圖片最大 4 MB", "bad"); continue; }
        cur.image = "data:" + f.type + ";base64," + await readB64(f);
        drawDocs();
        continue;
      }
      if (cur.docs.length >= 5) { toast("一則最多附 5 個檔案", "bad"); break; }
      if (f.size > 15 * 1024 * 1024) { toast(`${f.name} 超過 15 MB`, "bad"); continue; }
      const d = { name: f.name, busy: true };
      cur.docs.push(d);
      drawDocs();
      try {
        const r = await api("/api/extract", { method: "POST", body: { name: f.name, b64: await readB64(f) } });
        Object.assign(d, { busy: false, text: r.text, chars: r.chars, truncated: r.truncated, pages: r.pages });
      } catch (ex) {
        cur.docs.splice(cur.docs.indexOf(d), 1);
        toast(`${f.name}：${ex.message}`, "bad");
      }
      drawDocs();
    }
  };
  const send = el("button", "btn primary", "送出");
  const stopB = el("button", "btn sm danger", "停止");
  stopB.hidden = true;
  tools.append(attach, attBtn, toolB, imgB, vidB, el("span", "spacer"), stopB, send);
  inputRow.append(docsRow, ta, tools);
  drawDocs();
  mainCol.append(noteRow, inputRow);
  sel.onchange = () => { cur.target = sel.value; syncMode(); syncStatus(); if (cur.id) saveChat(cur); };
  syncMode();
  syncStatus();

  const lock = (on) => {
    cur.busy = on; send.disabled = on; stopB.hidden = !on;
    if (on) cur._genStop = false;
    box.querySelectorAll(".msg-acts button, .msg-acts select").forEach((b) => { b.disabled = on; });
  };
  const paintOf = (m) => () => paintReply(m, box, false, acts);

  // 產生一則回答（新的、重新生成、調整回答共用）：history 已組好；version＝留下舊版本
  async function answer(reply, msgsForCtx, extra, version) {
    const t = tgt();
    if (!t) { toast("這則對話的模型目前沒有在執行，先在上面換一個", "bad"); return; }
    if (version) {
      if (!reply.alts?.length) reply.alts = [{ content: reply.content, reasoning: reply.reasoning, stat: reply.stat }];
      Object.assign(reply, { content: "", reasoning: undefined, stat: undefined, error: undefined, pending: true });
      paintReply(reply, box, false, acts);
    }
    lock(true);
    const t0 = performance.now();
    cur.ctl = new AbortController();
    try {
      const before = cur.summary;
      let history = await maybeCompress(cur, t, msgsForCtx, setNote);
      if (cur.summary !== before) { drawAll(); syncStatus(); }
      if (extra) history = history.concat(extra);
      const hasImg = history.some((m) => Array.isArray(m.content));
      if (cur.settings?.tools && t.local && hasImg) setNote("這一則有附圖：工具只能處理文字，這次改用一般對話回答（看得到圖）");
      if (cur.settings?.tools && t.local && !hasImg) {
        // 系統提示詞與長對話的摘要都在 system 那一則：一起交給助理，壓縮過的前情才不會掉
        const sys = history.filter((m) => m.role === "system").map((m) => m.content).join("\n\n");
        const r = await api("/api/assistant", { method: "POST", signal: cur.ctl.signal, body: { node: t.n.id, slot: t.s.id,
          messages: history.filter((m) => m.role !== "system").map((m) => ({ role: m.role, content: m.content })),
          system: sys, sampling: sampleBody(cur, t) } });
        Object.assign(reply, { content: r.content, reasoning: r.reasoning, pending: false,
          stat: [r.steps?.length ? `查了：${r.steps.map((x) => TOOL_LABEL[x.tool] || x.tool).join("、")}` : "沒有用到工具",
            `${fmt((performance.now() - t0) / 1000, 1)} 秒`].filter(Boolean).join("・") });
        return;
      }
      const body = { messages: history, stream: true, ...sampleBody(cur, t), ...(t.prov ? { model: t.prov.default_model } : {}) };
      const { timings, usage } = await streamInto(t, body, reply, paintOf(reply), cur.ctl.signal);
      const sp = timings?.predicted_per_second;
      if (sp) cur.lastSpeed = sp;
      reply.stat = [usage?.completion_tokens ? `${usage.completion_tokens} tokens` : null, `${fmt((performance.now() - t0) / 1000, 1)} 秒`,
        sp ? `${fmt(sp, 1)} tok/s` : null].filter(Boolean).join("・");
    } catch (ex) {
      reply.pending = false;
      if (ex.name === "AbortError") reply.stat = "已停止";
      else reply.error = ex.message;
    } finally {
      cur.ctl = null;
      if (!reply.content && !reply.error && reply.stat !== "已停止") reply.content = "（沒有回答）";
      reply.pending = false;
      if (reply.alts) {
        reply.alts.push({ content: reply.content, reasoning: reply.reasoning, stat: reply.stat });
        if (reply.alts.length > 10) reply.alts.splice(0, reply.alts.length - 10);     // 主控台最多存 10 個版本：丟最舊的
        reply.alt = reply.alts.length - 1;
      }
      setNote("");
      lock(false);
      paintReply(reply, box, true, acts);
      syncStatus();
      saveChat(cur);
    }
  }

  const submit = async () => {
    const text = ta.value.trim();
    if (!text || cur.busy) return;
    if (cur.docs.some((d) => d.busy)) { toast("附件還在讀取，等一下再送", "bad"); return; }
    const t = tgt();
    const intent = detectGen(text, !!cur.image, cur.mode);
    if (!t && !intent) { toast("這則對話的模型目前沒有在執行，先在上面換一個", "bad"); return; }
    if (cur.image && !intent && !t.vision) { toast("這個模型看不懂圖片；要問圖請換一個標了「看圖」的模型，要改圖請說「把…改成…」或按「畫圖」", "bad"); return; }
    const user = { role: "user", content: text, image: cur.image || null,
      ...(cur.docs.length ? { docs: cur.docs.map(({ name, text: tx, chars, truncated }) => ({ name, text: tx, chars, truncated })) } : {}) };
    if (!cur.msgs.length) cur.title = text.replace(/\s+/g, " ").slice(0, 30);
    cur.msgs.push(user);
    const reply = { role: "assistant", content: "", pending: true };
    cur.msgs.push(reply);
    const initImage = intent && cur.image ? cur.image : null;
    ta.value = ""; cur.draft = ""; cur.image = null; cur.docs = []; drawDocs();
    if (cur.mode !== "chat") { cur.mode = "chat"; syncMode(); }
    if (box.querySelector(".chat-empty")) box.textContent = "";
    box.append(chatBubble(user, cur.msgs.length - 2, acts), chatBubble(reply, cur.msgs.length - 1, acts));
    box.scrollTop = box.scrollHeight;
    if (intent) {
      lock(true);
      try { await runGen(cur, t, reply, text, intent, initImage, paintOf(reply)); } finally {
        reply.content = genText(reply.gen);
        lock(false);
        paintReply(reply, box, true, acts);
        saveChat(cur);
      }
      return;
    }
    await answer(reply, cur.msgs.slice(0, -1));
  };

  // ---- 每則訊息的動作
  const acts = {
    busy: () => cur.busy,
    copy: (m) => copyText(m.content || ""),
    regen: (i) => {
      const m = cur.msgs[i];
      if (cur.summary && i < cur.summary.upto) cur.summary = null;     // 改的是壓縮進摘要的那段：摘要作廢，下次再重算
      if (m.gen) return regenMedia(i);
      answer(m, cur.msgs.slice(0, i), null, true);
    },
    rewrite: (i, kind) => {
      const m = cur.msgs[i];
      if (cur.summary && i < cur.summary.upto) cur.summary = null;
      const rw = REWRITE.find((x) => x[0] === kind);
      answer(m, cur.msgs.slice(0, i + 1), [{ role: "user", content: rw[2] }], true);
    },
    alt: (i, d) => {
      const m = cur.msgs[i];
      const k = Math.max(0, Math.min(m.alts.length - 1, (m.alt ?? m.alts.length - 1) + d));
      m.alt = k;
      Object.assign(m, { content: m.alts[k].content, reasoning: m.alts[k].reasoning, stat: m.alts[k].stat });
      paintReply(m, box, true, acts);
      saveChat(cur);
    },
    edit: (i) => editMsg(i),
    del: (i) => {
      cur.msgs.splice(i, 1);
      if (cur.summary && i < cur.summary.upto) cur.summary = null;
      drawAll();
      if (cur.msgs.length) saveChat(cur);
      else if (cur.id) api(`/api/chats/${cur.id}/delete`, { method: "POST", body: {} }).then(() => { cur.id = null; loadChatList().then(drawSide); }).catch(() => {});
      syncStatus();
    },
    reuse: (dataUrl) => { cur.image = dataUrl; drawDocs(); ta.focus(); toast("已把這張圖放進附件：說「把…改成…」就會照著改", "ok"); },
    gen: (m) => {
      const g = m.gen;
      if (g.state === "running" && !g._watch) {
        g._watch = true;
        watchChatGen(g, paintOf(m)).then(() => { m.content = genText(g); paintReply(m, box, true, acts); saveChat(cur); });
      }
    },
  };

  async function regenMedia(i) {
    const m = cur.msgs[i];
    const u = cur.msgs[i - 1];
    const t = tgt();
    const intent = { kind: m.gen.kind, count: m.gen.count || 1, edit: !!m.gen.edit };
    let init = null;
    if (m.gen.edit && u?.image) { try { init = await imgDataUrl(u.image); } catch (_) { init = null; } }
    const asked = m.gen.asked || u?.content || "";
    m.gen = null; m.content = ""; m.pending = true;
    paintReply(m, box, false, acts);
    lock(true);
    try { await runGen(cur, t, m, asked, intent, init, paintOf(m)); } finally {
      m.content = genText(m.gen); lock(false); paintReply(m, box, true, acts); saveChat(cur);
    }
  }

  function editMsg(i) {
    const m = cur.msgs[i];
    const b = m._el;
    b.textContent = "";
    const ed = el("textarea", "msg-edit");
    ed.value = m.content || "";
    ed.rows = Math.min(12, Math.max(3, ed.value.split("\n").length + 1));
    const row = el("div", "msg-acts");
    const cancel = el("button", "btn sm", "取消");
    const ok = el("button", "btn sm primary", m.role === "user" ? "儲存並重新送出" : "儲存");
    row.append(el("span", "spacer"), cancel, ok);
    b.append(ed, row);
    ed.focus();
    cancel.onclick = () => paintReply(m, box, true, acts);
    ok.onclick = async () => {
      const v = ed.value.trim();
      if (!v) { toast("內容不能空白", "bad"); return; }
      if (cur.summary && i < cur.summary.upto) cur.summary = null;
      if (m.role === "assistant") {
        m.content = v;
        if (m.alts) m.alts[m.alt ?? m.alts.length - 1].content = v;
        paintReply(m, box, true, acts);
        saveChat(cur);
        return;
      }
      m.content = v;
      cur.msgs.splice(i + 1);
      if (i === 0) cur.title = v.replace(/\s+/g, " ").slice(0, 30);
      const reply = { role: "assistant", content: "", pending: true };
      cur.msgs.push(reply);
      drawAll();
      const intent = detectGen(v, !!m.image, "chat");
      if (intent) {
        let init = null;
        if (m.image) { try { init = await imgDataUrl(m.image); } catch (_) { init = null; } }
        lock(true);
        try { await runGen(cur, tgt(), reply, v, intent, init, paintOf(reply)); } finally {
          reply.content = genText(reply.gen); lock(false); paintReply(reply, box, true, acts); saveChat(cur);
        }
        return;
      }
      answer(reply, cur.msgs.slice(0, -1));
    };
  }

  drawAll();
  send.onclick = submit;
  // 停止：文字回答中斷串流；畫圖、影片取消那台的產生工作（還在準備就不送出）
  const stopAll = () => {
    cur.ctl?.abort();
    cur._genStop = true;
    const g = [...cur.msgs].reverse().find((m) => m.gen)?.gen;
    if (g?.job && g.state === "running") api(`/api/node/${g.node}/sd/jobs/${g.job}/cancel`, { method: "POST", body: {} }).catch(() => {});
  };
  stopB.onclick = stopAll;
  imeGuard(ta, submit, { shiftNewline: true });
  ta.focus();
}

function renameChat(it, done) {
  const d = el("div");
  const inp = textInput(it.title || "", "新的標題");
  inp.maxLength = 80;
  d.appendChild(fieldBox("標題", inp));
  modal({ title: "改名", bodyNode: d, hasInput: true, confirmText: "儲存", onConfirm: async () => {
    const v = inp.value.trim();
    if (!v) { toast("標題不能空白", "bad"); return; }
    try { await api(`/api/chats/${it.id}/rename`, { method: "POST", body: { title: v } }); } catch (ex) { toast(ex.message, "bad"); return; }
    it.title = v;
    done();
  } });
}

function openChatSettings(cur, t, done) {
  const st = cur.settings || {};
  const d = el("div", "chat-set");
  const sys = el("textarea");
  sys.rows = 4;
  sys.maxLength = 8000;
  sys.placeholder = "例：你是一名資深的 Python 工程師，回答簡潔，程式碼要有中文註解。\n或：請用條列方式回答。";
  sys.value = st.system || "";
  d.appendChild(fieldBox("系統提示詞", sys, "每次對話都會先告訴模型。用來設定角色、語氣、回答格式。留空＝不設定。"));
  const adv = el("details", "chat-adv");
  adv.appendChild(el("summary", null, "進階：模型參數"));
  adv.appendChild(el("p", "small muted", "留空＝用模型自己的預設值（大多數情況下最好）。改了效果不好，按「恢復預設」即可。"
    + (t && !t.local ? "　第三方 API 只會送出溫度、Top P、新話題、最大回覆長度與隨機種子。" : "")));
  const grid = el("div", "form-row");
  const inputs = {};
  CHAT_PARAMS.forEach(([k, label, hint, lo, hi, step]) => {
    const i = numInput(st[k] ?? "", "預設");
    i.min = String(lo); i.max = String(hi); i.step = String(step);
    i.setAttribute("aria-label", label);
    if (t && !t.local && !STD_PARAMS.includes(k)) i.disabled = true;
    inputs[k] = i;
    grid.appendChild(fieldBox(label, i, hint));
  });
  adv.appendChild(grid);
  if (CHAT_PARAMS.some(([k]) => st[k] !== undefined)) adv.open = true;
  d.appendChild(adv);
  const asDef = el("label", "check-row");
  const cb = el("input");
  cb.type = "checkbox";
  asDef.append(cb, document.createTextNode("之後的新對話也用這組設定"));
  d.appendChild(asDef);
  const reset = el("button", "btn sm", "恢復預設");
  reset.onclick = () => { sys.value = ""; Object.values(inputs).forEach((i) => { i.value = ""; }); };
  d.appendChild(reset);
  modal({ title: "對話設定", bodyNode: d, hasInput: true, confirmText: "儲存", onConfirm: () => {
    const out = {};
    for (const [k, label, , lo, hi] of CHAT_PARAMS) {
      const raw = inputs[k].value.trim();
      if (raw === "") continue;
      const v = Number(raw);
      if (!Number.isFinite(v) || v < lo || v > hi || (["top_k", "max_tokens", "seed"].includes(k) && !Number.isInteger(v))) {
        toast(`${label} 要在 ${lo}～${hi} 之間${["top_k", "max_tokens", "seed"].includes(k) ? "的整數" : ""}`, "bad");
        return;
      }
      out[k] = v;
    }
    if (sys.value.trim()) out.system = sys.value.trim();
    if (st.tools) out.tools = true;
    cur.settings = out;
    if (cb.checked) { const { tools, ...keep } = out; try { localStorage.setItem(CHAT_DEF_KEY, JSON.stringify(keep)); } catch (_) { /* 無痕 */ } }
    toast("已套用到這則對話", "ok");
    done();
  } });
}

function exportChat(cur) {
  if (!cur.msgs.length) { toast("這則對話還是空的", "bad"); return; }
  const lines = [`# ${cur.title || "對話"}`, ""];
  if (cur.settings?.system) lines.push(`> 系統提示詞：${cur.settings.system.replace(/\n/g, " ")}`, "");
  cur.msgs.forEach((m) => {
    lines.push(m.role === "user" ? "## 我" : "## 模型", "");
    (m.docs || []).forEach((d) => lines.push(`（附件：${d.name}）`));
    if (m.image) lines.push("（附了一張圖）");
    lines.push(m.gen ? genText(m.gen) : (m.content || ""), "");
  });
  download(`${(cur.title || "對話").replace(/[\\/:*?"<>|]/g, "_").slice(0, 40)}.md`, lines.join("\n"), "text/markdown;charset=utf-8");
}

/* ---- 一則訊息：文字與程式碼區塊分開畫；回答下方是動作列 */

function chatBubble(m, i, acts) {
  const b = el("div", "msg " + m.role);
  m._el = b;
  m._i = i;
  paintMsg(m, true, acts);
  return b;
}

function paintReply(m, box, final, acts) {
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  paintMsg(m, final, acts);
  if (near) box.scrollTop = box.scrollHeight;
}

function paintMsg(m, final, acts) {
  const b = m._el;
  if (!b) return;
  b.textContent = "";
  if (m.image && !m.gen) { const im = el("img", "msg-img"); im.src = imgSrc(m.image); im.alt = "附加的圖片"; b.appendChild(im); }
  else if (m.image_lost) b.appendChild(el("p", "small muted", "這則附的圖當時沒有存下來；要繼續問這張圖請重新附上。"));
  if (m.docs?.length) {
    const row = el("div", "chat-docs");
    m.docs.forEach((d) => row.appendChild(el("span", "chat-doc", `${d.name}・${int(d.chars || 0)} 字${d.lost ? "（內容沒存下來，要再問請重新附上）" : ""}`)));
    b.appendChild(row);
  }
  if (m.gen) { paintGen(m, b, acts); return; }
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
  if (final && !m.pending && acts) b.appendChild(msgActs(m, acts));
}

function msgActs(m, acts, media) {
  const row = el("div", "msg-acts");
  const i = () => m._i ?? -1;
  const btn = (label, fn) => { const x = el("button", "link-btn", label); x.disabled = acts.busy(); x.onclick = fn; row.appendChild(x); return x; };
  // 畫圖、影片那則：只有重新產生與刪除（文字的複製、調整回答、編輯對它沒有意義）
  if (media) {
    btn("重新產生", () => acts.regen(i()));
    btn("刪除", () => acts.del(i()));
    return row;
  }
  btn("複製", () => acts.copy(m));
  if (m.role === "assistant") {
    btn("重新生成", () => acts.regen(i()));
    const rw = el("select", "msg-rw");
    rw.setAttribute("aria-label", "調整回答");
    rw.disabled = acts.busy();
    [["", "調整回答…"], ...REWRITE.map(([k, l]) => [k, l])].forEach(([k, l]) => { const o = el("option", null, l); o.value = k; rw.appendChild(o); });
    rw.onchange = () => { const k = rw.value; rw.value = ""; if (k) acts.rewrite(i(), k); };
    row.appendChild(rw);
  }
  btn("編輯", () => acts.edit(i()));
  btn("刪除", () => acts.del(i()));
  if (m.alts?.length > 1) {
    const k = m.alt ?? m.alts.length - 1;
    row.appendChild(el("span", "spacer"));
    const prev = btn("‹", () => acts.alt(i(), -1));
    prev.setAttribute("aria-label", "上一個版本");
    prev.disabled = acts.busy() || k === 0;
    row.appendChild(el("span", "small muted", `第 ${k + 1}/${m.alts.length} 版`));
    const next = btn("›", () => acts.alt(i(), 1));
    next.setAttribute("aria-label", "下一個版本");
    next.disabled = acts.busy() || k === m.alts.length - 1;
  }
  return row;
}

function paintGen(m, b, acts) {
  const g = m.gen;
  const kindT = g.kind === "video" ? "影片" : "圖片";
  if (m.pending || ["prepare", "prompt"].includes(g.state)) {
    b.appendChild(el("div", "small muted", g.state === "prompt" ? `正在把描述整理成${kindT}提示詞…` : `準備產生${kindT}…`));
    return;
  }
  if (g.state === "running") {
    b.appendChild(el("div", "small", `${g.title || ""}：${g._p || "排隊中"}`));
    const bar = el("div", "bar thin");
    const seg = el("div", "seg");
    seg.style.width = (g._pct || 0) + "%";
    bar.appendChild(seg);
    b.appendChild(bar);
    if (g.prompt) b.appendChild(el("div", "small muted mono msg-prompt", g.prompt));
    acts?.gen(m);
    return;
  }
  if (g.state !== "done") {
    const msg = g.state === "gone" ? `這件${kindT}已經在畫圖頁刪掉了。` : g.error || `${kindT}沒有產生成功`;
    const p = el("p", "note " + (["need_engine", "need_model"].includes(g.state) ? "warn" : "bad"), msg);
    if (g.state === "need_engine" || g.state === "need_model") {
      const go2 = el("button", "btn sm primary", g.state === "need_engine" ? "去安裝畫圖引擎" : `去下載${g.kind === "video" ? "影片" : "畫圖"}模型`);
      go2.onclick = () => { S.libTab = "sd"; S.sdSub = "rec"; go("models"); };
      p.appendChild(go2);
    }
    b.appendChild(p);
    if (acts) b.appendChild(msgActs(m, acts, true));
    return;
  }
  const holder = el("div", "msg-media");
  holder.appendChild(el("p", "small muted", "讀取中…"));
  b.appendChild(holder);
  b.appendChild(el("div", "small muted", `${g.title || ""}${g.edit ? "・改圖" : ""}・提示詞：${g.prompt || g.asked || ""}`));
  const row = el("div", "msg-acts");
  b.appendChild(row);
  genMedia(g).then((shots) => {
    holder.textContent = "";
    shots.forEach((sh) => {
      const cell = el("div", "msg-shot");
      const media = el(sh.mime.startsWith("video") ? "video" : "img");
      media.src = sh.url;
      if (media.tagName === "VIDEO") { media.controls = true; media.loop = true; } else media.alt = g.prompt || "";
      const dl = el("a", "link-btn", "下載");
      dl.href = sh.url; dl.download = sh.name || (g.kind === "video" ? "video.mp4" : "image.png");
      cell.append(media, dl);
      if (!sh.mime.startsWith("video") && acts) {
        const re = el("button", "link-btn", "用這張繼續改");
        re.onclick = () => acts.reuse(`data:${sh.mime};base64,${sh.b64}`);
        cell.appendChild(re);
      }
      holder.appendChild(cell);
    });
  }).catch((ex) => { holder.textContent = ""; holder.appendChild(el("p", "small muted", /404|沒有這/.test(ex.message) ? `這件${kindT}已經在畫圖頁刪掉了。` : `讀不到${kindT}：${ex.message}`)); });
  if (acts) {
    const r2 = msgActs(m, acts, true);
    const toPage = el("button", "link-btn", g.kind === "video" ? "到影片頁" : "到畫圖頁");
    toPage.onclick = () => go(g.kind === "video" ? "video" : "image");
    r2.appendChild(toPage);
    row.replaceWith(r2);
  }
}
