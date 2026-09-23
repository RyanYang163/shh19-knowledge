"""应用装配：路由注册、任务注册、状态上报。

路由约定（来自 ``tnasapp.server``，不是自创）：

* 一律注册成 ``/api/...``；框架会自动剥掉 ``/``、``/<appid>``、``/v2/proxy/<appid>``
  三种前缀，所以前端用相对路径即可。
* ``<kb_id>`` 这类占位符编译成 ``[^/]+``，**所以文件路径不能做路径参数**
  （路径里有 ``/``），一律走查询参数 ``?path=``。
* **首匹配即胜**，字面量路由要注册在占位符路由之前。
* 鉴权沿用框架默认的 ``lenient`` 模式，不自造第二套。
* 失败一律返回 ``Response.error(可读原因, 状态码, 下一步建议)``。
"""

import os
import time

from tnasapp import fsapi, logx
from tnasapp import server as srv
from tnasapp.paths import human_size

from . import (config, db, extract_office, extract_text, iconstore, kbs, library,
               rangeio, render, scanner, search, security)
from .extract import read_text_file


class KnowledgeApp(srv.App):
    """默认设置取自 ``config.DEFAULT_SETTINGS``。"""

    def default_settings(self):
        return dict(config.DEFAULT_SETTINGS)


def _int_arg(request, name, default=0, low=None, high=None):
    value = request.int_arg(name, default)
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _fail(exc):
    """把 :class:`kbs.KbError` 变成可读错误响应；其它异常交给框架兜底。"""
    if isinstance(exc, kbs.KbError):
        return srv.Response.error(str(exc), exc.status, exc.hint)
    raise exc


def _require_path(request):
    path = request.arg("path")
    if not path:
        raise kbs.KbError("缺少 path 参数", 400, "请从文件树里选择文件后重试。")
    return path


def _resolve(app, request, expect_file=None):
    """解析请求里的路径：给了 ``kb`` 就按该库校验，否则按路径反查所属知识库。"""
    path = _require_path(request)
    kb_id = request.arg("kb")
    if kb_id not in (None, "", "0"):
        kb = kbs.require_kb(app, kb_id)
        return kb, kbs.resolve_in_kb(app, kb_id, path, expect_file=expect_file)
    return kbs.resolve_any_kb(app, path, expect_file=expect_file)


# ---------------------------------------------------------------- 知识库

