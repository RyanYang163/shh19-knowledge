"""测试夹具生成器 —— **纯标准库，仓库里永不提交二进制样本**。

所有样本在运行时按字节构造。这样做有三个理由：

1. 二进制夹具一旦入库就变成「不知从哪来的文件」，审计时说不清；
2. 能精确控制每一个分支（未压缩 / FlateDecode / 对象流 / 加密 / 缺 ToUnicode），
   而这些分支用真实文件很难凑齐；
3. 夹具本身是代码，能被 review。

``make_pdf`` 支持的分支直接对应 ``extract_pdf`` 的每条路径。
"""

import os
import struct
import zlib
import zipfile


# ---------------------------------------------------------------- PDF

def _pdf_obj(number, body, stream=None, flate=False):
    """拼一个间接对象。``stream`` 给定时自动加 ``/Length``（与 ``/Filter``）并写流。

    ⚠️ 压缩了流就**必须**同时写 ``/Filter /FlateDecode`` ——
    早期版本的夹具漏了这一步，结果是「声称未压缩、字节却是 zlib」的假 PDF，
    让抽取器看似失败实则被夹具误导。夹具必须与规格一致，否则测的是自己。
    """
    out = bytearray(b"%d 0 obj\n" % number)
    if stream is None:
        out += body + b"\nendobj\n"
        return bytes(out)
    payload = zlib.compress(stream) if flate else stream
    if b"/Length" not in body:
        extra = b"/Length %d" % len(payload)
        if flate:
            extra += b" /Filter /FlateDecode"
        body = body.replace(b">>", extra + b" >>", 1)
    out += body + b"\nstream\n" + payload + b"\nendstream\nendobj\n"
    return bytes(out)


