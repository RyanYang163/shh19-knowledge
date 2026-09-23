"""数据库结构与迁移。

三处**必须照做**的设计决定，都是实测得出、写错不会报错但会静默出错的地方：

1. **抽取文本存在普通表 ``file_text``，FTS 用 ``content='file_text'`` 的 external-content 表。**
   这样做的理由有两个：
     * ``snippet()`` / ``highlight()`` 在 external-content 表上可用（在 ``content=''`` 的
       contentless 表上会返回 NULL，那样搜索就没法做高亮片段）；
     * 目标机若**没有** FTS5，文本仍然完整地留在 ``file_text`` 里，
       ``LIKE`` 降级检索与 ``/api/file/text`` 照常工作。FTS5 因此**不是**启动前提。
   （实测：本机 sqlite 3.49.1 上 snippet / highlight / rebuild / integrity-check 全通。）

2. **写入必须用 ``INSERT ... ON CONFLICT(file_id) DO UPDATE``，绝不能用 ``INSERT OR REPLACE``。**
   实测：REPLACE 为了满足主键冲突而删行时**不触发** ``AFTER DELETE`` 触发器
   （SQLite 只在 ``recursive_triggers`` 打开时才会），于是 FTS 索引里会残留旧词 ——
   搜索会返回一个早就不含该词的文件。``ON CONFLICT DO UPDATE`` 会触发 ``AFTER UPDATE``，
   同步正确。

3. **升级只做加法，永不删表删库**（指引 50）。所有语句 ``IF NOT EXISTS``，
   迁移用 ``PRAGMA user_version`` 按序补跑（由 ``tnasapp.store.Store`` 负责推进）。
"""

import os
import time

#: 与 tnasapp.jobs.SCHEMA（4 条）之后的偏移：我们的第一个迁移对应 user_version=5
SCHEMA_OFFSET = 4


# ---------------------------------------------------------------- 迁移

def _m_meta_and_kbs(conn):
    """user_version 5：应用元数据 + 知识库注册表。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS app_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS knowledge_bases (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        root_path   TEXT    NOT NULL UNIQUE,
        icon_type   TEXT    NOT NULL DEFAULT 'builtin',
        icon_value  TEXT    NOT NULL DEFAULT 'book',
        accent      TEXT    NOT NULL DEFAULT '',
        cover       TEXT    NOT NULL DEFAULT '',
        background  TEXT    NOT NULL DEFAULT '',
        enabled     INTEGER NOT NULL DEFAULT 1,
        scan_state  TEXT    NOT NULL DEFAULT 'idle',
        last_scan_at REAL,
        file_count  INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        created_at  REAL    NOT NULL,
        updated_at  REAL    NOT NULL
    );
    """)


def _m_files(conn):
    """user_version 6：文件元数据（扫描的产物，文件树与统计都读这里，不再扫盘）。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS files (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kb_id       INTEGER NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        -- 用父目录的**相对路径**而不是父行的 id：遍历时天然可得，
        -- 因此批量写入不依赖「父先于子」的插入顺序，也不需要在事后回填 id。
        parent_rel  TEXT    NOT NULL DEFAULT '',
        path        TEXT    NOT NULL,
        rel_path    TEXT    NOT NULL,
        name        TEXT    NOT NULL,
        ext         TEXT    NOT NULL DEFAULT '',
        mime        TEXT    NOT NULL DEFAULT '',
        kind        TEXT    NOT NULL DEFAULT 'other',
        size        INTEGER NOT NULL DEFAULT 0,
        mtime_ns    INTEGER NOT NULL DEFAULT 0,
        ctime_ns    INTEGER NOT NULL DEFAULT 0,
        inode       INTEGER NOT NULL DEFAULT 0,
        is_dir      INTEGER NOT NULL DEFAULT 0,
        is_symlink  INTEGER NOT NULL DEFAULT 0,
        depth       INTEGER NOT NULL DEFAULT 0,
        scan_gen    INTEGER NOT NULL DEFAULT 0,
        is_deleted  INTEGER NOT NULL DEFAULT 0,
        text_state  TEXT    NOT NULL DEFAULT 'pending',
        text_bytes  INTEGER NOT NULL DEFAULT 0,
        unit_count  INTEGER NOT NULL DEFAULT 0,
        image_w     INTEGER NOT NULL DEFAULT 0,
        image_h     INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        created_at  REAL    NOT NULL,
        updated_at  REAL    NOT NULL,
        UNIQUE (kb_id, path)
    );
    CREATE INDEX IF NOT EXISTS idx_files_parent  ON files(kb_id, parent_rel, is_dir, name);
    CREATE INDEX IF NOT EXISTS idx_files_relpath ON files(kb_id, rel_path);
    CREATE INDEX IF NOT EXISTS idx_files_kind    ON files(kb_id, kind, is_deleted);
    CREATE INDEX IF NOT EXISTS idx_files_path    ON files(path);
    CREATE INDEX IF NOT EXISTS idx_files_textst  ON files(text_state);
    CREATE INDEX IF NOT EXISTS idx_files_mtime   ON files(kb_id, mtime_ns);
    """)


