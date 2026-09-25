# 独显省电守卫

入口：首页 → 独显省电守卫，星标可固定到侧边栏。面向 Windows 11 的核显 + NVIDIA 独显笔记本，包括 Ryzen AI 9 365 + RTX 5070 的灵刃 14。

## 使用

1. 保留 Optimus / 混合输出；先断开独显直连显示器做对比。
2. 点击“检查独显”，添加希望使用核显的 `.exe`。核对 Windows 图形设置中“节能”对应 AMD 核显。
3. 启用守卫。拔电后约 5 秒内为名单内应用设置省电 GPU 偏好；已运行的应用需要自行重启。
4. 接电、停用或从托盘退出工具箱后恢复原偏好。关闭主窗口仅隐藏到托盘，守卫继续运行；下次启动需重新启用。

这是应用偏好管理，不是固件纯核显模式。CUDA、显式选卡、独显直连显示器可能不遵循偏好。工具不会强制结束进程或禁用显卡，不能保证独显断电。

## 自身开销与读数

默认不加载 NVML，不运行 nvidia-smi，不查询独显温度、时钟或功率，不创建 D3D 设备。显卡 DXGI 标识每个后端会话只枚举一次；驱动或显卡拓扑变化后需重启工具箱。

启用后每 5 秒读取系统电源状态；仅在电池供电时每 30 秒采集一次 Windows GPU 计数器。停用后后台线程休眠。页面可见时每 15 秒刷新电池数据，离开或隐藏后停止界面刷新。

- 显示 Windows 提供的电量、整机放电功率、剩余续航。系统未提供续航时，按剩余电量和当前放电率推算并标注；不可用时显示“未提供”。
- CPU 独立功率没有通用的无驱动读取实现，显示“未读取”；不安装内核驱动，不用整机功率冒充 CPU 功率。
- 页面仅保留“检查独显”入口，不读取独显功率，避免传感器查询唤醒独显。
- “设置与说明”内的软件渲染是可选排查项：托盘退出并重启后，只为本工具箱 WebView2 添加 `--disable-gpu`，不修改系统环境。可能增加 CPU 开销、降低视频或字幕渲染性能，不保证更省电。只影响界面，本地 AI 模型仍可能使用独显。

Windows 性能计数器是低侵入途径，但仍需在目标机验证驱动行为，不能承诺绝不唤醒。显存分配可能因驱动重复统计而偏大；0% 利用率不证明断电。活动提醒在工具箱内显示，不是 Windows 系统通知。

## 恢复与存储

名单、恢复记录、软件渲染选项存于 `%LOCALAPPDATA%/WinToolbox/gpu-guard/`，不进入便携数据和 WebDAV 同步，以免把本机程序路径应用到其他机器。

写入 `HKCU\Software\Microsoft\DirectX\UserGpuPreferences` 前，先原子写入并刷盘保存原值与预期新值。仅替换 `GpuPreference` 字段，保留其他字段。恢复时只恢复仍等于工具写入值的项目；用户或其他工具后来修改的值保留并提示。本机独占锁防止多个实例争用记录。

异常终止可能使省电偏好暂时保留。下次启动并打开本页时先恢复，再保持守卫关闭。恢复失败会报错并保留记录供重试；不要在恢复完成前手动删除上述目录。

## 验证范围

开发台式机已验证 NVIDIA/AMD LUID 映射与 Windows 性能计数器读取。电池切换和故障恢复使用临时目录、模拟电池及注册表测试，不修改开发机实际显卡偏好。尚未在目标灵刃 14 验证电池读数、独显休眠或续航改善。

参考：[Windows 图形偏好优先级](https://www.nvidia.com/content/Control-Panel-Help/vLatest/en-us/mergedProjects/nv3d/Setting_the_Preferred_Graphics_Processor.htm)、[电池状态接口](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-system_battery_state)、[WebView2 性能建议](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/performance)。
