"""對話紀錄：驗證、加密落地、搜尋。伺服器只負責接線（xconsole_server.py 的 /api/chats*）。

存法：一則對話一列，訊息整包 JSON 以 AES-256-GCM 加密（enc:v1:）後存進 SQLite；
使用者附的圖拆出來另存（同樣加密），訊息裡只留 "blob:<id>"，列表與搜尋不必把圖解開。
搜尋要解密才比得到內容：每人最多 500 則，逐則解開比對的成本可以接受（換來內容不以明文落地）。
"""
import base64
import binascii
import json
import re
import secrets

CHAT_ID_RX = re.compile(r"[0-9a-f]{12}")
BLOB_ID_RX = re.compile(r"[0-9a-f]{16}")
DATA_URL_RX = re.compile(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)")
NODE_RX = re.compile(r"[\w.-]{1,64}")
JOB_RX = re.compile(r"[0-9a-f]{8}")
IMG_MAX = 6 * 1024 * 1024          # 單張附圖解碼後上限
CHAT_MAX = 32 * 1024 * 1024        # 一則對話整包（不含附圖）上限
TEXT_MAX = 400_000
DOC_MAX = 220_000
MAX_MSGS = 400
KEEP_PER_USER = 500

# 對話設定：鍵、型別、範圍。第三方 API 只轉送 OPENAI_STD 這幾個（其他是 llama.cpp 的擴充，送過去可能被拒）
SETTINGS = {
    "temperature": (float, 0, 2), "top_p": (float, 0, 1), "top_k": (int, 0, 1000), "min_p": (float, 0, 1),
    "repeat_penalty": (float, 0.5, 2), "presence_penalty": (float, -2, 2), "max_tokens": (int, 1, 131072),
    "seed": (int, -1, 2 ** 31 - 1),
}
OPENAI_STD = ("temperature", "top_p", "presence_penalty", "max_tokens", "seed")


class Bad(ValueError):
    pass


def _s(v, limit, name):
    if v is None:
        return None
    if not isinstance(v, str):
        raise Bad(f"{name} 要是文字")
    if len(v) > limit:
        raise Bad(f"{name} 太長（上限 {limit:,} 字）")
    return v


def sampling(body, only=None):
    """從請求本文挑出取樣參數並檢查範圍。回 dict（只含有給的鍵）；範圍不對丟 Bad。
    only：只收這幾個鍵（第三方 API 用 OPENAI_STD）。"""
    out = {}
    for k, (typ, lo, hi) in SETTINGS.items():
        if only is not None and k not in only:
            continue
        v = body.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            raise Bad(f"{k} 要是數字")
        try:
            v = typ(v) if typ is float else int(v) if float(v) == int(float(v)) else None
        except (TypeError, ValueError, OverflowError):
            v = None
        if v is None:
            raise Bad(f"{k} 要是{'整數' if typ is int else '數字'}")
        if not lo <= v <= hi:
            raise Bad(f"{k} 要在 {lo}～{hi} 之間")
        out[k] = v
    return out


def clean_settings(st):
    if st is None:
        return {}
    if not isinstance(st, dict):
        raise Bad("對話設定格式不對")
    out = sampling(st)
    sysp = _s(st.get("system"), 8000, "系統提示詞")
    if sysp:
        out["system"] = sysp
    if st.get("tools") is True:
        out["tools"] = True
    return out


def _clean_alt(a):
    if not isinstance(a, dict):
        raise Bad("回答版本格式不對")
    return {k: v for k, v in (("content", _s(a.get("content"), TEXT_MAX, "回答")),
                              ("reasoning", _s(a.get("reasoning"), TEXT_MAX, "思考過程")),
                              ("stat", _s(a.get("stat"), 300, "統計"))) if v is not None}


