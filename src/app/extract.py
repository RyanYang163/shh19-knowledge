"""文本抽取调度器：编码探测、图片尺寸探测、分派到各格式抽取器。

**只用标准库。** 不 import Pillow / chardet / pdfminer，也不调用任何外部程序 ——
TOS 只预装 python3，任何额外依赖都会变成「command not found」驳回（指引 16.5 rank 13）。

抽取结果的四种状态（``files.text_state``）：

| 状态 | 含义 |
|---|---|
| ``ok``          | 抽到了文本，已写入全文索引 |
| ``empty``       | 文件可读，但没有可索引的文本（空文件、纯空白、图片） |
| ``unsupported`` | **明知抽不出来**：音视频、未知格式、加密 PDF、缺 ToUnicode 的 CID 字体 |
| ``failed``      | 抽取过程出错（损坏文件、IO 错误），``error`` 列给出可读原因 |

``unsupported`` 与 ``failed`` 分开，是为了让用户能区分「这个格式本来就不支持」
和「这个文件坏了」—— 两者的处置完全不同。
"""

import os
import struct

from . import config, scanner

#: 编码候选。**顺序不是判据** —— 下面用「打分 + 脚本特征」挑最优。
#: 顺序仍影响平手时的取舍（先出现者胜），所以把最可能的放前面。
_ENCODING_CANDIDATES = ("utf-8", "gb18030", "big5", "shift_jis", "euc-kr", "cp1252")

#: 判定「像正常文本」的字符区间。CJK 统一表意文字、假名、谚文、拉丁、常见标点
_TEXT_RANGES = (
    (0x0020, 0x007E), (0x00A0, 0x024F), (0x2000, 0x206F), (0x2190, 0x2BFF),
    (0x3000, 0x303F), (0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF),
)

#: 简体 ↔ 繁体 的**成对**特征字。只收真正不同的对，两个集合因此天然不相交。
#:
#: 为什么需要它：``gb18030`` 是超集，**任何**字节序列都能解出「看着像汉字」的结果，
#: 而且永不产生替换符。所以「解得出文本」这件事本身分不出对错 ——
#: 只有「解出来的字更像简体还是更像繁体」才能把 gb18030 与 big5 分开。
#: 这是实测踩出来的：早期版本把 Big5 与 Shift_JIS 的样本一律判成 gb18030，
#: 因为在「像文本的字符占比」这个尺度上它总能拿高分。
_SCRIPT_PAIRS = [
    ("国", "國"), ("学", "學"), ("说", "說"), ("这", "這"), ("时", "時"),
    ("会", "會"), ("发", "發"), ("电", "電"), ("车", "車"), ("东", "東"),
    ("门", "門"), ("马", "馬"), ("鸟", "鳥"), ("龙", "龍"), ("书", "書"),
    ("长", "長"), ("见", "見"), ("贝", "貝"), ("页", "頁"), ("风", "風"),
    ("飞", "飛"), ("华", "華"), ("个", "個"), ("们", "們"), ("来", "來"),
    ("对", "對"), ("开", "開"), ("还", "還"), ("没", "沒"), ("关", "關"),
    ("点", "點"), ("样", "樣"), ("应", "應"), ("该", "該"), ("让", "讓"),
    ("头", "頭"), ("问", "問"), ("间", "間"), ("无", "無"), ("与", "與"),
    ("专", "專"), ("业", "業"), ("务", "務"), ("两", "兩"), ("严", "嚴"),
    ("为", "為"), ("丽", "麗"), ("举", "舉"), ("义", "義"), ("乌", "烏"),
    ("乐", "樂"), ("乔", "喬"), ("习", "習"), ("乡", "鄉"), ("买", "買"),
    ("乱", "亂"), ("争", "爭"), ("亏", "虧"), ("云", "雲"), ("产", "產"),
    ("亲", "親"), ("亿", "億"), ("仅", "僅"), ("从", "從"), ("仓", "倉"),
    ("仪", "儀"), ("价", "價"), ("众", "眾"), ("优", "優"), ("伟", "偉"),
    ("传", "傳"), ("伤", "傷"), ("伦", "倫"), ("伪", "偽"), ("体", "體"),
    ("侠", "俠"), ("侣", "侶"), ("侦", "偵"), ("侧", "側"), ("侨", "僑"),
    ("债", "債"), ("倾", "傾"), ("偿", "償"), ("储", "儲"), ("儿", "兒"),
    ("党", "黨"), ("内", "內"), ("写", "寫"), ("军", "軍"), ("农", "農"),
    ("决", "決"), ("况", "況"), ("净", "淨"), ("准", "準"), ("划", "劃"),
    ("则", "則"), ("刚", "剛"), ("创", "創"), ("别", "別"), ("剂", "劑"),
    ("动", "動"), ("劳", "勞"), ("势", "勢"), ("区", "區"), ("医", "醫"),
    ("单", "單"), ("卖", "賣"), ("卢", "盧"), ("卫", "衛"), ("却", "卻"),
    ("厂", "廠"), ("厅", "廳"), ("历", "歷"), ("县", "縣"), ("参", "參"),
    ("双", "雙"), ("变", "變"), ("叙", "敘"), ("号", "號"), ("叹", "嘆"),
    ("后", "後"), ("团", "團"), ("园", "園"), ("围", "圍"), ("图", "圖"),
    ("团", "團"), ("场", "場"), ("坏", "壞"), ("块", "塊"), ("坚", "堅"),
    ("报", "報"), ("担", "擔"), ("拟", "擬"), ("拥", "擁"), ("择", "擇"),
    ("据", "據"), ("挂", "掛"), ("换", "換"), ("据", "據"), ("检", "檢"),
    ("权", "權"), ("钱", "錢"), ("铁", "鐵"), ("银", "銀"), ("针", "針"),
    ("钟", "鐘"), ("钢", "鋼"), ("录", "錄"), ("训", "訓"), ("记", "記"),
    ("讨", "討"), ("论", "論"), ("设", "設"), ("访", "訪"), ("证", "證"),
    ("评", "評"), ("词", "詞"), ("译", "譯"), ("试", "試"), ("话", "話"),
    ("语", "語"), ("误", "誤"), ("调", "調"), ("谈", "談"), ("请", "請"),
    ("读", "讀"), ("课", "課"), ("谁", "誰"), ("调", "調"), ("谢", "謝"),
    ("贝", "貝"), ("财", "財"), ("责", "責"), ("货", "貨"), ("质", "質"),
    ("购", "購"), ("费", "費"), ("资", "資"), ("赛", "賽"), ("赞", "讚"),
    ("军", "軍"), ("农", "農"), ("运", "運"), ("进", "進"), ("远", "遠"),
    ("连", "連"), ("迟", "遲"), ("选", "選"), ("递", "遞"), ("逻", "邏"),
    ("边", "邊"), ("达", "達"), ("过", "過"), ("还", "還"), ("适", "適"),
    ("邮", "郵"), ("郑", "鄭"), ("释", "釋"), ("针", "針"), ("钉", "釘"),
]
_SIMPLIFIED_ONLY = frozenset(s for s, t in _SCRIPT_PAIRS if s != t)
_TRADITIONAL_ONLY = frozenset(t for s, t in _SCRIPT_PAIRS if s != t)


