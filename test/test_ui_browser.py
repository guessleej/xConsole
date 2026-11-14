#!/usr/bin/env python3
"""用真的瀏覽器走網頁（Playwright）：另起一個代理與主控台（各自的家目錄與埠，HTTPS 並驗證憑證），
登入後檢查只有在畫面上才看得出來的問題。代理回的資料用 page.route 補上測試要的欄位（模型欄位、API 資訊），
其餘照真的主控台走。不碰正式環境。
跑法：python3 test/test_ui_browser.py（要先 pip install playwright && python -m playwright install chromium）
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("沒有安裝 playwright，略過（pip install playwright && python -m playwright install chromium）")
    sys.exit(0 if os.environ.get("CI") is None else 1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = tempfile.mkdtemp(prefix="xc-ui-")
AH, CH = os.path.join(W, "agent"), os.path.join(W, "console")
PW = "ui-browser-2026"
OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not cond else ""))
    OK = OK and bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# 一個開著的 llama.cpp 主腦欄位：清單頁、概覽都靠它
# 模型名故意取長：概覽分層長條上的「現在執行…」標籤最容易把手機版撐寬（BUG-3）
SLOT = {"id": "brain", "role": "主腦", "tier": "一般", "unit": "xconsole-llama-brain", "kind": "llama", "busy": True,
        "model": "Mistral-Medium-3.5-128B-Instruct-UD-Q4_K_XL", "quant": "Q4_K_XL", "config": {"max_context": 32768},
        "port": 18080, "vram_gib": 40, "ram_gib": 0, "vram_exact": True}
INFO = {"slot": "brain", "role": "主腦", "model": "UI-Test-Model", "kind": "llama", "engine": "llama", "port": 18080,
        "public_url": "http://127.0.0.1:18080/v1", "default_url": "http://127.0.0.1:18080/v1", "public_url_custom": False,
        "model_name": "ui-test-model-q4", "online": True, "status": 200, "served": [], "max_context": 32768,
        "api_key_set": False, "dialects": ["openai"], "editable": True, "last_test": None, "run": None}


def start():
    ap, cp = free_port(), free_port()
    os.makedirs(AH)
    os.makedirs(CH)
    aenv = {**os.environ, "XCONSOLE_HOME": AH, "XCONSOLE_AGENT_PORT": str(ap), "XCONSOLE_SUPERVISOR": "1"}
    agent = subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py"), "--home", AH], env=aenv,
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    cert = os.path.join(AH, ".xconsole", "agent-cert.pem")
    for _ in range(150):
        if os.path.exists(cert) and os.path.exists(os.path.join(AH, ".xconsole", "token")):
            try:
                cx = ssl.create_default_context(cafile=cert)
                cx.minimum_version = ssl.TLSVersion.TLSv1_2
                cx.wrap_socket(socket.create_connection(("127.0.0.1", ap), 1), server_hostname="127.0.0.1").close()
                break
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.2)
    token = open(os.path.join(AH, ".xconsole", "token")).read().strip()
    cenv = {**os.environ, "XCONSOLE_PORT": str(cp)}
    srv = os.path.join(ROOT, "console", "xconsole_server.py")
    subprocess.run([sys.executable, srv, "--home", CH, "--set-credentials"], input=f"admin\n{PW}\n".encode(), env=cenv,
                   check=True, capture_output=True)
    fp = os.path.join(CH, ".xconsole", "fleet.json")
    cfg = json.load(open(fp))
    cfg["nodes"] = [{"id": "local", "name": "本機", "host": "127.0.0.1", "agent_port": ap, "token": token}]
    cfg.setdefault("tls", {})["hostnames"] = ["127.0.0.1"]
    json.dump(cfg, open(fp, "w"))
    os.makedirs(os.path.join(CH, ".xconsole", "trust"), exist_ok=True)
    shutil.copy(cert, os.path.join(CH, ".xconsole", "trust", "local.pem"))
    console = subprocess.Popen([sys.executable, srv, "--home", CH], env=cenv, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(150):
        try:
            ssl.get_server_certificate(("127.0.0.1", cp), timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return agent, console, f"https://127.0.0.1:{cp}"


def with_slot(route):
    """真的 /api/fleet 加上一個開著的主腦欄位。"""
    r = route.fetch()
    data = r.json()
    for n in data.get("nodes") or []:
        st = n.setdefault("state", {}) or {}
        n["state"] = st
        st["slots"] = [SLOT] + [s for s in st.get("slots") or [] if s.get("id") != "brain"]
    route.fulfill(response=r, body=json.dumps(data), headers={**r.headers, "content-type": "application/json"})


def login(pg, base):
    pg.goto(base + "/")
    pg.locator("#un").fill("admin")
    pg.locator("#pw").fill(PW)
    pg.locator(".login-submit").click()
    pg.wait_for_selector("#nav button", state="attached", timeout=30000)


def test_api_list(pg):
    """BUG-1：清單頁打精簡版 apiinfo（不算執行設定），回來之後表格要出現模型名稱。"""
    asked = []

    def info(route):
        asked.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(INFO))
    pg.route("**/api/node/*/apiinfo/brain**", info)
    pg.evaluate("go('apis')")
    try:
        pg.wait_for_selector("table.api-table td:has-text('ui-test-model-q4')", timeout=5000)
        shown = True
    except Exception:  # noqa: BLE001
        shown = False
    check("API 串接清單：apiinfo 回來後表格出現模型名稱", shown)
    check("  清單頁打的是精簡版（/brief，不算執行設定）", asked and all(u.endswith("/brief") for u in asked), asked)
    pg.unroute("**/api/node/*/apiinfo/brain**")


def test_mobile_width(ctx, base):
    """BUG-3：390 與 360 寬逐頁檢查，整頁不准比視窗寬（不准出現橫向捲動）。"""
    views = ["home", "models", "run", "node:local", "apis", "api:local:brain", "chat", "agent", "image", "video"]
    for w in (390, 360):
        pg = ctx.new_page()
        pg.set_viewport_size({"width": w, "height": 800})
        pg.route("**/api/fleet", with_slot)
        pg.route("**/api/node/*/apiinfo/brain**",
                 lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(INFO)))
        pg.goto(base + "/")
        pg.wait_for_selector("#nav button", state="attached", timeout=30000)
        tabs = pg.evaluate("settingsTabs().map(([k]) => k)")
        bad = []
        for v in views + [f"settings:{t}" for t in tabs]:
            if v.startswith("settings:"):
                pg.evaluate("(t) => { S.settingsTab = t; go('settings'); }", v.split(":", 1)[1])
            else:
                pg.evaluate("(v) => go(v)", v)
            pg.wait_for_timeout(1200)
            sw = pg.evaluate("document.documentElement.scrollWidth")
            if sw > w:
                bad.append(f"{v}={sw}")
        check(f"手機 {w} 寬：每一頁都不比視窗寬", not bad, "、".join(bad))
        pg.close()


def test_desktop_toggle(pg):
    """BUG-4：這台不能操作桌面時，「操作電腦」核取方塊要停用，不是只用橘字說明。"""
    def machine(route):
        r = route.fetch()
        data = r.json()
        data["desktop"] = {"how": None, "why": "這台沒有圖形桌面（沒有 X11 或 Wayland 工作階段）"}
        data.setdefault("settings", {})["desktop_enabled"] = False
        route.fulfill(response=r, body=json.dumps(data), headers={**r.headers, "content-type": "application/json"})
    pg.route("**/api/node/local/machine", machine)
    pg.evaluate("openMachineSettings('local')")
    box = pg.locator(".modal label.check-row:has-text('操作這台的桌面') input[type=checkbox]")
    box.wait_for(state="attached", timeout=15000)
    check("不能操作桌面：「操作電腦」核取方塊停用", box.is_disabled())
    pg.locator(".modal .x, .modal button:has-text('取消')").first.click()
    pg.unroute("**/api/node/local/machine")


def test_home_quiet(pg):
    """BUG-8：概覽每 15 秒自動更新時，主視覺區（含漂移光暈）不准整塊換新，只換有變的數字。"""
    pg.evaluate("go('home')")
    pg.wait_for_selector("section.hero .hero-glow", state="attached", timeout=15000)
    pg.wait_for_timeout(1500)
    # 用 JS 屬性做記號，不用 data-*：屬性會改到 outerHTML，比對時就被當成有變
    pg.evaluate("""() => { document.querySelector('section.hero .hero-glow').__keep = 1;
                           document.querySelector('.hero h1').__keep = 1; }""")
    pg.evaluate("refresh()")
    pg.wait_for_timeout(1500)
    kept = pg.evaluate("() => [!!document.querySelector('section.hero .hero-glow').__keep, !!document.querySelector('.hero h1').__keep,"
                       " document.querySelectorAll('section.hero').length, document.querySelector('#live-text').textContent]")
    check("概覽自動更新：光暈與標題原封不動（動畫不重播）", kept[:3] == [True, True, 1], kept)
    check("  自動更新本身成功（不是重畫出錯被吞掉）", kept[3] == "即時", kept[3])
    # 有變的數字要真的換上去：把「現在執行」改成別顆再更新一次
    SLOT["model"] = "Changed-Model-For-Test"
    pg.evaluate("refresh()")
    pg.wait_for_timeout(1500)
    shown = pg.evaluate("() => [document.querySelector('.hero .tiles').textContent, !!document.querySelector('section.hero .hero-glow').__keep]")
    SLOT["model"] = "Mistral-Medium-3.5-128B-Instruct-UD-Q4_K_XL"
    check("  有變的數字會換上去，光暈照樣不動", "Changed-Model-For-Test" in shown[0] and shown[1], shown)
    # 留下來的元素要換上新的事件處理（改名按鈕抓著當時的機器資料，留舊的會用到改名前的名稱）
    # 卡片本身也要留著（進場動畫的 class 不能讓它每次都被當成不一樣而整張換掉）
    pg.evaluate("""() => { window.__fn = document.querySelector('.hero-edit').onclick;
                           document.querySelector('.hero-edit').__keep = 1;
                           const hw = document.querySelector('#main section.card.hw'); if (hw) hw.__keep = 1; }""")
    pg.evaluate("refresh()")
    pg.wait_for_timeout(1500)
    st = pg.evaluate("""() => { const b = document.querySelector('.hero-edit'), hw = document.querySelector('#main section.card.hw');
                                return [!!b.__keep, b.onclick !== window.__fn, hw ? !!hw.__keep : null]; }""")
    check("  留下來的按鈕換上新的 onclick（不會用到過期的資料）", st[0] and st[1], st)
    check("  內容沒變的卡片整張留著（進場動畫的 class 不影響比對）", st[2] is not False, st)
    # 只有屬性變（例：分層長條的寬度）、子元素結構一樣：屬性要跟著換
    got = pg.evaluate("""() => { const a = el('div', 'x'); a.appendChild(el('span', null, '1')); a.style.flex = '1 1 0';
                                 document.body.appendChild(a);
                                 const b = el('div', 'x'); b.appendChild(el('span', null, '2')); b.style.flex = '5 1 0';
                                 a.style.display = b.style.display = 'flex'; swapChanged(a, b);
                                 const r = [a.isConnected, getComputedStyle(a).flexGrow === '5', a.textContent]; a.remove(); return r; }""")
    check("  只有屬性變：留著的元素屬性也換成新的", got == [True, True, "2"], got)


UNTRANSLATED = """() => { const out = new Set();
  const w = document.createTreeWalker(document.querySelector('#app'), NodeFilter.SHOW_TEXT);
  let n; while ((n = w.nextNode())) {
    // 管理者取的名稱（機器「本機」、欄位「主腦」）是資料，照設計不翻；推薦清單的模型說明另案決定
    const t = n.nodeValue.replace(/本機|主腦/g, '').trim();
    if (!t || !/[\u3400-\u9fff]/.test(t)) continue;
    if (typeof I18N !== 'undefined' && I18N.data && I18N.data.has(t)) continue;   // 伺服器回的資料（金鑰名稱等），照設計不翻
    const p = n.parentElement;
    if (!p || p.closest(I18N_SKIP) || p.closest('[hidden]') || p.closest('.rec-row, .rec-list') || !p.offsetParent) continue;
    out.add(t.slice(0, 60)); }
  return [...out]; }"""


def test_gateway_ui(pg):
    """加密入口：在 API 串接頁產生金鑰（完整金鑰只顯示一次）、清單出現、撤銷；單格頁有加密網址、直連開關、範例切換。"""
    pg.route("**/api/node/*/apiinfo/brain**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(INFO)))
    pg.evaluate("go('apis')")
    pg.wait_for_selector("h2:has-text('加密入口與金鑰')", timeout=10000)
    url = pg.locator(".copyrow .v").first.text_content()
    check("API 串接頁最上面是加密入口：https 網址、/v1 結尾", url.startswith("https://") and url.endswith("/v1"), url)
    pg.locator("button:has-text('產生金鑰')").click()
    pg.locator(".modal input").first.fill("瀏覽器測試 王老師")
    pg.locator(".modal button:has-text('產生')").click()
    pg.wait_for_selector(".modal h3:has-text('新的金鑰')", timeout=10000)
    key = pg.locator(".modal .copyrow .v").nth(2).text_content()
    check("  產生金鑰：彈窗顯示完整金鑰一次", key.startswith("xck_") and len(key) == 47, key)
    pg.locator(".modal button:has-text('取消')").click()
    pg.wait_for_selector("td:has-text('瀏覽器測試 王老師')", timeout=10000)
    row = pg.locator("tr:has(td:has-text('瀏覽器測試 王老師'))")
    check("  清單出現這把：使用中、只顯示開頭", "使用中" in row.text_content() and key not in pg.content() and key[:8] in row.text_content())
    pg.evaluate("go('api:local:brain')")
    pg.wait_for_selector("h2:has-text('明文直連')", timeout=10000)
    pg.locator(".tabbar button:has-text('curl')").click()
    check("  單格頁的範例直接帶入剛產生的金鑰", key in pg.locator("pre.code").text_content())
    pg.evaluate("go('apis')")
    pg.wait_for_selector("tr:has(td:has-text('瀏覽器測試 王老師'))", timeout=10000)
    row = pg.locator("tr:has(td:has-text('瀏覽器測試 王老師'))")
    row.locator("button:has-text('撤銷')").click()
    pg.locator(".modal button.danger:has-text('撤銷')").click()
    pg.wait_for_selector("tr:has(td:has-text('瀏覽器測試 王老師')) td:has-text('已撤銷')", timeout=10000)
    check("  撤銷後狀態變成已撤銷、不再有撤銷按鈕",
          pg.locator("tr:has(td:has-text('瀏覽器測試 王老師')) button:has-text('撤銷')").count() == 0)
    pg.evaluate("go('api:local:brain')")
    pg.wait_for_selector("h2:has-text('明文直連')", timeout=10000)
    txt = pg.locator("#main").text_content()
    check("單格頁：加密入口網址（建議）與直連網址都在", "加密入口網址（建議）" in txt and "直連網址" in txt)
    check("  有「改成只走加密入口」按鈕", pg.locator("button:has-text('改成只走加密入口')").count() == 1)
    check("  直連本來沒金鑰、又沒有能用這格的個別金鑰：按鈕停用並說明要先發金鑰",
          pg.locator("button:has-text('改成只走加密入口')").is_disabled() and "還沒有任何一把能用這一格" in txt)
    code = pg.locator("pre.code").text_content()
    check("  撤銷之後範例不再帶入那把金鑰", key not in code and "<貼上金鑰>" in code, code[-200:])
    pg.evaluate("S.apiTab = 'copilot'")
    check("  工具設定範例預設用加密入口的網址", url in code, code[:120])
    pg.locator("button:has-text('直連')").first.click()
    pg.wait_for_selector("h2:has-text('明文直連')", timeout=10000)
    check("  切到直連：範例換成直連網址", INFO["public_url"] in pg.locator("pre.code").text_content())
    pg.evaluate("S.apiMode = 'gw'")
    pg.unroute("**/api/node/*/apiinfo/brain**")


def test_user_journey(pg, base):
    """多帳號：管理員在設定頁新增帳號拿到一次性密碼；使用者第一次登入只能改密碼，
    之後只看得到對話、Agent、畫圖、影片、API 金鑰、我的帳號，沒有管理員的按鈕，也不會打到被擋的 API。"""
    pg.evaluate("() => { S.settingsTab = 'users'; go('settings'); }")
    pg.wait_for_selector("h2:has-text('使用者帳號')", timeout=10000)
    pg.locator("button:has-text('新增帳號')").click()
    pg.locator(".modal input").nth(0).fill("s1101")
    pg.locator(".modal input").nth(1).fill("王小明")
    pg.locator(".modal button:has-text('新增')").click()
    pg.wait_for_selector(".modal h3:has-text('一次性密碼')", timeout=10000)
    otp = pg.locator(".modal td.mono").first.text_content()
    check("管理員新增帳號：彈窗顯示 16 碼一次性密碼", len(otp) == 16, otp)
    pg.locator(".modal button:has-text('取消')").click()
    check("  清單出現這個帳號，狀態是等他第一次登入改密碼", "等他第一次登入改密碼" in pg.locator("tr:has(td:has-text('s1101'))").text_content())

    u = pg.context.browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900}, locale="zh-TW").new_page()
    blocked, errs = [], []
    u.on("response", lambda r: blocked.append(r.url) if r.status == 403 and "/agent/folders" not in r.url else None)
    u.on("pageerror", lambda e: errs.append(str(e)))
    u.route("**/api/fleet", with_slot)
    u.goto(base + "/")
    u.locator("#un").fill("s1101")
    u.locator("#pw").fill(otp)
    u.locator(".login-submit").click()
    u.wait_for_selector("h2:has-text('變更密碼')", timeout=15000)
    check("使用者第一次登入：只看到改密碼，導覽列是空的", u.locator("#nav button").count() == 0)
    ins = u.locator("#main input[type=password]")
    ins.nth(0).fill(otp)
    ins.nth(1).fill("student-pass-2026")
    ins.nth(2).fill("student-pass-2026")
    u.locator("#main button:has-text('變更密碼')").click()
    u.wait_for_selector("#login:not([hidden])", timeout=10000)
    u.locator("#un").fill("s1101")
    u.locator("#pw").fill("student-pass-2026")
    u.locator(".login-submit").click()
    u.wait_for_selector("#nav button", state="attached", timeout=15000)
    nav = [b.text_content() for b in u.locator("#nav button").all()]
    check("  改完密碼重新登入：導覽列只有使用者用得到的六項", nav == ["對話", "Agent", "畫圖", "影片", "API 金鑰", "我的帳號"], nav)
    for v in ("chat", "agent", "image", "video", "mykeys", "account"):
        u.evaluate("(v) => go(v)", v)
        u.wait_for_timeout(1500)
    txt = u.locator("#app").text_content()
    check("  沒有管理員的按鈕（操作電腦、下載模型、修改網址）",
          not any(t in txt for t in ("操作電腦", "下載畫圖模型", "修改網址")))
    check("  走完六頁沒有打到被擋的 API", not blocked, blocked[:5])
    check("  沒有 JavaScript 錯誤", not errs, errs[:3])
    u.context.close()


def test_english(ctx, base):
    """BUG-9：英文介面下，概覽、執行、設定各分頁不准留下介面中文（資料除外）。"""
    pg = ctx.new_page()
    pg.add_init_script("try { localStorage.setItem('xconsole-lang', 'en') } catch (e) {}")
    pg.route("**/api/fleet", with_slot)
    pg.goto(base + "/")
    pg.wait_for_selector("#nav button", state="attached", timeout=30000)
    left = {}
    pg.route("**/api/node/*/apiinfo/brain**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(INFO)))
    for v in ["home", "run", "apis", "api:local:brain", "settings:machines", "settings:engine", "settings:tls", "settings:report",
              "settings:users"]:
        if v.startswith("settings:"):
            pg.evaluate("(t) => { S.settingsTab = t; go('settings'); }", v.split(":", 1)[1])
        else:
            pg.evaluate("(v) => go(v)", v)
        pg.wait_for_timeout(2500)
        got = pg.evaluate(UNTRANSLATED)
        if got:
            left[v] = got
    check("英文介面：概覽、執行與設定各分頁沒有沒翻到的介面字", not left, left)
    # 分開翻譯的兩段接在一起，中間要有空格（不能變成「first.The」「yet(complete」）
    glued = []
    for tab in ("machines", "tls"):
        pg.evaluate("(t) => { S.settingsTab = t; go('settings'); }", tab)
        pg.wait_for_timeout(2000)
        glued += pg.evaluate("() => [...document.querySelectorAll('#main p, #main .st')].map(e => e.textContent)"
                             ".filter(t => /[a-z][.)][A-Z]|[a-z]\\(/.test(t)).slice(0, 3)")
    check("  分開翻譯的兩段中間有空格", not glued, glued)
    pg.close()


def main():
    agent, console, base = start()
    errs = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900}, locale="zh-TW")
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.route("**/api/fleet", with_slot)
            login(pg, base)
            test_api_list(pg)
            test_gateway_ui(pg)
            test_user_journey(pg, base)
            test_desktop_toggle(pg)
            test_home_quiet(pg)
            test_mobile_width(ctx, base)
            test_english(ctx, base)
            b.close()
        check("瀏覽器沒有 JavaScript 錯誤", not errs, errs[:3])
    finally:
        for proc in (console, agent):
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(W, ignore_errors=True)
    print("全部通過" if OK else "有失敗")
    sys.exit(0 if OK else 1)


if __name__ == "__main__":
    main()
