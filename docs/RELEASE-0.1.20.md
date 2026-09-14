# WinToolbox 0.1.20

WinToolbox 的首次公开版本。它把语音、字幕、口语练习和日常效率工具放在一个可安装、可迁移的 Windows 桌面应用中，配置与个人数据默认保存在本机。

## 主要功能

- 音视频转写、翻译、字幕编辑与导出，支持阿里云百炼 API 和三档本地模型预设。
- 稳定分句的实时双语字幕悬浮窗，可自动识别语言、调整字号/透明度并控制鼠标穿透。
- 实时助手可预先索引少量文档，在会话中按所选资料库检索并回答。
- 口语练习以段落和分类管理朗读缓存，支持片段、音色、音标、循环播放和重新生成。
- 内置学术汇报音标课程与 45 段离线示范音频。
- 按月记账，支持支出、报销状态、转账收入、附件、区间汇总和均摊对账。
- WebDAV 快照同步设置、缓存和应用数据；API 密钥仅在用户明确选择并设置备份密码后进入加密备份。
- Codex 模型扫描与上下文切换、文件整理、可安装工具包、Shizuku 无线配对等扩展工具。

回家 VPN 的 TCP 隧道在本版修复单向关闭时响应被截断的问题，并增强认证保活、DNS 缓存、失败重试和慢链路写入保护。该功能需要 NAS 局域网桥 0.4.1；当前仍仅支持 IPv4 TCP，不支持 UDP/QUIC。

## 下载

- `WinToolbox_0.1.20_x64-setup.exe`：Windows x64 安装程序。
- `WinToolbox-0.1.20-portable.zip`：完整解压后运行 `WinToolbox.exe`，个人数据保存在相邻 `data` 目录。
- `SHA256SUMS.json`：上述两个文件的 SHA-256 校验值。

首次使用请阅读 [README](https://github.com/thdlrt/WinToolbox#readme) 和 [图文使用手册](https://github.com/thdlrt/WinToolbox/blob/main/output/pdf/WinToolbox_%E5%9B%BE%E6%96%87%E4%BD%BF%E7%94%A8%E6%89%8B%E5%86%8C%E4%B8%8E%E8%AE%BE%E8%AE%A1%E8%AF%B4%E6%98%8E.pdf)。第三方运行时和资源的许可证见 [THIRD_PARTY](https://github.com/thdlrt/WinToolbox/blob/main/docs/THIRD_PARTY.md)。

## 验证与限制

- 336 项 Python 测试、TypeScript 检查、Vite 生产构建和 Rust release 构建通过。
- 便携 ZIP 已检查，不包含开发者的 `data`、密钥、WebDAV 配置、数据库、备份或个人录音；解压后独立 Python RPC、FFmpeg 与 FFprobe 冒烟测试通过。
- 支持 Windows 11 x64；Windows 10 与所有音频设备组合仍需更多实机验证。
- 云端模型的费用、可用区域和最终效果取决于用户账号、素材与网络；大型本地模型需自行下载并满足对应硬件要求。
