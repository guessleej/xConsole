"""排版對齊量測（交付前跑）：並排卡片頂底邊、表格填滿、整頁左右緣、卡片內容貼齊內緣、區塊間距。
跑法（在裝了 playwright 的機器上）：python3 scripts/check_align.py；任何一行出現「沒對齊／沒貼齊／不一致」就要修。
"""
import getpass
import os

from playwright.sync_api import sync_playwright

BASE = os.environ.get("XC_URL", "https://127.0.0.1:9400")
USER = os.environ.get("XC_USER", "admin")
PASS = os.environ.get("XC_PASS") or getpass.getpass("主控台密碼：")
JS = """() => {
  const out = [], r = (e) => e.getBoundingClientRect(), near = (a, b, t = 1.5) => Math.abs(a - b) <= t;
  // 1. 並排的卡片：頂邊、底邊要一致
  document.querySelectorAll('main .grid.pair').forEach((g, i) => {
    const cs = [...g.children].map(r), t = cs.map(x => Math.round(x.top)), b = cs.map(x => Math.round(x.bottom));
    out.push(`並排卡片#${i}　頂邊 ${t.join('/')}　底邊 ${b.join('/')}　${t.every(v => near(v, t[0])) && b.every(v => near(v, b[0])) ? '對齊' : '沒對齊'}`);
  });
  // 2. 表格要填滿外層捲動框
  document.querySelectorAll('main .scrollbox').forEach((s, i) => {
    const t = s.querySelector('table'); if (!t) return;
    const ok = near(r(t).width, s.clientWidth, 2);
    out.push(`表格#${i}　表格寬 ${Math.round(r(t).width)}　捲動框內寬 ${s.clientWidth}　${ok ? '對齊' : '沒對齊'}`);
  });
  // 3. 整頁的卡片：左右緣要一致（並排的那一列看整列外框）
  const rows = [...document.querySelectorAll('main > .wrap > *')].map(r);
  const L = rows.map(x => Math.round(x.left)), R = rows.map(x => Math.round(x.right));
  out.push(`整頁區塊左緣 ${[...new Set(L)].join('/')}　右緣 ${[...new Set(R)].join('/')}　${L.every(v => near(v, L[0])) && R.every(v => near(v, R[0])) ? '對齊' : '沒對齊'}`);
  // 5. 卡片內容要貼齊卡片內緣：每張卡最右邊的數值右緣 = 卡片右緣 - 內距
  document.querySelectorAll('main .card').forEach((c, i) => {
    const vs = [...c.querySelectorAll('.kv .v, .bar, .scrollbox')].filter(v => v.closest('.card') === c && !v.closest('.slot')); if (!vs.length) return;
    const inner = r(c).right - parseFloat(getComputedStyle(c).paddingRight);
    const maxR = Math.max(...vs.map(v => r(v).right));
    if (!near(maxR, inner, 2)) out.push(`卡片#${i} 內容右緣 ${Math.round(maxR)}　卡片內緣 ${Math.round(inner)}　沒貼齊`);
  });
  // 4. 區塊之間的上下間距要一致（不能有緊貼或多推一截的）
  const gaps = rows.slice(1).map((x, i) => Math.round(x.top - rows[i].bottom));
  out.push(`區塊間距 ${[...new Set(gaps)].join('/')}　${gaps.every(v => near(v, 16)) ? '一致' : '不一致'}`);
  return out.join('\\n');
}"""
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/home/xcloud/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome", args=["--no-sandbox"])
    for w, h in ((1280, 800), (1440, 900)):
        pg = b.new_context(viewport={"width": w, "height": h}, ignore_https_errors=True, reduced_motion="reduce").new_page()
        pg.goto(BASE + "/", wait_until="networkidle")
        pg.fill("#un", USER); pg.fill("#pw", PASS); pg.click("button[type=submit]")
        pg.wait_for_selector("#app:not([hidden])"); pg.wait_for_timeout(3500)
        for grp, node in (("AMD Strix Halo", ".26"), ("AMD Strix Halo", ".27"), ("Jetson / MIC-743", ".20")):
            pg.click(f"nav#nav >> text={grp}"); pg.wait_for_timeout(1500)
            pg.click(f"#subnav >> text={node}"); pg.wait_for_timeout(3500)
            print(f"===== {w} 寬　{node} =====")
            print(pg.evaluate(JS))
            if w == 1440 and node == ".20":
                pg.screenshot(path=os.environ.get("XC_SHOT", "/tmp/align-20.png"), full_page=True)
    b.close()
