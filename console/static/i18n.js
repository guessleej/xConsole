/* 介面語言：繁體中文（原文）、English、日本語、한국어。
   畫面上的字都是程式直接寫的繁體中文；選了別的語言時，這裡把畫面上的文字與提示（placeholder、title、aria-label）
   照字典換掉。字典由 scripts/i18n_extract.py 抽出、翻好後放在 static/i18n-<語言>.json：
     - 完全相同的句子直接換；
     - 帶變數的句子（{0}、{1}）用樣板比對，變數原樣帶過去；
     - 都比不到時，把句子裡認得的片段由長到短換掉（程式把好幾段拼成一句的情況）。
   模型的回答、使用者打的字、程式碼、路徑、指令（.msg-text、pre、code、textarea、input、.mono、[data-i18n-skip]）一律不翻。
   畫面之後新加的內容由 MutationObserver 接著翻，同一個節點只翻一次。 */

const I18N = { lang: "zh-TW", exact: null, patterns: [], pieces: [], re: null, done: new WeakSet(), obs: null };
const I18N_LANGS = [["auto", "跟隨系統"], ["zh-TW", "繁體中文"], ["en", "English"], ["ja", "日本語"], ["ko", "한국어"]];
const I18N_KEY = "xconsole-lang";
const I18N_SKIP = ".msg-text, pre, code, textarea, input, .mono, .code-block, .msg-think, [data-i18n-skip], script, style";

// 日期、時間、數字的格式跟著介面語言
function uiLocale() {
  return { en: "en-US", ja: "ja-JP", ko: "ko-KR" }[I18N.lang] || "zh-TW";
}

function i18nPref() {
  try { return localStorage.getItem(I18N_KEY) || "auto"; } catch (_) { return "auto"; }
}

function i18nResolve(pref) {
  if (pref && pref !== "auto") return pref;
  const nav = (navigator.languages || [navigator.language || "zh-TW"]).map((x) => x.toLowerCase());
  for (const l of nav) {
    if (l.startsWith("zh")) return "zh-TW";
    if (l.startsWith("ja")) return "ja";
    if (l.startsWith("ko")) return "ko";
    if (l.startsWith("en")) return "en";
  }
  return "zh-TW";
}

const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

async function i18nInit() {
  I18N.lang = i18nResolve(i18nPref());
  document.documentElement.lang = I18N.lang;
  if (I18N.lang === "zh-TW") return;
  let dict;
  try {
    const res = await fetch(`i18n-${I18N.lang}.json`, { credentials: "same-origin" });
    if (!res.ok) throw new Error(String(res.status));
    dict = await res.json();
  } catch (_) { I18N.lang = "zh-TW"; return; }       // 字典讀不到就照原文，不擋畫面
  I18N.exact = new Map();
  const pieces = [];
  for (const [zh, tr] of Object.entries(dict)) {
    if (!tr) continue;
    if (/\{\d+\}/.test(zh)) {
      // 樣板：{n} 換成擷取群組；翻好的那句裡的 {n} 照編號放回
      const names = [];
      const rx = new RegExp("^" + reEsc(zh).replace(/\\\{(\d+)\\\}/g, (_, d) => { names.push(Number(d)); return "([\\s\\S]+?)"; }) + "$");
      I18N.patterns.push({ rx, names, tr, len: zh.length });
    } else {
      I18N.exact.set(zh, tr);
      if (zh.length >= 2 && /[㐀-鿿]/.test(zh)) pieces.push(zh);
    }
  }
  I18N.patterns.sort((a, b) => b.len - a.len);
  pieces.sort((a, b) => b.length - a.length);
  I18N.pieces = pieces;
  I18N.re = pieces.length ? new RegExp(pieces.slice(0, 4000).map(reEsc).join("|"), "g") : null;
}

