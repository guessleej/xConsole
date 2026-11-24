#!/usr/bin/env python3
"""Windows推理引擎要的系統元件與看得懂的錯誤（2026-11-24 HPE工作站實機回報：一鍵安裝之後還要自己裝東西、
Windows 11的智慧型應用程式控制擋下xConsole後一直跳錯誤）。全部用假資料夾，三個系統都能跑。

一、Visual C++執行階段：系統（System32）沒有的才從內附的那份補進引擎資料夾；系統有的不動；資料夾已經有的不蓋；
    內附的也沒有就略過、不報錯。代理與打包腳本的清單要一致（少一份就有一邊補不到）。
二、結束碼翻成人話：缺DLL（0xC0000135）、版本太舊（0xC0000139）、CPU指令集（0xC000001D）；
    Windows回的有號負數也要認得；其他結束碼不亂猜。
三、被智慧型應用程式控制（4551）或群組原則（1260）擋下：講清楚是誰擋、關掉之後回不去。
跑法：python3 test_win_runtime.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-winrt-")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "desktop", "scripts"))
import xconsole_agent as A  # noqa: E402
import prepare_payload as P  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


def touch(d, *names):
    os.makedirs(d, exist_ok=True)
    for n in names:
        open(os.path.join(d, n), "wb").write(b"MZ" + n.encode())


def crt():
    t = tempfile.mkdtemp(prefix="xc-crt-")
    system, src, eng = (os.path.join(t, x) for x in ("System32", "runtime", "engine"))
    touch(system, "vcruntime140.dll")                                   # 系統只有這一個
    touch(src, *A.WIN_CRT)
    touch(eng, "llama-server.exe", "msvcp140.dll")                      # 引擎資料夾已經有一個（官方包附的）
    open(os.path.join(eng, "msvcp140.dll"), "wb").write(b"engine-own")
    added = A.win_crt_fill(eng, system=system, sources=[src])
    case("系統沒有的執行階段：從內附的那份補進引擎資料夾", "vcomp140.dll" in added and "vcruntime140_1.dll" in added, added)
    case("  系統有的不補（系統那份比較新）", "vcruntime140.dll" not in added and not os.path.exists(os.path.join(eng, "vcruntime140.dll")))
    case("  引擎資料夾已經有的不蓋", "msvcp140.dll" not in added and open(os.path.join(eng, "msvcp140.dll"), "rb").read() == b"engine-own")
    case("  再跑一次：沒有東西要補", A.win_crt_fill(eng, system=system, sources=[src]) == [])
    eng2 = os.path.join(t, "engine2")
    touch(eng2, "llama-server.exe")
    case("內附的也沒有：略過、不報錯", A.win_crt_fill(eng2, system=system, sources=[os.path.join(t, "nothing")]) == [])
    case("不是Windows（沒給系統資料夾）：什麼都不做", A.OS == "windows" or A.win_crt_fill(eng2) == [])
    case("代理與打包腳本的清單一致", tuple(A.WIN_CRT) == tuple(P.WIN_CRT), (A.WIN_CRT, P.WIN_CRT))
    case("  打包時一定要有的幾個都在清單裡（含OpenMP的vcomp140）", set(P.WIN_CRT_MUST) <= set(P.WIN_CRT) and "vcomp140.dll" in P.WIN_CRT_MUST)
    py = os.path.dirname(os.path.abspath(sys.executable))
    case("找內附檔案的位置：payload/runtime與內附Python的資料夾", A.win_crt_dirs() == [os.path.join(os.path.dirname(py), "runtime"), py])


def hints():
    real = A.OS
    A.OS = "windows"
    try:
        h = A.win_exit_hint(0xC0000135)
        case("結束碼0xC0000135：缺系統元件，講要裝什麼", h and "Visual C++" in h and "驅動" in h, h)
        case("  Windows回的有號負數也認得", A.win_exit_hint(-1073741515) == h)
        case("結束碼0xC0000139：版本太舊", "太舊" in (A.win_exit_hint(0xC0000139) or ""))
        case("結束碼0xC000001D：CPU不支援指令集", "指令集" in (A.win_exit_hint(-1073741795) or ""))
        case("一般的結束碼（1）：不亂猜", A.win_exit_hint(1) is None and A.win_exit_hint(None) is None)
    finally:
        A.OS = real
    case("不是Windows：結束碼不翻（Linux的135不是缺DLL）", A.OS == "windows" or A.win_exit_hint(0xC0000135) is None)

    class Blocked(OSError):
        winerror = 4551
    b = A.win_block_hint(Blocked("應用程式控制原則已封鎖此檔案"))
    case("被智慧型應用程式控制擋下（4551）：講是誰擋、怎麼處理", b and "智慧型應用程式控制" in b and "重灌" in b, b)
    p = OSError("blocked")
    p.winerror = 1260
    case("  群組原則擋下（1260）也認得", A.win_block_hint(p) is not None)
    case("  其他錯誤照原文", A.explain_oserror(FileNotFoundError("找不到llama-server.exe")) == "找不到llama-server.exe")


if __name__ == "__main__":
    crt()
    hints()
    print("全部通過" if OK else "有失敗")
    sys.exit(0 if OK else 1)
