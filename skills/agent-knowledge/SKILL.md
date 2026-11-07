---
name: agent-knowledge
description: Recall or maintain durable project knowledge, explicit preferences and task recovery points in WinToolbox. Use for requested memory, project initialization, knowledge review or history-dependent work; ordinary self-contained tasks do not require recording.
metadata:
  short-description: 工具箱项目记忆与跨设备知识
---

# Agent Knowledge

WinToolbox 管理持久项目知识、明确偏好和任务恢复点。SQLite 操作日志是权威记录；AGENTS.md 与 Markdown 是文件副本，修改它们不等于写入知识库。普通自包含任务不要求召回或记录。

## 召回与采用

项目身份来自 `.agent/toolbox.json` 或已有 `.agents/toolbox.json`。使用本技能目录的 `node scripts/agent-knowledge.mjs`，运行时由设备的 `client.json` 指定；配置缺失时报告，不猜路径另建库。

`recall --project ROOT --query TEXT --limit 5 --max-chars 8000` 返回命中片段。仅对当前问题定向检索；采用关键条件或修改记录前，用 `read ID --current` 读取完整当前条目。处理冲突用 `read ID` 查看版本，不依据截断片段覆盖知识。

项目知识优先于冲突的全局参考，但不能覆盖用户当前要求。来源、证据和使用资格分开：论文报告不等于本项目实测，模型推断不等于用户要求，active 不等于已验证。新知识和实质修订保留来源、适用条件及治理字段，格式见 [记录与部署](references.md)。

## 记录与维护

- 只有后续复用价值或用户要求时才保存。更新沿用已有 ID，先 read 当前版本，再以当前 heads 作为 parents 提交；并发冲突重新读取合并。
- 保存项目规则、同步 AGENTS.md、capture 输入和写后核验：按需读 [保存流程](references.md#保存项目规则与写入流程)。用户只要求改文件时不额外写知识库。
- `init` 仅用于用户要求的项目登记；`promote ID` 提为全局候选，`curate ID --decision accept|reject --reason TEXT` 根据已核实复用价值决定，不确定则保留候选。
- `doctor` 检查冲突与缺失数据；`sweep --project ROOT --checkpoint FILE` 增量巡检，写检查点但不改知识、不调模型。无新问题保持静默，不自行创建调度。

候选、隔离、退役、替代、拒绝及冲突记录不进入默认召回；候选用 `--include-candidates` 显式检索。到期、未使用、单次失败只是复核线索，不能自动删除或全库重写；处置与恢复见 [记录与部署](references.md)。

标题与说明默认中文。任务用 active/blocked/done，完成即归档；知识用 knowledge_type，不维护任务进度，以 related_ids 关联。scope 为 project/global；上传不改变作用域。只在当前设备明确绑定的项目目录执行文件操作，旧 Obsidian 库不再写入。

技能由 CC Switch 主库或链接源码维护。记录操作不授权无关配置、发消息或发布代码；最终分别说明文件、知识库及同步的实际结果。
