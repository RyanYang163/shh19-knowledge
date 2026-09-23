"""收藏、最近阅读、自定义图标。

设计文档 §22 给 ``favorites`` / ``recent_files`` 带了 ``user_id``。**框架不暴露用户身份**
（``server._parse_auth`` 只解析平台的 CSRF 头，没有账号体系），所以去掉了那一列，
语义变成「设备级单份」：同一台 NAS 上的使用者共用一份收藏与最近记录。
这一点已写进 ``PRIVACY.md``，不是悄悄省略。

全部按**路径**而不是文件 id 引用。理由：索引可以被清空重建（重新扫描、注销知识库
再重建），而文件 id 会随之改变；路径是稳定的。代价是文件被移走后会留下悬空引用 ——
读取时用 ``LEFT JOIN files`` 过滤掉，不需要额外的清理任务。
"""

import time

from . import config


def toggle_favorite(app, path, kb_id=None, name=""):
    existing = app.store.query_one("SELECT path FROM favorites WHERE path=?", (path,))
    if existing:
        app.store.execute("DELETE FROM favorites WHERE path=?", (path,))
        return {"ok": True, "favorited": False, "path": path}
    app.store.execute(
        "INSERT INTO favorites (path, kb_id, name, added_at) VALUES (?,?,?,?)",
        (path, kb_id, name or path.rsplit("/", 1)[-1], time.time()),
    )
    return {"ok": True, "favorited": True, "path": path}


def list_favorites(app, kb_id=None, limit=300):
    clause = "WHERE 1=1"
    params = []
    if kb_id not in (None, "", "0"):
        clause += " AND v.kb_id=?"
        params.append(int(kb_id))
    params.append(int(limit))
    rows = app.store.query(
        "SELECT v.path, v.kb_id, v.name, v.added_at,"
        " f.rel_path, f.kind, f.ext, f.size, f.mtime_ns, f.is_deleted"
        " FROM favorites v LEFT JOIN files f ON f.path = v.path"
        " %s ORDER BY v.added_at DESC LIMIT ?" % clause, tuple(params))
    return [_entry(row, " 收藏") for row in rows]


def touch_recent(app, path, kb_id=None, name=""):
    """记录一次打开。UPSERT 累加计数。"""
    app.store.execute(
        "INSERT INTO recents (path, kb_id, name, last_opened, open_count)"
        " VALUES (?,?,?,?,1)"
        " ON CONFLICT(path) DO UPDATE SET last_opened=excluded.last_opened,"
        " open_count = recents.open_count + 1, name=excluded.name",
        (path, kb_id, name or path.rsplit("/", 1)[-1], time.time()),
    )
    return {"ok": True, "path": path}


def list_recents(app, kb_id=None, limit=50):
    clause = "WHERE 1=1"
    params = []
    if kb_id not in (None, "", "0"):
        clause += " AND r.kb_id=?"
        params.append(int(kb_id))
    params.append(int(limit))
    rows = app.store.query(
        "SELECT r.path, r.kb_id, r.name, r.last_opened, r.open_count,"
        " f.rel_path, f.kind, f.ext, f.size, f.mtime_ns, f.is_deleted"
        " FROM recents r LEFT JOIN files f ON f.path = r.path"
        " %s ORDER BY r.last_opened DESC LIMIT ?" % clause, tuple(params))
    out = []
    for row in rows:
        entry = _entry(row, "上次打开 ")
        entry["open_count"] = int(row["open_count"] or 1)
        entry["last_opened"] = row["last_opened"]
        out.append(entry)
    return out


def _entry(row, prefix):
    kind = row.get("kind") or "other"
    return {
        "path": row["path"],
        "name": row["name"] or (row["path"] or "").rsplit("/", 1)[-1],
        "kb_id": row.get("kb_id"),
        "rel_path": row.get("rel_path") or "",
        "kind": kind,
        "ext": row.get("ext") or "",
        "size": row.get("size") or 0,
        "mtime_ns": row.get("mtime_ns") or 0,
        "icon": config.ICON_BY_KIND.get(kind, "file"),
        # 索引里已经找不到这个文件（被移动或删除）—— 前端会置灰，不假装它还在
        "missing": bool(row.get("is_deleted")) or row.get("rel_path") is None,
    }


def clear_recents(app):
    app.store.execute("DELETE FROM recents")
    return {"ok": True}


# ---------------------------------------------------------------- 自定义图标

def set_icon(app, target_path, target_type, icon_type, icon_value, kb_id=None):
    """给文件或目录指定图标。**只写应用数据库，绝不修改原文件。**"""
    app.store.execute(
        "INSERT INTO custom_icons (kb_id, target_type, target_path, icon_type,"
        " icon_value, created_at, updated_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(target_path) DO UPDATE SET target_type=excluded.target_type,"
        " icon_type=excluded.icon_type, icon_value=excluded.icon_value,"
        " kb_id=excluded.kb_id, updated_at=excluded.updated_at",
        (kb_id, target_type or "file", target_path, icon_type or "builtin",
         str(icon_value or ""), time.time(), time.time()),
    )
    return {"ok": True, "path": target_path, "icon_type": icon_type,
            "icon_value": icon_value}


def clear_icon(app, target_path):
    app.store.execute("DELETE FROM custom_icons WHERE target_path=?", (target_path,))
    return {"ok": True, "path": target_path}


def icons_for(app, paths):
    """批量取图标，避免树上逐行查询造成 N+1。"""
    paths = [p for p in (paths or []) if p]
    if not paths:
        return {}
    out = {}
    for start in range(0, len(paths), 400):
        chunk = paths[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for row in app.store.query(
                "SELECT target_path, icon_type, icon_value FROM custom_icons"
                " WHERE target_path IN (%s)" % marks, tuple(chunk)):
            out[row["target_path"]] = {
                "type": row["icon_type"], "value": row["icon_value"]}
    return out


def all_icons(app, kb_id=None):
    if kb_id not in (None, "", "0"):
        rows = app.store.query(
            "SELECT target_path, target_type, icon_type, icon_value FROM custom_icons"
            " WHERE kb_id=?", (int(kb_id),))
    else:
        rows = app.store.query(
            "SELECT target_path, target_type, icon_type, icon_value FROM custom_icons")
    return [{"path": row["target_path"], "target_type": row["target_type"],
             "icon_type": row["icon_type"], "icon_value": row["icon_value"]}
            for row in rows]


def icon_for_file(app, path, kind, is_dir, custom=None):
    """图标优先级：自定义 → AI 推荐（V1 无）→ 按扩展名的内置图标 → 默认。"""
    if custom:
        return custom
    if is_dir:
        return {"type": "builtin", "value": "folder"}
    return {"type": "builtin", "value": config.ICON_BY_KIND.get(kind, "file")}
