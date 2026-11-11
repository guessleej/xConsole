#!/usr/bin/env python3
"""代理主動連回（工作站模式）代理這一側的自我檢查：讀連回設定、釘住通道憑證、位址與代理伺服器輪流試、
SNI 選通道憑證與收到新清單、
指令轉給本機代理後分片送回（一般、大檔、串流）、同一個編號不重做、取消會把本機連線關掉。
跑法：python3 test_link.py（只用標準函式庫＋代理本身，Linux／macOS／Windows 都能跑）
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["XCONSOLE_HOME"] = tempfile.mkdtemp(prefix="xc-link-")
sys.path.insert(0, HERE)
import xconsole_agent as A  # noqa: E402

OK = True


def case(name, good, detail=""):
    global OK
    OK &= bool(good)
    print(f"[{'通過' if good else '失敗'}] {name}" + (f"（{detail}）" if detail != "" and not good else ""))


BIG = os.urandom(9 * 1024 * 1024)


class H(A.Handler):
    """測試用：在代理原本的處理程式上加幾支假的路由。"""

    def do_GET(self):  # noqa: N802
        p = self.path
        if p.startswith("/t/json"):
            return self._send(200, {"ok": True, "auth": self.headers.get("Authorization") == f"Bearer {A.TOKEN}",
                                    "path": p})
        if p == "/t/big":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(BIG)))
            self.end_headers()
            self.wfile.write(BIG)
            return None
        if p.startswith("/t/stream"):
            n, gap = (10, 0.01) if p == "/t/stream" else (200, 0.1)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for i in range(n):
                    self.wfile.write(f"data: {i}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(gap)
            except OSError:
                H.broke = True
            return None
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path == "/t/echo":
            n = int(self.headers.get("Content-Length") or 0)
            return self._send(201, {"got": self.rfile.read(n).decode(), "ctype": self.headers.get("Content-Type")})
        return super().do_POST()


def main():
    A.ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(A.CERT_FILE, A.KEY_FILE)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    A.PORT = s.getsockname()[1]
    s.close()
    srv = A.AgentTLSServer(("127.0.0.1", A.PORT), H)
    srv.ctx = ctx
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ---- 1. 連回設定：沒有檔就不連；網址、節點編號不對就不連
    case("沒有 agent-link.json：不連", A.AgentLink.config() is None)
    with open(A.CERT_FILE) as fh:
        pem = fh.read()
    with open(A.LINK_CERT, "w") as fh:
        fh.write(pem)
    for bad in ({"url": "http://1.2.3.4:9402", "node": "a"}, {"url": "https://1.2.3.4:9402", "node": "../x"},
                {"url": "https://1.2.3.4:9402"}, {"url": "https://1.2.3.4:9402/x", "node": "a"}):
        with open(A.LINK_FILE, "w") as fh:
            json.dump(bad, fh)
        case(f"設定不合法不連：{bad}", A.AgentLink.config() is None)
    fp0 = A.AgentLink._fps(pem)[0]
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()                                          # 沒人聽的埠：模擬在家連不到內部位址
    with open(A.LINK_FILE, "w") as fh:
        json.dump({"url": f"https://127.0.0.1:{A.PORT}", "node": "ws-01", "public": ["bad addr", f"127.0.0.1:{dead_port}"]}, fh)
    cfg = A.AgentLink.config()
    case("讀到連回設定（內部位址＋對外網址，不合法的對外網址略過）",
         cfg and cfg["node"] == "ws-01" and cfg["addrs"] == [("127.0.0.1", A.PORT), ("127.0.0.1", dead_port)]
         and cfg["fps"] == [fp0] and cfg["current"] == fp0, cfg)

    # ---- 2. 釘住通道憑證：發給這台的那張連得上，不是的就拒絕（不是關掉驗證）
    try:
        A.AgentLink().connect(cfg).close()
        case("釘住的憑證：連得上", True)
    except Exception as exc:  # noqa: BLE001
        case("釘住的憑證：連得上", False, exc)
    try:
        A.AgentLink().connect({**cfg, "fps": ["0" * 64]})
        case("指紋不同：拒絕連線", False)
    except ssl.SSLError as exc:
        case("指紋不同：拒絕連線", "不是發給這台" in str(exc), exc)

    # ---- 2b. 位址輪流試：內部位址連不到就走對外網址，下次先試上次通的那條
    lk = A.AgentLink()
    cfg_out = {**cfg, "addrs": [("127.0.0.1", dead_port), ("127.0.0.1", A.PORT)]}
    try:
        lk.connect(cfg_out).close()
        case("第一個位址連不到：改走下一個", lk.route == 1, lk.route)
    except Exception as exc:  # noqa: BLE001
        case("第一個位址連不到：改走下一個", False, exc)

    # ---- 2c. 經 HTTP 代理伺服器（CONNECT）；指定的代理伺服器壞了就改直連
    seen = []

    def proxy_srv():
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(8)

        def pipe(x, y):
            try:
                while True:
                    d = x.recv(65536)
                    if not d:
                        break
                    y.sendall(d)
            except OSError:
                pass
            for z in (x, y):
                try:
                    z.close()
                except OSError:
                    pass

        def one(c):
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += c.recv(4096)
            head = buf.split(b"\r\n\r\n")[0].decode()
            seen.append(head)
            host, port = head.split()[1].rsplit(":", 1)
            up = socket.create_connection((host, int(port)))
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            threading.Thread(target=pipe, args=(c, up), daemon=True).start()
            threading.Thread(target=pipe, args=(up, c), daemon=True).start()

        def loop():
            while True:
                c, _ = ls.accept()
                threading.Thread(target=one, args=(c,), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()
        return ls.getsockname()[1]

    pp = proxy_srv()
    lk = A.AgentLink()
    try:
        c = lk.connect({**cfg, "addrs": [("127.0.0.1", A.PORT)], "proxy": f"http://127.0.0.1:{pp}"})
        c.close()
        case("經代理伺服器 CONNECT 連得上，憑證照樣釘住", seen and seen[0].startswith(f"CONNECT 127.0.0.1:{A.PORT} "), seen)
    except Exception as exc:  # noqa: BLE001
        case("經代理伺服器 CONNECT 連得上，憑證照樣釘住", False, exc)
    seen.clear()
    c = A._LinkConn("127.0.0.1", A.PORT, f"http://u%40x:p%3Aw@127.0.0.1:{pp}", ssl.create_default_context(), 5, "x")
    c.check_hostname = False
    try:
        c.connect()
    except OSError:
        pass                                              # 這裡只看送出的標頭，憑證驗不過沒關係
    c.close()
    want = "Proxy-Authorization: Basic " + base64.b64encode(b"u@x:p:w").decode()
    case("系統設定帶帳密的代理伺服器：帳密放在 Proxy-Authorization", seen and want in seen[0], seen)
    lk = A.AgentLink()
    try:
        lk.connect({**cfg, "addrs": [("127.0.0.1", A.PORT)], "proxy": f"http://127.0.0.1:{dead_port}"}).close()
        case("指定的代理伺服器連不到：改直連", lk.route == len(A.AgentLink.routes({**cfg, "addrs": [("127.0.0.1", A.PORT)],
                                                                            "proxy": f"http://127.0.0.1:{dead_port}"})) - 1, lk.route)
    except Exception as exc:  # noqa: BLE001
        case("指定的代理伺服器連不到：改直連", False, exc)

    # ---- 2d. 主控台用 SNI 選通道憑證：代理說它認哪一張就拿到哪一張（輪替期間新舊兩張同時服務）
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        import datetime as dt
    except ImportError:
        x509 = None
        print("（沒有 cryptography，略過通道憑證輪替）")
    if x509 is not None:
        now = dt.datetime.now(dt.timezone.utc)
        d = tempfile.mkdtemp(prefix="xc-ring-")
        ctxs, pems = {}, []
        for i in range(2):
            k = ec.generate_private_key(ec.SECP256R1())
            nm = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"xconsole-link-{i}")])
            c = (x509.CertificateBuilder().subject_name(nm).issuer_name(nm).public_key(k.public_key())
                 .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
                 .not_valid_after(now + dt.timedelta(days=30)).sign(k, hashes.SHA256()))
            cp, kp = os.path.join(d, f"c{i}.pem"), os.path.join(d, f"k{i}.pem")
            open(cp, "wb").write(c.public_bytes(serialization.Encoding.PEM))
            open(kp, "wb").write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()))
            x = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            x.load_cert_chain(cp, kp)
            fp = hashlib.sha256(c.public_bytes(serialization.Encoding.DER)).hexdigest()
            ctxs["k-" + fp[:32] + A.LINK_SNI] = x
            pems.append((fp, open(cp).read()))
        default = list(ctxs.values())[0]

        def sni(sobj, name, _c):
            if name in ctxs:
                sobj.context = ctxs[name]
        default.sni_callback = sni
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p2 = s.getsockname()[1]
        s.close()
        srv2 = A.AgentTLSServer(("127.0.0.1", p2), H)
        srv2.ctx = default
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        keep_pem, keep_json = open(A.LINK_CERT).read(), open(A.LINK_FILE).read()
        (f0, c0), (f1, c1) = pems
        open(A.LINK_CERT, "w").write(c0 + c1)
        cf = {**A.AgentLink.config(), "addrs": [("127.0.0.1", p2)]}
        for want in (f0, f1):
            try:
                cn = A.AgentLink().connect({**cf, "current": want})
                got = hashlib.sha256(cn.sock.getpeercert(binary_form=True)).hexdigest()
                cn.close()
                case(f"SNI 要第 {[f0, f1].index(want) + 1} 張：主控台送那一張", got == want)
            except Exception as exc:  # noqa: BLE001
                case(f"SNI 要第 {[f0, f1].index(want) + 1} 張：主控台送那一張", False, exc)
        # 收到主控台送來的新清單：存檔、版本更新；認的那張換了要重連
        ch = A.AgentLink.save_link(cf, {"cert": c1, "current": f1, "public": ["10.0.0.9:9402", "x y"], "proxy": "", "v": "7-abcdef01"})
        cf2 = A.AgentLink.config()
        case("收到新的通道憑證清單：存檔，認的那張換了就重連",
             ch is True and cf2["fps"] == [f1] and cf2["current"] == f1 and cf2["v"] == "7-abcdef01"
             and ("10.0.0.9", 9402) in cf2["addrs"] and cf2["node"] == "ws-01", cf2)
        case("  檔案權限只給自己（0600）", os.name == "nt" or (os.stat(A.LINK_CERT).st_mode & 0o777) == 0o600)
        for why, bad in (("目前那張不在清單裡", {"cert": c1, "current": f0}), ("沒有憑證", {"cert": "", "current": f1}),
                         ("代理伺服器不是 http://", {"cert": c1, "current": f1, "proxy": "socks5://x:1"})):
            case(f"  不合法的清單不存：{why}", A.AgentLink.save_link(cf2, bad) is False and A.AgentLink.config()["v"] == "7-abcdef01")
        open(A.LINK_CERT, "w").write(keep_pem)
        open(A.LINK_FILE, "w").write(keep_json)
        srv2.shutdown()

    # ---- 3. 執行指令：送回的片段
    link = A.AgentLink()
    sent = []
    link.send = lambda c, fr: sent.append(fr) or True

    def run(cmd):
        sent.clear()
        link.execute(cfg, {"timeout": 20, **cmd})
        return list(sent)

    def data(frs):
        return b"".join(base64.b64decode(f["data"]) for f in frs)

    fr = run({"id": "a" * 24, "method": "GET", "path": "/t/json?x=1"})
    body = json.loads(data(fr))
    case("一般指令：一片、帶狀態碼、最後一片", len(fr) == 1 and fr[0]["status"] == 200 and fr[0]["more"] is False, fr)
    case("  轉給本機代理時帶自己的權杖、查詢字串照帶", body["auth"] and body["path"] == "/t/json?x=1", body)
    fr = run({"id": "b" * 24, "method": "POST", "path": "/t/echo", "ctype": "text/plain",
              "body": base64.b64encode("中文本文".encode()).decode()})
    case("POST：本文與型別照轉、狀態碼照回", fr[0]["status"] == 201 and json.loads(data(fr)) == {"got": "中文本文", "ctype": "text/plain"}, fr)
    fr = run({"id": "c" * 24, "method": "GET", "path": "/t/big"})
    case("大的回應分片送（9 MB → 3 片，編號連續，只有最後一片 more=false）",
         [f["seq"] for f in fr] == [0, 1, 2] and [f["more"] for f in fr] == [True, True, False] and data(fr) == BIG,
         [(f["seq"], f["more"]) for f in fr])
    fr = run({"id": "d" * 24, "method": "GET", "path": "/t/stream"})
    case("串流：第一片帶 event-stream，內容完整，最後一片 more=false",
         "event-stream" in fr[0]["ctype"] and data(fr) == b"".join(f"data: {i}\n\n".encode() for i in range(10))
         and fr[-1]["more"] is False and all(f["more"] for f in fr[:-1]), len(fr))
    fr = run({"id": "e" * 24, "method": "GET", "path": "/t/x y"})
    case("路徑不合法：回 400，不送出去", fr[0]["status"] == 400, fr)
    fr = run({"id": "f" * 24, "method": "GET", "path": "/state", "timeout": 20})
    case("轉到代理原本的路由（/state）", fr[0]["status"] == 200 and "pools" in json.loads(data(fr)), fr[0]["status"])

    # ---- 4. 同一個編號不重做；做完再送來就重送上次的結果
    calls = []
    real = link.execute
    link.execute = lambda c, cmd: (calls.append(cmd["id"]), real(c, cmd))
    sent.clear()
    cid = "1" * 24
    link.dispatch(cfg, {"id": cid, "method": "GET", "path": "/t/json"})
    link.dispatch(cfg, {"id": cid, "method": "GET", "path": "/t/json"})
    time.sleep(1)
    case("同一個編號送兩次：只執行一次", calls == [cid], calls)
    n = len(sent)
    link.dispatch(cfg, {"id": cid, "method": "GET", "path": "/t/json"})
    time.sleep(0.5)
    case("  做完後又送來：重送上次的結果，不重做", calls == [cid] and len(sent) == n + 1 and sent[-1]["id"] == cid, (calls, len(sent)))
    case("  seen 帶著這個編號", cid in link.seen())
    link.dispatch(cfg, {"id": "zz", "path": "/t/json"})
    case("編號格式不對：不理", calls == [cid])

    # ---- 5. 取消：本機那條連線被關掉，執行緒很快結束並送最後一片
    H.broke = False
    sent.clear()
    cid = "2" * 24
    t0 = time.time()
    link.dispatch(cfg, {"id": cid, "method": "GET", "path": "/t/stream-long", "timeout": 60})
    time.sleep(0.8)
    link.dispatch(cfg, {"id": "3" * 24, "cancel": cid})
    for _ in range(50):
        if sent and sent[-1]["more"] is False:
            break
        time.sleep(0.1)
    case("取消：很快結束並送出最後一片", sent and sent[-1]["more"] is False and time.time() - t0 < 6, time.time() - t0)
    time.sleep(0.5)
    case("  本機的串流那一端也斷了（模型服務會跟著停）", H.broke)
    case("  取消指令本身也記進 seen", "3" * 24 in link.seen())

    # ---- 6. 結果送不回主控台：不卡住、照樣收尾
    link.send = lambda c, fr: False
    link.execute = real
    t0 = time.time()
    link.execute(cfg, {"id": "4" * 24, "method": "GET", "path": "/t/json", "timeout": 10})
    case("結果送不回去：執行緒照樣結束", time.time() - t0 < 5 and "4" * 24 not in link.inflight)

    print("全部通過" if OK else "有項目失敗")
    srv.shutdown()
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
