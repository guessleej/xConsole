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
SLOT = {"id": "brain", "role": "主腦", "tier": "一般", "unit": "xconsole-llama-brain", "kind": "llama", "busy": True,
        "model": "UI-Test-Model", "quant": "Q4_K_M", "config": {"max_context": 32768}, "port": 18080}
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
