"""知识库注册表与**路径作用域收口**。

把「一个文件夹」注册成知识库，只写应用自己的数据库，**不碰磁盘上的任何文件**。

本模块最重要的一个函数是 :func:`resolve_in_kb` —— 所有内容路由、提取器、图标操作、
相关文件计算都必须经过它。它做**两道**校验：

1. 框架的 ``AllowedRoots.check()``：realpath 规范化后比对白名单根。
   目录穿越（``../``）与指向白名单之外的软链都会在这里被拒。
2. 知识库根校验：路径必须落在**该知识库的根**之下。

第 2 道不能省。白名单里可能同时有 ``/Volume1/Docs`` 与 ``/Volume1/Docs-Archive``；
只做第 1 道的话，从「Docs」这个知识库就能读到隔壁 Archive 的内容 ——
白名单没被突破，但知识库的边界被突破了。
"""

import os
import time

from tnasapp.paths import PathDenied, is_subpath

from . import db

#: 单个 KB 允许的根路径必须是目录；文件名最长的一段做个上限，避免异常输入
MAX_NAME = 120


class KbError(Exception):
    """带 HTTP 状态码的业务错误。路由直接 ``Response.error(str(e), e.status)``。"""

    def __init__(self, message, status=400, hint=None):
        super().__init__(message)
        self.status = status
        self.hint = hint


# ---------------------------------------------------------------- 查询

def list_kbs(app, with_stats=True):
    rows = app.store.query("SELECT * FROM knowledge_bases ORDER BY id")
    if not with_stats:
        return rows
    for row in rows:
        counts = app.store.query_one(
            "SELECT COUNT(*) AS n FROM files"
            " WHERE kb_id=? AND is_deleted=0 AND is_dir=0", (row["id"],)
        )
        row["live_file_count"] = int(counts["n"] or 0) if counts else 0
        row["root_exists"] = os.path.isdir(row["root_path"])
    return rows


def get_kb(app, kb_id):
    return app.store.query_one(
        "SELECT * FROM knowledge_bases WHERE id=?", (int(kb_id),)
    )


def require_kb(app, kb_id):
    kb = get_kb(app, kb_id)
    if not kb:
        raise KbError("知识库不存在：%s" % kb_id, 404, "请刷新知识库列表后重试。")
    return kb


def kb_stats(app, kb_id):
    require_kb(app, kb_id)
    totals = app.store.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files"
        " WHERE kb_id=? AND is_deleted=0 AND is_dir=0", (int(kb_id),)
    )
    dirs = app.store.query_one(
        "SELECT COUNT(*) AS n FROM files WHERE kb_id=? AND is_deleted=0 AND is_dir=1",
        (int(kb_id),),
    )
    by_kind = app.store.query(
        "SELECT kind, COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files"
        " WHERE kb_id=? AND is_deleted=0 AND is_dir=0 GROUP BY kind ORDER BY n DESC",
        (int(kb_id),),
    )
    by_year = app.store.query(
        "SELECT CAST(strftime('%Y', mtime_ns/1000000000, 'unixepoch') AS INTEGER) AS y,"
        " COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files"
        " WHERE kb_id=? AND is_deleted=0 AND is_dir=0 GROUP BY y ORDER BY y DESC LIMIT 30",
        (int(kb_id),),
    )
    states = app.store.query(
        "SELECT text_state, COUNT(*) AS n FROM files"
        " WHERE kb_id=? AND is_deleted=0 AND is_dir=0 GROUP BY text_state",
        (int(kb_id),),
    )
    return {
        "file_count": int(totals["n"] or 0) if totals else 0,
        "total_bytes": int(totals["b"] or 0) if totals else 0,
        "dir_count": int(dirs["n"] or 0) if dirs else 0,
        "by_kind": by_kind,
        "by_year": by_year,
        "text_states": {row["text_state"]: int(row["n"]) for row in states},
    }


# ---------------------------------------------------------------- 路径收口

def resolve_in_kb(app, kb_id, raw_path, must_exist=True, expect_file=None):
    """把用户给的路径解析成「确实属于该知识库」的绝对真实路径。

    :param expect_file: ``True`` 只接受文件，``False`` 只接受目录，``None`` 不限
    :raises KbError: 路径越界、不存在、类型不符
    """
    kb = require_kb(app, kb_id)
    if raw_path is None or not str(raw_path).strip():
        raise KbError("缺少 path 参数", 400, "请从文件树里选择文件后重试。")

    try:
        real = app.allowed.check(str(raw_path), must_exist=True)
    except PathDenied as exc:
        # 白名单外（含目录穿越与软链逃逸）。不暴露真实路径，只给可读原因
        raise KbError(
            "该路径不在允许访问的目录内", 403,
            "%s。请先在「设置」里把它加入可访问目录。" % exc,
        )

    if not is_subpath(real, kb["root_path"]):
        raise KbError(
            "该路径不属于知识库「%s」" % kb["name"], 403,
            "知识库只能读取自己根目录下的内容。",
        )

    if not must_exist:
        return real

    exists = os.path.exists(real)
    if not exists:
        # 源文件可能在扫描之后被删掉了。给出明确原因，而不是笼统 404
        raise KbError(
            "文件已不存在：%s" % os.path.basename(real), 404,
            "它可能在扫描之后被移动或删除了。可在知识库里重新扫描。",
        )
    if expect_file is True and not os.path.isfile(real):
        raise KbError("这是一个目录，不是文件", 400, "请选择具体文件。")
    if expect_file is False and not os.path.isdir(real):
        raise KbError("这是一个文件，不是目录", 400, "请选择目录。")
    return real


