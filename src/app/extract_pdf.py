"""PDF 文本层抽取（纯标准库，不调用任何外部程序）。

设计文档 §11 推荐 PDF.js —— 那是**前端渲染**，已经 vendor 进 ``webui/``；
本模块解决的是另一个问题：**让 PDF 的内容可被全文检索**，并给出命中所在的页码。

### 做法

1. 扫全文找出所有 ``N G obj`` 的偏移，建立对象号 → 正文的映射
   （比实现 xref 表 + xref 流两套定位方式更稳，且天然容忍增量更新 —— 后出现的定义覆盖先前的）。
2. **解压对象流 ``/ObjStm``**：PDF 1.5 之后大量对象藏在压缩流里，
   不处理这一条，绝大多数现代 PDF 看起来都会是「没有文本」。
3. 找 ``/Type /Catalog`` → ``/Pages`` → 递归 ``/Kids``，按文档顺序收集 ``/Type /Page``。
4. 每页 ``/Contents``（可能是流或流数组）解压后解析文本算子：
   ``BT/ET``、``Tf``、``Td/TD/Tm/T*``、``Tj/TJ/'/"``。
5. 按字体解码字节：简单字体走 ``/Encoding``（WinAnsi / MacRoman / Standard + ``/Differences``
   字形名），Type0/CID 走 ``/ToUnicode`` CMap。

### 明确不做（都返回 ``unsupported`` 并给出可读原因，绝不猜）

* **加密 PDF**（``/Encrypt``，含空口令的 RC4/AES）—— 没有文本层可读。
* **扫描件 / 纯图片 PDF** —— 本来就没有文本层（那是 OCR 要解决的，V1 不做）。
* **Type0/CID 字体但缺 ``/ToUnicode``** —— 能取出字节却对不上字符。
  **宁可标 unsupported 也绝不索引乱码**：把一堆 ``\x03\x1a`` 塞进索引，
  用户搜什么都搜不到，还会以为是应用坏了。这是本模块最重要的一条规则。
* LZW / JPEG2000 / JBIG2 编码的流、注释、表单域、书签（``/Outlines`` 只做尽力而为）。

### 内存安全

所有解压都带硬上限（``MAX_PDF_STREAM`` / 单页 ``MAX_PDF_PAGE_TEXT``）：
一个 FlateDecode 炸弹可以在几 KB 的输入上产出几十 GB，没有上限就是一次 DoS。
"""

import os
import re
import zlib

from . import config

#: ``N G obj`` 的扫描模式。对象号最多 10 位，避免在二进制流里误匹配
_OBJ_RE = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")
_STREAM_RE = re.compile(rb"stream\r?\n")
_ENDOBJ_RE = re.compile(rb"\bendobj\b")

_WHITESPACE = b"\x00\t\n\x0c\r "
_DELIMITERS = b"()<>[]{}/%"


class _PdfError(Exception):
    def __init__(self, message, unsupported=False):
        super().__init__(message)
        self.unsupported = unsupported


# ---------------------------------------------------------------- 词法

