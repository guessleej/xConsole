"""對話「工具」裡跟外面網路有關的兩件事：讀網頁、聯網搜尋。主控台只負責接線（xconsole_server.py的/api/assistant）。

讀網頁：網址是模型給的（可能被網頁內容誘導），一律擋掉內網、本機、雲端中繼資料這類位址——
不然一句「去讀http://127.0.0.1:9400/…」或「http://169.254.169.254/」就能讓主控台替人去敲內網。
DNS解析一次、檢查每一個位址、再直接連那個位址（不讓第二次解析換成內網，DNS rebinding），轉址每一跳都重新檢查。
聯網搜尋：管理者在設定填的搜尋端點（SearXNG相容的JSON），沒設就不給模型這個工具。
"""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.parse
from html.parser import HTMLParser

FETCH_MAX = 3 * 1024 * 1024           # 一頁最多讀3 MB
TEXT_MAX = 20000                       # 回給模型的字數
REDIRECTS = 4
TEXT_TYPES = ("text/", "application/json", "application/xhtml", "application/xml", "application/rss", "application/atom")
UA = "Mozilla/5.0 (compatible; xConsole)"


class FetchError(Exception):
    pass


def ip_ok(ip):
    """只有公開網路的位址可以連：私有、本機、連結本機（含雲端中繼資料169.254.169.254）、保留、群播都不行。"""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def resolve(host, port, allow=ip_ok):
    """回一個檢查過的位址；任何一個解析結果是內網就整個拒絕（不挑一個公開的來用，免得輪詢DNS混進內網）。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise FetchError(f"找不到{host}") from exc
    ips = [i[4][0] for i in infos]
    if not ips:
        raise FetchError(f"找不到{host}")
    bad = [ip for ip in ips if not allow(ip)]
    if bad:
        raise FetchError(f"{host}指向內網或本機位址（{bad[0]}），為了安全不讀")
    return ips[0]


class _PinnedHTTP(http.client.HTTPConnection):
    """連到已檢查過的位址（不再解析一次）。"""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """連到已檢查過的位址，憑證照原本的主機名稱驗。"""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _open(url, timeout, allow=ip_ok):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError("只能讀http://或https://的網頁")
    if u.username or u.password:
        raise FetchError("網址不能帶帳號密碼")
    tls = u.scheme == "https"
    try:
        port = u.port or (443 if tls else 80)
    except ValueError as exc:
        raise FetchError("網址的埠不對") from exc
    ip = resolve(u.hostname, port, allow)
    conn = (_PinnedHTTPS if tls else _PinnedHTTP)(u.hostname, ip, port, timeout)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    try:
        conn.request("GET", path, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                                           "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
        return conn, conn.getresponse()
    except ssl.SSLError as exc:
        conn.close()
        raise FetchError(f"{u.hostname}的憑證驗證不過") from exc
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        raise FetchError(f"連不到{u.hostname}：{type(exc).__name__}") from exc


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "blockquote", "td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip:
            self.out.append(data)


def html_text(html):
    """HTML → (標題, 純文字)。空白收斂、連續空行合併。"""
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001  # 壞掉的HTML也盡量拿到字
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    text = re.sub(r"\n\s*\n+", "\n\n", "\n".join(x.strip() for x in text.split("\n"))).strip()
    return p.title.strip()[:300], text


def fetch(url, timeout=15, allow=ip_ok):
    """讀一頁網頁，回{url, title, text, truncated}。失敗丟FetchError（訊息可以直接給模型與畫面看）。"""
    url = str(url or "").strip()
    if len(url) > 2000:
        raise FetchError("網址太長")
    for _ in range(REDIRECTS + 1):
        conn, resp = _open(url, timeout, allow)
        try:
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location") or ""
                if not loc:
                    raise FetchError(f"對方回{resp.status}轉址但沒有新網址")
                url = urllib.parse.urljoin(url, loc)
                continue
            if resp.status >= 400:
                raise FetchError(f"對方回{resp.status}")
            ctype = (resp.getheader("Content-Type") or "").lower()
            if ctype and not ctype.startswith(TEXT_TYPES):
                raise FetchError(f"這不是網頁（{ctype.split(';')[0]}），讀不了")
            raw = resp.read(FETCH_MAX + 1)
        finally:
            conn.close()
        cut = len(raw) > FETCH_MAX
        raw = raw[:FETCH_MAX]
        m = re.search(r"charset=([\w-]+)", ctype) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
        enc = (m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)) if m else "utf-8"
        try:
            body = raw.decode(enc, "replace")
        except LookupError:
            body = raw.decode("utf-8", "replace")
        title, text = html_text(body) if ("html" in ctype or "<html" in body[:2000].lower()) else ("", body.strip())
        return {"url": url, "title": title, "text": text[:TEXT_MAX], "truncated": cut or len(text) > TEXT_MAX}
    raise FetchError("轉址太多次")


# ---------------------------------------------------------------- 聯網搜尋
def search_config_ok(url):
    """搜尋端點：https://或http://的網址，裡面要有{q}（換成搜尋字）。"""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname) and "{q}" in url and len(url) <= 500 and not u.username


def parse_results(d, limit=8):
    """SearXNG（results[]）與Brave類（web.results[]）的JSON都收：title、url、content／description／snippet。"""
    rows = (d.get("results") if isinstance(d, dict) else None) or ((d.get("web") or {}).get("results") if isinstance(d, dict) else None) or []
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("url"):
            continue
        out.append({"title": str(r.get("title") or "")[:200], "url": str(r["url"])[:500],
                    "snippet": str(r.get("content") or r.get("description") or r.get("snippet") or "")[:400]})
        if len(out) >= limit:
            break
    return out


def search(cfg, query, key="", timeout=20):
    """cfg：{"url": 含{q}的網址, "header": 送金鑰用的標頭名稱}。搜尋端點是管理者自己設的（常是內網的SearXNG），不擋內網。"""
    import urllib.request
    q = str(query or "").strip()[:300]
    if not q:
        raise FetchError("搜尋字是空的")
    url = cfg["url"].replace("{q}", urllib.parse.quote(q))
    h = {"User-Agent": UA, "Accept": "application/json"}
    if key:
        hn = cfg.get("header") or "Authorization"
        h[hn] = f"Bearer {key}" if hn.lower() == "authorization" else key
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(FETCH_MAX + 1)
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"搜尋服務連不上或回錯誤：{type(exc).__name__}") from exc
    try:
        return parse_results(json.loads(raw[:FETCH_MAX]))
    except ValueError as exc:
        raise FetchError("搜尋服務回的不是JSON（SearXNG要開format=json）") from exc
