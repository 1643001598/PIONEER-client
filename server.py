import argparse
import importlib
import json
import random
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Type

from google.protobuf.json_format import ParseDict
from google.protobuf.message import Message
import paho.mqtt.client as mqtt


PROTO_FILE = "messages.proto"
PB2_MODULE = "messages_pb2"
DEFAULT_TOPIC_PREFIX = ""

SHARK_TOPIC_ALIASES = {
    "KeyboardMouseControl": "RemoteControl",
    "SentryCtrlCommand": "GuardCtrlCommand",
}

SIMULATED_DOWNLINK_MESSAGES = (
    "GameStatus",
    "RobotDynamicStatus",
    "RobotPosition",
    "Event",
)

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



def parse_args():
    parser = argparse.ArgumentParser(description="RoboMaster 虚拟 MQTT 发布端（交互式）")
    parser.add_argument("--host", default="127.0.0.1", help="MQTT broker 主机")
    parser.add_argument("--port", type=int, default=3333, help="MQTT broker 端口")
    parser.add_argument("--client-id", default="rm-publisher-server", help="MQTT Client ID")
    parser.add_argument(
        "--topic-prefix",
        default=DEFAULT_TOPIC_PREFIX,
        help="主题前缀，SharkDataSever 兼容模式建议留空",
    )
    parser.add_argument(
        "--no-simulate",
        action="store_true",
        help="关闭启动时下行随机模拟发送",
    )
    parser.add_argument(
        "--simulate-interval",
        type=float,
        default=1.0,
        help="下行随机模拟发送间隔（秒）",
    )
    return parser.parse_args()



def print_usage_tip():
    print("\n可用命令:")
    print("  list                              查看推荐上行消息类型（Shark兼容）")
    print("  list all                          查看当前 proto 中所有消息类型")
    print("  send <MessageType> <JSON>         发送一条 protobuf 消息")
    print("  help                              查看帮助")
    print("  exit                              退出")
    print("示例:")
    print('  send KeyboardMouseControl {"mouse_x":10,"mouse_y":-5,"mouse_z":0,"left_button_down":false,"right_button_down":false,"keyboard_value":1,"mid_button_down":false}')


def publish_message(
    client: mqtt.Client,
    message_types: Dict[str, Type[Message]],
    message_type: str,
    payload_dict: dict,
    topic_prefix: str,
) -> str:
    message_cls = message_types.get(message_type)
    if message_cls is None:
        raise ValueError(f"未找到 protobuf 消息定义: {message_type}")

    msg = message_cls()
    ParseDict(payload_dict, msg)
    mapped_type = SHARK_TOPIC_ALIASES.get(message_type, message_type)
    topic = f"{topic_prefix}{mapped_type}"
    client.publish(topic, msg.SerializeToString(), qos=0)
    return topic


def build_simulated_payload(message_type: str, tick: int) -> dict:
    if message_type == "GameStatus":
        elapsed = tick
        return {
            "current_round": 1,
            "total_rounds": 3,
            "red_score": random.randint(0, 200),
            "blue_score": random.randint(0, 200),
            "current_stage": 4,
            "stage_countdown_sec": max(0, 420 - elapsed),
            "stage_elapsed_sec": elapsed,
            "is_paused": False,
        }

    if message_type == "RobotDynamicStatus":
        return {
            "current_health": random.randint(120, 600),
            "current_heat": round(random.uniform(0, 120), 2),
            "last_projectile_fire_rate": round(random.uniform(8, 20), 2),
            "current_chassis_energy": random.randint(0, 100),
            "current_buffer_energy": random.randint(0, 100),
            "current_experience": random.randint(0, 1000),
            "experience_for_upgrade": 1200,
            "total_projectiles_fired": tick * random.randint(1, 3),
            "remaining_ammo": random.randint(0, 200),
            "is_out_of_combat": random.random() > 0.6,
            "out_of_combat_countdown": random.randint(0, 10),
            "can_remote_heal": True,
            "can_remote_ammo": True,
        }

    if message_type == "RobotPosition":
        return {
            "x": round(random.uniform(-14, 14), 2),
            "y": round(random.uniform(-7.5, 7.5), 2),
            "z": 0.4,
            "yaw": round(random.uniform(0, 360), 2),
        }

    if message_type == "Event":
        return {
            "event_id": random.randint(1, 18),
            "param": f"sim-event-{tick}",
        }

    return {}


