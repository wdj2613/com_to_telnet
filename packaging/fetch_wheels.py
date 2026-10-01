# -*- coding: utf-8 -*-
"""手工抓取 PyInstaller 及其依赖的 wheel 到 vendor/，然后离线安装。

背景：本机 pip 在解析/下载阶段会长时间卡住（但 Python 自己的 HTTPS 请求正常），
所以这里直接用 PyPI JSON API 递归解析依赖并下载 wheel，最后让 pip 只从本地装。

    python packaging/fetch_wheels.py                # 只下载
    python packaging/fetch_wheels.py --install      # 下载后 pip install --no-index
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from comtel.console import setup_console

    setup_console()
except Exception:
    pass

from pip._vendor.packaging.markers import Marker  # noqa: E402
from pip._vendor.packaging.requirements import Requirement  # noqa: E402
from pip._vendor.packaging.specifiers import SpecifierSet  # noqa: E402
from pip._vendor.packaging.version import InvalidVersion, Version  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "vendor")
INDEX = os.environ.get("WHEEL_INDEX", "https://pypi.org/pypi")
UA = {"User-Agent": "Mozilla/5.0 (wheel-fetcher)"}

IS_WIN = os.name == "nt"
PY_TAG = "cp%d%d" % sys.version_info[:2]
PY_VER = "%d.%d" % sys.version_info[:2]
ENV = {
    "python_version": PY_VER,
    "python_full_version": platform.python_version(),
    "sys_platform": "win32" if IS_WIN else "linux",
    "os_name": "nt" if IS_WIN else "posix",
    "platform_system": "Windows" if IS_WIN else "Linux",
    "platform_machine": platform.machine(),
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "implementation_version": PY_VER + ".0",
    "extra": "",
}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def wheel_ok(filename: str) -> bool:
    """只接受「纯 Python 或本机 cp 版本」且平台为 any / win_amd64 的 wheel。"""
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return False
    pytag, abitag, plat = parts[-3], parts[-2], parts[-1]
    if plat not in ("any", "win_amd64", "win32"):
        return False
    if plat == "win32" and platform.machine().endswith("64"):
        return False
    py_ok = set(pytag.split(".")) & {"py3", "py2", PY_TAG, "py%d%d" % sys.version_info[:2]}
    return bool(py_ok) and abitag in ("none", "abi3", PY_TAG)


def pick(name: str):
    """选出该包在本机可用的最新版本与 wheel 文件。"""
    data = fetch_json("%s/%s/json" % (INDEX, name))
    versions = []
    for ver in data.get("releases", {}):
        try:
            versions.append((Version(ver), ver))
        except InvalidVersion:
            continue
    for version, raw in sorted(versions, reverse=True):
        for item in data["releases"][raw]:
            if not wheel_ok(item["filename"]):
                continue
            requires = item.get("requires_python")
            if requires:
                try:
                    if not SpecifierSet(requires).contains(PY_VER, prereleases=True):
                        continue
                except Exception:
                    pass
            return raw, item["filename"], item["url"]
    return None


def resolve(roots):
    """广度优先解析依赖，返回 {包名: (版本, 文件名, 下载地址)}。"""
    resolved = {}
    queue = list(roots)
    while queue:
        name = queue.pop(0)
        key = name.lower().replace("_", "-")
        if key in resolved:
            continue
        found = pick(name)
        if not found:
            print("  !! 找不到可用 wheel：%s" % name)
            resolved[key] = None
            continue
        version, filename, url = found
        resolved[key] = (version, filename, url)
        print("  %-28s %-12s %s" % (name, version, filename))
        try:
            meta = fetch_json("%s/%s/%s/json" % (INDEX, name, version))
        except Exception as exc:
            print("  !! 读取 %s 元数据失败：%s" % (name, exc))
            continue
        for req_str in meta.get("info", {}).get("requires_dist") or []:
            try:
                req = Requirement(req_str)
            except Exception:
                continue
            if req.extras:
                continue
            if req.marker is not None:
                try:
                    if not req.marker.evaluate(ENV):
                        continue
                except Exception:
                    continue
            dep_key = req.name.lower().replace("_", "-")
            if dep_key not in resolved:
                queue.append(req.name)
    return {k: v for k, v in resolved.items() if v}


def download(items) -> list:
    os.makedirs(VENDOR, exist_ok=True)
    paths = []
    for name, (version, filename, url) in sorted(items.items()):
        target = os.path.join(VENDOR, filename)
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            print("  已存在 %s" % filename)
            paths.append(target)
            continue
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as fh:
            data = resp.read()
            fh.write(data)
        print("  下载 %-52s %8.1f KB" % (filename, len(data) / 1024.0))
        paths.append(target)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 wheel 到 vendor/")
    parser.add_argument("packages", nargs="*", default=["pyinstaller"])
    parser.add_argument("--install", action="store_true", help="下载后离线安装")
    args = parser.parse_args()

    print("Python %s / %s，目标目录 %s" % (platform.python_version(), platform.machine(), VENDOR))
    print("解析依赖：")
    items = resolve(args.packages)
    if not items:
        print("没有解析到任何包")
        return 1
    print("\n下载 %d 个 wheel：" % len(items))
    paths = download(items)

    print("\n结果：%d 个 wheel 已就绪" % len(paths))
    if args.install:
        import subprocess

        cmd = [sys.executable, "-m", "pip", "install", "--no-index", "--find-links", VENDOR] + args.packages
        print("执行：" + " ".join(cmd))
        rc = subprocess.call(cmd)
        print("pip 退出码：%d" % rc)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
