#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Telnet 测试客户端：检查网关是否正常工作。

用法：
    python tools/telnet_probe.py 127.0.0.1 2323                  # 交互式（回车发送）
    python tools/telnet_probe.py 127.0.0.1 2323 --send "AT"      # 发一条就退出
    python tools/telnet_probe.py 127.0.0.1 2323 --hex             # 用十六进制显示
    python tools/telnet_probe.py 127.0.0.1 2323 --raw             # 裸 TCP，不做 Telnet 处理
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from comtel.console import setup_console  # noqa: E402

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


def strip_telnet(data: bytes) -> bytes:
    """去掉 IAC 协商序列，返回应用数据。"""
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= len(data):
            break
        cmd = data[i + 1]
        if cmd == IAC:
            out.append(IAC)
            i += 2
        elif cmd in (DO, DONT, WILL, WONT):
            i += 3
        elif cmd == SB:
            j = i + 2
            while j + 1 < len(data) and not (data[j] == IAC and data[j + 1] == SE):
                j += 1
            i = j + 2
        else:
            i += 2
    return bytes(out)


def show(data: bytes, use_hex: bool) -> str:
    if use_hex:
        return " ".join("%02X" % b for b in data)
    return "".join(chr(b) if 32 <= b < 127 or b in (13, 10, 9) else "\\x%02X" % b for b in data)


def main(argv=None) -> int:
    setup_console()
    p = argparse.ArgumentParser(description="Telnet 测试客户端")
    p.add_argument("host")
    p.add_argument("port", type=int)
    p.add_argument("--send", action="append", default=[], help="发送内容，可多次指定")
    p.add_argument("--hex", action="store_true", help="十六进制显示收到数据")
    p.add_argument("--raw", action="store_true", help="按裸 TCP 处理（不解析 Telnet 控制序列）")
    p.add_argument("--seconds", type=float, default=0, help="等待秒数，0 表示一直等到 Ctrl+C 或回车退出")
    p.add_argument("--no-newline", action="store_true", help="--send 的内容不追加回车")
    args = p.parse_args(argv)

    sock = socket.create_connection((args.host, args.port), timeout=5)
    sock.settimeout(0.3)
    print("已连接 %s:%d（%s）；%s" % (
        args.host, args.port, "裸 TCP" if args.raw else "Telnet",
        "按回车发送，输入 exit 退出" if not args.seconds and not args.send else "接收中…",
    ))

    stop = threading.Event()
    counter = {"rx": 0, "tx": 0}

    def reader() -> None:
        while not stop.is_set():
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                print("\n[服务端已断开]")
                stop.set()
                break
            counter["rx"] += len(data)
            payload = data if args.raw else strip_telnet(data)
            if payload:
                sys.stdout.write(show(payload, args.hex))
                sys.stdout.flush()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def send_line(text: str) -> None:
        data = text.encode("utf-8")
        if not args.no_newline:
            data += b"\r\n"
        sock.sendall(data)
        counter["tx"] += len(data)

    try:
        for line in args.send:
            time.sleep(0.2)
            send_line(line)
        if args.send or args.seconds:
            deadline = time.time() + (args.seconds or 1.5)
            while time.time() < deadline and not stop.is_set():
                time.sleep(0.05)
        else:
            while not stop.is_set():
                text = input()
                if text.strip().lower() in ("exit", "quit"):
                    break
                send_line(text)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop.set()
        try:
            sock.close()
        except OSError:
            pass
        thread.join(timeout=1.0)
        print("\n[收 %d 字节 / 发 %d 字节]" % (counter["rx"], counter["tx"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
