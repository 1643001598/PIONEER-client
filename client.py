import argparse
import importlib
import json
import random
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Set, Type

from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message import Message
import paho.mqtt.client as mqtt


PROTO_FILE = "messages.proto"
PB2_MODULE = "messages_pb2"
UP_TOPIC_PREFIX_DEFAULT = ""
DEFAULT_LISTEN_TOPIC = "#"

TOPIC_ALIASES_LOCAL_TO_SHARK = {
    "KeyboardMouseControl": "RemoteControl",
    "SentryCtrlCommand": "GuardCtrlCommand",
    "RadarInfoToClient": "RaderInfoToClient",
    "SentryStatusSync": "SentinelStatusSync",
    "SentryCtrlResult": "GuardCtrlResult",
}
TOPIC_ALIASES_SHARK_TO_LOCAL = {v: k for k, v in TOPIC_ALIASES_LOCAL_TO_SHARK.items()}

RECOMMENDED_UPLINK_MESSAGES = {
    "KeyboardMouseControl",
    "MapClickInfoNotify",
    "AssemblyCommand",
    "RobotPerformanceSelectionCommand",
    "HeroDeployModeEventCommand",
    "RuneActivateCommand",
    "DartCommand",
    "SentryCtrlCommand",
    "AirSupportCommand",
}


def ensure_pb2_generated(workdir: Path) -> None:
    proto_path = workdir / PROTO_FILE
    pb2_path = workdir / f"{PB2_MODULE}.py"
    if not proto_path.exists():
        raise FileNotFoundError(f"未找到 {proto_path}")

    need_generate = (not pb2_path.exists()) or (pb2_path.stat().st_mtime < proto_path.stat().st_mtime)
    if not need_generate:
        return

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{workdir}",
        f"--python_out={workdir}",
        str(proto_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "生成 messages_pb2.py 失败，请先安装 grpcio-tools。\n"
            f"命令: {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )


def load_message_types() -> Dict[str, Type[Message]]:
    module = importlib.import_module(PB2_MODULE)
    type_map: Dict[str, Type[Message]] = {}
    for name in dir(module):
        attr = getattr(module, name)
        if isinstance(attr, type) and issubclass(attr, Message):
            if hasattr(attr, "DESCRIPTOR") and attr.DESCRIPTOR is not None:
                type_map[name] = attr
    return type_map


class InteractiveClient:
    def __init__(self, host: str, port: int, client_id: str, up_topic_prefix: str):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.up_topic_prefix = up_topic_prefix
        self.message_types = load_message_types()
        self.subscribed_topics: Set[str] = set()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, reason_code, properties):
        print(f"[client] 已连接 broker: {self.host}:{self.port}, reason={reason_code}")

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        print(f"[client] 与 broker 断开连接, reason={reason_code}")

    def _resolve_message_name_from_topic(self, topic: str) -> Optional[str]:
        topic_name = topic.split("/")[-1]

        if topic_name in self.message_types:
            return topic_name

        mapped_local = TOPIC_ALIASES_SHARK_TO_LOCAL.get(topic_name)
        if mapped_local in self.message_types:
            return mapped_local

        for msg_name in self.message_types:
            if topic == msg_name or topic.endswith(f"/{msg_name}") or msg_name in topic:
                return msg_name

        for shark_name, local_name in TOPIC_ALIASES_SHARK_TO_LOCAL.items():
            if topic == shark_name or topic.endswith(f"/{shark_name}") or shark_name in topic:
                if local_name in self.message_types:
                    return local_name

        return None

    def _build_publish_topic(self, message_type: str) -> str:
        topic_name = TOPIC_ALIASES_LOCAL_TO_SHARK.get(message_type, message_type)
        if not self.up_topic_prefix:
            return topic_name
        if self.up_topic_prefix.endswith("/"):
            return f"{self.up_topic_prefix}{topic_name}"
        return f"{self.up_topic_prefix}/{topic_name}"

    def on_message(self, client, userdata, msg):
        message_name = self._resolve_message_name_from_topic(msg.topic)
        if message_name is None:
            print(f"[recv] 未识别类型 topic={msg.topic}, size={len(msg.payload)}")
            return

        message_cls = self.message_types.get(message_name)
        if message_cls is None:
            print(f"[recv] 未找到消息定义 topic={msg.topic}")
            return

        try:
            decoded = message_cls()
            decoded.ParseFromString(msg.payload)
            content = MessageToDict(decoded, preserving_proto_field_name=True)
            print(f"[recv] topic={msg.topic} type={message_name} data={content}")
        except Exception as exc:
            print(f"[recv] 解析失败 topic={msg.topic} type={message_name}: {exc}")

    def connect(self) -> None:
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def send(self, message_type: str, payload: dict) -> None:
        message_cls = self.message_types.get(message_type)
        if message_cls is None:
            raise ValueError(f"未找到 protobuf 消息定义: {message_type}")

        message_obj = message_cls()
        ParseDict(payload, message_obj)

        topic = self._build_publish_topic(message_type)
        self.client.publish(topic, message_obj.SerializeToString(), qos=0)
        print(f"[send] topic={topic}")

    def subscribe_topic(self, topic: str) -> None:
        self.client.subscribe(topic)
        self.subscribed_topics.add(topic)
        print(f"[sub] 已订阅: {topic}")

    def unsubscribe_topic(self, topic: str) -> None:
        self.client.unsubscribe(topic)
        if topic in self.subscribed_topics:
            self.subscribed_topics.remove(topic)
        print(f"[sub] 已取消订阅: {topic}")

    def list_recommended(self) -> None:
        for name in sorted(RECOMMENDED_UPLINK_MESSAGES):
            topic_name = TOPIC_ALIASES_LOCAL_TO_SHARK.get(name, name)
            print(f"  - {name}  -> topic: {topic_name}")

    def list_all_messages(self) -> None:
        for name in sorted(self.message_types.keys()):
            print(f"  - {name}")

    def simulate_keyboard(self, hz: float, seconds: float) -> None:
        hz = max(1.0, hz)
        seconds = max(0.0, seconds)
        interval = 1.0 / hz

        print(f"[sim] 开始模拟 KeyboardMouseControl, hz={hz}, seconds={seconds}")
        total = 0
        start = time.time()
        try:
            while True:
                payload = {
                    "mouse_x": random.randint(-30, 30),
                    "mouse_y": random.randint(-30, 30),
                    "mouse_z": random.randint(-2, 2),
                    "left_button_down": random.random() > 0.7,
                    "right_button_down": random.random() > 0.85,
                    "mid_button_down": random.random() > 0.95,
                    "keyboard_value": random.randint(0, 65535),
                }
                self.send("KeyboardMouseControl", payload)
                total += 1
                if seconds > 0 and (time.time() - start) >= seconds:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass

        print(f"[sim] 结束，共发送 {total} 条")


