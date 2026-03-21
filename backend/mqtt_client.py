"""
用于提供mqtt客户端连接的模块
"""
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # 设置日志级别为DEBUG以输出调试信息
logger.debug("日志系统已初始化，日志级别设置为DEBUG")
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


import paho.mqtt.client as mqtt
import threading
import queue
# from collections import namedtuple
from dataclasses import dataclass
from typing import Callable, Dict, Any
from protocol.protobuf_models import DOWN_TOPIC2MODEL_MAP
from google.protobuf.json_format import MessageToDict

TOTAL_TIME = 420  # 比赛总时长，单位为秒
ALL_STATES = "ALL_STATES"
UNKNOWN = "UNKNOWN"

ALLY = "ALLY"
ENEMY = "ENEMY"
ALL_SIDES = "ALL_SIDES"

RED = "RED"
BLUE = "BLUE"
ALL_COLORS = "ALL_COLORS"

BASE = "BASE"
OUTPOST = "OUTPOST"
ALL_BUILDINGS = "ALL_BUILDINGS"

HEALTH = "HEALTH"
STATUS = "STATUS"
SHIELD = "SHIELD"

RED_HERO = "RED_HERO"
RED_ENGINEER = "RED_ENGINEER"
RED_INFANTRY = "RED_INFANTRY"
RED_AIR = "RED_AIR"
RED_SENTRY = "RED_SENTRY"
RED_DART = "RED_DART"
RED_RADAR = "RED_RADAR"
RED_OUTPOST = "RED_OUTPOST"
RED_BASE = "RED_BASE"

BLUE_HERO = "BLUE_HERO"
BLUE_ENGINEER = "BLUE_ENGINEER"
BLUE_INFANTRY = "BLUE_INFANTRY"
BLUE_AIR = "BLUE_AIR"
BLUE_SENTRY = "BLUE_SENTRY"
BLUE_DART = "BLUE_DART"
BLUE_RADAR = "BLUE_RADAR"
BLUE_OUTPOST = "BLUE_OUTPOST"
BLUE_BASE = "BLUE_BASE"

REFREE_SERVER = "REFREE_SERVER"

NAME_TO_ID = {
    "RED_HERO": 1,
    "RED_ENGINEER": 2,
    "RED_INFANTRY": (3, 4, 5),
    "RED_AIR": 6,
    "RED_SENTRY": 7,
    "RED_DART": 8,
    "RED_RADAR": 9,
    "RED_OUTPOST": 10,
    "RED_BASE": 11,

    "BLUE_HERO": 101,
    "BLUE_ENGINEER": 102,
    "BLUE_INFANTRY": (103, 104, 105),
    "BLUE_AIR": 106,
    "BLUE_SENTRY": 107,
    "BLUE_DART": 108,
    "BLUE_RADAR": 109,
    "BLUE_OUTPOST": 110,
    "BLUE_BASE": 111,
}

ID_TO_NAME = {}
for name, ids in NAME_TO_ID.items():
    if isinstance(ids, tuple):
        for id in ids:
            ID_TO_NAME[id] = name
    else:
        ID_TO_NAME[ids] = name

NAME_TO_CLIENT_ID = {
    RED_HERO: 0x0101,
    RED_ENGINEER: 0x0102,
    RED_INFANTRY: (0x0103, 0x0104,0x0105),
    RED_AIR: 0x0106,

    BLUE_HERO: 0x0165,
    BLUE_ENGINEER: 0x0166,
    BLUE_INFANTRY: (0x0167, 0x0168, 0x0169),
    BLUE_AIR: 0x016A,

    REFREE_SERVER: 0x8080,
}

CLIENT_ID_TO_NAME = {v: k for k, v in NAME_TO_CLIENT_ID.items()}

ALLOWED_CLIENT_ID = []
for _, ids in NAME_TO_CLIENT_ID.items():
    if isinstance(ids, tuple):
        ALLOWED_CLIENT_ID.extend(ids)
    else:
        ALLOWED_CLIENT_ID.append(ids)