def _looks_like_text(char):
    code = ord(char)
    if char in "\t\n\r":
        return True
    for low, high in _TEXT_RANGES:
        if low <= code <= high:
            return True
    return False


def _base_score(text):
    """「像正常文本的字符占比」减去替换符惩罚。"""
    if not text:
        return 0.0
    sample = text[:20000]
    good = sum(1 for ch in sample if _looks_like_text(ch))
    bad = sample.count("�")
    return (good - bad * 5) / float(len(sample) or 1)


#: 日文/韩文的「脚本密度」阈值与**绝对数量**门槛。实测（见 tests/test_encoding.py）：
#:   真实日文假名密度 ≈ 0.77、真实韩文谚文密度 ≈ 0.78；
#:   而误判在**大样本**上最多到 0.22，在**小样本**上却能到 0.44 ——
#:   只有 38 字节的中文小文件被判成 euc-kr 就是这么来的。
#: 所以光看密度不够，还要**绝对数量**：
#: 真实日/韩文文档有上百个假名/谚文，几十字节的误判凑不出 20 个。
#: 中文是这个应用的主要目标市场，所以在证据不足时一律偏向中文编码。
_FOREIGN_SCRIPT_MIN_DENSITY = 0.5
_FOREIGN_SCRIPT_MIN_COUNT = 20


def _density(sample, low, high):
    if not sample:
        return 0.0
    return sum(1 for ch in sample if low <= ord(ch) <= high) / float(len(sample))


def _count_in(sample, low, high):
    return sum(1 for ch in sample if low <= ord(ch) <= high)