class _Lexer:
    """极简 PDF 对象词法分析器。只实现抽取文本所需的子集。"""

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos
        self.length = len(data)

    def skip_space(self):
        data, length = self.data, self.length
        while self.pos < length:
            char = data[self.pos:self.pos + 1]
            if char in _WHITESPACE:
                self.pos += 1
            elif char == b"%":
                newline = data.find(b"\n", self.pos)
                self.pos = length if newline < 0 else newline + 1
            else:
                return

    def read_token(self):
        """读一个 token，返回 ``(kind, value)``。kind ∈ name/number/string/hex/dict/array/kw/}。"""
        self.skip_space()
        if self.pos >= self.length:
            return None, None
        data = self.data
        char = data[self.pos:self.pos + 1]

        if char == b"/":
            self.pos += 1
            start = self.pos
            while self.pos < self.length:
                byte = data[self.pos:self.pos + 1]
                if byte in _WHITESPACE or byte in _DELIMITERS:
                    break
                self.pos += 1
            raw = data[start:self.pos]
            # #xx 是名字里的转义
            name = re.sub(rb"#([0-9A-Fa-f]{2})",
                          lambda m: bytes([int(m.group(1), 16)]), raw)
            return "name", name.decode("latin-1")

        if char == b"(":
            return "string", self._read_literal_string()

        if char == b"<":
            if data[self.pos + 1:self.pos + 2] == b"<":
                self.pos += 2
                return "dict", "<<"
            return "hex", self._read_hex_string()

        if char == b">":
            if data[self.pos + 1:self.pos + 2] == b">":
                self.pos += 2
                return "}", ">>"
            self.pos += 1
            return "kw", ">"

        if char == b"[":
            self.pos += 1
            return "array", "["

        if char == b"]":
            self.pos += 1
            return "kw", "]"

        if char in b"{}":
            self.pos += 1
            return "kw", char.decode()

        # 数字、引用（N G R）、关键字
        start = self.pos
        while self.pos < self.length:
            byte = data[self.pos:self.pos + 1]
            if byte in _WHITESPACE or byte in _DELIMITERS:
                break
            self.pos += 1
        raw = data[start:self.pos]
        if not raw:
            self.pos += 1
            return "kw", ""
        if re.fullmatch(rb"[+-]?\d+", raw):
            return "number", int(raw)
        if re.fullmatch(rb"[+-]?(\d*\.\d*|\d+)", raw):
            try:
                return "number", float(raw)
            except ValueError:
                return "kw", raw.decode("latin-1")
        return "kw", raw.decode("latin-1", "replace")

    def _read_literal_string(self):
        """读 ``( ... )``，处理嵌套括号与转义。返回 bytes。"""
        data = self.data
        self.pos += 1
        depth = 1
        out = bytearray()
        while self.pos < self.length:
            char = data[self.pos:self.pos + 1]
            if char == b"\\":
                nxt = data[self.pos + 1:self.pos + 2]
                mapping = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                           b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
                if nxt in mapping:
                    out += mapping[nxt]
                    self.pos += 2
                    continue
                digits = re.match(rb"[0-7]{1,3}", data[self.pos + 1:self.pos + 4])
                if digits:
                    out.append(int(digits.group(0), 8) & 0xFF)
                    self.pos += 1 + len(digits.group(0))
                    continue
                if nxt in (b"\n", b"\r"):
                    self.pos += 2
                    continue
                self.pos += 2
                continue
            if char == b"(":
                depth += 1
            elif char == b")":
                depth -= 1
                if depth == 0:
                    self.pos += 1
                    return bytes(out)
            out += char
            self.pos += 1
        return bytes(out)

    def _read_hex_string(self):
        data = self.data
        self.pos += 1
        end = data.find(b">", self.pos)
        if end < 0:
            end = self.length
        raw = re.sub(rb"[^0-9A-Fa-f]", b"", data[self.pos:end])
        self.pos = end + 1
        if len(raw) % 2:
            raw += b"0"
        try:
            return bytes.fromhex(raw.decode("ascii"))
        except ValueError:
            return b""


def _parse_object(lexer, depth=0):
    """解析一个对象（含嵌套 dict/array）。返回 Python 结构。

    间接引用 ``N G R`` 在这里**不解引用**（抽取文本不需要跨对象取值），
    统一表示成 ``("ref", num)``，由调用方按需查表。
    """
    if depth > 40:
        return None
    kind, value = lexer.read_token()
    if kind is None:
        return None
    if kind in ("number", "string", "hex", "name"):
        # 探测 ``N G R`` 间接引用
        if kind == "number" and isinstance(value, int):
            saved = lexer.pos
            kind2, value2 = lexer.read_token()
            if kind2 == "number" and isinstance(value2, int):
                kind3, value3 = lexer.read_token()
                if kind3 == "kw" and value3 == "R":
                    return ("ref", value)
                lexer.pos = saved
            elif kind2 is None:
                pass
            else:
                lexer.pos = saved
        return value
    if kind == "dict":
        out = {}
        while True:
            key_kind, key = lexer.read_token()
            if key_kind is None or (key_kind == "}" ):
                break
            if key_kind != "name":
                _parse_object(lexer, depth + 1)   # 跳过异常内容
                continue
            out[key] = _parse_object(lexer, depth + 1)
        return out
    if kind == "array":
        out = []
        while True:
            saved = lexer.pos
            item_kind, item = lexer.read_token()
            if item_kind is None:
                break
            if item_kind == "kw" and item == "]":
                break
            lexer.pos = saved
            out.append(_parse_object(lexer, depth + 1))
            if len(out) > 100000:
                break
        return out
    return value