def _register_kb_routes(app):
    @app.get("/api/kb/list")
    def _kb_list(request):
        return srv.Response.json({
            "ok": True,
            "knowledge_bases": kbs.list_kbs(app, with_stats=request.bool_arg("stats", True)),
            "allowed_roots": app.allowed.roots(),
        })

    @app.post("/api/kb/create")
    def _kb_create(request):
        body = request.json_body() or {}
        if not isinstance(body, dict):
            return srv.Response.error("请求体必须是 JSON 对象", 400)
        # 先把目录加进白名单再建库：这样「选了一个还没授权的目录」只需一次操作，
        # 而不是让用户先去设置页加白名单、再回来建库（两步都容易失败）
        root = body.get("root_path")
        if root and not app.allowed.is_allowed(root):
            try:
                app.set_allowed_roots(app.allowed.roots() + [root])
            except Exception:  # noqa: BLE001 —— 加不进去由下面的校验给出原因
                pass
        try:
            kb = kbs.create_kb(
                app, body.get("name"), root,
                description=body.get("description") or "",
                icon_type=body.get("icon_type") or "builtin",
                icon_value=body.get("icon_value") or "book",
                accent=body.get("accent") or "")
        except kbs.KbError as exc:
            return _fail(exc)
        # 建完立刻投一次扫描，用户不必再点一次
        job = app.jobs.submit("scan", {"kb": kb["id"]},
                              title="扫描「%s」" % kb["name"], max_attempts=1)
        return srv.Response.json({"ok": True, "kb": kb, "job": job}, status=201)

    @app.get("/api/kb/<kb_id>")
    def _kb_get(request, kb_id):
        try:
            kb = kbs.require_kb(app, kb_id)
        except kbs.KbError as exc:
            return _fail(exc)
        return srv.Response.json({"ok": True, "kb": kb,
                                  "stats": kbs.kb_stats(app, kb_id)})

    @app.post("/api/kb/<kb_id>/update")
    def _kb_update(request, kb_id):
        body = request.json_body() or {}
        if not isinstance(body, dict):
            return srv.Response.error("请求体必须是 JSON 对象", 400)
        try:
            kb = kbs.update_kb(app, kb_id, **body)
        except kbs.KbError as exc:
            return _fail(exc)
        return srv.Response.json({"ok": True, "kb": kb})

    @app.post("/api/kb/<kb_id>/delete")
    def _kb_delete(request, kb_id):
        body = request.json_body() or {}
        try:
            kb = kbs.require_kb(app, kb_id)
        except kbs.KbError as exc:
            return _fail(exc)
        # 二次确认：必须逐字输入知识库名。破坏性操作不能只靠一次点击
        if not isinstance(body, dict) or body.get("confirm") != kb["name"]:
            return srv.Response.error(
                "请确认要注销这个知识库", 400,
                "需要在 confirm 字段里逐字填写知识库名称「%s」。" % kb["name"])
        return srv.Response.json({"ok": True, "result": kbs.delete_kb(app, kb_id)})

    @app.post("/api/kb/<kb_id>/scan")
    def _kb_scan(request, kb_id):
        body = request.json_body() or {}
        force = bool(isinstance(body, dict) and body.get("force"))
        try:
            kb = kbs.require_kb(app, kb_id)
        except kbs.KbError as exc:
            return _fail(exc)
        job = app.jobs.submit(
            "scan", {"kb": int(kb_id), "force": force},
            title="%s「%s」" % ("强制重建索引" if force else "扫描", kb["name"]),
            max_attempts=1)
        return srv.Response.json({"ok": True, "job": job}, status=201)

    @app.get("/api/kb/<kb_id>/tree")
    def _kb_tree(request, kb_id):
        try:
            kb = kbs.require_kb(app, kb_id)
        except kbs.KbError as exc:
            return _fail(exc)
        return srv.Response.json(_tree_payload(app, kb, request))

    @app.get("/api/kb/<kb_id>/stats")
    def _kb_stats(request, kb_id):
        try:
            return srv.Response.json({"ok": True, "stats": kbs.kb_stats(app, kb_id)})
        except kbs.KbError as exc:
            return _fail(exc)

    @app.get("/api/kb/<kb_id>/types")
    def _kb_types(request, kb_id):
        try:
            kbs.require_kb(app, kb_id)
        except kbs.KbError as exc:
            return _fail(exc)
        rows = app.store.query(
            "SELECT kind, COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files"
            " WHERE kb_id=? AND is_deleted=0 AND is_dir=0"
            " GROUP BY kind ORDER BY n DESC", (int(kb_id),))
        return srv.Response.json({
            "ok": True,
            "types": [{"kind": row["kind"], "count": int(row["n"]),
                       "bytes": int(row["b"]),
                       "icon": config.ICON_BY_KIND.get(row["kind"], "file")}
                      for row in rows]})

    @app.get("/api/kb/<kb_id>/timeline")
    def _kb_timeline(request, kb_id):
        try:
            kbs.require_kb(app, kb_id)
        except kbs.KbError as exc:
            return _fail(exc)
        bucket = request.arg("bucket", "month")
        fmt = {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}.get(bucket, "%Y-%m")
        rows = app.store.query(
            "SELECT strftime(?, mtime_ns/1000000000, 'unixepoch', 'localtime') AS bucket,"
            " COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files"
            " WHERE kb_id=? AND is_deleted=0 AND is_dir=0"
            " GROUP BY bucket ORDER BY bucket DESC LIMIT 200", (fmt, int(kb_id)))
        return srv.Response.json({
            "ok": True, "bucket": bucket,
            "buckets": [{"bucket": row["bucket"], "count": int(row["n"]),
                         "bytes": int(row["b"])} for row in rows]})

    @app.get("/api/kb/<kb_id>/related")
    def _kb_related(request, kb_id):
        path = request.arg("path")
        if not path:
            return srv.Response.error("缺少 path 参数", 400)
        try:
            _kb, real = kbs.resolve_in_kb(app, kb_id, path)
        except kbs.KbError as exc:
            return _fail(exc)
        return srv.Response.json({
            "ok": True,
            "related": search.related(app, real, kb_id=kb_id,
                                      limit=_int_arg(request, "limit", 12, 1, 60))})


