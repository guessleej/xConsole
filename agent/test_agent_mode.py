#!/usr/bin/env python3
"""Agent 模式（代理端）的自我檢查：預設關閉、路徑跳不出工作資料夾（含符號連結）、讀寫上限、
指令的工作目錄與逾時、輸出截斷、環境變數裡的權杖與金鑰被拿掉，以及經 HTTP 打得到這幾支。
跑法：python3 test_agent_mode.py（只用標準函式庫，Linux／macOS／Windows 都能跑）
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-agentmode-")
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OK = True
WIN = os.name == "nt"


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def enable(on):
    with A.REG_LOCK:
        reg = A.registry()
        if on:
            reg["machine"]["agent_enabled"] = True
        else:
            reg["machine"].pop("agent_enabled", None)
        A.save_json(A.REGISTRY, reg)


def main():
    # ---- 1. 預設關閉
    code, out = A.agent_folders()
    case("預設關閉：列資料夾回 403 並講清楚去哪裡打開", code == 403 and out.get("disabled") and "這台的設定" in out["error"], out)
    case("預設關閉：執行指令也回 403", A.agent_exec("x", "echo hi")[0] == 403)
    case("預設關閉：寫檔也回 403", A.agent_write("x", "a.txt", "hi")[0] == 403)
    case("machine_info 回傳 agent_enabled（預設 false）", A.machine_info()["settings"]["agent_enabled"] is False)

    # ---- 2. 從「這台的設定」打開；沒帶就不動；型別要對
    code, out = A.save_machine({"memory_model": "discrete", "agent_enabled": True})
    case("save_machine 打開 Agent 模式", code == 200 and out["settings"]["agent_enabled"] is True, out)
    code, out = A.save_machine({"memory_model": "discrete", "note": "改備註"})
    case("只改別的設定：Agent 模式維持開著", code == 200 and out["settings"]["agent_enabled"] is True, out)
    case("agent_enabled 不是布林值 → 400", A.save_machine({"memory_model": "discrete", "agent_enabled": "yes"})[0] == 400)

    # ---- 3. 工作資料夾
    code, out = A.agent_folder_create("網站專案")
    case("建立工作資料夾（中文名稱）", code == 200, out)
    case("重複建立 → 409", A.agent_folder_create("網站專案")[0] == 409)
    for bad in ("../x", ".hidden", "a/b", "a\\b", "", "x" * 80, "結尾是點."):
        case(f"不合法的資料夾名稱「{bad[:12]}」→ 400", A.agent_folder_create(bad)[0] == 400)
    code, out = A.agent_folders()
    case("列資料夾：看得到剛建的，並回報作業系統", code == 200 and [f["name"] for f in out["folders"]] == ["網站專案"]
         and out["os"] == A.OS, out)
    root = os.path.realpath(A.AGENT_ROOT)
    proj = os.path.join(root, "網站專案")

    # ---- 4. 讀寫與路徑跳脫
    code, out = A.agent_write("網站專案", "src/index.html", "<h1>你好</h1>")
    case("寫檔：上層資料夾自動建立、回報是新建", code == 200 and out["created"] is True and os.path.isfile(os.path.join(proj, "src", "index.html")), out)
    code, out = A.agent_write("網站專案", "src/index.html", "<h1>改過</h1>")
    case("再寫一次：回報是修改", code == 200 and out["created"] is False)
    code, out = A.agent_read("網站專案", "src/index.html")
    case("讀回來的內容一致", code == 200 and out["content"] == "<h1>改過</h1>" and not out["binary"], out)
    outside = tempfile.mkdtemp(prefix="xc-outside-")
    outfile = os.path.join(outside, "secret.txt")
    with open(outfile, "w") as fh:
        fh.write("機密")
    for bad in ("../../secret.txt", "../網站專案/../../x", "/etc/passwd", outfile, "C:/Windows/win.ini", "a/../../b"):
        code_r, _ = A.agent_read("網站專案", bad)
        code_w, _ = A.agent_write("網站專案", bad, "x")
        case(f"路徑跳脫「{bad[:24]}」：讀寫都擋（400）", code_r == 400 and code_w == 400, (code_r, code_w))
    case("讀不存在的資料夾 → 404", A.agent_read("沒有這個", "a.txt")[0] == 404)
    if not WIN:
        os.symlink(outfile, os.path.join(proj, "link.txt"))
        os.symlink(outside, os.path.join(proj, "linkdir"))
        case("符號連結指到外面的檔案：讀擋下", A.agent_read("網站專案", "link.txt")[0] == 400)
        case("符號連結指到外面的檔案：寫擋下（不會寫到外面）", A.agent_write("網站專案", "link.txt", "x")[0] == 400
             and open(outfile).read() == "機密")
        case("經過指到外面的資料夾連結寫檔：擋下", A.agent_write("網站專案", "linkdir/new.txt", "x")[0] == 400
             and not os.path.exists(os.path.join(outside, "new.txt")))
        os.symlink(outside, os.path.join(root, "壞資料夾"))
        case("工作資料夾本身是指到外面的連結：擋下", A.agent_read("壞資料夾", "secret.txt")[0] in (400, 404))
        case("列資料夾不會列出指到外面的連結", "壞資料夾" not in [f["name"] for f in A.agent_folders()[1]["folders"]])
        code, out = A.agent_list("網站專案")
        case("列檔案：連結只列名字、不跟進去", code == 200 and not any(r["path"].startswith("linkdir/") for r in out["files"]), out)

    # ---- 5. 大小上限
    big = "a" * (A.AGENT_READ_MAX + 10)
    with open(os.path.join(proj, "big.txt"), "w") as fh:
        fh.write(big)
    code, out = A.agent_read("網站專案", "big.txt")
    case("讀檔上限 1 MB：多的截掉並標 truncated", code == 200 and len(out["content"]) == A.AGENT_READ_MAX and out["truncated"])
    code, out = A.agent_write("網站專案", "huge.txt", "b" * (A.AGENT_WRITE_MAX + 1))
    case("寫檔上限 2 MB：超過回 400", code == 400 and not os.path.exists(os.path.join(proj, "huge.txt")), out)
    case("寫檔內容不是文字 → 400", A.agent_write("網站專案", "x.txt", {"a": 1})[0] == 400)

    # ---- 6. 執行指令
    py = f'"{sys.executable}"' if not WIN else f"& '{sys.executable}'"
    code, out = A.agent_exec("網站專案", f"{py} -c \"import os;print(os.getcwd())\"")
    case("指令的工作目錄是該資料夾", code == 200 and out["code"] == 0
         and os.path.realpath(out["stdout"].strip()) == os.path.realpath(proj), out)
    code, out = A.agent_exec("網站專案", f"{py} -c \"import sys;sys.exit(3)\"")
    case("回傳結束碼", code == 200 and out["code"] == 3, out)
    t0 = time.time()
    code, out = A.agent_exec("網站專案", f"{py} -c \"import time;time.sleep(30)\"", timeout=1)
    case("逾時就收掉整個行程，回報 timed_out", code == 200 and out["timed_out"] and time.time() - t0 < 20, out)
    code, out = A.agent_exec("網站專案", "echo hi", timeout=99999)
    case("逾時上限 600 秒", code == 200 and out["timeout"] == 600, out.get("timeout"))
    code, out = A.agent_exec("網站專案", f"{py} -c \"print('x'*200000)\"")
    case("輸出截在 32 KB 以內、標 truncated、回報原本多大",
         code == 200 and len(out["stdout"].encode()) <= A.AGENT_OUT_MAX + 200 and out["truncated"]
         and out["stdout_bytes"] >= 200000 and "中間省略" in out["stdout"], (len(out.get("stdout", "")), out.get("stdout_bytes")))
    os.environ.update({"OPENAI_API_KEY": "sk" + "-should-not-leak", "HF_TOKEN": "hf_should_not_leak",
                       "MY_CLIENT_SECRET": "s3cr3t", "COPY_OF_AGENT": A.TOKEN, "XCONSOLE_HOME_X": "x",
                       "SAFE_VALUE": "ok-to-see"})
    code, out = A.agent_exec("網站專案", f"{py} -c \"import os,json;print(json.dumps(dict(os.environ)))\"")
    env = json.loads(out["stdout"]) if code == 200 and out["code"] == 0 else {}
    case("環境變數：*_KEY、*_TOKEN、*_SECRET 與代理權杖都拿掉",
         env and not any(k in env for k in ("OPENAI_API_KEY", "HF_TOKEN", "MY_CLIENT_SECRET", "COPY_OF_AGENT", "XCONSOLE_HOME_X"))
         and A.TOKEN not in out["stdout"], [k for k in ("OPENAI_API_KEY", "HF_TOKEN", "MY_CLIENT_SECRET", "COPY_OF_AGENT") if k in env])
    case("  一般的環境變數照樣在", env.get("SAFE_VALUE") == "ok-to-see")
    case("空指令 → 400", A.agent_exec("網站專案", "   ")[0] == 400)
    case("指令在不存在的資料夾 → 404", A.agent_exec("沒有這個", "echo hi")[0] == 404)
    if not WIN:
        case("Linux 用 bash -lc、macOS 用 zsh -lc", A.agent_shell("x")[:2] == (["/bin/zsh", "-lc"] if A.OS == "darwin" else ["bash", "-lc"]))

    # ---- 7. 經 HTTP：要權杖、路由都在
    srv = ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, token=A.TOKEN):
        r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                   method="POST" if body is not None else "GET",
                                   headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=30) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    case("沒有權杖打不進去", call("/agent/folders", token="wrong")[0] == 401)
    st, out = call("/agent/folders")
    case("GET /agent/folders", st == 200 and out["enabled"], out)
    st, out = call("/agent/fs/write", {"folder": "網站專案", "path": "http.txt", "content": "經 HTTP"})
    case("POST /agent/fs/write", st == 200 and out["created"], out)
    st, out = call("/agent/fs/read", {"folder": "網站專案", "path": "http.txt"})
    case("POST /agent/fs/read", st == 200 and out["content"] == "經 HTTP", out)
    st, out = call("/agent/fs/list", {"folder": "網站專案"})
    case("POST /agent/fs/list", st == 200 and any(r["path"] == "http.txt" for r in out["files"]), out)
    st, out = call("/agent/exec", {"folder": "網站專案", "cmd": "echo hi", "timeout": 10})
    case("POST /agent/exec", st == 200 and out["code"] == 0 and "hi" in out["stdout"], out)
    st, out = call("/agent/folders/create", {"name": "第二個"})
    case("POST /agent/folders/create", st == 200, out)
    enable(False)
    st, out = call("/agent/exec", {"folder": "網站專案", "cmd": "echo hi"})
    case("關掉之後經 HTTP 也打不進去（403）", st == 403, out)
    srv.shutdown()

    print("\n全部通過" if OK else "\n有項目失敗")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
