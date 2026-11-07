# AI 聚合

## 账户与应用接入（2026-10-29）

每张账户卡同时展示额度、默认模型和本机调用统计，齿轮进入统一的额度与 API 设置。Kimi Code、千问 Token Plan、OpenCode Go 可同时接入。页面下方「应用接入」提供本地 API URL 与应用 Key 创建/撤销；已移除独立任务输入面板和重复的平台卡片。

「模型库 → 一键获取模型」并行查询各已配置账号的模型目录，提供平台/模型搜索和逐项复制。目录持久保留，重启仍可查看；获取失败保留上次目录并标明失败账号。默认模型只是初始显示，不限制代理可调用的模型；当前代理仅展示适用于对话接口的目录，排除明显的图片生成、视频、语音专用型号。获取模型不发起生成请求；目录可见也不代表每个模型均已完成实际调用验收。外部应用的 `/v1/models` 返回同一份目录，5分钟内复用缓存。

默认 API Base URL 为 `http://127.0.0.1:18791/v1`，可修改端口或停止代理。只监听 127.0.0.1，随工具箱启动/退出。外部应用选择 OpenAI 兼容 **Chat Completions**，填写此地址及创建的 `wt_` 接入 Key。模型 ID 为 `kimi_code/kimi-for-coding`、`qianwen/qwen3.8-flash`、`opencode_go/deepseek-v4.1-flash`、`glm/glm-5.3` 等，可通过鉴权的 GET `/v1/models` 获取列表。相同平台多账号时前缀包含账号 ID，避免混用。

POST `/v1/chat/completions` 支持流式/非流式、消息与图片消息透传、工具声明和工具结果、常见采样参数；具体能力取决于上游模型。当前不提供 Responses、Anthropic Messages、图片生成或语音等其他 API，未支持的路径返回 404。单次模型请求上限为 16 MiB，适用于多图消息；管理 CLI 的 1 MiB 限制不变。并发最多 6，按模型 ID 路由到对应账号，不自动切换供应商或重试。平台错误和断流保留状态，调用与用量计入账户卡。上游错误仅保留有界、脱敏的错误类型、代码、说明及请求 ID，不保存原始错误体或凭据；客户端响应的 X-WinToolbox-Request-Id 可关联本机调用记录。

接入 Key 创建时仅显示一次，数据库只保存 SHA-256 摘要及名称/前缀。撤销后新请求立即失效，已发起的请求可结束。接入 Key 与上游 Key 独立，应用无法通过代理取出平台密钥。代理需要鉴权，拒绝网页 Origin 和外部 Host。CLI `call ai_proxy.status` 获取配置，`call ai_proxy.keys.create --input <JSON>` 创建，`call ai_proxy.keys.revoke` 撤销。原批量调用 CLI 保留供脚本使用。

Kimi 和 Go 默认复用额度账户 Key；千问额度仍用 Cookie 查询，模型调用另存 Token Plan 专用 Key。千问入口使用 `https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1`，与通用按量 Key、旧 Coding Plan 分开。智谱 GLM 额度账户默认指向中国站 Coding Plan 端点 `https://open.bigmodel.cn/api/coding/paas/v4`，模型调用与额度查询共用同一 Key；国际站账户通过模型服务来源自动识别。调用 Key 由 Windows 当前用户 DPAPI 加密，仅存私有 `data/engine.sqlite3` 的 `ai_call_routes_v1`，不写入源码、打包资源、任务参数或公开响应。`dist/` 和数据库被 Git 忽略，源码发布不包含这些凭据。

便携版 `wintoolbox.cmd ai list` 获取账户 ID，`ai models <account_id>` 读取模型，`ai submit --input <JSON路径>` 提交，`ai get <request_id>` 查询。每项可用不同 prompt 或 messages；支持传工具声明并返回工具调用，工具由主代理执行，可将工具结果放入下一批消息。

