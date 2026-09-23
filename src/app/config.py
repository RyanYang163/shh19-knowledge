"""常量与默认值。

**只用 Python 3.10 标准库**（指引 16.5 rank 13：依赖未预装会被判「command not found」；
TOS 7 只预装 python3 / nginx / systemd）。所以本应用不 import 任何 pip 包，
也不调用任何外部程序 —— Office 与 PDF 的解析都在应用内自己实现。
"""

APP_ID = "shh19-knowledge"
APP_VERSION = "1.0.0"
TITLE = "知识库阅读器"

# ---- 扫描上限（都是有界增长所必需的，README 的运行时文件清单里逐条对应）----

#: 目录递归深度上限。防止有人把 / 加进白名单后把整台机器扫进索引
MAX_DEPTH = 32
#: 单库收录文件数上限。达到后停止收录并在任务日志里说明原因，绝不静默截断
MAX_FILES_PER_KB = 300_000
#: 单文件最多索引前多少字节的文本。超出的部分置 truncated 标记
MAX_INDEX_BYTES = 1 << 20
#: 单次 executemany 的行数。Store 每次调用都新开一条连接，逐行写会慢到不可用
SCAN_BATCH = 500
#: 单个 Office XML part / 解压前允许的最大体积（防 zip 炸弹）
MAX_PART_BYTES = 8 << 20
#: 表格类查看器单次返回的最大行数
MAX_SHEET_ROWS = 5000
#: /api/file/text 单次返回的字节上限
MAX_TEXT_PAGE = 256 * 1024
#: 自定义图标上传上限
MAX_ICON_UPLOAD = 256 * 1024
#: 单次 Range 响应的字节上限（防止一个请求把大视频拖进内存）
RANGE_CHUNK = 4 << 20
#: JSON 查看器允许结构化解析的体积上限，超过则退化为纯文本
MAX_JSON_PARSE = 4 << 20
#: PDF 内容流解压后的硬上限（防解压炸弹）
MAX_PDF_STREAM = 32 << 20
#: PDF 单页文本上限
MAX_PDF_PAGE_TEXT = 64 << 10

#: 扩展名 → kind。kind 决定用哪个查看器与哪个提取器。
#: 未列出的扩展名一律 'other'，查看器会给出「不支持预览 + 下载」而不是空白面板。
EXT_KIND = {
    ".pdf": "pdf",
    ".md": "markdown", ".markdown": "markdown", ".mdx": "markdown",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".txt": "text", ".log": "text", ".ini": "text", ".cfg": "text",
    ".conf": "text", ".env": "text", ".toml": "text", ".rst": "text",
    ".csv": "csv", ".tsv": "csv",
    ".json": "json", ".jsonl": "json", ".ndjson": "json", ".geojson": "json",
    ".xml": "xml", ".xsl": "xml", ".xsd": "xml", ".plist": "xml",
    ".yaml": "yaml", ".yml": "yaml",
    ".docx": "docx", ".docm": "docx",
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".pptx": "pptx", ".pptm": "pptx",
    ".py": "code", ".pyi": "code", ".js": "code", ".mjs": "code", ".cjs": "code",
    ".ts": "code", ".tsx": "code", ".jsx": "code", ".go": "code", ".rs": "code",
    ".java": "code", ".kt": "code", ".c": "code", ".h": "code", ".cpp": "code",
    ".hpp": "code", ".cc": "code", ".cs": "code", ".rb": "code", ".php": "code",
    ".sh": "code", ".bash": "code", ".zsh": "code", ".ps1": "code", ".bat": "code",
    ".sql": "code", ".css": "code", ".scss": "code", ".less": "code",
    ".vue": "code", ".svelte": "code", ".lua": "code", ".pl": "code",
    ".r": "code", ".swift": "code", ".m": "code", ".scala": "code",
    ".dockerfile": "code", ".makefile": "code", ".cmake": "code", ".gradle": "code",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".ico": "image", ".svg": "image",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".m4a": "audio",
    ".aac": "audio", ".ogg": "audio", ".oga": "audio", ".opus": "audio",
    ".wma": "audio",
    ".mp4": "video", ".webm": "video", ".mkv": "video", ".mov": "video",
    ".m4v": "video", ".avi": "video", ".wmv": "video", ".flv": "video",
}

#: kind → 语言标识，供前端语法高亮选择（前端用轻量 tokenizer，不是完整解析器）
LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java",
    ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".cc": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".sh": "bash",
    ".bash": "bash", ".zsh": "bash", ".ps1": "powershell", ".bat": "batch",
    ".sql": "sql", ".css": "css", ".scss": "scss", ".less": "less",
    ".vue": "vue", ".svelte": "svelte", ".lua": "lua", ".pl": "perl",
    ".r": "r", ".swift": "swift", ".scala": "scala", ".json": "json",
    ".xml": "xml", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".env": "ini",
    ".md": "markdown", ".markdown": "markdown", ".rst": "rst",
    ".html": "html", ".htm": "html", ".css": "css", ".log": "log",
}

#: 扫描时默认跳过的目录名。这些是回收站 / 版本控制 / 构建缓存，
#: 扫进去只会把索引塞满垃圾。用户可在设置里增删。
DEFAULT_EXCLUDES = [
    ".git", ".svn", ".hg", ".bzr",
    "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "target", "dist", "build",
    "@eaDir", "#recycle", "$RECYCLE.BIN", ".Trash", ".Trashes", ".Spotlight-V100",
    "System Volume Information", "lost+found",
]

#: 内置图标优先级：自定义 > 本表 > 系统默认。名字必须都存在于前端 Icons.names()
ICON_BY_KIND = {
    "pdf": "fileText", "markdown": "fileText", "html": "globe", "text": "fileText",
    "csv": "chart", "json": "type", "xml": "type", "yaml": "type",
    "docx": "fileText", "xlsx": "chart", "pptx": "layers",
    "code": "terminal", "image": "image", "audio": "music", "video": "video",
    "other": "file",
}

#: App.default_settings() 的返回值。放进 SQLite 的只有索引数据，偏好走 runtime.json。
DEFAULT_SETTINGS = {
    "auth_mode": "lenient",
    "debug": False,
    "theme": "system",             # system | light | dark
    "reader_width": "normal",      # normal | wide | full
    "index_mode": "fast",          # fast（unicode61）| thorough（+ trigram 子串索引）
    "max_index_bytes": MAX_INDEX_BYTES,
    "auto_scan": "off",            # off | daily | weekly —— 由访问时的机会式检查驱动，无后台常驻线程
    "exclude_dirs": DEFAULT_EXCLUDES,
    "tree_width": 288,
    "info_width": 320,
    "info_visible": True,
}

#: 供 /api/status 上报、方便售后一眼看出哪些能力在当前机器上不可用
DEGRADABLE = ("fts5", "trigram")