def _tree_payload(app, kb, request):
    """文件树的一层。**完全读索引，不扫盘** —— 这是「展开 <100ms」的实现方式。"""
    parent = (request.arg("parent") or "").strip("/")
    limit = _int_arg(request, "limit", 2000, 1, 5000)
    sort = request.arg("sort", "name")
    dirs_first = request.bool_arg("dirs_first", True)

    order = {
        "name": "is_dir DESC, name COLLATE NOCASE",
        "mtime": "is_dir DESC, mtime_ns DESC",
        "size": "is_dir DESC, size DESC",
    }.get(sort, "is_dir DESC, name COLLATE NOCASE")
    if not dirs_first:
        order = order.replace("is_dir DESC, ", "")

    rows = app.store.query(
        "SELECT id, path, rel_path, name, ext, kind, size, mtime_ns, is_dir,"
        " is_symlink, text_state, image_w, image_h, error"
        " FROM files WHERE kb_id=? AND parent_rel=? AND is_deleted=0"
        " ORDER BY %s LIMIT ?" % order, (int(kb["id"]), parent, limit))

    paths = [row["path"] for row in rows]
    icons = library.icons_for(app, paths)
    favorites = {row["path"] for row in app.store.query("SELECT path FROM favorites")}

    entries = []
    for row in rows:
        entry = dict(row)
        entry["is_dir"] = bool(row["is_dir"])
        entry["is_symlink"] = bool(row["is_symlink"])
        entry["icon"] = library.icon_for_file(
            app, row["path"], row["kind"], row["is_dir"], icons.get(row["path"]))
        entry["favorited"] = row["path"] in favorites
        if row["kind"] not in config.ICON_BY_KIND and not row["is_dir"]:
            entry["icon"] = {"type": "builtin", "value": "file"}
        entries.append(entry)

    total = app.store.query_one(
        "SELECT COUNT(*) AS n FROM files WHERE kb_id=? AND parent_rel=? AND is_deleted=0",
        (int(kb["id"]), parent))["n"]

    # 面包屑：从根一路到当前目录，供前端显示
    trail = []
    if parent:
        current = parent
        while current:
            item = app.store.query_one(
                "SELECT name FROM files WHERE kb_id=? AND rel_path=?",
                (int(kb["id"]), current))
            trail.append({"rel_path": current,
                          "name": (item or {}).get("name") or current.rsplit("/", 1)[-1]})
            current = current.rsplit("/", 1)[0] if "/" in current else ""
        trail.reverse()

    return {
        "ok": True,
        "kb_id": int(kb["id"]),
        "parent": parent,
        "parent_rel": (parent.rsplit("/", 1)[0] if "/" in parent else "") if parent else None,
        "breadcrumb": trail,
        "entries": entries,
        "total": int(total or 0),
        "has_more": len(entries) >= limit,
        "sort": sort,
        "roots": [{"rel_path": "", "name": kb["name"]}],
    }


# ---------------------------------------------------------------- 文件

