"""PyInstaller 入口：命令行版（打包成带控制台的 exe）。

用法（打包后）：``com-telnet-cli.exe -p COM3 -b 115200 -t 2323``
"""

from __future__ import annotations

import sys

from comtel.cli import run_cli


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    sys.exit(main())
