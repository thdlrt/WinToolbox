# 硬盘挂载（NetMount）

把 SFTP / WebDAV 服务器目录挂成 Windows 盘符。行为对标本机 RaiDrive 2021.12.35，引擎改用随包的 rclone 1.75.2 + 系统安装的 WinFsp。

## 与 RaiDrive 的对应关系

RaiDrive 的实现（逆向结论，证据见下）：

| RaiDrive | WinToolbox 硬盘挂载 |
| --- | --- |
| CBFS6 驱动（`driver/cbfs.cab`，服务内 `CBFS6Net` 程序集） | WinFsp 用户态驱动 + rclone mount |
| Rebex.Sftp / Rebex.Ftp / Rebex.Http | rclone sftp / webdav 后端 |
| 服务常驻（RaiDrive.Service，LocalSystem） | 工具箱进程内监督线程，随工具箱退出卸载 |
| LiteDB + 注册表 DPAPI blob 存配置 | `data/netmount.json`（DPAPI 密文），不进备份 |
| SignalR 与 UI 通信 | JSON-RPC（同一后端） |

逆向证据：`RaiDrive.Service.x64.exe`（.NET Framework 4.8 程序集，REA `inspect-managed-artifact`）的引用表包含 `Rebex.Sftp`、`Rebex.Http`、`CBFS6Net`、`LiteDB`；`C:\ProgramData\OpenBoxLab\RaiDrive\log\service.log` 记录完整挂载生命周期（`CreateStorage` → `MountMedia` → `MountPoint Add X:`，卷大小恒为 9223372036854775807，`IsNetwork : True`）。注册表 `HKLM\SOFTWARE\OpenBoxLab\RaiDrive\Drives` 的配置是 DPAPI（LocalMachine + 自定义 entropy）blob，entropy 未能离线提取（对两个程序集共 2370 万个 16 字节窗口暴力尝试均不中），因此迁移走服务日志。

## 功能

- **类型**：SFTP（SSH，默认 22）、WebDAV（http/https，默认 80/443，vendor=other）。
- **挂载形态**：网络盘（`--network-mode`），固定盘符，卷标为名称，可选只读。
- **缓存**：`--vfs-cache-mode writes`（默认）+ `--dir-cache-time 10s`，缓存目录在工具箱数据目录。
- **断线重连**：子进程意外退出且存活超过 10 秒时，5 秒后用已存密码重启，300 秒稳定后重置计数，最多 10 次；启动即崩（<10 秒）不重试并给出日志末行。
- **自动挂载**：每条挂载可开「自动挂载」，工具箱启动后 2 秒自动挂载；配合「静默自启」达到 RaiDrive 式开机自动挂盘。挂载随工具箱退出卸载（`close`/`prepare_exit` 钩子）。
- **RaiDrive 迁移**：`netmount.import_raidrive` 解析 `C:\ProgramData\OpenBoxLab\RaiDrive\log\service.log*`，恢复名称/类型/地址/远端路径/盘符/只读（本机实测 4 条全部恢复正确）；RaiDrive 的密码不可导出，导入时逐条补填。
- **WebDAV 同步导入**：`netmount.import_webdav_sync` 读取 WebDAV 备份连接（`webdav.json`），一键填入添加表单并可复用其已保存密码（`reuse_webdav_password`）。

## 凭据与安全

- 密码只存当前用户 DPAPI 密文（`password_dpapi`），文件不在备份白名单内，不随配置备份/同步。
- rclone 子进程的配置通过 `RCLONE_CONFIG_NM*` 环境变量传入；密码先经 `rclone obscure`（stdin 传入，参数不含明文）遮蔽。命令行参数、任务参数、事件与日志中都不出现明文密码。
- 「留空密码保留旧值」与 RaiDrive 一致：同端点+同账户保留，改端点/账户或 `clear_password` 清除。
- 挂载/测试任务用一次性凭据令牌（5 分钟过期）在内存中传递密码。

## 依赖与边界

- **WinFsp** 需要用户安装一次（程序内提供下载页跳转）；rclone 1.75.2 随包提供（`scripts/prepare_netmount.py` 固定版本 + SHA-256）。
- SFTP v1 只支持密码认证（RaiDrive 亦常用）；主机密钥默认接受（rclone 1.75 未配置 known_hosts 时提示式处理）。
- 盘符冲突：与其他挂载条目冲突在保存时拒绝；被系统占用在挂载时报错。
- 修改/删除挂载中的条目会要求先卸载（删除自动卸载）。
- 卸载用 `taskkill /T /F` 结束 rclone 进程树；WinFsp 随之释放盘符（最多等 8 秒）。

## RPC

- `netmount.status {}` → 引擎/WinFsp 状态、已占用盘符、drives（含 state/message/restarts/mounted）。
- `netmount.save {id?, kind, name, host, port?, username?, password?, clear_password?, remote_path?, drive_letter, read_only?, auto_mount?, volname?, cache_mode?, tls?, insecure_tls?, webdav_vendor?, reuse_webdav_password?}` → `{id, drives}`；挂载中拒绝修改。
- `netmount.remove {id}` → `{drives}`；挂载中先卸载。
- `netmount.mount {id, password?}` → 后台任务（30 秒盘符就绪等待，失败含 rclone 日志末行）。
- `netmount.unmount {id}` → 后台任务。
- `netmount.test {id, password?}` → 后台任务，`rclone lsf --max-depth 1` 抽样 5 项。
- `netmount.import_raidrive {}` → `{candidates, log_directory}`；`netmount.import_raidrive.apply {candidates, passwords?}` → 逐条结果。
- `netmount.import_webdav_sync {}` → `{candidate}`（无配置时 null）。
- 事件 `netmount.state {id, state, message}`：状态迁移（mounted/reconnecting/error/stopped）。
