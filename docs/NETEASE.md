# 网易云音乐

工具首页进入「网易云音乐」。选择 NCM 文件或文件夹和输出目录，选择统一 MP3 或还原原始格式，点击开始转换。目录递归处理 NCM；保留源文件，同名结果自动编号，任务中可打开音频与逐文件报告。还原使用 GitHub ncmdump 1.5.1，转 MP3 使用随附 FFmpeg。

账号区会显示本机网易云保存的用户 ID。可以直接获取公开歌单、喜欢的音乐和歌单歌曲，搜索、多选并导出 JSON / CSV。标称数量和接口实际返回数量不一致时会提示不完整，不补造缺失记录。

私密内容和下载权限使用「扫码登录」，在手机网易云 App 中确认。凭据以 Windows DPAPI 保存在本机 data/netease/auth.dpapi.json。断开连接只删除该凭据。下载使用平台下载接口，不自动改为试听、其他音源或解锁服务；失败项保存在逐歌曲报告。输出目录和格式与本地转换共用。

技能 `netease-music` 的真实源码在本项目 skills/netease-music，CC Switch 主库链接到该目录并登记启用 Codex。命令入口 `node scripts/netease.mjs`，例如 `likes`、`export --kind likes --output-dir DIR`、`download --ids ID1,ID2 --output-dir DIR`、`convert FILE --output-dir DIR`。CLI 不打开主引擎数据库，可与桌面端共存。

BetterNCM 不是连接的必要条件。本机界面核实有 BetterNCM 1.3.4；本实现直接连接网易云接口，不改变客户端、插件或登录状态。