# ---------------------------------------------------------------- 文档

class _Document:
    """把一个 PDF 文件读成「对象号 → 对象」的映射 + 页序。"""

    def __init__(self, data):
        self.data = data
        self.objects = {}
        self.trailers = []
        self.encrypted = False
        self._scan_objects()
        self._expand_object_streams()
        self._scan_trailers()

    # -- 对象表 --

    def _scan_objects(self):
        for match in _OBJ_RE.finditer(self.data):
            number = int(match.group(1))
            body_start = match.end()
            stream = _STREAM_RE.search(self.data, body_start, body_start + 4096)
            endobj = _ENDOBJ_RE.search(self.data, body_start)
            limit = stream.start() if stream else (endobj.start() if endobj else body_start + 65536)
            if limit <= body_start:
                continue
            try:
                lexer = _Lexer(self.data, body_start)
                obj = _parse_object(lexer)
            except Exception:  # noqa: BLE001 —— 坏对象跳过，不让整篇失败
                continue
            # 后出现的定义覆盖先前的（增量更新语义）
            self.objects[number] = (obj, stream, endobj, body_start)

    def _expand_object_streams(self):
        """把 ``/ObjStm`` 里的对象取出来。

        不处理这一条，PDF 1.5+ 的文件会看起来「没有文本」—— 正文对象全在压缩流里。
        """
        for number in list(self.objects):
            obj, _stream, _endobj, _start = self.objects[number]
            if not isinstance(obj, dict) or obj.get("Type") != "ObjStm":
                continue
            try:
                payload = self.stream_data(number)
                count = int(obj.get("N") or 0)
                first = int(obj.get("First") or 0)
                header = payload[:first].split()
                pairs = []
                for index in range(0, min(len(header) - 1, count * 2), 2):
                    pairs.append((int(header[index]), int(header[index + 1])))
                for position, (obj_num, offset) in enumerate(pairs):
                    end = pairs[position + 1][1] if position + 1 < len(pairs) else len(payload) - first
                    raw = payload[first + offset:first + end]
                    lexer = _Lexer(raw)
                    parsed = _parse_object(lexer)
                    if parsed is not None:
                        # 已存在的顶层对象优先（顶层定义通常更新）
                        self.objects.setdefault(obj_num, (parsed, None, None, 0))
            except Exception:  # noqa: BLE001
                continue

    def _scan_trailers(self):
        for match in re.finditer(rb"\btrailer\b", self.data):
            try:
                lexer = _Lexer(self.data, match.end())
                parsed = _parse_object(lexer)
                if isinstance(parsed, dict):
                    self.trailers.append(parsed)
            except Exception:  # noqa: BLE001
                continue
        for _number, (obj, _stream, _endobj, _start) in self.objects.items():
            if isinstance(obj, dict) and obj.get("Type") == "XRef":
                for key in ("Root", "Encrypt", "Info"):
                    if key in obj:
                        self.trailers.append(obj)
                        break
        for trailer in self.trailers:
            if "Encrypt" in trailer:
                self.encrypted = True

    def resolve(self, value, depth=0):
        """解引用 ``("ref", N)``；其它值原样返回。"""
        if depth > 32:
            return None
        if isinstance(value, tuple) and len(value) == 2 and value[0] == "ref":
            entry = self.objects.get(int(value[1]))
            if not entry:
                return None
            return self.resolve(entry[0], depth + 1)
        return value

    def stream_data(self, number, limit=None):
        """取一个流对象的**解压后**内容（带硬上限）。"""
        entry = self.objects.get(int(number))
        if not entry:
            return b""
        obj, stream, endobj, _start = entry
        if stream is None:
            return b""
        start = stream.end()
        end = endobj.start() if endobj else len(self.data)
        raw = self.data[start:end]
        # 去掉结尾的换行与 "endstream"
        raw = re.sub(rb"\s*endstream\s*$", b"", raw)
        return _decode_stream(raw, obj if isinstance(obj, dict) else {}, limit)

    def catalog(self):
        for trailer in reversed(self.trailers):
            root = trailer.get("Root")
            catalog = self.resolve(root)
            if isinstance(catalog, dict):
                return catalog
        for _number, (obj, _stream, _endobj, _start) in self.objects.items():
            if isinstance(obj, dict) and obj.get("Type") == "Catalog":
                return obj
        return None

    def pages(self, limit=20000):
        """按文档顺序返回 ``/Type /Page`` 对象（及其对象号，用于取 Resources 继承）。"""
        catalog = self.catalog()
        if not isinstance(catalog, dict):
            return []
        root = self.resolve(catalog.get("Pages"))
        if not isinstance(root, dict):
            return []
        out = []
        stack = [root]
        seen = set()

        def walk(node, inherited):
            if len(out) >= limit:
                return
            if not isinstance(node, dict):
                return
            merged = dict(inherited)
            for key in ("Resources", "MediaBox", "Rotate", "CropBox"):
                if key in node:
                    merged[key] = node[key]
            kind = node.get("Type")
            if kind == "Page":
                out.append((node, merged))
                return
            kids = self.resolve(node.get("Kids"))
            if not isinstance(kids, list):
                return
            for kid in kids:
                if isinstance(kid, tuple) and kid[0] == "ref":
                    number = int(kid[1])
                    if number in seen:
                        continue
                    seen.add(number)
                    walk(self.resolve(kid) or {}, merged)
                else:
                    walk(kid, merged)

        walk(root, {})
        return out

    def fonts_for(self, page, inherited):
        """收集一页可用的字体：``{字体资源名: 字体字典}``。"""
        resources = self.resolve(page.get("Resources")) or self.resolve(
            inherited.get("Resources"))
        if not isinstance(resources, dict):
            return {}
        fonts = self.resolve(resources.get("Font"))
        if not isinstance(fonts, dict):
            return {}
        out = {}
        for key, value in fonts.items():
            resolved = self.resolve(value)
            if isinstance(resolved, dict):
                out[key] = resolved
        return out

    def content_for(self, page, inherited):
        """一页的正文内容（多个流会拼接，**按数组顺序**）。

        ⚠️ 这里**不能**先 ``resolve`` ``/Contents``：``resolve`` 会把 ``("ref", N)``
        变成对象字典，而取流内容需要的是**对象号**本身（``stream_data(N)``）。
        早期版本先 resolve 再判 tuple，结果一个流都取不到、每页都判成「没有文本层」。
        """
        contents = page.get("Contents")
        if contents is None:
            contents = inherited.get("Contents")

        numbers = []
        if isinstance(contents, tuple) and len(contents) == 2 and contents[0] == "ref":
            numbers.append(int(contents[1]))
        elif isinstance(contents, list):
            for item in contents:
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "ref":
                    numbers.append(int(item[1]))

        chunks = []
        total = 0
        for number in numbers:
            chunk = self.stream_data(number, config.MAX_PDF_STREAM)
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > config.MAX_PDF_STREAM:
                break
        return b"\n".join(chunks)