def _clean_gen(g):
    if not isinstance(g, dict):
        raise Bad("畫圖結果格式不對")
    node, job = g.get("node"), g.get("job")
    if not isinstance(node, str) or not NODE_RX.fullmatch(node):
        raise Bad("畫圖結果的機器不對")
    if job is not None and (not isinstance(job, str) or not JOB_RX.fullmatch(job)):
        raise Bad("畫圖結果的工作編號不對")
    kind = g.get("kind")
    if kind not in ("image", "video"):
        raise Bad("畫圖結果的種類不對")
    count = g.get("count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 4:
        raise Bad("張數不對")
    out = {"node": node, "kind": kind, "count": count}
    if job:
        out["job"] = job
    for k, lim in (("prompt", 2000), ("model", 120), ("title", 200), ("state", 20), ("error", 1000), ("asked", 2000)):
        v = _s(g.get(k), lim, k)
        if v:
            out[k] = v
    if g.get("edit") is True:
        out["edit"] = True
    return out


def clean_msgs(msgs):
    """回 (乾淨的訊息, 新附圖 [(位置, mime, bytes)])。新附圖先占位，存好拿到 id 再填回去。"""
    if not isinstance(msgs, list) or len(msgs) > MAX_MSGS:
        raise Bad(f"訊息格式不對（最多 {MAX_MSGS} 則）")
    out, new = [], []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            raise Bad("訊息格式不對")
        c = {"role": m["role"], "content": _s(m.get("content"), TEXT_MAX, "訊息") or ""}
        for k, lim in (("reasoning", TEXT_MAX), ("stat", 300), ("error", 1000)):
            v = _s(m.get(k), lim, k)
            if v:
                c[k] = v
        img = m.get("image")
        if isinstance(img, str) and img:
            if img.startswith("blob:") and BLOB_ID_RX.fullmatch(img[5:]):
                c["image"] = img
            else:
                mt = DATA_URL_RX.fullmatch(img)
                if not mt:
                    raise Bad("附圖只收 PNG、JPEG、WebP")
                try:
                    raw = base64.b64decode(mt.group(2), validate=False)
                except (binascii.Error, ValueError) as exc:
                    raise Bad("附圖內容壞了") from exc
                if len(raw) > IMG_MAX:
                    raise Bad("附圖最大 6 MB")
                new.append((i, mt.group(1), raw))
                c["image"] = None
        elif img:
            raise Bad("附圖格式不對")
        if m.get("image_lost") is True:
            c["image_lost"] = True
        docs = m.get("docs")
        if docs:
            if not isinstance(docs, list) or len(docs) > 5:
                raise Bad("一則最多附 5 個檔案")
            c["docs"] = []
            for d in docs:
                if not isinstance(d, dict):
                    raise Bad("附件格式不對")
                dd = {"name": _s(d.get("name"), 200, "附件名稱") or "附件"}
                t = _s(d.get("text"), DOC_MAX, "附件內容")
                if t:
                    dd["text"] = t
                for k in ("truncated", "lost"):
                    if d.get(k) is True:
                        dd[k] = True
                ch = d.get("chars")
                if isinstance(ch, int) and not isinstance(ch, bool) and ch >= 0:
                    dd["chars"] = ch
                c["docs"].append(dd)
        alts = m.get("alts")
        if alts:
            if not isinstance(alts, list) or len(alts) > 10:
                raise Bad("回答版本最多 10 個")
            c["alts"] = [_clean_alt(a) for a in alts]
            alt = m.get("alt", len(c["alts"]) - 1)
            c["alt"] = alt if isinstance(alt, int) and not isinstance(alt, bool) and 0 <= alt < len(c["alts"]) else len(c["alts"]) - 1
        if m.get("gen"):
            c["gen"] = _clean_gen(m["gen"])
        out.append(c)
    return out, new


def clean_summary(sm):
    if not sm:
        return None
    if not isinstance(sm, dict):
        raise Bad("摘要格式不對")
    upto = sm.get("upto")
    if not isinstance(upto, int) or isinstance(upto, bool) or upto < 0:
        raise Bad("摘要格式不對")
    return {"text": _s(sm.get("text"), 60_000, "摘要") or "", "upto": upto}


def save(store, seal, user, body):
    """存一則對話。回 (碼, 結果)。新附圖存成獨立的加密 blob，訊息裡換成 "blob:<id>"。"""
    try:
        cid = body.get("id")
        if cid is None:
            cid = secrets.token_hex(6)
        elif not isinstance(cid, str) or not CHAT_ID_RX.fullmatch(cid):
            raise Bad("對話編號不對")
        title = (_s(body.get("title"), 200, "標題") or "新對話").strip()[:80] or "新對話"
        target = _s(body.get("target"), 200, "模型") or ""
        msgs, new = clean_msgs(body.get("msgs") or [])
        settings = clean_settings(body.get("settings"))
        summary = clean_summary(body.get("summary"))
    except Bad as exc:
        return 400, {"error": str(exc)}
    owner = store.chat_owner(cid)
    if owner is not None and owner != user:
        return 404, {"error": "沒有這則對話"}
    have = store.blob_ids(user, cid)
    old_refs = {m["image"][5:] for m in msgs if isinstance(m.get("image"), str)}
    if old_refs - have:
        return 400, {"error": "訊息引用了不存在的附圖"}
    for i, mime, raw in new:
        bid = secrets.token_hex(8)
        store.blob_put(user, cid, bid, mime, seal(raw))
        msgs[i]["image"] = "blob:" + bid
    refs = {m["image"][5:] for m in msgs if isinstance(m.get("image"), str)}
    doc = {"msgs": msgs, "settings": settings, **({"summary": summary} if summary else {})}
    raw = json.dumps(doc, ensure_ascii=False).encode()
    if len(raw) > CHAT_MAX:
        return 413, {"error": "這則對話太大了（上限 32 MB）；請開新對話"}
    if not store.chat_put(user, {"id": cid, "title": title, "target": target, "n_msgs": len(msgs), "body": seal(raw)},
                          keep=KEEP_PER_USER):
        return 404, {"error": "沒有這則對話"}
    store.blob_gc(user, cid, refs)
    row = store.chat_get(user, cid)
    return 200, {"id": cid, "title": title, "updated": row["updated"], "created": row["created"],
                 "images": {str(i): msgs[i]["image"] for i, _, _ in new}}


def load(store, unseal, user, cid):
    if not isinstance(cid, str) or not CHAT_ID_RX.fullmatch(cid):
        return 404, {"error": "沒有這則對話"}
    row = store.chat_get(user, cid)
    if not row:
        return 404, {"error": "沒有這則對話"}
    doc = json.loads(unseal(row["body"])) if row["body"] else {"msgs": []}
    return 200, {k: row[k] for k in ("id", "title", "target", "created", "updated")} | doc


def _snippet(text, q, width=60):
    i = text.lower().find(q)
    if i < 0:
        return None
    a = max(0, i - width // 2)
    return ("…" if a else "") + text[a:a + width].replace("\n", " ") + ("…" if a + width < len(text) else "")


def listing(store, unseal, user, q=""):
    q = (q or "").strip().lower()[:100]
    rows = store.chat_list(user, with_body=bool(q))
    out = []
    for r in rows:
        item = {k: r[k] for k in ("id", "title", "target", "n_msgs", "created", "updated")}
        if q:
            hit = q in (r["title"] or "").lower()
            snip = None
            if r.get("body"):
                try:
                    doc = json.loads(unseal(r["body"]))
                except Exception:  # noqa: BLE001  # 壞掉的一則不擋整個列表
                    doc = {"msgs": []}
                for m in doc.get("msgs", []):
                    texts = [m.get("content") or ""] + [d.get("name") or "" for d in m.get("docs", [])]
                    for t in texts:
                        snip = _snippet(t, q)
                        if snip:
                            break
                    if snip:
                        break
            if not hit and not snip:
                continue
            if snip:
                item["snippet"] = snip
        out.append(item)
    return {"items": out, "limit": KEEP_PER_USER}


def blob(store, unseal, user, bid):
    if not isinstance(bid, str) or not BLOB_ID_RX.fullmatch(bid):
        return None
    b = store.blob_get(user, bid)
    if not b:
        return None
    return b["mime"], unseal(b["data"])