def _script_bonus(text, encoding):
    """按编码**应有的文字系统**给分。

    这是区分 gb18030 / big5 / shift_jis / euc-kr 的**唯一**可靠依据 ——
    它们都能把对方的字节解成「看着像文字」的结果，只有文字系统不同。
    """
    sample = text[:20000]
    if not sample:
        return 0.0
    total = float(len(sample))

    if encoding in ("gb18030", "big5"):
        simplified = sum(1 for ch in sample if ch in _SIMPLIFIED_ONLY)
        traditional = sum(1 for ch in sample if ch in _TRADITIONAL_ONLY)
        # 中文文本里出现假名/谚文 = 解错了
        foreign = _density(sample, 0x3040, 0x30FF) + _density(sample, 0xAC00, 0xD7AF)
        if encoding == "gb18030":
            return (simplified - traditional) * 2.0 / total - foreign * 6.0
        return (traditional - simplified) * 2.0 / total - foreign * 6.0

    if encoding == "shift_jis":
        low, high = 0x3040, 0x30FF
    elif encoding == "euc-kr":
        low, high = 0xAC00, 0xD7AF
    else:
        # 单字节编码（cp1252）不加成，免得把 CJK 的候选抢走
        return 0.0

    density = _density(sample, low, high)
    count = _count_in(sample, low, high)
    if density >= _FOREIGN_SCRIPT_MIN_DENSITY and count >= _FOREIGN_SCRIPT_MIN_COUNT:
        return 4.0 * density
    # 证据不足：给惩罚而不是给分，让中文编码候选胜出
    return -1.0


def _guess_utf16(data):
    """无 BOM 的 UTF-16 靠**空字节的奇偶分布**识别。

    Windows 记事本存「Unicode」不写 BOM，而 ASCII 正文在 UTF-16LE 下
    奇数位几乎全是 0x00 —— 这个特征比任何统计打分都可靠。
    """
    sample = data[:8192]
    if len(sample) < 8:
        return None
    pairs = len(sample) // 2
    even_nul = sample[0::2].count(0)
    odd_nul = sample[1::2].count(0)
    even_ratio = even_nul / float(pairs or 1)
    odd_ratio = odd_nul / float(pairs or 1)
    if odd_ratio > 0.6 and even_ratio < 0.1:
        return "utf-16-le"
    if even_ratio > 0.6 and odd_ratio < 0.1:
        return "utf-16-be"
    return None


def detect_encoding(data):
    """猜测字节串的编码。返回 ``(encoding, text)``。

    判定顺序（每一步都比下一步可靠）：

    1. **BOM** —— 最确定，直接采信。
    2. **无 BOM 的 UTF-16** —— 按空字节奇偶分布识别。
    3. **UTF-8 严格解码** —— UTF-8 有强校验约束：GBK/Big5 的字节串几乎必然
       无法通过严格解码。所以「严格可解且文本合理」就足以采信，这一步把
       绝大多数情况（含所有现代中文文档）一次定下来。
    4. **打分比选** —— 剩下的是 GBK/Big5/Shift_JIS 这类双字节同族编码，
       靠**简繁特征字分布**分辨（见 ``_script_bonus``）。

    都不行时用 ``utf-8 errors='replace'`` 收尾：宁可留几个替换符，
    也不能抛异常 —— 否则一个编码怪异的日志就能让整个索引任务失败。
    """
    if not data:
        return "utf-8", ""

    for bom, name in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if data.startswith(bom):
            try:
                text = data.decode(name)
            except (UnicodeDecodeError, LookupError):
                break
            # utf-8-sig 会自动吃掉 BOM，utf-16-le/be 不会 —— 手工剥掉，
            # 否则每篇文档的第一个字符都是一个看不见的 U+FEFF，会污染索引与片段
            if text[:1] == "﻿":
                text = text[1:]
            return name, text

    # 无 BOM 的 UTF-16：先按空字节奇偶分布（ASCII 正文的特征）
    guessed = _guess_utf16(data)
    if guessed:
        try:
            return guessed, data.decode(guessed)
        except UnicodeDecodeError:
            pass

    # UTF-8 严格可解就采信 —— 校验约束本身就足以排除 GBK/Big5
    utf8_text = None
    try:
        strict = data.decode("utf-8")
        utf8_text = strict
        if _base_score(strict) >= 0.75:
            # 但 UTF-16 的**中文**正文（无 BOM）也能通过 UTF-8 严格校验，
            # 因为两字节码位常常巧合地构成合法序列。中文在 UTF-16 里解出来会得到
            # 大量汉字，而误解成 UTF-8 只会得到零星控制符 —— 用这个差距反过来纠正。
            better = _prefer_utf16(data, strict)
            if better:
                return better
            return "utf-8", strict
    except UnicodeDecodeError:
        pass

    best_name, best_text, best_score = None, None, None
    for name in _ENCODING_CANDIDATES:
        if name == "utf-8":
            continue           # 上面已判过（严格解码失败），不再用宽松形式参与比选
        try:
            text = data.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _base_score(text) + _script_bonus(text, name)
        if best_score is None or score > best_score:
            best_name, best_text, best_score = name, text, score

    if best_text is None:
        return "utf-8", (utf8_text if utf8_text is not None
                         else data.decode("utf-8", "replace"))
    return best_name, best_text


