"""配置模型与 JSON 持久化。

从 v2 起支持**多通道**：每个通道 = 一个串口 + 一个 Telnet 监听端口，各通道互相独立
（各自的客户端、写权限、日志文件），可以同时把 COM3 发布到 2323、COM5 发布到 2324……

* :class:`ChannelConfig` —— 一个通道的全部参数（串口 / 网络 / 转换 / 日志 / 显示）
* :class:`BridgeConfig`  —— 整个程序的配置：通道列表 + 界面相关项

旧版（v1）的单串口 ``config.json`` 会自动迁移成一个通道，不需要手工改配置。
所有字段都有可用的默认值，配置文件不存在时直接使用默认值启动。
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple

# 配置结构版本：1 = 单串口（旧），2 = 多通道
CONFIG_VERSION = 2
DEFAULT_LISTEN_PORT = 2323
MAX_CHANNELS = 32

# 界面缩放范围与窗口几何串格式（"1180x720" 或 "1180x720+100+80"）
UI_SCALE_MIN = 0.7
UI_SCALE_MAX = 2.0
WINDOW_GEOMETRY_RE = re.compile(r"^\d{3,5}x\d{3,5}(?:[+-]\d{1,5}[+-]\d{1,5})?$")

# 换行转换方式（客户端 -> 串口）
TX_NEWLINE_CHOICES = ("asis", "cr", "lf", "crlf", "crnul")
TX_NEWLINE_LABELS = {
    "asis": "不转换",
    "cr": "CR (\\r)",
    "lf": "LF (\\n)",
    "crlf": "CRLF (\\r\\n)",
    "crnul": "CR NUL (\\r\\0)",
}

# 客户端写入权限策略
WRITER_POLICY_CHOICES = ("first", "all", "none")
WRITER_POLICY_LABELS = {
    "first": "仅第一个客户端可写（其余只读）",
    "all": "所有客户端都可写",
    "none": "全部只读（只监听串口输出）",
}

BAUD_CHOICES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
PARITY_CHOICES = ("N", "E", "O", "M", "S")
PARITY_LABELS = {
    "N": "None",
    "E": "Even",
    "O": "Odd",
    "M": "Mark",
    "S": "Space",
}
BYTESIZE_CHOICES = (5, 6, 7, 8)
STOPBITS_CHOICES = (1, 1.5, 2)
FLOW_CHOICES = ("none", "rtscts", "dsrdtr", "xonxoff")
FLOW_LABELS = {
    "none": "无",
    "rtscts": "RTS/CTS 硬件",
    "dsrdtr": "DSR/DTR 硬件",
    "xonxoff": "XON/XOFF 软件",
}

# 文件名里不能出现的字符（通道名会被用来生成默认日志文件名）
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def app_dir() -> str:
    """程序所在目录（与当前工作目录无关）。

    打包成 exe 后 ``__file__`` 指向临时解包目录，必须改用 exe 自身的目录，
    这样 config.json / logs 才会落在 exe 旁边。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def safe_filename(name: str, fallback: str = "channel") -> str:
    """把通道名转成可以当文件名用的字符串。"""
    text = _INVALID_FILENAME_CHARS.sub("-", (name or "").strip()).strip(" .-")
    return text or fallback


def suggest_channel_name(device: str) -> str:
    """按设备名给通道起个默认名字（命令行用）：``COM3`` / ``socket-1.2.3.4-4001``。"""
    text = (device or "").strip()
    if not text:
        return "通道"
    return safe_filename(text, "通道")


def _is_wildcard(host: str) -> bool:
    """是否是「所有网卡」这类通配监听地址。"""
    text = (host or "").strip().strip("[]").lower()
    return text in ("", "*", "0.0.0.0", "::")


