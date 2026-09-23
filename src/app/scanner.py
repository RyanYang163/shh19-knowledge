"""扫描与增量索引。

分两个阶段，**都在任务队列里跑**（路由绝不阻塞）：

* :func:`scan_kb` —— 只读目录项的元数据（``stat``），不读文件内容。
  十万文件也能在秒级完成，因为不碰文件正文。
* :func:`index_pending` —— 对 ``text_state='pending'`` 的文件做文本抽取并写 FTS。

**增量判据是 (size, mtime_ns, ctime_ns, inode)，绝不整文件算哈希。**
设计文档 §22 的 ``files.hash`` 一列因此没有建（§23 自己也说「不要每次对所有文件做
SHA-256」）—— 两处矛盾就此消解。判据的比较写在 ``db.UPSERT_FILE_SQL`` 的
``ON CONFLICT DO UPDATE`` 里，未变化的文件 ``text_state`` 原样保留，于是第二阶段
自然只会处理真正变了的文件。

用 ``st_mtime_ns``（整数纳秒）而不是 ``st_mtime``（浮点）：浮点比较会出现
「同一个文件两次 stat 得到不同值」的抖动，纳秒整数没有这个问题。
"""

import mimetypes
import os
import time

from tnasapp.paths import human_size

from . import config, db

#: 无扩展名但应当按代码/文本处理的名字（小写比较）
_SPECIAL_NAMES = {
    "dockerfile": ("code", "text/x-dockerfile"),
    "makefile": ("code", "text/x-makefile"),
    "cmakelists.txt": ("code", "text/x-cmake"),
    "rakefile": ("code", "text/x-ruby"),
    "gemfile": ("code", "text/x-ruby"),
    "readme": ("text", "text/plain"),
    "license": ("text", "text/plain"),
    "notice": ("text", "text/plain"),
    "changelog": ("text", "text/plain"),
}

mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/markdown", ".markdown")
mimetypes.add_type("application/json", ".jsonl")
mimetypes.add_type("application/x-yaml", ".yaml")
mimetypes.add_type("application/x-yaml", ".yml")
mimetypes.add_type("application/vnd.openxmlformats-officedocument"
                   ".wordprocessingml.document", ".docx")
mimetypes.add_type("application/vnd.openxmlformats-officedocument"
                   ".spreadsheetml.sheet", ".xlsx")
mimetypes.add_type("application/vnd.openxmlformats-officedocument"
                   ".presentationml.presentation", ".pptx")


def kind_for(name):
    """按名字判 kind。无扩展名时看特征名，再不行归 ``other``。"""
    lowered = (name or "").lower()
    ext = os.path.splitext(lowered)[1]
    if ext in config.EXT_KIND:
        return config.EXT_KIND[ext]
    if lowered in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[lowered][0]
    return "other"


def mime_for(name, kind):
    ext = os.path.splitext(name or "")[1].lower()
    lowered = (name or "").lower()
    if lowered in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[lowered][1]
    guessed = mimetypes.guess_type(name or "")[0]
    if guessed:
        return guessed
    if kind in ("text", "code", "markdown"):
        return "text/plain"
    return "application/octet-stream"


def lang_for(name):
    """给前端语法高亮用的语言标识。认不出就返回空串（前端退化为纯文本）。"""
    return config.LANG_BY_EXT.get(os.path.splitext(name or "")[1].lower(), "")


# ---------------------------------------------------------------- 阶段一：扫描

