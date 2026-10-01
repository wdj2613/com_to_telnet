"""PyInstaller 入口：图形界面版（打包成无控制台窗口的 exe）。

用法（打包后）：双击 exe，或 ``COM_To_Telnet.exe -c D:\\my\\config.json``
"""

from __future__ import annotations

import sys

from comtel.gui import run_gui


def main() -> int:
    argv = sys.argv[1:]
    config = ""
    for flag in ("-c", "--config"):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            config = argv[argv.index(flag) + 1]
            break
    return run_gui(config)


if __name__ == "__main__":
    sys.exit(main())
