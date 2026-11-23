#!/usr/bin/env python3
"""身分與複製偵測（清單 #8）代理這一側的檢查。全部在隔離目錄跑，不碰這台的設定。

一、識別碼有效性：全 0、全 F、廠商預設值、太短的主機板識別碼不拿來判定；系統碟序號同理。
二、判定規則：SMBIOS 變了＝換了一台；只有作業系統識別碼變＝重灌；UUID 無效改看系統碟；都讀不到＝無法判定；
    暫時讀不到（例如開機時 root 的副本還沒寫好）不能誤判成複製。網卡不參與。
三、啟動流程：第一次記下來、權杖不動；重灌保留身分並記一筆；複製出來的丟掉權杖、憑證、連回設定與群組狀態，
    顯示需要重新加入（不用登入的 /healthz 也看得到）；重新加入後清掉；讀不到的值之後補上。
四、這台實際讀得到的識別碼（三個作業系統各自的取法）；給主控台的只有雜湊，不露作業系統識別碼與碟序號原文。
跑法：python3 test_identity.py
"""
import json
import os
import ssl
import sys
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-ident-")
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


U1 = "4c4c4544-0042-3510-8051-b4c04f4d4a32"
U2 = "4c4c4544-0042-3510-8051-b4c04f4d4a99"


def ids(smbios=U1, os_id="a1b2c3d4e5f60718293a4b5c6d7e8f90", disk="S4EWNX0R123456", macs=("aa:bb:cc:00:00:01",)):
    return {"smbios": A._hwnorm(smbios), "os": A._hwnorm(os_id), "disk": A._hwnorm(disk), "macs": list(macs)}


def unit():
    case("正常的 SMBIOS UUID 有效", A.hwid_valid(U1))
    for bad in ("00000000-0000-0000-0000-000000000000", "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
                "03000200-0400-0500-0006-000700080009", "To be filled by O.E.M.", "Default string", "1234", "", None):
        case(f"無效的主機板識別碼不拿來判定：{bad!r}", not A.hwid_valid(bad))
    case("系統碟序號：正常的有效、空的與全 0 無效",
         A.disk_valid("S4EWNX0R123456") and not A.disk_valid("") and not A.disk_valid("00000000"))

    v = A.identity_verdict
    case("都沒變：同一台、依據主機板", v(ids(), ids()) == ("same", "smbios"))
    case("SMBIOS 變了：判定複製", v(ids(), ids(smbios=U2)) == ("clone", "smbios"))
    case("只有作業系統識別碼變：重灌，保留身分", v(ids(), ids(os_id="ffeeddccbbaa99887766554433221100")) == ("reinstall", "smbios"))
    case("網卡全換：不影響（筆電換網路、擴充座）", v(ids(), ids(macs=("11:22:33:44:55:66",))) == ("same", "smbios"))
    case("SMBIOS 跟碟都變：仍判複製", v(ids(), ids(smbios=U2, disk="ZZZ999")) == ("clone", "smbios"))
    z = "00000000-0000-0000-0000-000000000000"
    case("UUID 無效：改看系統碟，碟變了＝複製", v(ids(smbios=z), ids(smbios=z, disk="OTHER123")) == ("clone", "disk"))
    case("UUID 無效：碟沒變＝同一台", v(ids(smbios=z), ids(smbios=z)) == ("same", "disk"))
    case("UUID 無效＋碟讀不到：無法判定，照舊", v(ids(smbios=z, disk=None), ids(smbios=z, disk=None)) == ("unknown", None))
    case("UUID 暫時讀不到（root 的副本還沒寫好）：無法判定，不誤判", v(ids(), ids(smbios=None)) == ("unknown", None))
    case("  同時換過系統碟：仍然不判複製（不拿碟序號推翻主機板識別碼）", v(ids(), ids(smbios=None, disk="NEWDISK99")) == ("unknown", None))
    case("  這次讀到的是無效值：一樣不判", v(ids(), ids(smbios="00000000-0000-0000-0000-000000000000", disk="NEWDISK99")) == ("unknown", None))
    case("以前讀不到、現在讀得到：不誤判", v(ids(smbios=None), ids()) == ("same", "disk"))


