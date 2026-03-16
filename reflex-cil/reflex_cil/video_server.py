"""video_server.py
UDP 3334 (HEVC分片) → 帧重组 → FFmpeg (HEVC → frag H.264 MP4) → WebSocket 8765

报头格式（每个UDP包前8字节）：
  帧编号（递增）     : 2 byte  (big-endian uint16)
  当前帧内分片序号   : 2 byte  (big-endian uint16)
  当前帧总字节数     : 4 byte  (big-endian uint32)
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import struct
import subprocess
import threading
import time
from typing import Optional

UDP_PORT = 3334
WS_PORT = 8765
UDP_BIND = "0.0.0.0"

MAX_FRAME_BYTES = 6 * 1024 * 1024  # 6 MB
MAX_BUFFERED_FRAMES = 64
FRAME_TIMEOUT_S = 2.0
CLEANUP_INTERVAL_S = 0.5

# ── Shared async state ─────────────────────────────────────────────────────────
_loop: Optional[asyncio.AbstractEventLoop] = None
_async_queue: Optional[asyncio.Queue] = None
_ws_clients: set = set()
_ws_lock = threading.Lock()


def _broadcast_chunk(chunk: bytes) -> None:
    """Thread-safe: push an encoded chunk to the websocket broadcast queue."""
    if _loop is None or _async_queue is None:
        return
    loop = _loop
    q = _async_queue  # narrow Optional away

    def _put() -> None:
        if q.full():
            try:
                q.get_nowait()
            except Exception:
                pass
        try:
            q.put_nowait(chunk)
        except Exception:
            pass

    loop.call_soon_threadsafe(_put)


# ── FFmpeg subprocess ──────────────────────────────────────────────────────────
_ffmpeg_proc: Optional[subprocess.Popen] = None
_ffmpeg_lock = threading.Lock()

_FFMPEG_ARGS = [
    "-loglevel", "warning",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-f", "hevc",
    "-i", "pipe:0",
    "-an",
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-tune", "zerolatency",
    "-profile:v", "baseline",
    "-level", "3.1",
    "-pix_fmt", "yuv420p",
    "-b:v", "1500k",
    "-maxrate", "2000k",
    "-bufsize", "2000k",
    "-g", "30",
    "-keyint_min", "30",
    "-sc_threshold", "0",
    "-f", "mp4",
    "-movflags", "frag_keyframe+empty_moov+default_base_moof+faststart",
    "-frag_duration", "100000",
    "pipe:1",
]


def _start_ffmpeg() -> Optional[subprocess.Popen]:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        print("[VideoServer] ⚠️  未找到 ffmpeg，视频功能不可用。请安装 ffmpeg 并加入 PATH。")
        return None
    try:
        proc = subprocess.Popen(
            [ffmpeg_path] + _FFMPEG_ARGS,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        print(f"[VideoServer] ✅ FFmpeg 启动 (HEVC→H.264 frag MP4)")
        return proc
    except Exception as exc:
        print(f"[VideoServer] ❌ 启动 ffmpeg 失败: {exc}")
        return None


def _ffmpeg_reader(proc: subprocess.Popen) -> None:
    """Background thread: read FFmpeg stdout and broadcast chunks."""
    if proc.stdout is None:
        return
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            _broadcast_chunk(chunk)
    except Exception:
        pass


def _write_hevc_frame(frame: bytes) -> None:
    """Write a reassembled HEVC frame to FFmpeg stdin. Restarts FFmpeg if needed."""
    global _ffmpeg_proc

    with _ffmpeg_lock:
        proc = _ffmpeg_proc

    if proc is None or proc.poll() is not None:
        with _ffmpeg_lock:
            _ffmpeg_proc = _start_ffmpeg()
            if _ffmpeg_proc:
                threading.Thread(
                    target=_ffmpeg_reader, args=(_ffmpeg_proc,), daemon=True
                ).start()
        with _ffmpeg_lock:
            proc = _ffmpeg_proc

    if proc is None:
        return

    try:
        if proc.stdin is not None:
            proc.stdin.write(frame)
            proc.stdin.flush()
    except Exception:
        pass


# ── UDP receiver / frame reassembly ───────────────────────────────────────────
# {frame_id: {"total": int, "received": int, "slices": dict[int, bytes], "ts": float}}
_frame_buf: dict = {}  # 暂存frame_id到分片数据的字典


def _cleanup_frames() -> None:
    now = time.monotonic()
    expired = [fid for fid, f in _frame_buf.items() if now - f["ts"] > FRAME_TIMEOUT_S]
    for fid in expired:
        del _frame_buf[fid]


def _ingest_packet(data: bytes) -> None:  # 目标：处理一个UDP包，更新对应帧的重组状态，并在完成时写入FFmpeg
    if len(data) < 9:
        return

    frame_id = struct.unpack_from(">H", data, 0)[0]  # 帧编号
    slice_id = struct.unpack_from(">H", data, 2)[0]  # 分片序号
    total = struct.unpack_from(">I", data, 4)[0]  # 当前帧总字节数
    payload = data[8:]  # 当前包的有效负载（可以用内存视图来优化）

    if total <= 0 or total > MAX_FRAME_BYTES or not payload:
        return

    if frame_id not in _frame_buf:  # 若不在，则添加的逻辑
        if len(_frame_buf) >= MAX_BUFFERED_FRAMES:  # 若buffer已满，则丢弃最旧的一帧
            oldest = min(_frame_buf, key=lambda k: _frame_buf[k]["ts"])  # 这里min取的是最旧的frame_id
            del _frame_buf[oldest]  # 删除最旧的一帧
        _frame_buf[frame_id] = {  # 初始化新帧的字典
            "total": total,
            "received": 0,
            "slices": {},
            "ts": time.monotonic(),
        }

    frame = _frame_buf[frame_id]  # 获取当前帧的字典
    if frame["total"] != total:  # 若帧的总大小不匹配，则丢弃该帧
        del _frame_buf[frame_id]
        return

    frame["ts"] = time.monotonic()  # 更新最后接收时间戳

    if slice_id not in frame["slices"]:
        frame["slices"][slice_id] = payload
        frame["received"] += len(payload)

    if frame["received"] >= frame["total"]:
        ordered = sorted(frame["slices"].items())
        complete = b"".join(p for _, p in ordered)
        del _frame_buf[frame_id]
        if len(complete) == frame["total"]:
            _write_hevc_frame(complete)


def _udp_thread() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((UDP_BIND, UDP_PORT))
    except OSError as exc:
        print(f"[VideoServer] ❌ UDP 绑定失败 {UDP_BIND}:{UDP_PORT}: {exc}")
        return
    print(f"[VideoServer] ✅ UDP 监听 {UDP_BIND}:{UDP_PORT}")
    last_clean = time.monotonic()
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            _ingest_packet(data)
            now = time.monotonic()
            if now - last_clean > CLEANUP_INTERVAL_S:
                _cleanup_frames()
                last_clean = now
        except Exception:
            pass


# ── WebSocket server ───────────────────────────────────────────────────────────
async def _broadcaster() -> None:
    assert _async_queue is not None
    while True:
        chunk = await _async_queue.get()
        with _ws_lock:
            clients = set(_ws_clients)
        if not clients:
            continue
        dead: set = set()
        for ws in clients:
            try:
                await ws.send(chunk)
            except Exception:
                dead.add(ws)
        if dead:
            with _ws_lock:
                _ws_clients.difference_update(dead)


async def _ws_handler(websocket, *_args) -> None:
    """Compatible with websockets 10 (path arg) and 11+ (no path arg)."""
    with _ws_lock:
        _ws_clients.add(websocket)
    print(f"[VideoServer] 🎥 视频客户端接入 (共 {len(_ws_clients)})")
    try:
        await websocket.wait_closed()
    finally:
        with _ws_lock:
            _ws_clients.discard(websocket)
        print(f"[VideoServer] 视频客户端断开 (共 {len(_ws_clients)})")


async def _serve() -> None:
    global _loop, _async_queue
    import websockets  # imported lazily so start() only fails if websockets missing

    _loop = asyncio.get_running_loop()
    _async_queue = asyncio.Queue(maxsize=512)
    asyncio.create_task(_broadcaster())
    async with websockets.serve(_ws_handler, "0.0.0.0", WS_PORT):
        print(f"[VideoServer] ✅ WebSocket 视频流 0.0.0.0:{WS_PORT}")
        await asyncio.Future()  # block forever


def _ws_server_thread() -> None:
    asyncio.run(_serve())


# ── Public API ─────────────────────────────────────────────────────────────────
_started = False
_start_lock = threading.Lock()


def start() -> None:
    """Start video server (UDP + FFmpeg + WebSocket). Safe to call multiple times."""
    global _started, _ffmpeg_proc
    with _start_lock:
        if _started:
            return
        _started = True

    # Start FFmpeg and its stdout reader
    with _ffmpeg_lock:
        _ffmpeg_proc = _start_ffmpeg()
        if _ffmpeg_proc:
            threading.Thread(
                target=_ffmpeg_reader, args=(_ffmpeg_proc,), daemon=True
            ).start()

    # Start UDP receiver
    threading.Thread(target=_udp_thread, daemon=True).start()

    # Start WebSocket server (runs its own asyncio event loop)
    threading.Thread(target=_ws_server_thread, daemon=True).start()
