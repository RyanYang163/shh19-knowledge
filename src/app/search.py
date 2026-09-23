"""全文检索：FTS5 双索引 + 降级 + 片段高亮。

### 两个索引各司其职（都是实测结论）

* ``fts_word``（``unicode61``）—— 拉丁词、以及**被空格/标点分隔开的 CJK 串**。
  ``bm25`` 权重可用，是主排序索引。
* ``fts_tri``（``trigram``）—— 覆盖 ≥3 字符的**子串**与长 CJK 串内部的中文词。
  存在的理由是 ``unicode61`` 把连续 CJK 当成**一个整词**：
  「版本号与硬盘兼容性」是一个 token，查「硬盘」命不中；trigram 能命中。

### 中文检索的**真实边界**（不掩盖，界面上如实标注）

| 查询 | 走哪条路 |
|---|---|
| 拉丁词 / 前缀 | ``fts_word``，正常排序 |
| 恰好等于文中一个被分隔的 CJK 串 | ``fts_word``（实测能命中） |
| ≥3 字的 CJK 子串 | ``fts_tri`` |
| **1–2 字的 CJK 子串** | **降级为有上限的 ``LIKE`` 扫描**（``unicode61`` 与 ``trigram`` 都覆盖不到） |

降级时响应里带 ``engine`` / ``degraded`` / ``hint``，前端显示徽标 ——
**绝不假装全文检索成功了**。

### 转义

用户输入**绝不拼接**进 MATCH。FTS5 有自己的语法（``"`` ``*`` ``(`` ``-`` ``:`` ``^``
``AND/OR/NOT`` ``NEAR``），原样拼进去轻则报错重则语义被改写。做法是把输入切成词，
每个词用双引号包成字面量短语（内部的 ``"`` 双写）。**词为空时直接返回空结果，
绝不把空串送进 MATCH** —— 实测 ``MATCH '""'`` 会抛 FTS5 语法错误。
"""

import html
import re

from . import config, db

#: 词法切分：字母数字、CJK 统一表意、假名、谚文都算作词的一部分
_TOKEN_RE = re.compile(r"[\w㐀-䶿一-鿿぀-ヿ가-힯]+",
                       re.UNICODE)
_CJK_RE = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]")

#: 传给 MATCH 的最大词数（防超长查询把查询计划撑爆）
MAX_TERMS = 16
#: 候选集上限
CANDIDATE_LIMIT = 300
#: 降级 LIKE 扫描的行数上限
LIKE_LIMIT = 500
#: snippet 用的哨兵字符，先把标记塞进去、整体转义后再换回来
_SENT_OPEN = "\x01"
_SENT_CLOSE = "\x02"


def _tokens(query):
    return _TOKEN_RE.findall(query or "")[:MAX_TERMS]


def build_match_query(query, prefix_last=True):
    """把用户输入转成安全的 FTS5 MATCH 表达式。

    ``""`` 表示「没有可检索的词」，调用方必须直接返回空结果而不是拿去 MATCH。
    """
    terms = _tokens(query)
    if not terms:
        return ""
    parts = []
    for index, term in enumerate(terms):
        quoted = '"' + term.replace('"', '""') + '"'
        # 只在最后一个词且是纯 ASCII 且够长时加前缀通配 —— 中文加 * 没有意义
        if (prefix_last and index == len(terms) - 1 and len(term) >= 3
                and term.isascii()):
            quoted += "*"
        parts.append(quoted)
    return " AND ".join(parts)


def build_prefix_query(query):
    """供「快速打开」（Ctrl+P）用的前缀查询：只查 name 列。"""
    terms = _tokens(query)
    if not terms:
        return ""
    term = terms[-1]
    return 'name:"%s"*' % term.replace('"', '""')


def _has_cjk(text):
    return bool(_CJK_RE.search(text or ""))


def _short_cjk_only(query):
    """查询是否「只由 1–2 字的 CJK 词构成」—— 这类查询两个索引都覆盖不到。"""
    terms = _tokens(query)
    if not terms:
        return False
    for term in terms:
        if term.isascii():
            return False
        if _has_cjk(term) and len(term) > 2:
            return False
        if not _has_cjk(term):
            return False
    return True