# ---------------------------------------------------------------- 流解码

_PNG_PREDICTOR = {10: "sub", 11: "up", 12: "average", 13: "paeth", 14: "optimum"}


def _decode_stream(raw, obj, limit=None):
    """按 ``/Filter`` 解压流内容。认不出的编码返回空串（调用方按 unsupported 处理）。"""
    cap = int(limit or config.MAX_PDF_STREAM)
    filters = obj.get("Filter") if isinstance(obj, dict) else None
    if isinstance(filters, str):
        filters = [filters]
    if not filters:
        return raw[:cap]

    data = raw
    for name in filters:
        if name in ("FlateDecode", "Fl"):
            data = _inflate(data, cap)
            if data is None:
                return b""
        elif name in ("ASCIIHexDecode", "AHx"):
            cleaned = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
            if len(cleaned) % 2:
                cleaned += b"0"
            try:
                data = bytes.fromhex(cleaned.decode("ascii"))
            except ValueError:
                return b""
        elif name in ("ASCII85Decode", "A85"):
            try:
                import base64

                data = base64.a85decode(data.split(b"~>")[0], adobe=False)
            except Exception:  # noqa: BLE001
                return b""
        else:
            # LZW / DCT / JPX / JBIG2 / CCITT：本模块不处理
            return b""
    return data[:cap]


