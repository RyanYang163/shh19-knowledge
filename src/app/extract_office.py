"""Office Open XML 抽取器：DOCX / XLSX / PPTX。

**不用 Mammoth.js、不用 SheetJS** —— 它们是第三方 JS，会引入 Apache/BSD 许可与
「与描述不符」的风险；而 OOXML 本身就是 zip + XML，标准库完全够用。

两个必须做的防御：

1. **zip 炸弹**：``zipfile.read()`` 会把整个成员解压进内存。读之前必须先查
   ``getinfo(name).file_size`` 与上限比对（``config.MAX_PART_BYTES``），
   否则一个声明 600 GB 的 docx 能直接把服务打死。**只有 ``file_size`` 也来自
   压缩包头部、不可全信**，所以读取时还用 ``.read(limit)`` 二次封顶。
2. **XML 实体膨胀**：``ElementTree`` 不解析外部实体（无 XXE），但仍会用内存展开
   内部实体，所以同样靠体积上限兜住。

文档顺序很重要：DOCX 的段落与表格必须**按 document.xml 里的先后**输出，
PPTX 的幻灯片顺序**只能**由 ``p:sldIdLst`` 决定（文件名 ``slide1.xml``/``slide10.xml``
的字典序是错的，实测过的坑）。
"""

import os
import re
import zipfile

from xml.etree import ElementTree as ET

from . import config

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_HEADING_RE = re.compile(r"(?i)^\s*(heading|标题|標題|見出し|제목)\s*([1-9])\s*$")

#: Excel 内置的日期/时间数字格式 id（ECMA-376 第 18.8.30 节）
_BUILTIN_DATE_IDS = set(range(14, 23)) | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
                                         45, 46, 47, 50, 51, 52, 53, 54, 55, 56, 57, 58}
_DATE_CODE_RE = re.compile(r"(?i)(yy|mm?|dd?|hh?|ss?|am/pm)")


# ---------------------------------------------------------------- 工具

class _OfficeError(Exception):
    """格式层面的问题（不是坏文件，而是我们明知读不了）。"""

    def __init__(self, message, unsupported=False):
        super().__init__(message)
        self.unsupported = unsupported


def _read_part(archive, name, limit=None):
    """安全读一个 zip 成员：先按声明体积判上限，再用 ``read(limit)`` 二次封顶。"""
    cap = int(limit or config.MAX_PART_BYTES)
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > cap:
        raise _OfficeError(
            "文档内部部件 %s 解压后达 %d 字节，超过 %d 上限，已跳过。"
            % (name, info.file_size, cap))
    with archive.open(name) as handle:
        return handle.read(cap + 1)


def _parse(archive, name, limit=None):
    data = _read_part(archive, name, limit)
    if data is None:
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise _OfficeError("文档内部部件 %s 的 XML 无法解析：%s" % (name, exc))


def _rels(archive, part):
    """读某个部件的关系表，返回 ``{rId: Target}``。"""
    directory, _, base = part.rpartition("/")
    rels_name = "%s/_rels/%s.rels" % (directory, base) if directory else "_rels/%s.rels" % base
    root = _parse(archive, rels_name, limit=1 << 20)
    if root is None:
        return {}
    out = {}
    for rel in root.findall(PKG_REL + "Relationship"):
        target = rel.get("Target") or ""
        if rel.get("TargetMode") == "External":
            continue
        # Target 是相对部件所在目录的路径，归一到包内绝对路径
        # （例如 workbook.xml 的 rId1 → worksheets/sheet1.xml）
        joined = os.path.normpath(os.path.join(directory, target)).replace("\\", "/")
        out[rel.get("Id")] = joined
    return out


def _all_text(element):
    """取一个节点下所有 ``a:t`` / ``w:t`` 文本（用于表格单元格等简单场景）。"""
    parts = []
    for node in element.iter():
        if node.tag in (A + "t", W + "t"):
            if node.text:
                parts.append(node.text)
    return "".join(parts).strip()


def _open(path):
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise _OfficeError("文件不是有效的 OOXML 文档（zip 结构损坏）：%s" % exc,
                           unsupported=True)
    except OSError as exc:
        raise _OfficeError("无法打开文件：%s" % exc)


# ---------------------------------------------------------------- DOCX