def make_pdf(pages=1, *, flate=True, objstm=False, encrypt=False,
             cid_no_tounicode=False, to_unicode=False, text=None,
             page_texts=None, broken_xref=False, garbage=False):
    """构造一个最小 PDF。

    :param pages: 页数
    :param flate: 内容流是否用 FlateDecode 压缩
    :param objstm: 是否把对象塞进 ``/ObjStm`` 对象流（PDF 1.5+ 的常见形态）
    :param encrypt: 是否声明 ``/Encrypt``（验证「加密 → unsupported」）
    :param to_unicode: 用 **Type0/CID + ToUnicode**（2 字节十六进制串），
        验证 CID 路径能正确还原
    :param cid_no_tounicode: 用 Type0/CID 且**不给** ToUnicode，
        验证「宁可 unsupported 也绝不索引乱码」

    默认（两者都不给）走**最常见的形态**：简单 Type1 字体 + WinAnsiEncoding +
    单字节字面量字符串。真实 PDF 里 Type0 必然配 2 字节码，绝不会配单字节字面量 ——
    早期版本的夹具犯了这个错，让抽取器看起来失败，其实是夹具不合规格。
    """
    if garbage:
        return b"this is not a pdf at all\x00\x01\x02" * 40

    if page_texts is None:
        if text is not None:
            page_texts = [text] * pages
        else:
            page_texts = ["BIOS upgrade requirements page %d" % (i + 1)
                          for i in range(pages)]

    is_cid = bool(to_unicode or cid_no_tounicode)

    obj = {}
    page_ids = []
    next_id = 10
    for _index in range(len(page_texts)):
        page_ids.append(next_id)
        next_id += 2                       # page + contents

    font_id = 3
    tounicode_id = 31
    descendant_id = 32

    obj[1] = _pdf_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    obj[2] = _pdf_obj(2, b"<< /Type /Pages /Kids [%s] /Count %d >>"
                      % (kids, len(page_ids)))

    if is_cid:
        obj[descendant_id] = _pdf_obj(
            descendant_id,
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /TestCID"
            b" /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>")
        body = (b"<< /Type /Font /Subtype /Type0 /BaseFont /TestCID"
                b" /Encoding /Identity-H /DescendantFonts [%d 0 R]" % descendant_id)
        if not cid_no_tounicode:
            # 把正文里用到的每个码位都映射到自身，这样解码后应还原出原文
            codes = sorted({ord(ch) for ch in "".join(page_texts)})
            entries = b"".join(b"<%04X> <%04X>\n" % (c, c) for c in codes)
            cmap = (b"/CIDInit /ProcSet findresource begin\n"
                    b"12 dict begin begincmap\n"
                    b"%d beginbfchar\n" % len(codes) + entries + b"endbfchar\n"
                    b"endcmap end end")
            obj[tounicode_id] = _pdf_obj(tounicode_id, b"<< >>", stream=cmap)
            body += b" /ToUnicode %d 0 R" % tounicode_id
        obj[font_id] = _pdf_obj(font_id, body + b" >>")
    else:
        obj[font_id] = _pdf_obj(
            font_id,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
            b" /Encoding /WinAnsiEncoding >>")

    for index, body_text in enumerate(page_texts):
        pid = page_ids[index]
        cid = pid + 1
        if is_cid:
            # Type0/Identity-H：两字节十六进制串（这才是真实形态）
            literal = b"<" + body_text.encode("utf-16-be").hex().upper().encode() + b">"
        else:
            literal = b"(" + body_text.encode("latin-1", "replace") + b")"
        content = (b"BT /F1 12 Tf 72 720 Td " + literal + b" Tj ET")
        obj[cid] = _pdf_obj(cid, b"<< >>", stream=content, flate=flate)
        obj[pid] = _pdf_obj(
            pid,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Contents %d 0 R /Resources << /Font << /F1 %d 0 R >> >> >>"
            % (cid, font_id))

    if objstm:
        # 只把**非流对象**收进对象流。规格不允许把 stream 放进 /ObjStm
        # （流必须是顶层间接对象），早期夹具把内容流也塞了进去，造出了不合规格的文件。
        stored = sorted(k for k in obj if k >= 10 and b"stream\n" not in obj[k])
        header = bytearray()
        payload = bytearray()
        for key in stored:
            raw = obj[key]
            body = raw.split(b"obj\n", 1)[1].rsplit(b"\nendobj", 1)[0]
            header += b"%d %d " % (key, len(payload))
            payload += body + b" "
            del obj[key]
        stream = bytes(header) + bytes(payload)
        obj[40] = _pdf_obj(
            40,
            b"<< /Type /ObjStm /N %d /First %d >>" % (len(stored), len(header)),
            stream=stream, flate=True)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    for number in sorted(obj):
        out += obj[number]

    trailer = b"<< /Root 1 0 R /Size %d" % (max(obj) + 1)
    if encrypt:
        trailer += b" /Encrypt 99 0 R"
    if broken_xref:
        # 故意给一个指向错误偏移的 xref —— 提取器必须靠扫描 obj 恢复
        out += b"xref\n0 1\n0000000000 65535 f \n"
    trailer += b" >>\n"
    out += b"trailer\n" + trailer
    out += b"\nstartxref\n%d\n%%%%EOF\n" % (0 if broken_xref else len(out))
    return bytes(out)


# ---------------------------------------------------------------- OOXML

def _zip_of(parts):
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


DOCX_HEAD = (b'<?xml version="1.0" encoding="UTF-8"?>'
             b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
             b'wordprocessingml/2006/main"><w:body>')


def _docx_para(text, style=None, outline=None):
    props = b""
    if style:
        props += b'<w:pStyle w:val="%s"/>' % style.encode()
    if outline is not None:
        props += b'<w:outlineLvl w:val="%d"/>' % outline
    ppr = b"<w:pPr>" + props + b"</w:pPr>" if props else b""
    return (b'<w:p>' + ppr + b'<w:r><w:t xml:space="preserve">'
            + text.encode("utf-8") + b'</w:t></w:r></w:p>')


