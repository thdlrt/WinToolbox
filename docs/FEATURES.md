# 内置工具与工具包接口

这些工具使用独立数据目录，不导入 AItext 的旧配置、密钥或历史数据。所有测试使用临时目录和注入的模型接口。

## API 用量统计

百炼配置页显示本机今日、本月与累计用量，并提供本月按模型明细。普通与流式问答、翻译、视觉、嵌入、文件/实时转写和云端配音都会记录请求结果；Token 只采用接口返回值，实时与文件语音使用已发送或已处理的时长。

数据保存在 `engine.sqlite3`，只包含时间、供应商、模型、功能、成功/失败状态、Token、语音秒数和耗时，不保存提示词、回答、音频或密钥。历史请求不回填，其他客户端的调用不在统计中，因此不能替代服务商账单或余额。

## Codex 配置

`codex.scan` 仅读取所选 Codex 目录的 `config.toml` 与 `models_cache.json`；默认读取 `CODEX_HOME`，未设置时使用用户目录 `.codex`。不读取 `auth.json`。

`codex.preview` 接收准确模型 ID，以及 `default`、`max`、正整数、`500k` 或 `1m` 形式的上下文大小，返回 TOML 修改前后内容和版本 `hash`。缓存未列出的最大上下文不作推断；超过已知上限时拒绝写入。

`codex.apply` 与 `codex.restore` 必须传入 `expected_hash`。只修改顶层 `model`、`model_context_window`，保留注释、表内字段、显式自动压缩阈值、BOM 和换行。写入前备份旧文件，重新检查版本后原子替换。备份与原配置放在一起；恢复只接受该配置旁的备份。写入前检测其他进程修改，遇到冲突需要重新预览。操作系统不提供跨应用事务，无法阻止不遵循锁的外部编辑器在最后一次检查后写入。

## 文件整理

`files.preview` 保存可审阅操作清单；`files.apply` 只执行该清单。扩展名或文件基本名末尾都可作为后缀匹配；递归移动保留相对目录。翻译始终在源目录内重命名，默认译为简体中文并保留扩展名。`prefix_only` 只翻译首个下划线之前的文本，其余名称保留。

执行前和执行中校验源文件大小、修改时间和文件标识。目标使用独占创建，始终不覆盖已存在文件。失败时回滚已完成项，若文件已被外部修改则保留文件并在操作清单记录回滚问题。执行记录存放在 `file-operations`。

## 实时助手资料库

资料库在实时助手内准备和选择，不提供独立问答页面。适合会前导入少量相关文档，等待文本提取与索引完成，再开始实时问答；准备状态和导入错误应在会前检查。预先索引省去现场导入开销，不代表模型回答没有延迟。

支持 PDF、PPTX、DOCX、Markdown、TXT、PNG、JPEG。PDF 按实际页提取并渲染；PPTX 无 LibreOffice 时仍可按幻灯片提取文字和表格；DOCX 无 LibreOffice 时提供文本和表格，不编造物理页码。Office 图表分析和 Word 物理页引用需要模型中心的 LibreOffice 文档运行包，或 `preferences.libreoffice_path`。

原文与视觉模型解释分别索引，结果保留 `kind: text | vision`、源文件、页码或文本位置、页面图片。原文 `quote` 直接来自提取片段；视觉来源属于模型解读。无法解析的页面、未配置的视觉模型、缺失嵌入索引都会出现在文档和导入报告的 `warnings` 中，文档状态为 `partial`。

LibreOffice 支持模型中心“导出离线包 / 导入离线包”。离线格式与其他运行包一致，使用 `wintoolbox-runtime` v1 清单、`model_id: libreoffice`、`engine: document`、`runtime/` 文件前缀、平台标识和逐文件 SHA-256。导入不运行 MSI，不需要管理员权限；先展开、校验并使用独立用户配置启动版本检测，再更新运行目录和设置。Windows 版本检测使用 `soffice.com` 输出版本文本，文档转换入口仍为 `soffice.exe`。

检索使用关键词 BM25 与嵌入余弦相似度的排名融合。嵌入不可用时返回明确的关键词模式提示。实时助手根据所选资料库检索，回答附带候选来源供核对；无检索结果时说明资料不足。未选择资料库时使用会话上下文。

## 工具包 API 1

`.toolpkg` 是 ZIP 文件，根目录需有 `manifest.json`。清单字段：

```json
{
  "id": "file-hash",
  "name": "文件哈希",
  "version": "1.0.0",
  "api_version": 1,
  "runtime": "python",
  "entrypoint": "main.py",
  "permissions": ["filesystem"],
  "ui": {"fields": [{"name": "path", "type": "file", "label": "文件", "required": true}]}
}
```

字段类型支持 `text`、`textarea`、`number`、`boolean`、`select`、`file`、`directory`。工具 ID 使用小写字母、数字、连字符；版本使用三段语义版本。安装前验证版本、入口、路径、链接、Windows 保留名、大小及文件数。升级先暂存旧版本，失败时恢复。

入口通过独立 Python 进程执行，接收一行 JSON：

```json
{"params":{"path":"E:/example.txt"},"context":{"api_version":1,"output_dir":".../artifacts/job-id"}}
```

stdout 只能输出一行一个 JSON 事件，日志写 stderr：

```json
{"type":"progress","percent":42,"message":"正在计算"}
{"type":"result","result":{"sha256":"..."}}
```

