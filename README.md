# PIONEER-client

PIONEER-client 是 RoboMaster 自定义客户端实现，基于 Reflex + MQTT + Protobuf，包含：

- 协议桥接与界面联动
- 图传接收与网页播放
- 联调调试脚本

## 当前目录结构

- debug/
	- debug_video.py
	- 用于调试图传链路：监听 UDP 3334，并作为客户端连接 WS 8765 打印数据摘要。。
- protocol/
	- messages.proto
	- messages_pb2.py
	- Python 侧协议定义
- reflex-cil/
	- Reflex 客户端工程
	- 包含 UI、MQTT 协议桥、视频服务、静态资源
- require.md
	- 客户端行为要求说明

## 运行依赖

- Python 3.10+
- Node.js 18+（用于 SharkDataSever 测试端）
- ffmpeg（需要在 PATH 中）

## 联调启动顺序

1. 启动 SharkDataSever（提供 MQTT + UDP 视频模拟流）

```powershell
cd CustomClient\SharkDataSever
.\runner.bat
```

2. 启动 Reflex 客户端

```powershell
cd CustomClient\PIONEER-client\reflex-cil
pip install -r requirements.txt
reflex run
```

3. 可选：启动图传调试脚本（用于确认 UDP/WS 数据是否在流动）

```powershell
cd CustomClient\PIONEER-client\debug
python .\debug_video.py
```

## 默认端口

- MQTT：3333
- UDP 图传输入：3334
- Reflex 内部视频 WS：8765
- Reflex Web 页面：默认 3000（以 reflex run 输出为准）

## 图传链路说明

发送端（SharkDataSever 或真实设备）将 HEVC 分片通过 UDP 3334 发入客户端。

在 reflex-cil 内部：

- video_server.py
	- 重组 UDP 分片为完整 HEVC 帧
	- 调用 ffmpeg 转码为浏览器可播放的分片 MP4（H.264）
	- 通过 WebSocket 8765 广播给页面
- assets/video-player.js
	- 使用 MSE 接收并追加视频数据
	- 渲染到页面左上图传区域

## 备注

- 若图传无画面，优先检查：
	- ffmpeg 是否可执行
	- 3334 是否有 UDP 包
	- 8765 是否有二进制数据输出
	- 浏览器控制台中 VideoPlayer 日志
