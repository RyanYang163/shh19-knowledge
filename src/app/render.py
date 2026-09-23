"""服务端渲染：Markdown / DOCX → **已转义**的 HTML。

放在服务端做而不是前端，理由是**转义逻辑可以用 Python 单测**。
在浏览器里拼 HTML 时转义顺序一旦写反，就是一个注入口；而在这里，
顺序是硬性写死的两步：

1. **先把整段文本整体转义**（``html.escape``）；
2. **再插入我们自己生成的标签**。

只要遵守这两步，源文件里写什么 ``<script>`` / ``<img onerror>``
都只会变成可见的文字。``tests/test_render.py`` 用例覆盖了这条。

设计文档 §15 提到用 Mammoth.js 转 DOCX，并特别提醒 Mammoth **不做**
sanitization、输出不能直接插入页面。本应用改成服务端 ``zipfile`` + ``ElementTree``
自己解析，转义从一开始就到位 —— 那条提醒因此被结构性满足，而不是靠记得处理。
"""

import html
import re

#: 内联代码
_CODE_SPAN = re.compile(r"`([^`]+)`")
#: 粗体 / 斜体（** 优先于 *）
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)|(?<!_)_([^_\n]+?)_(?!_)")
#: 删除线
_STRIKE = re.compile(r"~~(.+?)~~")
#: 链接与图片
_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
#: 行首标题
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
#: 无序 / 有序列表
_ULIST = re.compile(r"^\s*[-*+]\s+(.*)$")
_OLIST = re.compile(r"^\s*\d+[.)]\s+(.*)$")
#: 分隔线
_RULE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")
#: 表格分隔行（|---|:--:|）
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _safe_url(url):
    """过滤危险协议。``javascript:`` / ``data:`` / ``file:`` 一律不生成链接。"""
    from .security import is_dangerous_uri

    raw = (url or "").strip()
    if not raw or is_dangerous_uri(raw):
        return None
    if raw.startswith("//"):
        return None
    if re.match(r"(?i)^(https?:|mailto:|tel:|#|\./|\.\./|/)", raw):
        return raw
    # 相对路径一律加上前缀由前端处理；这里只放行明确的相对形式
    if "://" in raw:
        return None
    return raw


def _inline(text):
    """处理行内标记。**调用方必须已转义过 text。**"""
    out = _CODE_SPAN.sub(lambda m: "<code>%s</code>" % m.group(1), text)
    out = _BOLD.sub(lambda m: "<strong>%s</strong>" % (m.group(1) or m.group(2)), out)
    out = _ITALIC.sub(lambda m: "<em>%s</em>" % (m.group(1) or m.group(2)), out)
    out = _STRIKE.sub(lambda m: "<del>%s</del>" % m.group(1), out)

    def _link(match):
        label, url = match.group(1), match.group(2)
        safe = _safe_url(html.unescape(url))
        if not safe:
            return label        # 危险链接：只留可见文字，不生成 <a>
        is_image = match.group(0).startswith("!")
        target = html.escape(safe, quote=True)
        if is_image and safe.lower().split("?")[0].endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")):
            return ('<a class="md-image" href="%s" target="_blank" rel="noopener">'
                    "[图片] %s</a>" % (target, label or target))
        return ('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
                % (target, label or target))

    return _LINK.sub(_link, out)