def engine_for(app, query):
    """判断本次查询会走哪条路。返回 ``(engine, degraded, hint)``。"""
    if not db.fts_ready(app):
        return ("like", True,
                "本机的 SQLite 未启用 FTS5，已降级为模糊匹配（速度较慢）。")
    if _short_cjk_only(query):
        return ("like", True,
                "1–2 字的中文查询无法走全文索引，已降级为模糊匹配。"
                "输入 3 个字以上可得到更精确的结果。")
    if db.trigram_ready(app) and _has_cjk(query):
        return ("fts5+trigram", False, None)
    return ("fts5", False, None)


# ---------------------------------------------------------------- 片段与高亮

def _highlight(text, terms):
    """把命中的词包成 ``<mark>``。**先整体转义再插入标记。**

    顺序反了，一个名为 ``<img onerror=…>`` 的文件就能把 HTML 注进页面 ——
    转义发生在插入标记之前，标记才是唯一不被转义的 HTML。
    """
    if not text:
        return ""
    escaped = html.escape(text, quote=False)
    for term in sorted(set(terms), key=len, reverse=True):
        if not term:
            continue
        pattern = re.compile(re.escape(html.escape(term, quote=False)), re.IGNORECASE)
        escaped = pattern.sub(lambda m: "<mark>%s</mark>" % m.group(0), escaped)
    return escaped


def _clean_snippet(raw):
    """把 ``snippet()`` 用哨兵标记包出来的片段转成安全 HTML。"""
    if not raw:
        return ""
    return (html.escape(raw, quote=False)
            .replace(_SENT_OPEN, "<mark>")
            .replace(_SENT_CLOSE, "</mark>"))


# ---------------------------------------------------------------- 过滤

def _filter_clause(args, params):
    clauses = ["f.is_deleted=0", "f.is_dir=0"]
    kb_id = args.get("kb")
    if kb_id not in (None, "", "0"):
        clauses.append("f.kb_id=?")
        params.append(int(kb_id))

    kinds = [k for k in (args.get("kind") or "").split(",") if k.strip()]
    if kinds:
        clauses.append("f.kind IN (%s)" % ",".join("?" * len(kinds)))
        params.extend(kinds)

    exts = [e.strip().lower().lstrip(".") for e in (args.get("ext") or "").split(",")
            if e.strip()]
    if exts:
        clauses.append("f.ext IN (%s)" % ",".join("?" * len(exts)))
        params.extend("." + e for e in exts)

    since = args.get("since")
    if since:
        try:
            clauses.append("f.mtime_ns >= ?")
            params.append(int(float(since) * 1_000_000_000))
        except (TypeError, ValueError):
            pass
    until = args.get("until")
    if until:
        try:
            clauses.append("f.mtime_ns <= ?")
            params.append(int(float(until) * 1_000_000_000))
        except (TypeError, ValueError):
            pass

    prefix = args.get("path_prefix")
    if prefix:
        clauses.append("(f.rel_path = ? OR f.rel_path LIKE ?)")
        params.append(prefix.strip("/"))
        params.append(prefix.strip("/") + "/%")
    return " AND ".join(clauses)


def _rerank(rows, terms, favorites):
    """在 Python 里重排。

    为什么不在 SQL 里做：``bm25`` 对 trigram 索引几乎不可用，而降级 LIKE 更是没有分数；
    把三种来源（word / tri / like）统一到一套可解释、可单测的评分里最省心。
    """
    lowered = [t.lower() for t in terms]
    scored = []
    for row in rows:
        name = (row.get("name") or "").lower()
        rel = (row.get("rel_path") or "").lower()
        score = 0.0
        for term in lowered:
            if name == term:
                score += 4.0
            elif name.startswith(term):
                score += 2.5
            elif term in name:
                score += 1.8
            if term in rel:
                score += 0.9
        score += float(row.get("rank_score") or 0.0)
        if row.get("path") in favorites:
            score += 0.4
        # 轻微偏好较新的文件，但不足以压过名字命中的差距
        age_days = max(0.0, (row.get("age_seconds") or 0.0) / 86400.0)
        import math

        score += min(0.3, 0.3 * math.exp(-age_days / 90.0))
        row["score"] = round(score, 4)
        scored.append(row)
    scored.sort(key=lambda item: (-item["score"], item["name"].lower()))
    return scored


# ---------------------------------------------------------------- 主检索