def _m_text_and_units(conn):
    """user_version 7：抽取文本 + 可定位单元（PDF 页 / 幻灯片 / 工作表 / DOCX 小节）。

    注意 ``files.hash`` 一列**刻意不建**——设计文档 §22 列了它，但 §23 同时说
    「不要每次对所有文件做 SHA-256」。改成用 (size, mtime_ns, ctime_ns, inode) 判增量，
    两处矛盾就此消解。
    """
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS file_text (
        file_id    INTEGER PRIMARY KEY,
        name       TEXT    NOT NULL DEFAULT '',
        path       TEXT    NOT NULL DEFAULT '',
        content    TEXT    NOT NULL DEFAULT '',
        lang       TEXT    NOT NULL DEFAULT '',
        -- 探测到的源文件编码。存下来是为了让「文本看起来是乱码」这件事可解释：
        -- 界面上显示「按 GB18030 解码」，用户就知道该怀疑哪一环。
        encoding   TEXT    NOT NULL DEFAULT '',
        chars      INTEGER NOT NULL DEFAULT 0,
        truncated  INTEGER NOT NULL DEFAULT 0,
        source     TEXT    NOT NULL DEFAULT 'native',
        ocr        INTEGER NOT NULL DEFAULT 0,
        error      TEXT,
        indexed_at REAL    NOT NULL
    );

    CREATE TABLE IF NOT EXISTS doc_units (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id   INTEGER NOT NULL,
        unit_type TEXT    NOT NULL,
        unit_no   INTEGER NOT NULL DEFAULT 0,
        label     TEXT    NOT NULL DEFAULT '',
        content   TEXT    NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_units_file ON doc_units(file_id, unit_type, unit_no);
    """)


def _m_library(conn):
    """user_version 8：收藏 / 最近阅读 / 自定义图标。

    设计文档 §22 给 favorites / recent_files 带了 user_id。**框架不暴露用户身份**
    （``server._parse_auth`` 只解析平台 CSRF 头，没有账号），所以去掉了 user_id，
    语义变成「设备级单份」——这一点已写进 PRIVACY.md。
    """
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS custom_icons (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kb_id       INTEGER,
        target_type TEXT    NOT NULL,
        target_path TEXT    NOT NULL UNIQUE,
        icon_type   TEXT    NOT NULL,
        icon_value  TEXT    NOT NULL,
        created_at  REAL    NOT NULL,
        updated_at  REAL    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_icons_kb ON custom_icons(kb_id);

    CREATE TABLE IF NOT EXISTS favorites (
        path     TEXT PRIMARY KEY,
        kb_id    INTEGER,
        name     TEXT NOT NULL DEFAULT '',
        added_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS recents (
        path        TEXT PRIMARY KEY,
        kb_id       INTEGER,
        name        TEXT NOT NULL DEFAULT '',
        last_opened REAL    NOT NULL,
        open_count  INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS idx_recents_time ON recents(last_opened DESC);
    """)


