"""命令行模式：无界面运行，适合服务/随系统启动。

支持**多通道**：每个通道是一个「串口 + Telnet 端口」，可以一次把多个串口发布到不同端口。

    # 两个串口，各发布到一个端口（-p / -t 按顺序配对）
    python com_telnet_bridge.py -n -p COM3 -t 2323 -p COM5 -t 2324 -b 115200

    # 设备=端口[:波特率] 写法（socket:// 里带冒号也不会歧义）
    python com_telnet_bridge.py -n --map COM3=2323 --map socket://192.168.1.50:4001=2324:9600

    # 检查配置与端口冲突，不启动
    python com_telnet_bridge.py -n --check

不带 ``-p/--map`` 时直接使用 ``config.json`` 里保存的全部通道。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

from . import APP_NAME, __version__
from .config import (
    DEFAULT_LISTEN_PORT,
    BridgeConfig,
    ChannelConfig,
    default_config_path,
    is_port_available,
    suggest_channel_name,
)
from .gateway import hexdump
from .manager import ChannelManager
from .serial_link import list_serial_ports

LEVEL_TAGS = {"RX": "RX", "TX": "TX", "SYS": " SYS ", "ERR": " !!  "}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="com_telnet_bridge",
        description="%s v%s —— 把 COM 串口发布成 Telnet（多串口/多端口，支持多客户端广播）"
                    % (APP_NAME, __version__),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="示例：-p COM3 -t 2323 -p COM5 -t 2324 ；或 --map COM3=2323 --map COM5=2324",
    )
    # ---- 通道定义（可多次指定） ----
    p.add_argument("-p", "--port", action="append", metavar="串口",
                   help="串口名，可重复：-p COM3 -p COM5；也支持 loop:// socket://host:port")
    p.add_argument("-t", "--tcp-port", action="append", type=int, metavar="端口",
                   help="Telnet 监听端口，可重复，与 -p 按顺序配对：-p COM3 -t 2323 -p COM5 -t 2324")
    p.add_argument("--map", action="append", metavar="设备=端口[:波特率]",
                   help="一次性给一个通道指定串口与端口，可重复；与 -p/-t 不要混用")
    p.add_argument("--name", action="append", metavar="名称",
                   help="通道名（可重复，按顺序对应），默认取串口名")

    # ---- 所有通道共用的参数 ----
    p.add_argument("-b", "--baud", type=int, help="波特率（所有通道）")
    p.add_argument("--bytesize", type=int, choices=[5, 6, 7, 8], help="数据位")
    p.add_argument("-P", "--parity", choices=["N", "E", "O", "M", "S"], help="校验位")
    p.add_argument("--stopbits", type=float, choices=[1, 1.5, 2], help="停止位")
    p.add_argument("--flow", choices=["none", "rtscts", "dsrdtr", "xonxoff"], help="流控")

    p.add_argument("-H", "--listen-host", help="Telnet 监听地址")
    p.add_argument("--raw", action="store_true", help="裸 TCP 模式（不做 Telnet 协商）")
    p.add_argument("--single", action="store_true", help="每个通道只允许一个客户端")
    p.add_argument("--policy", choices=["first", "all", "none"], help="客户端写入权限策略")
    p.add_argument("--server-echo", action="store_true", help="由服务端回显客户端输入")
    p.add_argument("--banner", help="连接后发送的欢迎语，支持 \\r\\n 与 {port} {baud} {peer} {writable}")

    p.add_argument("--tx-newline", choices=["asis", "cr", "lf", "crlf", "crnul"], help="客户端到串口的换行转换")
    p.add_argument("--no-rx-crlf", action="store_true", help="串口到客户端不做 CRLF 归一化")
    p.add_argument("--encoding", help="文本编码，如 utf-8 / gbk")

    p.add_argument("--log", action="store_true", help="把日志写入文件")
    p.add_argument("--log-file", help="日志文件路径（多通道时自动加 -1/-2 后缀）")
    p.add_argument("--hex-log", action="store_true", help="日志里附带十六进制")
    p.add_argument("--quiet", action="store_true", help="不显示收发数据，只显示状态")

    p.add_argument("-c", "--config", default=default_config_path(), help="配置文件路径")
    p.add_argument("-n", "--no-gui", action="store_true", help="强制命令行模式")
    p.add_argument("--check", action="store_true", help="只检查通道配置与端口冲突，不启动")
    p.add_argument("--list-ports", action="store_true", help="列出本机串口与配置里的通道后退出")
    p.add_argument("-v", "--version", action="version", version="%s %s" % (APP_NAME, __version__))
    return p


# ----------------------------------------------------------------------
def parse_map(spec: str) -> Tuple[str, int, Optional[int]]:
    """解析 ``--map``：``COM3=2323``、``COM3=2323:115200``、``socket://1.2.3.4:4001=2324``。"""
    text = (spec or "").strip()
    if "=" not in text:
        raise ValueError("--map 需要「设备=端口」形式，例如 COM3=2323：%s" % spec)
    device, _, right = text.rpartition("=")
    device = device.strip()
    right = right.strip()

    baud: Optional[int] = None
    if ":" in right:
        right, _, baud_text = right.partition(":")
        right, baud_text = right.strip(), baud_text.strip()
        if not baud_text.isdigit():
            raise ValueError("--map 的波特率必须是数字：%s" % spec)
        baud = int(baud_text)

    if not device or not right.isdigit():
        raise ValueError("--map 需要「设备=端口」形式，例如 COM3=2323：%s" % spec)
    port = int(right)
    if not (0 < port < 65536):
        raise ValueError("--map 的端口需在 1-65535 之间：%s" % spec)
    return device, port, baud


