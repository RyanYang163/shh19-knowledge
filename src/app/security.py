"""清洗与限额：把「不可信输入」收口到一处。

本应用有三类输入是不可信的，都必须在这里处理，**不允许在业务代码里各写一套**：

1. **文件路径** —— 来自 API 的 ``?path=``。统一走 ``kbs.resolve_in_kb()``，
   它内部用框架的 ``AllowedRoots.check()``（realpath → 白名单前缀比对）再做 KB 根校验。
2. **文件内容渲染出的 HTML** —— Markdown / DOCX / HTML 查看器。原则是
   **先整体转义，再插入我们自己生成的标签**；顺序反了就等于开了注入口。
3. **用户上传的图标** —— 按**内容嗅探**判类型（不信 ``declared_type``），
   SVG 逐标签白名单清洗，并用 CSP 二次兜底。
"""

import html
import html.parser
import re

# ---------------------------------------------------------------- 数值

def clamp_int(value, low, high, default):
    """把任意输入钳到 ``[low, high]``。分页参数全部走这里，避免负 offset 之类的怪值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def clamp_float(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


# ---------------------------------------------------------------- HTML 文本提取

class _TextExtractor(html.parser.HTMLParser):
    """把 HTML 变成纯文本，用于建立索引（**不用于渲染**）。

    跳过 ``script`` / ``style`` —— 它们的正文是代码不是内容，
    进了索引只会让搜索命中一堆噪声。
    """

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "header", "footer", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK and self._parts:
            self._parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self._BLOCK and self._parts:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK and self._parts:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()
        if self._skip_depth:
            return
        if data.strip():
            self._parts.append(data)

    def text(self):
        joined = "".join(self._parts)
        # 折叠多余空行，但保留段落分隔
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return joined.strip()


def strip_html_to_text(markup):
    """HTML → 纯文本。解析器出任何岔子都退化为「粗暴去标签」，绝不抛异常。"""
    parser = _TextExtractor()
    try:
        parser.feed(str(markup or ""))
        parser.close()
        return parser.text()
    except Exception:  # noqa: BLE001 —— 坏 HTML 不该让索引任务失败
        rough = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(markup or ""))
        rough = re.sub(r"(?s)<[^>]+>", " ", rough)
        return re.sub(r"\s+", " ", html.unescape(rough)).strip()


def html_title(markup):
    parser = _TextExtractor()
    try:
        parser.feed(str(markup or "")[:200_000])
        return parser.title
    except Exception:  # noqa: BLE001
        match = re.search(r"(?is)<title[^>]*>(.*?)</title>", str(markup or ""))
        return html.unescape(match.group(1)).strip() if match else ""


def escape(text):
    """统一的转义入口。任何把文件内容放进 HTML 的地方都必须经过它。"""
    return html.escape(str(text if text is not None else ""), quote=True)


# ---------------------------------------------------------------- URI

_DANGEROUS_URI = re.compile(
    r"(?i)^\s*(javascript|vbscript|data|file|blob|filesystem)\s*:"
)


def is_dangerous_uri(value):
    """判断 URL 是否属于「不应点击」的协议。

    ``data:`` 一律拦掉：Markdown / HTML 里嵌 ``data:text/html;base64,…`` 是常见的
    绕过沙箱手法，而正常的文档不需要在链接里用 data URI。
    """
    if not value:
        return False
    # 去掉控制字符与空白后的形式再判，防止 ``java\nscript:`` 这类绕过
    cleaned = re.sub(r"[\s\x00-\x1f\x7f]+", "", str(value))
    return bool(_DANGEROUS_URI.match(cleaned))


# ---------------------------------------------------------------- 图片嗅探

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def sniff_image(data):
    """按魔数判图片类型；认不出返回 None。**不信任任何声明类型。**"""
    if not data:
        return None
    for magic, name in _MAGIC:
        if data.startswith(magic):
            return name
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def sniff_svg(data):
    """判断是否是 SVG（按内容，不看扩展名）。"""
    head = (data or b"")[:4096].lstrip()
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:].lstrip()
    lowered = head.lower()
    if lowered.startswith(b"<?xml"):
        lowered = lowered[lowered.find(b"<svg"):] if b"<svg" in lowered else b""
    return lowered.startswith(b"<svg")


# ---------------------------------------------------------------- SVG 清洗

#: 允许保留的标签。**白名单**，其余一律丢弃（含 script / foreignObject /
#: animate / set / iframe / object / embed / style），这样 XSS、外部引用、
#: XXE 与「动画劫持」都没有落脚点。
_SVG_ALLOWED_TAGS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
    "polygon", "text", "tspan", "defs", "use", "title", "desc",
    "lineargradient", "radialgradient", "stop", "clippath", "mask",
}

#: 允许保留的属性（去掉前缀后比较）。事件处理器 ``on*`` 与 ``href`` 另判。
_SVG_ALLOWED_ATTRS = {
    "viewbox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy",
    "r", "rx", "ry", "d", "points", "fill", "fill-opacity", "fill-rule",
    "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-dasharray", "opacity", "transform", "font-size", "font-family",
    "font-weight", "text-anchor", "dominant-baseline", "letter-spacing",
    "offset", "stop-color", "stop-opacity", "gradientunits", "id", "class",
    "clip-path", "mask", "preserveaspectratio", "xmlns",
}

_VOID_IN_SVG = {"path", "rect", "circle", "ellipse", "line", "polyline",
                "polygon", "stop", "use"}


class _SvgSanitizer(html.parser.HTMLParser):
    """按白名单重写 SVG。输出的是**我们重新生成的标签**，不是原文回显。"""

    def __init__(self, max_nodes=4096):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._open = []
        self._max_nodes = max_nodes
        self._nodes = 0
        self._dropped = 0

    # -- 内部工具 --

    def _clean_attrs(self, attrs):
        keep = []
        for raw_name, value in attrs:
            name = (raw_name or "").strip().lower()
            if not name:
                continue
            if name.startswith("on"):
                # 事件处理器一律丢弃（即使白名单里没有，也显式拦一道）
                self._dropped += 1
                continue
            if name in ("href", "xlink:href"):
                if is_dangerous_uri(value):
                    self._dropped += 1
                    continue
                keep.append((name, value or ""))
                continue
            if name.startswith("xlink:"):
                name = name[6:]
            if name in _SVG_ALLOWED_ATTRS:
                keep.append((name, value if value is not None else ""))
            else:
                self._dropped += 1
        return keep

    def handle_decl(self, decl):
        # <!DOCTYPE …>：直接丢，防 XXE 与实体膨胀
        self._dropped += 1

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in _SVG_ALLOWED_TAGS or self._nodes >= self._max_nodes:
            if tag not in _SVG_ALLOWED_TAGS:
                self._dropped += 1
            return
        self._nodes += 1
        rendered = "".join(
            ' %s="%s"' % (name, html.escape(value, quote=True))
            for name, value in self._clean_attrs(attrs)
        )
        self.out.append("<%s%s>" % (tag, rendered))
        if tag not in _VOID_IN_SVG:
            self._open.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag not in _SVG_ALLOWED_TAGS or self._nodes >= self._max_nodes:
            return
        self._nodes += 1
        rendered = "".join(
            ' %s="%s"' % (name, html.escape(value, quote=True))
            for name, value in self._clean_attrs(attrs)
        )
        self.out.append("<%s%s/>" % (tag, rendered))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID_IN_SVG:
            return
        # 只闭合真正开着的标签，避免 </g> 把结构搞乱
        if tag in self._open:
            while self._open:
                opened = self._open.pop()
                self.out.append("</%s>" % opened)
                if opened == tag:
                    break

    def handle_data(self, data):
        # SVG 里的文本节点原样保留（已经过 escape 的逆：这里重新转义一次）
        if self._open and data.strip():
            self.out.append(html.escape(data, quote=False))

    def handle_entityref(self, name):
        if self._open:
            self.out.append(html.escape("&%s;" % name, quote=False))

    def handle_charref(self, name):
        if self._open:
            self.out.append(html.escape("&#%s;" % name, quote=False))

    def result(self):
        while self._open:
            self.out.append("</%s>" % self._open.pop())
        return "".join(self.out)


def sanitize_svg(data, max_bytes=256 * 1024):
    """清洗用户上传的 SVG，返回可安全内联的 SVG 文本。

    认不出是 SVG、或清洗后没有 ``<svg>`` 根节点都返回 ``None``（调用方拒绝该上传）。
    """
    if not data:
        return None
    raw = data if isinstance(data, bytes) else str(data).encode("utf-8", "ignore")
    if len(raw) > max_bytes:
        return None
    if not sniff_svg(raw):
        return None

    text = raw.decode("utf-8", "replace")
    # 先掐掉 DOCTYPE（含 ENTITY 声明），HTMLParser 的 handle_decl 也会丢，双保险
    text = re.sub(r"(?is)<!DOCTYPE.*?>", "", text)
    text = re.sub(r"(?is)<!ENTITY.*?>", "", text)

    sanitizer = _SvgSanitizer()
    try:
        sanitizer.feed(text)
        sanitizer.close()
    except Exception:  # noqa: BLE001 —— 坏 SVG 直接判为不可用
        return None

    cleaned = sanitizer.result().strip()
    if "<svg" not in cleaned.lower():
        return None
    if len(cleaned.encode("utf-8")) > max_bytes:
        return None
    return cleaned


#: 上传图标一律用这个 CSP 回给浏览器：即便清洗漏了什么，也执行不了。
ICON_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"

#: HTML 查看器 iframe 的 CSP：禁脚本、禁外链、禁表单、禁父页面访问
HTML_VIEW_CSP = (
    "default-src 'none'; img-src data: blob: 'self'; media-src 'self'; "
    "style-src 'unsafe-inline'; font-src data:; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'self'"
)


def safe_filename(name, fallback="download"):
    """把文件名收敛成安全的下载名（去掉路径分隔符与控制字符）。"""
    text = str(name or "").strip() or fallback
    text = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", text)
    text = text.replace('"', "'").strip(". ")
    return text[:180] or fallback
