#!/usr/bin/env python3
"""線上更新的自我檢查（不連 GitHub、不真的重啟）：
- 更新包核對 sha256；不對就不更新
- 解壓擋 ..、絕對路徑、連結；少了主程式不收；新版編譯不過不收
- 換檔前備份；換好後版本變新、要重啟；退回上一版換回原本的檔
- 測試檔與不認得的檔不會被換上
跑法：python3 test_updater.py
"""
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import updater as U  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def pkg(version="2099.1.1", extra=None, drop=None, bad_py=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name, data):
            if drop and name.endswith(drop):
                return
            ti = tarfile.TarInfo(f"xconsole-{version}/{name}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for f in ("xconsole_server.py", "chats.py", "updater.py"):
            add(f"console/{f}", open(os.path.join(HERE, f), "rb").read() + (b"\n(\n" if bad_py and f == "chats.py" else b""))
        add("console/test_secret.py", b"print('should not be installed')\n")
        add("console/static/index.html", b"<!doctype html><title>new</title>")
        add("console/static/app.js", b"// new app\n")
        add("agent/xconsole_agent.py", b"# xconsole-agent new\nprint('agent')\n")
        add("requirements.txt", open(os.path.join(ROOT, "requirements.txt"), "rb").read())
        add("VERSION", version.encode())
        for name, data, kind in extra or []:
            ti = tarfile.TarInfo(name)
            if kind == "link":
                ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def wait_job(states=("restarting", "failed"), sec=20):
    end = time.time() + sec
    while time.time() < end:
        job, _ = U.status()
        if job.get("state") in states:
            return job
        time.sleep(0.05)
    return U.status()[0]


def main():
    inst = tempfile.mkdtemp(prefix="xc-inst-")
    state = tempfile.mkdtemp(prefix="xc-state-")
    os.makedirs(os.path.join(inst, "static"))
    os.makedirs(os.path.join(inst, "agent"))
    for f in ("xconsole_server.py", "chats.py", "updater.py"):
        shutil.copy2(os.path.join(HERE, f), os.path.join(inst, f))
    open(os.path.join(inst, "static", "index.html"), "w").write("old index")
    open(os.path.join(inst, "static", "app.js"), "w").write("// old app")
    open(os.path.join(inst, "agent", "xconsole_agent.py"), "w").write("# old agent")
    shutil.copy2(os.path.join(ROOT, "requirements.txt"), os.path.join(inst, "requirements.txt"))
    open(os.path.join(inst, "VERSION"), "w").write("2026.1.1")
    U.HERE = inst
    restarted = []
    U._restart_soon = lambda r: restarted.append(1)

    check("讀得到目前版本", U.current_version() == "2026.1.1")
    data = pkg()
    c, out = U.start_update(state, None, data=data, sha256="0" * 64)
    job = wait_job()
    check("sha256 對不上：不更新", c == 200 and job["state"] == "failed" and "sha256" in job["error"]
          and open(os.path.join(inst, "static", "app.js")).read() == "// old app", job)
    check("sha256 格式不對當場擋", U.start_update(state, None, data=data, sha256="abc")[0] == 400)

    for name, d, why in ((pkg(extra=[("../evil.py", b"x", "file")]), None, "路徑有 .."),
                         (pkg(extra=[("/abs/evil.py", b"x", "file")]), None, "絕對路徑"),
                         (pkg(extra=[("xconsole-x/console/link", b"", "link")]), None, "連結"),
                         (pkg(drop="agent/xconsole_agent.py"), None, "少了代理主程式"),
                         (pkg(bad_py=True), None, "新版編譯不過")):
        U.STATE["job"] = None
        U.start_update(state, None, data=name, sha256=hashlib.sha256(name).hexdigest())
        job = wait_job()
        check(f"擋下：{why}", job["state"] == "failed", job)
    check("  擋下的都沒有動到目前的檔", open(os.path.join(inst, "static", "index.html")).read() == "old index")

    real_max = U.MAX_UNPACKED
    U.MAX_UNPACKED = 1000
    try:
        U.STATE["job"] = None
        big = pkg(extra=[("xconsole-x/console/big.json", b"0" * 5000, "file")])
        U.start_update(state, None, data=big, sha256=hashlib.sha256(big).hexdigest())
        job = wait_job()
        check("擋下：解開後總大小超過上限", job["state"] == "failed" and "太大" in job["error"], job)
    finally:
        U.MAX_UNPACKED = real_max

    bdir = os.path.join(state, "update-backups")
    for i in range(12):                                   # 以前留下的舊備份
        os.makedirs(os.path.join(bdir, f"2020010{i // 10}-00000{i % 10}"))
        open(os.path.join(bdir, f"2020010{i // 10}-00000{i % 10}", "manifest.json"), "w").write('{"version":"old","files":[]}')
    U.STATE["job"] = None
    c, out = U.start_update(state, None, data=data, sha256=hashlib.sha256(data).hexdigest())
    job = wait_job()
    check("正常更新：換上新版並要求重啟", job["state"] == "restarting" and restarted and U.current_version() == "2099.1.1", job)
    check("  主控台、畫面、隨附的代理都換了", open(os.path.join(inst, "static", "app.js")).read().strip() == "// new app"
          and "new" in open(os.path.join(inst, "agent", "xconsole_agent.py")).read())
    check("  測試檔不會被換上", not os.path.exists(os.path.join(inst, "test_secret.py")))
    bs = U.backups(state)
    check("  換之前有備份（記著原本的版本）", bs and bs[0]["version"] == "2026.1.1", bs)

    check("  備份只留最近 10 份，剛做的那份還在", len(os.listdir(bdir)) == 10 and bs[0]["version"] == "2026.1.1", sorted(os.listdir(bdir)))

    check("更新進行中不准退回", U.rollback(state, None)[0] == 409)
    U.STATE["job"] = None
    open(os.path.join(inst, "requirements.txt"), "a").write("\nnewdep==9.9\n")   # 新版多了一個相依套件
    piped = []
    real_pip = U._pip
    U._pip = lambda p: (piped.append(open(p).read()), (True, ""))[1]
    try:
        c, out = U.rollback(state, None)
        job = wait_job()
    finally:
        U._pip = real_pip
    check("退回上一版：檔換回原本的", c == 200 and out["version"] == "2026.1.1" and job["state"] == "restarting"
          and U.current_version() == "2026.1.1" and open(os.path.join(inst, "static", "app.js")).read() == "// old app", (out, job))
    check("  相依清單不同：把舊版的相依套件裝回去", piped and "newdep" not in piped[0], piped)
    data2, sha2 = U.bundled_agent()
    check("隨附代理的 sha256", sha2 == hashlib.sha256(open(os.path.join(inst, "agent", "xconsole_agent.py"), "rb").read()).hexdigest())

    # ---- 代理自我更新
    sys.path.insert(0, os.path.join(ROOT, "agent"))
    os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-agent-")
    import base64
    import xconsole_agent as A  # noqa: E402
    tmpd = tempfile.mkdtemp(prefix="xc-self-")
    fake = os.path.join(tmpd, "xconsole_agent.py")
    open(fake, "w").write("# xconsole-agent old\n")
    A.AGENT_FILE = fake

    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass
    real_thread = A.threading.Thread
    A.threading.Thread = NoThread
    try:
        new = b"# xconsole-agent new\nx = 1\n"
        c, out = A.self_update({"code": base64.b64encode(new).decode(), "sha256": "0" * 64})
        check("代理：sha256 對不上不換", c == 400 and open(fake).read() == "# xconsole-agent old\n")
        bad = b"# xconsole-agent bad\n(\n"
        c, out = A.self_update({"code": base64.b64encode(bad).decode(), "sha256": hashlib.sha256(bad).hexdigest()})
        check("代理：新版編譯不過不換", c == 400 and open(fake).read() == "# xconsole-agent old\n", out)
        other = b"print('not an agent')\n"
        c, out = A.self_update({"code": base64.b64encode(other).decode(), "sha256": hashlib.sha256(other).hexdigest()})
        check("代理：不像代理程式的不換", c == 400)
        c, out = A.self_update({"code": base64.b64encode(new).decode(), "sha256": hashlib.sha256(new).hexdigest()})
        check("代理：核對過就換上、舊的留 .bak、準備重啟", c == 200 and open(fake, "rb").read() == new
              and open(fake + ".bak").read() == "# xconsole-agent old\n" and out["restarting"], out)
    finally:
        A.threading.Thread = real_thread

    print("全部通過" if OK else "有項目失敗")
    sys.exit(0 if OK else 1)


if __name__ == "__main__":
    main()
