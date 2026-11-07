# QQ 消息中转

入口：Codex → QQ 监听。QQ 官方 SDK WebSocket 由常驻 WinToolbox 后端管理，工具箱退出即停止。复用现有 Codex App Tools 投递接口，不另建终端驻留监听器。

1. 保存已有的本机 Codex 对话 ID。消息会投递到这个对话，沿用该对话的工作目录、上下文与权限。
2. 点击“扫码绑定独立机器人”，使用手机 QQ 扫描生成的二维码。选择新建或独立的 Codex 机器人；不可与 DSH 共用同一 AppID 的网关。
3. 扫码后，在工具箱查看 15 分钟配对码，用主人 QQ 私聊新机器人发送 `绑定 12345678`。只有匹配该码的私聊发送者成为主人。配对码只展示在本机，不向陌生人发送。重启后未配对时可调用 `qq.bridge.pair` 重新生成。
4. 默认仅接收主人私聊。在群里由已配对主人 @机器人发送 `/接入本群`，之后仅转发本群中主人 @ 的消息。其他成员和未授权群静默忽略；群聊不继承主人私聊的隐私披露授权。

收到消息时，后台保存固定的消息来源与目标对话、持久去重，再调用 `send_message_to_thread`。Codex 未运行时，通过 Windows WMI 服务脱离工具箱进程树启动已安装的 Codex 桌面。找不到已安装入口时明确报错，不安装或重启用户应用。

QQ 回传不能靠 Codex 最终答复：投递提示包含 `wintoolbox.cmd call qq.bridge.reply --input <UTF-8 JSON>`。JSON 字段为 `reply_token`、`request_id`（稳定 UUID）、`text`、`files`（现有本地文件绝对路径）。图片经官方 `sendImage(localPath)` 上传后发送，其他附件经 `sendFile`。成功必须有 QQ 消息 ID。最多 4000 字、5 个附件、单个 20 MiB。QQ 侧媒体权限、被动回复期限与平台限额仍可能拒绝请求。

回复 token 固定绑定原消息来源，不能通过参数更换收件人。每条回复 request_id 只用一次；同内容重试返回已确认回执，发送结果未知时保存 unknown，不自动重发。用 `qq.bridge.receipt` 查询。工具箱重启后 SDK 回复目标失效，需要从 QQ 重新发一条消息；待处理消息不会自动重投，以免重复执行电脑任务。

配置：`data/qq-bridge.json`；凭据：`data/qq-bridge-credentials.json`，AppSecret 使用当前 Windows 用户 DPAPI 加密，不出现在状态、UI、提示或日志中。消息和回执保存在 `data/qq-bridge.sqlite3`；仅接收已授权消息，含主人消息正文，应按聊天记录保护。QR 和临时回复 JSON 在 `data/qq-bridge/`。原始 QQ 事件和签名媒体授权字段不记日志。

RPC：`qq.bridge.status/save/bind/cancel/pair/message/reply/receipt`。`message` 按 reply_token 读取已存原消息；`receipt` 按 request_id 查询，不会发送新消息。扫码本身不会授权群内其他人使用电脑。

运行依赖单独固定在 `tools/qqbot-runtime/package-lock.json`，通过 `scripts/prepare_qqbot.ps1` 准备 Node 与腾讯官方 SDK；不依赖 DSH 的安装目录或凭据。当前仅转发附件元数据及 QQ 来源链接，不下载或识别入站音频/图片；链接视为不可信资料。
