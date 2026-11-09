"""代理通道專用憑證（不和管理網頁共用）與輪替。

管理者在網頁上換管理網頁的憑證時，這裡完全不動，主動連回的工作站就不會一起斷線。

- 通道另外聽一個埠（預設 9402），只回 /api/agent/poll 與 /api/agent/result，對外只開這個埠就好。
- 憑證是主控台自己產的自簽 EC P-256。代理加入時拿到、釘住整張憑證的 SHA-256，之後只認釘住的那幾張。
- 代理握手時用 SNI 說它認的是哪一張（tag(fp)），主控台照著送那一張。所以輪替期間新舊兩張可以同時服務：
  1. rotate()：產下一張（next），版本加一。代理下一次輪詢時，主控台發現它的版本比較舊，就在回應裡帶新的憑證清單。
  2. 代理存好以後，輪詢時回報新版本。所有在線的機器都回報了，check() 才把 next 換成 current。
  3. 舊的改成 retired，再保留 30 天：這段時間沒上線的機器用舊的 SNI 照樣連得上，連上就拿到新清單。
     超過 30 天才上線的機器握手會失敗，要重新加入。
- 私鑰檔用隨機密碼加密（PKCS#8），密碼本身以 AES-256-GCM 封存在 ring.json（tls.seal）；磁碟上沒有明文私鑰。
"""
import datetime as dt
import json
import os
import secrets
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import tls

RETAIN = 30 * 86400
SNI_SUFFIX = ".link.xconsole"


def tag(fp):
    """代理握手時放在 SNI 的名字：說它認的是哪一張。"""
    return "k-" + fp[:32] + SNI_SUFFIX


def _new_cert(days=3650):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "xCloudinfo"),
                      x509.NameAttribute(NameOID.COMMON_NAME, "xconsole-link")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days))
            .sign(key, hashes.SHA256()))
    return key, cert


class Ring:
    def __init__(self, state_dir):
        self.p = tls.Paths(state_dir)          # 借用它的主金鑰（tls.seal）
        self.dir = os.path.join(state_dir, "link")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.file = os.path.join(self.dir, "ring.json")
        self.lock = threading.RLock()
        self.acked = {}                        # 節點 → 它回報的憑證版本（記憶體就好：重開後輪詢會再報）
        self.ctx = None
        self._by_tag = {}
        with self.lock:
            self.d = tls._load_json(self.file, None) or {"v": 0, "keys": []}
            if not any(k["state"] == "current" for k in self.d["keys"]):
                self._add("current")
            self._expire()
            self._build()

    # ---- 內部
    def _path(self, fp):
        return os.path.join(self.dir, f"key-{fp[:16]}.pem")

    def _add(self, state):
        key, cert = _new_cert()
        pw = secrets.token_bytes(32)
        fp = cert.fingerprint(hashes.SHA256()).hex()
        tls._write(self._path(fp), key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.BestAvailableEncryption(pw)))
        self.d["v"] += 1
        self.d["keys"].append({"fp": fp, "state": state, "since": self.d["v"], "created": int(time.time()),
                               "cert": cert.public_bytes(serialization.Encoding.PEM).decode(),
                               "pw": tls.seal(self.p, pw)})
        self._save()

    def _save(self):
        tls._write(self.file, json.dumps(self.d, ensure_ascii=False))

    def _expire(self):
        now, keep = time.time(), []
        for k in self.d["keys"]:
            if k["state"] == "retired" and k.get("retire_at", 0) <= now:
                try:
                    os.remove(self._path(k["fp"]))
                except OSError:
                    pass
                continue
            keep.append(k)
        if len(keep) != len(self.d["keys"]):
            self.d["keys"] = keep
            self.d["v"] += 1
            self._save()
            return True
        return False

    def _build(self):
        ctxs = {}
        for k in self.d["keys"]:
            c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            c.minimum_version = ssl.TLSVersion.TLSv1_2
            cp = os.path.join(self.dir, f"cert-{k['fp'][:16]}.pem")
            tls._write(cp, k["cert"], 0o644)
            c.load_cert_chain(cp, self._path(k["fp"]), password=tls.unseal(self.p, k["pw"]))
            ctxs[k["fp"]] = c
        cur = self.current()["fp"]
        by_tag = {tag(fp): c for fp, c in ctxs.items()}
        ctx = ctxs[cur]
        ctx.sni_callback = self._sni
        self._by_tag, self.ctx = by_tag, ctx    # 先換表再換預設：握手到一半的連線看到的永遠是完整的一組

    def _sni(self, sslobj, name, _ctx):
        c = self._by_tag.get(name or "")
        if c is not None and c is not sslobj.context:
            sslobj.context = c
        return None

    # ---- 查詢
    def current(self):
        return next(k for k in self.d["keys"] if k["state"] == "current")

    def pending(self):
        return next((k for k in self.d["keys"] if k["state"] == "next"), None)

    def version(self):
        return self.d["v"]

    def trusted(self):
        """發給代理的憑證：目前這張，加上輪替中的下一張。退役的不發：舊私鑰日後就算外洩，也冒充不了已更新的代理。"""
        ks = [self.current()] + ([self.pending()] if self.pending() else [])
        return "".join(k["cert"] for k in ks), self.current()["fp"]

    def summary(self, push_nodes, online):
        """給設定頁看：每張的狀態，與輪替中還沒收到新憑證的在線機器。"""
        with self.lock:
            nx = self.pending()
            waiting = [n for n in push_nodes if online(n) and nx and self.acked.get(n, 0) < nx["since"]]
            return {"version": self.d["v"], "keys": [
                {"fingerprint256": k["fp"], "state": k["state"], "created": k["created"],
                 "retire_at": k.get("retire_at")} for k in self.d["keys"]],
                "rotating": bool(nx), "waiting": waiting, "retain_days": RETAIN // 86400}

    # ---- 動作
    def ack(self, node, v):
        """回 True 表示這台剛回報了比較新的版本（輪替中就該馬上看能不能換上）。"""
        with self.lock:
            if isinstance(v, int) and 0 < v <= self.d["v"] and self.acked.get(node) != v:
                self.acked[node] = v
                return True
            return False

    def rotate(self):
        with self.lock:
            if self.pending():
                return False, "上一次換通道憑證還沒完成（有在線的機器還沒收到新的）"
            self._add("next")
            self._build()
            return True, "已產生新的通道憑證，正在發給每台主動連回的機器；在線的都收到後自動換上"

    def check(self, push_nodes, online):
        """所有在線的機器都收到下一張了就換上；退役超過 30 天的刪掉。回 True 表示有變。"""
        with self.lock:
            changed = self._expire()
            nx = self.pending()
            if nx and all(self.acked.get(n, 0) >= nx["since"] for n in push_nodes if online(n)):
                cur = self.current()
                cur["state"], cur["retire_at"] = "retired", int(time.time()) + RETAIN
                nx["state"] = "current"
                self.d["v"] += 1
                self._save()
                changed = True
            if changed:
                self._build()
            return changed

    def forget(self, node):
        with self.lock:
            self.acked.pop(node, None)
