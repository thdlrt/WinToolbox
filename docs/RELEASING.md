# 发布约定

2026-09-29 起，公开 Release 只提供 Windows 安装包 `WinToolbox_<版本>_x64-setup.exe` 和对应校验文件 `SHA256SUMS.json`，不再制作或上传便携 ZIP。已发布旧版本的附件保留。

每次发布仍需用户当次明确授权。先完成版本号、发行说明、测试、构建和文件校验，再上传草稿附件、核对远端哈希并发布。不要用 `dist/*` 一类通配符上传，避免带入旧版本 ZIP 或本地数据。

- 正式构建：`scripts/package.ps1 -SkipInstall`。
- 本地迭代：`scripts/package.ps1 -PortableOnly -SkipInstall`，更新固定的 `dist/WinToolbox-portable`，保留其 `data`。
- 本地启动：`scripts/start-portable.ps1`；只显示悬浮球使用 `-Silent`。

本地便携目录用于开发验证，与公开发行格式分开管理，不作为发布资产。历史版本发行说明记录当时产物，不按新约定改写。
