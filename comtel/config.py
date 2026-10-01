"""配置模型与 JSON 持久化。

所有字段都有可用的默认值，配置文件不存在时直接使用默认值启动。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict

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


@dataclass
class BridgeConfig:
    # ---- 串口 ----
    port: str = ""
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    flow: str = "none"

    # ---- 网络 ----
    listen_host: str = "0.0.0.0"
    listen_port: int = 2323
    telnet_mode: str = "telnet"  # telnet | raw
    allow_multiple: bool = True
    writer_policy: str = "first"
    server_echo: bool = False
    banner: str = ""

    # ---- 数据转换 ----
    tx_newline: str = "cr"
    rx_normalize_crlf: bool = True
    encoding: str = "utf-8"

    # ---- 日志 / 显示 ----
    log_to_file: bool = False
    log_file: str = "logs/bridge.log"
    log_hex: bool = False
    timestamps: bool = True
    show_rx: bool = True
    show_tx: bool = True
    autoscroll: bool = True

    # ---- 界面 ----
    ui_scale: float = 1.0        # 界面字号缩放（1.0 = 100%，Ctrl+滚轮可调）
    window_geometry: str = ""    # 上次关闭时的窗口大小/位置，空 = 用默认尺寸

    # ---------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BridgeConfig":
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
            self.listen_port = 2323
        if not (0 < self.listen_port < 65536):
            self.listen_port = 2323

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

        try:
            "".encode(self.encoding)
        except (LookupError, TypeError):
            self.encoding = "utf-8"

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
        link = "Telnet" if self.telnet_mode == "telnet" else "TCP(裸)"
        stop = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        flow = "" if self.flow == "none" else "/" + self.flow
        return "%s@%s | %s%s%s%s | %s %s:%d" % (
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