def simulation_loop(
    client: mqtt.Client,
    message_types: Dict[str, Type[Message]],
    topic_prefix: str,
    interval_sec: float,
    stop_event: threading.Event,
):
    tick = 0
    safe_interval = max(0.1, interval_sec)
    while not stop_event.is_set():
        tick += 1
        message_type = random.choice(SIMULATED_DOWNLINK_MESSAGES)
        payload_dict = build_simulated_payload(message_type, tick)
        try:
            topic = publish_message(client, message_types, message_type, payload_dict, topic_prefix)
            print(f"[sim] 已发送 {message_type} -> topic={topic}")
        except Exception as exc:
            print(f"[sim] 发送失败 {message_type}: {exc}")

        if stop_event.wait(safe_interval):
            break



def main():
    workdir = Path(__file__).resolve().parent
    ensure_pb2_generated(workdir)

    args = parse_args()
    message_types = load_message_types()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.client_id)
    try:
        client.connect(args.host, args.port, keepalive=30)
    except (ConnectionRefusedError, TimeoutError, OSError, socket.error) as exc:
        print(f"连接 MQTT broker 失败: {exc}")
        print(f"请确认 broker 已启动并监听 {args.host}:{args.port}")
        print("可用示例:")
        print("  1) 启动你已有的 Node broker（SharkDataSever）")
        print("  2) 或安装并启动 Mosquitto: mosquitto -v -p 3333")
        return
    client.loop_start()

    stop_simulation = threading.Event()
    simulation_thread = None
    if not args.no_simulate:
        simulation_thread = threading.Thread(
            target=simulation_loop,
            args=(
                client,
                message_types,
                args.topic_prefix,
                args.simulate_interval,
                stop_simulation,
            ),
            daemon=True,
        )
        simulation_thread.start()

    print(f"已连接 MQTT broker: {args.host}:{args.port}")
    print(f"主题格式: {args.topic_prefix}<MessageName>")
    print("SharkDataSever 兼容提示: 使用上行消息名 topic（例如 RemoteControl / AssemblyCommand）")
    if args.no_simulate:
        print("启动随机下行模拟: 已关闭")
    else:
        print(f"启动随机下行模拟: 已开启，间隔 {max(0.1, args.simulate_interval)} 秒")
    print_usage_tip()

    try:
        while True:
            line = input("\nserver> ").strip()
            if not line:
                continue

            if line in {"exit", "quit"}:
                break
            if line == "help":
                print_usage_tip()
                continue
            if line == "list":
                names = sorted(name for name in message_types if name in RECOMMENDED_UPLINK_MESSAGES)
                for name in names:
                    topic_name = SHARK_TOPIC_ALIASES.get(name, name)
                    print(f"  - {name}  -> topic: {topic_name}")
                continue
            if line == "list all":
                names = sorted(name for name in message_types)
                for name in names:
                    print(f"  - {name}")
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

            message_cls = message_types.get(message_type)
            if message_cls is None:
                print(f"未找到 protobuf 消息定义: {message_type}")
                continue

            try:
                payload_dict = json.loads(json_text)
                if not isinstance(payload_dict, dict):
                    raise ValueError("JSON 必须是对象")

                topic = publish_message(client, message_types, message_type, payload_dict, args.topic_prefix)
                print(f"已发送 -> topic={topic}")
            except Exception as exc:
                print(f"发送失败: {exc}")

    except KeyboardInterrupt:
        print("\n收到中断，准备退出")
    finally:
        stop_simulation.set()
        if simulation_thread is not None:
            simulation_thread.join(timeout=2)
        time.sleep(0.1)
        client.loop_stop()
        client.disconnect()
        print("已退出")


if __name__ == "__main__":
    main()
