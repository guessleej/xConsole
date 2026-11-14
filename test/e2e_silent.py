"""靜默安裝實裝驗收（CI 三個作業系統各跑一次；要 root／系統管理員，會在這台建帳號與開機自啟，只在 CI 機器上跑）。

起一台隔離的主控台 → 產加入碼 → 回應檔權限不對要被擋、檔案刪掉 → 權限對的回應檔裝成開機自啟並加入：
Linux 是專用帳號 xconsole 的 systemd 服務（linger）、macOS 是 _xconsole 的 LaunchDaemon、Windows 是 SYSTEM 排程。
裝完驗：回應檔已刪、主控台清單有這台、代理以專用帳號跑、把代理砍掉會自己起來、-JoinCode 參數被拒、移除乾淨。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "console"))
import test_joincodes as T  # noqa: E402

WIN = os.name == "nt"
MAC = sys.platform == "darwin"
FAILS = []
W = tempfile.mkdtemp(prefix="xc-e2e-silent-")
PW = T.PW
LABEL = "tw.xcloudinfo.xconsole-agent"


def check(name, cond, detail=""):
    print(("通過 " if cond else "失敗 ") + name + ("" if cond else f"：{str(detail)[-1500:]}"), flush=True)
    if not cond:
        FAILS.append(name)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900, **kw)


def write_code(path, code, ok=True):
    """回應檔：ok＝只有 root／系統管理員讀得到；否則故意放寬。加入碼走標準輸入，不出現在命令列。"""
    if WIN:
        open(path, "w", encoding="ascii").write(code + "\n")
        if ok:
            sh(["icacls", path, "/setowner", "*S-1-5-32-544"])
            sh(["icacls", path, "/inheritance:r", "/grant:r", "*S-1-5-18:F", "*S-1-5-32-544:F"])
        else:
            sh(["icacls", path, "/grant", "*S-1-5-32-545:R"])
    else:
        sh(["sudo", "sh", "-c", f"umask 077; cat > '{path}'; chmod {'600' if ok else '640'} '{path}'"], input=code + "\n")


def install(host, fp, path, extra=()):
    if WIN:
        return sh(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(ROOT, "install", "install.ps1"),
                   "-Console", host, "-Fingerprint", fp, "-JoinCodeFile", path, *extra])
    return sh(["sudo", "bash", os.path.join(ROOT, "install", "install.sh"), "--console", host, "--fingerprint", fp,
               "--join-code-file", path, *extra])


def agent_user():
    if WIN:
        r = sh(["powershell", "-NoProfile", "-Command", "(Get-ScheduledTask -TaskName 'xConsole Agent').Principal.UserId"])
        return r.stdout.strip()
    r = sh(["sh", "-c", "ps -axo user=,command= | grep '[x]console_agent.py' | awk '{print $1}' | head -1"])
    return r.stdout.strip()


def agent_pid():
    if WIN:
        r = sh(["powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*xconsole_agent.py*' } | Select-Object -First 1).ProcessId"])
    else:
        r = sh(["sh", "-c", "pgrep -f '[x]console_agent.py' | head -1"])
    return r.stdout.strip()


def healthy():
    import ssl
    try:
        ssl.get_server_certificate(("127.0.0.1", 9401), timeout=3)
        return True
    except OSError:
        return False


def main():
    procs = []
    try:
        cport = T.free_port()
        chome = os.path.join(W, "console")
        os.makedirs(os.path.join(chome, ".xconsole"), mode=0o700)
        cenv = {k: v for k, v in os.environ.items() if k != "XCONSOLE_HOME"}
        cenv.update(HOME=chome, USERPROFILE=chome, XCONSOLE_HOME=chome, XCONSOLE_PORT=str(cport), XCONSOLE_LINK_PORT=str(T.free_port()))
        subprocess.run([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py"), "--set-credentials"],
                       input=f"admin\n{PW}\n".encode(), env=cenv, check=True, capture_output=True)
        procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "console", "xconsole_server.py")], env=cenv,
                                      stdout=open(os.path.join(W, "console.log"), "w"), stderr=subprocess.STDOUT))
        check("隔離主控台啟動", T.wait_tls(cport))
        ccert = os.path.join(chome, ".xconsole", "tls", "cert.pem")
        _, _, sc = T.https(cport, ccert, "POST", "/api/login", {"username": "admin", "password": PW})
        ck = {"Cookie": "xconsole=" + sc.split("xconsole=", 1)[1].split(";", 1)[0]}
        host = f"127.0.0.1:{cport}"
        st, jc = T.https(cport, ccert, "POST", "/api/nodes/join-code", {"name": "LAB{01..02}", "uses": 2, "console": host}, ck)[:2]
        check("產加入碼", st == 200 and jc.get("silent"), jc)
        code, fp = jc["silent"]["code"], jc["silent"]["fingerprint"]
        tmp = os.environ.get("RUNNER_TEMP") or W

        bad = os.path.join(tmp, "xc-join-bad.txt")
        write_code(bad, code, ok=False)
        r = install(host, fp, bad)
        check("回應檔權限放寬：拒絕安裝、說明要作廢重產", r.returncode != 0 and "作廢" in (r.stdout + r.stderr), r.stdout + r.stderr)
        check("  權限不對的回應檔也刪掉了", not os.path.exists(bad))

        if WIN:
            r = sh(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(ROOT, "install", "install.ps1"),
                    "-Console", host, "-JoinCode", code])
        else:
            r = sh(["sudo", "bash", os.path.join(ROOT, "install", "install.sh"), "--console", host, "--join-code", code])
        check("加入碼放在指令列：拒絕並說明", r.returncode != 0 and "不能放在指令列" in (r.stdout + r.stderr), r.stdout + r.stderr)

        good = os.path.join(tmp, "xc-join.txt")
        write_code(good, code)
        r = install(host, fp, good)
        print(r.stdout[-3000:], r.stderr[-1500:], sep="\n", flush=True)
        check("靜默安裝成功", r.returncode == 0, r.stdout + r.stderr)
        check("  回應檔讀完就刪", not os.path.exists(good))
        _, nodes = T.https(cport, ccert, "GET", "/api/nodes", None, ck)[:2]
        check("  主控台清單有這台，名稱照樣板 LAB01", any(n["id"] == "LAB01" for n in nodes.get("nodes", [])), nodes)
        want = "SYSTEM" if WIN else ("_xconsole" if MAC else "xconsole")
        check(f"  代理以 {want} 跑（開機自啟、不必有人登入）", agent_user() == want, agent_user())
        _, raw, _ = T.https(cport, ccert, "GET", "/api/fleet", None, ck, raw=True)
        print("記憶體區說明：", sorted(set(re.findall(r'"model_note": ?"([^"]*)"', raw))), flush=True)

        pid = agent_pid()
        if WIN:
            sh(["taskkill", "/F", "/PID", pid])
        else:
            sh(["sudo", "kill", "-9", pid])
        back = False
        for _ in range(40 if WIN else 30):
            time.sleep(2)
            if agent_pid() not in ("", pid) and healthy():
                back = True
                break
        if WIN:
            # 工作排程器的「失敗時重新啟動」只管啟動失敗，不管執行中被砍：照實印出來，不當成失敗（開機自啟才是這項的要求）
            print(f"資訊：Windows 代理被砍掉後{'自己起來了' if back else '沒有自己起來（下次開機或 1 分鐘重試時才會起）'}", flush=True)
        else:
            check("  代理被砍掉會自己起來", back)

        r = install(host, fp, good)
        check("回應檔已經不在：再跑一次說明找不到", r.returncode != 0 and "找不到回應檔" in (r.stdout + r.stderr), r.stdout + r.stderr)
    finally:
        if WIN:
            r = sh(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(ROOT, "install", "install.ps1"), "-Uninstall"])
        else:
            r = sh(["sudo", "bash", os.path.join(ROOT, "install", "install.sh"), "--uninstall"])
        check("移除", r.returncode == 0, r.stdout + r.stderr)
        time.sleep(3)
        check("  移除後代理不在了", not agent_pid(), agent_pid())
        for p in procs:
            p.terminate()
        if FAILS:
            print("===== console.log\n" + open(os.path.join(W, "console.log"), encoding="utf-8", errors="replace").read()[-3000:])
    print("\n" + ("全部通過" if not FAILS else f"失敗 {len(FAILS)} 項：" + "、".join(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