失败可输出 `{"type":"error","message":"具体原因"}` 并返回非零退出码。无 result、异常退出、协议错误、超时都会让任务失败。取消时终止进程树，结果 JSON 与 stderr 日志归入任务记录。工具代码拥有当前用户权限；`permissions` 是能力声明，进程隔离不是安全沙箱。首版安装可信本地工具，不提供在线市场或依赖自动安装。

`examples/file-hash.toolpkg` 可直接安装，源码位于 `examples/file-hash/`。无需重编译宿主即可安装、运行、启停、升级和卸载。

## 备份与换机

备份导出使用 SQLite 一致性快照，归档附带逐文件 SHA-256 清单。默认包含文字、索引、资料、工具包、录音和结果，可排除媒体及模型。用户配置的外部媒体库纳入归档，并在新机器恢复到应用数据目录内的媒体库，更新配置、会话及数据库内的相关路径。

API 密钥默认排除。包含密钥时必须设置至少 8 字符的密码：整个归档使用 AES-256-GCM 加密，PBKDF2-HMAC-SHA256 派生密钥；恢复后使用当前 Windows 用户的 DPAPI 重新保护密钥。密码不写入任务参数、日志或备份清单。更换机器后通过密码恢复，无需迁移旧 Windows DPAPI 数据。

勾选模型运行环境时，备份包含应用管理的基础 Python 和独立环境；恢复同时重写 `pyvenv.cfg` 的 `home`、`executable` 等运行路径，保留版本和系统包隔离设置。

恢复先校验文件、大小、散列和数据库，再暂存旧目录后替换；失败回滚。其他任务或实时会话运行中不能恢复。恢复后建议重启。备份刻意排除的模型保持现状；排除媒体时也不会删除当前同目录已有录音。单独指定 `output_dir` 的任务，其记录中的输出文件也会纳入备份并更新路径，不复制所在文件夹内的无关文件。原始导入媒体不属于生成结果；如需换机继续重新处理，请另外保留原始音视频。已经被用户移动或删除的输出会在备份结果中列出警告。

## 脚本工具

「脚本工具」是本机脚本的清单页：竖排列表，左边一个运行按钮，右边是打开脚本文件、打开所在文件夹、编辑与删除，运行后该条目下面展开自己的终端，显示实时输出与状态，运行中可停止。「新增脚本」选择 `.py`、`.ps1`、`.cmd`、`.bat`、`.exe`、`.js`、`.mjs` 文件后即可用；名称与说明可随时编辑，删除只出列表，脚本文件保留。可执行文件（`.exe`/`.cmd`/`.bat` 等）不提供「打开脚本文件」，避免用系统默认动作把脚本直接跑起来。

清单存在本机 `%LOCALAPPDATA%/WinToolbox/script-tools/state.json`：脚本路径是本机事实，不进入同步的应用数据。随工具箱提供的内置脚本在首次使用时复制到 `scripts/` 子目录，未被改动过的副本随升级自动更新，你改过的副本永不覆盖，只在条目上提示可在编辑里恢复内置版本。

首个内置脚本「同步 MCP 到 DSH」把 Codex 配置里的 `mcp_servers` 写成 DeepSeek Harness 的 `dsh-mcp-client` 插件行：默认读 `CODEX_HOME` 或用户目录 `.codex` 的 `config.toml`，写入 `DSH_HOME/profiles/<profile>/cordis.patch.yml`（默认 profile 为 desktop），改动只落在 `# >>> dsh-mcp-sync` 与 `# <<< dsh-mcp-sync` 之间，写入前备份原文件，重复运行整段替换。要调整跳过的服务器、目标 profile 或输出细节，直接编辑这个脚本文件（它支持 `--dry-run`、`--source`、`--target`、`--exclude`、`--profile` 等参数）；服务器名不合法或既无 url 也无 command 的条目会被跳过并在输出里逐条说明。

第二个内置脚本「同步技能库到 DSH」让 Harness 直接读取 cc-switch 维护的技能库：在 profile 的 `cordis.patch.yml` 里维护一段 `skill-ccswitch` 行（独立的 skill-filesystem 提供方，不扫默认根，只挂技能库目录），所以技能库的增删改名对 Harness 立即生效，不需要同步或复制。它同时检查库里每个技能的 frontmatter：Harness 对名字不是 kebab-case 或缺 description 的技能会静默跳过，脚本会把这些条目点名出来，并列出 Harness 会读取的另一个来源（`~/.agents/skills`）。技能库默认 `~/.cc-switch/skills`，可用参数 `--library` 指定。如果补丁里标记段之外已经存在同名的 `skill-ccswitch` 条目，脚本会拒绝写入并指出行号，避免把同一个提供方挂载两次。

## 桌面机器人（StackChan）

左右滑动页面，上滑应用面板；PC 提供 CPU/GPU/内存、图案天气及 Codex 真实额度。仅工具箱运行期间同步；城市、设备连接和 GPU 在本页设置。详见 [使用与协议](STACKCHAN.md)。

## 硬盘挂载

把 SFTP / WebDAV 目录挂成 Windows 网络盘符，行为对齐 RaiDrive：固定盘符、只读可选、断线自动重连（最多 10 次）、单条挂载可设「自动挂载」（工具箱启动后自动挂载，退出时卸载）。密码用当前用户 DPAPI 加密只存本机，任务与日志中不出现明文。页面提供「从 RaiDrive 导入」（读取本机 RaiDrive 服务日志恢复配置，密码需补填一次）与「从 WebDAV 同步导入」（复用 WebDAV 备份连接与已存密码）。挂载前需要安装一次 WinFsp（页面内可跳转下载）；rclone 引擎随包提供。详见 [NETMOUNT.md](NETMOUNT.md)。
