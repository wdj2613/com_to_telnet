"""命令行模式：无界面运行，适合服务/随系统启动。"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from . import APP_NAME, __version__
from .config import BridgeConfig, default_config_path
from .gateway import Gateway, hexdump
from .serial_link import list_serial_ports

LEVEL_TAGS = {"RX": "RX", "TX": "TX", "SYS": " SYS ", "ERR": " !!  "}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="com_telnet_bridge",
        description="%s v%s —— 把 COM 串口发布成 Telnet（支持多客户端广播）" % (APP_NAME, __version__),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-p", "--port", help="串口名，如 COM3；也支持 loop:// socket://host:port")
    p.add_argument("-b", "--baud", type=int, help="波特率")
    p.add_argument("--bytesize", type=int, choices=[5, 6, 7, 8], help="数据位")
    p.add_argument("-P", "--parity", choices=["N", "E", "O", "M", "S"], help="校验位")
    p.add_argument("--stopbits", type=float, choices=[1, 1.5, 2], help="停止位")
    p.add_argument("--flow", choices=["none", "rtscts", "dsrdtr", "xonxoff"], help="流控")

    p.add_argument("-H", "--listen-host", help="Telnet 监听地址")
    p.add_argument("-t", "--tcp-port", type=int, help="Telnet 监听端口")
    p.add_argument("--raw", action="store_true", help="裸 TCP 模式（不做 Telnet 协商）")
    p.add_argument("--single", action="store_true", help="只允许一个客户端，其余拒绝")
    p.add_argument("--policy", choices=["first", "all", "none"], help="客户端写入权限策略")
    p.add_argument("--server-echo", action="store_true", help="由服务端回显客户端输入")
    p.add_argument("--banner", help="连接后发送的欢迎语，支持 \\r\\n 与 {port} {baud} {peer} {writable}")

    p.add_argument("--tx-newline", choices=["asis", "cr", "lf", "crlf", "crnul"], help="客户端到串口的换行转换")
    p.add_argument("--no-rx-crlf", action="store_true", help="串口到客户端不做 CRLF 归一化")
    p.add_argument("--encoding", help="文本编码，如 utf-8 / gbk")

    p.add_argument("--log", action="store_true", help="把日志写入文件")
    p.add_argument("--log-file", help="日志文件路径")
    p.add_argument("--hex-log", action="store_true", help="日志里附带十六进制")
    p.add_argument("--quiet", action="store_true", help="不显示收发数据，只显示状态")

    p.add_argument("-c", "--config", default=default_config_path(), help="配置文件路径")
    p.add_argument("-n", "--no-gui", action="store_true", help="强制命令行模式")
    p.add_argument("--list-ports", action="store_true", help="列出本机串口后退出")
    p.add_argument("-v", "--version", action="version", version="%s %s" % (APP_NAME, __version__))
    return p


def apply_args(cfg: BridgeConfig, args: argparse.Namespace) -> BridgeConfig:
    if args.port:
        cfg.port = args.port
    if args.baud:
        cfg.baudrate = args.baud
    if args.bytesize:
        cfg.bytesize = args.bytesize
    if args.parity:
        cfg.parity = args.parity
    if args.stopbits:
        cfg.stopbits = args.stopbits
    if args.flow:
        cfg.flow = args.flow
    if args.listen_host:
        cfg.listen_host = args.listen_host
    if args.tcp_port:
        cfg.listen_port = args.tcp_port
    if args.raw:
        cfg.telnet_mode = "raw"
    if args.single:
        cfg.allow_multiple = False
    if args.policy:
        cfg.writer_policy = args.policy
    if args.server_echo:
        cfg.server_echo = True
    if args.banner is not None:
        cfg.banner = args.banner
    if args.tx_newline:
        cfg.tx_newline = args.tx_newline
    if args.no_rx_crlf:
        cfg.rx_normalize_crlf = False
    if args.encoding:
        cfg.encoding = args.encoding
    if args.log:
        cfg.log_to_file = True
    if args.log_file:
        cfg.log_file = args.log_file
    if args.hex_log:
        cfg.log_hex = True
    cfg.normalize()
    return cfg


def print_ports() -> int:
    ports = list_serial_ports()
    if not ports:
        print("未检测到串口。")
        return 1
    print("检测到 %d 个串口：" % len(ports))
    for item in ports:
        print("  %-10s %s" % (item["device"], item["description"]))
    return 0


def run_cli(argv: Optional[List[str]] = None) -> int:
    from .console import setup_console

    setup_console()
    args = build_parser().parse_args(argv)
    if args.list_ports:
        return print_ports()

    cfg = BridgeConfig.load(args.config)
    apply_args(cfg, args)
    if not cfg.port:
        print("错误：未指定串口，请用 -p COM3，或先在图形界面里保存配置。", file=sys.stderr)
        return 2

    show_data = not args.quiet
    state = {"running": False}

    def emit(event: str, **payload) -> None:
        if event == "log":
            level = payload.get("level", "SYS")
            if level in ("RX", "TX") and not show_data:
                return
            stamp = time.strftime("%H:%M:%S")
            line = "%s [%s] %s" % (stamp, LEVEL_TAGS.get(level, "INFO"), payload.get("text", ""))
            raw = payload.get("raw")
            if raw and cfg.log_hex:
                line += "  | HEX: " + hexdump(raw)
            print(line, flush=True)
        elif event == "stats":
            state["rx"] = payload.get("rx", 0)
            state["tx"] = payload.get("tx", 0)
            state["clients"] = payload.get("clients", 0)
        elif event == "state":
            state["running"] = payload.get("running", False)

    gateway = Gateway(cfg, emit)
    try:
        gateway.start()
    except Exception as exc:
        print("启动失败：%s" % exc, file=sys.stderr)
        return 1

    print("%s 已启动：%s" % (APP_NAME, cfg.describe()))
    print("Telnet 连接地址： %s:%d" % (cfg.listen_host, cfg.listen_port))
    print("按 Ctrl+C 退出。", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…", flush=True)
    finally:
        gateway.stop()
    return 0