def _walk(app, ctx, kb, root, gen, force, excludes, seen_paths):
    """迭代式遍历（显式栈，不用递归——深目录不会撞 Python 递归上限）。

    每个目录一次 ``os.scandir``，一次 ``executemany`` 落库。单个目录读不动
    （权限 / 已被删除）只计入 errors 并继续，**绝不中断整次扫描**：
    十万文件的扫描不该因为一个坏目录就前功尽弃。
    """
    now = time.time()
    stats = {"added": 0, "dirs": 0, "skipped_excluded": 0, "errors": 0, "truncated": False}
    stack = [(root, "", 0)]
    checked_dirs = 0

    while stack:
        abs_dir, rel_dir, depth = stack.pop()
        ctx.checkpoint()
        checked_dirs += 1
        if checked_dirs % 200 == 0:
            ctx.progress(stats["added"], None, "已扫描 %d 个目录，收录 %d 项"
                         % (checked_dirs, stats["added"]))

        try:
            entries = list(os.scandir(abs_dir))
        except (PermissionError, OSError) as exc:
            stats["errors"] += 1
            ctx.log("跳过无法读取的目录 %s（%s）" % (rel_dir or "/", exc), "WARN")
            continue

        batch = []
        for entry in entries:
            if stats["added"] >= config.MAX_FILES_PER_KB:
                stats["truncated"] = True
                break
            name = entry.name
            try:
                is_link = entry.is_symlink()
                if not is_link and entry.is_dir(follow_symlinks=False):
                    if name in excludes:
                        stats["skipped_excluded"] += 1
                        continue
                    if depth + 1 > config.MAX_DEPTH:
                        stats["errors"] += 1
                        ctx.log("目录层级超过 %d，不再深入：%s"
                                % (config.MAX_DEPTH, entry.path), "WARN")
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    child_rel = "%s/%s" % (rel_dir, name) if rel_dir else name
                    batch.append(_row(kb["id"], rel_dir, entry.path, child_rel, name,
                                      "other", "inode/directory", 0, stat, gen, 1, 0,
                                      depth + 1, now))
                    stack.append((entry.path, child_rel, depth + 1))
                    stats["dirs"] += 1
                    stats["added"] += 1
                    seen_paths.add(entry.path)
                    continue

                if is_link:
                    # 软链**记为条目但不下钻**：否则一个指回上层的软链会造成无限遍历，
                    # 而指向白名单外的软链本来就会被 resolve_in_kb 拒掉。
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        stats["errors"] += 1
                        continue
                    child_rel = "%s/%s" % (rel_dir, name) if rel_dir else name
                    batch.append(_row(kb["id"], rel_dir, entry.path, child_rel, name,
                                      kind_for(name), "inode/symlink", stat.st_size,
                                      stat, gen, 0, 1, depth + 1, now))
                    stats["added"] += 1
                    seen_paths.add(entry.path)
                    continue

                if not entry.is_file(follow_symlinks=False):
                    continue  # 设备节点 / FIFO / socket 一律不收
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                stats["errors"] += 1
                continue

            kind = kind_for(name)
            child_rel = "%s/%s" % (rel_dir, name) if rel_dir else name
            batch.append(_row(kb["id"], rel_dir, entry.path, child_rel, name, kind,
                              mime_for(name, kind), stat.st_size, stat, gen, 0, 0,
                              depth + 1, now))
            stats["added"] += 1
            seen_paths.add(entry.path)

        if batch:
            _flush(app, batch)
        if stats["truncated"]:
            ctx.log("已达单库收录上限 %d，停止收录更多文件。"
                    "如需完整收录请缩小知识库范围。" % config.MAX_FILES_PER_KB, "WARN")
            break

    return stats


def _row(kb_id, parent_rel, path, rel_path, name, kind, mime, size, stat, gen,
         is_dir, is_symlink, depth, now):
    """按 ``db.UPSERT_FILE_SQL`` 的占位顺序排列的一行。"""
    ext = os.path.splitext(name)[1].lower()
    return (
        int(kb_id), parent_rel, path, rel_path, name, ext, mime, kind,
        int(size or 0),
        int(getattr(stat, "st_mtime_ns", 0) or 0),
        int(getattr(stat, "st_ctime_ns", 0) or 0),
        int(getattr(stat, "st_ino", 0) or 0),
        1 if is_dir else 0,
        1 if is_symlink else 0,
        int(depth), int(gen),
        0, 0,          # image_w / image_h：留到索引阶段按需补，扫描阶段不解码
        now, now,
    )


def _flush(app, batch):
    """批量写入。**撞外键约束说明知识库在扫描途中被注销了** ——
    这时要给出可读原因，而不是把 IntegrityError 原样抛给用户。

    实测场景：新建知识库会自动投一个扫描任务；如果用户在任务跑完前就注销了它，
    后续批次写入就会 FOREIGN KEY constraint failed。原样抛出的话，任务详情里
    只有一句数据库错误，谁也看不出发生了什么。
    """
    import sqlite3

    for start in range(0, len(batch), config.SCAN_BATCH):
        chunk = batch[start:start + config.SCAN_BATCH]
        try:
            db.upsert_files(app, chunk)
        except sqlite3.IntegrityError as exc:
            kb_id = chunk[0][0] if chunk else None
            exists = app.store.query_one(
                "SELECT id FROM knowledge_bases WHERE id=?", (int(kb_id or 0),))
            if not exists:
                raise RuntimeError(
                    "知识库（#%s）在扫描过程中被注销，本次扫描已中止。" % kb_id) from exc
            raise RuntimeError("写入索引失败：%s" % exc) from exc


def scan_kb(app, ctx, kb_id, force=False):
    """扫描一个知识库。返回统计字典（会作为任务 result 落库）。"""
    started = time.time()
    kb = app.store.query_one("SELECT * FROM knowledge_bases WHERE id=?", (int(kb_id),))
    if not kb:
        raise ValueError("知识库不存在：#%s" % kb_id)

    root = kb["root_path"]
    if not os.path.isdir(root):
        raise RuntimeError(
            "知识库根目录已不可访问：%s（可能已被移动、卸载或删除）" % root
        )

    excludes = set(app.settings.get("exclude_dirs") or config.DEFAULT_EXCLUDES)
    gen = db.next_scan_gen(app)
    app.store.execute(
        "UPDATE knowledge_bases SET scan_state='scanning', updated_at=? WHERE id=?",
        (started, int(kb_id)),
    )
    ctx.log("开始扫描 #%s %s" % (kb_id, root))

    seen = set()
    stats = _walk(app, ctx, kb, root, gen, force, excludes, seen)
    db.finalize_scan(app, kb_id, gen)
    summary = db.refresh_kb_stats(app, kb_id)

    elapsed = time.time() - started
    app.store.execute(
        "UPDATE knowledge_bases SET scan_state='idle', last_scan_at=?, updated_at=?"
        " WHERE id=?",
        (time.time(), time.time(), int(kb_id)),
    )
    result = {
        "kb": int(kb_id),
        "name": kb["name"],
        "entries": stats["added"],
        "dirs": stats["dirs"],
        "excluded": stats["skipped_excluded"],
        "errors": stats["errors"],
        "truncated": stats["truncated"],
        "elapsed": round(elapsed, 2),
        "file_count": summary["file_count"],
        "total_bytes": summary["total_bytes"],
        "total_text": human_size(summary["total_bytes"]),
    }
    ctx.log("扫描完成：%d 项（其中目录 %d），耗时 %.2fs%s"
            % (stats["added"], stats["dirs"], elapsed,
               "，**已达收录上限**" if stats["truncated"] else ""))
    return result