def is_port_available(host: str, port: int) -> bool:
    """本机现在能否在 ``host:port`` 上监听。

    故意**不设** ``SO_REUSEADDR``：Windows 上设了它会出现"抢占式"绑定，
    别的程序占着端口也能绑成功，检测就不准了。
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if not (0 < port < 65536):
        return False

    probe = (host or "").strip()
    if probe.startswith("["):
        probe = probe.strip("[]")
    family = socket.AF_INET
    if ":" in probe:                       # IPv6 字面量
        family = socket.AF_INET6
        probe = probe or "::"
    elif probe in ("", "*"):
        probe = "0.0.0.0"

    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((probe, port))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _unique_name(base: str, used: set) -> str:
    """在 ``used`` 里挑一个不重复的名字。"""
    name = (base or "").strip() or "通道"
    if name not in used:
        return name
    for i in range(2, 1000):
        candidate = "%s (%d)" % (name, i)
        if candidate not in used:
            return candidate
    return name


@dataclass
class ChannelConfig:
    """一个通道（一个串口 <-> 一个 Telnet 端口）。"""

    name: str = ""

    # ---- 串口 ----
    port: str = ""
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    flow: str = "none"

    # ---- 网络 ----
    listen_host: str = "0.0.0.0"
    listen_port: int = DEFAULT_LISTEN_PORT
    telnet_mode: str = "telnet"  # telnet | raw
    allow_multiple: bool = True
    writer_policy: str = "first"
    server_echo: bool = False
    banner: str = ""

    # ---- 数据转换 ----
    tx_newline: str = "cr"
    rx_normalize_crlf: bool = True
    encoding: str = "utf-8"

    # ---- 日志 / 显示（本通道） ----
    log_to_file: bool = False
    log_file: str = ""           # 空 = logs/<通道名>.log
    log_hex: bool = False
    show_rx: bool = True
    show_tx: bool = True

    # ---------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChannelConfig":
        known = {f.name: f.type for f in fields(cls)}
        kwargs: Dict[str, Any] = {}
        for key, value in (data or {}).items():
            if key in known and value is not None:
                kwargs[key] = value
        cfg = cls(**kwargs)
        cfg.normalize()
        return cfg

    def normalize(self) -> None:
        """把越界/非法的值收敛到安全值。"""
        self.name = (self.name or "").strip()

        try:
            self.baudrate = int(self.baudrate)
        except (TypeError, ValueError):
            self.baudrate = 115200
        if self.baudrate <= 0:
            self.baudrate = 115200

        if self.bytesize not in BYTESIZE_CHOICES:
            self.bytesize = 8
        if self.parity not in PARITY_CHOICES:
            self.parity = "N"
        if self.stopbits not in STOPBITS_CHOICES:
            self.stopbits = 1
        if self.flow not in FLOW_CHOICES:
            self.flow = "none"

        try:
            self.listen_port = int(self.listen_port)
        except (TypeError, ValueError):
            self.listen_port = DEFAULT_LISTEN_PORT
        if not (0 < self.listen_port < 65536):
            self.listen_port = DEFAULT_LISTEN_PORT

        self.listen_host = (self.listen_host or "0.0.0.0").strip()
        if self.telnet_mode not in ("telnet", "raw"):
            self.telnet_mode = "telnet"
        if self.writer_policy not in WRITER_POLICY_CHOICES:
            self.writer_policy = "first"
        if self.tx_newline not in TX_NEWLINE_CHOICES:
            self.tx_newline = "cr"

        self.allow_multiple = bool(self.allow_multiple)
        self.server_echo = bool(self.server_echo)
        self.rx_normalize_crlf = bool(self.rx_normalize_crlf)
        self.log_to_file = bool(self.log_to_file)
        self.log_hex = bool(self.log_hex)
        self.show_rx = bool(self.show_rx)
        self.show_tx = bool(self.show_tx)

        try:
            "".encode(self.encoding)
        except (LookupError, TypeError):
            self.encoding = "utf-8"

        self.log_file = (self.log_file or "").strip()
        if not self.log_file:
            self.log_file = "logs/%s.log" % safe_filename(self.name)

    # ---------------------------------------------------------------
    def describe(self) -> str:
        link = "Telnet" if self.telnet_mode == "telnet" else "TCP(裸)"
        stop = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        flow = "" if self.flow == "none" else "/" + self.flow
        return "[%s] %s@%s | %s%s%s%s | %s %s:%d" % (
            self.name or "通道",
            self.port or "(未选择)",
            self.baudrate,
            self.bytesize,
            self.parity,
            stop,
            flow,
            link,
            self.listen_host,
            self.listen_port,
        )

    def endpoint(self) -> Tuple[str, int]:
        """监听标识，用于查重：``("0.0.0.0", 2323)``。"""
        return ((self.listen_host or "0.0.0.0").strip().lower() or "0.0.0.0", int(self.listen_port))


@dataclass
class BridgeConfig:
    """整个程序的配置：若干通道 + 界面相关项。"""

    channels: List[ChannelConfig] = field(default_factory=list)

    # ---- 界面（全局） ----
    ui_scale: float = 1.0        # 界面字号缩放（1.0 = 100%，Ctrl+滚轮可调）
    window_geometry: str = ""    # 上次关闭时的窗口大小/位置，空 = 用默认尺寸
    selected_channel: int = 0    # 上次选中的通道下标

    # ---- 日志窗口的共享显示项 ----
    timestamps: bool = True
    autoscroll: bool = True

    def __post_init__(self) -> None:
        if not self.channels:
            self.channels = [ChannelConfig()]

    # ---------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "ui_scale": round(float(self.ui_scale), 2),
            "window_geometry": self.window_geometry,
            "selected_channel": int(self.selected_channel),
            "timestamps": bool(self.timestamps),
            "autoscroll": bool(self.autoscroll),
            "channels": [ch.to_dict() for ch in self.channels],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BridgeConfig":
        data = data or {}
        raw_channels = data.get("channels")
        if isinstance(raw_channels, list):
            channels = [
                item if isinstance(item, ChannelConfig) else ChannelConfig.from_dict(item)
                for item in raw_channels
                if isinstance(item, (dict, ChannelConfig))
            ]
        else:
            # v1：整个文件就是一个串口的参数
            channels = [ChannelConfig.from_dict(data)]

        cfg = cls(
            channels=channels,
            ui_scale=data.get("ui_scale", 1.0),
            window_geometry=data.get("window_geometry", ""),
            selected_channel=data.get("selected_channel", 0),
            timestamps=data.get("timestamps", True),
            autoscroll=data.get("autoscroll", True),
        )
        cfg.normalize()
        return cfg

    def normalize(self) -> None:
        """收敛到合法状态：至少 1 个通道、名字不重复、界面项合法。"""
        if not isinstance(self.channels, list):
            self.channels = []
        fixed: List[ChannelConfig] = []
        for item in self.channels:
            if isinstance(item, ChannelConfig):
                fixed.append(item)
            elif isinstance(item, dict):
                fixed.append(ChannelConfig.from_dict(item))
        self.channels = fixed or [ChannelConfig()]
        del self.channels[MAX_CHANNELS:]

        used: set = set()
        for i, ch in enumerate(self.channels):
            ch.normalize()
            base = ch.name.strip() or ("通道%d" % (i + 1))
            ch.name = _unique_name(base, used)
            used.add(ch.name)
            if not ch.log_file.strip():
                ch.log_file = "logs/%s.log" % safe_filename(ch.name)

        # ---- 界面 ----
        try:
            self.ui_scale = float(self.ui_scale)
        except (TypeError, ValueError):
            self.ui_scale = 1.0
        if self.ui_scale != self.ui_scale:  # NaN
            self.ui_scale = 1.0
        self.ui_scale = round(min(UI_SCALE_MAX, max(UI_SCALE_MIN, self.ui_scale)), 2)

        geometry = (self.window_geometry or "").strip()
        self.window_geometry = geometry if WINDOW_GEOMETRY_RE.match(geometry) else ""

        try:
            index = int(self.selected_channel)
        except (TypeError, ValueError):
            index = 0
        self.selected_channel = min(max(0, index), len(self.channels) - 1)

        self.timestamps = bool(self.timestamps)
        self.autoscroll = bool(self.autoscroll)

    # ---------------------------------------------------------------
    def save(self, path: str = "") -> str:
        target = path or default_config_path()
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        return target

    @classmethod
    def load(cls, path: str = "") -> "BridgeConfig":
        target = path or default_config_path()
        if not os.path.isfile(target):
            cfg = cls()
            cfg.normalize()
            return cfg
        try:
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            cfg = cls()
            cfg.normalize()
            return cfg
        return cls.from_dict(data)

    def describe(self) -> str:
        if len(self.channels) == 1:
            return self.channels[0].describe()
        return "%d 个通道：" % len(self.channels) + "；".join(ch.describe() for ch in self.channels)

    # ---------------------------------------------------------------
    # 通道管理辅助（界面 / 命令行共用）
    # ---------------------------------------------------------------
    def next_channel_name(self) -> str:
        used = {ch.name for ch in self.channels}
        for i in range(1, 1000):
            candidate = "通道%d" % i
            if candidate not in used:
                return candidate
        return "通道"

    def used_ports(self, exclude: Optional[ChannelConfig] = None) -> set:
        """已被其它通道占用的监听端口（不看监听地址，端口相同就算冲突）。"""
        return {ch.listen_port for ch in self.channels if ch is not exclude}

    def next_free_port(self, host: str = "0.0.0.0", exclude: Optional[ChannelConfig] = None,
                       start: int = DEFAULT_LISTEN_PORT) -> int:
        """从 ``start`` 开始找一个既没被其它通道占用、本机也真的能监听的端口。"""
        used = self.used_ports(exclude)
        try:
            port = int(start)
        except (TypeError, ValueError):
            port = DEFAULT_LISTEN_PORT
        for _ in range(1000):
            if not (0 < port < 65536):
                port = DEFAULT_LISTEN_PORT
            if port not in used and is_port_available(host, port):
                return port
            port += 1
        return DEFAULT_LISTEN_PORT

    def new_channel(self, copy_from: Optional[ChannelConfig] = None) -> ChannelConfig:
        """新建一个通道：默认名字、自动分配监听端口与日志文件。"""
        base = copy_from.to_dict() if copy_from is not None else {}
        channel = ChannelConfig.from_dict(base)
        channel.name = self.next_channel_name()
        channel.listen_port = self.next_free_port(host=channel.listen_host or "0.0.0.0")
        channel.log_file = "logs/%s.log" % safe_filename(channel.name)
        return channel

    def conflicts(self, exclude: Optional[ChannelConfig] = None) -> Tuple[List[str], List[str]]:
        """检查通道之间的冲突。

        返回 ``(errors, warnings)``：

        * errors：两个通道要抢同一个端口（其中至少一个是 ``0.0.0.0`` 这种通配地址，
          或者监听地址完全相同）—— 必须先改掉才能启动。Windows 上带 SO_REUSEADDR 的
          第二个绑定会**静默成功**，两个通道抢一个端口时连接会随机落到其中一个；
        * warnings：两个通道端口相同但监听地址是各自具体的网卡（理论上能共存，但同样
          容易互相抢，只提示不阻断）；以及两个通道用同一个串口设备（多半有一个打不开）。

        回环口（``loop://``）各自独立，重复使用不算问题。
        """
        errors: List[str] = []
        warnings: List[str] = []

        by_port: Dict[int, List[ChannelConfig]] = {}
        for channel in self.channels:
            if channel is exclude:
                continue
            by_port.setdefault(int(channel.listen_port), []).append(channel)

        for port, group in sorted(by_port.items()):
            if len(group) < 2:
                continue
            hosts = [ch.listen_host for ch in group]
            wildcard = any(_is_wildcard(host) for host in hosts)
            distinct = len({(host or "").strip().lower() for host in hosts}) == len(hosts)
            if wildcard or not distinct:
                first, second = group[0], group[1]
                errors.append(
                    "通道「%s」（%s:%d）与「%s」（%s:%d）要抢同一个端口，请给其中一个换端口"
                    % (first.name, first.listen_host, port, second.name, second.listen_host, port)
                )
            else:
                names = "、".join("「%s」%s" % (ch.name, ch.listen_host) for ch in group)
                warnings.append(
                    "通道 %s 都用端口 %d，只是监听地址不同；Windows 上仍可能互相抢，建议换端口"
                    % (names, port)
                )

        devices: Dict[str, str] = {}
        for channel in self.channels:
            if channel is exclude:
                continue
            device = (channel.port or "").strip().lower()
            if not device or device.startswith("loop://"):
                continue
            if device in devices:
                warnings.append(
                    "通道「%s」与「%s」都使用串口 %s，第二个多半会提示拒绝访问"
                    % (devices[device], channel.name, channel.port)
                )
            else:
                devices[device] = channel.name
        return errors, warnings