def _docx_heading_level(para, style_names):
    """判一个段落是不是标题，是则返回层级（1–9）。"""
    props = para.find(W + "pPr")
    if props is None:
        return 0
    outline = props.find(W + "outlineLvl")
    if outline is not None:
        try:
            return max(1, min(9, int(outline.get(W + "val", "0")) + 1))
        except (TypeError, ValueError):
            pass
    style = props.find(W + "pStyle")
    if style is not None:
        style_id = style.get(W + "val") or ""
        # 先用 styles.xml 把 styleId 换成样式**名**——中文/日文 Word 里的样式名
        # 是「标题 1」而不是「Heading 1」，只按 id 匹配会漏掉整篇文档的标题
        name = style_names.get(style_id, style_id)
        match = _HEADING_RE.match(name)
        if match:
            return int(match.group(2))
        if re.match(r"(?i)^heading\s*[1-9]$", style_id):
            return int(style_id[-1])
    return 0


def _docx_style_names(archive):
    """读 styles.xml，返回 ``{styleId: 样式名}``。"""
    root = _parse(archive, "word/styles.xml", limit=2 << 20)
    if root is None:
        return {}
    out = {}
    for style in root.findall(W + "style"):
        style_id = style.get(W + "styleId")
        name_node = style.find(W + "name")
        if style_id and name_node is not None:
            out[style_id] = name_node.get(W + "val") or ""
    return out


def _docx_paragraph_text(para):
    """按字符在段落里的**先后顺序**取文本，正确映射制表符与换行。"""
    parts = []
    for node in para.iter():
        tag = node.tag
        if tag == W + "t":
            if node.text:
                parts.append(node.text)
        elif tag == W + "tab":
            parts.append("\t")
        elif tag in (W + "br", W + "cr"):
            parts.append("\n")
        elif tag == W + "noBreakHyphen":
            parts.append("-")
    return "".join(parts).strip()


def from_docx(path, budget=0, lang="", **kwargs):
    """DOCX → 文本 + 以标题分节的可定位单元。"""
    try:
        with _open(path) as archive:
            if "word/document.xml" not in archive.namelist():
                raise _OfficeError("文档缺少 word/document.xml，不是有效的 DOCX。",
                                   unsupported=True)
            style_names = _docx_style_names(archive)
            root = _parse(archive, "word/document.xml", limit=min(budget, 16 << 20)
                          if budget else None)
            if root is None:
                raise _OfficeError("无法读取文档正文。", unsupported=True)

            body = root.find(W + "body")
            if body is None:
                return {"state": "empty", "text": "", "lang": lang, "units": []}

            chunks = []          # 顺序敏感的正文
            headings = []        # (level, label, position_in_chunks)
            tables = 0
            for node in list(body):
                if node.tag == W + "p":
                    text = _docx_paragraph_text(node)
                    level = _docx_heading_level(node, style_names)
                    if level and text:
                        headings.append((level, text, len(chunks)))
                        chunks.append(("#" * level) + " " + text)
                    elif text:
                        chunks.append(text)
                elif node.tag == W + "tbl":
                    tables += 1
                    rows = []
                    for tr in node.findall(W + "tr"):
                        cells = [_all_text(tc) for tc in tr.findall(W + "tc")]
                        if any(cells):
                            rows.append(" | ".join(cells))
                    if rows:
                        chunks.append("[表格 %d]\n%s" % (tables, "\n".join(rows)))
    except _OfficeError as exc:
        return {"state": "unsupported" if exc.unsupported else "failed",
                "text": "", "error": str(exc), "lang": lang, "units": []}

    text = "\n\n".join(chunks).strip()
    units = []
    for index, (level, label, position) in enumerate(headings):
        end = (headings[index + 1][2] if index + 1 < len(headings) else len(chunks))
        section = "\n".join(chunks[position:end])
        units.append(("section", index + 1, "%s %s" % ("#" * level, label), section))

    return {
        "state": "ok" if text else "empty",
        "text": text,
        "lang": lang or "word",
        "units": units,
        "tables": tables,
        "headings": len(headings),
    }


# ---------------------------------------------------------------- XLSX

def _xlsx_shared_strings(archive):
    root = _parse(archive, "xl/sharedStrings.xml", limit=16 << 20)
    if root is None:
        return []
    out = []
    for si in root.findall(S + "si"):
        # 富文本会拆成多个 <r><t>，必须合并而不是只取第一个 <t>
        out.append("".join(node.text or "" for node in si.iter(S + "t")))
    return out


def _xlsx_sheets(archive):
    """返回 ``[(表名, 部件路径)]``，**顺序与工作簿里一致**（不是文件名顺序）。"""
    root = _parse(archive, "xl/workbook.xml", limit=4 << 20)
    if root is None:
        return []
    rels = _rels(archive, "xl/workbook.xml")
    out = []
    container = root.find(S + "sheets")
    if container is None:
        return []
    for sheet in container.findall(S + "sheet"):
        name = sheet.get("name") or "Sheet"
        rid = sheet.get(R + "id")
        target = rels.get(rid)
        if target:
            out.append((name, target))
    return out


