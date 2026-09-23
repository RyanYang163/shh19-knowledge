# Knowledge Base（知识库阅读器）

> TOS 7 Deb 单包应用 · WebUI 内嵌（iframe）· 版本 **1.0.1**

| 项 | 值 |
|---|---|
| 应用 ID | `shh19-knowledge` |
| 包类型 | Deb 单包（`application_type: "deb"`） |
| 打开方式 | WebUI 内嵌（`type: "iframe"`，`path: "/shh19-knowledge/"`） |
| 版本 | 1.0.1 |
| 分类 | `Utilities`, `Web_Services` |
| 发布者 | shh |
| 开发者仓库 | <https://github.com/RyanYang163/shh19-knowledge> |
| 隐私政策（公网可访问） | <https://github.com/RyanYang163/shh19-knowledge/blob/main/PRIVACY.md> |

## 简介

把 TNAS 上任意文件夹变成只读、可搜索、好阅读的私有知识空间。

Turn any folder on your TNAS into a read-only, searchable, beautifully readable knowledge space.

## 功能

- 把任意文件夹注册为只读知识库，原文件不动
- 扫描一次后文件树来自本地索引，不再重复扫盘
- 增量重扫：mtime + size + inode 未变则跳过
- 13 类查看器：PDF / 图片 / Markdown / HTML / 文本 / 代码 / JSON / XML / YAML / CSV / Word / Excel / PowerPoint / 音视频
- Office 与 PDF 文字层由应用自身解析，不依赖外部程序
- PDF 由随包的内置阅读器渲染：页码跳转、文档内搜索
- 全文检索（SQLite FTS5）：类型 / 路径 / 时间过滤 + 相关度高亮 + 命中页码定位
- 收藏、最近阅读、相关文件、快速打开（Ctrl+P）
- 自定义文件与目录图标（只写应用数据库）
- 浅色 / 深色 / 跟随系统，三栏可拖动、支持专注阅读
- 严格只读：不修改、不删除、不移动、不改名任何源文件
- 全部离线，无任何对外网络请求

**实现方式**：后端为 **Python 3 标准库**（打成 zipapp 单文件），前端为**原生
HTML / CSS / JavaScript**，无 React、无 Vite、无 Node 构建链 —— 因为 TOS 7 只预装
Python 3、nginx 与 systemd，任何 Node 依赖都会变成「command not found」驳回项。

**第三方组件共 1 项**：Mozilla PDF.js（Apache-2.0），用于在浏览器内渲染 PDF。
除此之外无非自有代码、无图标字体、无模型、无数据集。
详见 [`NOTICE`](./NOTICE) 与 [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)。

## 不承诺的功能

以下项目**本版本没有**，`.lang` 的描述里也**没有承诺**它们。列出是为了让「描述与
实际功能相符」（指引 16.5 rank 5 的驳回项）经得起逐条核对：

| 类别 | 不做的具体项 |
|---|---|
| 编辑类 | 在线编辑、重命名、移动、删除、上传、新建文件/文件夹、版本管理 |
| 协作类 | 评论、协作、分享链接、云端同步 |
| AI 类 | AI 摘要、文档问答、知识库问答、语义搜索、向量嵌入、相关文档的 AI 推荐 |
| OCR | 图片文字识别（扫描件 PDF 因此没有文本层） |
| 格式支持 | `.doc` / `.xls` / `.ppt`（OLE2 旧格式）、`.odt` / `.ods` / `.odp`、`.epub`、`.pages` / `.numbers` / `.key`、`.msg`、`.dwg`、HEIC / TIFF 解码 |
| 转换类 | PPTX 转 PDF、Office 转其它格式（需要 LibreOffice，系统未预装且不作为依赖） |
| 渲染限制 | PDF 中 JPEG2000 图像可能不显示；**未嵌入 CID 字体**的 PDF 可能缺字形；ICC 色彩管理缺失（原因见 `THIRD_PARTY_NOTICES.md` 的排除说明） |
| 结构解析 | YAML **按纯文本**处理（标准库无 YAML 解析器），只额外抽取顶层键 |
| 检索限制 | **1–2 个字的中文子串查询**走的是有上限的模糊匹配而非全文索引，界面上会明确标注「模糊匹配」 |
| 后台调度 | 没有常驻的定时自动扫描；扫描由你手动触发或由「新建知识库」时自动发起 |
| 缩略图 | 不做服务端缩略图；图片由浏览器缩放显示（零服务端开销、全彩） |
| 用户体系 | 无账号、无权限分级；收藏与最近阅读是**设备级一份**（见 `PRIVACY.md` 第 5 节） |