UPLINK_TOPICS = {
    "CommonCommand",
    "RobotPerformanceSelectionCommand",
    "HeroDeployModeEventCommand",
    "RuneActivateCommand",
    "DartCommand",
    "MapSentryPathSearchCommand",
    "SentryPathControlCommand",
    "MapRadarMarkCommand",
    "AirsupportCommand",
    "TechCoreAssembleOperationCommand",
}
DOWNLINK_TOPICS = {
    "GameStatus",
    "GlobalUnitStatus",
    "GlobalLogisticsStatus",
    "GlobalSpecialMechanism",
    "Event",
    "RobotInjuryStat",
    "RobotRespawnStatus",
    "RobotStaticStatus",
    "RobotDynamicStatus",
    "RobotModuleStatus",
    "RobotPosition",
    "Buff",
    "PenaltyInfo",
    "RobotPathPlanInfo",
    "RaderInfoToClient",
    "CustomByteBlock",
    "TechCoreMotionStateSync",
    "RobotPerformanceSelectionSync",
    "DeployModeStatusSync",
    "RuneStatusSync",
    "SentryStatusSync",
    "DartSelectTargetStatusSync",
    "SentryCtrlResult",
    "AirSupportStatusSync",
}
    

# DataClass Structures
# Time
@dataclass
class RMTime:
    remaining_time: int
    passed_time: int
    total_time: int

    def __str__(self):
        return f"{self.remaining_time} - {self.total_time}"

# Building status(including ENEMY/ALLY, BASE/OUTPOST)
@dataclass
class RMBuildingStatus:
    health: int
    status: int
    shield: int

@dataclass
class RMSideBuildingStatus:
    BASE: RMBuildingStatus
    OUTPOST: RMBuildingStatus

@dataclass
class RMAllBuildingsStatus:
    RED: RMSideBuildingStatus
    BLUE: RMSideBuildingStatus


