"""把這台的節點代理加入主控台（靜默安裝用；install.sh 與 install.ps1 都呼叫這支）。

加入碼只從標準輸入讀：放在參數會留在行程清單與派送紀錄，放在環境變數會出現在 /proc/<pid>/environ。
主控台憑證：給了 --fingerprint 就只接受那一張（比對整張憑證的 SHA-256，主控台「加一台機器」畫面上抄得到）；
沒給就照一般方式驗證（主控台換成正式憑證之後用這條）。兩條都不是關掉驗證。

  python join.py --console 主控台位址:埠 [--fingerprint 十六進位] --home 資料夾 [--agent-port 9401] < 加入碼
結束碼：0 加入成功；1 失敗（原因印在畫面上）。
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import sys

HOST_RX = re.compile(r"[A-Za-z0-9.-]+(?::\d{1,5})?|\[[0-9A-Fa-f:]+\](?::\d{1,5})?")
CODE_RX = re.compile(r"[A-Za-z0-9_-]{20,64}")


def read_code(stream):
    code = (stream.readline() or "").strip().lstrip("﻿")
    if not CODE_RX.fullmatch(code):
        raise SystemExit("加入碼格式不對：請把主控台「加一台機器」畫面上的加入碼原樣放進回應檔（一行，不含其他文字）")
    return code


def my_ips():
    """UDP connect 不送封包，三個系統都能取到對外網卡的 IP。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return [ip]
    except OSError:
        return []


class Pinned(http.client.HTTPSConnection):
    """TLS 交握完、送出任何資料之前，先比對主控台憑證的 SHA-256；不一樣就斷線。"""

    def __init__(self, host, port, fp, **kw):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # ponytail: 主控台多半還是暫用自簽，沒有 CA 可驗；改由下面 connect() 比對整張憑證，等同只信這一張
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        super().__init__(host, port, context=ctx, **kw)
        self.fp = fp

    def connect(self):
        super().connect()
        got = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest().upper()
        if got != self.fp:
            self.sock.close()
            raise ssl.SSLError(f"主控台憑證對不上（拿到 {got[:16]}…，預期 {self.fp[:16]}…）："
                               "可能連錯主控台，或主控台換過憑證；請到主控台「加一台機器」重新抄指紋")


def post(console, fp, body, timeout=150):
    host, _, port = console.rpartition(":") if re.search(r":\d+$", console) else (console, "", "443")
    host = host.strip("[]")
    if fp:
        c = Pinned(host, int(port), fp, timeout=timeout)
    else:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        c = http.client.HTTPSConnection(host, int(port), context=ctx, timeout=timeout)
    c.request("POST", "/api/join", body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, {"error": data.decode("utf-8", "replace")[:300]}


def save_link(sd, d):
    """主控台連不進這台時，把連回的位址與通道憑證寫給代理（代理每 5 秒看一次）；改由主控台連這台時，清掉以前的連回設定。
    ponytail: 與 console/xconsole_server.py 的 JOIN_SAVE_PY 同一套規則（那份要嵌進下載的 bash，不能 import 這支）。"""
    c = d.get("console") or {}
    if d.get("ok") and d.get("link") != "push":
        for name in ("agent-link.json", "agent-link.pem"):
            p = os.path.join(sd, name)
            if os.path.exists(p):
                os.remove(p)
    if d.get("link") == "push" and c.get("url") and c.get("cert"):
        meta = {k: c.get(k) for k in ("url", "node", "current", "public", "proxy", "v")}
        for name, data in (("agent-link.pem", c["cert"]), ("agent-link.json", json.dumps(meta))):
            tmp = os.path.join(sd, name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, os.path.join(sd, name))


def main(argv=None):
    ap = argparse.ArgumentParser(description="把這台的節點代理加入主控台；加入碼從標準輸入讀")
    ap.add_argument("--console", required=True)
    ap.add_argument("--fingerprint", default="")
    ap.add_argument("--home", required=True)
    ap.add_argument("--agent-port", type=int, default=9401)
    ap.add_argument("--linger", default="yes")
    a = ap.parse_args(argv)
    if not HOST_RX.fullmatch(a.console):
        raise SystemExit("主控台位址格式不對，例：192.0.2.10:9400")
    fp = re.sub(r"[^0-9A-Fa-f]", "", a.fingerprint).upper()
    if fp and len(fp) != 64:
        raise SystemExit("指紋要是 64 個十六進位字（SHA-256），請從主控台「加一台機器」畫面原樣複製")
    code = read_code(sys.stdin)
    sd = os.path.join(a.home, ".xconsole")
    body = {"code": code, "hostname": socket.gethostname(), "port": a.agent_port, "ips": my_ips(),
            "linger": a.linger == "yes", "push": True,
            "token": open(os.path.join(sd, "token"), encoding="utf-8").read().strip(),
            "cert": open(os.path.join(sd, "agent-cert.pem"), encoding="utf-8").read()}
    try:
        st, d = post(a.console, fp, body)
    except ssl.SSLCertVerificationError as ex:
        raise SystemExit(f"主控台憑證驗證不過（{ex.verify_message}）。主控台還在用暫用自簽憑證時，"
                         "請加上指紋參數（主控台「加一台機器 → 大量部署」那裡抄）") from None
    except (OSError, ssl.SSLError, http.client.HTTPException) as ex:
        raise SystemExit(f"連不到主控台 {a.console}：{ex}") from None
    print(d.get("message") or d.get("error") or d)
    save_link(sd, d)
    return 0 if st == 200 and d.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
