# UDP 包到浏览器播放的线程/对象关系图

本文说明 reflex-cil 图传链路中，从 UDP 分片到浏览器播放的主要对象、线程归属和数据流向。

```mermaid
flowchart LR
    subgraph Producer[发送端]
        UdpSource["机器人/上游视频源<br/>UDP HEVC 分片"]
    end

    subgraph PythonProcess[Python 进程: reflex_cil.video_server]
        direction LR

        subgraph MainThread[主线程]
            Start["start()"]
            FfmpegProc["_ffmpeg_proc<br/>subprocess.Popen"]
            FfmpegLock["_ffmpeg_lock"]
            StartLock["_start_lock"]
        end

        subgraph UdpThread[UDP 接收线程]
            UdpReceiver["_udp_thread()"]
            FrameBuf["_frame_buf<br/>按 frame_id 缓存分片"]
            Ingest["_ingest_packet()"]
            WriteFrame["_write_hevc_frame()"]
        end

        subgraph FfmpegReaderThread[FFmpeg stdout 读取线程]
            Reader["_ffmpeg_reader(proc)"]
        end

        subgraph WsThread[WebSocket 服务线程]
            WsRunner["_ws_server_thread()"]
            Serve["asyncio.run(_serve())"]
            Queue["_async_queue"]
            Broadcaster["_broadcaster()"]
            Clients["_ws_clients"]
        end

        FFmpeg["FFmpeg 子进程<br/>stdin: HEVC<br/>stdout: frag MP4 / H.264"]
    end

    subgraph Browser[浏览器]
        WsClient["video-player.js<br/>WebSocket 客户端"]
        MSE["MediaSource / SourceBuffer"]
        VideoTag["video 元素播放"]
    end

    Start --> StartLock
    Start --> FfmpegLock
    Start --> FfmpegProc
    Start --> UdpReceiver
    Start --> WsRunner

    UdpSource -- UDP 3334 --> UdpReceiver
    UdpReceiver --> Ingest
    Ingest --> FrameBuf
    FrameBuf -- 拼出完整帧 --> WriteFrame
    WriteFrame -- 受 _ffmpeg_lock 保护 --> FfmpegProc
    FfmpegProc --> FFmpeg
    FFmpeg -- stdout 分片输出 --> Reader
    Reader --> Queue
    WsRunner --> Serve
    Serve --> Queue
    Serve --> Broadcaster
    Serve --> Clients
    Queue --> Broadcaster
    Broadcaster -- WS 8765 二进制分片 --> WsClient
    WsClient --> MSE
    MSE --> VideoTag
```

## 链路说明

1. start() 在模块导入阶段被调用，只负责把整条图传流水线拉起一次。
2. UDP 接收线程监听 3334 端口，调用 _ingest_packet() 按 frame_id 和 slice_id 重组 HEVC 帧。
3. 当某一帧接收完整后，_write_hevc_frame() 把整帧写入 FFmpeg 的 stdin。
4. FFmpeg 把 HEVC 转成浏览器更容易消费的 frag MP4 H.264，并持续写到 stdout。
5. _ffmpeg_reader() 在独立线程中读取 stdout，把分片送入 _async_queue。
6. WebSocket 线程内的 asyncio 事件循环运行 _broadcaster()，把队列中的分片广播给所有浏览器客户端。
7. 浏览器端 video-player.js 通过 WebSocket 接收二进制分片，写入 MSE 的 SourceBuffer，最终驱动 video 元素播放。

## 线程职责

- 主线程：调用 start()，初始化共享对象并启动各工作线程。
- UDP 接收线程：收包、重组帧、把完整帧写给 FFmpeg。
- FFmpeg stdout 读取线程：持续消费 FFmpeg 输出，避免 stdout 管道阻塞。
- WebSocket 服务线程：运行独立 asyncio 循环，负责客户端连接和广播。

## 关键共享对象

- _ffmpeg_proc：保存当前 FFmpeg 子进程句柄。
- _ffmpeg_lock：保护 _ffmpeg_proc 的读取、替换和重启流程，避免并发竞态。
- _frame_buf：缓存未收全的 UDP 分片帧。
- _async_queue：跨线程把 FFmpeg 输出桥接到 asyncio 广播协程。
- _ws_clients：当前连接到视频流的浏览器客户端集合。
- _start_lock：确保 start() 的启动逻辑只执行一次。

