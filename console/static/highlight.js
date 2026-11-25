/* 程式碼區塊的語法高亮：離線、不靠外部套件。
   一律用 textContent 組 <span>，模型輸出的程式碼不會被當成 HTML 執行。
   用法：HL.paint(pre, code, lang)；沒標語言時自動認 HTML、JSON、SVG。 */
const HL = (() => {
  const set = (s) => new Set(s.split(/\s+/).filter(Boolean));
  const KW = {
    js: set(`break case catch class const continue debugger default delete do else export extends finally for function if import in
      instanceof let new of return super switch this throw try typeof var void while with yield async await static get set null undefined
      true false interface type enum implements private public protected readonly declare namespace abstract as keyof from`),
    py: set(`and as assert async await break class continue def del elif else except finally for from global if import in is lambda
      nonlocal not or pass raise return try while with yield None True False self match case`),
    c: set(`auto break case char const continue default do double else enum extern float for goto if inline int long register return
      short signed sizeof static struct switch typedef union unsigned void volatile while bool true false nullptr class namespace template
      typename public private protected virtual override new delete using include define ifdef ifndef endif pragma __global__ __device__
      __host__ __shared__ abstract boolean byte extends final finally implements import instanceof interface package super synchronized
      this throw throws try null var val fun object string`),
    go: set(`break case chan const continue default defer else fallthrough for func go goto if import interface map package range return
      select struct switch type var nil true false`),
    rust: set(`as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref
      return self Self static struct super trait true type unsafe use where while`),
    php: set(`echo function class public private protected return if else elseif foreach for while new null true false array namespace use
      static as`),
    sh: set(`if then else elif fi for while do done case esac in function return export local echo exit set unset source alias sudo cd
      param foreach switch try catch finally throw`),
    sql: set(`select from where insert into values update set delete create table index view drop alter add join left right inner outer on
      as and or not null is in like group by order having limit offset distinct union all primary key foreign references default case
      when then else end count sum avg min max`),
    bat: set(`echo set if else goto call exit rem for in do not exist errorlevel setlocal endlocal`),
  };
  const ALIAS = {
    javascript: "js", js: "js", jsx: "js", ts: "js", tsx: "js", typescript: "js", mjs: "js", node: "js",
    python: "py", py: "py", python3: "py",
    c: "c", h: "c", cpp: "c", "c++": "c", cc: "c", hpp: "c", cuda: "c", cu: "c", java: "c", cs: "c", csharp: "c", kotlin: "c", kt: "c",
    swift: "c", scala: "c", go: "go", golang: "go", rust: "rust", rs: "rust", php: "php",
    bash: "sh", sh: "sh", shell: "sh", zsh: "sh", console: "sh", powershell: "sh", ps1: "sh", pwsh: "sh",
    sql: "sql", mysql: "sql", postgresql: "sql", sqlite: "sql", bat: "bat", batch: "bat", cmd: "bat",
    html: "html", xml: "html", svg: "html", vue: "html", css: "css", scss: "css", less: "css", json: "json", jsonc: "json",
    yaml: "yaml", yml: "yaml", toml: "yaml", ini: "yaml", conf: "yaml", env: "yaml", diff: "diff", patch: "diff",
  };
  const STR = String.raw`"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'`;
  const NUM = String.raw`\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b`;
  // 每一種：依序試的 [類別, 正規式片段]；"w" 表示一般識別字（再看是不是關鍵字、函式、型別）
  const RULES = {
    js: [["com", String.raw`\/\/[^\n]*|\/\*[\s\S]*?\*\/`], ["str", STR + "|`(?:\\\\.|[^`\\\\])*`"], ["num", NUM], ["w", String.raw`[A-Za-z_$][\w$]*`]],
    py: [["com", "#[^\\n]*"], ["str", String.raw`"""[\s\S]*?"""|'''[\s\S]*?'''|[rbfu]{0,2}` + `(?:${STR})`], ["num", NUM], ["w", String.raw`[A-Za-z_]\w*`]],
    c: [["com", String.raw`\/\/[^\n]*|\/\*[\s\S]*?\*\/`], ["meta", "^\\s*#\\s*\\w+[^\\n]*"], ["str", STR], ["num", NUM], ["w", String.raw`[A-Za-z_]\w*`]],
    go: [["com", String.raw`\/\/[^\n]*|\/\*[\s\S]*?\*\/`], ["str", STR + "|`[^`]*`"], ["num", NUM], ["w", String.raw`[A-Za-z_]\w*`]],
    rust: [["com", String.raw`\/\/[^\n]*|\/\*[\s\S]*?\*\/`], ["str", STR], ["num", NUM], ["w", String.raw`[A-Za-z_]\w*!?`]],
    php: [["com", String.raw`\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/`], ["str", STR], ["var", String.raw`\$\w+`], ["num", NUM], ["w", String.raw`[A-Za-z_]\w*`]],
    sh: [["com", "#[^\\n]*"], ["str", STR], ["var", String.raw`\$\{?[\w:-]+\}?`], ["num", NUM], ["w", String.raw`[A-Za-z_][\w-]*`]],
    sql: [["com", String.raw`--[^\n]*|\/\*[\s\S]*?\*\/`], ["str", STR], ["num", NUM], ["w", String.raw`[A-Za-z_]\w*`]],
    bat: [["com", String.raw`(?:^|\n)\s*(?:rem\b|::)[^\n]*`], ["str", STR], ["var", String.raw`%[\w~]+%?|![\w]+!`], ["w", String.raw`[A-Za-z_]\w*`]],
    css: [["com", String.raw`\/\*[\s\S]*?\*\/`], ["str", STR], ["meta", "@[\\w-]+"], ["num", String.raw`#[0-9a-fA-F]{3,8}\b|-?\d*\.?\d+(?:px|em|rem|%|vh|vw|s|ms|deg|fr)?\b`],
      ["key", String.raw`[\w-]+(?=\s*:[^:{]*[;}\n])`]],
    json: [["key", String.raw`"(?:\\.|[^"\\\n])*"(?=\s*:)`], ["str", STR], ["num", "-?" + NUM], ["kw", String.raw`\b(?:true|false|null)\b`]],
    yaml: [["com", String.raw`(?:^|[ \t])[#;][^\n]*`], ["meta", String.raw`^\s*\[[^\]\n]+\]`], ["key", String.raw`^[ \t-]*[\w.-]+(?=\s*[:=])`], ["str", STR],
      ["num", NUM], ["kw", String.raw`\b(?:true|false|yes|no|on|off|null)\b`]],
    html: [["com", String.raw`<!--[\s\S]*?-->`], ["tag", String.raw`<\/?[A-Za-z][\w:-]*|\/?>`], ["attr", String.raw`[\w:-]+(?==)`], ["str", STR]],
  };
  const compiled = {};
  const rx = (fam) => compiled[fam] || (compiled[fam] = new RegExp(RULES[fam].map(([, s]) => `(${s})`).join("|"), "gm"));

  function guess(code) {
    const t = code.trimStart();
    if (/^(<!doctype html|<html|<svg|<\?xml|<[a-z]+[\s>])/i.test(t)) return "html";
    if (/^[[{]/.test(t)) { try { JSON.parse(code); return "json"; } catch (_) { /* 不是 JSON */ } }
    return "";
  }
  const family = (lang, code) => ALIAS[(lang || "").toLowerCase()] || guess(code || "");

  // 回 [[類別或空字串, 文字], …]
  function tokens(code, fam) {
    if (fam === "diff") {
      return (code.match(/[^\n]*\n|[^\n]+$/g) || []).map((ln) => [/^\+(?!\+\+)/.test(ln) ? "add" : /^-(?!--)/.test(ln) ? "del" : /^(@@|diff |index |\+\+\+|---)/.test(ln) ? "meta" : "", ln]);
    }
    if (fam === "html") return htmlTokens(code);
    if (!RULES[fam]) return [["", code]];
    const out = [];
    const re = rx(fam);
    const kinds = RULES[fam].map(([k]) => k);
    const kw = KW[fam === "css" || fam === "json" || fam === "yaml" ? "" : fam];
    const ci = fam === "sql" || fam === "bat";
    let last = 0, m;
    re.lastIndex = 0;
    while ((m = re.exec(code))) {
      if (!m[0]) { re.lastIndex++; continue; }
      if (m.index > last) out.push(["", code.slice(last, m.index)]);
      const k = kinds[m.slice(1).findIndex((x) => x !== undefined)];
      let cls = k;
      if (k === "w") {
        const w = m[0];
        cls = kw && kw.has(ci ? w.toLowerCase() : w) ? "kw" : /^\s*\(/.test(code.slice(re.lastIndex, re.lastIndex + 3)) ? "fn"
          : /^[A-Z][a-z]\w*$/.test(w) && fam !== "sh" ? "type" : "";
      }
      out.push([cls, m[0]]);
      last = re.lastIndex;
    }
    if (last < code.length) out.push(["", code.slice(last)]);
    return out;
  }

  // HTML：標籤與屬性另外上色；<script>、<style> 裡面照 JS、CSS 上色
  function htmlTokens(code) {
    const out = [];
    const re = /(<(script|style)\b[^>]*>)([\s\S]*?)(<\/\2\s*>)/gi;
    let last = 0, m;
    const base = (s) => tokensRaw(s, "html").forEach((t) => out.push(t));
    while ((m = re.exec(code))) {
      if (m.index > last) base(code.slice(last, m.index));
      base(m[1]);
      tokens(m[3], m[2].toLowerCase() === "script" ? "js" : "css").forEach((t) => out.push(t));
      base(m[4]);
      last = re.lastIndex;
    }
    if (last < code.length) base(code.slice(last));
    return out;
  }
  function tokensRaw(code, fam) {
    const out = [];
    const re = rx(fam);
    const kinds = RULES[fam].map(([k]) => k);
    let last = 0, m;
    re.lastIndex = 0;
    while ((m = re.exec(code))) {
      if (!m[0]) { re.lastIndex++; continue; }
      if (m.index > last) out.push(["", code.slice(last, m.index)]);
      out.push([kinds[m.slice(1).findIndex((x) => x !== undefined)], m[0]]);
      last = re.lastIndex;
    }
    if (last < code.length) out.push(["", code.slice(last)]);
    return out;
  }

  const LIMIT = 200000;     // 太長的檔整段照純文字，免得一則回答把分頁卡住
  function paint(pre, code, lang) {
    const fam = family(lang, code);
    if (!fam || code.length > LIMIT) { pre.textContent = code; return; }
    const frag = document.createDocumentFragment();
    tokens(code, fam).forEach(([cls, text]) => {
      if (!cls) { frag.appendChild(document.createTextNode(text)); return; }
      const s = document.createElement("span");
      s.className = "t-" + cls;
      s.textContent = text;
      frag.appendChild(s);
    });
    pre.replaceChildren(frag);
  }

  return { paint, family, tokens };
})();