def flow():
    d = A.STATE_DIR
    tok0 = A.TOKEN
    case("第一次啟動：記下身分，判定同一台", A.identity_check(ids()) == "same" and os.path.exists(A.IDENTITY_FILE))
    case("  權杖不動", A.TOKEN == tok0 and open(A.TOKEN_FILE).read().strip() == tok0)
    case("再啟動一次（識別碼都一樣）：同一台", A.identity_check(ids()) == "same" and A.TOKEN == tok0)
    case("重灌：保留身分、記一筆", A.identity_check(ids(os_id="ffeeddccbbaa99887766554433221100")) == "reinstall"
         and A.TOKEN == tok0 and any("重灌" in x["text"] for x in A.IDENTITY["notes"]))
    case("UUID 暫時讀不到：不誤判、也不把讀得到的值蓋掉",
         A.identity_check(ids(smbios=None, os_id="ffeeddccbbaa99887766554433221100")) == "unknown" and A.TOKEN == tok0
         and json.load(open(A.IDENTITY_FILE))["ids"]["smbios"] == A._hwnorm(U1))

    # 模擬加入過主控台：有憑證、連回設定、群組狀態
    A.ensure_cert()
    for f in (A.LINK_FILE, A.LINK_CERT, A.GROUP_FILE):
        open(f, "w").write("{}")
    case("複製到另一台（SMBIOS 不同）：判定複製", A.identity_check(ids(smbios=U2, os_id="ffeeddccbbaa99887766554433221100")) == "clone")
    case("  換了新權杖（舊的跟著映像在兩台上，不能再用）", A.TOKEN != tok0 and open(A.TOKEN_FILE).read().strip() == A.TOKEN)
    gone = [os.path.basename(f) for f in (A.CERT_FILE, A.KEY_FILE, A.LINK_FILE, A.LINK_CERT, A.GROUP_FILE) if os.path.exists(f)]
    case("  憑證、連回設定、群組狀態都丟掉（下次啟動另產新憑證）", not gone, gone)
    case("  顯示需要重新加入，寫明原因", (A.IDENTITY.get("rejoin") or {}).get("reason", "").startswith("主機板識別碼"))
    case("  新身分以這台的識別碼為準：下次啟動不會再判一次複製",
         A.identity_check(ids(smbios=U2, os_id="ffeeddccbbaa99887766554433221100")) == "same" and A.IDENTITY.get("rejoin"))

    # 不用登入的 /healthz 也看得到「需要重新加入」
    A.ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(A.CERT_FILE, A.KEY_FILE)
    srv = A.AgentTLSServer(("127.0.0.1", 0), A.Handler)
    srv.ctx = ctx
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cc = ssl.create_default_context(cafile=A.CERT_FILE)
    cc.check_hostname = False
    url = f"https://127.0.0.1:{srv.server_address[1]}/healthz"
    h = json.load(urllib.request.urlopen(url, context=cc, timeout=10))
    case("  /healthz 帶 needs_rejoin（不用登入、不含機密）", h.get("needs_rejoin") is True and set(h) == {"ok", "service", "needs_rejoin"}, h)

    A.identity_rejoined()
    h = json.load(urllib.request.urlopen(url, context=cc, timeout=10))
    case("重新加入後：不再顯示，並記一筆", not A.IDENTITY.get("rejoin") and "needs_rejoin" not in h
         and any("重新加入" in x["text"] for x in A.IDENTITY["notes"]))
    srv.shutdown()

    p = A.identity_public()
    raw = json.load(open(A.IDENTITY_FILE))["ids"]
    case("給主控台的：SMBIOS 與網卡照實，作業系統識別碼與碟序號只有雜湊",
         p["smbios"] == A._hwnorm(U2) and p["os"] != raw["os"] and p["disk"] != raw["disk"] and len(p["os"]) == 12
         and json.dumps(p).find(raw["os"]) < 0 and json.dumps(p).find(raw["disk"]) < 0, p)
    case("  有判定用的指紋", p["fp"] and len(p["fp"]) == 12 and p["basis"] == "smbios", p)


def real():
    got = A.hw_ids()
    print(f"這台（{A.OS}）讀到：主機板 {'有效' if A.hwid_valid(got['smbios']) else '無效或讀不到'}、"
          f"作業系統識別碼 {'有' if got['os'] else '沒有'}、系統碟序號 {'有' if A.disk_valid(got['disk']) else '沒有'}、"
          f"實體網卡 {len(got['macs'])} 張")
    case("這台實際讀得到識別碼：格式正確", set(got) == {"smbios", "os", "disk", "macs"} and isinstance(got["macs"], list))
    if A.OS == "darwin":
        case("  macOS：IOPlatformUUID 一定讀得到", A.hwid_valid(got["smbios"]))
    if A.OS == "windows":
        case("  Windows：MachineGuid 一定讀得到", bool(got["os"]))
    if A.OS == "linux":
        case("  Linux：/etc/machine-id 讀得到", bool(got["os"]) or not os.path.exists("/etc/machine-id"))
    if not (A.hwid_valid(got["smbios"]) or A.disk_valid(got["disk"])):
        # Linux 一般帳號讀不到 product_uuid（大量部署有 root 寫的副本）；雲端虛擬機的碟常常沒有序號：照實印出，不算失敗
        print("提醒：這台主機板識別碼與系統碟序號都讀不到，複製偵測在這台只能靠主控台看「同時連回」")


if __name__ == "__main__":
    unit()
    flow()
    real()
    print("全部通過" if OK else "有失敗")
    sys.exit(0 if OK else 1)
