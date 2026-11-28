// 對話頁對齊LocModel 3.6.2（比對5.2每天#4、#5、#15～#17）：跑法 node console/test_chat_parity.js
// 實際執行 chat.js 的產圖判斷與 highlight.js，不是只掃字串
const fs = require("fs");
const path = require("path");
const read = (f) => fs.readFileSync(path.join(__dirname, "static", f), "utf8");
const chat = read("chat.js");
eval(chat.slice(chat.indexOf("const GEN_CMD"), chat.indexOf("async function genImageUrl"))
  + "; global.detectGen = detectGen; global.genOptions = genOptions; global.lastGenPrompt = lastGenPrompt; global.lastGenImage = lastGenImage;");
eval(read("highlight.js") + "; global.HL = HL;");

let ok = true;
const check = (name, cond, detail) => { ok = ok && !!cond; console.log(cond ? "[通過]" : "[失敗]", name, cond ? "" : JSON.stringify(detail)); };

// ---- 不依文字猜：沒按畫圖、沒打斜線指令就是一般對話
check("一般對話裡說「畫一張流程圖」不會產圖", detectGen("畫一張系統流程圖", false, "chat") === null);
check("  說「讓這個柱狀圖動起來」也不會做影片", detectGen("用HTML讓這個柱狀圖動起來", false, "chat") === null);
check("  說「畫一隻貓」也要先按「畫圖」", detectGen("畫一隻貓", false, "chat") === null);
let g = detectGen("/畫 一隻貓在窗邊", false, "chat");
check("斜線指令 /畫：產圖，送給模型的文字拿掉指令", g && g.kind === "image" && !g.edit && g.text === "一隻貓在窗邊", g);
g = detectGen("/draw a cat", false, "chat");
check("  英文 /draw 也認", g && g.kind === "image" && g.text === "a cat", g);
g = detectGen("/影片 海浪拍打礁石", false, "chat");
check("  /影片 做影片", g && g.kind === "video" && g.text === "海浪拍打礁石", g);
g = detectGen("/video waves", false, "chat");
check("  /video 也認", g && g.kind === "video", g);
check("  /畫圖不是指令（要空格或結尾）", detectGen("/畫圖教學", false, "chat") === null);
g = detectGen("一隻貓，畫4張", false, "image");
check("按了「畫圖」每句都產圖，張數照講的", g && g.kind === "image" && g.count === 4, g);
g = detectGen("把背景換成海邊", true, "image");
check("  附了圖就是改圖", g && g.edit && !g.last, g);

// ---- 接著上一張改
g = detectGen("把背景換成海邊", false, "image", true);
check("剛畫完一張，說「把背景換成海邊」就改上一張", g && g.edit && g.last && g.count === 1, g);
g = detectGen("再畫一張", false, "image", true);
check("  說「再畫一張」是重畫，不是改上一張", g && !g.edit, g);
g = detectGen("這張圖的背景是什麼？", false, "image", true);
check("  問圖片內容不算改圖", g && !g.edit, g);
g = detectGen("/改圖 加上一頂帽子", false, "chat", true);
check("  /改圖 一定是改上一張", g && g.edit && g.last && g.text === "加上一頂帽子", g);
g = detectGen("把背景換成海邊", false, "image", false);
check("  上一個回答不是圖就照常畫新的", g && !g.edit, g);

const img = (job, state = "done") => ({ role: "assistant", content: "", gen: { kind: "image", node: "n1", job, state, prompt: "p-" + job } });
check("找上一張圖：上一個回答是畫好的圖", lastGenImage({ msgs: [{ role: "user" }, img("j1")] })?.job === "j1");
check("  中間隔了一般回答就不算", lastGenImage({ msgs: [img("j1"), { role: "user" }, { role: "assistant", content: "好的" }] }) === null);
check("  沒畫完的不算", lastGenImage({ msgs: [img("j2", "failed")] }) === null);
check("上一次的提示詞：同種類、畫好的最近一次", lastGenPrompt({ msgs: [img("j1"), img("j2"), img("j3", "failed")] }, "image") === "p-j2");
check("  影片不會拿畫圖的提示詞", lastGenPrompt({ msgs: [img("j1")] }, "video") === "");