class RoboMasterMQTT:

    def __init__(self, client_id: int, host: str = "192.168.12.1", port: int = 3333):
        if client_id not in ALLOWED_CLIENT_ID:
            logger.critical(f"Invalid client_id: {client_id}. Allowed client_ids are: {ALLOWED_CLIENT_ID}")
            raise ValueError(f"Invalid client_id: {client_id}. Please choose from {ALLOWED_CLIENT_ID}.")
        # 初始化MQTT客户端
        self.client_id = client_id  # 机器人ID：1=红方英雄, 8=红方飞镖, 108=蓝方飞镖等
        self.host = host
        self.port = port
        self.client = mqtt.Client(client_id=str(client_id))
        self.message_queue = queue.Queue()
        self.callbacks: Dict[str, Callable] = {}
        # 初始化状态机
        self.states = RMClientStates()
        self.states.set_ally_color(RED if NAME_TO_CLIENT_ID[RED_HERO] <= client_id < NAME_TO_CLIENT_ID[RED_AIR] else BLUE)
        # 定义需要更新状态的topic和对应的状态字段列表
        self.update_items = {
            "GameStatus": ["red_score", "blue_score", "stage_countdown_sec", "stage_elapsed_sec"],
            "GlobalUnitStatus": ALL_STATES,

        }
        # 设置回调
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        logger.info(f"MQTT[{CLIENT_ID_TO_NAME[client_id]}] 服务端 连接到了: {host}:{port}")
    
    def _on_connect(self, client, userdata, flags, rc):
        """
        - client: MQTT客户端实例
        - userdata: 用户数据，连接时传入的参数
        - flags: 连接结果标志
        - rc: 连接结果代码, 0表示成功
        """
        if rc == 0:
            logger.info(f"连接成功: {CLIENT_ID_TO_NAME[self.client_id]} - {self.client_id} 已连接到 MQTT 服务器")
            # 连接成功后订阅必要topic
            for topic in DOWNLINK_TOPICS:
                self.client.subscribe(topic)
        else:
            logger.error(f"连接失败，错误代码: {rc}")
            
    def _on_message(self, client, userdata, msg):
        # 将消息放入队列，由处理线程解析
        logger.debug(f"收到了消息: {msg.topic}")
        self.message_queue.put((msg.topic, msg.payload))
        
    def _on_disconnect(self, client, userdata, rc):
        logger.warning(f"连接断开, rc={rc}, 正在尝试重连...")
        # 当
        while(True):
            try:
                self.client.connect(self.host, self.port, 60)
                logger.info("重连成功")
                # self.client.loop_start()
                break
            except Exception as e:
                logger.error(f"重连失败: {e}正在重试连接MQTT服务器...")        

    def _start_mqtt(self):
        logger.info("正在尝试启动MQTT客户端...")
        while(True):
            try:
                self.client.connect(self.host, self.port, 60)
                logger.info("连接MQTT服务器成功")
                break
            except Exception as e:
                logger.error(f"连接MQTT服务器失败: {e}正在重试连接MQTT服务器...")
        
        self.client.loop_start()

    def _start_message_processing(self):
        # 启动消息处理线程
        threading.Thread(target=self._process_messages, daemon=True).start()

    def start(self):
        self.register_callbacks()
        self._start_mqtt()
        self._start_message_processing()
    # ==============信息接收================
    def _process_messages(self):
        """消息处理循环"""
        logger.info("消息处理线程已启动")
        while True:
            topic, payload = self.message_queue.get()
            logger.debug(f"从队列中获取消息: {topic} - {payload}")
            if topic in self.callbacks:
                logger.debug(f"消息处理线程开始处理消息: {topic} - {payload}")
                self.callbacks[topic](payload)
                
    def state_update(self, state, msgs:Any = None):
        # 根据主题对应的消息内容更新状态，并记录日志
        # state: 传入的状态标识，msgs: 解析后的消息对象
        if isinstance(state, str):
            if state == ALL_STATES:
                msg_dict = MessageToDict(msgs, preserving_proto_field_name=True)
                self.states.updates.update(msg_dict)
                logger.info(f"状态机全部更新: {msg_dict}")
            else:
                val: Any = getattr(msgs, state, "未知分数")  
                self.states.updates[state] = val  
                logger.info(f"状态机更新: {state} = {val}")
        else:
            # 处理列表
            assert isinstance(state, list), "状态标识必须是字符串或字符串列表"
            for s in state:
                val: Any = getattr(msgs, s, "未知分数")  
                self.states.updates[s] = val  
                logger.info(f"状态机批量更新: {s} = {val}")

    def register_callbacks(self):
        def register(topic: str):
            # 装饰器工厂，用于注册回调函数并记录日志
            def wrapper(func: Callable[[RoboMasterMQTT, bytes], None]):
                # func: 需要注册的回调函数，必须接受(self, payload)两个参数
                logger.debug(f"注册回调函数: {func.__name__} 用于处理 topic: {topic}")
                self.callbacks[topic] = func.__get__(self)  # 将函数绑定到实例上，使其成为实例方法
                return func
            return wrapper

        def parse_AND_update(topic: str, payload: bytes):
            """
            解析消息并更新状态的通用函数, 适用于所有topic
             - topic: 消息主题
             - payload: 消息负载
             - return: 解析后的消息对象
            """
            logger.info(f"收到并开始处理 {topic} 消息")
            # 解析消息并记录解析结果
            parsed_msg = DOWN_TOPIC2MODEL_MAP[topic]()
            parsed_msg.ParseFromString(payload)

            # 遍历person.DESCRIPTOR.fields获取字段信息，并记录日志
            # 是否可以用MessageToDict直接转换成字典并记录日志？如果字段较多，手动遍历可能更清晰
            # msg_dict = MessageToDict(parsed_msg, preserving_proto_field_name=True)
            # logger.debug(f"{topic} 解析结果: {msg_dict}")
            field_info = []
            for field in parsed_msg.DESCRIPTOR.fields:
                value = getattr(parsed_msg, field.name)
                field_info.append(f"{field.name}={value}")
            logger.debug(f"{topic} 解析结果: {', '.join(field_info)}")

            # 根据update_items中的配置更新状态机，并记录更新结果
            self.state_update(self.update_items[topic], parsed_msg)
            return parsed_msg

        # 批量注册，定义回调函数（严格保持模板结构，便于后续扩展功能）
        @register("GameStatus")
        def process_game_status(self, payload: bytes):
            parse_AND_update("GameStatus", payload)

        @register("GlobalUnitStatus")
        def process_global_unit_status(self, payload: bytes):
            parse_AND_update("GlobalUnitStatus", payload)

        @register("GlobalLogisticsStatus")
        def process_global_logistics_status(self, payload: bytes):
            parse_AND_update("GlobalLogisticsStatus", payload)

        @register("GlobalSpecialMechanism")
        def process_global_special_mechanism(self, payload: bytes):
            parse_AND_update("GlobalSpecialMechanism", payload)

        @register("Event")
        def process_event(self, payload: bytes):
            parse_AND_update("Event", payload)

        @register("RobotInjuryStat")
        def process_robot_injury_stat(self, payload: bytes):
            parse_AND_update("RobotInjuryStat", payload)

        @register("RobotRespawnStatus")
        def process_robot_respawn_status(self, payload: bytes):
            parse_AND_update("RobotRespawnStatus", payload)

        @register("RobotStaticStatus")
        def process_robot_static_status(self, payload: bytes):
            parse_AND_update("RobotStaticStatus", payload)

        @register("RobotDynamicStatus")
        def process_robot_dynamic_status(self, payload: bytes):
            parse_AND_update("RobotDynamicStatus", payload)

        @register("RobotModuleStatus")
        def process_robot_module_status(self, payload: bytes):
            parse_AND_update("RobotModuleStatus", payload)

        @register("RobotPosition")
        def process_robot_position(self, payload: bytes):
            parse_AND_update("RobotPosition", payload)

        @register("Buff")
        def process_buff(self, payload: bytes):
            parse_AND_update("Buff", payload)

        @register("PenaltyInfo")
        def process_penalty_info(self, payload: bytes):
            parse_AND_update("PenaltyInfo", payload)

        @register("RobotPathPlanInfo")
        def process_robot_path_plan_info(self, payload: bytes):
            parse_AND_update("RobotPathPlanInfo", payload)

        @register("RadarInfoToClient")
        def process_radar_info_to_client(self, payload: bytes):
            parse_AND_update("RadarInfoToClient", payload)

        @register("CustomByteBlock")
        def process_custom_byte_block(self, payload: bytes):
            parse_AND_update("CustomByteBlock", payload)

        @register("TechCoreMotionStateSync")
        def process_tech_core_motion_state_sync(self, payload: bytes):
            parse_AND_update("TechCoreMotionStateSync", payload)

        @register("RobotPerformanceSelectionSync")
        def process_robot_performance_selection_sync(self, payload: bytes):
            parse_AND_update("RobotPerformanceSelectionSync", payload)

        @register("DeployModeStatusSync")
        def process_deploy_mode_status_sync(self, payload: bytes):
            parse_AND_update("DeployModeStatusSync", payload)

        @register("RuneStatusSync")
        def process_rune_status_sync(self, payload: bytes):
            parse_AND_update("RuneStatusSync", payload)

        @register("SentryStatusSync")
        def process_sentry_status_sync(self, payload: bytes):
            parse_AND_update("SentryStatusSync", payload)

        @register("DartSelectTargetStatusSync")
        def process_dart_select_target_status_sync(self, payload: bytes):
            parse_AND_update("DartSelectTargetStatusSync", payload)

        @register("SentryCtrlResult")
        def process_sentry_ctrl_result(self, payload: bytes):
            parse_AND_update("SentryCtrlResult", payload)

        @register("AirSupportStatusSync")
        def process_air_support_status_sync(self, payload: bytes):
            parse_AND_update("AirSupportStatusSync", payload)
    # ==============信息发送================
    def publish(self, topic: str, message: bytes):
        logger.debug(f"发布消息: {topic} - {message}")
        self.client.publish(topic, message)