def apply_common(channel: ChannelConfig, args: argparse.Namespace) -> None:
    """把命令行里的公共参数套到一个通道上。"""
    if args.baud:
        channel.baudrate = args.baud
    if args.bytesize:
        channel.bytesize = args.bytesize
    if args.parity:
        channel.parity = args.parity
    if args.stopbits:
        channel.stopbits = args.stopbits
    if args.flow:
        channel.flow = args.flow
    if args.listen_host:
        channel.listen_host = args.listen_host
    if args.raw:
        channel.telnet_mode = "raw"
    if args.single:
        channel.allow_multiple = False
    if args.policy:
        channel.writer_policy = args.policy
    if args.server_echo:
        channel.server_echo = True
    if args.banner is not None:
        channel.banner = args.banner
    if args.tx_newline:
        channel.tx_newline = args.tx_newline
    if args.no_rx_crlf:
        channel.rx_normalize_crlf = False
    if args.encoding:
        channel.encoding = args.encoding
    if args.log:
        channel.log_to_file = True
    if args.hex_log:
        channel.log_hex = True
    channel.normalize()


def build_channels(cfg: BridgeConfig, args: argparse.Namespace) -> List[ChannelConfig]:
    """按命令行参数得到本次要运行的通道列表。"""
    entries: List[Tuple[str, Optional[int], Optional[int]]] = []

    if args.map:
        if args.port or args.tcp_port:
            raise ValueError("--map 与 -p/-t 不要混用，选一种写法即可")
        for spec in args.map:
            entries.append(parse_map(spec))
    elif args.port:
        tcp_ports = list(args.tcp_port or [])
        for i, device in enumerate(args.port):
            entries.append((device.strip(), tcp_ports[i] if i < len(tcp_ports) else None, None))

    names = list(args.name or [])

    if not entries:
        # 没给通道参数：直接用配置文件里的通道
        channels = list(cfg.channels)
    else:
        template = cfg.channels[0] if cfg.channels else ChannelConfig()
        channels = []
        wanted: List[Optional[int]] = []
        used_names: set = set()
        for i, (device, tcp_port, baud) in enumerate(entries):
            channel = ChannelConfig.from_dict(template.to_dict())
            channel.port = device
            name = (names[i] if i < len(names) and names[i].strip()
                    else suggest_channel_name(device))
            # 同一个串口名出现多次（比如两个 loop://）时自动区分通道名，避免日志文件互相覆盖
            candidate, suffix = name, 2
            while candidate in used_names:
                candidate = "%s (%d)" % (name, suffix)
                suffix += 1
            channel.name = candidate
            used_names.add(candidate)
            if baud:
                channel.baudrate = baud
            channels.append(channel)
            wanted.append(tcp_port)

        # 没写端口的通道：从 2323（或配置文件里的端口）开始自动分配，跳过被占用的
        start = cfg.channels[0].listen_port if cfg.channels else DEFAULT_LISTEN_PORT
        if not (0 < start < 65536):
            start = DEFAULT_LISTEN_PORT
        taken = {port for port in wanted if port}
        for channel, tcp_port in zip(channels, wanted):
            if tcp_port:
                channel.listen_port = tcp_port
                continue
            port = start
            for _ in range(2000):
                if port not in taken and is_port_available(channel.listen_host or "0.0.0.0", port):
                    break
                port = port + 1 if port < 65535 else DEFAULT_LISTEN_PORT
            channel.listen_port = port
            taken.add(port)

    for channel in channels:
        apply_common(channel, args)

    # 日志文件：多通道时自动加序号后缀，避免几个通道抢同一个文件
    if args.log_file:
        base, ext = os.path.splitext(args.log_file)
        ext = ext or ".log"
        if len(channels) == 1:
            channels[0].log_file = args.log_file
        else:
            for i, channel in enumerate(channels, 1):
                channel.log_file = "%s-%d%s" % (base, i, ext)

    return channels


