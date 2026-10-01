#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自检脚本：不需要真实串口，用内置 ``loop://`` 回环口和本地 TCP 回显服务验证整条链路。

覆盖：
1. Telnet 协商、多客户端广播、换行转换、0xFF 转义、只读客户端拦截
2. 裸 TCP 模式（不做协商、不转义）
3. ``socket://`` 串口服务器后端
4. 单客户端模式（第二个连接被拒绝）
5. 服务端回显模式
6. 真实 COM 口打不开时的报错信息与 Win32 结构体布局
7. 多通道：多个串口 -> 多个端口，互不串台、单独启停、端口释放
8. 多通道：端口冲突检查与故障隔离（一个通道坏了不影响其它通道）
9. 多通道配置：旧配置迁移、存取往返、自动分配端口/名字/日志文件
10. 命令行多通道参数解析（-p/-t 配对、--map、自动分配端口）

用法： ``.venv\\Scripts\\python.exe tests\\selftest.py``
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import socketserver
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from comtel import cli  # noqa: E402
from comtel.config import BridgeConfig, ChannelConfig  # noqa: E402
from comtel.console import setup_console  # noqa: E402
from comtel.gateway import Gateway  # noqa: E402
from comtel.manager import ChannelManager  # noqa: E402
from comtel.serial_backend import IS_WINDOWS, open_serial  # noqa: E402
from comtel.telnet_server import DO, DONT, IAC, OPT_ECHO, OPT_SGA, WILL, WONT  # noqa: E402

setup_console()

FAILED = []
HOST = "127.0.0.1"


# ----------------------------------------------------------------------
def check(name: str, ok: bool, detail: str = "") -> None:
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" —— " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def recv_some(sock: socket.socket, timeout: float = 1.5) -> bytes:
    sock.settimeout(timeout)
    try:
        return sock.recv(4096)
    except socket.timeout:
        return b""


def drain(sock: socket.socket, timeout: float = 0.3) -> bytes:
    buf = b""
    while True:
        chunk = recv_some(sock, timeout)
        if not chunk:
            return buf
        buf += chunk


def connect(port: int) -> socket.socket:
    return socket.create_connection((HOST, port), timeout=3)


def make_gateway(**overrides) -> Gateway:
    params = dict(
        port="loop://",
        baudrate=115200,
        listen_host=HOST,
        listen_port=free_port(),
        telnet_mode="telnet",
        allow_multiple=True,
        writer_policy="first",
        tx_newline="cr",
        rx_normalize_crlf=True,
        log_to_file=False,
    )
    params.update(overrides)
    return Gateway(ChannelConfig(**params), lambda event, **kw: None)


class EchoTCPHandler(socketserver.BaseRequestHandler):
    """模拟串口服务器：收到什么就回什么。"""

    def handle(self) -> None:
        while True:
            try:
                data = self.request.recv(4096)
            except OSError:
                return
            if not data:
                return
            try:
                self.request.sendall(data)
            except OSError:
                return


class EchoTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ----------------------------------------------------------------------
def test_telnet_loopback() -> None:
    print("\n[1] Telnet 模式 + loop:// 回环")
    cfg_port = free_port()
    gw = make_gateway(listen_port=cfg_port, banner="欢迎 {port}@{baud} [{writable}]")
    gw.start()
    try:
        a = connect(cfg_port)
        hello = drain(a)
        check("客户端 A 收到欢迎语", "欢迎".encode("utf-8") in hello, repr(hello[:60]))
        check("Telnet 协商：WONT ECHO", bytes([IAC, WONT, OPT_ECHO]) in hello, repr(hello[:12]))
        check("Telnet 协商：WILL SGA", bytes([IAC, WILL, OPT_SGA]) in hello, repr(hello[:12]))
        check("第一个客户端可写", "读写".encode("utf-8") in hello, repr(hello[-20:]))

        b = connect(cfg_port)
        drain(b)
        time.sleep(0.2)
        check("网关记录 2 个客户端", gw.client_count == 2, "count=%d" % gw.client_count)

        a.sendall(b"hello\r\n")
        time.sleep(0.5)
        got_a, got_b = drain(a), drain(b)
        check("A 的输入经串口广播给 A", b"hello" in got_a, repr(got_a[:40]))
        check("A 的输入经串口广播给 B", b"hello" in got_b, repr(got_b[:40]))
        check("换行被转换为 CRLF", b"hello\r\n" in (got_a + got_b), repr(got_a[:40]))

        a.sendall(b"\xff\xff")  # 客户端用 IAC IAC 表示数据 0xFF
        time.sleep(0.5)
        check("0xFF 在 Telnet 模式下被正确转义", bytes([IAC, IAC]) in drain(a))

        rx_before = gw.serial.rx_bytes
        b.sendall(b"SHOULD-NOT-PASS\r\n")
        time.sleep(0.6)
        check("只读客户端收到提示", "只读".encode("utf-8") in drain(b))
        check("只读客户端输入未写入串口", gw.serial.rx_bytes == rx_before,
              "rx %d -> %d" % (rx_before, gw.serial.rx_bytes))
        check("只读输入未被广播", b"SHOULD-NOT-PASS" not in drain(a))

        gw.send_to_serial("PING")
        time.sleep(0.5)
        check("本地发送可见于客户端", b"PING" in (drain(a) + drain(b)))

        a.sendall(bytes([0x80, 0x81, 0xC0, 0xFE]))
        time.sleep(0.5)
        check("8 位数据可透传", bytes([0x80, 0x81, 0xC0, 0xFE]) in (drain(a) + drain(b)))

        a.close()
        time.sleep(0.4)
        check("客户端断开后计数下降", gw.client_count == 1, "count=%d" % gw.client_count)
        check("最早客户端离开后，剩余客户端接管写权限", gw.clients()[0].writable)
        b.close()
        time.sleep(0.4)
        check("全部断开后计数为 0", gw.client_count == 0, "count=%d" % gw.client_count)
        check("串口统计非零", gw.serial.rx_bytes > 0 and gw.serial.tx_bytes > 0,
              "rx=%d tx=%d" % (gw.serial.rx_bytes, gw.serial.tx_bytes))
    finally:
        gw.stop()


def test_raw_mode() -> None:
    print("\n[2] 裸 TCP 模式")
    port = free_port()
    gw = make_gateway(listen_port=port, telnet_mode="raw", banner="RAW")
    gw.start()
    try:
        c = connect(port)
        hello = drain(c)
        check("裸 TCP 不发送 Telnet 协商", bytes([IAC]) not in hello, repr(hello[:20]))
        check("裸 TCP 收到欢迎语", b"RAW" in hello, repr(hello[:20]))
        c.sendall(b"\xff\x01\x02")
        time.sleep(0.5)
        got = drain(c)
        check("裸 TCP 原样透传 0xFF", b"\xff\x01\x02" in got, repr(got))
    finally:
        gw.stop()


