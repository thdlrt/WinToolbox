# 统一配置备份 v2

应用数据自动合并同步；配置备份仅在用户操作时上传或恢复。两者共用 WebDAV 连接和根目录，但不会相互替换数据。

新备份位于 `<root>/config-backups/shared/config-{windows|android}-YYYYMMDD-HHmmss-32hex.wtconfig.json`，每次创建新文件，不覆盖其他设备备份。

AES-256-GCM；PBKDF2-HMAC-SHA256，200000次，salt16字节、nonce12字节，AAD `wintoolbox-config-backup-v2`。密码为当前WebDAV密码。完整密文上限4MiB。密文对象字段为format/version/salt/nonce/ciphertext，其中format=`wintoolbox-config-backup-encrypted`、version=2，二进制值Base64编码。

明文字段为format/version/platform/created_at/config/ai，format=`wintoolbox-config-backup`、version=2，created_at为Unix秒。ai沿用AI配置v1的供应商、角色和密钥结构。config是源平台设置：Windows只允许settings.json、orb-settings.json、memory-cleaner.json、fnconnect-tun.json；Android使用AndroidConfigBackup v1快照。

同平台恢复设置和共享AI，异平台仅恢复共享AI。接收端保留备份缺少的本机功能角色与其供应商、密钥；同ID供应商发生碰撞时为保留的本机角色复制供应商，避免引用另一个服务或密钥。每个平台只展示自己支持的功能。

恢复先下载解密并预览，确认令牌绑定当时的本机配置和连接；修改后需重新预览。恢复失败回滚，恢复副本只保留本机受保护的密钥或加密备份，不落地明文密钥。Windows恢复需重启使所有设置生效。

旧Windows .wtbak配置/完整备份继续只恢复配置；旧完整备份的业务数据不通过此入口导入。旧Android配置JSON和旧ai-config/config-v1.json仍可读取。旧Windows加密备份需要原备份密码。

文件中转、记账附件、项目记忆、短信取件记录均不装入配置备份。当前WebDAV连接不从备份覆盖。