def _cjk_ratio(text):
    sample = text[:20000]
    if not sample:
        return 0.0
    cjk = sum(1 for ch in sample if "㐀" <= ch <= "鿿")
    return cjk / float(len(sample))


def _prefer_utf16(data, utf8_text):
    """偶数长度时比较 UTF-16 与 UTF-8 两种解法的可信度，差距明显才改判。

    ⚠️ 判定必须**双向设防**，早期版本只奖励「汉字密度」，结果把纯 ASCII 英文
    判成了 UTF-16BE —— 因为两个 ASCII 字节会拼成一个 CJK 码位
    （``"Th"`` → 0x5468 → 「周」），于是 ASCII 文本被误读成 UTF-16 时
    「汉字密度」接近 100%，反而拿到最高分。

    真实文本的判据是**混合性**：任何自然语言的正文都含有空格、标点或 ASCII 字符。
    而被误读出来的假文本恰恰是「几乎纯汉字、几乎没有 ASCII」。
    """
    if len(data) % 2 or len(data) < 64:
        return None
    # UTF-8 解得很干净（例如纯 ASCII）时不该被推翻 —— 那是毫无歧义的情况
    if _base_score(utf8_text) >= 0.98:
        return None

    baseline = _base_score(utf8_text)
    ascii_bytes = sum(1 for byte in data[:8192] if byte < 0x80)
    best = None
    for name in ("utf-16-le", "utf-16-be"):
        try:
            candidate = data.decode(name)
        except UnicodeDecodeError:
            continue
        sample = candidate[:8000]
        if not sample or "�" in sample:
            continue
        ascii_chars = sum(1 for ch in sample if ch < "\x80") / float(len(sample))
        cjk = _cjk_ratio(sample)
        # 真实文本：汉字与 ASCII/标点混合；误读文本：汉字占比极高且几乎无 ASCII
        if ascii_chars < 0.02 or cjk > 0.92:
            continue
        score = _base_score(candidate) + cjk * 0.8
        if best is None or score > best[0]:
            best = (score, name, candidate)

    # 差距要求大，避免把正常的 UTF-8 文本误判成 UTF-16
    if best and best[0] > baseline + 0.6:
        return best[1], best[2]
    return None


