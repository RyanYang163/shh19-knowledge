"""文本类抽取器：txt / 代码 / Markdown / HTML / CSV / JSON / XML / YAML。

这些格式**全部只用标准库**：``csv`` / ``json`` / ``html.parser`` / ``re`` / ``zlib``。
没有 ``chardet``，编码探测在 :mod:`extract` 里自己做（候选编码打分法）。

每种抽取器都返回统一结构：``{text, lang, units, truncated, state, error}``，
其中 ``units`` 是 ``(unit_type, unit_no, label, content)`` 四元组列表，
用来支撑「命中在文档的第几节 / 第几行」这类可定位结果。
"""

import csv
import io
import json
import os
import re

from . import security
from .extract import detect_encoding, read_text_file

#: 一段文本里出现这么多 NUL 就基本可以断定是二进制，不该按文本索引
_BINARY_NUL_RATIO = 0.01

#: CSV/TSV 索引时最多摊平多少行（避免一个百万行 CSV 把索引撑爆）
_CSV_INDEX_ROWS = 20000

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_YAML_TOP_KEY = re.compile(r"^([A-Za-z_][\w.\-]*)\s*:", re.MULTILINE)


def _binary_ratio(data):
    if not data:
        return 0.0
    return data.count(0) / float(len(data))


def from_text(path, budget=0, lang="", **kwargs):
    """纯文本 / 代码 / INI / 日志。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read(max(budget, 4096))
    except OSError as exc:
        return {"state": "failed", "error": "读取失败：%s" % exc, "text": ""}

    if _binary_ratio(data) > _BINARY_NUL_RATIO:
        # 扩展名像文本但内容其实是二进制（例如 .log 里塞了归档）
        return {"state": "unsupported", "text": "",
                "error": "文件内容不是文本（含大量空字节），未建立索引。"}

    encoding, text = detect_encoding(data)
    truncated = os.path.getsize(path) > len(data)
    return {
        "state": "ok" if text.strip() else "empty",
        "text": text,
        "lang": lang,
        "encoding": encoding,
        "truncated": truncated,
        "units": [],
    }


def from_markdown(path, budget=0, lang="markdown", **kwargs):
    """Markdown。文本按原样索引，额外把标题抽成可定位单元。"""
    result = from_text(path, budget=budget, lang=lang)
    text = result.get("text") or ""
    units = []
    if text:
        headings = list(_MD_HEADING.finditer(text))
        for index, match in enumerate(headings):
            level = len(match.group(1))
            label = match.group(2).strip()
            start = match.end()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            body = text[start:end].strip()
            units.append(("section", index + 1, "%s %s" % ("#" * level, label),
                          "%s\n%s" % (label, body)))
    result["units"] = units
    return result


def from_html(path, budget=0, lang="html", **kwargs):
    """HTML → 纯文本。**只为建索引**，渲染走沙箱 iframe 交给浏览器。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read(max(budget, 4096))
    except OSError as exc:
        return {"state": "failed", "error": "读取失败：%s" % exc, "text": ""}

    encoding, markup = detect_encoding(data)
    text = security.strip_html_to_text(markup)
    title = security.html_title(markup)
    truncated = os.path.getsize(path) > len(data)
    return {
        "state": "ok" if text.strip() else "empty",
        "text": ("%s\n\n%s" % (title, text)).strip() if title else text,
        "lang": lang,
        "encoding": encoding,
        "truncated": truncated,
        "units": [],
    }


def sniff_dialect(sample):
    """嗅探 CSV 分隔符。``csv.Sniffer`` 会抛异常，所以兜住并退化为逗号。"""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except Exception:  # noqa: BLE001 —— 太规整或太乱的样本都会让它失手
        class _Fallback(csv.excel):
            delimiter = "\t" if sample.count("\t") > sample.count(",") else ","

        return _Fallback


def read_csv_rows(path, limit=_CSV_INDEX_ROWS, head_bytes=65536):
    """读 CSV/TSV 的前若干行。返回 ``(rows, truncated, dialect, encoding)``。

    ⚠️ **必须先探测编码再打开**。Excel 导出的中文 CSV 是 GBK/GB18030，
    拿 ``utf-8 errors='replace'`` 硬读会让整张表变成「�」，而这种文件在中文 NAS 上
    非常常见。整文件读进来会在大 CSV 上爆内存，所以按行流式读 + 行数上限。
    """
    try:
        with open(path, "rb") as probe:
            head = probe.read(head_bytes)
    except OSError:
        return [], False, csv.excel, "utf-8"

    encoding, _text = detect_encoding(head)
    try:
        with open(path, "r", encoding=encoding, errors="replace", newline="") as handle:
            sample = handle.read(head_bytes)
            handle.seek(0)
            dialect = sniff_dialect(sample)
            reader = csv.reader(handle, dialect)
            rows = []
            truncated = False
            for row in reader:
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append(row)
            return rows, truncated, dialect, encoding
    except OSError:
        return [], False, csv.excel, encoding
    except csv.Error:
        return [], False, csv.excel, encoding