def markdown_to_html(text, max_blocks=20000):
    """Markdown → 安全 HTML。

    支持：标题、段落、粗体/斜体/删除线、行内代码、代码块、无序/有序列表、
    引用、分隔线、链接、表格。**不支持** Mermaid 与数学公式（README 里声明）。
    """
    if not text:
        return ""

    lines = str(text).split("\n")
    blocks = []
    index = 0
    paragraph = []
    list_kind = None
    list_items = []

    def flush_paragraph():
        if paragraph:
            joined = " ".join(part.strip() for part in paragraph if part.strip())
            if joined:
                blocks.append("<p>%s</p>" % _inline(html.escape(joined, quote=False)))
            del paragraph[:]

    def flush_list():
        nonlocal list_kind
        if list_items:
            tag = "ol" if list_kind == "ol" else "ul"
            items = "".join("<li>%s</li>" % item for item in list_items)
            blocks.append("<%s>%s</%s>" % (tag, items, tag))
            del list_items[:]
        list_kind = None

    while index < len(lines) and len(blocks) < max_blocks:
        line = lines[index]

        # 围栏代码块
        fence = re.match(r"^\s*(```+|~~~+)\s*([\w+-]*)\s*$", line)
        if fence:
            flush_paragraph()
            flush_list()
            marker, language = fence.group(1), fence.group(2)
            index += 1
            body = []
            while index < len(lines) and not lines[index].strip().startswith(marker[:3]):
                body.append(lines[index])
                index += 1
            index += 1
            escaped = html.escape("\n".join(body), quote=False)
            cls = ' class="lang-%s"' % html.escape(language, quote=True) if language else ""
            blocks.append("<pre%s><code>%s</code></pre>" % (cls, escaped))
            continue

        if not line.strip():
            flush_paragraph()
            flush_list()
            index += 1
            continue

        if _RULE.match(line):
            flush_paragraph()
            flush_list()
            blocks.append("<hr/>")
            index += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            level = min(6, len(heading.group(1)))
            blocks.append("<h%d>%s</h%d>"
                          % (level, _inline(html.escape(heading.group(2), quote=False)),
                             level))
            index += 1
            continue

        if line.lstrip().startswith(">"):
            flush_paragraph()
            flush_list()
            quote = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote.append(lines[index].lstrip()[1:].strip())
                index += 1
            blocks.append("<blockquote>%s</blockquote>"
                          % _inline(html.escape(" ".join(quote), quote=False)))
            continue

        # 表格：当前行是 | 开头，且下一行是分隔行
        if "|" in line and index + 1 < len(lines) and _TABLE_SEP.match(lines[index + 1]):
            flush_paragraph()
            flush_list()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            index += 2
            body_rows = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                body_rows.append([c.strip() for c in
                                  lines[index].strip().strip("|").split("|")])
                index += 1
            head_html = "".join("<th>%s</th>"
                                % _inline(html.escape(c, quote=False)) for c in header)
            rows_html = "".join(
                "<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(html.escape(c, quote=False))
                                        for c in row)
                for row in body_rows)
            blocks.append("<table class=\"md-table\"><thead><tr>%s</tr></thead>"
                          "<tbody>%s</tbody></table>" % (head_html, rows_html))
            continue

        ordered = _OLIST.match(line)
        unordered = _ULIST.match(line)
        if ordered or unordered:
            flush_paragraph()
            kind = "ol" if ordered else "ul"
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            item = (ordered or unordered).group(1)
            list_items.append(_inline(html.escape(item, quote=False)))
            index += 1
            continue

        paragraph.append(line)
        index += 1

    flush_paragraph()
    flush_list()
    return "\n".join(blocks)


# ---------------------------------------------------------------- DOCX → HTML

def docx_to_html(path, max_blocks=20000):
    """DOCX → 安全 HTML。复用 ``extract_office`` 的解析，但输出结构而不是纯文本。

    标题用 ``<h1>``–``<h6>``，表格用 ``<table class="doc-table">``，
    正文段落用 ``<p>``。所有文本都在插入前转义。
    """
    from . import extract_office as office

    try:
        with office._open(path) as archive:
            if "word/document.xml" not in archive.namelist():
                return None, "文档缺少 word/document.xml，不是有效的 DOCX。"
            style_names = office._docx_style_names(archive)
            root = office._parse(archive, "word/document.xml", limit=16 << 20)
            if root is None:
                return None, "无法读取文档正文。"
            body = root.find(office.W + "body")
            if body is None:
                return None, "文档正文为空。"
            blocks = []
            for node in list(body):
                if len(blocks) >= max_blocks:
                    blocks.append("<p class=\"doc-truncated\">（文档过长，预览已截断；"
                                  "完整内容可用全文检索查看）</p>")
                    break
                if node.tag == office.W + "p":
                    text = office._docx_paragraph_text(node)
                    if not text:
                        continue
                    level = office._docx_heading_level(node, style_names)
                    escaped = _inline(html.escape(text, quote=False))
                    if level:
                        level = min(6, level)
                        blocks.append("<h%d>%s</h%d>" % (level, escaped, level))
                    else:
                        blocks.append("<p>%s</p>" % escaped)
                elif node.tag == office.W + "tbl":
                    rows = []
                    for tr in node.findall(office.W + "tr"):
                        cells = [office._all_text(tc) for tc in tr.findall(office.W + "tc")]
                        if any(cells):
                            rows.append("<tr>%s</tr>" % "".join(
                                "<td>%s</td>" % html.escape(cell, quote=False)
                                for cell in cells))
                    if rows:
                        blocks.append('<table class="doc-table"><tbody>%s</tbody></table>'
                                      % "".join(rows))
            return "\n".join(blocks), None
    except office._OfficeError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, "解析文档时出错：%s" % exc


def plain_to_html(text, max_chars=200_000):
    """纯文本 → 保留换行的 ``<pre>``（用于没有专门渲染器的格式）。"""
    if not text:
        return ""
    return "<pre class=\"plain\">%s</pre>" % html.escape(text[:max_chars], quote=False)
