// 對話回答的 Markdown 排版自我檢查（mdBlock／mdInline）：跑法 node console/test_markdown.js
// 最小 DOM：只夠 mdBlock／mdInline 用，輸出成類 HTML 字串好比對
class N { constructor(t){this.tag=t;this.kids=[];this.attrs={};} appendChild(c){this.kids.push(c);c.parent=this;return c;} get lastChild(){return this.kids[this.kids.length-1];}
  set textContent(v){this.kids=[new T(v)];} set href(v){this.attrs.href=v;} set target(v){} set rel(v){} set start(v){this.attrs.start=v;}
  html(){ return `<${this.tag}${this.attrs.href?` href="${this.attrs.href}"`:""}>`+this.kids.map(k=>k.html()).join("")+`</${this.tag}>`; } }
class T { constructor(v){this.v=v;} html(){return this.v.replace(/</g,"&lt;");} }
global.document = { createElement:(t)=>new N(t), createTextNode:(v)=>new T(v) };
global.el = (t, cls, text) => { const n=new N(t); if (text!=null) n.textContent=text; return n; };
const src = require("fs").readFileSync(require("path").join(__dirname, "static", "pages.js"), "utf8");
const a = src.indexOf("function mdBlock"), b = src.indexOf("function codeBlock");
eval(src.slice(a, b) + "; global.mdBlock = mdBlock;");
const cases = [
 ["## 💡 第一部分：核心概念", "<h4>"],
 ["*   **優點：** 結構簡單", "<ul><li><strong>優點：</strong> 結構簡單</li></ul>"],
 ["1. 第一\n2. 第二", "<ol>"],
 ["| 項目 | 說明 |\n|---|---|\n| A | **粗** |", "<table><thead><tr><th>項目</th><th>說明</th></tr></thead><tbody><tr><td>A</td><td><strong>粗</strong></td></tr></tbody></table>"],
 ["用 `llama-server` 跑", "<code>llama-server</code>"],
 ["這是 *斜體* 與 a*b*c 不變", "<em>斜體</em>"],
 ["[點我](javascript:alert(1)) 與 [官網](https://x.com/a)", '<a href="https://x.com/a">官網</a>'],
 ["<script>alert(1)</script>", "&lt;script>"],
 ["---", "<hr>"],
 ["> 引用一段", "<blockquote>"],
];
let ok = true;
for (const [md, want] of cases) { const h = mdBlock(md).html(); const pass = h.includes(want); ok = ok && pass; console.log(pass ? "[通過]" : "[失敗]", JSON.stringify(md).slice(0,40), pass ? "" : h); }
const h = mdBlock("[點我](javascript:alert(1))").html();
console.log(!h.includes("href=\"javascript") ? "[通過] javascript: 連結不會變成可點" : "[失敗] " + h);
// Hugging Face 網址或名稱 → 作者/模型（模型頁可以直接貼網址）
const app = require("fs").readFileSync(require("path").join(__dirname, "static", "app.js"), "utf8");
eval(app.slice(app.indexOf("function hfRepoOf"), app.indexOf("const SETTINGS_TABS")) + "; global.hfRepoOf = hfRepoOf;");
for (const [t, w] of [["https://huggingface.co/google/gemma-3-4b-it", "google/gemma-3-4b-it"],
  ["huggingface.co/unsloth/gemma-4-E4B-it-GGUF/tree/main", "unsloth/gemma-4-E4B-it-GGUF"], ["https://hf.co/a/b?x=1", "a/b"],
  ["unsloth/gpt-oss-20b-GGUF", "unsloth/gpt-oss-20b-GGUF"], ["gemma 4", null], ["https://evil.com/a/b", null]]) {
  const g = hfRepoOf(t); const pass = g === w; ok = ok && pass;
  console.log(pass ? "[通過]" : "[失敗]", "HF 網址", t, pass ? "" : g);
}
console.log(ok ? "全部通過" : "有項目失敗"); process.exit(ok ? 0 : 1);
