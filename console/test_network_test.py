#!/usr/bin/env python3
"""API 串接「從主控台經網路實測」的位址檢查：單機部署要放行本機網卡位址，其他一律不放行。
跑法：python3 test_network_test.py（不連外、不碰正式環境）
"""
import os
import socket
import sys
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="xc-net-")   # 狀態目錄跟著家目錄走，一定要在 import 之前
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xconsole_server as x  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(f"[{'通過' if cond else '失敗'}] {name}" + (f"：{detail}" if detail and not cond else ""))
    OK = OK and bool(cond)


def lan_ip():
    """本機對外網卡的位址（UDP connect 不送封包）；沒有網卡時回 None。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") or ip == "0.0.0.0" else ip
    except OSError:
        return None


# 驗尺：本機位址判斷本身要對
check("127.0.0.1 是本機位址", x.is_local_address("127.0.0.1"))
check("8.8.8.8 不是本機位址", not x.is_local_address("8.8.8.8"))
check("亂寫的位址不算本機", not x.is_local_address("not-an-ip"))

check("本機節點、網址也是 127.0.0.1：放行", x.same_machine("127.0.0.1", "127.0.0.1") == {"127.0.0.1"})
check("本機節點、網址是外部位址：擋下", not x.same_machine("127.0.0.1", "8.8.8.8"))
check("本機節點、網址是雲端 metadata：擋下", not x.same_machine("127.0.0.1", "169.254.169.254"))
check("解析不到的主機：擋下", not x.same_machine("127.0.0.1", "no-such-host.invalid"))

ip = lan_ip()
if ip:
    # 單機部署的情況：節點記成 127.0.0.1，對外網址是這台的網卡位址
    check("本機節點、網址是本機網卡位址：放行（單機部署）", x.same_machine("127.0.0.1", ip) == {ip}, ip)
    check("localhost 節點同樣適用", x.same_machine("localhost", ip) == {ip}, ip)
    # 遠端節點不能因為網址剛好是主控台這台就放行
    check("遠端節點、網址是主控台這台：擋下", not x.same_machine("192.0.2.10", ip), ip)
    check("節點與網址同一個網卡位址：放行", x.same_machine(ip, ip) == {ip}, ip)
else:
    print("[略過] 這台沒有對外網卡，不測網卡位址那幾項")

check("遠端節點、網址是同一個遠端位址：放行", x.same_machine("192.0.2.10", "192.0.2.10") == {"192.0.2.10"})
check("遠端節點、網址是另一台：擋下", not x.same_machine("192.0.2.10", "192.0.2.11"))

print("全部通過" if OK else "有項目失敗")
sys.exit(0 if OK else 1)
