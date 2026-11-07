# WinToolbox CLI

先打开桌面工具箱，再运行便携目录中的 `wintoolbox.cmd`。CLI 连接同一进程的后台，使用相同的数据锁、校验、后台任务、附件复制与 WebDAV 同步；不会另开后台或直接改数据库。结果为 UTF-8 JSON，失败退出码为 1。`--help` 无需启动工具箱。

```powershell
$tool = 'E:\code\other\WinToolbox\dist\WinToolbox-portable\wintoolbox.cmd'
& $tool status
& $tool methods --prefix expenses.
& $tool expenses projects
& $tool expenses list --month 2026-10 --project-id 00000000000000000000000000000001
& $tool quota list
& $tool quota refresh
& $tool ai list
& $tool ai models <账户ID>
& $tool --wait-timeout 600 ai submit --input E:\tmp\model-tasks.json
& $tool ai get <request_id>
& $tool jobs get <任务ID>
& $tool jobs wait <任务ID>
& $tool jobs cancel <任务ID>
```

可通过 `call <方法> --input 参数.json` 调用 `methods` 列出的其他后台方法；短参数可用 `--params '{}'`。加 `--wait` 会等待返回的后台任务，输出最终任务记录（结果在 `result` 字段）。RPC 参数见 [接口约定](CONTRACT.md)。CLI 不提供原生窗口、系统授权弹窗的自动操作。

## 带附件入账

推荐将参数保存为 UTF-8 JSON 文件，避免命令行引号与文件路径转义问题。金额使用两位小数字符串，币种分别统计。`waiting` 表示等待报销，`split_enabled: false` 只排除均摊，仍计入消费与报销。

```json
{
  "request_id": "7eb00023-d9ae-4626-92bd-fdb9119f675e",
  "project_id": "00000000000000000000000000000001",
  "title": "AI 订阅",
  "date": "2026-10-25",
  "amount": "199.00",
  "currency": "CNY",
  "entry_type": "expense",
  "status": "waiting",
  "category": "AI订阅",
  "split_enabled": false,
  "notes": "发票信息及用途",
  "attachments_add": [{"path": "C:/票据/发票.pdf", "kind": "invoice"}]
}
```

```powershell
& $tool expenses save --input C:\票据\entry.json
& $tool expenses get <条目ID>
```

保存使用 `expenses.commit` 一次提交字段与附件，默认等待完成。`--no-wait` 仅返回任务记录。更新条目时携带 `id` 和最新 `revision`；附件类型为 `invoice`、`receipt`、`payment` 或 `other`，路径会复制到工具箱私有存储。`attachments_remove` 为要移除的附件 ID 数组。

同一保存请求重试必须复用相同 `request_id` 和参数，防止重复入账。不同交易或修改后的内容使用新 UUID。省略时 CLI 会生成 UUID，并在发送前写到 stderr；可靠自动化应预先写入输入文件。等待超时不表示保存失败，先用任务 ID 和 `jobs get/wait` 查询。

全局选项须放在命令前：`--data-dir <目录>`、`--timeout 30`、`--wait-timeout 120`。便携入口已指定相邻 data 目录；开发环境可运行 `python -m toolbox.cli --data-dir <目录> ...`，安装 Python 包时也提供 `wintoolbox` 入口。

## 本机连接

后台只监听 `127.0.0.1` 的随机端口；每次启动轮换随机凭据，凭据使用当前 Windows 用户 DPAPI 加密，存于不参与备份和同步的 `data/cli-runtime.json`。不记录请求凭据、参数和返回数据；拒绝浏览器 Origin、外部 Host、非 JSON 和超过 1 MiB 的请求。正常退出时清除连接文件。CLI 忽略 HTTP 代理环境变量，只直连本机。

此接口向同一 Windows 用户下的 CLI 开放已有后台能力，包含修改、删除、模型请求等操作，授权范围由调用者负责。`app.prepare_exit` 由原生桌面生命周期管理，不对 CLI 开放。桌面程序未运行、用户不一致、数据目录不一致或连接过期时会明确报错，不会悄悄建立第二份数据。