def resolve_any_kb(app, raw_path, expect_file=None):
    """不指定知识库时，按路径反查它属于哪个知识库，再解析。

    首页的「最近阅读」「收藏」只存了路径，需要这个入口。
    路径同时落在多个知识库里是不可能的——``create_kb`` 会拒绝互相嵌套的根。
    """
    if raw_path is None or not str(raw_path).strip():
        raise KbError("缺少 path 参数", 400)

    try:
        real = app.allowed.check(str(raw_path), must_exist=False)
    except PathDenied as exc:
        raise KbError("该路径不在允许访问的目录内", 403, str(exc))

    kbs = app.store.query("SELECT * FROM knowledge_bases ORDER BY LENGTH(root_path) DESC")
    for kb in kbs:
        if is_subpath(real, kb["root_path"]):
            return kb, resolve_in_kb(app, kb["id"], real, expect_file=expect_file)
    raise KbError(
        "该路径不属于任何知识库", 404,
        "它可能来自已删除的知识库。请重新扫描或把它加入某个知识库。",
    )


def relpath_in_kb(kb, real_path):
    """知识库内的相对路径（用于显示与相关文件计算）。"""
    root = kb["root_path"].rstrip(os.sep)
    if real_path == root:
        return ""
    if real_path.startswith(root + os.sep):
        return real_path[len(root) + 1:]
    return real_path


def parent_relpath(rel_path):
    """相对路径的父目录（``""`` 表示知识库根）。"""
    rel = (rel_path or "").strip("/")
    if not rel or "/" not in rel:
        return ""
    return rel.rsplit("/", 1)[0]


# ---------------------------------------------------------------- 增删改

def _validate_root(app, root_path):
    try:
        real = app.allowed.check(str(root_path), must_exist=True)
    except PathDenied as exc:
        raise KbError(
            "该目录不在允许访问的范围内", 403,
            "%s。请先在「设置」里添加它。" % exc,
        )
    if not os.path.isdir(real):
        raise KbError("请选择目录，而不是文件", 400, "知识库的根必须是一个文件夹。")
    return real


def _check_overlap(app, real_root, exclude_kb_id=None):
    """拒绝互相嵌套的知识库根。

    嵌套会让同一个文件同时属于两个知识库，而 ``files`` 表的唯一键是
    ``(kb_id, path)`` —— 收录两份会造成重复计数与「搜到两次」，
    语义上也说不清该归谁。
    """
    for kb in app.store.query("SELECT id, name, root_path FROM knowledge_bases"):
        if exclude_kb_id is not None and int(kb["id"]) == int(exclude_kb_id):
            continue
        other = kb["root_path"]
        if is_subpath(real_root, other) or is_subpath(other, real_root):
            raise KbError(
                "该目录与已有知识库「%s」的目录互相包含" % kb["name"], 409,
                "请换一个不与现有知识库重叠的目录，避免同一个文件被收录两次。",
            )


def create_kb(app, name, root_path, description="", icon_type="builtin",
              icon_value="book", accent="", cover="", background=""):
    label = str(name or "").strip()
    if not label:
        raise KbError("知识库名称不能为空", 400, "给这个知识库起个名字，例如「产品文档」。")
    if len(label) > MAX_NAME:
        raise KbError("知识库名称过长", 400, "请控制在 %d 字以内。" % MAX_NAME)

    real = _validate_root(app, root_path)
    _check_overlap(app, real)

    now = time.time()
    kb_id = app.store.insert(
        "INSERT INTO knowledge_bases (name, description, root_path, icon_type,"
        " icon_value, accent, cover, background, enabled, scan_state,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,1,'idle',?,?)",
        (label, str(description or ""), real, str(icon_type or "builtin"),
         str(icon_value or "book"), str(accent or ""), str(cover or ""),
         str(background or ""), now, now),
    )
    app.log.info("知识库已创建：#%s %s（%s）", kb_id, label, real)
    return get_kb(app, kb_id)


def update_kb(app, kb_id, **fields):
    kb = require_kb(app, kb_id)
    allowed = {"name", "description", "icon_type", "icon_value", "accent",
               "cover", "background", "enabled"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if "name" in updates:
        label = str(updates["name"] or "").strip()
        if not label:
            raise KbError("知识库名称不能为空", 400)
        if len(label) > MAX_NAME:
            raise KbError("知识库名称过长", 400, "请控制在 %d 字以内。" % MAX_NAME)
        updates["name"] = label
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    if not updates:
        return kb

    updates["updated_at"] = time.time()
    keys = sorted(updates)
    app.store.execute(
        "UPDATE knowledge_bases SET %s WHERE id=?" % ",".join("%s=?" % k for k in keys),
        tuple(updates[k] for k in keys) + (int(kb_id),),
    )
    return get_kb(app, kb_id)


def delete_kb(app, kb_id):
    """注销知识库：**只删索引行，不删磁盘上的任何文件**。"""
    kb = require_kb(app, kb_id)
    removed = db.purge_kb(app, kb_id)
    app.log.info("知识库已注销：#%s %s（索引 %d 行，源文件未改动）",
                 kb_id, kb["name"], removed)
    return {
        "id": int(kb_id),
        "name": kb["name"],
        "root_path": kb["root_path"],
        "unregistered_files": removed,
        "note": "已注销该知识库的索引；源目录与其中的文件未被修改、删除或移动。",
    }