def _register_file_routes(app):
    @app.get("/api/file/meta")
    def _file_meta(request):
        try:
            kb, real = _resolve(app, request, expect_file=None)
        except kbs.KbError as exc:
            return _fail(exc)

        is_dir = os.path.isdir(real)
        row = app.store.query_one("SELECT * FROM files WHERE kb_id=? AND path=?",
                                  (int(kb["id"]), real))
        text_row = db.text_get(app, row["id"]) if row else None
        icons = library.icons_for(app, [real])
        favorite = app.store.query_one("SELECT path FROM favorites WHERE path=?", (real,))
        units = []
        if row and not is_dir:
            units = [{"type": item["unit_type"], "no": item["unit_no"],
                      "label": item["label"]}
                     for item in app.store.query(
                         "SELECT unit_type, unit_no, label FROM doc_units"
                         " WHERE file_id=? ORDER BY unit_type, unit_no LIMIT 500",
                         (int(row["id"]),))]

        import mimetypes

        stat_result = os.stat(real)
        payload = {
            "ok": True,
            "path": real,
            "rel_path": kbs.relpath_in_kb(kb, real),
            "kb_id": int(kb["id"]),
            "kb_name": kb["name"],
            "name": os.path.basename(real) or real,
            "is_dir": is_dir,
            "ext": os.path.splitext(real)[1].lower(),
            "kind": (row or {}).get("kind") or ("dir" if is_dir else scanner.kind_for(real)),
            "size": stat_result.st_size,
            "mtime_ns": int(stat_result.st_mtime_ns),
            "mime": rangeio.guess_mime(real) if not is_dir
                    else "inode/directory",
            "favorited": bool(favorite),
            "icon": library.icon_for_file(
                app, real, (row or {}).get("kind") or "other", is_dir, icons.get(real)),
            "indexed": bool(row) and (row.get("text_state") == "ok"),
            "text_state": (row or {}).get("text_state") or "pending",
            "text_chars": (text_row or {}).get("chars") or 0,
            "encoding": (text_row or {}).get("encoding") or "",
            "truncated": bool((text_row or {}).get("truncated")),
            "note": _extraction_note(row, text_row),
            "units": units,
            "image_w": (row or {}).get("image_w") or 0,
            "image_h": (row or {}).get("image_h") or 0,
            "viewer": _viewer_for((row or {}).get("kind") if row else
                                  ("dir" if is_dir else scanner.kind_for(real))),
        }
        return srv.Response.json(payload)

    @app.get("/api/file/content")
    def _file_content(request):
        try:
            _kb, real = _resolve(app, request, expect_file=True)
        except kbs.KbError as exc:
            return _fail(exc)
        return rangeio.serve_content(app, request, real)

    @app.get("/api/file/raw")
    def _file_raw(request):
        try:
            _kb, real = _resolve(app, request, expect_file=True)
        except kbs.KbError as exc:
            return _fail(exc)
        return rangeio.serve_content(app, request, real,
                                     download_name=os.path.basename(real))

    @app.get("/api/file/view")
    def _file_view(request):
        """HTML 文件的沙箱预览：带严格 CSP，前端再用 sandbox iframe 包一层。"""
        try:
            _kb, real = _resolve(app, request, expect_file=True)
        except kbs.KbError as exc:
            return _fail(exc)
        if os.path.splitext(real)[1].lower() not in (".html", ".htm", ".xhtml"):
            return srv.Response.error("该接口只用于 HTML 文件", 400,
                                      "其它类型请用 /api/file/content。")
        return rangeio.serve_html_view(app, real)

    @app.get("/api/file/text")
    def _file_text(request):
        try:
            _kb, real = _resolve(app, request, expect_file=True)
        except kbs.KbError as exc:
            return _fail(exc)
        offset = _int_arg(request, "offset", 0, 0)
        limit = _int_arg(request, "limit", config.MAX_TEXT_PAGE, 1024,
                         config.MAX_TEXT_PAGE)
        payload = rangeio.read_text_window(real, offset=offset, limit=limit)
        return srv.Response.json(payload, status=200 if payload.get("ok") else 404)

    @app.get("/api/file/html")
    def _file_html(request):
        """服务端渲染好的 HTML（Markdown / DOCX）。**已转义，可安全内联。**"""
        try:
            kb, real = _resolve(app, request, expect_file=True)
        except kbs.KbError as exc:
            return _fail(exc)
        ext = os.path.splitext(real)[1].lower()
        kind = scanner.kind_for(real)

        if kind == "markdown":
            text, encoding, truncated = read_text_file(real, config.MAX_INDEX_BYTES)
            return srv.Response.json({
                "ok": True, "kind": "markdown", "encoding": encoding,
                "truncated": truncated, "html": render.markdown_to_html(text),
                "name": os.path.basename(real)})
        if kind == "docx":
            html_text, error = render.docx_to_html(real)
            if html_text is None:
                return srv.Response.error(
                    error or "无法渲染该文档", 415,
                    "可改用「下载」在本地打开，或用全文检索查看内容。")
            return srv.Response.json({"ok": True, "kind": "docx",
                                      "html": html_text,
                                      "name": os.path.basename(real)})
        if kind == "pptx":
            slides = extract_office.pptx_slides(real)
            if slides is None:
                return srv.Response.error("无法解析该演示文稿", 415,
                                          "可改用「下载」在本地打开。")
            return srv.Response.json({"ok": True, "kind": "pptx", "slides": [
                {"index": item["index"],
                 "title": security.escape(item["title"]),
                 "html": render.plain_to_html(item["text"])} for item in slides],
                "name": os.path.basename(real)})
        if kind in ("text", "code", "yaml", "xml", "json"):
            text, encoding, truncated = read_text_file(real, config.MAX_INDEX_BYTES)
            return srv.Response.json({
                "ok": True, "kind": kind, "encoding": encoding,
                "truncated": truncated, "text": text,
                "lang": scanner.lang_for(real), "name": os.path.basename(real)})
        return srv.Response.error("该格式不支持服务端渲染", 415,
                                  "请用 /api/file/content 直接交给浏览器显示。")

    @app.get("/api/file/sheet")
    def _file_sheet(request):
        try:
            _kb, real = _resolve(app, request, expect_file=True)
        except kbs.KbError as exc:
            return _fail(exc)
        offset = _int_arg(request, "offset", 0, 0)
        limit = _int_arg(request, "limit", 200, 1, config.MAX_SHEET_ROWS)
        kind = scanner.kind_for(real)
        if kind == "xlsx":
            payload = extract_office.read_sheet(
                real, sheet_index=_int_arg(request, "sheet", 0, 0), offset=offset,
                limit=limit)
        elif kind == "csv":
            payload = extract_text.csv_preview(real, offset=offset, limit=limit)
        else:
            return srv.Response.error("该接口只用于表格文件（xlsx / csv）", 400)
        if payload.get("error"):
            return srv.Response.error(payload["error"], 415,
                                      "可改用「下载」在本地打开。")
        payload["ok"] = True
        return srv.Response.json(payload)

    @app.get("/api/file/outline")
    def _file_outline(request):
        try:
            kb, real = _resolve(app, request, expect_file=True)
        except kbs.KbError as exc:
            return _fail(exc)
        row = app.store.query_one("SELECT id FROM files WHERE kb_id=? AND path=?",
                                  (int(kb["id"]), real))
        if not row:
            return srv.Response.json({"ok": True, "outline": [],
                                      "hint": "该文件尚未索引，暂无大纲。"})
        outline = [{"type": item["unit_type"], "no": item["unit_no"],
                    "label": item["label"],
                    "preview": (item["content"] or "")[:160]}
                   for item in app.store.query(
                       "SELECT unit_type, unit_no, label, content FROM doc_units"
                       " WHERE file_id=? ORDER BY unit_type, unit_no LIMIT 2000",
                       (int(row["id"]),))]
        return srv.Response.json({"ok": True, "outline": outline})

    @app.get("/api/file/related")
    def _file_related(request):
        try:
            kb, real = _resolve(app, request, expect_file=True)
        except kbs.KbError as exc:
            return _fail(exc)
        return srv.Response.json({
            "ok": True,
            "related": search.related(app, real, kb_id=kb["id"],
                                      limit=_int_arg(request, "limit", 12, 1, 60))})

    @app.post("/api/file/touch")
    def _file_touch(request):
        body = request.json_body() or {}
        path = (body or {}).get("path") if isinstance(body, dict) else None
        if not path:
            return srv.Response.error("缺少 path", 400)
        try:
            kb, real = kbs.resolve_any_kb(app, path, expect_file=True)
        except kbs.KbError as exc:
            return _fail(exc)
        return srv.Response.json(
            library.touch_recent(app, real, kb_id=kb["id"],
                                 name=os.path.basename(real)))


