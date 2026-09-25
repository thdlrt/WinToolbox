# 记录与部署

新增输入示例：

```json
{"kind":"knowledge","scope":"project","project_id":"项目ID","title":"中文经验标题","body":"结论、适用条件与证据","knowledge_type":"procedure","related_ids":[]}
```

更新输入保留原 `id` 和元数据，并添加 `parents: [读取时的 heads]`。正文不会隐式覆盖并发修改。任务 `kind: task` 并使用 `status: active|blocked|done`。附件使用 `attachments: [{hash, name, size}]`，哈希必须对应已导入的附件。

客户端配置：Windows `%LOCALAPPDATA%/WinToolbox/agent/client.json`；Linux/SSH `${XDG_STATE_HOME:-~/.local/state}/wintoolbox-agent/client.json`。字段 `python`、`core`、`store` 分别为解释器、含 toolbox 包的目录和记忆数据库目录。环境变量 `WINTOOLBOX_MEMORY_CONFIG` 可显式选择客户端配置。WSL 可由该配置调用 Windows 解释器，共用 PC 的同一个库与设备身份。

远端安装后可以直接运行：

```sh
PYTHONPATH=/指定存储目录/runtime python3 -m toolbox.project_memory --store /指定存储目录/data init --project /远端项目目录 --library-id 库ID --project-id 项目ID
```

初次连接由 PC 通过 SSH 交换记录；之后服务器断开 PC 仍可 capture，恢复连接后再次交换。远端无需保存 WebDAV 密码。不要删除离线操作日志；完成、拒绝和归档均保留历史版本。

Markdown 导出是可携带副本；修改导出文件不会自动回写数据库。旧 Obsidian 文档原样保留，迁移后若需回退应先导出新产生的记录，不能用旧快照覆盖新数据。
