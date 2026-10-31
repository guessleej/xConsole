#!/usr/bin/env python3
"""跨平台自我檢查（Linux／macOS／Windows 都要跑，CI 三個系統各跑一次）。
硬體讀數合不合理，以及沒有 systemd 時代理自己看管模型服務的整套行為：帶起來、掛了自動重開、停掉、
代理重開後認回還在跑的、不重複帶起第二份。假的 llama-server 是一支 Python 模組，聽在指定的埠回 /v1/models。
跑法：python test_platform.py
"""
import json
import os
import socket
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp()
os.environ["XCONSOLE_SUPERVISOR"] = "1"          # Linux 上也走代理自己看管的那條路，三個系統測同一段程式
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}{'（' + str(detail) + '）' if detail != '' else ''}")


FAKE = '''import json, socket, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps({"data": [{"id": "fake"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def models_ok(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def wait(fn, secs):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.5)
    return False


def main():
    print(f"系統：{A.OS}　Python {sys.version.split()[0]}")
    # ---- 硬體讀數
    m = A.mem_stats()
    case("系統記憶體總量讀得到", m["total"] > 1 << 30, round(m["total"] / A.GIB, 1))
    case("可用量不超過總量、不是 0", 0 < m["avail"] <= m["total"], round(m["avail"] / A.GIB, 1))
    case("處理器型號讀得到", bool(A.cpu_name()), A.cpu_name())
    load, up = A.load_and_uptime()
    case("開機時間讀得到", up and up > 0, up and round(up))
    st = A.storage_choices()
    case("至少一個可以當存放區的磁碟", bool(st), [x["mount"] for x in st])
    free, total = A.disk_free(A.HOME)
    case("剩餘空間讀得到", 0 < free <= total, f"{free}/{total} GiB")
    case("本機名稱含 127.0.0.1", "127.0.0.1" in A.local_names())
    case("記憶體架構有建議", A.suggest_memory_model()[0] in A.MEMORY_MODELS, A.suggest_memory_model()[0])
    hw = A.hardware()
    case("hardware() 回得出 CPU 與記憶體", hw["cpu"].get("cores") and hw["ram"]["total"], hw.get("_source"))
    with socket.socket() as ls:
        ls.bind(("127.0.0.1", 0))
        ls.listen()
        port = ls.getsockname()[1]
        case("聽著的埠列得出來", any(p["port"] == port for p in A.ports()), port)
    root, err = A.check_root(os.path.join(A.HOME, "models", "gguf"))
    case("家目錄底下可以當存放區", err is None, err or root)
    case("系統以外的路徑擋下", A.check_root(os.path.join(tempfile.gettempdir(), "..", "..", "x"))[1] is not None)

    # ---- 代理自己看管模型服務（Supervisor）
    mod_dir = tempfile.mkdtemp()
    with open(os.path.join(mod_dir, "fakellama.py"), "w", encoding="utf-8") as fh:
        fh.write(FAKE)
    reg = A.registry()
    reg["machine"]["llama_env"] = {"PYTHONPATH": mod_dir}
    A.save_json(A.REGISTRY, reg)
    port = free_port()
    cfg = A.engine_file("t1", "-llama.json")
    os.makedirs(A.ENGINE_DIR, exist_ok=True)
    # llama_argv 組出「exe -m 模型 …」：exe 用這支 Python、模型名稱用模組名，就變成 python -m fakellama --port N
    A.save_json(cfg, {"engine": "llama", "exe": sys.executable, "model": "fakellama", "port": port, "ctx": 4096,
                      "parallel": 1, "alias": "fake", "mmap": True})
    unit = A.ensure_llama_unit("t1", cfg)
    case("沒有 systemd：服務定義存在 units.json", unit in A.SUP._meta(), unit)
    case("還沒啟動是 inactive", A.unit_state(unit) == "inactive", A.unit_state(unit))
    A.sysctl(unit, "enable")
    rc, _, err = A.sysctl(unit, "start")
    case("啟動", rc == 0, err)
    case("埠回得出 /v1/models", wait(lambda: models_ok(port), 20))
    case("狀態 active", A.unit_state(unit) == "active")
    case("is-enabled", A.unit_enabled(unit))
    case("記憶體用量讀得到", A.unit_rss_gib(unit) > 0, A.unit_rss_gib(unit))
    pid1 = int(A.sysctl(unit, "show -p MainPID --value")[1])
    A.kill_tree(pid1)
    case("被砍掉後先進入重開等待（activating）", wait(lambda: A.unit_state(unit) == "activating", 10), A.unit_state(unit))
    case("十秒左右自動重開", wait(lambda: A.unit_state(unit) == "active" and models_ok(port), 25))
    pid2 = int(A.sysctl(unit, "show -p MainPID --value")[1])
    case("重開後是新的行程", pid2 and pid2 != pid1, f"{pid1} → {pid2}")
    ok, why = A.wait_healthy({"unit": unit, "port": port}, seconds=20)
    case("wait_healthy 認得出服務好了", ok, why)

    # 代理重開：舊的看管停掉（真的重開時舊代理整個不在了），新的 Supervisor 認回還在跑的，不帶起第二份
    A.SUP.running = False
    time.sleep(3.5)
    A.SUP = A.Supervisor()
    A.SUP.boot()
    case("代理重開後認回同一個行程", int(A.sysctl(unit, "show -p MainPID --value")[1]) == pid2)
    case("認回來的狀態是 active", A.unit_state(unit) == "active")
    A.kill_tree(pid2)
    case("認回來的被砍掉也會自動重開", wait(lambda: A.unit_state(unit) == "active" and models_ok(port), 30))
    case("記錄檔讀得到", A.do_logs.__name__ and os.path.exists(A.SUP.log_path(unit)))

    A.sysctl(unit, "stop")
    case("停掉後 inactive", wait(lambda: A.unit_state(unit) == "inactive", 10), A.unit_state(unit))
    case("停掉後埠也關了", wait(lambda: not models_ok(port), 10))
    A.sysctl(unit, "disable")
    case("disable", not A.unit_enabled(unit))
    # 起不來的服務：一分鐘內掛五次就標成失敗，不無限重開
    bad = A.engine_file("t2", "-llama.json")
    A.save_json(bad, {"engine": "llama", "exe": sys.executable, "model": "no_such_module_xc", "port": free_port(),
                      "ctx": 4096, "parallel": 1, "alias": "bad", "mmap": True})
    u2 = A.ensure_llama_unit("t2", bad)
    A.SUP.RESTART_SEC = 1
    A.sysctl(u2, "start")
    case("一直起不來會停在 failed", wait(lambda: A.unit_state(u2) == "failed", 60), A.unit_state(u2))
    case("SubState 回報 failed", A.sysctl(u2, "show -p SubState --value")[1] == "failed")

    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