def make_docx(paras=("Hello", "World"), table=None, styles=None,
              missing_document=False, bogus_zip_bomb=False):
    """构造一个 DOCX。``styles`` 为 ``{styleId: 样式名}``（用于验证本地化标题）。"""
    body = bytearray()
    for item in paras:
        if isinstance(item, dict):
            body += _docx_para(item.get("text", ""), item.get("style"),
                               item.get("outline"))
        else:
            body += _docx_para(item)
    if table:
        rows = bytearray(b"<w:tbl>")
        for row in table:
            rows += b"<w:tr>"
            for cell in row:
                rows += (b"<w:tc><w:p><w:r><w:t>" + str(cell).encode("utf-8")
                         + b"</w:t></w:r></w:p></w:tc>")
            rows += b"</w:tr>"
        rows += b"</w:tbl>"
        body += rows

    parts = {
        "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
        "_rels/.rels": b'<?xml version="1.0"?><Relationships/>',
    }
    if not missing_document:
        parts["word/document.xml"] = DOCX_HEAD + bytes(body) + b"</w:body></w:document>"
    if styles:
        style_xml = bytearray(b'<?xml version="1.0"?><w:styles xmlns:w="http://schemas.'
                              b'openxmlformats.org/wordprocessingml/2006/main">')
        for style_id, name in styles.items():
            style_xml += (b'<w:style w:styleId="%s"><w:name w:val="%s"/></w:style>'
                          % (style_id.encode(), name.encode("utf-8")))
        style_xml += b"</w:styles>"
        parts["word/styles.xml"] = bytes(style_xml)
    if bogus_zip_bomb:
        parts["word/huge.bin"] = b"\x00" * 1024
    return _zip_of(parts)


XLSX_HEAD = (b'<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
             b'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
             b'officeDocument/2006/relationships"><sheets>')


