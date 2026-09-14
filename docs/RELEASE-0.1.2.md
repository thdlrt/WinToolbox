# WinToolbox 0.1.2

删除整个“关于”区。设置现在只有“模型、数据与备份、外观”。模型配置优先展示两个入口：阿里云百炼 API、本地模式。

百炼填写 Key 后应用，自动配置转写、实时识别、翻译、问答、视觉、检索及语音合成。已保存的百炼 Key 复用；其他 API 服务和角色分配保留在高级设置。

| 本地预设 | 语音识别 | 翻译、问答、视觉 | 建议硬件 |
|---|---|---|---|
| 轻量 | Whisper small，CPU int8 | Qwen3.5 0.8B | 8 GB 内存，无需独显 |
| 均衡 | Whisper large-v3-turbo | Qwen3.5 4B | 16 GB 内存、8 GB NVIDIA 显存 |
| 高质量 | Whisper large-v3 | Qwen3.5 9B | 32 GB 内存、16 GB NVIDIA 显存 |

三套均包含 EmbeddingGemma 本地向量检索。硬件要求是建议值，不是实测性能保证。安装预设下载模型，完成后点击使用；不会安装后自动切换正在使用的模式。配音、说话人区分、画面增强等附加运行包仍按需安装。

本地推理使用工具箱管理的独立进程、模型目录和回环连接；未安装时明确报错，不会回退到云模型。媒体任务、实时会话及录音补转写均继承当前模式，轻量预设实际使用 CPU int8。切换模式、恢复备份后，本地服务会按需重新建立。

## 验证

- 109 项自动测试通过，包括模式切换、密钥复用、不调用云端、断流取消和完整安装检查。
- 使用官方独立 Ollama 0.33.3 运行包（校验 SHA256），实际安装轻量整套模型。通过 Windows 本地语音合成产生公开测试句，再完成 CPU 识别、中文翻译、SRT/VTT/JSON 输出；单独验证本地问答和 768 维向量。
- 实测修复了轻量模型偶尔忽略语言代码的问题：翻译提示使用完整语言名称，并使用较低随机性参数。轻量模型仍适合简单任务，复杂回答应核对来源。
- 检查百炼/本地界面、安装状态、主题、外观页面和文件/实时页面，在 1280×860、940×640 下无横向溢出。
- 未使用真实百炼密钥调用计费服务；均衡、高质量模型和长时间实时会话未在本次更新逐一实测。

截图位于 `output/playwright/model-setup/`。真实验证使用 `.build/local-mode-validation/` 隔离目录，未修改已安装程序和个人设置。轻量模型不随安装包重复打包，在软件中按需安装。

## 来源

百炼默认模型及协议依据官方 [Qwen3.8-Flash](https://help.aliyun.com/zh/model-studio/qwen3-8-flash) 和 [语音识别接口](https://help.aliyun.com/zh/model-studio/non-real-time-speech-recognition-for-fun-asr-flash)核对。本地模型规格见 [Qwen3.5 模型库](https://ollama.com/library/qwen3.5/tags)、[EmbeddingGemma](https://ollama.com/library/embeddinggemma)；独立运行包依据 [Ollama Windows 文档](https://docs.ollama.com/windows)。