def _inflate(data, cap):
    """zlib 解压并**硬性截断**到 ``cap``。

    用 ``decompressobj`` 而不是 ``zlib.decompress``：后者会一次性把整块产出放在内存里，
    遇到解压炸弹直接 OOM。
    """
    try:
        engine = zlib.decompressobj()
        out = engine.decompress(data, cap)
        if engine.unconsumed_tail:
            return out        # 触顶：只用前 cap 字节，不继续解
        return out
    except zlib.error:
        # 有些流是裸 deflate（没有 zlib 头）
        try:
            engine = zlib.decompressobj(-15)
            return engine.decompress(data, cap)
        except zlib.error:
            return None


# ---------------------------------------------------------------- 字体解码

_STANDARD = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#",
    "dollar": "$", "percent": "%", "ampersand": "&", "quotesingle": "'",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+",
    "comma": ",", "hyphen": "-", "period": ".", "slash": "/", "zero": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "colon": ":", "semicolon": ";",
    "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|",
    "braceright": "}", "asciitilde": "~", "bullet": "•", "endash": "–",
    "emdash": "—", "quoteleft": "‘", "quoteright": "’",
    "quotedblleft": "“", "quotedblright": "”", "ellipsis": "…",
    "dagger": "†", "daggerdbl": "‡", "perthousand": "‰", "guilsinglleft": "‹",
    "guilsinglright": "›", "quotedblbase": "„", "quotesinglbase": "‚",
    "fi": "ﬁ", "fl": "ﬂ", "fraction": "⁄", "degree": "°", "plusminus": "±",
    "multiply": "×", "divide": "÷", "minus": "−", "periodcentered": "·",
    "trademark": "™", "copyright": "©", "registered": "®", "section": "§",
    "paragraph": "¶", "germandbls": "ß", "euro": "€", "sterling": "£",
    "yen": "¥", "cent": "¢", "currency": "¤", "brokenbar": "¦",
    "acute": "´", "cedilla": "¸", "dieresis": "¨", "macron": "¯",
    "circumflex": "ˆ", "caron": "ˇ", "breve": "˘", "ogonek": "˛",
    "gravecomb": "̀", "nobreakspace": " ", "softhyphen": "­",
}
# 常见带重音字母
for _name, _char in {
    "agrave": "à", "aacute": "á", "acircumflex": "â", "atilde": "ã",
    "adieresis": "ä", "aring": "å", "ae": "æ", "ccedilla": "ç",
    "egrave": "è", "eacute": "é", "ecircumflex": "ê", "edieresis": "ë",
    "igrave": "ì", "iacute": "í", "icircumflex": "î", "idieresis": "ï",
    "eth": "ð", "ntilde": "ñ", "ograve": "ò", "oacute": "ó",
    "ocircumflex": "ô", "otilde": "õ", "odieresis": "ö", "oslash": "ø",
    "ugrave": "ù", "uacute": "ú", "ucircumflex": "û", "udieresis": "ü",
    "yacute": "ý", "thorn": "þ", "ydieresis": "ÿ",
    "Agrave": "À", "Aacute": "Á", "Acircumflex": "Â", "Atilde": "Ã",
    "Adieresis": "Ä", "Aring": "Å", "AE": "Æ", "Ccedilla": "Ç",
    "Egrave": "È", "Eacute": "É", "Ecircumflex": "Ê", "Edieresis": "Ë",
    "Igrave": "Ì", "Iacute": "Í", "Icircumflex": "Î", "Idieresis": "Ï",
    "Ntilde": "Ñ", "Ograve": "Ò", "Oacute": "Ó", "Ocircumflex": "Ô",
    "Otilde": "Õ", "Odieresis": "Ö", "Oslash": "Ø", "Ugrave": "Ù",
    "Uacute": "Ú", "Ucircumflex": "Û", "Udieresis": "Ü", "Yacute": "Ý",
}.items():
    _STANDARD[_name] = _char