def _extraction_note(row, text_row):
    """把「为什么没有文本」讲清楚。用户看到空白面板时最需要的是一句原因。"""
    if not row:
        return "该文件尚未被扫描到，请重新扫描这个知识库。"
    state = row.get("text_state")
    if state == "ok":
        return None
    if state == "pending":
        return "该文件尚未建立索引，正在排队。索引完成后即可全文检索与显示内容。"
    if state == "empty":
        return "该文件没有可索引的文本内容（例如图片，或空文件）。"
    if state == "failed":
        return "建立索引时出错：%s" % (row.get("error") or "未知原因")
    if state == "unsupported":
        return ((text_row or {}).get("error")
                or row.get("error")
                or "该格式没有可提取的文本层，无法全文检索。仍可直接查看或下载。")
    return None


#: kind → 前端用哪个查看器
_VIEWERS = {
    "pdf": "pdf", "markdown": "markdown", "html": "html", "text": "text",
    "code": "code", "json": "json", "xml": "code", "yaml": "code",
    "csv": "sheet", "xlsx": "sheet", "docx": "doc", "pptx": "slides",
    "image": "image", "audio": "audio", "video": "video", "dir": "dir",
}


def _viewer_for(kind):
    return _VIEWERS.get(kind, "unsupported")


# ---------------------------------------------------------------- 搜索