```json
{
  "request_id": "development-review-unique-id",
  "project": "E:/study/agent",
  "concurrency": 3,
  "tasks": [
    {"account_id": "<Kimi账户ID>", "prompt": "检查给定代码的边界条件"},
    {"account_id": "<TokenPlan账户ID>", "prompt": "为给定函数列出遗漏测试"},
    {"account_id": "<Go账户ID>", "model": "deepseek-v4.1-flash", "prompt": "审查给定接口的调用约定"}
  ]
}
```

相同 request_id 和参数返回原任务，参数冲突报错；不自动重试或跨平台切换。新一轮用新 ID。每批最多 12 项，并发 1–6（默认 3），每项独立超时。单项失败不丢弃其他结果；批任务 completed 只代表各项已返回，须检查 successful/failed 和逐项 status。断流和预算截断保留部分输出并标 error/incomplete。取消停止后续读取，已发给上游的请求可能仍消耗额度。

逐项保留请求模型、实际返回模型、输出、工具调用、原始 usage、耗时、首字延迟与 TPS。缺失 Token 为 null，TPS 是输出 Token 除以包含排队和网络的耗时；套餐现金实际成本未知，actual_cost 为 null。本机调用统计与平台共享额度独立展示。任务文字和结果保存在本机调用记录中。

