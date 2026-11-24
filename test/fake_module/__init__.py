"""合約測試用的假企業版模組（test/test_module_contract.py在執行時用臨時金鑰簽章後載入）。

擴充點各用一次：網址裡的群組、本文裡的群組、模組自己的資料、只限管理全部群組的全域設定、前端檔、稽核。
"""
G = r"(?P<room_id>[0-9a-f]{8})"


def register(api):
    notes = api.store("ent").table("notes")
    tpl = api.store("ent").table("templates")

    def room(req):
        return {"room": req.group_id, "user": req.user, "role": req.role}

    def add_note(req):
        notes.put(req.body["id"], {"text": req.body.get("text", "")}, req.group_id)
        api.audit("note.add", req.body["id"], True, {"chars": len(req.body.get("text", "")), "password": "不該進稽核"})
        return {"ok": True}

    def list_notes(req):
        return {"items": notes.list()}

    def put_note_anywhere(req):
        # 模組故意試著寫到本文指定的「別的」群組：核心的資料表要擋下（範圍外回404）
        notes.put("sneak", {"text": "x"}, req.body.get("other"))
        return {"ok": True}

    def template(req):
        return {"tpl": tpl.get(req.params["tid"]), "group": req.group_id}

    def seed(req):
        for rid, gid in (req.body or {}).get("templates", {}).items():
            tpl.put(rid, {"name": rid}, gid)
        return {"ok": True}

    def license_info(req):
        return {"license": "ok"}

    def boom(req):
        raise RuntimeError("模組裡的錯誤：內部路徑 /secret/path")

    api.route("GET", f"/api/ent/rooms/{G}", room, role="viewer", scope=api.scope.path("room_id"))
    api.route("POST", "/api/ent/notes", add_note, role="operator", scope=api.scope.body("group_id"))
    api.route("GET", f"/api/ent/rooms/{G}/notes", list_notes, role="viewer", scope=api.scope.path("room_id"))
    api.route("POST", "/api/ent/sneak", put_note_anywhere, role="operator", scope=api.scope.body("group_id"))
    api.route("GET", r"/api/ent/templates/(?P<tid>[\w-]{1,40})", template, role="viewer", scope=api.scope.obj("templates", "tid"))
    api.route("POST", "/api/ent/seed", seed, role="admin", scope=api.scope.GLOBAL)
    api.route("GET", "/api/ent/license", license_info, role="admin", scope=api.scope.GLOBAL)
    api.route("GET", "/api/ent/boom", boom, role="viewer", scope=api.scope.GLOBAL)
    api.static("static")