class RMClientStates:

    def __init__(self) -> None:
        self.updates = {
            # GameStatus
            "red_score": 0,
            "blue_score": 0,
            "stage_countdown_sec": 0,
            "stage_elapsed_sec": 0,
            # GlobalUnitStatus
            # ally
            "base_health": 0,
            "base_status": 0,
            "base_shield": 0,
            "outpost_health": 0,
            "outpost_status": 0,
            # enemy
            "enemy_base_health": 0,
            "enemy_base_status": 0,
            "enemy_base_shield": 0,
            "enemy_outpost_health": 0,
            "enemy_outpost_status": 0,
            # 其他状态字段...
        }  
        self.side2color = {
            ALLY: UNKNOWN,
            ENEMY: UNKNOWN,
        }
        self.color2side = {
            RED: UNKNOWN,
            BLUE: UNKNOWN,
        }

    def set_ally_color(self, color: str):
        # 若color为RED，则ALLY对应RED，ENEMY对应BLUE；反之亦然
        if color == RED:
            self.side2color[ALLY] = RED
            self.side2color[ENEMY] = BLUE
            self.color2side[RED] = ALLY
            self.color2side[BLUE] = ENEMY
        elif color == BLUE:
            self.side2color[ALLY] = BLUE
            self.side2color[ENEMY] = RED
            self.color2side[BLUE] = ALLY
            self.color2side[RED] = ENEMY
        else:
            raise ValueError("Invalid color. Please specify 'RED' or 'BLUE'.")

    def get_time(self):
        """
        返回(剩余时间，已用时间，总时间)，单位为秒
         - 剩余时间 = stage_countdown_sec
         - 已用时间 = stage_elapsed_sec
         - 总时间 = TOTAL_TIME
        """
        return RMTime(
            remaining_time=self.updates.get("stage_countdown_sec", 0),
            passed_time=self.updates.get("stage_elapsed_sec", 0),
            total_time=TOTAL_TIME
        )

    def get_building_status(self, side: str = ALL_SIDES, building_type: str = ALL_BUILDINGS):
        """
        返回建筑状态，包括基地和前哨站的血量、状态和护盾
        """
        building_status = RMAllBuildingsStatus(
            RED=RMSideBuildingStatus(
                BASE=RMBuildingStatus(health=self.updates["base_health"], status=self.updates["base_status"], shield=self.updates["base_shield"]),
                OUTPOST=RMBuildingStatus(health=self.updates["outpost_health"], status=self.updates["outpost_status"], shield=0)
            ),
            BLUE=RMSideBuildingStatus(
                BASE=RMBuildingStatus(health=self.updates["base_health"], status=self.updates["base_status"], shield=self.updates["base_shield"]),
                OUTPOST=RMBuildingStatus(health=self.updates["outpost_health"], status=self.updates["outpost_status"], shield=0)
            )
        )
        if side == ALL_SIDES:
            assert building_type == ALL_BUILDINGS, "当side为ALL_SIDES时, building_type必须为ALL_BUILDINGS"
            return building_status
        else:
            assert side in (RED, BLUE), "Invalid side. Please specify 'RED', 'BLUE' or 'ALL_SIDES'."
            if building_type == ALL_BUILDINGS:
                return getattr(building_status, side)
            else:
                assert building_type in (BASE, OUTPOST), "Invalid building_type. Please specify 'BASE', 'OUTPOST' or 'ALL_BUILDINGS'."
                return getattr(getattr(building_status, side), building_type)
    

if __name__ == "__main__":
    r = RoboMasterMQTT(client_id=NAME_TO_CLIENT_ID[RED_HERO], host="localhost", port=3333)
    r.start()
    test_proto = DOWN_TOPIC2MODEL_MAP["GameStatus"]()

