# WinToolbox 0.1.21 验收记录

最终回归日期：2026-09-15。开发与实际执行环境为 Windows 11 x64、Ryzen 9 9950X、RTX 5080 16 GB、64 GB 内存。

## 自动测试

完整测试命令：`core/.venv/Scripts/python.exe -m pytest tests -q`。

最终核心测试：**341 passed**。覆盖引擎、语音与字幕、资料索引、口语练习、记账、WebDAV、扩展、备份迁移、Shizuku 和 FN Connect 等功能。

- 任务队列、状态保存、取消、重试、输入/参数指纹检查点。
- 字幕时间校验、片段对应翻译、全稿分段总结、实际 FFmpeg 字幕压制与配音音轨混合。
- TOML 注释/表结构/BOM/换行保留、上下文越界、并发修改校验、备份与恢复。
- 文件操作预览、重名拦截、外部变更、失败回滚。
- 工具包版本、路径穿越/链接/大小限制、真实独立插件进程。
- PDF/PPTX/DOCX 正文与表格、图表解析接口、引用来源、关键词检索降级说明。
- AES-GCM 密码拒绝、SQLite WAL 快照、跨目录路径重定位、DPAPI 密钥恢复、外部输出文件迁移。
- WebSocket 转写事件、Qwen 临时文本、结束尾句、重连补转写、取消、双路时间轴和重复问题。
- 独立 JSON-RPC 进程 stdout 协议纯净度、未知方法错误、删除服务清除密钥、新版本数据库拒绝降级。
- FN API 端口映射的原始 TCP 往返、固定 LAN 范围、局域网策略校验、端口限制、规则持久化与启停删除。

Python 3.12 的 `audioop` 弃用提示不影响当前随应用提供的3.12运行环境；升级 Python 3.13 前需替换实时重采样实现。

## 真实运行验证

| 验证 | 结果 |
|---|---|
| Rust 原生壳 | cargo check / debug build / release build 通过 |
| React 界面 | TypeScript、Vite 生产构建通过；主生产脚本约 424 KB，gzip 约 127 KB |
| Windows UI | 实际 WebView2 窗口检查首页、媒体页、模型设置、Codex扫描；真实后台连接成功 |
| Codex 只读扫描 | 界面正确读取7个本机模型及现有上下文；没有修改用户配置 |
| Windows 音频 | 主线程枚举后，在工作线程调用 WASAPI loopback 采集成功；仅检查缓冲区，不保存环境音频 |
| Real-ESRGAN | RTX 5080实际处理128×72、8fps、1秒合成视频，输出256×144，保留AAC音轨 |
| GPU运行包迁移 | 离线导出91,359,891字节并导入第二目录，再次实际输出512×288，音轨保留 |
| LibreOffice安装 | 官方26.8.0 MSI SHA256校验、私有目录提取成功；未注册系统Office |
| Office页面渲染 | 两页DOCX和一页含图表PPTX成功渲染；图表Q1=100/Q2=112保留 |
| Office运行包迁移 | 582,577,676字节运行包导出/导入后，再次渲染全部3页成功 |
| 独立Python | 从另一临时工作目录以隔离模式启动打包Python，导入打包核心并访问真实RPC功能成功 |
| 真实安装 | NSIS静默安装到项目隔离目录成功，退出码0；安装版实际窗口启动并连接随包Python后台 |
| 发布环境隔离 | 安装版和便携版均在仅保留Windows系统PATH的环境检查RPC入口，随包FFmpeg实际生成并探测合成音频；追加首次启动的双次设备枚举检查 |
| 0.1.20 发布包 | NSIS 安装包与干净便携 ZIP 构建成功；ZIP 无用户 data、密钥、WebDAV 设置、数据库或备份，解压后独立 RPC 与 FFmpeg 冒烟测试通过 |
| 0.1.21 发布包 | NSIS 安装包与干净便携 ZIP 构建成功；ZIP 6527 项中无用户 data、设置、数据库、WebDAV 凭据或备份；安装版与解压后的便携版均完成 8 项独立 RPC 和包内 FFmpeg 合成音频验证，安装版静默卸载通过 |
| 安装清理 | 隔离测试安装与卸载均退出码0；开发预览进程已停止，交付目录保留安装包与便携包 |

详细功能验证见 [FEATURE_VALIDATION.md](FEATURE_VALIDATION.md)。原始实际GPU与文档证据在项目构建目录 `.build/gpu-validation/validation.json` 和 `.build/document-runtime-validation.json`。

## 未做的实测

- 没有迁入或读取旧 AItext 的 API 密钥。阿里云百炼的真实文件转写、翻译、嵌入、实时转写和悬浮字幕已单独验证，详见 [真实 API 功能测试](REAL-API-VALIDATION.md)；短样本结果不代表所有网络与素材的固定效果。
- 尚未进行一小时实际会议，以及问题结束至回答的 P50/P95 延迟评测。
- Whisper、Qwen3-ASR、SenseVoice、CosyVoice、Pyannote等大模型的安装适配器已经实现；本轮没有下载并实际推理全部大型模型。
- 没有可用的干净 Windows 10 虚拟机；安装与便携包用本机隔离目录验证，Windows10兼容性仍需目标设备实测。

这是一版可运行、可安装的首发版本。上述未实测项不能视为已达到模型效果或性能承诺。
# 2026-09-26 · 0.2.0 项目记账与跨端诊断

- Windows Python 完整套件：520 项通过；并发零收入等最终边界修正后，相关记账/同步/导出/网络诊断 89 项通过。
- 前端 11 组测试通过（使用 esbuild 处理既有 TypeScript 测试导入），TypeScript 和 Vite 生产构建通过。
- Playwright/Edge 真实临时 Python 后端联调：项目创建、普通项目统计、按项目日期导出、编辑保存关闭、过期版本拒绝覆盖且保留草稿、重新载入、同步后列表刷新、真实 HTTP 网络诊断、诊断配置并发更新拒绝通过；940px 页面无横向溢出。
- Python→Android Java→Python 一致性检查：项目、精确金额、不同字段合并、同字段冲突、解决冲突、收入状态以及删除与离线修改竞争通过。
- 最新 Android 调试 APK + `emulator-5560` + 独立合成 WebDAV 真实互通：PC 项目/账目/附件/诊断配置传入 Android，Android 新账目与 PNG 传回 PC 并校验，相同字段冲突保留与解决，删除传播，PC 表格包含手机账目，Android 冷启动自动同步通过。
- 未在用户实体手机或真实私人 NAS 进行测试；测试数据和凭据均独立构造。