def test_socket_backend() -> None:
    print("\n[3] socket:// 串口服务器后端")
    echo_port = free_port()
    echo = EchoTCPServer((HOST, echo_port), EchoTCPHandler)
    threading.Thread(target=echo.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    port = free_port()
    gw = make_gateway(port="socket://%s:%d" % (HOST, echo_port), listen_port=port)
    try:
        gw.start()
        c = connect(port)
        drain(c)
        c.sendall(b"AT\r\n")
        time.sleep(0.6)
        got = drain(c)
        check("socket:// 后端可双向透传", b"AT" in got, repr(got[:40]))
        check("socket:// 后端统计非零", gw.serial.rx_bytes > 0 and gw.serial.tx_bytes > 0,
              "rx=%d tx=%d" % (gw.serial.rx_bytes, gw.serial.tx_bytes))
        c.close()
    finally:
        gw.stop()
        echo.shutdown()
        echo.server_close()


def test_single_client() -> None:
    print("\n[4] 单客户端模式")
    port = free_port()
    gw = make_gateway(listen_port=port, allow_multiple=False)
    gw.start()
    try:
        a = connect(port)
        drain(a)
        b = connect(port)
        reply = drain(b)
        check("第二个连接被拒绝", "拒绝".encode("utf-8") in reply, repr(reply[:60]))
        check("第一个连接仍在", gw.client_count == 1, "count=%d" % gw.client_count)
        a.close()
        b.close()
    finally:
        gw.stop()


def test_server_echo() -> None:
    print("\n[5] 服务端回显模式")
    port = free_port()
    gw = make_gateway(listen_port=port, server_echo=True)
    gw.start()
    try:
        c = connect(port)
        hello = drain(c)
        check("开启回显时声明 WILL ECHO", bytes([IAC, WILL, OPT_ECHO]) in hello, repr(hello[:12]))
        c.sendall(bytes([IAC, DO, OPT_ECHO]) + b"ab")
        time.sleep(0.5)
        got = drain(c)
        check("客户端输入被服务端回显", b"ab" in got, repr(got[:40]))
        check("回显后仍广播到串口", gw.serial.tx_bytes > 0, "tx=%d" % gw.serial.tx_bytes)
        c.close()
    finally:
        gw.stop()

    # 客户端拒绝回显时应自动关闭
    port = free_port()
    gw = make_gateway(listen_port=port, server_echo=True)
    gw.start()
    try:
        c = connect(port)
        drain(c)
        c.sendall(bytes([IAC, DONT, OPT_ECHO]) + b"cd")
        time.sleep(0.5)
        got = drain(c)
        check("客户端拒绝回显后不再回显", got.count(b"cd") <= 1, repr(got[:60]))
        c.close()
    finally:
        gw.stop()


def test_errors() -> None:
    print("\n[6] 错误处理与 Win32 结构体布局")
    if IS_WINDOWS:
        from comtel import serial_backend as sb

        check("DCB 结构体大小 = 28 字节", ctypes.sizeof(sb._DCB) == 28, "%d" % ctypes.sizeof(sb._DCB))
        check("COMMTIMEOUTS 大小 = 20 字节", ctypes.sizeof(sb._COMMTIMEOUTS) == 20,
              "%d" % ctypes.sizeof(sb._COMMTIMEOUTS))
        check("COMSTAT 大小 = 12 字节", ctypes.sizeof(sb._COMSTAT) == 12, "%d" % ctypes.sizeof(sb._COMSTAT))
        flags = sb.Win32Serial._dcb_flags("none", "N")
        check("DCB flags：默认 fBinary+DTR/RTS 使能",
              flags == (1 << 0) | (1 << 4) | (1 << 12), hex(flags))
        flags = sb.Win32Serial._dcb_flags("rtscts", "E")
        expected = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 4) | (2 << 12)
        check("DCB flags：RTS/CTS + 偶校验", flags == expected, hex(flags))
        flags = sb.Win32Serial._dcb_flags("xonxoff", "N")
        check("DCB flags：XON/XOFF", flags == (1 << 0) | (1 << 4) | (1 << 8) | (1 << 9) | (1 << 12), hex(flags))
        check("设备路径自动补 \\\\.\\ 前缀",
              sb.Win32Serial._device_path("COM3") == "\\\\.\\COM3"
              and sb.Win32Serial._device_path("\\\\.\\COM10") == "\\\\.\\COM10")

        try:
            open_serial("COM249").open()
            check("打开不存在的 COM 口应报错", False, "竟然成功了")
        except Exception as exc:
            check("打开不存在的 COM 口报错清晰", "打开 COM249 失败" in str(exc), str(exc))
    gw = make_gateway(port="COM249")
    try:
        gw.start()
        check("串口打不开时网关启动失败", False, "应当抛异常")
    except Exception as exc:
        check("串口打不开时网关启动失败", True, str(exc)[:60])
    check("启动失败后不残留监听", not gw.running and not gw.server.is_running)


def make_manager(channels, events=None) -> ChannelManager:
    """按通道配置建一个管理器（事件都收集到 events 里，方便断言通道号）。"""
    sink = events if events is not None else []
    manager = ChannelManager(lambda event, **kw: sink.append((event, kw)))
    manager.sync(channels)
    return manager


