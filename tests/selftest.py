#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自检脚本：不需要真实串口，用内置 ``loop://`` 回环口和本地 TCP 回显服务验证整条链路。

覆盖：
1. Telnet 协商、多客户端广播、换行转换、0xFF 转义、只读客户端拦截
2. 裸 TCP 模式（不做协商、不转义）
3. ``socket://`` 串口服务器后端
4. 单客户端模式（第二个连接被拒绝）
5. 真实 COM 口打不开时的报错信息

用法： ``.venv\\Scripts\\python.exe tests\\selftest.py``
"""

from __future__ import annotations

import ctypes
import os
import socket
import socketserver
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from comtel.config import BridgeConfig  # noqa: E402
from comtel.console import setup_console  # noqa: E402
from comtel.gateway import Gateway  # noqa: E402
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
    return Gateway(BridgeConfig(**params), lambda event, **kw: None)


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


def main() -> int:
    test_telnet_loopback()
    test_raw_mode()
    test_socket_backend()
    test_single_client()
    test_server_echo()
    test_errors()
    print("\n结果：%s" % ("全部通过 🎉" if not FAILED else "失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED))))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