def read_text_file(path, budget):
    """读文本文件，返回 ``(text, encoding, truncated)``。超预算只读前 ``budget`` 字节。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", "utf-8", False
    limit = min(size, max(budget, 4096))
    with open(path, "rb") as handle:
        data = handle.read(limit)
    encoding, text = detect_encoding(data)
    return text, encoding, size > limit


# ---------------------------------------------------------------- 图片尺寸

def probe_image_size(path, head_bytes=262144):
    """只读文件头判图片宽高（**不做全量解码**）。

    扫描/索引十万张照片时，全量解码会慢到不可用；而它们只有几个像素级字段有价值。
    认不出的格式返回 ``(0, 0)`` —— 前端仍能用浏览器原生 ``<img>`` 正常显示。
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(head_bytes)
    except OSError:
        return 0, 0
    if len(head) < 16:
        return 0, 0

    # PNG：IHDR 固定在偏移 16
    if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
        width, height = struct.unpack(">II", head[16:24])
        return int(width), int(height)

    # GIF：逻辑屏幕描述符
    if head[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", head[6:10])
        return int(width), int(height)

    # BMP
    if head[:2] == b"BM":
        width, height = struct.unpack("<ii", head[18:26])
        return abs(int(width)), abs(int(height))

    # WebP（VP8 / VP8L / VP8X 三种块）
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        chunk = head[12:16]
        try:
            if chunk == b"VP8X":
                width = int.from_bytes(head[24:27], "little") + 1
                height = int.from_bytes(head[27:30], "little") + 1
                return width, height
            if chunk == b"VP8 ":
                width = int.from_bytes(head[26:28], "little") & 0x3FFF
                height = int.from_bytes(head[28:30], "little") & 0x3FFF
                return width, height
            if chunk == b"VP8L":
                bits = int.from_bytes(head[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        except (IndexError, ValueError):
            return 0, 0

    # JPEG：走框架里的 SOF 段解析（比全量解码廉价得多）
    if head[:2] == b"\xff\xd8":
        from tnasapp.imagedec import jpeg_dimensions

        width, height = jpeg_dimensions(head)
        return int(width or 0), int(height or 0)

    return 0, 0


# ---------------------------------------------------------------- 分派

def extractor_for(kind):
    """按 kind 选抽取器。返回 ``None`` 表示这个格式不做抽取。"""
    from . import extract_office, extract_pdf, extract_text

    return {
        "text": extract_text.from_text,
        "code": extract_text.from_text,
        "markdown": extract_text.from_markdown,
        "html": extract_text.from_html,
        "csv": extract_text.from_csv,
        "json": extract_text.from_json,
        "xml": extract_text.from_xml,
        "yaml": extract_text.from_yaml,
        "docx": extract_office.from_docx,
        "xlsx": extract_office.from_xlsx,
        "pptx": extract_office.from_pptx,
        "pdf": extract_pdf.from_pdf,
    }.get(kind)


def extract_file(app, path, kind, name=None):
    """抽取一个文件。返回统一结构，**不抛异常**（异常由调用方按 ``failed`` 处理）。"""
    budget = int(app.settings.get("max_index_bytes") or config.MAX_INDEX_BYTES)
    language = scanner.lang_for(name or path)

    if kind in ("image",):
        width, height = probe_image_size(path)
        return {"state": "empty", "text": "", "lang": "", "units": [],
                "chars": 0, "truncated": False, "image_w": width,
                "image_h": height, "error": None}

    if kind in ("audio", "video", "other"):
        return {"state": "unsupported", "text": "", "lang": "", "units": [],
                "chars": 0, "truncated": False, "image_w": 0, "image_h": 0,
                "error": None}

    extractor = extractor_for(kind)
    if extractor is None:
        return {"state": "unsupported", "text": "", "lang": "", "units": [],
                "chars": 0, "truncated": False, "image_w": 0, "image_h": 0,
                "error": None}

    result = extractor(path, budget=budget, lang=language)
    text = (result.get("text") or "").strip()
    state = result.get("state")
    if state is None:
        state = "ok" if text else "empty"
    return {
        "state": state,
        "text": text,
        "lang": result.get("lang") or language,
        "encoding": result.get("encoding") or "",
        "units": result.get("units") or [],
        "chars": len(text),
        "truncated": bool(result.get("truncated")),
        "image_w": result.get("image_w") or 0,
        "image_h": result.get("image_h") or 0,
        "error": result.get("error"),
    }


def extract_and_store(app, row):
    """抽取并写入 ``file_text`` / ``doc_units`` / FTS。返回给扫描器的结果字典。

    写入走 ``db.text_put``（``ON CONFLICT DO UPDATE``），由触发器同步 FTS ——
    绝不能用 ``INSERT OR REPLACE``，那会让索引残留旧词（见 ``db`` 模块说明）。
    """
    from . import db

    path = row["path"]
    name = row["name"]
    kind = row["kind"]

    # 先清掉上一轮可能留下的 trigram / 单元行，避免重抽后残留「幽灵命中」
    db.fts_drop_file(app, row["id"])

    result = extract_file(app, path, kind, name=name)
    state = result["state"]

    if state in ("ok", "empty"):
        db.text_put(app, row["id"], name, row["rel_path"], result["text"],
                    lang=result["lang"], encoding=result.get("encoding") or "",
                    truncated=1 if result["truncated"] else 0, error=None)
    else:
        # unsupported / failed：清掉文本，但把原因写进 file_text.error 以便界面上说明
        db.text_drop(app, row["id"])

    units = result.get("units") or []
    if units:
        db.units_replace(app, row["id"], units)
        stored = db.units_for(app, row["id"])
        db.fts_index_units(app, [(int(u["id"]), u["content"]) for u in stored])
    else:
        db.units_replace(app, row["id"], [])

    if state in ("ok", "empty") and db.trigram_ready(app) and result["text"]:
        # trigram 索引覆盖「≥3 字符的子串」与长 CJK 串内部的中文词，
        # 这是 unicode61 做不到的部分（它把连续 CJK 当一个整词）。
        app.store.execute("DELETE FROM fts_tri WHERE rowid=?", (int(row["id"]),))
        app.store.execute(
            "INSERT INTO fts_tri(rowid, name, path, content) VALUES (?,?,?,?)",
            (int(row["id"]), name, row["rel_path"], result["text"]),
        )

    return {
        "state": state,
        "chars": result["chars"],
        "units": len(units),
        "image_w": result["image_w"],
        "image_h": result["image_h"],
        "error": result.get("error"),
    }