def test_multi_channel() -> None:
    print("\n[7] 多通道：多个串口 -> 不同端口，互不串台")
    events = []
    port_a, port_b = free_port(), free_port()
    cfg = BridgeConfig(channels=[
        ChannelConfig(name="A", port="loop://", listen_host=HOST, listen_port=port_a),
        ChannelConfig(name="B", port="loop://", listen_host=HOST, listen_port=port_b),
    ])
    manager = make_manager(cfg.channels, events)
    failures = manager.start_all()
    check("两个通道同时启动成功", not failures, str(failures))
    check("管理器统计到 2 个通道在运行", len(manager.running()) == 2,
          "running=%d" % len(manager.running()))
    try:
        a = connect(port_a)
        b = connect(port_b)
        drain(a)
        drain(b)

        a.sendall(b"AAA-FROM-A\r\n")
        time.sleep(0.7)
        got_a, got_b = drain(a), drain(b)
        check("A 通道数据回显给 A 的客户端", b"AAA-FROM-A" in got_a, repr(got_a[:40]))
        check("A 通道数据不会串到 B 的客户端", b"AAA-FROM-A" not in got_b, repr(got_b[:40]))

        b.sendall(b"BBB-FROM-B\r\n")
        time.sleep(0.7)
        got_a2, got_b2 = drain(a), drain(b)
        check("B 通道数据回显给 B 的客户端", b"BBB-FROM-B" in got_b2, repr(got_b2[:40]))
        check("B 通道数据不会串到 A 的客户端", b"BBB-FROM-B" not in got_a2, repr(got_a2[:40]))

        check("两个通道各自统计到客户端", manager.channels()[0].client_count == 1
              and manager.channels()[1].client_count == 1,
              "%d / %d" % (manager.channels()[0].client_count, manager.channels()[1].client_count))

        totals = manager.totals()
        check("汇总统计把两个通道加在一起", totals["running"] == 2 and totals["clients"] == 2,
              str(totals))

        ids = {payload.get("channel") for event, payload in events if event == "log"}
        check("事件里带上了通道号（界面靠它区分）", len({i for i in ids if i is not None}) == 2, str(ids))

        first_id = manager.channels()[0].id
        manager.stop(first_id)
        check("单独停掉一个通道", len(manager.running()) == 1 and manager.running()[0].cfg.name == "B",
              str([ch.cfg.name for ch in manager.running()]))
        check("另一个通道的客户端还在", manager.channels()[1].client_count == 1,
              "%d" % manager.channels()[1].client_count)
        check("停掉的通道端口已释放（可以重新监听）",
              _can_bind(port_a), str(port_a))

        failures = manager.start_channels([manager.channels()[0]])
        check("停掉后可以再单独启动它", not failures and len(manager.running()) == 2, str(failures))

        a.close()
        b.close()
        time.sleep(0.3)
        manager.stop_all()
        check("全部停止后没有通道在运行", not manager.running())
    finally:
        manager.stop_all()


def test_multi_channel_errors() -> None:
    print("\n[8] 多通道：端口冲突与故障隔离")
    shared = free_port()
    cfg = BridgeConfig(channels=[
        ChannelConfig(name="一", port="loop://", listen_host=HOST, listen_port=shared),
        ChannelConfig(name="二", port="loop://", listen_host=HOST, listen_port=shared),
    ])
    errors, warnings = cfg.conflicts()
    check("两个通道监听同一端口被识别为冲突", bool(errors), str(errors))

    # 监听地址写法不同、端口相同：只要有一个是 0.0.0.0 就算冲突
    mixed = BridgeConfig(channels=[
        ChannelConfig(name="一", port="loop://", listen_host=HOST, listen_port=shared),
        ChannelConfig(name="二", port="loop://", listen_host="0.0.0.0", listen_port=shared),
    ])
    errors, warnings = mixed.conflicts()
    check("一个监听 127.0.0.1、一个监听 0.0.0.0 但端口相同也算冲突", bool(errors), str(errors))

    # 两个各自具体的网卡地址 + 同一端口：理论能共存，只提示
    separate = BridgeConfig(channels=[
        ChannelConfig(name="一", port="loop://", listen_host="127.0.0.1", listen_port=shared),
        ChannelConfig(name="二", port="loop://", listen_host="192.168.1.10", listen_port=shared),
    ])
    errors, warnings = separate.conflicts()
    check("两个不同的具体地址用同一端口只给提示",
          not errors and bool(warnings), "%s / %s" % (errors, warnings))

    manager = make_manager(cfg.channels)
    failures = manager.start_all()
    check("冲突时只启动一个通道", len(manager.running()) == 1, str([ch.cfg.name for ch in manager.running()]))
    check("冲突原因里说清了是哪个通道占了端口",
          any("占用" in reason or "监听" in reason for reason in failures.values()), str(failures))
    manager.stop_all()

    # 串口打不开的通道不能拖垮别的通道
    good_port = free_port()
    cfg = BridgeConfig(channels=[
        ChannelConfig(name="坏", port="COM249", listen_host=HOST, listen_port=free_port()),
        ChannelConfig(name="好", port="loop://", listen_host=HOST, listen_port=good_port),
    ])
    manager = make_manager(cfg.channels)
    failures = manager.start_all()
    check("打不开串口的通道被单独报错", len(failures) == 1, str(failures))
    check("另一个通道不受影响，照常运行", len(manager.running()) == 1 and manager.running()[0].cfg.name == "好",
          str([ch.cfg.name for ch in manager.running()]))
    client = connect(good_port)
    drain(client)
    client.sendall(b"STILL-ALIVE\r\n")
    time.sleep(0.6)
    check("好通道的客户端仍能正常收发", b"STILL-ALIVE" in drain(client))
    client.close()
    manager.stop_all()

    # 同一个串口被两个通道使用：只提示，不阻断
    cfg = BridgeConfig(channels=[
        ChannelConfig(name="一", port="COM3", listen_host=HOST, listen_port=free_port()),
        ChannelConfig(name="二", port="COM3", listen_host=HOST, listen_port=free_port()),
    ])
    errors, warnings = cfg.conflicts()
    check("重复使用同一个串口只给提示不阻断", not errors and bool(warnings), "%s / %s" % (errors, warnings))

    # 回环口重复使用不算问题
    cfg = BridgeConfig(channels=[
        ChannelConfig(name="一", port="loop://", listen_host=HOST, listen_port=free_port()),
        ChannelConfig(name="二", port="loop://", listen_host=HOST, listen_port=free_port()),
    ])
    errors, warnings = cfg.conflicts()
    check("回环口重复使用不提示", not errors and not warnings, "%s / %s" % (errors, warnings))


