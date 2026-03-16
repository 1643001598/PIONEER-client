# reflex-cil

reflex-cil 是 PIONEER-client 的 Reflex 前端实现，负责：

- 显示对局状态（时间、基地/前哨血量、经济、科技、伤害、部署、飞镖灯态等）
- 发送控制指令（CommonCommand、DartCommand、部署、性能体系等）
- 启动并承载图传播放链路（UDP 3334 -> 转码 -> WS 8765 -> 页面 MSE）

## 目录说明

- reflex_cil/reflex_cil.py
   - 页面布局与状态机（DashboardState）
- reflex_cil/protocol_bridge.py
   - MQTT 协议桥（订阅下行、发送上行）
- reflex_cil/video_server.py
   - 图传服务：UDP 帧重组 + ffmpeg 转码 + WebSocket 广播
- assets/styles.css
   - 页面样式
- assets/video-player.js
   - 浏览器端 MSE 播放器
- requirements.txt
   - Python 依赖

## 快速启动

```powershell
cd CustomClient\PIONEER-client\reflex-cil
pip install -r requirements.txt
reflex run
```

## 依赖

requirements.txt：

- reflex>=0.6.0
- paho-mqtt>=2.1.0
- protobuf>=5.29.0
- websockets>=12.0

系统依赖：

- ffmpeg（需在 PATH 中，供 video_server.py 调用）

## 协议与消息

MQTT 默认连接：127.0.0.1:3333

下行订阅：

- GameStatus
- GlobalUnitStatus
- GlobalLogisticsStatus
- DeployModeStatusSync
- DartSelectTargetStatusSync
- RobotRespawnStatus
- RobotDynamicStatus
- RobotStaticStatus

上行发送：

- CommonCommand
- RobotPerformanceSelectionCommand
- HeroDeployModeEventCommand
- RuneActivateCommand
- DartCommand

## 图传机制

1. video_server.py 监听 UDP 0.0.0.0:3334
2. 解析每个包前 8 字节头部：frame_id(2B) + slice_id(2B) + frame_total(4B)
3. 重组完整 HEVC 帧后写入 ffmpeg
4. ffmpeg 输出分片 MP4（H.264）
5. 通过 WS 0.0.0.0:8765 广播给页面
6. video-player.js 使用 MSE 追加播放

## 调试建议

- 开启协议日志：
   - 在 protocol_bridge.py 将 ProtocolBridge.debug 设为 True
- 图传链路验证：
   - 运行 ../debug/debug_video.py
   - 观察 UDP 包统计与 WS 二进制帧输出
- 无画面时排查顺序：
   - ffmpeg 可执行
   - UDP 3334 有数据
   - WS 8765 可连接且有二进制输出
   - 浏览器控制台中 VideoPlayer 相关日志



# 页面加载调用栈
1) 模块导入阶段（先于页面 on_load）
入口：reflex_cil.py:7
调用链：
video_server.py:278
video_server.py:87
启动线程 video_server.py:202
启动线程 video_server.py:269
在线程内 asyncio.run(video_server.py:257)
说明：这一段在模块导入时就会执行，不依赖页面点击。

2) 页面 on_load 阶段
注册位置：app.add_page(... on_load=[DashboardState.init_data, DashboardState.sync_loop])

on_load 调用链 A（协议初始化）：

DashboardState.init_data
bridge.connect
MQTT client.connect + loop_start
返回连接状态字符串写入 reflex_cil.py:63
on_load 调用链 B（后台状态同步）：

DashboardState.sync_loop
周期调用 bridge.poll
更新页面状态并执行 reflex_cil.py:66
补充：MQTT 成功后触发的 ProtocolBridge._on_connect 属于 MQTT 后台线程回调，不在 on_load 同步主栈中。