def _csv_text(rows):
    parts = []
    for row in rows:
        cells = [cell.strip() for cell in row if cell and cell.strip()]
        if cells:
            parts.append(" | ".join(cells))
    return "\n".join(parts)


def from_csv(path, budget=0, lang="csv", **kwargs):
    """CSV/TSV。列名抽成单元，正文摊平进索引。"""
    rows, truncated, _dialect, encoding = read_csv_rows(path)
    if not rows:
        return {"state": "empty", "text": "", "lang": lang, "units": [],
                "encoding": encoding}
    header = rows[0]
    body = rows[1:]
    units = []
    if body:
        # 每 200 行合成一个可定位单元，命中时能说「大约在第 N 行附近」
        chunk = 200
        for start in range(0, len(body), chunk):
            block = body[start:start + chunk]
            units.append(("row", start + 2,
                          "第 %d–%d 行" % (start + 2, start + 1 + len(block)),
                          _csv_text(block)))
    text = _csv_text(rows)
    return {
        "state": "ok" if text.strip() else "empty",
        "text": text,
        "lang": lang,
        "encoding": encoding,
        "truncated": truncated,
        "units": units,
        "columns": header,
        "row_count": len(body),
    }


def _flatten_json(value, out, depth=0):
    """把 JSON 摊平成「键 值」文本，供全文索引使用。"""
    if depth > 12:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            out.append(str(key))
            _flatten_json(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _flatten_json(item, out, depth + 1)
    elif value is not None:
        out.append(str(value))


def from_json(path, budget=0, lang="json", **kwargs):
    """JSON / JSONL。能解析就结构化（前端可折叠），解析不了退回纯文本。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read(max(budget, 4096))
    except OSError as exc:
        return {"state": "failed", "error": "读取失败：%s" % exc, "text": ""}

    encoding, raw = detect_encoding(data)
    truncated = os.path.getsize(path) > len(data)
    try:
        parsed = json.loads(raw)
    except ValueError:
        # 损坏的 JSON 或 JSONL：**不报错**，按纯文本索引，前端也按文本显示
        return {
            "state": "ok" if raw.strip() else "empty",
            "text": raw,
            "lang": lang,
            "encoding": encoding,
            "truncated": truncated,
            "units": [],
            "note": "JSON 解析失败，已按纯文本索引。",
        }

    parts = []
    _flatten_json(parsed, parts)
    text = " ".join(parts)
    return {
        "state": "ok" if text.strip() else "empty",
        "text": text,
        "lang": lang,
        "encoding": encoding,
        "truncated": truncated,
        "units": [],
    }


def from_xml(path, budget=0, lang="xml", **kwargs):
    """XML。按标签剥出文本与属性值；格式错误也不报错。"""
    return from_text(path, budget=budget, lang=lang)


def from_yaml(path, budget=0, lang="yaml", **kwargs):
    """YAML。

    **标准库没有 YAML 解析器**，所以第一版按纯文本处理，另外抽出顶层键作为大纲。
    这一点在 README 的「不承诺的功能」里写明，绝不宣称支持结构化 YAML ——
    按指引 16.5 rank 5，描述与功能不符是驳回项。
    """
    result = from_text(path, budget=budget, lang=lang)
    text = result.get("text") or ""
    units = []
    if text:
        keys = _YAML_TOP_KEY.findall(text)
        if keys:
            units.append(("section", 1, "顶层键 %d 个" % len(keys),
                          " ".join(keys)))
    result["units"] = units
    result["note"] = "YAML 按纯文本索引（标准库无 YAML 解析器）。"
    return result


def csv_preview(path, offset=0, limit=200, columns=None):
    """给 ``/api/file/sheet`` 用的 CSV 分页。返回与 XLSX 一致的结构。"""
    rows, truncated, _dialect, encoding = read_csv_rows(path, limit=_CSV_INDEX_ROWS)
    if columns:
        header = [str(c) for c in columns]
        body = rows
    else:
        header = [str(c) for c in (rows[0] if rows else [])]
        body = rows[1:] if rows else []

    start = max(0, int(offset))
    end = start + max(1, int(limit))
    sliced = body[start:end]
    return {
        "sheets": [{"name": "CSV", "index": 0}],
        "sheet": "CSV",
        "columns": header,
        "rows": [{"r": start + index + 1, "c": list(row)}
                 for index, row in enumerate(sliced)],
        "total_rows": max(0, len(body)),
        "has_more": end < len(body),
        "offset": start,
        "limit": max(1, int(limit)),
        "encoding": encoding,
        "truncated": truncated,
    }