def test_multi_channel_config() -> None:
    print("\n[9] 多通道配置：旧配置迁移 / 冲突检查 / 自动分配")
    v1 = {
        "port": "COM7", "baudrate": 9600, "listen_host": "127.0.0.1", "listen_port": 2400,
        "ui_scale": 1.25, "timestamps": False, "autoscroll": True,
        "log_to_file": True, "log_file": "logs/x.log", "show_rx": True, "log_hex": True,
    }
    cfg = BridgeConfig.from_dict(v1)
    check("旧版单串口配置迁移成一个通道", len(cfg.channels) == 1, "%d 个通道" % len(cfg.channels))
    check("迁移后串口参数保留", cfg.channels[0].port == "COM7" and cfg.channels[0].baudrate == 9600,
          "%s@%s" % (cfg.channels[0].port, cfg.channels[0].baudrate))
    check("迁移后监听地址与端口保留",
          cfg.channels[0].listen_host == "127.0.0.1" and cfg.channels[0].listen_port == 2400)
    check("迁移后界面缩放保留", abs(cfg.ui_scale - 1.25) < 1e-6, str(cfg.ui_scale))
    check("时间戳设置迁移到全局显示项", cfg.timestamps is False)
    check("旧日志路径保留", cfg.channels[0].log_file == "logs/x.log", cfg.channels[0].log_file)
    check("十六进制设置跟着通道走", cfg.channels[0].log_hex is True)
    check("迁移后通道自动命名", bool(cfg.channels[0].name), cfg.channels[0].name)

    cfg2 = BridgeConfig(channels=[
        ChannelConfig(port="COM1", listen_port=2323),
        ChannelConfig(port="COM2", listen_port=2324),
    ])
    data = json.loads(json.dumps(cfg2.to_dict()))
    check("新版配置带版本号与通道数组", data.get("version") == 2 and len(data.get("channels", [])) == 2,
          "version=%s" % data.get("version"))
    back = BridgeConfig.from_dict(data)
    check("配置存取往返后串口与端口不变",
          back.channels[1].port == "COM2" and back.channels[1].listen_port == 2324)
    check("通道名自动去重", back.channels[0].name != back.channels[1].name,
          "%s / %s" % (back.channels[0].name, back.channels[1].name))

    cfg3 = BridgeConfig(channels=[ChannelConfig(name="通道1", port="COM1", listen_port=2323)])
    fresh = cfg3.new_channel()
    check("新建通道自动避开已用端口", fresh.listen_port != 2323 and fresh.listen_port >= 2324,
          str(fresh.listen_port))
    check("新建通道名字不重复", fresh.name != "通道1", fresh.name)
    check("新建通道默认日志按通道名生成", fresh.log_file == "logs/%s.log" % fresh.name, fresh.log_file)
    duplicate = cfg3.new_channel(copy_from=cfg3.channels[0])
    check("复制通道继承串口参数但换端口",
          duplicate.port == "COM1" and duplicate.listen_port != 2323,
          "%s -> %s" % (duplicate.port, duplicate.listen_port))

    weird = BridgeConfig.from_dict({"channels": [{"name": "", "listen_port": 0, "baudrate": "abc",
                                                 "parity": "Z", "bytesize": 42}]})
    check("非法值被收敛到默认值",
          weird.channels[0].listen_port == 2323 and weird.channels[0].baudrate == 115200
          and weird.channels[0].parity == "N" and weird.channels[0].bytesize == 8,
          weird.channels[0].describe())
    check("空通道名被补上", bool(weird.channels[0].name), weird.channels[0].name)
    check("配置里一个通道都没有时也会给一个默认通道",
          len(BridgeConfig.from_dict({"channels": []}).channels) == 1)
    big = BridgeConfig(channels=[ChannelConfig(name="c%d" % i) for i in range(40)])
    big.normalize()
    check("通道数量有上限保护", len(big.channels) <= 32, str(len(big.channels)))