# ---------------------------------------------------------------- 阶段二：索引

def index_pending(app, ctx, kb_id=None, force=False, limit=None):
    """对待索引的文件做文本抽取并写全文索引。"""
    from . import extract

    started = time.time()
    clauses = ["is_deleted=0", "is_dir=0"]
    params = []
    if kb_id is not None:
        clauses.append("kb_id=?")
        params.append(int(kb_id))
    if not force:
        # 默认只处理真正的「待索引」——这是增量索引省下全部重复工作的关键
        clauses.append("text_state='pending'")

    where = " AND ".join(clauses)
    rows = app.store.query(
        "SELECT * FROM files WHERE %s ORDER BY kb_id, id%s"
        % (where, " LIMIT %d" % int(limit) if limit else ""),
        tuple(params),
    )
    total = len(rows)
    ctx.log("待索引文件 %d 个%s" % (total, "（强制重抽）" if force else ""))

    stats = {"ok": 0, "empty": 0, "unsupported": 0, "failed": 0, "chars": 0}
    for position, row in enumerate(rows, 1):
        ctx.checkpoint()
        if position % 5 == 1 or total <= 20:
            ctx.progress(position, total, "已索引 %d/%d：%s"
                         % (position, total, row["name"][:40]))
        try:
            outcome = extract.extract_and_store(app, row)
        except Exception as exc:  # noqa: BLE001 —— 单文件失败绝不能炸掉整个任务
            stats["failed"] += 1
            app.store.execute(
                "UPDATE files SET text_state='failed', error=?, updated_at=?"
                " WHERE id=?",
                ("%s: %s" % (type(exc).__name__, exc)[:400], time.time(), row["id"]),
            )
            ctx.log("抽取失败：%s（%s）" % (row["rel_path"], exc), "WARN")
            continue

        state = outcome.get("state") or "failed"
        stats[state] = stats.get(state, 0) + 1
        stats["chars"] += outcome.get("chars") or 0
        app.store.execute(
            "UPDATE files SET text_state=?, text_bytes=?, unit_count=?,"
            " image_w=?, image_h=?, error=?, updated_at=? WHERE id=?",
            (state, outcome.get("chars") or 0, outcome.get("units") or 0,
             outcome.get("image_w") or 0, outcome.get("image_h") or 0,
             outcome.get("error"), time.time(), row["id"]),
        )

    elapsed = time.time() - started
    result = {
        "total": total,
        "indexed": stats.get("ok", 0),
        "empty": stats.get("empty", 0),
        "unsupported": stats.get("unsupported", 0),
        "failed": stats.get("failed", 0),
        "chars": stats["chars"],
        "elapsed": round(elapsed, 2),
    }
    ctx.log("索引完成：成功 %d，无文本 %d，不支持 %d，失败 %d，共 %.2fs"
            % (result["indexed"], result["empty"], result["unsupported"],
               result["failed"], elapsed))
    if kb_id is not None:
        db.refresh_kb_stats(app, kb_id)
    return result


def scan_and_index(app, ctx, kb_id, force=False):
    """``scan`` 任务类型的处理器：先扫元数据，紧接着索引新变化的文件。

    合成一个任务而不是两个，是为了让用户在任务列表里只看到一条、
    且进度连续（扫描 → 索引），不必自己判断「扫完了该点哪个」。
    """
    scan_result = scan_kb(app, ctx, kb_id, force=force)
    index_result = index_pending(app, ctx, kb_id, force=force)
    app.jobs.purge_finished(keep=200)
    combined = {"scan": scan_result, "index": index_result}
    # 结果在这里统一发布：两个阶段各自 set_result 会让后者覆盖前者，
    # 任务详情里就只剩「索引」而没有「扫描」了（实测踩过）。
    ctx.set_result(combined)
    return combined


def scan_paths(app, ctx, kb_id, paths):
    """定点重扫（目前用于「重新扫描这个文件」的单项刷新）。

    实现上仍走一次整库扫描：增量判据会让未变化的文件直接跳过，
    代价只有一次 stat 遍历，比维护一套「单路径增量」逻辑可靠得多。
    """
    ctx.log("定点刷新 %d 个路径（走整库增量扫描）" % len(paths or []))
    return scan_and_index(app, ctx, kb_id)