def _m_fts(conn):
    """user_version 9：全文索引（**守卫式**创建）。

    ``tnasapp.store.Store.migrate`` 对 callable 迁移会**重新抛出**异常，所以这里必须自己
    兜住 ``sqlite3.OperationalError``：目标机若没有编译进 FTS5，应用仍要能正常启动、
    搜索降级为 LIKE（见 ``search.py``）。结果写入 ``app_meta``，供 ``/api/status`` 上报。

    两个索引各司其职（都是实测结论）：
      * ``fts_word`` —— unicode61。拉丁词与「被空格/标点分隔开的 CJK 串」可检索，
        ``bm25`` 权重可用，是主排序索引。
      * ``fts_tri`` —— trigram。覆盖 ≥3 字符的**子串**与长 CJK 串内部的中文词。
        ``unicode61`` 把连续 CJK 当成一个整词，所以「长串内部的 2 字子串」它命不中，
        这正是 trigram 存在的理由。
    """
    ok_word = ok_tri = False
    detail = ""

    try:
        conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_word USING fts5(
            name, path, content,
            content='file_text', content_rowid='file_id',
            tokenize="unicode61 remove_diacritics 2"
        );
        CREATE TRIGGER IF NOT EXISTS file_text_ai AFTER INSERT ON file_text BEGIN
            INSERT INTO fts_word(rowid, name, path, content)
            VALUES (new.file_id, new.name, new.path, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS file_text_ad AFTER DELETE ON file_text BEGIN
            INSERT INTO fts_word(fts_word, rowid, name, path, content)
            VALUES ('delete', old.file_id, old.name, old.path, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS file_text_au AFTER UPDATE ON file_text BEGIN
            INSERT INTO fts_word(fts_word, rowid, name, path, content)
            VALUES ('delete', old.file_id, old.name, old.path, old.content);
            INSERT INTO fts_word(rowid, name, path, content)
            VALUES (new.file_id, new.name, new.path, new.content);
        END;
        """)
        ok_word = True
    except Exception as exc:  # noqa: BLE001 —— 目标机没有 FTS5 时必须继续启动
        detail = "%s: %s" % (type(exc).__name__, exc)

    if ok_word:
        try:
            conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_tri USING fts5(
                name, path, content, tokenize='trigram'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_unit USING fts5(
                content, tokenize='trigram'
            );
            """)
            ok_tri = True
        except Exception as exc:  # noqa: BLE001 —— trigram 需要 SQLite ≥ 3.34
            detail = (detail + " | " if detail else "") + "%s: %s" % (type(exc).__name__, exc)

    _meta_set_conn(conn, "fts5", "1" if ok_word else "0")
    _meta_set_conn(conn, "fts_trigram", "1" if ok_tri else "0")
    try:
        import sqlite3 as _s

        _meta_set_conn(conn, "sqlite_version", _s.sqlite_version)
    except Exception:  # noqa: BLE001
        pass
    if detail:
        _meta_set_conn(conn, "fts_error", detail[:500])


MIGRATIONS = [
    _m_meta_and_kbs,
    _m_files,
    _m_text_and_units,
    _m_library,
    _m_fts,
]


# ---------------------------------------------------------------- 元数据

def _meta_set_conn(conn, key, value):
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(key), str(value)),
    )


def meta_get(app, key, default=None):
    row = app.store.query_one("SELECT value FROM app_meta WHERE key=?", (str(key),))
    return row["value"] if row else default


def meta_set(app, key, value):
    app.store.execute(
        "INSERT INTO app_meta(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(key), str(value)),
    )


def meta_int(app, key, default=0):
    try:
        return int(meta_get(app, key, default))
    except (TypeError, ValueError):
        return default


def next_scan_gen(app):
    """自增并返回本次扫描的代次。软删除靠它区分「本代没出现」与「本代刚加进来」。"""
    gen = meta_int(app, "scan_gen", 0) + 1
    meta_set(app, "scan_gen", gen)
    return gen


# ---------------------------------------------------------------- 能力探测

def fts_ready(app):
    """FTS5 主索引是否可用。整份代码里**唯一**的能力判据。"""
    return meta_get(app, "fts5", "0") == "1"


def trigram_ready(app):
    return meta_get(app, "fts_trigram", "0") == "1" and fts_ready(app)


def sqlite_version(app):
    return meta_get(app, "sqlite_version", "")


# ---------------------------------------------------------------- 文件行

def file_row(app, kb_id, path):
    return app.store.query_one(
        "SELECT * FROM files WHERE kb_id=? AND path=?", (int(kb_id), path)
    )


def file_by_id(app, file_id):
    return app.store.query_one("SELECT * FROM files WHERE id=?", (int(file_id),))


def file_id_for(app, kb_id, path):
    row = app.store.query_one(
        "SELECT id FROM files WHERE kb_id=? AND path=?", (int(kb_id), path)
    )
    return int(row["id"]) if row else None