def test_cli_channels() -> None:
    print("\n[10] 命令行多通道参数解析")
    parser = cli.build_parser()

    args = parser.parse_args(["-p", "COM3", "-t", "2323", "-p", "COM5", "-t", "2324"])
    channels = cli.build_channels(BridgeConfig(), args)
    check("-p/-t 按顺序配对生成两个通道",
          len(channels) == 2 and channels[0].port == "COM3" and channels[1].listen_port == 2324,
          str([(c.port, c.listen_port) for c in channels]))
    check("通道名默认取串口名", channels[0].name == "COM3" and channels[1].name == "COM5",
          "%s / %s" % (channels[0].name, channels[1].name))

    args = parser.parse_args(["--map", "COM3=2500", "--map", "socket://1.2.3.4:4001=2501:9600"])
    channels = cli.build_channels(BridgeConfig(), args)
    check("--map 支持带冒号的 socket:// 设备名", channels[1].port == "socket://1.2.3.4:4001",
          channels[1].port)
    check("--map 的端口与波特率都生效",
          channels[1].listen_port == 2501 and channels[1].baudrate == 9600,
          "%d @ %d" % (channels[1].listen_port, channels[1].baudrate))
    check("--map 可以指定通道名", cli.build_channels(
        BridgeConfig(), parser.parse_args(["--map", "COM3=2500", "--name", "机柜A"]))[0].name == "机柜A")

    try:
        cli.build_channels(BridgeConfig(), parser.parse_args(["--map", "COM3=2323", "-p", "COM4"]))
        check("--map 与 -p 混用会被拒绝", False, "没有报错")
    except ValueError as exc:
        check("--map 与 -p 混用会被拒绝", True, str(exc)[:40])

    args = parser.parse_args(["-p", "loop://", "-p", "loop://"])
    channels = cli.build_channels(BridgeConfig(), args)
    check("没写端口时自动分配且互不重复",
          channels[0].listen_port != channels[1].listen_port,
          "%d / %d" % (channels[0].listen_port, channels[1].listen_port))
    check("同名串口自动区分通道名", channels[0].name != channels[1].name,
          "%s / %s" % (channels[0].name, channels[1].name))

    cfg = BridgeConfig(channels=[ChannelConfig(port="COM1", listen_port=2323)])
    channels = cli.build_channels(cfg, parser.parse_args(["-b", "9600"]))
    check("不给 -p 时沿用配置文件里的通道",
          len(channels) == 1 and channels[0].port == "COM1" and channels[0].baudrate == 9600,
          channels[0].describe())

    channels = cli.build_channels(BridgeConfig(), parser.parse_args(
        ["--map", "COM3=2500", "--map", "COM5=2501", "--log", "--log-file", "logs/all.log"]))
    check("多通道 + --log-file 自动加序号避免抢同一个文件",
          channels[0].log_file == "logs/all-1.log" and channels[1].log_file == "logs/all-2.log",
          "%s / %s" % (channels[0].log_file, channels[1].log_file))


def _can_bind(port: int) -> bool:
    """端口是否能重新监听（用来确认已释放）。"""
    sock = socket.socket()
    try:
        sock.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def main() -> int:
    test_telnet_loopback()
    test_raw_mode()
    test_socket_backend()
    test_single_client()
    test_server_echo()
    test_errors()
    test_multi_channel()
    test_multi_channel_errors()
    test_multi_channel_config()
    test_cli_channels()
    print("\n结果：%s" % ("全部通过 🎉" if not FAILED else "失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED))))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