def _register_search_routes(app):
    @app.get("/api/search")
    def _search(request):
        return srv.Response.json(search.search(app, request.query))

    @app.get("/api/search/suggest")
    def _suggest(request):
        return srv.Response.json(search.suggest(
            app, request.arg("q", ""), limit=_int_arg(request, "limit", 30, 1, 100)))

    @app.post("/api/search/reindex")
    def _reindex(request):
        body = request.json_body() or {}
        kb_id = body.get("kb") if isinstance(body, dict) else None
        job = app.jobs.submit(
            "index", {"kb": int(kb_id) if kb_id else None,
                      "force": True},
            title="重建全文索引" + ("（知识库 #%s）" % kb_id if kb_id else "（全部）"))
        return srv.Response.json({"ok": True, "job": job}, status=201)


# ---------------------------------------------------------------- 收藏 / 图标

def _register_library_routes(app):
    @app.get("/api/fav/list")
    def _fav_list(request):
        return srv.Response.json({
            "ok": True,
            "favorites": library.list_favorites(
                app, kb_id=request.arg("kb"),
                limit=_int_arg(request, "limit", 300, 1, 1000))})

    @app.post("/api/fav/toggle")
    def _fav_toggle(request):
        body = request.json_body() or {}
        path = body.get("path") if isinstance(body, dict) else None
        if not path:
            return srv.Response.error("缺少 path", 400)
        try:
            kb, real = kbs.resolve_any_kb(app, path, expect_file=None)
        except kbs.KbError as exc:
            return _fail(exc)
        return srv.Response.json(
            library.toggle_favorite(app, real, kb_id=kb["id"],
                                    name=os.path.basename(real)))

    @app.get("/api/recent/list")
    def _recent_list(request):
        return srv.Response.json({
            "ok": True,
            "recents": library.list_recents(
                app, kb_id=request.arg("kb"),
                limit=_int_arg(request, "limit", 50, 1, 500))})

    @app.post("/api/recent/clear")
    def _recent_clear(request):
        return srv.Response.json(library.clear_recents(app))

    @app.post("/api/icon/set")
    def _icon_set(request):
        body = request.json_body() or {}
        if not isinstance(body, dict) or not body.get("target_path"):
            return srv.Response.error("缺少 target_path", 400)
        try:
            kb, real = kbs.resolve_any_kb(app, body["target_path"], expect_file=None)
        except kbs.KbError as exc:
            return _fail(exc)
        return srv.Response.json(library.set_icon(
            app, real, body.get("target_type") or ("dir" if os.path.isdir(real) else "file"),
            body.get("icon_type") or "builtin", body.get("icon_value") or "file",
            kb_id=kb["id"]))

    @app.post("/api/icon/upload")
    def _icon_upload(request):
        body = request.json_body() or {}
        if not isinstance(body, dict) or not body.get("target_path"):
            return srv.Response.error("缺少 target_path", 400)
        try:
            kb, real = kbs.resolve_any_kb(app, body["target_path"], expect_file=None)
        except kbs.KbError as exc:
            return _fail(exc)
        result = iconstore.save_icon(
            app, kb["id"], body.get("data_base64") or body.get("data") or b"",
            target_path=real,
            target_type=body.get("target_type")
            or ("dir" if os.path.isdir(real) else "file"))
        if not result.get("ok"):
            return srv.Response.error(result.get("error") or "图标无法保存", 400)
        return srv.Response.json(library.set_icon(
            app, real, result.get("target_type") or "file",
            result["icon_type"], result["icon_value"], kb_id=kb["id"]))

    @app.post("/api/icon/clear")
    def _icon_clear(request):
        body = request.json_body() or {}
        path = body.get("target_path") if isinstance(body, dict) else None
        if not path:
            return srv.Response.error("缺少 target_path", 400)
        try:
            _kb, real = kbs.resolve_any_kb(app, path, expect_file=None)
        except kbs.KbError as exc:
            return _fail(exc)
        return srv.Response.json(library.clear_icon(app, real))

    @app.get("/api/icon/file/<icon_id>")
    def _icon_file(request, icon_id):
        data, ctype = iconstore.read_icon(app, icon_id)
        if data is None:
            return srv.Response.error("图标不存在", 404)
        # 即清洗过的 SVG 也要带上 CSP：万一漏了什么，浏览器这层还是执行不了
        return srv.Response(200, data, ctype, {
            "Content-Security-Policy": security.ICON_CSP,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        })

    @app.get("/api/icon/builtin")
    def _icon_builtin(request):
        return srv.Response.json({"ok": True, "icons": iconstore.builtin_icons()})

    @app.get("/api/icon/list")
    def _icon_list(request):
        return srv.Response.json({"ok": True,
                                  "icons": library.all_icons(app, request.arg("kb"))})