## 与设计文档的偏差（及原因）

产品设计文档见 `开发应用计划/2026-0923/TNAS_Knowledge_详细开发设计文档.md`。
以下几处**有意偏离**，原因都是硬约束而不是取舍偏好：

| 设计文档 | 实际做法 | 原因 |
|---|---|---|
| §27 后端用 **Go 单二进制** | **Python 3 标准库** zipapp | 审核标准 §16.4 一票否决：包内不得含二进制可执行文件。`shh9-ddns-updater` 正因「deb 打包了预编译二进制」被驳回。而驳回原因 Top 13 恰好点名推荐 Python 实现 |
| §26 前端用 **React + Vite + Tailwind** | 原生 HTML/CSS/JS | TOS 未预装 Node.js，`Depends: nodejs` 会导致安装失败；且构建产物属于「预编译资源」 |
| §15 用 **Mammoth.js** 转 DOCX | 服务端 `zipfile` + `ElementTree` | 减少第三方组件；设计文档自己也提醒 Mammoth 输出必须二次 sanitize，改成服务端解析后转义从一开始就到位 |
| §16 研究 **SheetJS CE** | 服务端自解析 XLSX | 同上；且设计文档要求「采用前必须锁定版本并核查许可证」 |
| §17 PPTX 转 PDF | 按页提取文字卡片 | 需要 LibreOffice，系统未预装、不可作为依赖 |
| §35 用 **Tesseract** 做 OCR | V1 完全不做 OCR | 系统未预装；且 OCR 需要用户显式授权磁盘与 CPU 开销 |
| §30 服务端缩略图缓存 | 不做缩略图 | 应用内的图片解码只能得到灰度图，灰度缩略图对照片库是可见的质量倒退；交给浏览器缩放反而全彩且零服务端成本 |
| §48 `/opt/tnas-knowledge`、`/var/cache/…`、`/etc/…` | 平台规定的 `/usr/local/<appid>/` + 安装目录下 `data/` | 官方指引 8.2 要求安装到 `/usr/local/<app_id>/`；`/etc` 在 `ProtectSystem=strict` 下只读；`/var/cache` 会触发 systemd 命名空间冲突 |
| §22 `files.hash` 列 + `favorites.user_id` | 都不建 | §23 自己说「不要每次对所有文件做 SHA-256」，两处矛盾取 §23；框架不暴露用户身份，故收藏为设备级 |
| §31–§37, §45 AI 全部功能 | V1 无任何 AI 代码 | 按 §34「AI 不是应用启动依赖」；也为让 C6「数据收集一致性」与 C8「第三方 API 披露」天然无懈可击 |

## 权限声明

| 权限 | 用途 | 说明 |
|---|---|---|
| 网络：无宿主端口 | — | 只监听 Unix socket，不占用任何宿主端口，不接受远程连接 |
| 出网：无 | — | 应用**不发起任何对外网络请求** |
| 文件系统：`/Volume*/@apps/shh19-knowledge/data` | 运行期数据 | 唯一的写入位置 |
| 文件系统：用户加入白名单的目录 | 读取用户文件 | **只读**；白名单初始为空，必须由用户显式添加 |
| 系统用户：`shh19knowledge` | 隔离运行 | 由平台创建（`postinst` 幂等兜底）；**非 root** |
| `CAP_DAC_OVERRIDE` | 在 `/var/api`（`755 root:root`）里创建自家 Unix socket | ⚠️ **见下方「关于 CAP_DAC_OVERRIDE」** |
| 共享文件夹 / 容器 / 特权 | 不使用 | 非 Docker 应用，不使用 `privileged`、不使用 `network_mode: host` |

资源上限（systemd 单元内声明）：`MemoryMax=1024M`、`CPUQuota=200%`、
`LimitNOFILE=65536`、`LimitNPROC=256`。

### 关于 `CAP_DAC_OVERRIDE`

systemd 单元声明了 `AmbientCapabilities=CAP_DAC_OVERRIDE` 与
`CapabilityBoundingSet=CAP_DAC_OVERRIDE`。**这是有意为之，也必须如实说明**：