def search(app, args):
    """执行一次检索。``args`` 是 ``Request.query`` 形态的映射（值为列表）。"""
    from . import kbs

    query = (args.get("q") or [""])[0] if isinstance(args.get("q"), list) else (args.get("q") or "")
    query = str(query).strip()
    limit = _clamp(args.get("limit"), 1, 200, 40)
    offset = _clamp(args.get("offset"), 0, 100000, 0)
    sort = (args.get("sort") or ["relevance"])[0]
    favorite_only = str(args.get("fav") or "") .lower() in ("1", "true", "yes", "on")

    flat = {key: (value[0] if isinstance(value, list) and value else value)
            for key, value in args.items()}
    engine, degraded, hint = engine_for(app, query)

    if not query:
        return {"ok": True, "query": "", "results": [], "total": 0,
                "engine": engine, "degraded": False, "hint": None,
                "terms": []}

    terms = _tokens(query)
    favorites = {row["path"] for row in
                 app.store.query("SELECT path FROM favorites")}

    params = []
    where = _filter_clause(flat, params)
    rows = _candidates(app, query, where, params, engine)
    if favorite_only:
        rows = [row for row in rows if row["path"] in favorites]

    for row in rows:
        row["age_seconds"] = _age(app, row)
    rows = _rerank(rows, terms, favorites)

    total = len(rows)
    if sort == "time":
        rows.sort(key=lambda item: -(item.get("mtime_ns") or 0))
    elif sort == "name":
        rows.sort(key=lambda item: item["name"].lower())
    window = rows[offset:offset + limit]

    _attach_snippets(app, window, query, engine)
    _attach_units(app, window, query)
    results = [_shape(app, row) for row in window]

    return {
        "ok": True,
        "query": query,
        "terms": terms,
        "results": results,
        "total": total,
        "offset": offset,
        "limit": limit,
        "engine": engine,
        "degraded": degraded,
        "hint": hint,
    }


def _clamp(values, low, high, default):
    try:
        value = int(values[0] if isinstance(values, list) else values)
    except (TypeError, ValueError, IndexError):
        return default
    return max(low, min(high, value))


def _age(app, row):
    import time

    mtime = (row.get("mtime_ns") or 0) / 1_000_000_000.0
    return max(0.0, time.time() - mtime) if mtime else 0.0


def _candidates(app, query, where, params, engine):
    """收集候选行。三来源合并后去重。

    ⚠️ **过滤条件必须 JOIN 进 FTS 查询本身**。早期版本先查 ``fts_word`` 拿到 rowid，
    再用 ``SELECT * FROM files WHERE id=?`` 逐条取行 —— 那条路径上没有 where，
    于是 ``kind`` / ``path_prefix`` / ``since`` 这些过滤对正常查询**静默失效**，
    只在 LIKE 降级路径上才生效。这是个不报错但结果错的 bug。
    """
    found = {}

    if engine != "like":
        match = build_match_query(query)
        if match:
            try:
                rows = app.store.query(
                    "SELECT f.*, bm25(fts_word, 10.0, 4.0, 1.0) AS rank"
                    " FROM fts_word JOIN files f ON f.id = fts_word.rowid"
                    " WHERE fts_word MATCH ? AND (%s)"
                    " ORDER BY rank LIMIT ?" % where,
                    (match,) + tuple(params) + (CANDIDATE_LIMIT,))
                for row in rows:
                    raw_rank = float(row.pop("rank", 0) or 0)
                    # bm25 越小越相关，转成「越大越好」的近似分（仅作次要权重）
                    row["rank_score"] = 0.35 * max(0.0, min(1.0, -raw_rank / 10.0))
                    found[row["id"]] = row
            except Exception as exc:  # noqa: BLE001 —— 查询语法问题不该 500
                app.log.warning("FTS 查询失败，回退模糊匹配：%s", exc)
                engine = "like"

    if engine == "fts5+trigram" and db.trigram_ready(app):
        match = build_match_query(query, prefix_last=False)
        if match:
            try:
                rows = app.store.query(
                    "SELECT f.* FROM fts_tri JOIN files f ON f.id = fts_tri.rowid"
                    " WHERE fts_tri MATCH ? AND (%s) LIMIT ?" % where,
                    (match,) + tuple(params) + (CANDIDATE_LIMIT,))
                for row in rows:
                    if row["id"] in found:
                        continue
                    row["rank_score"] = 0.05   # trigram 只当候选来源，不靠它排序
                    found[row["id"]] = row
            except Exception as exc:  # noqa: BLE001
                app.log.debug("trigram 查询失败：%s", exc)

    if engine == "like" or len(found) < limit_floor():
        _like_candidates(app, query, where, params, found)
    return list(found.values())


def limit_floor():
    return 12


