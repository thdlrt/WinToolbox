---
name: netease-music
description: 获取网易云音乐账号的歌单和喜欢的音乐，导出 JSON/CSV，下载账号有权限的歌曲，并用 ncmdump 将本地 NCM 还原或转为 MP3。用于网易云音乐列表、下载与格式转换工作。
---

# 网易云音乐

通过本目录的 `node scripts/netease.mjs` 调用 WinToolbox 的实际实现。默认使用主库源码关联的便携版，`WINTOOLBOX_HOME` 可指定工具箱源码或便携版；`WINTOOLBOX_DATA_DIR` 可指定数据目录。脚本与桌面端共享网易云登录态，不启动第二个后台引擎、不修改运行中任务状态。

- `status`：检测已登录账号、本机网易云用户 ID 和 ncmdump。只有 `authenticated=true` 表示已验证登录；`local_uid` 只是客户端保存的 ID。
- `playlists [--uid ID]`、`likes [--uid ID]`、`tracks --id PLAYLIST_ID`：在线读取列表。未登录时可用客户端 ID 或指定 ID 读取公开内容。喜欢列表通过自己创建的 `specialType=5` 歌单识别，不按歌单名称猜测。详情分批补全并保持原 ID 顺序。
- `export --kind likes|playlists|playlist [--id ID] [--uid ID] --output-dir DIR`：保存 JSON 和含 BOM 的 CSV。检查 `complete`、`track_count` 和实际 `tracks` 数量；有缺失时明确说明，不报告完整备份。
- `login`：生成二维码并返回 `key`、`qr_path`（三分钟有效）。将本地二维码图片展示给用户，请用户使用网易云 App 扫码确认；再执行 `qr-check --key KEY`。`login --wait` 可自动等待。扫码是用户完成的登录步骤，不能声称已连接直到接口验证成功。过期时重新生成；用户未回复时不要不断轮询或生成二维码。
- `cookie-import --path FILE`：可导入用户提供的单行 Cookie 请求头文件。不要把 Cookie 放进命令参数、聊天、日志或源码。验证成功后才保存。凭据使用当前 Windows 用户的 DPAPI 加密，保存在工具箱数据目录 `netease/auth.dpapi.json`，不属于 WebDAV 配置同步。
- `download --ids ID1,ID2 --output-dir DIR [--format mp3|original] [--bitrate 320]`：下载明确选定歌曲，默认不批量下载全部喜欢列表。仅使用网易云下载接口实际返回的地址；不转用播放接口、第三方解锁或替代音源。下载会核对真实音频和时长，试听、无权限和下架项写入逐歌曲报告。部分成功时保留已完成产物并报告失败，不算全量完成。
- `convert FILE_OR_DIR ... --output-dir DIR [--format mp3|original] [--bitrate 128|192|256|320]`：使用上游 `taurusxin/ncmdump 1.5.1`（固定发布包 SHA-256）批量转换，目录递归查找 NCM。`original` 还原原编码；`mp3` 对 FLAC 使用 FFmpeg 转码，已有 MP3 直接保留。原文件保留，同名输出自动编号。返回逐文件报告及真实音频产物。
- `disconnect`：仅删除工具箱本地凭据，不退出网易云桌面客户端。

使用绝对路径和参数数组。下载、转换、导出运行进度在 stderr，stdout 是 JSON；错误退出码非零，部分成功的产物也会输出。桌面界面入口为工具首页的“网易云音乐”，含文件选择、输出格式、扫码、歌单、歌曲多选和任务结果。

BetterNCM 只是可选客户端插件平台，不是必需的服务。当前连接直接请求网易云接口，避免因插件/客户端版本不匹配导致必须降级。需要诊断 BetterNCM 时阅读 [接入证据与边界](references/integration.md)，不要为了列表读取自动安装注入 DLL 或降级网易云。
