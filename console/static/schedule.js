/* ================================================================ 定時任務
   到時間叫模型做一件事（查新聞、彙整、提醒），結果存成對話（「定時任務」分類），也可以推送到手機或自家系統。
   排程在主控台跑，這一頁只是管理；不必開著網頁。放在 Agent 的第二列（頂層維持八格）。 */

const SCHED_TEMPLATES = [
  { name: "每日AI新聞", rule: { kind: "daily", time: "08:30" }, web: true,
    prompt: "搜尋今天的AI與大型語言模型重要新聞，挑5則，每則用兩三句話說明重點並附來源網址。" },
  { name: "明天天氣與穿搭", rule: { kind: "daily", time: "21:00" }, web: true,
    prompt: "查台北市明天的天氣預報（溫度、降雨機率、紫外線），給一段穿搭與要不要帶傘的建議，附資料來源。" },
  { name: "匯率與金價", rule: { kind: "daily", time: "09:00" }, web: true,
    prompt: "查今天美元、日圓、歐元兌新台幣的匯率，以及國際金價，跟昨天比漲跌多少，用表格列出並附來源。" },
  { name: "週五一週總結提醒", rule: { kind: "weekly", days: [4], time: "17:30" }, web: false,
    prompt: "提醒我整理這週的工作：列出一份「本週完成、遇到的問題、下週要做」的空白模板，最後提醒記得寫週報。" },
];
const WEEK = ["一", "二", "三", "四", "五", "六", "日"];
const pad2 = (n) => String(n).padStart(2, "0");
const fmtWhen = (ts) => { const d = new Date(ts * 1000); return `${d.getMonth() + 1}/${d.getDate()} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };

// 可以排定時任務的模型：主控台管的每一台的語言模型欄位（沒在跑也列，跑的時候可以自動啟動），加上第三方API（只有管理員）
function schedTargets() {
  const out = [];
  (S.fleet?.nodes || []).forEach((n) => llmSlots(n).filter((s) => s.unit).forEach((s) => out.push({
    key: `local:${n.id}:${s.id}`, label: `${s.model || s.role}（${n.name}）${s.busy ? "" : "・沒在跑"}`,
    target: { kind: "local", node: n.id, slot: s.id } })));
  if (isAdmin()) (S.providers || []).filter((p) => p.key_set).forEach((p) => out.push({
    key: `provider:${p.id}`, label: `第三方API・${p.name}・${p.default_model || ""}${p.cn_origin ? "（中國來源）" : ""}`,
    target: { kind: "provider", id: p.id } }));
  return out;
}
const targetKey = (t) => (t?.kind === "provider" ? `provider:${t.id}` : t ? `local:${t.node}:${t.slot}` : "");

// 含輸入欄位的視窗：不准點背景關（填到一半點到背景整張表單會蒸發）；Esc 或「取消」才關。出錯時留在原處給人改
function schedDialog(title, body, buttons) {
  const overlay = el("div", "overlay");
  const box = el("div", "modal sched-modal");
  box.appendChild(el("h3", null, title));
  box.appendChild(body);
  const err = el("div");
  const actions = el("div", "actions");
  const close = () => { overlay.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape" && $("#modal-host").lastElementChild === overlay) close(); };     // 疊了兩層只關最上面那層
  const cancel = el("button", "btn", "取消");
  cancel.onclick = close;
  actions.appendChild(cancel);
  buttons.forEach(([label, cls, fn]) => {
    const b = el("button", "btn " + cls, label);
    b.onclick = async () => {
      errNote(err, "");
      b.disabled = true;
      try { if (await fn() !== false) close(); } catch (ex) { errNote(err, ex.message); } finally { b.disabled = false; }
    };
    actions.appendChild(b);
  });
  box.append(err, actions);
  overlay.appendChild(box);
  document.addEventListener("keydown", esc);
  $("#modal-host").appendChild(overlay);
  return { overlay, close };
}

async function viewSchedule() {
  const main = $("#main");
  const v = S.view;
  main.textContent = "";
  const wrap = el("div", "wrap");
  main.appendChild(wrap);
  const head = el("div", "page-head");
  const h = el("div");
  h.append(el("h1", null, "定時任務"),
    el("p", "small muted", "到時間叫模型做一件事，結果存成對話（放在「定時任務」分類，可以接著問），也可以推送到手機。主控台開著就會跑，不必開著這個網頁。"));
  const acts = el("div", "slot-actions");
  const chanB = el("button", "btn", "推送管道");
  const addB = el("button", "btn primary", "新增任務");
  acts.append(chanB, addB);
  head.append(h, acts);
  wrap.appendChild(head);
  const listBox = el("div", "stack");
  wrap.appendChild(listBox);
  loading(listBox);
  if (!S.providers && isAdmin()) {
    try { S.providers = (await api("/api/providers")).items || []; } catch (_) { S.providers = []; }
  }
  let data;
  try { data = await api("/api/schedules"); } catch (ex) { errNote(listBox, ex.message); return; }
  if (S.view !== v) return;
  S.sched = data;
  const reload = async () => { try { S.sched = await api("/api/schedules"); } catch (_) { return; } draw(); };
  addB.onclick = () => openSchedEditor(null, reload);
  chanB.onclick = () => openChannels(reload);
  chanB.textContent = `推送管道（${data.channels.length}）`;
  const open = new Set();
  let sig = "";
  const draw = () => {
    const d = S.sched;
    chanB.textContent = `推送管道（${d.channels.length}）`;
    const s2 = JSON.stringify(d.tasks.map((t) => [t.id, t.enabled, t.running, t.queued, t.next, t.runs[0]?.ts, t.name, t.rule_text, t.channels]))
      + JSON.stringify(d.channels.map((c) => c.name)) + [...open].join();
    if (s2 === sig) return;
    sig = s2;
    listBox.replaceChildren(...(d.tasks.length ? d.tasks.map((t) => schedCard(t, d, open, reload)) : [schedEmpty(reload)]));
  };
  draw();
  // 有任務在跑或排隊時每3秒看一次進度；只有內容真的變了才換那一塊，不重播進場動畫
  const poll = async () => {
    if (S.view !== v || !listBox.isConnected) return;
    if (!document.hidden && S.sched.tasks.some((t) => t.running || t.queued)) await reload();
    setTimeout(poll, 3000);
  };
  setTimeout(poll, 3000);
  S.live = () => { if (!S.sched.tasks.some((t) => t.running || t.queued)) reload(); };
  reveal(wrap);
}

function schedEmpty(reload) {
  const c = card("還沒有定時任務", "從範本開始，或按右上角「新增任務」自己寫");
  const g = el("div", "grid sched-tpl");
  SCHED_TEMPLATES.forEach((tp) => {
    const b = el("button", "sched-tpl-card");
    b.append(el("b", null, tp.name), el("span", "small muted", ruleTextLocal(tp.rule)), el("span", "small", tp.prompt));
    b.onclick = () => openSchedEditor({ ...tp, channels: [] }, reload, true);
    g.appendChild(b);
  });
  c.appendChild(g);
  return c;
}

function ruleTextLocal(r) {
  if (r.kind === "daily") return `每天${r.time}`;
  if (r.kind === "weekly") return (r.days.join() === "0,1,2,3,4" ? "週一到週五" : "每週" + r.days.map((d) => WEEK[d]).join("、")) + r.time;
  if (r.kind === "every") return r.minutes % 60 ? `每${r.minutes}分鐘` : `每${r.minutes / 60}小時`;
  return "只跑一次";
}

function schedCard(t, d, open, reload) {
  const c = el("section", "card sched-card");
  const top = el("div", "sched-top");
  const title = el("div");
  title.append(el("b", null, t.name), el("div", "small muted", [t.rule_text,
    t.running ? "正在執行" : t.queued ? "排隊中" : t.enabled ? (t.next ? `下次${fmtWhen(t.next)}` : "不會再跑") : "已暫停"].join("・")));
  const sw = el("input");
  sw.type = "checkbox";
  sw.className = "switch";
  sw.checked = t.enabled;
  sw.title = t.enabled ? "暫停這個任務" : "重新啟用（從現在開始算，不補暫停期間）";
  sw.setAttribute("aria-label", "啟用這個任務");
  sw.onchange = async () => {
    try { await api(`/api/schedules/${t.id}/toggle`, { method: "POST", body: { enabled: sw.checked } }); } catch (ex) { toast(ex.message, "bad"); }
    reload();
  };
  top.append(title, sw);
  c.appendChild(top);
  const chans = t.channels.map((id) => d.channels.find((x) => x.id === id)?.name).filter(Boolean);
  c.appendChild(el("p", "small", `模型：${t.model}${t.web ? "・可聯網" : ""}${chans.length ? `・推送到${chans.join("、")}` : "・只存成對話"}`));
  const last = t.runs[0];
  if (last) c.appendChild(el("p", "small " + (last.ok ? "ok-t" : "bad-t"), `上次${fmtWhen(last.ts)}${last.manual ? "（手動）" : ""}：${last.ok ? "完成" : "沒有完成，" + (last.error || "")}`));
  const row = el("div", "slot-actions");
  const runB = el("button", "btn sm " + (t.running || t.queued ? "danger" : "primary"), t.running || t.queued ? "停止" : "立即執行");
  runB.onclick = async () => {
    try { await api(`/api/schedules/${t.id}/${t.running || t.queued ? "stop" : "run"}`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); }
    reload();
  };
  const edB = el("button", "btn sm", "編輯");
  edB.onclick = () => openSchedEditor(t, reload);
  const histB = el("button", "btn sm", open.has(t.id) ? "收起紀錄" : `執行紀錄（${t.runs.length}）`);
  histB.disabled = !t.runs.length;
  histB.onclick = () => { if (open.has(t.id)) open.delete(t.id); else open.add(t.id); reload(); };
  const delB = el("button", "btn sm danger", "刪除");
  delB.onclick = () => modal({ title: "刪除這個任務", bodyNode: el("p", null, `「${t.name}」和它的執行紀錄會一起刪掉；已經存成對話的結果不受影響。`),
    confirmText: "刪除", danger: true, onConfirm: async () => {
      try { await api(`/api/schedules/${t.id}/delete`, { method: "POST", body: {} }); toast("已刪除", "ok"); } catch (ex) { toast(ex.message, "bad"); }
      reload();
    } });
  row.append(runB, edB, histB, el("span", "spacer"), delB);
  c.appendChild(row);
  if (open.has(t.id)) c.appendChild(schedRuns(t));
  return c;
}

function schedRuns(t) {
  const box = el("div", "sched-runs");
  t.runs.forEach((r, i) => {
    const dd = el("details");
    dd.open = i === 0;
    dd.appendChild(el("summary", null, `${fmtWhen(r.ts)}${r.manual ? "・手動" : ""}・${r.ok ? "完成" : "沒有完成"}${r.secs != null ? `・${r.secs}秒` : ""}${r.model ? `・${r.model}` : ""}`));
    if (r.error) dd.appendChild(el("p", "note bad", r.error));
    if (r.steps?.length) dd.appendChild(el("p", "small muted", "用了：" + r.steps.map((s) => (TOOL_LABEL[s.tool] || s.tool) + (s.error ? "（失敗）" : "")).join("、")));
    (r.pushes || []).forEach((p) => dd.appendChild(el("p", "small " + (p.ok ? "ok-t" : "bad-t"), `推送到${p.name}：${p.ok ? "成功" : p.error}`)));
    if (r.result) dd.appendChild(mdBlock(r.result));
    if (r.chat) dd.appendChild(el("p", "small muted", "這次的結果也存在對話頁的「定時任務」分類，可以接著問。"));
    box.appendChild(dd);
  });
  return box;
}

function openSchedEditor(t, reload, fromTemplate) {
  const d = S.sched || { channels: [], search: false, may_operate: false };
  const editing = t && !fromTemplate;
  const body = el("div", "sched-form");
  const name = textInput(t?.name || "", "空白就用要做的事的前幾個字");
  name.maxLength = 60;
  const prompt = el("textarea");
  prompt.rows = 4;
  prompt.maxLength = 4000;
  prompt.value = t?.prompt || "";
  prompt.placeholder = "例：搜尋今天的AI重要新聞，挑5則，每則兩三句話說明並附來源網址。";
  const kind = selectInput([["daily", "每天"], ["weekly", "每週"], ["every", "每隔一段時間"], ["once", "只跑一次"]], t?.rule?.kind || "daily");
  const tm = el("input");
  tm.type = "time";
  tm.value = t?.rule?.time || "08:30";
  const days = el("div", "sched-days");
  const dayBoxes = WEEK.map((w, i) => {
    const l = el("label", "check-row sched-chk");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = (t?.rule?.days || [0, 1, 2, 3, 4]).includes(i);
    l.append(cb, document.createTextNode("週" + w));
    days.appendChild(l);
    return cb;
  });
  const everyN = el("input");
  everyN.type = "number";
  everyN.min = "1";
  const mins = t?.rule?.minutes || 120;
  const everyU = selectInput([["60", "小時"], ["1", "分鐘"]], mins % 60 ? "1" : "60");
  everyN.value = String(mins % 60 ? mins : mins / 60);
  const everyBox = el("div", "sched-inline");
  everyBox.append(document.createTextNode("每"), everyN, everyU);
  const at = el("input");
  at.type = "datetime-local";
  const atTs = t?.rule?.at ? new Date(t.rule.at * 1000) : new Date(Date.now() + 3600 * 1000);
  at.value = `${atTs.getFullYear()}-${pad2(atTs.getMonth() + 1)}-${pad2(atTs.getDate())}T${pad2(atTs.getHours())}:${pad2(atTs.getMinutes())}`;
  const ruleRow = el("div", "sched-rule");
  const syncRule = () => {
    ruleRow.replaceChildren(kind);
    if (kind.value === "daily") ruleRow.appendChild(tm);
    if (kind.value === "weekly") ruleRow.append(days, tm);
    if (kind.value === "every") ruleRow.appendChild(everyBox);
    if (kind.value === "once") ruleRow.appendChild(at);
  };
  kind.onchange = syncRule;
  syncRule();
  const targets = schedTargets();
  const tgt = selectInput(targets.length ? targets.map((x) => [x.key, x.label]) : [["", "還沒有可以用的模型欄位"]], targetKey(t?.target) || targets[0]?.key || "");
  const check = (label, on, sub) => {
    const l = el("label", "check-row sched-chk");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!on;
    l.append(cb, document.createTextNode(label));
    if (sub) l.appendChild(el("span", "small muted", "　" + sub));
    return [l, cb];
  };
  const [webL, web] = check("允許聯網搜尋與讀網頁", t?.web, d.search ? "" : "（管理者還沒設定搜尋服務，只能讀指定的網頁）");
  const [saveL, saveC] = check("結果存成對話（可以接著問）", t?.save_chat !== false);
  const [failL, failC] = check("沒有完成時也推送通知", t?.notify_fail !== false);
  const [autoL, autoC] = check("模型沒在跑就自動啟動（最多等10分鐘）", t?.auto_start);
  const [stopL, stopC] = check("跑完把自動啟動的模型關掉，讓出顯示卡", t?.auto_stop);
  const chanBox = el("div", "sched-chans");
  const chanCbs = d.channels.map((ch) => {
    const [l, cb] = check(ch.name, editing ? (t.channels || []).includes(ch.id) : true, ch.kind === "telegram" ? "Telegram" : "Webhook");
    chanBox.appendChild(l);
    return [ch.id, cb];
  });
  if (!d.channels.length) chanBox.appendChild(el("p", "small muted", "還沒有推送管道：結果只會存成對話。要推到手機，先到「推送管道」加一個Telegram。"));
  body.append(fieldBox("名稱", name), fieldBox("要模型做什麼", prompt, "寫清楚要查什麼、要什麼格式；這段會原樣交給模型。"),
    fieldBox("什麼時候跑", ruleRow, "照主控台這台的時間；主控台關機錯過的，12小時內開機會補跑一次。"), fieldBox("用哪個模型", tgt),
    el("div", "sched-checks"), fieldBox("推送到", chanBox));
  body.querySelector(".sched-checks").append(webL, saveL, failL, ...(d.may_operate ? [autoL, stopL] : []));
  const collect = () => {
    const k = kind.value;
    const rule = k === "daily" ? { kind: k, time: tm.value } : k === "weekly" ? { kind: k, time: tm.value, days: dayBoxes.map((cb, i) => (cb.checked ? i : -1)).filter((i) => i >= 0) }
      : k === "every" ? { kind: k, minutes: Math.round(Number(everyN.value) * Number(everyU.value)) } : { kind: k, at: Math.floor(new Date(at.value).getTime() / 1000) };
    return { ...(editing ? { id: t.id } : {}), name: name.value, prompt: prompt.value, rule, enabled: editing ? t.enabled : true,
      target: targets.find((x) => x.key === tgt.value)?.target, web: web.checked, save_chat: saveC.checked, notify_fail: failC.checked,
      ...(d.may_operate ? { auto_start: autoC.checked, auto_stop: stopC.checked } : {}),
      channels: chanCbs.filter(([, cb]) => cb.checked).map(([id]) => id) };
  };
  const save = async (runNow) => {
    await api("/api/schedules/save", { method: "POST", body: { ...collect(), run_now: runNow } });
    toast(runNow ? "已儲存，正在執行一次" : "已儲存", "ok");
    reload();
  };
  schedDialog(editing ? "編輯定時任務" : "新增定時任務", body, [["儲存並立即執行一次", "", () => save(true)], ["儲存", "primary", () => save(false)]]);
  (editing ? name : prompt).focus();
}

function openChannels(reload) {
  const body = el("div", "stack");
  const list = el("div", "stack");
  const drawList = () => {
    const chans = S.sched?.channels || [];
    list.replaceChildren(...(chans.length ? chans.map((ch) => {
      const r = el("div", "sched-chan-row");
      r.append(el("b", null, ch.name), el("span", "small muted", ch.kind === "telegram" ? `Telegram・Chat ID ${ch.chat_id}` : `Webhook・${ch.host}`));
      const sp = el("span", "spacer");
      const test = el("button", "btn sm", "發測試訊息");
      test.onclick = async () => {
        test.disabled = true;
        try { await api(`/api/push/${ch.id}/test`, { method: "POST", body: {} }); toast("測試訊息送出了，去手機看看", "ok"); } catch (ex) { toast(ex.message, "bad"); }
        test.disabled = false;
      };
      const ed = el("button", "btn sm", "編輯");
      ed.onclick = () => editChannel(ch, after);
      const del = el("button", "btn sm danger", "刪除");
      del.onclick = async () => {
        if (!confirm(`刪除「${ch.name}」？用到它的任務會自動拿掉這個管道。`)) return;
        try { await api(`/api/push/${ch.id}/delete`, { method: "POST", body: {} }); } catch (ex) { toast(ex.message, "bad"); }
        after();
      };
      r.append(sp, test, ed, del);
      return r;
    }) : [el("p", "small muted", "還沒有推送管道。")]));
  };
  const after = async () => { try { S.sched = await api("/api/schedules"); } catch (_) { /* 下次打開再讀 */ } drawList(); reload(); };
  const addT = el("button", "btn sm primary", "加Telegram");
  addT.onclick = () => editChannel({ kind: "telegram" }, after);
  const addW = el("button", "btn sm", "加Webhook");
  addW.onclick = () => editChannel({ kind: "webhook" }, after);
  const addRow = el("div", "slot-actions");
  addRow.append(addT, addW);
  body.append(el("p", "small muted", "令牌與網址加密存在主控台，存好之後畫面只顯示「已設定」。Telegram最方便：手機裝Telegram、找@BotFather建一個機器人拿Token。"), list, addRow);
  drawList();
  schedDialog("推送管道", body, []);
}

function editChannel(ch, after) {
  const isT = ch.kind === "telegram";
  const body = el("div", "stack");
  const name = textInput(ch.name || "", isT ? "例：我的手機" : "例：Teams頻道");
  name.maxLength = 40;
  let save;
  if (isT) {
    const token = el("input");
    token.type = "password";
    token.autocomplete = "off";
    token.placeholder = ch.id ? "已設定（要換才重填）" : "123456789:AA…";
    const chat = textInput(ch.chat_id || "", "數字（群組是負數），或頻道的@名稱");
    const pick = el("div", "stack");
    const look = el("button", "btn sm", "查找Chat ID");
    look.onclick = async () => {
      look.disabled = true;
      try {
        const r = await api("/api/push/telegram/chats", { method: "POST", body: token.value ? { token: token.value } : { id: ch.id } });
        pick.replaceChildren(...(r.chats.length ? r.chats.map((c) => {
          const b = el("button", "btn sm", `${c.name}（${c.type}・${c.id}）`);
          b.onclick = () => { chat.value = c.id; pick.textContent = ""; };
          return b;
        }) : [el("p", "small muted", r.hint)]));
      } catch (ex) { pick.replaceChildren(el("p", "note bad", ex.message)); }
      look.disabled = false;
    };
    body.append(fieldBox("名稱", name), fieldBox("Bot Token", token, "跟Telegram的@BotFather建機器人時給的那一串。"),
      fieldBox("Chat ID", chat, "先在Telegram傳一句話給你的機器人，再按「查找Chat ID」自動填。"), look, pick);
    save = () => api("/api/push/save", { method: "POST", body: { ...(ch.id ? { id: ch.id } : {}), kind: "telegram", name: name.value, token: token.value, chat_id: chat.value } });
  } else {
    const url = el("input");
    url.type = "password";
    url.autocomplete = "off";
    url.placeholder = ch.id ? `已設定（${ch.host}，要換才重填）` : "https://…";
    const bearer = el("input");
    bearer.type = "password";
    bearer.autocomplete = "off";
    bearer.placeholder = "選填";
    body.append(fieldBox("名稱", name), fieldBox("Webhook網址", url, "主控台會POST一個JSON：{\"title\", \"content\", \"source\": \"xConsole\"}，可以接Teams、LINE、n8n、Home Assistant或自家系統。"),
      fieldBox("Bearer Token", bearer, "對方要驗身分才填；放在Authorization標頭送，不會放進網址。"));
    save = () => api("/api/push/save", { method: "POST", body: { ...(ch.id ? { id: ch.id } : {}), kind: "webhook", name: name.value, url: url.value, bearer: bearer.value } });
  }
  schedDialog(ch.id ? `編輯${ch.name}` : isT ? "加Telegram" : "加Webhook", body, [["儲存", "primary", async () => { await save(); toast("已儲存", "ok"); after(); }]]);
  name.focus();
}