def parse_args():
    parser = argparse.ArgumentParser(description="RoboMaster 虚拟 MQTT 客户端（交互式）")
    parser.add_argument("--host", default="127.0.0.1", help="MQTT broker 主机")
    parser.add_argument("--port", type=int, default=3333, help="MQTT broker 端口")
    parser.add_argument("--client-id", default="rm-virtual-client", help="MQTT Client ID")
    parser.add_argument(
        "--up-topic-prefix",
        default=UP_TOPIC_PREFIX_DEFAULT,
        help="上行主题前缀，SharkDataSever 兼容模式建议留空",
    )
    parser.add_argument(
        "--listen-topic",
        default=DEFAULT_LISTEN_TOPIC,
        help="启动后默认订阅的主题，默认 #",
    )
    return parser.parse_args()


def print_usage_tip():
    print("\n可用命令:")
    print("  list                                      查看推荐上行消息类型")
    print("  list all                                  查看当前 proto 中所有消息类型")
    print("  send <MessageType> <JSON>                 发送一条 protobuf 消息")
    print("  listen <topic>                            订阅主题")
    print("  unlisten <topic>                          取消订阅主题")
    print("  subscriptions                             查看已订阅主题")
    print("  simulate keyboard [hz] [seconds]          模拟键鼠上行")
    print("  help                                      查看帮助")
    print("  exit                                      退出")
    print("示例:")
    print('  send KeyboardMouseControl {"mouse_x":10,"mouse_y":-5,"mouse_z":0,"left_button_down":false,"right_button_down":false,"keyboard_value":1,"mid_button_down":false}')
    print("  listen RemoteControl")
    print("  simulate keyboard 10 5")


def main():
    workdir = Path(__file__).resolve().parent
    ensure_pb2_generated(workdir)

    args = parse_args()
    cli = InteractiveClient(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        up_topic_prefix=args.up_topic_prefix,
    )

    try:
        cli.connect()
    except (ConnectionRefusedError, TimeoutError, OSError, socket.error) as exc:
        print(f"连接 MQTT broker 失败: {exc}")
        print(f"请确认 broker 已启动并监听 {args.host}:{args.port}")
        return

    time.sleep(0.2)
    cli.subscribe_topic(args.listen_topic)

    print(f"已连接 MQTT broker: {args.host}:{args.port}")
    print(f"上行主题格式: {args.up_topic_prefix}<MessageName>")
    print("SharkDataSever 兼容提示: 建议 topic 直接使用上行消息名")
    print_usage_tip()

    try:
        while True:
            line = input("\nclient> ").strip()
            if not line:
                continue

            if line in {"exit", "quit"}:
                break
            if line == "help":
                print_usage_tip()
                continue
            if line == "list":
                cli.list_recommended()
                continue
            if line == "list all":
                cli.list_all_messages()
                continue
            if line == "subscriptions":
                if not cli.subscribed_topics:
                    print("  (空)")
                else:
                    for topic in sorted(cli.subscribed_topics):
                        print(f"  - {topic}")
                continue

            if line.startswith("listen "):
                topic = line[len("listen "):].strip()
                if not topic:
                    print("格式错误: listen <topic>")
                    continue
                cli.subscribe_topic(topic)
                continue

            if line.startswith("unlisten "):
                topic = line[len("unlisten "):].strip()
                if not topic:
                    print("格式错误: unlisten <topic>")
                    continue
                cli.unsubscribe_topic(topic)
                continue

            if line.startswith("simulate "):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "keyboard":
                    hz = float(parts[2]) if len(parts) >= 3 else 10.0
                    seconds = float(parts[3]) if len(parts) >= 4 else 5.0
                    cli.simulate_keyboard(hz=hz, seconds=seconds)
                    continue
                print("格式错误: simulate keyboard [hz] [seconds]")
                continue

            if not line.startswith("send "):
                print("未知命令，输入 help 查看帮助")
                continue

            parts = line.split(" ", 2)
            if len(parts) < 3:
                print("格式错误: send <MessageType> <JSON>")
                continue

            message_type = parts[1]
            json_text = parts[2]

            try:
                payload = json.loads(json_text)
                if not isinstance(payload, dict):
                    raise ValueError("JSON 必须是对象")
                cli.send(message_type, payload)
            except Exception as exc:
                print(f"发送失败: {exc}")

    except KeyboardInterrupt:
        print("\n收到中断，准备退出")
    finally:
        time.sleep(0.1)
        cli.close()
        print("已退出")


if __name__ == "__main__":
    main()