function i18nText(s) {
  if (!I18N.exact || !s || !/[㐀-鿿]/.test(s)) return s;
  const lead = s.match(/^\s*/)[0], tail = s.match(/\s*$/)[0];
  const core = s.trim();
  const hit = I18N.exact.get(core);
  if (hit != null) return lead + hit + tail;
  for (const p of I18N.patterns) {
    const m = core.match(p.rx);
    if (m) {
      let out = p.tr;
      p.names.forEach((n, i) => { out = out.split(`{${n}}`).join(i18nText(m[i + 1])); });
      return lead + out + tail;
    }
  }
  if (!I18N.re) return s;
  // 拼起來的句子：認得的片段換掉（由長到短），空白補在中文與英文之間
  return s.replace(I18N.re, (m) => {
    const t = I18N.exact.get(m);
    return t == null ? m : (I18N.lang === "en" ? ` ${t} ` : t);
  }).replace(/ {2,}/g, " ");
}

function i18nNode(root) {
  if (I18N.lang === "zh-TW" || !I18N.exact) return;
  const els = root.nodeType === 1 ? [root, ...root.querySelectorAll("[placeholder],[title],[aria-label]")] : [];
  els.forEach((e) => {
    if (e.closest && e.closest("[data-i18n-skip]")) return;
    ["placeholder", "title", "aria-label"].forEach((a) => {
      const v = e.getAttribute && e.getAttribute(a);
      if (v && /[㐀-鿿]/.test(v)) {
        const t = i18nText(v);
        if (t !== v) e.setAttribute(a, t);
      }
    });
    // 下拉選單的選項也是文字節點，下面的走訪會處理
  });
  const walk = document.createTreeWalker(root.nodeType === 3 ? root.parentNode : root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement && n.parentElement.closest(I18N_SKIP) ? NodeFilter.FILTER_REJECT
      : /[㐀-鿿]/.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
  });
  const todo = [];
  for (let n = walk.nextNode(); n; n = walk.nextNode()) todo.push(n);
  todo.forEach((n) => {
    const t = i18nText(n.nodeValue);
    if (t !== n.nodeValue) n.nodeValue = t;
  });
}

function i18nStart() {
  if (I18N.lang === "zh-TW" || !I18N.exact) return;
  document.title = i18nText(document.title);
  i18nNode(document.body);
  let queued = new Set(), timer = null;
  const flush = () => {
    timer = null;
    const list = [...queued];
    queued = new Set();
    I18N.obs.disconnect();             // 自己改字不要再觸發自己
    list.forEach((n) => { if (n.isConnected) i18nNode(n); });
    I18N.obs.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["placeholder", "title", "aria-label"] });
  };
  I18N.obs = new MutationObserver((recs) => {
    recs.forEach((r) => {
      if (r.type === "childList") r.addedNodes.forEach((n) => queued.add(n.nodeType === 3 ? n.parentNode || n : n));
      else queued.add(r.target.nodeType === 3 ? r.target.parentNode || r.target : r.target);
    });
    // 串流回答每 120 毫秒改一次字：合併到下一個畫面更新再翻，不要每個字元翻一次
    if (!timer) timer = requestAnimationFrame(flush);
  });
  I18N.obs.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["placeholder", "title", "aria-label"] });
}

function i18nSet(pref) {
  try { localStorage.setItem(I18N_KEY, pref); } catch (_) { /* 無痕：只這一頁 */ }
  location.reload();
}

// 語言選單（登入畫面與頂列共用）：選項本身固定用各語言自己的寫法，不翻
function i18nPicker() {
  const s = document.createElement("select");
  s.className = "lang-pick";
  s.setAttribute("aria-label", "介面語言 Language");
  s.setAttribute("data-i18n-skip", "");
  I18N_LANGS.forEach(([v, label]) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = v === "auto" ? "Auto（跟隨系統）" : label;
    s.appendChild(o);
  });
  s.value = i18nPref();
  s.onchange = () => i18nSet(s.value);
  return s;
}