UPSERT_FILE_SQL = """
INSERT INTO files (kb_id, parent_rel, path, rel_path, name, ext, mime, kind,
                   size, mtime_ns, ctime_ns, inode, is_dir, is_symlink, depth,
                   scan_gen, is_deleted, text_state, image_w, image_h,
                   created_at, updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'pending',?,?,?,?)
ON CONFLICT(kb_id, path) DO UPDATE SET
    parent_rel = excluded.parent_rel,
    name       = excluded.name,
    ext        = excluded.ext,
    mime       = excluded.mime,
    kind       = excluded.kind,
    is_dir     = excluded.is_dir,
    is_symlink = excluded.is_symlink,
    depth      = excluded.depth,
    scan_gen   = excluded.scan_gen,
    is_deleted = 0,
    updated_at = excluded.updated_at,
    -- 只有「身份」变了才需要重新抽取文本；没变就保留原状态，增量扫描因此才有可能
    text_state = CASE
        WHEN files.size <> excluded.size
          OR files.mtime_ns <> excluded.mtime_ns
          OR files.inode <> excluded.inode
        THEN 'pending'
        ELSE files.text_state
    END,
    size     = excluded.size,
    mtime_ns = excluded.mtime_ns,
    ctime_ns = excluded.ctime_ns,
    inode    = excluded.inode,
    error    = CASE
        WHEN files.size <> excluded.size
          OR files.mtime_ns <> excluded.mtime_ns
          OR files.inode <> excluded.inode
        THEN NULL
        ELSE files.error
    END
"""


def upsert_files(app, rows):
    """批量写入扫描结果。``rows`` 是已按 UPSERT_FILE_SQL 占位顺序排好的元组序列。

    **必须批量**：``Store`` 每次调用都新开一条 SQLite 连接，逐文件调用会把
    十万文件的扫描拖到不可用（见 ``paths.SCAN_BATCH``）。
    """
    if not rows:
        return 0
    app.store.executemany(UPSERT_FILE_SQL, rows)
    return len(rows)


def finalize_scan(app, kb_id, gen):
    """把本代未出现的行标记为已删除（**软删除**，不物理删）。

    软删除而非物理删的理由：收藏与最近阅读按路径引用文件，物理删会让它们变成悬空引用；
    而且用户可能只是把目录临时挪走，挪回来时历史记录应当还在。
    """
    cur = app.store.execute(
        "UPDATE files SET is_deleted=1, scan_gen=?, updated_at=?"
        " WHERE kb_id=? AND scan_gen<>? AND is_deleted=0",
        (int(gen), time.time(), int(kb_id), int(gen)),
        commit=True,
    )
    return cur


def refresh_kb_stats(app, kb_id):
    """重算知识库的文件数与总字节数（只统计未删除的非目录项）。"""
    row = app.store.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM files"
        " WHERE kb_id=? AND is_deleted=0 AND is_dir=0",
        (int(kb_id),),
    )
    n = int(row["n"] or 0) if row else 0
    total = int(row["b"] or 0) if row else 0
    app.store.execute(
        "UPDATE knowledge_bases SET file_count=?, total_bytes=?, updated_at=?"
        " WHERE id=?",
        (n, total, time.time(), int(kb_id)),
    )
    return {"file_count": n, "total_bytes": total}


def purge_kb(app, kb_id):
    """删除一个知识库的**索引行**。绝不触碰磁盘上的任何真实文件。"""
    with_ids = app.store.query(
        "SELECT id FROM files WHERE kb_id=?", (int(kb_id),)
    )
    ids = [int(r["id"]) for r in with_ids]
    if ids:
        # 分块删除，避免 SQLite 变量数上限
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            app.store.execute("DELETE FROM file_text WHERE file_id IN (%s)" % marks, tuple(chunk))
            app.store.execute("DELETE FROM doc_units WHERE file_id IN (%s)" % marks, tuple(chunk))
            app.store.execute("DELETE FROM files WHERE id IN (%s)" % marks, tuple(chunk))
    app.store.execute("DELETE FROM custom_icons WHERE kb_id=?", (int(kb_id),))
    app.store.execute("DELETE FROM knowledge_bases WHERE id=?", (int(kb_id),))
    return len(ids)


# ---------------------------------------------------------------- 文本与单元

UPSERT_TEXT_SQL = """
INSERT INTO file_text (file_id, name, path, content, lang, encoding, chars,
                       truncated, source, ocr, error, indexed_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(file_id) DO UPDATE SET
    name=excluded.name, path=excluded.path, content=excluded.content,
    lang=excluded.lang, encoding=excluded.encoding, chars=excluded.chars,
    truncated=excluded.truncated, source=excluded.source, ocr=excluded.ocr,
    error=excluded.error, indexed_at=excluded.indexed_at
"""