def _xlsx_date_formats(archive):
    """返回 ``{样式序号: 是否为日期}``。日期要按 1900 历还原，不能把序列号当数字显示。"""
    root = _parse(archive, "xl/styles.xml", limit=4 << 20)
    if root is None:
        return {}
    custom = {}
    for node in root.iter(S + "numFmt"):
        try:
            custom[int(node.get("numFmtId"))] = node.get("formatCode") or ""
        except (TypeError, ValueError):
            continue
    out = {}
    xfs = root.find(S + "cellXfs")
    if xfs is None:
        return out
    for index, xf in enumerate(xfs.findall(S + "xf")):
        try:
            fmt_id = int(xf.get("numFmtId") or 0)
        except (TypeError, ValueError):
            fmt_id = 0
        if fmt_id in _BUILTIN_DATE_IDS:
            out[index] = True
        elif fmt_id in custom:
            out[index] = bool(_DATE_CODE_RE.search(custom[fmt_id]))
    return out


def _serial_to_date(value):
    """Excel 序列号 → ``YYYY-MM-DD`` / ``HH:MM:SS``。

    Excel 沿用 Lotus 1-2-3 的 bug，认为 1900 年是闰年（1900-02-29 存在），
    所以 60 之前的序列号与之后差一天。这里用 1899-12-30 作起点来抵消。
    """
    import datetime

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number < 0 or number > 2958465:      # 超出 9999-12-31
        return str(value)
    days = int(number)
    fraction = number - days
    base = datetime.date(1899, 12, 30)
    if days == 60:
        return "1900-02-29"                  # 那个并不存在的日子，原样示人更诚实
    if days < 60:
        days += 1
    try:
        result = base + datetime.timedelta(days=days)
    except OverflowError:
        return str(value)
    if fraction <= 0:
        return result.isoformat()
    seconds = int(round(fraction * 86400))
    if seconds >= 86400:                     # 浮点舍入可能撞到次日
        result += datetime.timedelta(days=1)
        seconds = 0
    return "%s %02d:%02d:%02d" % (result.isoformat(), seconds // 3600,
                                  (seconds % 3600) // 60, seconds % 60)


def _column_index(ref):
    """``"AB12"`` → 27（0 基列号）。"""
    letters = "".join(ch for ch in str(ref or "") if ch.isalpha())
    index = 0
    for ch in letters.upper():
        index = index * 26 + (ord(ch) - 64)
    return max(0, index - 1)


def _xlsx_cell_value(cell, shared, date_styles):
    """解析一个 ``<c>`` 单元格的值。t 属性决定取值方式。"""
    kind = cell.get("t") or "n"
    style = cell.get("s")
    if kind == "inlineStr":
        node = cell.find(S + "is")
        return _all_text(node) if node is not None else ""
    value_node = cell.find(S + "v")
    raw = value_node.text if value_node is not None and value_node.text else ""
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if raw not in ("", "0") else "FALSE"
    if kind == "e":
        return raw or "#ERR"
    if kind == "str":
        return raw
    if raw == "":
        return ""
    if style is not None:
        try:
            if date_styles.get(int(style)):
                return _serial_to_date(raw)
        except (TypeError, ValueError):
            pass
    return raw


#: 工作表部件超过这个体积就不做「数总行数」的整趟扫描（避免分页请求变慢）
_COUNT_ROWS_MAX_PART = 32 << 20


def _sheet_dimension(archive, part):
    """读工作表自己声明的 ``<dimension ref="A1:Z1234"/>``，返回末行号。

    这是**唯一免费**拿到精确总行数的途径；OOXML 要求它出现在 ``<sheetData>`` 之前。
    拿不到就返回 None（下游退化为 has_more 语义，而不是编一个数字出来）。
    """
    try:
        if archive.getinfo(part).file_size > _COUNT_ROWS_MAX_PART:
            return None
        with archive.open(part) as handle:
            head = handle.read(8192)
    except (KeyError, OSError):
        return None
    match = re.search(rb"<dimension[^>]*ref=\"[A-Z]+(\d+)(?::[A-Z]+(\d+))?\"", head)
    if not match:
        return None
    try:
        return int(match.group(2) or match.group(1))
    except (TypeError, ValueError):
        return None


def _count_sheet_rows(archive, part, cap=2_000_000):
    """整趟流式数行数（只在 ``<dimension>`` 缺失且部件不大时才做）。"""
    total = 0
    try:
        with archive.open(part) as handle:
            for _event, element in ET.iterparse(handle, events=("end",)):
                if element.tag == S + "row":
                    total += 1
                    element.clear()
                    if total >= cap:
                        return None
    except (ET.ParseError, OSError, KeyError):
        return None
    return total


def read_sheet(path, sheet_index=0, offset=0, limit=200, columns=None):
    """给 ``/api/file/sheet`` 用的表格分页读取。

    用 ``iterparse`` 逐行解析并 ``clear()``：一张 50 万行的表不会把内存吃满，
    且只解析到窗口结束（多读一行用于判断 ``has_more``），不为翻一页去读整张表。

    ``total_rows`` **可能为 None** —— 表示「本机无法在不整趟扫描的前提下得知总行数」。
    这时前端显示「还有更多」而不是编一个数字；用户翻到底时 ``has_more`` 会变 false。
    """
    try:
        with _open(path) as archive:
            sheets = _xlsx_sheets(archive)
            if not sheets:
                raise _OfficeError("工作簿里没有工作表。", unsupported=True)
            index = max(0, min(int(sheet_index), len(sheets) - 1))
            name, part = sheets[index]
            shared = _xlsx_shared_strings(archive)
            date_styles = _xlsx_date_formats(archive)
            header, rows, has_more = _iter_sheet_rows(
                archive, part, shared, date_styles, offset, limit)
            if has_more:
                total_rows = _sheet_dimension(archive, part)
                if total_rows is None:
                    total_rows = _count_sheet_rows(archive, part)
                if total_rows is not None:
                    # dimension 声明的是「最大行号」，可能与实际行数不等（中间有空行）
                    total_rows = max(total_rows - 1, 0)
            else:
                total_rows = max(0, int(offset)) + len(rows)
    except _OfficeError as exc:
        return {"error": str(exc), "unsupported": exc.unsupported}

    if columns:
        header = list(columns)
    return {
        "sheets": [{"name": n, "index": i} for i, (n, _p) in enumerate(sheets)],
        "sheet": name,
        "columns": header,
        "rows": rows,
        "total_rows": total_rows,
        "has_more": bool(has_more),
        "offset": max(0, int(offset)),
        "limit": max(1, int(limit)),
    }


def _iter_sheet_rows(archive, part, shared, date_styles, offset, limit):
    """流式读一个工作表窗口，返回 ``(header, rows, has_more)``。

    第一行按约定当作表头（与 CSV 查看器保持一致）。``has_more`` 由「多读一行」得出，
    而不是猜的。
    """
    start = max(0, int(offset))
    want = max(1, int(limit))
    rows = []
    header = []
    seen = 0
    has_more = False
    with archive.open(part) as handle:
        for _event, element in ET.iterparse(handle, events=("end",)):
            if element.tag != S + "row":
                continue
            cells = {}
            for cell in element.findall(S + "c"):
                cells[_column_index(cell.get("r"))] = _xlsx_cell_value(
                    cell, shared, date_styles)
            width = (max(cells) + 1) if cells else 0
            line = [cells.get(i, "") for i in range(width)]
            seen += 1
            if seen == 1:
                header = line
            elif seen - 1 > start:
                if len(rows) < want:
                    rows.append({"r": seen, "c": line})
                else:
                    # 窗口已满又读到一行 —— 确定「还有更多」，到此为止
                    has_more = True
            element.clear()
            if has_more:
                break
    return header, rows, has_more


def from_xlsx(path, budget=0, lang="", **kwargs):
    """XLSX → 每个工作表一个可定位单元，供全文检索与「命中在第几张表」使用。"""
    units = []
    chunks = []
    sheet_names = []
    try:
        with _open(path) as archive:
            sheets = _xlsx_sheets(archive)
            if not sheets:
                raise _OfficeError("工作簿里没有工作表。", unsupported=True)
            shared = _xlsx_shared_strings(archive)
            date_styles = _xlsx_date_formats(archive)
            for index, (name, part) in enumerate(sheets):
                sheet_names.append(name)
                _header, rows, has_more = _iter_sheet_rows(
                    archive, part, shared, date_styles, 0, config.MAX_SHEET_ROWS)
                # 行数：优先用工作表声明的 dimension（免费），否则整趟数（有界）
                declared = _sheet_dimension(archive, part)
                if declared is None:
                    declared = _count_sheet_rows(archive, part)
                row_count = (declared - 1) if declared else len(rows)
                lines = []
                for row in rows:
                    text = " | ".join(str(c) for c in row["c"] if str(c).strip())
                    if text:
                        lines.append(text)
                body = "\n".join(lines)
                suffix = "共 %d 行" % row_count if row_count else "行数未知"
                label = "工作表 %s（%s%s）" % (name, suffix,
                                             "，已索引前 %d 行" % len(rows) if has_more else "")
                if body:
                    units.append(("sheet", index, label, "%s\n%s" % (name, body)))
                    chunks.append("%s\n%s" % (label, body))
    except _OfficeError as exc:
        return {"state": "unsupported" if exc.unsupported else "failed",
                "text": "", "error": str(exc), "lang": lang, "units": []}

    text = "\n\n".join(chunks).strip()
    return {
        "state": "ok" if text else "empty",
        "text": text,
        "lang": lang or "excel",
        "units": units,
        "sheets": sheet_names,
    }


# ---------------------------------------------------------------- PPTX

def _pptx_slide_parts(archive):
    """返回幻灯片部件路径，**顺序取自 ``p:sldIdLst``**。

    绝不能按文件名排序：``slide1.xml`` / ``slide10.xml`` / ``slide2.xml``
    的字典序会把第 10 页排到第 2 页前面。
    """
    root = _parse(archive, "ppt/presentation.xml", limit=4 << 20)
    if root is None:
        raise _OfficeError("演示文稿缺少 ppt/presentation.xml。", unsupported=True)
    rels = _rels(archive, "ppt/presentation.xml")
    container = root.find(P + "sldIdLst")
    if container is None:
        return []
    out = []
    for slide in container.findall(P + "sldId"):
        target = rels.get(slide.get(R + "id"))
        if target:
            out.append(target)
    return out


def _pptx_slide_text(root):
    """取一张幻灯片上的文字（含表格），按文本框出现顺序。"""
    parts = []
    for shape in root.iter(P + "sp"):
        # 形状名里带 Title 的当标题优先输出，便于生成单元标签
        texts = [node.text for node in shape.iter(A + "t") if node.text]
        if texts:
            parts.append("".join(texts).strip())
    for table in root.iter(A + "tbl"):
        rows = []
        for tr in table.findall(A + "tr"):
            cells = [_all_text(tc) for tc in tr.findall(A + "tc")]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            parts.append("[表格]\n" + "\n".join(rows))
    return "\n".join(part for part in parts if part).strip()


def _pptx_slide_title(root):
    for shape in root.iter(P + "sp"):
        name = ""
        c_nv = shape.find(".//" + P + "cNvPr")
        if c_nv is not None:
            name = c_nv.get("name") or ""
        texts = [node.text for node in shape.iter(A + "t") if node.text]
        if texts and re.search(r"(?i)(title|标题|標題)", name):
            return "".join(texts).strip()
    return ""


def from_pptx(path, budget=0, lang="", **kwargs):
    """PPTX → 每张幻灯片一个可定位单元（``#slide=N`` 深链依赖它）。"""
    units = []
    chunks = []
    try:
        with _open(path) as archive:
            parts = _pptx_slide_parts(archive)
            if not parts:
                raise _OfficeError("演示文稿里没有幻灯片。", unsupported=True)
            for index, part in enumerate(parts):
                root = _parse(archive, part, limit=8 << 20)
                if root is None:
                    continue
                body = _pptx_slide_text(root)
                title = _pptx_slide_title(root)
                label = "第 %d 页%s" % (index + 1, "：%s" % title if title else "")
                if body:
                    units.append(("slide", index + 1, label,
                                  "%s\n%s" % (label, body)))
                    chunks.append("%s\n%s" % (label, body))
    except _OfficeError as exc:
        return {"state": "unsupported" if exc.unsupported else "failed",
                "text": "", "error": str(exc), "lang": lang, "units": []}

    text = "\n\n".join(chunks).strip()
    return {
        "state": "ok" if text else "empty",
        "text": text,
        "lang": lang or "powerpoint",
        "units": units,
        "slides": len(units),
    }


def pptx_slides(path):
    """给 ``/api/file/html`` 用的逐页预览（标题 + 正文）。"""
    slides = []
    try:
        with _open(path) as archive:
            for index, part in enumerate(_pptx_slide_parts(archive)):
                root = _parse(archive, part, limit=8 << 20)
                if root is None:
                    continue
                slides.append({
                    "index": index + 1,
                    "title": _pptx_slide_title(root),
                    "text": _pptx_slide_text(root),
                })
    except _OfficeError:
        return None
    return slides
