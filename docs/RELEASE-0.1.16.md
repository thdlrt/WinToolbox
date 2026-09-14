# 0.1.16

- 增加回家 VPN：FN Connect 原生认证、短期会话续登、WebSocket TCP 隧道、仅回家/全局出口、SOCKS5。
- 集成 Mihomo 1.19.30 TUN 核心，按 NAS 允许网段生成路由，排除中继入口以避免回环。运行租约过期会停止 TUN。
- 主窗口关闭默认隐藏到托盘，托盘退出停止后台。
- 飞牛插件支持隧道开关、允许网段和全局出口权限，修改后断开旧隧道。

验证：真实 FN Connect 中继访问家中路由器 HTTP 200；SOCKS5 访问互联网的出口与 NAS 出口一致；移除短期 Cookie 后自动续登通过。TCP 试验版不支持 UDP/IPv6、广播发现或双重验证。Android APK 尚未实现。

TUN 实测：Windows 管理员授权后内核 running，未配置应用代理的 HTTP 请求返回 200，客户端隧道上行增加 141 字节；停止后内核状态 stopped。此项验证仅回家模式；全局出口已通过 SOCKS5 验证，完整 UDP/IPv6 VPN 不在本版范围。
