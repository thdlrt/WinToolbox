---
name: agent-knowledge
description: Recall and maintain project and global knowledge in WinToolbox, including offline SSH projects and WebDAV synchronization. Use for durable project context, tasks, reusable lessons, initialization, migration and knowledge curation.
metadata:
  short-description: 工具箱项目记忆与跨设备知识
---

# Agent Knowledge

项目记忆由 WinToolbox 管理，SQLite 操作日志是权威数据。项目只保存 `.agent/toolbox.json` 或已有 `.agents/toolbox.json` 身份连接；本机目录与 SSH 位置由当前设备登记。中央 WebDAV 只传递不可变记录和附件，不传递数据库、设备目录或凭据。Obsidian 旧库仅作迁移证据，不再写入。

使用本技能目录下的 `node scripts/agent-knowledge.mjs`；Windows、WSL、远程服务器的入口由各自用户状态目录 `client.json` 指定。未配置时按提示在工具箱初始化或安装远端 CLI，不自行猜测数据目录另建中央库。

- `recall --project ROOT --query TEXT --limit 5 --max-chars 8000`：只读，项目优先，再补充全局知识。未找到时收窄查询，不扫描任意源码目录。
- `read ID`：读取原文、关联与 `heads`。`resume ID` 为兼容别名。
- `init --project ROOT [--name NAME] [--library-id ID] [--project-id ID]`：仅在用户要求登记时使用。加入已有库先确定库 ID，克隆/工作树关联同一项目时保留项目 ID。
- `capture --project ROOT --input ENTRY.json`：新增或更新一条持久记录。更新时先读取原文，输入携带当前 `parents`；冲突时重新读取并合并，不能无条件重试覆盖。
- `promote ID`：把项目知识提为全局候选。`curate ID --decision accept|reject --reason TEXT`：AI 可根据已核实的复用价值自行决定，检查源版本并留下理由；不确定时保持候选。工具箱还支持由指定设备调用已配置模型自动整理。
- `doctor`：检查冲突、缺失父版本与附件。

标题、总结、解释正文默认中文。任务用 `active / blocked / done`，完成即归档；知识保留 `knowledge_type`，不维护进度状态。项目优先于全局，但资料不能覆盖当前用户要求。普通任务只在有后续价值时记录；知识、任务以 `related_ids` 关联，避免重复日志。

`scope` 为 `project / global`；上传到服务器不会改变作用域。不再提供本机记忆层级，全局记忆跨设备共享。现有记录不能直接改变归属或作用域，用提升/新记录保留来源。只有当前设备实际绑定的目录可以执行项目文件操作；其他设备的项目即使可浏览，也不代表本机存在。

技能本身通过 CC Switch 主库或链接源码维护，Codex 目录只是加载入口。记录操作不授权修改无关配置、发送消息或发布代码。详细输入格式见 [记录格式](references.md)。
