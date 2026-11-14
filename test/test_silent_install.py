"""靜默安裝（第 7 項）：加入碼只收標準輸入或回應檔，指令列與環境變數一律不收。

一、install/join.py 對隔離的主控台＋真的代理：碼從標準輸入、指紋對才加入、指紋錯或沒給（自簽）都擋、碼格式不對擋。
二、install.sh／install.ps1：-JoinCode 參數一律拒絕並說明改法；大量部署參數沒配 --console 擋；不是 root 擋；
    回應檔權限不對會刪掉並要求作廢。
三、主控台產的安裝腳本：加入碼不再經過環境變數，改走標準輸入。
實際以 root／SYSTEM 裝成開機自啟、加入主控台，見 test/e2e_silent.py（CI 三個作業系統各跑一次）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "console"))
import test_joincodes as T  # noqa: E402  共用隔離主控台與代理的小工具

FAILS = []
W = tempfile.mkdtemp(prefix="xc-silent-")
T.W = W
PW = T.PW
JOIN = os.path.join(ROOT, "install", "join.py")
SH = os.path.join(ROOT, "install", "install.sh")


def check(name, cond, detail=""):
    print(("通過 " if cond else "失敗 ") + name + ("" if cond else f"：{str(detail)[:400]}"))
    if not cond:
        FAILS.append(name)


def run_join(args, code, home):
    return subprocess.run([sys.executable, JOIN, *args, "--home", home], input=code + "\n", capture_output=True, text=True, timeout=200)


def e2e():
    os.environ.pop("XCONSOLE_HOME", None)        # scripts() 為了 import 主控台設過；留著的話隔離主控台會跑去用那個資料夾
    procs = []
    try:
        ahome = os.path.join(W, "agent")
        os.makedirs(os.path.join(ahome, ".xconsole"))
        json.dump({"machine": {"name": "a", "memory_model": "discrete"}, "slots": []},
                  open(os.path.join(ahome, ".xconsole", "registry.json"), "w"))
        aport = T.free_port()
        procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "agent", "xconsole_agent.py")],
                                      env={**os.environ, "HOME": ahome, "XCONSOLE_AGENT_PORT": str(aport)},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("節點代理啟動", T.wait_tls(aport))
        cport = T.free_port()
        chome = os.path.join(W, "console")
        os.makedirs(os.path.join(chome, ".xconsole"), mode=0o700)
        cenv = {**os.environ, "HOME": chome, "XCONSOLE_PORT": str(cport), "XCONSOLE_LINK_PORT": str(T.free_port())}
        subprocess.run([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py"), "--set-credentials"],
                       input=f"admin\n{PW}\n".encode(), env=cenv, check=True, capture_output=True)
        procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py")], env=cenv,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        check("隔離主控台啟動", T.wait_tls(cport))
        ccert = os.path.join(chome, ".xconsole", "tls", "cert.pem")
        _, _, sc = T.https(cport, ccert, "POST", "/api/login", {"username": "admin", "password": PW})
        ck = {"Cookie": "xconsole=" + sc.split("xconsole=", 1)[1].split(";", 1)[0]}
        host = f"127.0.0.1:{cport}"
        st, jc = T.https(cport, ccert, "POST", "/api/nodes/join-code", {"uses": 2, "name": "S{1..2}", "console": host}, ck)[:2]
        sl = jc.get("silent") or {}
        check("產碼回傳大量部署用的加入碼、指紋與兩種指令", st == 200 and len(sl.get("fingerprint", "")) == 64
              and sl.get("code") and sl["code"] in jc["command"]
              and f"--console {host} --fingerprint {sl['fingerprint']} --join-code-file" in sl.get("unix", "")
              and f"-Console {host} -Fingerprint {sl['fingerprint']} -JoinCodeFile" in sl.get("windows", ""), jc)
        check("  指令裡沒有加入碼", sl["code"] not in sl["unix"] and sl["code"] not in sl["windows"])
        code, fp = sl["code"], sl["fingerprint"]
        common = ["--console", host, "--agent-port", str(aport)]

        r = run_join(common + ["--fingerprint", "00" * 32], code, ahome)
        check("指紋不對：不送出任何東西就斷線，說明可能連錯或換過憑證", r.returncode == 1 and "憑證對不上" in r.stderr, r)
        r = run_join(common, code, ahome)
        check("沒給指紋、主控台是自簽：一般驗證不過，說明要加指紋", r.returncode == 1 and "指紋" in r.stderr, r)
        r = run_join(common + ["--fingerprint", fp], "not-a-code", ahome)
        check("加入碼格式不對：擋下", r.returncode == 1 and "格式不對" in r.stderr, r)
        _, js = T.https(cport, ccert, "GET", f"/api/nodes/join-code/{jc['id']}", None, ck)[:2]
        check("  上面三次都沒用掉名額", js.get("used") == 0, js)
        fp_spaced = ":".join(fp[i:i + 2] for i in range(0, 64, 2)).lower()
        r = run_join(common + ["--fingerprint", fp_spaced], code, ahome)
        check("碼從標準輸入、指紋對（大小寫與冒號不拘）：加入成功，名稱照樣板", r.returncode == 0 and "已加入主控台：S1" in r.stdout, r)
        _, nodes = T.https(cport, ccert, "GET", "/api/nodes", None, ck)[:2]
        check("  主控台清單上看得到 S1", any(n["id"] == "S1" for n in nodes.get("nodes", [])), nodes)
    finally:
        for p in procs:
            p.terminate()


def installers():
    for args, word in ((["--join-code", "abc"], "不能放在指令列"), (["--join-code=abc"], "不能放在指令列"),
                       (["--fingerprint", "AA"], "要搭配 --console"), (["--bogus"], "不認得的參數")):
        r = subprocess.run(["bash", SH, *args], capture_output=True, text=True, timeout=30)
        check(f"install.sh {args[0]}：拒絕並說明", r.returncode == 1 and word in r.stderr, r.stderr)
    if os.geteuid() != 0:
        r = subprocess.run(["bash", SH, "--console", "192.0.2.10:9400", "--join-code-file", "-"],
                           input="x" * 32 + "\n", capture_output=True, text=True, timeout=30)
        check("install.sh 大量部署不是 root：擋下並說明", r.returncode == 1 and "要用 root" in r.stderr, r.stderr)
    # 回應檔權限：抽出 perm_ok 單獨跑（整支要 root 才會走到）
    body = open(SH, encoding="utf-8").read()
    fn = body[body.index("perm_ok() {"):body.index("managed() {")]
    f = os.path.join(W, "code.txt")
    open(f, "w").write("x" * 32)
    os.chmod(f, 0o644)
    osn = "Darwin" if sys.platform == "darwin" else "Linux"
    r = subprocess.run(["bash", "-c", f"OSN={osn}\n{fn}\nperm_ok \"$1\" && echo ok || echo bad", "_", f], capture_output=True, text=True)
    check("回應檔 644：判定不合格", r.stdout.strip() == "bad", r)
    os.chmod(f, 0o600)
    r = subprocess.run(["bash", "-c", f"OSN={osn}\n{fn}\nperm_ok \"$1\" && echo ok || echo bad", "_", f], capture_output=True, text=True)
    want = "ok" if os.geteuid() == 0 else "bad"
    check(f"回應檔 600：擁有者{'是' if want == 'ok' else '不是'} root 時判定{'合格' if want == 'ok' else '不合格'}", r.stdout.strip() == want, r)
    check("install.sh 讀完回應檔就刪", 'IFS= read -r CODE < "$CODEFILE" || true\n    rm -f "$CODEFILE"' in body)

    ps1 = open(os.path.join(ROOT, "install", "install.ps1"), encoding="utf-8-sig").read()
    check("install.ps1：-JoinCode 一律拒絕、讀完回應檔就刪、權限只准 SYSTEM 與 Administrators",
          "ContainsKey('JoinCode')" in ps1 and "Remove-Item -LiteralPath $JoinCodeFile -Force" in ps1
          and "'S-1-5-18', 'S-1-5-32-544'" in ps1)
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh:
        r = subprocess.run([pwsh, "-NoProfile", "-File", os.path.join(ROOT, "install", "install.ps1"), "-JoinCode", "abc"],
                           capture_output=True, text=True, timeout=60)
        check("install.ps1 -JoinCode 實際執行：拒絕並說明", r.returncode == 1 and "不能放在指令列" in r.stdout, r.stdout[-400:])


def scripts():
    os.environ.setdefault("XCONSOLE_HOME", os.path.join(W, "render"))
    import xconsole_server as X
    j = {"console": "192.0.2.10:9400", "pin": "sha256//AAAA", "fp": "AB" * 32}
    sh, ps = X.install_script("code1234567890abcdefgh", j), X.install_script_ps1("code1234567890abcdefgh", j)
    check("產出的 bash 與 PowerShell 腳本都不再用環境變數傳加入碼", "XC_CODE" not in sh and "XC_CODE" not in ps)
    check("  bash 改走 here-string、PowerShell 改走管線", '<<<"$CODE"' in sh and "$payload = $CODE | & $vpy $jp" in ps)
    home = os.path.join(W, "payload")
    os.makedirs(os.path.join(home, ".xconsole"))
    open(os.path.join(home, ".xconsole", "token"), "w").write("tok")
    open(os.path.join(home, ".xconsole", "agent-cert.pem"), "w").write("CERT")
    r = subprocess.run([sys.executable, "-c", X.JOIN_PAYLOAD_PY], input="abc_DEF-123\n", capture_output=True, text=True,
                       env={**os.environ, "XCONSOLE_HOME": home})
    d = json.loads(r.stdout)
    check("  送回主控台的資料：加入碼來自標準輸入", d["code"] == "abc_DEF-123" and d["token"] == "tok", r)
    r = subprocess.run(["bash", "-n"], input=sh, capture_output=True, text=True)
    check("  bash 腳本語法正確", r.returncode == 0, r.stderr)


def main():
    try:
        scripts()
        installers()
        e2e()
    finally:
        shutil.rmtree(W, ignore_errors=True)
    print("\n" + ("全部通過" if not FAILS else f"失敗 {len(FAILS)} 項：" + "、".join(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