- **为什么需要**：平台要求 iframe 应用把 Unix socket 建在 `/var/api/<appid>.sock`
  （指引 8.7.1），而 TOS 上 **`/var/api` 是 `/tmp/api` 的软链，权限是 `755 root:root`** ——
  非 root 的应用用户**既不能在里建文件也不能 unlink**（真机实测 `touch` 与
  `socket.bind()` 都是 `Permission denied`）。没有这个能力时，服务会以
  `status=1/FAILURE` 反复重启、socket 永不出现，应用彻底不可用。
- **它换来了什么**：服务 `active`，socket 建成且属主与权限完全符合规范
  （`srw-rw---- shh19knowledge:shh19knowledge`，mode `0660`）。
- **安全性上是净减少而非放松**：`CapabilityBoundingSet=CAP_DAC_OVERRIDE` 把能力集
  从内核默认的 **41 个收窄到 1 个**。授予的那一个正好是「在平台自有的 `/var/api` 里
  创建自家 socket」所必需的 —— 即指引 12.7 要求的
  「drop all capabilities, add only the required ones」写法。
- **它的作用范围**：只绕过**文件系统的权限位检查**。应用仍然：不联网、不监听网络端口、
  不加载内核模块、不访问设备节点、不修改任何文件、不写 `/etc` `/usr` `/boot`，
  且写入路径被 `ReadWritePaths` 限制在安装目录与 `/var/api`。
- 若管理员出于更严的策略不接受它，替代方案是把 `/var/api` 的属主或权限改为允许
  应用用户写入（例如为应用建立子目录或加 ACL），然后从单元文件中移除这两行 ——
  但这属于偏离平台默认布局，需自行承担。

## 运行时写入路径清单（指引 12.9.6）

| 路径（除特别注明外，均相对 `/Volume*/@apps/shh19-knowledge/data/`） | 用途 | 格式 | 创建时机 | 增长上限 / 轮转 | 生命周期 |
|---|---|---|---|---|---|
| `config/runtime.json` | 应用偏好：主题、栏宽、可访问目录白名单、索引选项 | JSON | 首次启动创建，配置变更时重写 | < 64 KB | 持久，随升级保留 |
| `db/app.db`（及 `-wal` / `-shm`） | 知识库注册表、文件元数据、抽取文本、全文索引（FTS5）、收藏、最近阅读、自定义图标、任务队列 | SQLite | 首次启动建库并迁移 | 文件数与抽取文本受「单库 30 万文件」「单文件索引前 1 MiB 文本」两个上限约束；已完成任务只保留最近 200 条 | 持久，随升级保留；**升级绝不删库**（用 `PRAGMA user_version` 迁移，只做加法） |
| `cache/icons/` | 用户上传的自定义图标（经安全清洗后落盘） | SVG / PNG / JPEG / GIF / WebP / BMP | 用户上传图标时 | 单个 ≤ 256 KB；取消图标后不再被引用，启动时不主动清理 | 可再生，可安全删除 |
| `tmp/` | 处理中的临时文件（当前实现未使用，保留给后续功能） | — | — | 上限受 `data/` 配额约束 | **每次启动清空全部残留** |
| `logs/app.log`（及 `app.log.1` … `.5`） | 应用日志（标准格式，写入前已对凭据类字样脱敏） | 文本 | 服务运行时持续写入 | 单文件 2 MB，轮转保留 5 个（约 12 MB 上限） | 持久，自动轮转；也可由 journald 收集 |
| `output/` | **本应用不使用**（它是只读阅读器，不导出、不转码、不生成结果文件） | — | 由框架在首次启动时创建为空目录 | 始终为空 | 可安全删除 |
| `/var/api/shh19-knowledge.sock`（绝对路径） | 平台代理 Unix socket，mode `0660` | Unix socket | 服务启动时创建 | 不适用 | **每次启动前先删除残留**（否则启动失败） |
| `/var/lib/shh19-knowledge/`、`/var/log/shh19-knowledge/`、`/run/shh19-knowledge/`（绝对路径） | systemd `StateDirectory` / `LogsDirectory` / `RuntimeDirectory` 提供的兼容目录 | 目录 | systemd 创建 | 不适用 | 由 systemd 管理（`/run` 下的在停止时删除） |