# ---------------------------------------------------------------- 总览 / 状态

def _register_status_routes(app):
    @app.get("/api/status")
    def _status(request):
        counters = db.counts(app)
        active = app.jobs.list(active_only=True, limit=20)
        degraded = []
        if not db.fts_ready(app):
            degraded.append({
                "feature": "full_text",
                "reason": "本机的 SQLite 未编译 FTS5 模块，全文检索已降级为模糊匹配。"})
        if not db.trigram_ready(app) and db.fts_ready(app):
            degraded.append({
                "feature": "cjk_substring",
                "reason": "trigram 索引不可用（需要 SQLite ≥ 3.34），"
                          "中文子串检索已降级。"})
        return srv.Response.json({
            "ok": True,
            "app": config.APP_ID,
            "version": config.APP_VERSION,
            "counts": counters,
            "db_bytes": db.db_size(app),
            "db_bytes_text": human_size(db.db_size(app)),
            "fts5": db.fts_ready(app),
            "trigram": db.trigram_ready(app),
            "sqlite_version": db.sqlite_version(app),
            "degraded_features": degraded,
            "active_jobs": active,
            "job_counts": app.jobs.counts(),
            "allowed_roots": app.allowed.roots(),
            "last_scan_at": db.meta_get(app, "last_auto_scan"),
            "uptime": round(time.time() - app.started_at, 1),
        })

    @app.get("/api/home")
    def _home(request):
        """首页一次性取齐：知识库卡片 + 最近阅读 + 收藏 + 最近更新。

        合并成一个请求是刻意的 —— 设计文档 §25 的目标是首页 < 500ms，
        四个请求会在弱机上串成四倍延迟。
        """
        kb_list = kbs.list_kbs(app, with_stats=True)
        for kb in kb_list:
            kb["live_file_count"] = int(kb.get("live_file_count") or 0)
        newest = app.store.query(
            "SELECT f.path, f.name, f.kb_id, f.rel_path, f.kind, f.size, f.mtime_ns"
            " FROM files f WHERE f.is_deleted=0 AND f.is_dir=0"
            " ORDER BY f.mtime_ns DESC LIMIT 12")
        return srv.Response.json({
            "ok": True,
            "knowledge_bases": kb_list,
            "recents": library.list_recents(app, limit=12),
            "favorites": library.list_favorites(app, limit=12),
            "recently_updated": [
                {**row, "icon": config.ICON_BY_KIND.get(row["kind"], "file")}
                for row in newest],
            "counts": db.counts(app),
            "allowed_roots": app.allowed.roots(),
        })


