"""内容字节服务：Range 支持与 MIME 判定。

**这是让浏览器内置媒体播放器与 PDF 阅读器能用的关键**：它们都会发
``Range: bytes=…`` 做分段加载与拖动进度，没有 206 就只能整文件下载。

两个必须遵守的实现约束：

1. **绝不能调用 ``srv.Response.file()``** —— 它会把整个文件读进内存
   （``server.py`` 里就是 ``fh.read()``）。一个 4 GiB 的视频足以把服务打死。
   这里一律 ``seek`` + 读一小块。
2. 框架的 ``_send`` 会无条件补上 ``Content-Length`` 与 ``Cache-Control: no-store``，
   我们**无法**设置缓存头。所以查看器性能靠 URL 上的 ``?v=<mtime_ns>`` 破缓存
   加上前端内存缓存，任何设计都不能依赖 HTTP 缓存。
"""

import mimetypes
import os
import re

from tnasapp.server import Response

from . import config, security

#: ``bytes=start-end`` / ``bytes=start-`` / ``bytes=-suffix``
RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)

_MIME_OVERRIDES = {
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".yaml": "application/x-yaml; charset=utf-8",
    ".yml": "application/x-yaml; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".docx": ("application/vnd.openxmlformats-officedocument"
              ".wordprocessingml.document"),
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": ("application/vnd.openxmlformats-officedocument"
              ".presentationml.presentation"),
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".flac": "audio/flac",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mdx": "text/markdown; charset=utf-8",
}


def guess_mime(path):
    ext = os.path.splitext(path or "")[1].lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed = mimetypes.guess_type(path or "")[0]
    if guessed:
        if guessed.startswith("text/") and "charset" not in guessed:
            return guessed + "; charset=utf-8"
        return guessed
    return "application/octet-stream"


def parse_range(header, size):
    """解析 Range 头。

    返回 ``None``（无 Range，给全量 200）、``"invalid"``（416）、或 ``(start, end)`` 闭区间。
    """
    if not header:
        return None
    match = RANGE_RE.match(header)
    if not match:
        return "invalid"
    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        return "invalid"
    try:
        if not raw_start:
            # bytes=-N —— 最后 N 字节
            length = int(raw_end)
            if length <= 0:
                return "invalid"
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(raw_start)
            end = int(raw_end) if raw_end else size - 1
            if end >= size:
                end = size - 1
    except (TypeError, ValueError):
        return "invalid"
    if start >= size or start > end or size == 0:
        return "invalid"
    return (start, end)


def serve_content(app, request, real_path, download_name=None):
    """把一个真实文件作为响应返回（支持 Range）。"""
    try:
        stat = os.stat(real_path)
    except OSError as exc:
        return Response.error("无法读取文件：%s" % exc, 404,
                              "文件可能已被移动或删除，可在知识库里重新扫描。")
    if os.path.isdir(real_path):
        return Response.error("这是一个目录，不是文件", 400, "请选择具体文件。")

    size = stat.st_size
    ctype = guess_mime(real_path)
    headers = {
        "Accept-Ranges": "bytes",
        # 内容安全：非 HTML 资源一律不允许被当作脚本执行
        "X-Content-Type-Options": "nosniff",
    }
    if download_name:
        quoted = security.safe_filename(download_name)
        from urllib.parse import quote

        headers["Content-Disposition"] = (
            "attachment; filename=\"%s\"; filename*=UTF-8''%s"
            % (quoted.encode("ascii", "replace").decode("ascii"), quote(quoted)))

    # 文本类不设 nosniff 会让浏览器直接内联渲染，OK；但 HTML 要交给沙箱 iframe，
    # 所以这里额外给 CSP（见 html_view）
    parsed = parse_range(request.headers.get("Range"), size)
    if parsed == "invalid":
        headers["Content-Range"] = "bytes */%d" % size
        return Response(416, b"", ctype, headers)

    if parsed is None:
        # 无 Range：小文件直读；大文件主动分块返回 206，避免一次把大文件拖进内存
        if size <= config.RANGE_CHUNK:
            start, end = 0, max(0, size - 1)
            with open(real_path, "rb") as handle:
                body = handle.read() if size else b""
            return Response(200, body, ctype, headers)
        start, end = 0, config.RANGE_CHUNK - 1
        headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        with open(real_path, "rb") as handle:
            handle.seek(start)
            return Response(206, handle.read(end - start + 1), ctype, headers)

    start, end = parsed
    # 单次响应封顶，防止客户端要「整个大文件」时把它读进内存
    if end - start + 1 > config.RANGE_CHUNK:
        end = start + config.RANGE_CHUNK - 1
    headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    with open(real_path, "rb") as handle:
        handle.seek(start)
        body = handle.read(end - start + 1)
    return Response(206, body, ctype, headers)


def serve_html_view(app, real_path, download_name=None):
    """HTML 查看器专用：附加严格 CSP，配合前端 ``sandbox`` iframe 双重隔离。

    指引与设计文档 §14 要求 HTML 必须沙箱化：禁脚本、禁父页面访问、
    禁任意本地文件与外部网络请求。CSP 是这一层；``sandbox`` 属性是另一层。
    """
    response = serve_content(app, _FakeRequest(), real_path, download_name)
    response.headers["Content-Security-Policy"] = security.HTML_VIEW_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


class _FakeRequest:
    """``serve_html_view`` 不需要 Range —— 给它一个空头即可。"""

    headers = {}


def read_text_window(path, offset=0, limit=config.MAX_TEXT_PAGE, encoding=None):
    """给 ``/api/file/text`` 用的分页读取。

    按**字节**偏移读取而不是先整体解码：一个 500 MB 的日志不该为了看第一屏而全读。
    代价是偏移可能落在多字节字符中间，所以解码时用 ``errors='replace'`` 兜住。
    """
    from .extract import detect_encoding

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return {"ok": False, "error": "无法读取文件：%s" % exc}

    start = max(0, int(offset))
    want = max(1024, min(int(limit), config.MAX_TEXT_PAGE))
    with open(path, "rb") as handle:
        handle.seek(start)
        raw = handle.read(want)
        if encoding is None:
            head = b""
            handle.seek(0)
            head = handle.read(65536)
            encoding, _ = detect_encoding(head or raw)

    try:
        text = raw.decode(encoding or "utf-8", "replace")
    except LookupError:
        encoding, text = "utf-8", raw.decode("utf-8", "replace")

    end = start + len(raw)
    return {
        "ok": True,
        "text": text,
        "encoding": encoding,
        "offset": start,
        "next_offset": end,
        "size": size,
        "has_more": end < size,
        "truncated": end < size,
    }
