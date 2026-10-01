"""界面缩放相关的平台辅助：高 DPI 感知、缩放系数、字体挑选。

为什么需要这些：

* 如果进程不声明 DPI 感知，Windows 会把窗口当位图拉伸，125%/150% 缩放时界面糊成一片；
* 声明之后 Tk 需要按真实 DPI 设置 ``tk scaling``，否则字号会偏小；
* 中文字体 / 等宽字体在不同机器上不一定存在，直接写死会回退成很难看的默认字体。

所有函数都做了异常兜底，在非 Windows 或受限环境下也能安全调用。
"""

from __future__ import annotations

import ctypes
import os
import tkinter as tk
from tkinter import font as tkfont
from typing import Iterable

STATE: dict = {"dpi_aware": "unknown"}


def enable_dpi_awareness() -> str:
    """声明进程的 DPI 感知级别，必须在创建任何窗口之前调用。

    返回实际生效的方式（``shcore`` / ``user32`` / ``skip`` / ``failed``），便于日志与自测。
    """
    if os.name != "nt":
        STATE["dpi_aware"] = "skip"
        return "skip"
    if STATE["dpi_aware"] not in ("unknown",):
        return str(STATE["dpi_aware"])

    try:
        # 1 = PROCESS_SYSTEM_DPI_AWARE：主屏按真实缩放渲染（清晰），
        # 拖到另一块不同缩放的显示器时交给系统整体缩放，不会出现错位。
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        STATE["dpi_aware"] = "shcore"
        return "shcore"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        STATE["dpi_aware"] = "user32"
        return "user32"
    except Exception:
        STATE["dpi_aware"] = "failed"
        return "failed"


def detect_scale(root: tk.Misc) -> float:
    """返回相对 96 DPI 的缩放系数：100% -> 1.0，125% -> 1.25，150% -> 1.5。"""
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:
        dpi = 96.0
    if not dpi or dpi <= 0:
        dpi = 96.0
    return dpi / 96.0


def apply_tk_scaling(root: tk.Misc, scale: float) -> None:
    """让 Tk 按真实 DPI 解释「磅」字号（scaling = 每磅多少像素）。"""
    try:
        root.tk.call("tk", "scaling", max(1.0, 96.0 * scale / 72.0))
    except Exception:
        pass


def pick_family(root: tk.Misc, candidates: Iterable[str], fallback: str = "TkDefaultFont") -> str:
    """在候选列表里挑第一个系统真正装了的字体。"""
    try:
        available = {name.lower() for name in tkfont.families(root)}
    except Exception:
        available = set()
    for name in candidates:
        if name.lower() in available:
            return name
    return fallback


def screen_info(root: tk.Misc) -> dict:
    """屏幕/虚拟桌面尺寸，用于把窗口限制在可见范围内。"""
    info = {}
    for key, method in (
        ("width", "winfo_screenwidth"),
        ("height", "winfo_screenheight"),
        ("vx", "winfo_vrootx"),
        ("vy", "winfo_vrooty"),
        ("vwidth", "winfo_vrootwidth"),
        ("vheight", "winfo_vrootheight"),
    ):
        try:
            info[key] = int(getattr(root, method)())
        except Exception:
            info[key] = 0
    if info["vwidth"] <= 0:
        info["vwidth"] = info["width"]
    if info["vheight"] <= 0:
        info["vheight"] = info["height"]
    return info
