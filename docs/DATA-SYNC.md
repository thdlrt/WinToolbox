# 统一数据同步调度

`data_sync.status` 返回本地状态，不访问网络：

```json
{"configured":true,"syncing":false,"last_sync":1720000000.0,"error":null,
 "services":[{"id":"ledger","label":"记账与网络诊断配置","configured":true,"syncing":false,"last_sync":1720000000.0,"error":null}],
 "transfers":[{"id":"relay","label":"文件中转","mode":"manual","configured":true,"syncing":false,"last_sync":null,"error":null}]}
```

时间为 Unix 秒；未成功过是 `null`。`services` 包含 `ledger` 和 `project_memory`。项目记忆使用统一 WebDAV，以及已经启用的 SSH 目标。记账包含项目、条目、附件和网络诊断配置。原有 CRDT 操作、附件校验和冲突解决协议不变。

`data_sync.now {}` 立即唤醒统一调度器并返回状态，不创建任务记录，也不阻塞等待网络。应用启动后自动同步；本机内容变更去抖 1 秒，平时每 60 秒检查远端，失败按 5、10、20 秒递增重试，最多 300 秒。每个服务独立记录失败；旧位置尚未合并或部分 SSH 目标失败不计作完整成功。全局上次成功只有全部已配置服务完成当前待同步变更后才前进；没有配置的服务不制造成功记录。

同一服务的自动和手动同步互斥，运行期间的重复请求合并为一次后续同步。自动运行不生成每分钟任务历史。旧 `expenses.sync` 和 `memory.sync.run` 仍可手动调用，任务会经过同一调度器。原 `memory.sync.configure` 自动开关仅保留输入兼容，不能关闭统一调度；状态总是反映统一间隔。远端 ingest 不触发本机变更钩子；本机编辑、整理产生的新记录会唤醒调度。

## 文件传输边界

`transfers` 为只读汇总，不参与全局自动合并成功时间：

- `relay` / `mode:manual`：用户选文件上传或下载；统一同步按钮不会选择文件或执行清理。
- `filesync` / `mode:rules`：仅按用户保存并启用的规则执行文件复制、覆盖和删除；保留原独立规则检查间隔、预览及冲突保护。

配置备份和配置覆盖仍由用户手动执行；短信不纳入数据同步。

## 生命周期与扩展

服务注册：`app.data_sync.register(id, label, configured_callback, run_callback)`。执行上下文提供 `params`、`check_cancelled()` 和 `progress()`。执行结果中的 `warnings` 或 `migration_pending` 会保留失败状态并重试。

统一 WebDAV 配置保存后调用 `app.data_sync.request()`；旧 `ledger_auto_sync()` 和 `memory_auto_sync_wake()` 保留委托。`data-sync-state.json` 只记录最后成功和错误，不保存业务数据及凭据。关闭应用会停止调度、通知正在执行的自动任务取消，并等待其退出后关闭数据库。恢复备份在 `data_lock` 下调用 `assert_idle()`，运行中直接提示稍后恢复，不持维护锁等待网络。
