"""自定义图标的落盘与读取。

上传的图标是**不可信输入**，所以：

1. **按内容嗅探判类型**，不信客户端声明的 ``Content-Type`` 或扩展名；
2. SVG 走 :func:`security.sanitize_svg` 逐标签白名单清洗（丢弃 ``script`` /
   ``on*`` / ``foreignObject`` / ``javascript:`` 等）；
3. 回给浏览器时带 ``Content-Security-Policy: default-src 'none'; sandbox``，
   即便清洗漏了什么也执行不了 —— 这是可以省事但不能省的一层。

落盘位置在 ``data/cache/icons/``。为什么放 cache 而不是别处：图标可以随时重新上传，
丢了不心疼；而 ``data/cache`` 已在 README 的运行时文件清单里声明为可清理目录。
数据库里只存**文件名**，不存绝对路径 —— 数据目录可能因为卷挂载点变化而改变。
"""

import os
import time

from . import config, security


def icon_dir(app):
    path = os.path.join(app.paths.cache_dir, "icons")
    os.makedirs(path, exist_ok=True)
    return path


def _icon_path(app, file_name):
    """把数据库里的文件名解析成绝对路径，并确保它没跑出 icons 目录。"""
    safe = os.path.basename(str(file_name or ""))
    if not safe or safe.startswith("."):
        return None
    full = os.path.realpath(os.path.join(icon_dir(app), safe))
    root = os.path.realpath(icon_dir(app))
    if not full.startswith(root + os.sep):
        return None
    return full


def save_icon(app, kb_id, raw, target_path=None, target_type="file"):
    """保存一个上传的图标。返回 ``{ok, icon_type, icon_value}``（``icon_value`` 为文件名）。"""
    import base64

    if isinstance(raw, str):
        try:
            data = base64.b64decode(raw, validate=False)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "图标数据不是合法的 base64。"}
    else:
        data = bytes(raw or b"")

    if not data:
        return {"ok": False, "error": "图标内容为空。"}
    if len(data) > config.MAX_ICON_UPLOAD:
        return {"ok": False,
                "error": "图标超过 %d KB 上限。" % (config.MAX_ICON_UPLOAD // 1024)}

    if security.sniff_svg(data):
        cleaned = security.sanitize_svg(data, max_bytes=config.MAX_ICON_UPLOAD)
        if not cleaned:
            return {"ok": False,
                    "error": "这个 SVG 无法通过安全清洗（可能含脚本或外部引用），已拒绝。"}
        payload, ext = cleaned.encode("utf-8"), "svg"
    else:
        kind = security.sniff_image(data)
        if kind is None:
            return {"ok": False,
                    "error": "只接受 SVG / PNG / JPEG / GIF / WebP / BMP 图标。"}
        payload, ext = data, ("jpg" if kind == "jpeg" else kind)

    file_name = "icon-%d-%d.%s" % (int(time.time() * 1000), os.getpid(), ext)
    full = os.path.join(icon_dir(app), file_name)
    tmp = full + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(payload)
    os.replace(tmp, full)

    app.log.info("自定义图标已保存：%s（%d 字节，%s）", file_name, len(payload), ext)
    return {"ok": True, "icon_type": "upload", "icon_value": file_name,
            "bytes": len(payload), "format": ext}


def read_icon(app, file_name):
    """读取图标字节。返回 ``(bytes, content_type)`` 或 ``(None, None)``。"""
    full = _icon_path(app, file_name)
    if not full or not os.path.isfile(full):
        return None, None
    try:
        with open(full, "rb") as handle:
            data = handle.read(config.MAX_ICON_UPLOAD + 1)
    except OSError:
        return None, None
    if len(data) > config.MAX_ICON_UPLOAD:
        return None, None
    ext = os.path.splitext(full)[1].lower()
    ctype = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
             ".bmp": "image/bmp"}.get(ext, "application/octet-stream")
    return data, ctype


def prune_orphans(app, keep_names):
    """删掉不再被任何记录引用的图标文件（有界增长）。"""
    keep = {str(name) for name in keep_names if name}
    removed = 0
    root = icon_dir(app)
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        if name in keep or name.endswith(".tmp"):
            if name.endswith(".tmp"):
                try:
                    os.remove(os.path.join(root, name))
                    removed += 1
                except OSError:
                    pass
            continue
        try:
            os.remove(os.path.join(root, name))
            removed += 1
        except OSError:
            continue
    return removed


def builtin_icons():
    """内置图标表，供设置界面选择。

    名字必须都存在于前端 ``Icons.names()`` —— 这里的图标是**自绘 SVG**，
    不是 emoji。用 emoji 会引入字体依赖，在部分 TOS 版本上渲染不一致，
    而且设计文档 §51 明确要求不要复制其它产品的视觉素材。
    """
    return [
        {"value": "book", "label": "知识库"},
        {"value": "folder", "label": "文件夹"},
        {"value": "folderOpen", "label": "打开的文件夹"},
        {"value": "file", "label": "文件"},
        {"value": "fileText", "label": "文档"},
        {"value": "type", "label": "文本 / 结构化"},
        {"value": "chart", "label": "表格"},
        {"value": "layers", "label": "演示文稿"},
        {"value": "image", "label": "图片"},
        {"value": "music", "label": "音频"},
        {"value": "video", "label": "视频"},
        {"value": "terminal", "label": "代码"},
        {"value": "globe", "label": "网页"},
        {"value": "tag", "label": "标记"},
        {"value": "clock", "label": "时间"},
        {"value": "archive", "label": "归档"},
    ]