接口依据：[Kimi Code API](https://www.kimi.com/code/docs/)、[OpenCode Go API](https://opencode.ai/v2/docs/console/go/)。请求使用真实 WinToolbox 客户端身份，上游拒绝时如实报错。

在工具首页打开「AI 聚合」，可星标到侧边栏。不同平台、币种及套餐周期独立展示，不相加成一个总额度。支持多账户、手动刷新及页面可见时每 1 / 5 / 15 分钟自动刷新，默认手动；同一账户至少间隔 60 秒查询。

| 平台 | 查询内容 | 连接方式 |
| --- | --- | --- |
| DeepSeek | 可用、充值和赠送余额，保留 CNY / USD | API Key，或复用工具箱模型设置 |
| OpenCode Go | 5 小时、周度、月度剩余比例及重置时间 | Go API Key，或复用对应模型服务 |
| Kimi Code（中国站） | 5 小时限额、月度共享额度；旧套餐的周限额按接口实际返回展示 | Kimi Code API Key，或复用 api.kimi.com/coding 模型服务 |
| 千问 Token Plan | 控制台实际返回的周期剩余比例、重置时间；套餐名称及到期时间尽力读取 | 千问控制台 Cookie |
| 阿里云 Coding Plan（中国站） | 5 小时、周度、账单月的请求配额及重置时间 | 百炼控制台 Cookie |
| 智谱 GLM（中国站 open.bigmodel.cn / 国际站 api.z.ai） | Coding Plan 的 5 小时与每周剩余比例、重置时间与联网搜索计数；订阅套餐名称与续期时间；标准 API 可用、充值和赠送余额；Token 包余量按平台原始数值 | GLM API Key，或复用 open.bigmodel.cn / api.z.ai 的模型服务；凭据区域自动识别，中国站 Key 误填时自动重试国际站 |
| SiliconFlow（中国站） | 总余额、充值和赠送余额 | API Key，或复用模型设置 |
| Moonshot（中国站） | 开放平台 API 可用余额、现金和代金券 | API Key，或复用模型设置 |
| OpenRouter | 当前 Key 剩余额度及累计消耗；无限额 Key 仅显示消耗 | API Key，或复用模型设置 |

千问新版 Token Plan 与阿里云 Coding Plan 是两个独立接入项。Kimi Code 与 Moonshot 开放平台分别连接，Key 不通用；Moonshot API 余额不代表 Kimi 会员或 Kimi Code 套餐；OpenCode Go 不包含 Zen 钱包余额；OpenRouter Key 额度不代表账户钱包。智谱的套餐额度按平台返回的百分比显示，不换算为请求次数或 Token 数；其用量监控接口在中国站使用裸 Key 鉴权、其余业务接口使用 Bearer，应用已分别处理。

Cookie：登录平台控制台后，在浏览器 F12「网络」中刷新页面，选择控制台 info.json 或额度请求，复制「请求标头」的 Cookie 值。不要把 Cookie 或 API Key 发到聊天里。登录失效时在账户设置中替换。阿里两个控制台的 Cookie 不通用，模型调用 Key 也不能代替控制台会话。

独立填写的凭据使用 Windows DPAPI 加密后存入 engine.sqlite3 的版本表 ai_quota_accounts_v1；复用的模型服务仅保存引用。列表 RPC、任务参数和结果均不含凭据。换 Windows 用户或设备后，独立凭据可能需要重新连接。移除账户会删除本地该账户的凭据与快照，保留模型设置中的 Key。

所有查询走固定 HTTPS 只读接口，不发起模型生成或试探性模型调用，不跟随登录重定向。没有可确认的数字时显示失败，绝不把空值当作 0 消耗或 100% 剩余。查询失败保留上次成功快照、其时间及错误状态。平台未提供某个周期时只展示已有周期。

实现依据与查询口径（2026-10-23 核实）：

- [DeepSeek 余额接口](https://api-docs.deepseek.com/api/get-user-balance/)
- [OpenCode Go 上游实际响应](https://github.com/anomalyco/opencode/issues/50085)，GET /zen/go/v1/usage。
- [Kimi Code 官方会员文档](https://www.kimi.com/code/docs/kimi-code/membership.html)：新套餐无周限额，月度额度与其他会员功能共享。2026-10-25 已用真实账户核实 GET https://api.kimi.com/coding/v1/usages，并对照官方控制台。新版 usages.limit_5h / limit_month_total.used_ratio 是 0–1 的已用比例，优先于旧 limits 取整值；不把 limit_month_code 当成共享月总额度。旧 usage / limits 计数只作为缺失周期的兼容来源，按窗口时长识别，单位为额度单位。此查询接口未在公共 API 文档中承诺稳定性，变更时会显示查询失败并保留旧快照。
- [千问控制台](https://platform.qianwenai.com/home/analytics/token-plan/individual)：已登录页面实际观察 /tokenplan/personal/api/v2/usage 与 subscription 网关响应；per1MonthPercentage 是已用比例（0–1），不猜测 Token 总量。
- [阿里云官方说明](https://help.aliyun.com/zh/model-studio/coding-plan)及[公开控制台接口说明](https://github.com/steipete/CodexBar/blob/main/docs/alibaba-coding-plan.md)。控制台接口可能随平台更新，需要有效登录态。
- [硅基流动文档](https://docs.siliconflow.cn/)，GET /v1/user/info。
- [Moonshot 开放平台](https://platform.moonshot.cn/docs/intro)，GET /v1/users/me/balance。
- [OpenRouter 当前 Key 接口](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)：有 limit_remaining 时按本周期剩余量计算，不用累计消耗推算本周期比例。
- [智谱 Coding Plan 概览](https://docs.bigmodel.cn/cn/coding-plan/overview)与社区实现（[glm-for-copilot 用量客户端](https://github.com/KiwiGaze/glm-for-copilot)、[opencodex GLM 额度支持](https://github.com/lidge-jun/opencodex/pull/2028)）：中国站 open.bigmodel.cn 与国际站 api.z.ai 共用 `/api/monitor/usage/quota/limit`、`/api/biz/subscription/list`、`/api/biz/account/query-customer-account-report`、`/api/biz/tokenAccounts/list/my`；`percentage` 为 0–100 的已用比例，`nextResetTime` 为毫秒时间戳，monitor 接口在中国站使用裸 Key、其余接口使用 Bearer；未在官方文档中承诺稳定的查询接口保留上次快照。

开发验收使用临时目录与 HTTP 固定响应，不读取真实浏览器或 Codex 配置。真实账户连通性须单独核实；固定响应测试不等于每个平台均已完成真实账户验收。