def print_ports(config_path: str = "") -> int:
    ports = list_serial_ports()
    if not ports:
        print("未检测到串口。")
    else:
        print("检测到 %d 个串口：" % len(ports))
        for item in ports:
            print("  %-10s %s" % (item["device"], item["description"]))

    if config_path and os.path.isfile(config_path):
        cfg = BridgeConfig.load(config_path)
        print("\n配置文件 %s 里的通道：" % config_path)
        for i, channel in enumerate(cfg.channels):
            print("  %d. %s" % (i + 1, channel.describe()))
    return 0 if ports else 1


def _print_channels(channels: List[ChannelConfig]) -> None:
    print("本次运行 %d 个通道：" % len(channels))
    for i, channel in enumerate(channels, 1):
        print("  %d. %s" % (i, channel.describe()))


# ----------------------------------------------------------------------
def run_cli(argv: Optional[List[str]] = None) -> int:
    from .console import setup_console

    setup_console()
    args = build_parser().parse_args(argv)
    if args.list_ports:
        return print_ports(args.config)

    cfg = BridgeConfig.load(args.config)
    try:
        channels = build_channels(cfg, args)
    except ValueError as exc:
        print("参数错误：%s" % exc, file=sys.stderr)
        return 2

    run_cfg = BridgeConfig(
        channels=channels,
        ui_scale=cfg.ui_scale,
        window_geometry=cfg.window_geometry,
        selected_channel=0,
        timestamps=cfg.timestamps,
        autoscroll=cfg.autoscroll,
    )
    run_cfg.normalize()
    channels = run_cfg.channels

    errors, warnings = run_cfg.conflicts()
    if warnings:
        for text in warnings:
            print("提示：%s" % text, file=sys.stderr)
    if errors:
        for text in errors:
            print("端口冲突：%s" % text, file=sys.stderr)
        return 1

    missing = [ch.name for ch in channels if not ch.port]
    if missing:
        print("错误：通道 %s 未指定串口，请用 -p COM3 或 --map COM3=2323，"
              "也可以先在图形界面里保存配置。" % "、".join(missing), file=sys.stderr)
        return 2

    _print_channels(channels)
    if args.check:
        for channel in channels:
            state = "可监听" if is_port_available(channel.listen_host, channel.listen_port) else "端口已被占用"
            print("  [%s] %s:%d %s" % (channel.name, channel.listen_host, channel.listen_port, state))
        print("检查完成：配置与端口都没有问题。")
        return 0

    multi = len(channels) > 1
    labels: Dict[int, str] = {}
    hex_flags: Dict[int, bool] = {}
    show_data = not args.quiet

    def emit(event: str, **payload) -> None:
        cid = payload.get("channel")
        prefix = ("[%s] " % labels.get(cid, "?")) if multi else ""
        if event == "log":
            level = payload.get("level", "SYS")
            if level in ("RX", "TX") and not show_data:
                return
            stamp = time.strftime("%H:%M:%S")
            line = "%s%s [%s] %s" % (prefix, stamp, LEVEL_TAGS.get(level, "INFO"), payload.get("text", ""))
            raw = payload.get("raw")
            if raw and hex_flags.get(cid):
                line += "  | HEX: " + hexdump(raw)
            print(line, flush=True)

    manager = ChannelManager(emit)
    manager.sync(channels)
    labels.update({ch.id: ch.name for ch in manager.channels()})
    hex_flags.update({ch.id: ch.cfg.log_hex for ch in manager.channels()})

    failures = manager.start_all()
    for cid, reason in failures.items():
        name = labels.get(cid, str(cid))
        print("[%s] 启动失败：%s" % (name, reason), file=sys.stderr)
    if not manager.running():
        print("没有任何通道启动成功。", file=sys.stderr)
        return 1

    print("\n%s 已启动，共 %d 个通道在运行：" % (APP_NAME, len(manager.running())))
    for channel in manager.running():
        print("  %-12s %s  ->  Telnet %s:%d" % (
            channel.name, channel.cfg.port, channel.cfg.listen_host, channel.cfg.listen_port))
    print("按 Ctrl+C 退出。", flush=True)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…", flush=True)
    finally:
        manager.stop_all()
    return 0