// ---- 從一句話猜尺寸與長度
const D = { width: 1024, height: 1024 };
let o = genOptions("image", "一張手機桌布，直式", D);
check("說「直式」：9:16，像素總量跟預設差不多", o.height > o.width && Math.abs(o.width * o.height - 1024 * 1024) / (1024 * 1024) < 0.05 && o.width % 16 === 0, o);
o = genOptions("image", "做一張 16:9 的 banner", D);
check("  16:9", Math.abs(o.width / o.height - 16 / 9) < 0.03, o);
o = genOptions("image", "活動海報", D);
check("  海報是3:4", Math.abs(o.width / o.height - 0.75) < 0.03, o);
o = genOptions("image", "一張貓", D);
check("  沒講比例就用模型預設", o.width === 1024 && o.height === 1024, o);
o = genOptions("video", "5秒，直式", { width: 768, height: 512, fps: 24, frame_step: 8, frames: 33 });
check("影片說「5秒」：LTX 8n+1 格；說「直式」就直的", o.frames === 121 && (o.frames - 1) % 8 === 0 && o.height > o.width, o);
o = genOptions("video", "3秒", { width: 832, height: 480, fps: 16, frame_step: 4, frames: 33 });
check("  Wan 4n+1 格", o.frames === 49 && (o.frames - 1) % 4 === 0, o);
o = genOptions("video", "60秒", { width: 832, height: 480, fps: 16, frame_step: 4 });
check("  太長就到上限，不超過畫圖頁的選項", o.frames === 97, o);
o = genOptions("video", "海浪", { width: 768, height: 512, fps: 24, frames: 49 });
check("  沒講秒數用模型預設長度", o.frames === 49, o);

// ---- 語法高亮
const kinds = (code, lang) => HL.tokens(code, HL.family(lang, code)).filter(([k]) => k).map(([k, t]) => `${k}:${t}`);
check("Python：關鍵字、函式、字串、註解", ["kw:def", "fn:add", "com:# x", "str:\"s\""].every((x) => kinds("def add():\n  # x\n  return \"s\"", "python").includes(x)));
check("JavaScript的樣板字串", kinds("const a = `t${1}`;", "js").includes("kw:const") && kinds("const a = `t`;", "js").includes("str:`t`"));
check("SQL不分大小寫", kinds("select id from t", "sql").includes("kw:select"));
check("沒標語言：自動認HTML，script裡照JS上色", kinds("<div><script>let x = 1;</script></div>", "").includes("kw:let"));
check("沒標語言：自動認JSON", kinds('{"a": 1}', "").includes('key:"a"'));
check("diff的加減行", kinds("+a\n-b\n", "diff").join() === "add:+a\n,del:-b\n");
check("認不得的語言照純文字", HL.family("brainfuck", "+++") === "");
check("上色只用textContent組span，不碰innerHTML", !/innerHTML|insertAdjacentHTML|outerHTML/.test(read("highlight.js")));
check("不用後行斷言（舊版WebKit編不過）", !/\(\?<[=!]/.test(read("highlight.js")));

check("程式碼區塊不分主題都是深底：高亮字色只定義一組、限在區塊裡", /\.code-block \{ --hl-kw:/.test(read("style.css")) && !/:root[^{]*\{[^}]*--hl-kw/.test(read("style.css")));

// ---- 接線
const pages = read("pages.js");
check("程式碼區塊寫完才上色，SVG有預覽與下載", /if \(open\) pre\.textContent = code;\s*else HL\.paint\(pre, code, lang\);/.test(pages)
  && pages.includes('"預覽圖形"') && pages.includes("image/svg+xml"));
check("index.html 先載入 highlight.js 再載入 pages.js", (() => { const h = read("index.html"); return h.indexOf("highlight.js") > 0 && h.indexOf("highlight.js") < h.indexOf("pages.js"); })());
check("生成中顯示即時速度，答完換成正式統計；最近一次速度留給執行頁",
  chat.includes("reply.live = `${n} tokens・") && chat.includes("else if (!final && m.live)") && chat.includes("S.lastTps = { ...(S.lastTps || {})"));
check("對話頁的模型選單列出已下載的模型，選了就載入（只給維運與管理員）",
  chat.includes("if (canOperate() && ln?.online)") && chat.includes("o.value = `load:${m.id}`") && chat.includes('sel.value.startsWith("load:")'));
check("畫圖／影片模式送出後不自動關掉（每句都產圖，跟LocModel一樣）", !chat.includes('if (cur.mode !== "chat") { cur.mode = "chat"; syncMode(); }'));
check("改上一張圖要存來源，重新產生時拿得到原圖", chat.includes("base: { node: base.node, job: base.job }") && chat.includes("else if (m.gen.edit && m.gen.base)"));

console.log(ok ? "\n全部通過" : "\n有項目失敗");
process.exit(ok ? 0 : 1);