def _glyph_to_char(name):
    """字形名 → 字符。支持 ``uniXXXX`` / ``uXXXX`` 形式。"""
    if name in _STANDARD:
        return _STANDARD[name]
    match = re.fullmatch(r"uni([0-9A-Fa-f]{4})", name)
    if match:
        try:
            return chr(int(match.group(1), 16))
        except ValueError:
            return ""
    match = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    if match:
        try:
            return chr(int(match.group(1), 16))
        except ValueError:
            return ""
    if len(name) == 1:
        return name
    return ""


def _parse_tounicode(data):
    """解析 ``/ToUnicode`` 的 CMap，返回 ``{码点: 字符}``。"""
    mapping = {}
    if not data:
        return mapping
    text = data.decode("latin-1", "replace")
    for block in re.finditer(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block.group(1)):
            mapping[int(src, 16)] = _hex_to_text(dst)
    for block in re.finditer(r"beginbfrange(.*?)endbfrange", text, re.S):
        body = block.group(1)
        for src, dst, tail in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]+>|\[[^\]]*\])", body):
            start, end = int(src, 16), int(dst, 16)
            if tail.startswith("["):
                items = re.findall(r"<([0-9A-Fa-f]+)>", tail)
                for offset, item in enumerate(items):
                    mapping[start + offset] = _hex_to_text(item)
            else:
                base = int(tail.strip("<>"), 16)
                for offset in range(0, min(end - start + 1, 65536)):
                    try:
                        mapping[start + offset] = chr(base + offset)
                    except (ValueError, OverflowError):
                        break
    return mapping


def _hex_to_text(hex_text):
    """把 CMap 目标（可能是多字节 UTF-16BE）转成字符串。"""
    try:
        raw = bytes.fromhex(hex_text)
    except ValueError:
        return ""
    if len(raw) % 2:
        raw += b"\x00"
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


class _Font:
    """把一个字体字典编译成 ``字节 → 字符`` 的解码器。

    ``reliable=False`` 表示**我们已知解不出来**（Type0/CID 缺 ``/ToUnicode``）。
    这时 :meth:`decode` 返回 ``None``，调用方据此把文档标成 ``unsupported`` ——
    宁可承认读不了，也不能把乱码写进索引。
    """

    def __init__(self, document, font_dict):
        self.subtype = str(font_dict.get("Subtype") or "")
        self.two_byte = self.subtype == "Type0"
        self.map = {}
        self.reliable = True
        self._build(document, font_dict)

    def _build(self, document, font_dict):
        # 一、/ToUnicode 是最可靠的来源，有它就够
        tounicode = font_dict.get("ToUnicode")
        if isinstance(tounicode, tuple) and tounicode[0] == "ref":
            raw = document.stream_data(int(tounicode[1]), 8 << 20)
            if raw:
                self.map = _parse_tounicode(raw)
        if self.map:
            return

        # 二、Type0/CID 没拿到 ToUnicode 就到此为止，不猜
        if self.two_byte:
            self.reliable = False
            return

        # 三、简单字体：/Encoding 的 /Differences 覆盖基础编码
        encoding = font_dict.get("Encoding")
        base = None
        differences = {}
        if isinstance(encoding, str):
            base = encoding
        elif isinstance(encoding, dict):
            if isinstance(encoding.get("BaseEncoding"), str):
                base = encoding["BaseEncoding"]
            diff = encoding.get("Differences")
            if isinstance(diff, list):
                code = 0
                for item in diff:
                    if isinstance(item, (int, float)):
                        code = int(item)
                    elif isinstance(item, str):
                        differences[code] = item
                        code += 1

        for code in range(32, 127):
            self.map[code] = chr(code)
        for code, name in differences.items():
            char = _glyph_to_char(name)
            if char:
                self.map[code] = char
        if base in ("WinAnsiEncoding", "MacRomanEncoding", None):
            # 0xA0–0xFF 在 WinAnsi 下与 Latin-1 一致；检索关心的字母数字在两种
            # 编码里本来就相同，所以这里不追求 0x80–0x9F 那几个特殊符号的精确性。
            for code in range(0xA0, 0x100):
                self.map.setdefault(code, chr(code))

    def decode(self, raw):
        """解码一段字符串字节；已知解不出来时返回 ``None``。"""
        if not self.reliable:
            return None
        if self.two_byte:
            out = []
            for index in range(0, len(raw) - 1, 2):
                code = (raw[index] << 8) | raw[index + 1]
                out.append(self.map.get(code, ""))
            return "".join(out)
        return "".join(self.map.get(byte, "") for byte in raw)