**本应用不写入上表以外的任何路径。**

> **关于临时文件**：本应用**刻意不启用 `PrivateTmp=true`**。原因是它与平台的
> `/var/api/<appid>.sock` 形态互斥 —— `PrivateTmp` 会给服务一个私有 `/tmp`，
> socket 落在里面之后平台代理（在宿主上）永远看不到它，应用会彻底不可用。
> 代价是临时文件不能靠 systemd 隔离，因此本应用**不写共享的系统 `/tmp`**，
> 只在需要时使用上表中的 `data/tmp/`，并在每次启动时清空残留。

## 隐私政策（审核项 C3 / C4 / C5）

**公网地址（可直接访问）**：<https://github.com/RyanYang163/shh19-knowledge/blob/main/PRIVACY.md>

包内另有 `PRIVACY.md` 全文，`config.ini` 的 `help` 字段指向同一地址——三条路径都可查。

- **本地优先**：所有扫描、索引、检索、渲染都在你的 TNAS 上完成。
- **不采集**：不收集任何使用统计、遥测、设备标识或个人信息；不保存任何 API Key。
- **不上传**：不联网，没有任何对外请求，也没有远程接口设置项。
- **只读**：不修改、不删除、不移动、不改名任何源文件。

## 构建

```bash
./build.sh            # 默认 x86_64
./build.sh aarch64
```

产物在 `build/output/`：

```
shh19-knowledge_x86_64.deb
shh19-knowledge_x86_64.deb.sha256
```

> 包文件名**不含版本号**——版本由 GitHub Release 的 tag 承载（指引 15.1 / 4.2.4）。
> 构建器是 `tools/build_deb.py`，纯 Python 实现，不依赖 `dpkg-deb`，因此在
> Windows 开发机上也能产出真 deb 并做结构自检：
> `python3 tools/build_deb.py --inspect build/output/shh19-knowledge_x86_64.deb`

## 本地运行（无需 TOS 设备）

```bash
python3 src/__main__.py --tcp 127.0.0.1:18019 --data-dir ./data
# 浏览器打开 http://127.0.0.1:18019/
```

同一份业务代码，只是把 Unix socket 换成 TCP。跑测试：

```bash
python3 -m unittest discover -s tests -v
```

## 安装、升级与卸载验证

```bash
# 安装
sudo dpkg -i shh19-knowledge_x86_64.deb
sudo systemctl status shh19-knowledge
sudo journalctl -u shh19-knowledge -f

# socket 是否就绪（iframe 应用的关键一项）
ls -l /var/api/shh19-knowledge.sock
curl --unix-socket /var/api/shh19-knowledge.sock http://localhost/health

# 启停
sudo systemctl restart shh19-knowledge

# 卸载（保留数据）
sudo dpkg --remove shh19-knowledge
# 彻底卸载（仍按设计保留数据盘上的运行数据）
sudo dpkg --purge shh19-knowledge

# 升级（数据必须保留）
sudo dpkg -i shh19-knowledge_x86_64.deb
```

**排障要点**（两个真机踩过的坑，现象与原因都记在这里）：

| 现象 | 原因 | 处理 |
|---|---|---|
| `status=216/GROUP`，应用一行代码都没跑到 | 平台创建应用用户时主组是既有的 `allusers`，**不会创建同名组**；systemd 解析不到 `Group=` 就整步失败 | 本包的 `postinst` 已幂等补建同名组（`getent group … \|\| groupadd -s`），安装时即可解析 |
| `status=226/NAMESPACE` | `/var/log` 在 TOS 上是 `/tmp/log` 的软链，与 `PrivateTmp=true` 同时出现在 `ReadWritePaths` 会冲突 | `ReadWritePaths` 只写安装目录与 `/var/api`，改用 `StateDirectory=` / `LogsDirectory=`；并**不启用 `PrivateTmp`** |

**卸载后残留说明**：`dpkg --purge` 会删除 `/usr/local/shh19-knowledge`、`/var/api/shh19-knowledge.sock`、
`/var/lib/shh19-knowledge`、`/var/log/shh19-knowledge`、systemd 单元与专用用户。
**数据盘上的 `/Volume*/@apps/shh19-knowledge/data/` 按设计保留**（指引 51 / 12.9.7：
卸载默认保留用户数据）。其中包含索引数据库与你的偏好设置。需要彻底清理时手动执行：

