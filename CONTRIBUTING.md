# 参与开发

WinToolbox 使用 Tauri 2、React/TypeScript 和 Python 3.12。请先阅读 [README](README.md) 中的开发说明。

提交改动前，请至少运行与改动相关的 Python 测试和前端构建：

```powershell
.\core\.venv\Scripts\python.exe -m pytest tests -q
Set-Location desktop
npm.cmd ci
npm.cmd run build
```

请勿提交 API 密钥、WebDAV 凭据、`dist/WinToolbox-portable/data`、模型文件、构建产物或个人录音。新增外部运行时或模型时，应固定来源、校验完整性，并在 `docs/THIRD_PARTY.md` 记录许可证。

提交问题时，请说明 Windows 和 WinToolbox 版本、使用的功能、复现步骤及可公开的错误信息。日志可能包含本地路径，请先检查再上传。