def _like_candidates(app, query, where, params, found):
    """有上限的模糊扫描。**所有值都走参数绑定**，绝不拼接。"""
    like = "%" + query.replace("%", r"\%").replace("_", r"\_") + "%"
    sql = ("SELECT f.* FROM files f LEFT JOIN file_text t ON t.file_id = f.id"
           " WHERE %s AND (f.name LIKE ? ESCAPE '\\' OR f.rel_path LIKE ? ESCAPE '\\'"
           "               OR t.content LIKE ? ESCAPE '\\')"
           " ORDER BY f.mtime_ns DESC LIMIT ?" % where)
    try:
        rows = app.store.query(sql, tuple(params) + (like, like, like, LIKE_LIMIT))
    except Exception as exc:  # noqa: BLE001
        app.log.warning("模糊匹配失败：%s", exc)
        return
    for row in rows:
        if row["id"] not in found:
            row["rank_score"] = 0.0
            found[row["id"]] = row


def _attach_snippets(app, rows, query, engine):
    """给结果附上高亮片段。优先用 FTS 的 ``snippet()``，降级时自己截。"""
    if not rows:
        return
    ids = [row["id"] for row in rows]
    marks = ",".join("?" * len(ids))
    snippets = {}
    if engine != "like" and db.fts_ready(app):
        match = build_match_query(query)
        if match:
            try:
                for hit in app.store.query(
                        "SELECT rowid, snippet(fts_word, 2, char(1), char(2), ' … ', 18)"
                        " AS snip FROM fts_word"
                        " WHERE fts_word MATCH ? AND rowid IN (%s)" % marks,
                        (match,) + tuple(ids)):
                    snippets[hit["rowid"]] = _clean_snippet(hit["snip"])
            except Exception as exc:  # noqa: BLE001
                app.log.debug("片段生成失败：%s", exc)

    terms = _tokens(query)
    for row in rows:
        if snippets.get(row["id"]):
            row["snippet_html"] = snippets[row["id"]]
            continue
        row["snippet_html"] = _fallback_snippet(app, row, terms)


def _fallback_snippet(app, row, terms):
    """没有 FTS 片段时，从抽取文本里自己截一段并高亮。"""
    text_row = db.text_get(app, row["id"])
    content = (text_row or {}).get("content") or ""
    if content:
        lowered = content.lower()
        position = -1
        for term in terms:
            position = lowered.find(term.lower())
            if position >= 0:
                break
        if position < 0:
            position = 0
        start = max(0, position - 60)
        excerpt = content[start:start + 220]
        if start > 0:
            excerpt = "… " + excerpt
        return _highlight(excerpt, terms)
    return _highlight(row.get("rel_path") or "", terms)


def _shape(app, row):
    """把一行整理成前端需要的形状。"""
    return {
        "id": row["id"],
        "kb_id": row["kb_id"],
        "path": row["path"],
        "rel_path": row["rel_path"],
        "name": row["name"],
        "ext": row["ext"],
        "kind": row["kind"],
        "size": row["size"],
        "mtime_ns": row["mtime_ns"],
        "text_state": row.get("text_state"),
        "score": row.get("score"),
        "snippet_html": row.get("snippet_html") or "",
        "icon": config.ICON_BY_KIND.get(row["kind"], "file"),
        # 「命中在 PDF 第 23 页 / 工作表 X」这类可定位信息，由 _attach_units 填
        "unit": row.get("unit"),
    }


def _attach_units(app, rows, query):
    """把可定位单元（页/幻灯片/工作表/小节）挂到命中的结果上。"""
    if not rows or not query:
        return
    units = search_units(app, query)
    if not units:
        return
    for row in rows:
        hit = units.get(row["id"])
        if hit:
            unit_type, unit_no, label = hit
            row["unit"] = {"type": unit_type, "no": unit_no, "label": label}


def search_units(app, query, limit=20):
    """在可定位单元里找命中，返回 ``{file_id: (unit_type, unit_no, label)}``。

    用来把「命中在 PDF 第 23 页」这样的信息带给用户；没有 trigram 时返回空。
    """
    if not query or not db.trigram_ready(app):
        return {}
    match = build_match_query(query, prefix_last=False)
    if not match:
        return {}
    try:
        hits = app.store.query(
            "SELECT u.file_id, u.unit_type, u.unit_no, u.label"
            " FROM fts_unit ft JOIN doc_units u ON u.id = ft.rowid"
            " WHERE ft.content MATCH ? LIMIT ?", (match, limit))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for hit in hits:
        out.setdefault(hit["file_id"],
                       (hit["unit_type"], hit["unit_no"], hit["label"]))
    return out


