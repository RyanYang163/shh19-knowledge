# 第三方组件清单 / Third-Party Notices

**本应用包内含 1 个第三方组件：Mozilla PDF.js（Apache-2.0）。**
除它之外，包内不含任何第三方代码、二进制可执行文件、模型、数据集或字体。

## 一、包内组件清单

| 组件 | 来源 | 许可证 | 是否随包分发 | 形式 |
|---|---|---|---|---|
| `tnasapp` 应用框架 | 本项目自有 | MIT | 是 | Python 源码 |
| 后端业务代码 `src/app/*` | 本项目自有 | MIT | 是 | Python 源码 |
| 前端 `app.css` / `app.js` / `icons.js` | 本项目自有 | MIT | 是 | 文本 |
| 前端 `knowledge.css` / `knowledge.js` / `viewer.js` | 本项目自有 | MIT | 是 | 文本 |
| 应用图标 SVG | 本项目自有手绘 | MIT | 是 | 文本 |
| **Mozilla PDF.js** | <https://github.com/mozilla/pdf.js> | **Apache-2.0** | **是** | **仅 2 个 `.mjs` 文本文件** |
| Python 3 标准库 | 系统预装 | PSF License | **否**（运行时由系统提供） | — |

### 1.1 Mozilla PDF.js 明细

| 项 | 值 |
|---|---|
| 组件名 | pdfjs-dist |
| 版本 | **6.3.289**（锁定版本，不使用浮动标签） |
| 上游仓库 | <https://github.com/mozilla/pdf.js> |
| 许可证 | Apache License 2.0 |
| 版权 | Copyright 2012 Mozilla Foundation |
| 包内路径 | `webui/pdfjs/pdf.min.mjs`（448 KB）、`webui/pdfjs/pdf.worker.min.mjs`（1.2 MB） |
| 许可证全文 | `webui/pdfjs/LICENSE.txt`，同时随包分发 |
| 是否修改 | **否**，原样复制上游构建产物；仅统一了行尾为 LF、去除了可能的 BOM |
| 用途 | 在浏览器内渲染 PDF（翻页、缩放、旋转、文档内查找） |
| 网络行为 | **无**。纯本地执行，不联网、不上传、不与任何服务器通信 |

**上游获取方式（可复现）**：

```bash
npm pack pdfjs-dist@6.3.289
tar -xzf pdfjs-dist-6.3.289.tgz \
    package/build/pdf.min.mjs package/build/pdf.worker.min.mjs package/LICENSE
```

#### 1.1.1 刻意排除的部分（合规决定，务必保留此说明）

PDF.js 官方发行包还包含下列内容，**本应用一个都没有放进包里**：

| 排除项 | 体积 | 排除原因 |
|---|---|---|
| `wasm/openjpeg.wasm`、`wasm/qcms_bg.wasm` | 约 2 MB | **可执行的 WebAssembly 字节码**，会直接撞上审核标准 §16.4 一票否决项：包内不得含二进制可执行文件 |
| `cmaps/*.bcmap` | 168 个文件，约 1.5 MB | 二进制数据文件。放进一个以「包内无二进制」为合规基础的包里不值得 |
| `standard_fonts/*` | 约 1 MB | 同上；且其中含 Foxit 的额外许可条款，引入不必要的许可证复杂度 |
| `*.mjs.map` | 约 6 MB | 仅开发调试用，无功能价值 |

**排除带来的功能限制**（已如实写入 `README.md` 的「不承诺的功能」）：

- 含 **JPEG2000** 压缩图像的 PDF 页面可能无法显示图像（正文仍正常）；
- 使用**未嵌入 CID 字体**的 PDF 可能出现字形缺失或走系统替代字体；
- ICC 色彩管理缺失，色彩可能略有偏差。

**不受影响的能力**：PDF 的**全文检索**由应用自己解析文本层实现（见 `src/app/extract_pdf.py`），
与 PDF.js 无关；页面导航、缩放、旋转、文档内查找全部正常。

> 若日后需要补上上述限制，**必须先解决 `wasm` 的合规问题**（例如改用不含 wasm 的旧版
> 构建，或申请豁免），不得直接把官方完整包放进来。

## 二、可选调用的系统程序（不随包分发）

**（无）** 本应用不调用任何外部程序。

设计文档 §17 曾建议 PPTX 转 PDF（需要 LibreOffice）、§35 建议用 Tesseract 做 OCR、
§11 提到 PDF.js 的图像解码扩展 —— **这些外部程序本应用一概不依赖、不调用、不打包**。
PPTX 以「每页文字卡片」形式呈现，PDF 文本层由应用自己解析。

## 三、显式排除项

为避免许可证与合规风险，本项目**刻意不使用**下列组件：

| 组件 | 许可证 | 排除原因 |
|---|---|---|
| Mammoth.js（设计文档 §15 提及） | BSD-2-Clause | DOCX 改由服务端 `zipfile` + `ElementTree` 自己解析，减少第三方面 |
| SheetJS CE（设计文档 §16 提及） | Apache-2.0（需锁版本核查） | 同上，XLSX 自己解析；该组件许可证条件设计文档本身就要求单独核查 |
| Tesseract（设计文档 §35 提及） | Apache-2.0 | 系统未预装，V1 不做 OCR |
| LibreOffice（设计文档 §17 隐含） | MPL-2.0 | 体积巨大且系统未预装，不作为依赖 |
| React / Vite / Tailwind（设计文档 §26 提及） | MIT | TOS 未预装 Node.js，无法构建；且构建产物属于「预编译资源」 |
| Ultralytics YOLO | AGPL-3.0 | 与商业友好分发冲突 |

## 四、模型与数据集

**本应用不打包任何 AI 模型权重或数据集，也不含任何 AI 功能。**
（设计文档 §31–§37 的 AI 摘要 / 问答 / 语义搜索 / 嵌入属于 1.1 版本规划，
本版本既无代码也无配置项，`PRIVACY.md` 已明确声明。）

## 五、SBOM

机器可读的软件物料清单见 [`sbom.spdx.json`](./sbom.spdx.json)，
其中同时列出本应用与 Mozilla PDF.js 两个 package。