# ---------------------------------------------------------------- 内容流文本

def _decode_operand(operand):
    if isinstance(operand, bytes):
        return operand
    if isinstance(operand, str):
        return operand.encode("latin-1", "ignore")
    return None


def _extract_page_text(content, fonts):
    """解析一页的正文内容流，取出文本。"""
    lexer = _Lexer(content)
    pieces = []
    current_font = None
    last_y = None
    last_x = None
    pending = []
    operand_stack = []

    def flush():
        if pending:
            pieces.append("".join(pending))
            del pending[:]

    while True:
        mark = lexer.pos
        kind, value = lexer.read_token()
        if kind is None:
            break
        if kind in ("number", "string", "hex", "name", "dict", "array"):
            if kind == "array":
                lexer.pos = mark
                operand_stack.append(_parse_object(lexer))
            else:
                operand_stack.append(value)
            if len(operand_stack) > 64:
                operand_stack = operand_stack[-64:]
            continue

        operator = value
        if operator == "Tf" and len(operand_stack) >= 2:
            candidate = operand_stack[-2]
            if isinstance(candidate, str):
                current_font = fonts.get(candidate)
        elif operator in ("Td", "TD") and len(operand_stack) >= 2:
            dx, dy = operand_stack[-2], operand_stack[-1]
            if isinstance(dx, (int, float)):
                if isinstance(last_x, (int, float)) and isinstance(last_y, (int, float)):
                    last_x += dx
                    last_y += dy
                else:
                    last_x, last_y = dx, dy
                if isinstance(dy, (int, float)) and abs(dy) > 0.5:
                    flush()
                    pieces.append("\n")
        elif operator == "Tm" and len(operand_stack) >= 6:
            x, y = operand_stack[-2], operand_stack[-1]
            if isinstance(y, (int, float)) and isinstance(last_y, (int, float)):
                if abs(y - last_y) > 0.5:
                    flush()
                    pieces.append("\n")
            last_x, last_y = x, y
        elif operator == "T*":
            flush()
            pieces.append("\n")
        elif operator in ("Tj", "'", '"'):
            if operator in ("'", '"'):
                flush()
                pieces.append("\n")
            if operand_stack:
                raw = _decode_operand(operand_stack[-1])
                if raw is None and isinstance(operand_stack[-1], str):
                    raw = operand_stack[-1].encode("latin-1", "ignore")
                if raw is not None:
                    text = _decode_with(current_font, raw)
                    if text:
                        pending.append(text)
        elif operator == "TJ":
            if operand_stack:
                items = operand_stack[-1]
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, bytes):
                            text = _decode_with(current_font, item)
                            if text:
                                pending.append(text)
                        elif isinstance(item, (int, float)):
                            # 负的调整量达到一定幅度近似为一个空格
                            if item < -180:
                                pending.append(" ")
        elif operator == "ET":
            flush()

        operand_stack = []

    flush()
    return "".join(pieces)


