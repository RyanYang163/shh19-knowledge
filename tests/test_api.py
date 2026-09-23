"""端到端 API 测试：真实起服务、真实建库扫描、真实走 HTTP。

参照 ``shh11-media-audio/tests/test_api.py`` 的做法：
``AppPaths`` 显式指定临时数据目录 → **在发出第一个请求之前**设好白名单
（顺序反了会得到一个「白名单为空」的 403，而不是真正要测的东西）→
``app.run(host, port=0, background=True)`` 起真实 HTTP 服务 → 用 ``urllib`` 打。

跑法：``python -m unittest discover -s tests -t .``
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)
sys.path.insert(
    0, os.path.dirname(os.path.abspath(__file__)),
)

import synth  # noqa: E402
from tnasapp.paths import AppPaths  # noqa: E402

from app import config, main as appmain  # noqa: E402

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ApiCase(unittest.TestCase):
    """公共夹具：一个装好样本文件的知识库 + 一个跑着的后端。"""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="shh19-api-")
        cls.docs = os.path.join(cls.root, "Docs")
        cls.other = os.path.join(cls.root, "Other")
        os.makedirs(cls.docs, exist_ok=True)
        os.makedirs(cls.other, exist_ok=True)
        cls._populate()

        cls.paths = AppPaths(config.APP_ID, install_dir=APP_DIR,
                             data_dir=os.path.join(cls.root, "data"),
                             webui_dir=os.path.join(APP_DIR, "webui"))
        cls.app = appmain.create_app(paths=cls.paths, log_level="ERROR")
        # 白名单必须在第一个请求之前设好
        cls.app.set_allowed_roots([cls.docs])
        cls.server = cls.app.run(host="127.0.0.1", port=0, background=True)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.kb_id = cls._create_and_scan()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.shutdown()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _populate(cls):
        synth.make_tree(cls.docs, {
            "guide.md": "# TOS 7 指南\n\n## 升级前准备\n\n请先升级 BIOS，"
                        "然后检查硬盘兼容性。\n\n## 常见问题\n\n升级失败时请查看日志。\n",
            "notes/readme.txt": "关于固件升级的说明：升级前务必备份数据。\n",
            "notes/中文 文件.txt": "中文文件名测试，包含 BIOS 与固件两个关键词。\n",
            "数据.csv": "名称,数量\n硬盘,4\n内存,8\n",
            "empty.txt": "",
            "sub/deep/nested.md": "# 深层文档\n\n这是一份很深的文档。\n",
        })
        with open(os.path.join(cls.docs, "report.pdf"), "wb") as fh:
            fh.write(synth.make_pdf(2, page_texts=[
                "BIOS upgrade requirements page one",
                "firmware update checklist page two"]))
        with open(os.path.join(cls.docs, "sheet.xlsx"), "wb") as fh:
            fh.write(synth.make_xlsx(sheets=[("清单", [["名称", "数量"],
                                                       ["硬盘", 4], ["内存", 8]])]))
        with open(os.path.join(cls.docs, "deck.pptx"), "wb") as fh:
            fh.write(synth.make_pptx(["固件升级流程", "注意事项"]))
        with open(os.path.join(cls.docs, "doc.docx"), "wb") as fh:
            fh.write(synth.make_docx(["第一段正文", "第二段正文", "版本号 v1.2.3"]))
        with open(os.path.join(cls.docs, "img.png"), "wb") as fh:
            fh.write(synth.make_png(16, 12))
        with open(os.path.join(cls.docs, "gbk.txt"), "wb") as fh:
            fh.write("GB18030 编码的中文内容，关键词：固件。".encode("gb18030"))
        with open(os.path.join(cls.other, "secret.txt"), "w", encoding="utf-8") as fh:
            fh.write("白名单之外的文件，绝不应被读到。\n")

    # ---- HTTP ----

    def call(self, method, path, body=None):
        url = self.base + path
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=payload, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return response.status, self._decode(raw), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, self._decode(exc.read()), dict(exc.headers)

    @staticmethod
    def _decode(raw):
        if raw[:1] in (b"{", b"["):
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                return raw
        return raw

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body):
        return self.call("POST", path, body)

    def q(self, path):
        return "/api/file/" + path

    def url_for(self, endpoint, doc_path, extra=""):
        return "%s?path=%s%s" % (endpoint, urllib.parse.quote(doc_path), extra)

    # ---- 夹具 ----

    @classmethod
    def _create_and_scan(cls):
        status, payload, _ = cls._call_class(
            "POST", "/api/kb/create",
            {"name": "测试库", "root_path": cls.docs})
        assert status == 201, payload
        kb_id = payload["kb"]["id"]
        cls._wait_idle()
        return kb_id

    @classmethod
    def _call_class(cls, method, path, body=None):
        url = cls.base + path
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=payload, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw), None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw), None
            except ValueError:
                return exc.code, raw, None

    @classmethod
    def _wait_idle(cls, timeout=90):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            _status, payload, _ = cls._call_class("GET", "/api/jobs?limit=10")
            jobs = payload.get("jobs") or []
            active = [job for job in jobs if job.get("is_active")]
            if not active:
                finished = [job for job in jobs if job["type"] == "scan"]
                last = finished[0] if finished else None
                break
            time.sleep(0.3)
        return last


class TestFrameworkAndStatus(ApiCase):
    def test_health(self):
        status, payload, _ = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["app"], config.APP_ID)

    def test_app_info_lists_job_types(self):
        status, payload, _ = self.get("/api/app")
        self.assertEqual(status, 200)
        self.assertIn("scan", payload["job_types"])
        self.assertIn("index", payload["job_types"])

    def test_status_reports_capabilities(self):
        status, payload, _ = self.get("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("fts5", payload)
        self.assertTrue(payload["sqlite_version"])
        self.assertIsInstance(payload["degraded_features"], list)

    def test_prefix_compatibility(self):
        """平台会以 ``/``、``/<appid>/``、``/v2/proxy/<appid>/`` 三种前缀访问。"""
        for prefix in ("", "/" + config.APP_ID, "/v2/proxy/" + config.APP_ID):
            status, payload, _ = self.get(prefix + "/api/status")
            self.assertEqual(status, 200, "前缀 %r 失败" % prefix)
            self.assertTrue(payload["ok"])


class TestIndexPage(ApiCase):
    def test_index_html_present_and_no_absolute_assets(self):
        """审核项 F7：反代前缀下绝对路径会白屏，必须全部相对路径。"""
        status, raw, _ = self.get("/")
        self.assertEqual(status, 200)
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        self.assertIn("knowledge.js", text)
        # 关键守卫：不能出现 src="/ 或 href="/
        self.assertNotIn('src="/', text)
        self.assertNotIn("href=\"/", text)

    def test_spa_fallback_for_unknown_page(self):
        status, raw, _ = self.get("/some/deep/page")
        self.assertEqual(status, 200)


class TestKnowledgeBases(ApiCase):
    def test_list_contains_created_kb(self):
        status, payload, _ = self.get("/api/kb/list")
        self.assertEqual(status, 200)
        ids = [kb["id"] for kb in payload["knowledge_bases"]]
        self.assertIn(self.kb_id, ids)

    def test_scan_indexed_files(self):
        status, payload, _ = self.get("/api/kb/%d/stats" % self.kb_id)
        self.assertEqual(status, 200)
        stats = payload["stats"]
        self.assertGreaterEqual(stats["file_count"], 10)
        self.assertGreater(stats["total_bytes"], 0)
        self.assertGreater(stats["text_states"].get("ok", 0), 5)

    def test_tree_root_and_subdir(self):
        status, payload, _ = self.get("/api/kb/%d/tree" % self.kb_id)
        self.assertEqual(status, 200)
        names = [entry["name"] for entry in payload["entries"]]
        self.assertIn("guide.md", names)
        self.assertIn("notes", names)
        notes = next(e for e in payload["entries"] if e["name"] == "notes")
        self.assertTrue(notes["is_dir"])

        status, sub, _ = self.get("/api/kb/%d/tree?parent=notes" % self.kb_id)
        self.assertEqual(status, 200)
        sub_names = [entry["name"] for entry in sub["entries"]]
        self.assertIn("readme.txt", sub_names)
        self.assertIn("中文 文件.txt", sub_names)
        self.assertEqual(sub["breadcrumb"][0]["name"], "notes")

    def test_nested_kb_rejected(self):
        """互相嵌套的知识库根会让同一个文件被收录两次。"""
        status, payload, _ = self.post(
            "/api/kb/create", {"name": "嵌套", "root_path": os.path.join(self.docs, "notes")})
        self.assertEqual(status, 409)
        self.assertIn("互相包含", payload["error"])

    def test_delete_requires_exact_name(self):
        status, payload, _ = self.post("/api/kb/create",
                                       {"name": "待删库", "root_path": self.other})
        kb_id = payload["kb"]["id"]
        status, payload, _ = self.post("/api/kb/%d/delete" % kb_id, {"confirm": "错名字"})
        self.assertEqual(status, 400)
        status, payload, _ = self.post("/api/kb/%d/delete" % kb_id, {"confirm": "待删库"})
        self.assertEqual(status, 200)
        # 注销只删索引，源文件必须还在
        self.assertTrue(os.path.isfile(os.path.join(self.other, "secret.txt")))
        self.assertIn("未被修改", payload["result"]["note"])


class TestSearch(ApiCase):
    def search(self, query, extra=""):
        return self.get("/api/search?q=%s%s" % (urllib.parse.quote(query), extra))

    def test_latin_word(self):
        status, payload, _ = self.search("BIOS")
        self.assertEqual(status, 200)
        self.assertGreater(payload["total"], 0)
        names = [r["name"] for r in payload["results"]]
        self.assertTrue(any("guide" in n or "report" in n or "文件" in n for n in names))

    def test_cjk_term(self):
        status, payload, _ = self.search("固件")
        self.assertEqual(status, 200)
        self.assertGreater(payload["total"], 0)

    def test_prefix_query(self):
        status, payload, _ = self.search("firmw")
        self.assertEqual(status, 200)
        self.assertGreater(payload["total"], 0)

    def test_two_char_cjk_degrades_honestly(self):
        """1–2 字中文必须如实标降级，而不是假装全文检索成功。"""
        status, payload, _ = self.search("硬盘")
        self.assertEqual(status, 200)
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["engine"], "like")
        self.assertTrue(payload["hint"])

    def test_pdf_page_locating(self):
        status, payload, _ = self.search("firmware")
        self.assertEqual(status, 200)
        hits = [r for r in payload["results"] if r.get("unit")]
        self.assertTrue(hits, "应至少有一条结果带页码定位")
        self.assertEqual(hits[0]["unit"]["type"], "page")

    def test_filters(self):
        status, payload, _ = self.search("BIOS", "&kind=markdown")
        self.assertEqual(status, 200)
        for result in payload["results"]:
            self.assertEqual(result["kind"], "markdown")

    def test_evil_queries_never_500(self):
        """FTS5 有自己的一套语法，任何输入都必须返回 200 而不是 500。"""
        for query in ['"', "*", "NEAR", "-", "^", "OR OR", "AND", "(", ")",
                      "a" * 4000, "   ", "\t", "\\", "字段:", "a:b:c"]:
            status, payload, _ = self.search(query)
            self.assertEqual(status, 200, "查询 %r 返回了 %s" % (query, status))
            self.assertIn("results", payload)

    def test_snippet_escapes_dangerous_content(self):
        """片段里的 HTML 必须被转义 —— 否则一个文件名或文件内容就能注入页面。

        注意：**不能**用含 ``<`` ``>`` 的文件名做夹具 —— Windows 不允许这些字符，
        测试在 Windows 上会直接建不出文件（实测踩过）。改把危险标签放进**内容**，
        走的是同一条「内容 → 片段 → HTML」的转义路径。
        """
        danger_dir = os.path.join(self.docs, "danger")
        os.makedirs(danger_dir, exist_ok=True)
        with open(os.path.join(danger_dir, "xss.txt"), "w", encoding="utf-8") as fh:
            fh.write("dangerous marker zzz <img src=x onerror=alert(1)> "
                     "<script>alert(2)</script>\n")
        self.app.jobs.submit("scan", {"kb": self.kb_id}, max_attempts=1)
        self._wait_idle()

        status, payload, _ = self.search("marker")
        self.assertEqual(status, 200)
        self.assertGreater(payload["total"], 0)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("<img src=x", blob)
        self.assertNotIn("<script>alert", blob)
        snippet = payload["results"][0]["snippet_html"]
        self.assertIn("&lt;img", snippet)

    def test_suggest(self):
        status, payload, _ = self.get("/api/search/suggest?q=guide")
        self.assertEqual(status, 200)
        self.assertTrue(payload["results"])
        self.assertEqual(payload["source"], "files")


class TestContentAndRange(ApiCase):
    def test_full_content(self):
        status, raw, headers = self.get(
            self.url_for("/api/file/content", os.path.join(self.docs, "guide.md")))
        self.assertEqual(status, 200)
        self.assertIn("TOS 7".encode(), raw)
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")

    def test_range_206_and_content_range(self):
        request = urllib.request.Request(
            self.base + self.url_for("/api/file/content",
                                     os.path.join(self.docs, "guide.md")),
            headers={"Range": "bytes=0-9"})
        with urllib.request.urlopen(request, timeout=20) as response:
            self.assertEqual(response.status, 206)
            body = response.read()
            self.assertEqual(len(body), 10)
            self.assertTrue(response.headers["Content-Range"].startswith("bytes 0-9/"))

    def test_range_suffix(self):
        size = os.path.getsize(os.path.join(self.docs, "guide.md"))
        request = urllib.request.Request(
            self.base + self.url_for("/api/file/content",
                                     os.path.join(self.docs, "guide.md")),
            headers={"Range": "bytes=-5"})
        with urllib.request.urlopen(request, timeout=20) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(response.read()), 5)
            self.assertEqual(response.headers["Content-Range"],
                             "bytes %d-%d/%d" % (size - 5, size - 1, size))

    def test_unsatisfiable_range_416(self):
        request = urllib.request.Request(
            self.base + self.url_for("/api/file/content",
                                     os.path.join(self.docs, "guide.md")),
            headers={"Range": "bytes=9999999-"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=20)
        self.assertEqual(caught.exception.code, 416)
        self.assertTrue(caught.exception.headers["Content-Range"].startswith("bytes */"))

    def test_malformed_range_416(self):
        request = urllib.request.Request(
            self.base + self.url_for("/api/file/content",
                                     os.path.join(self.docs, "guide.md")),
            headers={"Range": "bytes=abc-def"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=20)
        self.assertEqual(caught.exception.code, 416)

    def test_download_sets_attachment(self):
        status, raw, headers = self.get(
            self.url_for("/api/file/raw", os.path.join(self.docs, "guide.md")))
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))

    def test_html_view_carries_csp(self):
        html_path = os.path.join(self.docs, "page.html")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write("<html><body><script>alert(1)</script>hi</body></html>")
        self.app.jobs.submit("scan", {"kb": self.kb_id}, max_attempts=1)
        self._wait_idle()
        status, raw, headers = self.get(self.url_for("/api/file/view", html_path))
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))


class TestViewers(ApiCase):
    def test_markdown_html_escapes_script(self):
        path = os.path.join(self.docs, "xss.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# 标题\n\n<script>alert(1)</script>\n\n"
                     "[点我](javascript:alert(2))\n")
        self.app.jobs.submit("scan", {"kb": self.kb_id}, max_attempts=1)
        self._wait_idle()
        status, payload, _ = self.get(self.url_for("/api/file/html", path))
        self.assertEqual(status, 200)
        self.assertNotIn("<script>", payload["html"])
        self.assertIn("&lt;script&gt;", payload["html"])
        self.assertNotIn("javascript:", payload["html"])

    def test_docx_html(self):
        status, payload, _ = self.get(
            self.url_for("/api/file/html", os.path.join(self.docs, "doc.docx")))
        self.assertEqual(status, 200)
        self.assertIn("第一段正文", payload["html"])
        self.assertNotIn("<script", payload["html"])

    def test_pptx_slides_ordered_by_sldidlst(self):
        status, payload, _ = self.get(
            self.url_for("/api/file/html", os.path.join(self.docs, "deck.pptx")))
        self.assertEqual(status, 200)
        titles = [slide["title"] for slide in payload["slides"]]
        self.assertEqual(titles, ["固件升级流程", "注意事项"])

    def test_xlsx_sheet_paging(self):
        status, payload, _ = self.get(
            self.url_for("/api/file/sheet", os.path.join(self.docs, "sheet.xlsx")))
        self.assertEqual(status, 200)
        self.assertEqual(payload["columns"], ["名称", "数量"])
        self.assertEqual(payload["rows"][0]["c"], ["硬盘", "4"])
        self.assertFalse(payload["has_more"])

    def test_csv_sheet_with_gb18030(self):
        status, payload, _ = self.get(
            self.url_for("/api/file/sheet", os.path.join(self.docs, "数据.csv")))
        self.assertEqual(status, 200)
        self.assertEqual(payload["columns"], ["名称", "数量"])
        self.assertEqual(payload["encoding"], "utf-8")

    def test_text_paging_reports_encoding(self):
        status, payload, _ = self.get(
            self.url_for("/api/file/text", os.path.join(self.docs, "gbk.txt")))
        self.assertEqual(status, 200)
        self.assertIn("固件", payload["text"])
        self.assertEqual(payload["encoding"], "gb18030")

    def test_meta_for_image_and_pdf(self):
        status, payload, _ = self.get(
            self.url_for("/api/file/meta", os.path.join(self.docs, "img.png")))
        self.assertEqual(status, 200)
        self.assertEqual(payload["kind"], "image")
        self.assertEqual(payload["viewer"], "image")
        self.assertEqual((payload["image_w"], payload["image_h"]), (16, 12))

        status, payload, _ = self.get(
            self.url_for("/api/file/meta", os.path.join(self.docs, "report.pdf")))
        self.assertEqual(payload["viewer"], "pdf")
        self.assertEqual(payload["text_state"], "ok")
        self.assertGreater(len(payload["units"]), 0)

    def test_unsupported_sheet_endpoint(self):
        status, payload, _ = self.get(
            self.url_for("/api/file/sheet", os.path.join(self.docs, "guide.md")))
        self.assertEqual(status, 400)


class TestLibrary(ApiCase):
    def test_favorite_roundtrip(self):
        path = os.path.join(self.docs, "guide.md")
        status, payload, _ = self.post("/api/fav/toggle", {"path": path})
        self.assertEqual(status, 200)
        self.assertTrue(payload["favorited"])
        status, payload, _ = self.get("/api/fav/list")
        self.assertTrue(any(f["name"] == "guide.md" for f in payload["favorites"]))
        self.post("/api/fav/toggle", {"path": path})
        status, payload, _ = self.get("/api/fav/list")
        self.assertFalse(any(f["name"] == "guide.md" for f in payload["favorites"]))

    def test_recent_touch(self):
        path = os.path.join(self.docs, "guide.md")
        for _ in range(2):
            self.post("/api/file/touch", {"path": path})
        status, payload, _ = self.get("/api/recent/list")
        entry = next((r for r in payload["recents"] if r["name"] == "guide.md"), None)
        self.assertIsNotNone(entry)
        self.assertGreaterEqual(entry["open_count"], 2)

    def test_icon_set_and_clear(self):
        path = os.path.join(self.docs, "notes")
        status, payload, _ = self.post("/api/icon/set", {
            "target_path": path, "target_type": "dir",
            "icon_type": "builtin", "icon_value": "chart"})
        self.assertEqual(status, 200)
        status, payload, _ = self.get("/api/kb/%d/tree" % self.kb_id)
        notes = next(e for e in payload["entries"] if e["name"] == "notes")
        self.assertEqual(notes["icon"]["value"], "chart")
        self.post("/api/icon/clear", {"target_path": path})

    def test_icon_upload_png(self):
        import base64
        path = os.path.join(self.docs, "guide.md")
        status, payload, _ = self.post("/api/icon/upload", {
            "target_path": path,
            "data_base64": base64.b64encode(synth.make_png(6, 6)).decode()})
        self.assertEqual(status, 200)
        self.assertEqual(payload["icon_type"], "upload")
        icon_id = payload["icon_value"]
        status, raw, headers = self.get("/api/icon/file/" + icon_id)
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertIn("sandbox", headers.get("Content-Security-Policy", ""))

    def test_icon_upload_rejects_bad_svg(self):
        import base64
        payload_bytes = (b'<svg xmlns="http://www.w3.org/2000/svg">'
                         b'<script>alert(1)</script></svg>')
        status, payload, _ = self.post("/api/icon/upload", {
            "target_path": os.path.join(self.docs, "notes"),
            "data_base64": base64.b64encode(payload_bytes).decode()})
        # 清洗后脚本被剥掉，但仍应得到一个可用的 SVG（或明确拒绝）
        if status == 200:
            status2, raw, _ = self.get("/api/icon/file/" + payload["icon_value"])
            self.assertEqual(status2, 200)
            self.assertNotIn(b"<script", raw)
        else:
            self.assertEqual(status, 400)

    def test_icon_upload_rejects_non_image(self):
        import base64
        status, payload, _ = self.post("/api/icon/upload", {
            "target_path": os.path.join(self.docs, "notes"),
            "data_base64": base64.b64encode(b"not an image at all").decode()})
        self.assertEqual(status, 400)

    def test_builtin_icon_list(self):
        status, payload, _ = self.get("/api/icon/builtin")
        self.assertEqual(status, 200)
        self.assertTrue(payload["icons"])


class TestHome(ApiCase):
    def test_home_aggregate(self):
        status, payload, _ = self.get("/api/home")
        self.assertEqual(status, 200)
        self.assertTrue(payload["knowledge_bases"])
        self.assertIn("recents", payload)
        self.assertIn("favorites", payload)
        self.assertIn("counts", payload)


class TestJobs(ApiCase):
    def test_scan_job_completes_and_reports_result(self):
        status, payload, _ = self.post("/api/kb/%d/scan" % self.kb_id, {})
        self.assertEqual(status, 201)
        job_id = payload["job"]["id"]
        for _ in range(200):
            status, job, _ = self.get("/api/jobs/%d" % job_id)
            if job["job"]["state"] in ("completed", "failed", "canceled"):
                break
            time.sleep(0.2)
        self.assertEqual(job["job"]["state"], "completed", job["job"].get("error"))
        self.assertIn("scan", job["job"]["result"])
        self.assertIn("index", job["job"]["result"])

    def test_reindex_submits(self):
        status, payload, _ = self.post("/api/search/reindex", {"kb": self.kb_id})
        self.assertEqual(status, 201)
        self.assertEqual(payload["job"]["type"], "index")
        self._wait_idle()

    def test_unknown_job_type_rejected(self):
        status, payload, _ = self.post("/api/jobs", {"type": "nope"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