```bash
sudo rm -rf /Volume*/@apps/shh19-knowledge
```

## 安全设计

- **非 root 运行**：专用系统用户 `shh19knowledge`，systemd `User=` 与 `config.ini`
  的 `user` 字段逐字一致。
- **systemd 加固**：`NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome`、
  `PrivateDevices`、`ProtectKernelTunables`、`ProtectKernelModules`、
  `ProtectControlGroups`、`RestrictRealtime`、`RestrictSUIDSGID`、`LockPersonality`、
  `RemoveIPC`、`SystemCallArchitectures=native`；可写路径用 `ReadWritePaths` 显式枚举
  （仅安装目录与 `/var/api`）。**不启用 `PrivateTmp`**，原因见上文。
- **能力集最小化**：只声明 `CAP_DAC_OVERRIDE` 一项（用途见上文专节），
  且 `CapabilityBoundingSet` 同样只限这一项 —— 实际效果是把内核默认的 41 个能力
  **收窄到 1 个**，属净减少。
- **无 shell 执行入口**：所有接口都是固定命名的具体能力，不存在
  `POST /exec` 这类接受任意命令行的入口，应用也从不调用任何外部程序。
- **路径双层校验**：所有用户文件访问都经过唯一收口 `app/kbs.py::resolve_in_kb` ——
  先 `realpath` 后比对白名单根（挡住目录穿越与逃逸软链），**再**校验路径必须落在
  目标知识库根之下（挡住「白名单内的相邻目录」）。
- **内容注入防护**：Markdown / Word 服务端渲染**先整体转义再插入自有标签**；
  HTML 走 `sandbox` iframe + 严格 CSP；上传的 SVG 图标按标签白名单清洗并另加 CSP。
- **日志脱敏**：写入前对密码、Token、API Key、Cookie、`Bearer` 凭据做兜底打码。
- **生命周期脚本无网络操作**：`preinst` / `postinst` / `prerm` / `postrm` 只做目录、
  属主与服务注册，不含 `apt` / `pip` / `curl` / `wget` / `git clone`。
- **不写系统目录**：不在 `/etc`、`/usr`、`/boot` 下写任何运行期配置。
- **包内无二进制**：无 ELF / `.so` / `.wasm`，全部为可审计的源码与纯文本资源。

## 待真机验证清单（本机无法验证，交付时如实标注）

本机为 Windows 开发环境，**没有 Docker、没有 dpkg-deb、没有可访问的 TNAS**，
以下项目**只能在真实设备上验证**：

- [ ] 安装 / 启动 / 停止 / 重启 / `--remove` / `--purge` / 升级的数据保留
- [ ] `216/GROUP` 与 `226/NAMESPACE` 两项是否已彻底规避
- [ ] 目标机上 Python 3.10 的 `sqlite3` 是否编译了 **FTS5**（未编译时应用照常启动、
      搜索自动降级为模糊匹配，`/api/status` 会给出 `fts5: false` 与降级原因）
- [ ] `CAP_DAC_OVERRIDE` 是否足以在 `/var/api` 建出 socket（同批应用已在真机验证过这条，本应用沿用同一配置）
- [ ] 读取 `/Volume*/` 下**不属于应用用户**的共享文件夹文件是否成功（若因权限读不到，用户需为该目录授予读取权限或加 ACL；本应用不会绕过这一点去改文件属主）
- [ ] 大目录（10 万文件）的扫描耗时与文件树展开响应
- [ ] 中文/繁体/日文/韩文编码文档、中文文件名、超长路径
- [ ] 含 JPEG2000 图像的 PDF、未嵌入 CID 字体的中文 PDF 的实际渲染效果
- [ ] `aarch64` 架构的包（本版本只发布 `x86_64`）

## 许可证

本项目自有代码以 **MIT** 发布，全文见 [`LICENSE`](./LICENSE)。

第三方组件共 **1 项**：Mozilla PDF.js（Apache-2.0，许可证全文见
[`webui/pdfjs/LICENSE.txt`](./webui/pdfjs/LICENSE.txt)）。署名、版本锁定与
刻意排除的部分见 [`NOTICE`](./NOTICE) 与 [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)；
机器可读清单见 [`sbom.spdx.json`](./sbom.spdx.json)。