def text_put(app, file_id, name, path, content, lang="", encoding="",
             truncated=0, source="native", error=None):
    """写入抽取文本。**必须走这条语句**——``INSERT OR REPLACE`` 不会触发
    ``AFTER DELETE`` 触发器，会让 FTS 索引残留旧词（见模块 docstring 第 2 条）。"""
    app.store.execute(
        UPSERT_TEXT_SQL,
        (int(file_id), name or "", path or "", content or "", lang or "",
         encoding or "", len(content or ""), int(truncated), source or "native",
         0, error, time.time()),
    )


def text_drop(app, file_id):
    """删除某文件的抽取文本（其 FTS 行由触发器一并清掉）。"""
    app.store.execute("DELETE FROM file_text WHERE file_id=?", (int(file_id),))


def text_get(app, file_id):
    return app.store.query_one("SELECT * FROM file_text WHERE file_id=?", (int(file_id),))


def units_replace(app, file_id, units):
    """整体替换某文件的可定位单元。``units`` 为 ``(unit_type, unit_no, label, content)``。"""
    app.store.execute("DELETE FROM doc_units WHERE file_id=?", (int(file_id),))
    if not units:
        return 0
    rows = [(int(file_id), str(t), int(no), str(label or ""), str(content or ""))
            for (t, no, label, content) in units]
    app.store.executemany(
        "INSERT INTO doc_units (file_id, unit_type, unit_no, label, content)"
        " VALUES (?,?,?,?,?)",
        rows,
    )
    return len(rows)


def units_for(app, file_id, unit_type=None, limit=5000):
    if unit_type:
        return app.store.query(
            "SELECT * FROM doc_units WHERE file_id=? AND unit_type=?"
            " ORDER BY unit_no LIMIT ?",
            (int(file_id), unit_type, int(limit)),
        )
    return app.store.query(
        "SELECT * FROM doc_units WHERE file_id=? ORDER BY unit_type, unit_no LIMIT ?",
        (int(file_id), int(limit)),
    )


def fts_index_units(app, units_with_ids):
    """把可定位单元写进 trigram 索引，供「命中在 PDF 第几页」使用。

    ``units_with_ids`` 为 ``(doc_units.id, content)``。仅在 trigram 可用时写入。
    """
    if not trigram_ready(app) or not units_with_ids:
        return 0
    rows = [(int(uid), str(content or "")) for uid, content in units_with_ids]
    app.store.executemany("DELETE FROM fts_unit WHERE rowid=?", [(r[0],) for r in rows])
    app.store.executemany(
        "INSERT INTO fts_unit(rowid, content) VALUES (?,?)", rows
    )
    return len(rows)


def fts_drop_file(app, file_id):
    """把一个文件从所有 FTS 索引里摘掉（trigram 表不是 external-content，需显式删）。"""
    app.store.execute("DELETE FROM fts_tri WHERE rowid=?", (int(file_id),))
    unit_ids = [int(r["id"]) for r in app.store.query(
        "SELECT id FROM doc_units WHERE file_id=?", (int(file_id),)
    )]
    for uid in unit_ids:
        app.store.execute("DELETE FROM fts_unit WHERE rowid=?", (uid,))


def counts(app):
    """给 /api/status 用的总体计数。"""
    out = {}
    for key, sql in (
        ("kbs", "SELECT COUNT(*) AS n FROM knowledge_bases"),
        ("files", "SELECT COUNT(*) AS n FROM files WHERE is_deleted=0 AND is_dir=0"),
        ("dirs", "SELECT COUNT(*) AS n FROM files WHERE is_deleted=0 AND is_dir=1"),
        ("indexed", "SELECT COUNT(*) AS n FROM files WHERE is_deleted=0 AND text_state='ok'"),
        ("pending", "SELECT COUNT(*) AS n FROM files WHERE is_deleted=0 AND text_state='pending'"),
        ("failed", "SELECT COUNT(*) AS n FROM files WHERE is_deleted=0 AND text_state='failed'"),
        ("unsupported", "SELECT COUNT(*) AS n FROM files WHERE is_deleted=0 AND text_state='unsupported'"),
        ("bytes", "SELECT COALESCE(SUM(size),0) AS n FROM files WHERE is_deleted=0 AND is_dir=0"),
    ):
        row = app.store.query_one(sql)
        out[key] = int(row["n"] or 0) if row else 0
    return out


def db_size(app):
    """数据库文件占用的字节数（含 WAL），用于运行时文件清单的「增长上界」说明。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(app.paths.db_path + suffix)
        except OSError:
            continue
    return total