def _decode_with(font, raw):
    if font is None:
        # 没有 /Tf 就出现文本是非法的，但真文件里偶有；按 Latin-1 尽力解码
        return raw.decode("latin-1", "replace")
    return font.decode(raw)


# ---------------------------------------------------------------- 对外

def from_pdf(path, budget=0, lang="pdf", max_pages=5000):
    """抽取 PDF 文本层。返回统一结构。"""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return {"state": "failed", "text": "", "error": "无法读取文件：%s" % exc,
                "lang": lang, "units": []}

    read_limit = min(size, max(config.MAX_PDF_STREAM, 64 << 20))
    try:
        with open(path, "rb") as handle:
            data = handle.read(read_limit)
    except OSError as exc:
        return {"state": "failed", "text": "", "error": "无法读取文件：%s" % exc,
                "lang": lang, "units": []}

    if not data.lstrip().startswith(b"%PDF"):
        return {"state": "unsupported", "text": "",
                "error": "文件头不是 %PDF，可能不是有效的 PDF。", "lang": lang,
                "units": []}

    try:
        document = _Document(data)
    except Exception as exc:  # noqa: BLE001
        return {"state": "failed", "text": "",
                "error": "PDF 结构解析失败：%s" % exc, "lang": lang, "units": []}

    if document.encrypted:
        return {
            "state": "unsupported", "text": "", "lang": lang, "units": [],
            "error": "该 PDF 已加密，文本层无法读取（仍可用阅读器查看，可能需输入口令）。",
        }

    pages = document.pages(limit=max_pages)
    if not pages:
        return {"state": "unsupported", "text": "",
                "error": "未能在该 PDF 中找到页面结构（文件可能损坏）。", "lang": lang,
                "units": []}

    units = []
    chunks = []
    for index, (page, inherited) in enumerate(pages):
        content = document.content_for(page, inherited)
        if not content:
            continue
        fonts = {}
        for name, font_dict in document.fonts_for(page, inherited).items():
            try:
                fonts[name] = _Font(document, font_dict)
            except Exception:  # noqa: BLE001
                continue
        text = _extract_page_text(content, fonts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > config.MAX_PDF_PAGE_TEXT:
            text = text[:config.MAX_PDF_PAGE_TEXT]
        if text:
            units.append(("page", index + 1, "第 %d 页" % (index + 1), text))
            chunks.append("第 %d 页\n%s" % (index + 1, text))
        if sum(len(c) for c in chunks) > (budget or config.MAX_INDEX_BYTES):
            break

    full = "\n\n".join(chunks).strip()
    if not full:
        # 有页面结构却没有文本层：加密之外最常见的原因就是扫描件。
        # 绝不索引乱码 —— 直接把原因讲清楚，让用户知道该怎么办。
        details = []
        for page, inherited in pages[:20]:
            for font_dict in document.fonts_for(page, inherited).values():
                try:
                    font = _Font(document, font_dict)
                except Exception:  # noqa: BLE001
                    continue
                if not font.reliable:
                    details.append(font.subtype or "CID")
                    break
            if details:
                break
        if details:
            reason = ("该 PDF 使用了缺少 ToUnicode 映射的 CID 字体（%s），"
                      "文本无法可靠还原。为避免把乱码写进索引，本文件不作全文索引。"
                      % details[0])
        else:
            reason = ("该 PDF 没有文本层（多半是扫描件或纯图片），"
                      "因此无法全文检索。仍可用内置阅读器正常查看。")
        return {"state": "unsupported", "text": "", "lang": lang, "units": [],
                "error": reason, "pages": len(pages)}

    truncated = sum(len(c) for c in chunks) > (budget or config.MAX_INDEX_BYTES)
    return {
        "state": "ok",
        "text": full,
        "lang": lang,
        "units": units,
        "truncated": truncated,
        "pages": len(pages),
        "pages_with_text": len(units),
    }