# ---------------------------------------------------------------- 任务

def _register_jobs(app):
    @app.jobs.register("scan")
    def _job_scan(ctx):
        kb_id = ctx.params.get("kb")
        if not kb_id:
            raise ValueError("scan 任务缺少 kb 参数")
        return scanner.scan_and_index(app, ctx, int(kb_id),
                                      force=bool(ctx.params.get("force")))

    @app.jobs.register("index")
    def _job_index(ctx):
        kb_id = ctx.params.get("kb")
        return scanner.index_pending(app, ctx,
                                     kb_id=int(kb_id) if kb_id else None,
                                     force=bool(ctx.params.get("force")))


# ---------------------------------------------------------------- 装配

def create_app(paths=None, log_level="INFO"):
    app = KnowledgeApp(
        config.APP_ID,
        config.TITLE,
        version=config.APP_VERSION,
        workers=2,
        log_level=log_level,
        extra_migrations=db.MIGRATIONS,
        paths=paths,
        description="把 NAS 上的文件夹变成只读、可搜索、好阅读的知识空间",
        engines={},
    )
    fsapi.register(app)
    # 立刻迁移，而不是等 app.run() —— 框架的 JobManager.start() 才会调 migrate()，
    # 但装配阶段就要读 app_meta（能力探测）与建库表，不先迁移就会撞
    # 「no such table」。migrate() 幂等，run() 时再调一次无副作用。
    app.store.migrate(logger=app.log)
    _register_status_routes(app)
    _register_kb_routes(app)
    _register_file_routes(app)
    _register_search_routes(app)
    _register_library_routes(app)
    _register_jobs(app)
    app.log.info(
        "知识库应用已装配：%d 条路由，FTS5=%s，trigram=%s", len(app.routes.routes),
        db.fts_ready(app), db.trigram_ready(app))
    return app


def main(argv=None):
    from tnasapp import cli

    return cli.main(config.APP_ID, config.APP_VERSION, create_app,
                    description="TNAS Knowledge —— 只读私有知识库", argv=argv)