def suggest(app, query, limit=30):
    """快速打开：只查文件名与相对路径，永远很快。"""
    text = (query or "").strip()
    if not text:
        recent = app.store.query(
            "SELECT r.path, r.name, r.kb_id, f.rel_path, f.kind, f.ext, f.mtime_ns"
            " FROM recents r LEFT JOIN files f ON f.path = r.path"
            " ORDER BY r.last_opened DESC LIMIT ?", (int(limit),))
        return {"ok": True, "query": "", "results": [
            {"path": row["path"], "name": row["name"], "kb_id": row["kb_id"],
             "rel_path": row.get("rel_path") or "", "kind": row.get("kind") or "other",
             "icon": config.ICON_BY_KIND.get(row.get("kind") or "other", "file")}
            for row in recent], "source": "recent"}

    like = "%" + text.replace("%", r"\%").replace("_", r"\_") + "%"
    rows = app.store.query(
        "SELECT path, name, kb_id, rel_path, kind, ext, mtime_ns FROM files"
        " WHERE is_deleted=0 AND is_dir=0"
        " AND (name LIKE ? ESCAPE '\\' OR rel_path LIKE ? ESCAPE '\\')"
        " ORDER BY CASE WHEN name LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,"
        "          LENGTH(name), name LIMIT ?",
        (like, like, text.replace("%", r"\%").replace("_", r"\_") + "%", int(limit)))
    return {
        "ok": True,
        "query": text,
        "source": "files",
        "results": [
            {"path": row["path"], "name": row["name"], "kb_id": row["kb_id"],
             "rel_path": row["rel_path"], "kind": row["kind"] or "other",
             "ext": row["ext"], "mtime_ns": row["mtime_ns"],
             "icon": config.ICON_BY_KIND.get(row["kind"] or "other", "file")}
            for row in rows],
    }


def related(app, path, kb_id=None, limit=12):
    """相关文件：同目录优先，其次名字里有共同词，再次同类型。

    V1 刻意**不做 embedding**（那需要外部模型）；这里全部是本地可解释的启发式，
    界面上如实说「基于文件名与目录」而不是「AI 推荐」。
    """
    row = app.store.query_one("SELECT * FROM files WHERE path=?", (path,))
    if not row:
        return []
    tokens = [t.lower() for t in _TOKENS_FOR_RELATED.findall(row["name"]) if len(t) > 1]
    results = {}
    # 同目录
    for item in app.store.query(
            "SELECT * FROM files WHERE kb_id=? AND parent_rel=? AND is_deleted=0"
            " AND is_dir=0 AND path<>? ORDER BY mtime_ns DESC LIMIT ?",
            (row["kb_id"], row["parent_rel"], path, int(limit))):
        item["reason"] = "同目录"
        item["weight"] = 3.0
        results[item["id"]] = item

    if tokens:
        clause = " OR ".join(["LOWER(name) LIKE ?"] * len(tokens))
        for item in app.store.query(
                "SELECT * FROM files WHERE kb_id=? AND is_deleted=0 AND is_dir=0"
                " AND path<>? AND (%s) LIMIT ?" % clause,
                (row["kb_id"], path) + tuple("%" + t + "%" for t in tokens)
                + (int(limit),)):
            if item["id"] in results:
                continue
            item["reason"] = "文件名相近"
            item["weight"] = 2.0
            results[item["id"]] = item

    if len(results) < limit:
        for item in app.store.query(
                "SELECT * FROM files WHERE kb_id=? AND kind=? AND is_deleted=0"
                " AND is_dir=0 AND path<>? ORDER BY mtime_ns DESC LIMIT ?",
                (row["kb_id"], row["kind"], path, int(limit))):
            if item["id"] in results:
                continue
            item["reason"] = "同类型"
            item["weight"] = 1.0
            results[item["id"]] = item

    ordered = sorted(results.values(), key=lambda item: (-item["weight"],
                                                         -int(item["mtime_ns"] or 0)))
    return [{
        "id": item["id"], "path": item["path"], "name": item["name"],
        "rel_path": item["rel_path"], "kind": item["kind"], "ext": item["ext"],
        "size": item["size"], "mtime_ns": item["mtime_ns"],
        "reason": item["reason"],
        "icon": config.ICON_BY_KIND.get(item["kind"], "file"),
    } for item in ordered[:limit]]


_TOKENS_FOR_RELATED = re.compile(r"[A-Za-z0-9一-鿿]+")
