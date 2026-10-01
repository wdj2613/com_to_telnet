#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""COM 串口 <-> Telnet 网关（入口）。

用法：
    python com_telnet_bridge.py                              # 图形界面（多通道）
    python com_telnet_bridge.py -n -p COM3 -t 2323           # 命令行模式
    python com_telnet_bridge.py -n -p COM3 -t 2323 -p COM5 -t 2324
    python com_telnet_bridge.py -n --map COM3=2323 --map COM5=2324
    python com_telnet_bridge.py --list-ports                 # 列出串口
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    info_flags = {"-h", "--help", "-v", "--version", "--list-ports"}
    force_cli = any(a in ("-n", "--no-gui") for a in argv)
    force_gui = any(a in ("-g", "--gui") for a in argv)

    if force_cli:
        from comtel.cli import run_cli

        return run_cli(argv)

    if not force_gui and any(a in info_flags for a in argv):
        from comtel.cli import run_cli

        return run_cli(argv)

    try:
        from comtel.gui import run_gui

        config = ""
        for flag in ("-c", "--config"):
            if flag in argv and argv.index(flag) + 1 < len(argv):
                config = argv[argv.index(flag) + 1]
                break
        return run_gui(config)
    except ImportError as exc:
        if force_gui:
            print("无法加载图形界面（缺 tkinter）：%s" % exc, file=sys.stderr)
            return 1
        print("未检测到图形界面支持，改用命令行模式。", file=sys.stderr)
        from comtel.cli import run_cli

        return run_cli([a for a in argv if a not in ("-g", "--gui")])


if __name__ == "__main__":
    sys.exit(main())
