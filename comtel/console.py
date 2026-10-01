"""控制台编码处理：让 Windows 终端能正确显示中文与特殊字符。"""

from __future__ import annotations

import os
import sys


def setup_console() -> None:
    """把 Windows 控制台切到 UTF-8，并把标准输出改成 UTF-8。"""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
