# 接入证据与边界

实现：WinToolbox `core/toolbox/features/netease.py`、`core/toolbox/netease_cli.py`；桌面 `desktop/src/pages/NetEase.tsx`。

连接直接使用网易云 `music.163.com/weapi/`，AES-CBC / RSA 请求编码参照当前 [API Enhanced 源码](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/main/util/crypto.js) 的协议，使用工具箱现有 cryptography，不部署第三方 API 服务器，不向第三方转交账号凭据。用户 ID 从 `%LOCALAPPDATA%/Netease/CloudMusic/info` 只读提取。网页版扫码会建立独立登录态，桌面客户端的 localdata 是私有存储，当前没有声称复用了桌面 Cookie。

接口包括 nuser/account/get、user/playlist、v6/playlist/detail、v3/song/detail、login/qrcode/unikey、login/qrcode/client/login、song/enhance/download/url。平台接口可能变化；错误时保留用户原文件和已完成的产物，核对上游源码后再修复。

2026-09-30 本机在线实测：客户端 ID 612988742，115 个歌单，喜欢歌单 ID 949673609；接口显示 trackCount=1038，但匿名返回 1033 个 trackIds，complete=false。该记录是测试时的快照，每次应实时查询，不把它作为永久数量。扫码返回等待扫描 code=801，完整登录与受权限限制的下载需用户扫码后再验证。

[BetterNCM 插件库](https://github.com/BetterNCM/BetterNCM-Plugins) 中登记了 LibEAPIRequest；[ActionEnhancement](https://github.com/BetterNCM/ActionEnhancement) 描述有下载操作增强。它们不是开箱即用的外部账号列表/批量下载服务，存在版本依赖。本机实际客户端界面已核实安装 BetterNCM 1.3.4，插件页列出 PluginMarket 与 InfLink；未发现已对外开放的账号列表/下载 API 服务。默认 C:/betterncm 路径不存在并不能证明插件未安装，应以实际界面为准。

转换使用 [ncmdump 1.5.1](https://github.com/taurusxin/ncmdump/tree/1.5.1)，MIT 许可，exe 原样分发；发布 ZIP SHA-256 为 `bb849221c06b8fdbff42aefb86baea9c07256568658d80f4be72a39a2a1632dc`，来源与许可随工具箱放在 tools/ncmdump。转换本地 NCM 不需要网易云登录。FLAC 转 MP3 是有损转码，已是 MP3 时不再次编码，保留 ncmdump 输出中的元数据和内置封面。

2026-09-30 登录排查：旧 type=1 扫码在真实账号确认时返回 8821，提示切换方式/升级版本。已改为当前上游 type=3，并保留二维码生成响应中的 NMTID 会话信息；会话文件用 DPAPI 保存。8821 必须如实反馈，不持续轮询，不绕过行为验证；必要时让用户在官方网页完成登录后，用 cookie-import 导入本地文件。

真实扫码验收：改用 type=3 并保留 NMTID 后，用户扫码返回 803，账号接口验证 thdlrt / 612988742。成功下载喜欢列表中 Devil Woman (22498556) 并保存为真实 MP3，ffprobe 时长 214.573 秒。登录后返回 1041 个歌曲 ID，而 trackCount=1038，仍标注 complete=false；不强行删掉差异记录。