def make_xlsx(sheets=None, shared=None, styles=None, dimension=True):
    """构造一个 XLSX。

    :param sheets: ``[(表名, [[单元格, ...], ...])]``；单元格可以是 str/int/float
                   或用 ``("date", 45000)`` 指定日期序列号
    :param shared: 共享字符串表；不给则由 sheets 里的字符串自动生成
    :param styles: ``{样式序号: numFmtId}``
    """
    if sheets is None:
        sheets = [("Sheet1", [["Name", "Value"], ["alpha", 1], ["beta", 2]])]

    if shared is None:
        seen = {}
        shared = []
        for _name, rows in sheets:
            for row in rows:
                for cell in row:
                    if isinstance(cell, str) and cell not in seen:
                        seen[cell] = len(shared)
                        shared.append(cell)

    parts = {
        "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
        "_rels/.rels": b'<?xml version="1.0"?><Relationships/>',
    }

    # workbook.xml：表顺序就是这里的顺序
    workbook = bytearray(XLSX_HEAD)
    rels = bytearray(b'<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                     b'openxmlformats.org/package/2006/relationships">')
    for index, (name, _rows) in enumerate(sheets):
        rid = "rId%d" % (index + 1)
        workbook += (b'<sheet name="%s" sheetId="%d" r:id="%s"/>'
                     % (name.encode("utf-8"), index + 1, rid.encode()))
        rels += (b'<Relationship Id="%s" Type="http://schemas.openxmlformats.org/'
                 b'officeDocument/2006/relationships/worksheet"'
                 b' Target="worksheets/sheet%d.xml"/>' % (rid.encode(), index + 1))
    workbook += b"</sheets></workbook>"
    rels += b"</Relationships>"
    parts["xl/workbook.xml"] = bytes(workbook)
    parts["xl/_rels/workbook.xml.rels"] = bytes(rels)

    if shared:
        sst = bytearray(b'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.'
                        b'org/spreadsheetml/2006/main">')
        for text in shared:
            sst += b"<si><t>" + text.encode("utf-8") + b"</t></si>"
        sst += b"</sst>"
        parts["xl/sharedStrings.xml"] = bytes(sst)

    if styles:
        style_xml = bytearray(b'<?xml version="1.0"?><styleSheet xmlns="http://schemas.'
                              b'openxmlformats.org/spreadsheetml/2006/main">'
                              b"<cellXfs>")
        count = max(styles) + 1 if styles else 0
        for index in range(count):
            style_xml += b'<xf numFmtId="%d"/>' % styles.get(index, 0)
        style_xml += b"</cellXfs></styleSheet>"
        parts["xl/styles.xml"] = bytes(style_xml)

    for index, (_name, rows) in enumerate(sheets):
        sheet = bytearray(b'<?xml version="1.0"?><worksheet xmlns="http://schemas.'
                          b'openxmlformats.org/spreadsheetml/2006/main">')
        if dimension:
            widest = max((len(r) for r in rows), default=1)
            sheet += (b'<dimension ref="A1:%s%d"/>'
                      % (_column_letter(widest).encode(), len(rows)))
        sheet += b"<sheetData>"
        for row_index, row in enumerate(rows, 1):
            sheet += b'<row r="%d">' % row_index
            for col_index, cell in enumerate(row):
                ref = "%s%d" % (_column_letter(col_index + 1), row_index)
                if isinstance(cell, tuple) and cell and cell[0] == "date":
                    sheet += (b'<c r="%s" s="1"><v>%s</v></c>'
                              % (ref.encode(), str(cell[1]).encode()))
                elif isinstance(cell, bool):
                    sheet += (b'<c r="%s" t="b"><v>%d</v></c>'
                              % (ref.encode(), 1 if cell else 0))
                elif isinstance(cell, str):
                    try:
                        position = shared.index(cell)
                        sheet += (b'<c r="%s" t="s"><v>%d</v></c>'
                                  % (ref.encode(), position))
                    except ValueError:
                        sheet += (b'<c r="%s" t="inlineStr"><is><t>%s</t></is></c>'
                                  % (ref.encode(), cell.encode("utf-8")))
                else:
                    sheet += (b'<c r="%s"><v>%s</v></c>'
                              % (ref.encode(), str(cell).encode()))
            sheet += b"</row>"
        sheet += b"</sheetData></worksheet>"
        parts["xl/worksheets/sheet%d.xml" % (index + 1)] = bytes(sheet)

    return _zip_of(parts)


def _column_letter(index):
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def make_pptx(slides=("First slide", "Second slide"), filename_order=None,
              tables=None):
    """构造一个 PPTX。

    :param filename_order: 刻意让**文件名顺序**与**页序**不一致，
        用来验证提取器确实按 ``p:sldIdLst`` 而不是按文件名排序
    """
    parts = {
        "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
        "_rels/.rels": b'<?xml version="1.0"?><Relationships/>',
    }
    count = len(slides)
    # 默认制造乱序：第 1 页存成 slide10.xml，第 2 页存成 slide2.xml
    names = filename_order or ["slide%d.xml" % (10 if i == 0 else i)
                               for i in range(count)]

    presentation = bytearray(b'<?xml version="1.0"?><p:presentation xmlns:p="http://'
                             b'schemas.openxmlformats.org/presentationml/2006/main"'
                             b' xmlns:r="http://schemas.openxmlformats.org/'
                             b'officeDocument/2006/relationships"><p:sldIdLst>')
    rels = bytearray(b'<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                     b'openxmlformats.org/package/2006/relationships">')
    for index, filename in enumerate(names):
        rid = "rId%d" % (index + 100)
        presentation += b'<p:sldId id="%d" r:id="%s"/>' % (256 + index, rid.encode())
        rels += (b'<Relationship Id="%s" Type="http://schemas.openxmlformats.org/'
                 b'officeDocument/2006/relationships/slide"'
                 b' Target="slides/%s"/>' % (rid.encode(), filename.encode()))
    presentation += b"</p:sldIdLst></p:presentation>"
    rels += b"</Relationships>"
    parts["ppt/presentation.xml"] = bytes(presentation)
    parts["ppt/_rels/presentation.xml.rels"] = bytes(rels)

    for index, filename in enumerate(names):
        body = bytearray(b'<?xml version="1.0"?><p:sld xmlns:p="http://schemas.'
                         b'openxmlformats.org/presentationml/2006/main"'
                         b' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                         b"<p:cSld><p:spTree>")
        body += (b'<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/></p:nvSpPr>'
                 b"<p:txBody><a:p><a:r><a:t>"
                 + str(slides[index]).encode("utf-8") + b"</a:t></a:r></a:p></p:txBody></p:sp>")
        if tables and index < len(tables):
            body += b"<p:graphicFrame><a:tbl>"
            for row in tables[index]:
                body += b"<a:tr>"
                for cell in row:
                    body += (b"<a:tc><a:txBody><a:p><a:r><a:t>"
                             + str(cell).encode("utf-8")
                             + b"</a:t></a:r></a:p></a:txBody></a:tc>")
                body += b"</a:tr>"
            body += b"</a:tbl></p:graphicFrame>"
        body += b"</p:spTree></p:cSld></p:sld>"
        parts["ppt/slides/" + filename] = bytes(body)

    return _zip_of(parts)


# ---------------------------------------------------------------- 其它

def make_zip_bomb(declared=600 * 1024 * 1024, name="word/document.xml"):
    """构造一个**头部声明体积巨大、实际内容很小**的 zip。

    用来验证「读取成员前先比 ``getinfo().file_size``」这道守卫真的生效。

    为什么不能靠 ``ZipInfo.file_size``：``ZipFile.writestr`` 会用实际数据长度**覆盖**
    你手设的值，所以那样造出来的夹具声明的是真实大小，测不到守卫。
    这里改为构造正常 zip 后，直接改写**本地文件头**与**中央目录**里的
    未压缩大小字段（两处都要改，``getinfo`` 读的是中央目录）。
    """
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(name, b"x" * 1024)
    data = bytearray(buffer.getvalue())

    fake = struct.pack("<I", declared)
    # 本地文件头：PK\x03\x04 之后，压缩大小在 18、未压缩大小在 22
    local = data.find(b"PK\x03\x04")
    if local >= 0:
        data[local + 18:local + 22] = fake
        data[local + 22:local + 26] = fake
    # 中央目录：PK\x01\x02 之后，压缩大小在 20、未压缩大小在 24
    central = data.find(b"PK\x01\x02")
    if central >= 0:
        data[central + 20:central + 24] = fake
        data[central + 24:central + 28] = fake
    return bytes(data)


def make_png(width=8, height=8, rgb=(200, 40, 40)):
    """最小 PNG 编码器（与 ``extract.probe_image_size`` 的读取路径对称）。"""
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def make_gif(width=4, height=4):
    return (b"GIF89a" + struct.pack("<HH", width, height)
            + b"\x00\x00\x00" + b"\x00" * 8 + b"\x3b")


def make_bmp(width=3, height=2):
    """最小 BMP（24 位）。**头部必须严格按规格**：DIB 头长度 40，然后才是宽高。

    早期版本把「头长度 + 保留字段」写成了两个 int32，于是宽高落在错误偏移上，
    探测函数读到 (0, 3)。夹具的字节布局错了，测出来的「失败」是假的。
    """
    row_bytes = ((width * 3 + 3) // 4) * 4
    pixels = b"\x00" * (row_bytes * height)
    offset = 54
    return (b"BM"
            + struct.pack("<I", offset + len(pixels))       # 文件大小
            + b"\x00\x00\x00\x00"                            # 保留
            + struct.pack("<I", offset)                      # 像素数据偏移
            + struct.pack("<I", 40)                          # DIB 头长度
            + struct.pack("<ii", width, height)              # 宽 / 高（signed）
            + struct.pack("<HH", 1, 24)                      # 平面数 / 位深
            + struct.pack("<I", 0)                           # 压缩方式
            + struct.pack("<I", len(pixels))                 # 图像数据大小
            + struct.pack("<ii", 2835, 2835)                 # 水平/垂直分辨率
            + struct.pack("<II", 0, 0)                       # 调色板
            + pixels)


def make_tree(root, spec):
    """按 ``spec`` 造一棵目录树。

    ``spec`` 是 ``{相对路径: 内容}``；``内容`` 为 ``bytes`` / ``str`` 时写文件，
    为 ``None`` 时建目录，为 ``symlink:<目标>`` 时建软链。
    """
    for rel, content in spec.items():
        full = os.path.join(root, rel)
        if content is None:
            os.makedirs(full, exist_ok=True)
            continue
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if isinstance(content, str) and content.startswith("symlink:"):
            target = content[len("symlink:"):]
            try:
                if os.path.exists(full) or os.path.islink(full):
                    os.remove(full)
                os.symlink(target, full)
            except (OSError, NotImplementedError, AttributeError):
                # Windows 上建软链需要特权；跳过而不是让测试报错
                pass
            continue
        data = content.encode("utf-8") if isinstance(content, str) else content
        with open(full, "wb") as handle:
            handle.write(data)
    return root
