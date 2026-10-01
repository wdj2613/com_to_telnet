#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GUI 冒烟测试：真正创建 Tk 窗口、点「启动」、连一个客户端、发数据、点「停止」。

额外覆盖界面缩放相关行为：DPI 识别、字号缩放、窗口变窄时的自动重排。
不依赖真实串口（用 loop://）。运行中窗口会短暂显示一次（为了验证真实拖拽重排）。

用法： ``.venv\\Scripts\\python.exe tests\\gui_smoke.py``
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

from comtel.config import BridgeConfig, default_config_path  # noqa: E402
from comtel.console import setup_console  # noqa: E402
from comtel.gui import BridgeApp  # noqa: E402
from comtel.ui_scale import STATE as DPI_STATE  # noqa: E402

setup_console()
FAILED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" —— " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


def pump(root: tk.Tk, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def widget_rects(app) -> dict:
    """关键控件相对窗口左上角的位置与尺寸（真正布局后的像素值）。"""
    win = app.master_window
    ox, oy = win.winfo_rootx(), win.winfo_rooty()
    widgets = {
        "串口区": app.opt_boxes[0],
        "服务端区": app.opt_boxes[1],
        "转换区": app.opt_boxes[2],
        "日志框": app.log_box,
        "客户端框": app.cli_box,
        "日志文本": app.txt,
        "发送框": app.ent_send,
        "末位按钮": app.btn_logdir,
        "状态文字": app.lbl_status,
    }
    return {
        name: (w.winfo_rootx() - ox, w.winfo_rooty() - oy, w.winfo_width(), w.winfo_height())
        for name, w in widgets.items()
    }


def _overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def check_layout(app, label: str) -> dict:
    """控件必须在窗口内、尺寸正常、配置区三块互不重叠（缩放时不被裁剪/重叠）。"""
    app.update_idletasks()
    win_w = app.master_window.winfo_width()
    win_h = app.master_window.winfo_height()
    rects = widget_rects(app)
    problems = []
    for name, (x, y, w, h) in rects.items():
        if w <= 1 or h <= 1:
            problems.append("%s 尺寸异常 %dx%d" % (name, w, h))
        elif x < -4 or y < -4 or x + w > win_w + 4 or y + h > win_h + 4:
            problems.append("%s 超出窗口 %s（窗口 %dx%d）" % (name, (x, y, w, h), win_w, win_h))
    names = ["串口区", "服务端区", "转换区"]
    for i in range(3):
        for j in range(i + 1, 3):
            if _overlaps(rects[names[i]], rects[names[j]]):
                problems.append("%s 与 %s 重叠" % (names[i], names[j]))
    check("缩放后布局正常（%s）" % label, not problems, "; ".join(problems))
    return rects


def main() -> int:
    tcp_port = free_port()
    had_config = os.path.isfile(default_config_path())
    cfg = BridgeConfig(port="loop://", listen_host="127.0.0.1", listen_port=tcp_port, log_to_file=False)
    root = tk.Tk()
    root.withdraw()
    app = BridgeApp(root, cfg)
    pump(root, 0.3)
    check("界面构建成功", app.winfo_exists() == 1)

    # ------------------------------------------------------------------
    # 缩放相关的界面行为
    # ------------------------------------------------------------------
    info = app.layout_info()
    scale = app.dpi_scale
    check("识别到 DPI 缩放系数", info["dpi"] >= 1.0, "dpi=%.2f（%s）" % (info["dpi"], DPI_STATE["dpi_aware"]))
    check("窗口最小尺寸按 DPI 放大", info["min_width"] >= int(round(620 * scale)),
          "min_width=%s dpi=%.2f" % (info["min_width"], scale))

    # 布局引擎：按宽度选三栏 / 两栏 / 单栏
    s = app.scale()
    app._reflow(int(1200 * s))
    check("宽窗口：配置区三栏并排", app.layout_info()["options"] == "three")
    check("宽窗口：客户端列表在右侧", app.layout_info()["body"] == "right")
    app._reflow(int(800 * s))
    check("中等宽度：配置区变两栏", app.layout_info()["options"] == "two")
    app._reflow(int(640 * s))
    check("窄窗口：配置区纵向堆叠", app.layout_info()["options"] == "stack")
    check("窄窗口：客户端列表挪到日志下方", app.layout_info()["body"] == "below")
    app._reflow(int(1200 * s))
    check("拉宽后恢复三栏", app.layout_info()["options"] == "three")

    # 字号缩放
    base_ui = app.layout_info()["ui_font"]
    base_log = app.layout_info()["log_font"]
    base_col = int(app.lst_clients.column("peer", "width"))
    app.zoom_in()
    pump(root, 0.05)
    bigger = app.layout_info()
    check("放大后界面字号变大", bigger["ui_font"] > base_ui, "%s -> %s" % (base_ui, bigger["ui_font"]))
    check("放大后日志字号变大", bigger["log_font"] > base_log)
    check("放大后列宽同步变大", int(app.lst_clients.column("peer", "width")) > base_col)
    check("缩放比例已写回配置", abs(app.cfg.ui_scale - app.zoom) < 1e-6, "ui_scale=%s" % app.cfg.ui_scale)
    check("状态栏显示缩放百分比", app.var_zoom.get() == "%d%%" % round(app.zoom * 100), app.var_zoom.get())

    app.zoom_out()
    app.zoom_out()
    check("连续缩小后字号变小", app.layout_info()["ui_font"] < base_ui)
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    check("缩放有下限（不会小到看不清）", app.zoom >= 0.7, "zoom=%.2f" % app.zoom)
    for _ in range(30):
        app.zoom_in()
    check("缩放有上限", app.zoom <= 2.0, "zoom=%.2f" % app.zoom)
    app.zoom_reset()
    check("重置回到 100%", app.var_zoom.get() == "100%" and abs(app.zoom - 1.0) < 1e-6)

    # 真实窗口拖拽：验证 <Configure> 绑定的重排确实生效，且控件不被裁剪/重叠
    s = app.scale()
    root.deiconify()
    root.geometry("%dx%d" % (int(1200 * s), int(700 * s)))
    pump(root, 0.5)
    check("真实窗口拉宽 -> 三栏", app.layout_info()["options"] == "three",
          "width=%s" % app.layout_info()["width"])
    rects = check_layout(app, "宽 1200")
    check("宽窗口：串口区与服务端区并排", rects["服务端区"][0] > rects["串口区"][0],
          "x=%s vs %s" % (rects["串口区"][0], rects["服务端区"][0]))
    check("宽窗口：客户端列表在日志右侧", rects["客户端框"][0] >= rects["日志框"][0] + rects["日志框"][2] - 2)
    check("宽窗口：日志区是主区（比客户端列表宽）", rects["日志框"][2] > rects["客户端框"][2],
          "日志 %d / 客户端 %d" % (rects["日志框"][2], rects["客户端框"][2]))

    root.geometry("%dx%d" % (int(1000 * s), int(700 * s)))
    pump(root, 0.4)
    check("真实窗口收窄 -> 两栏", app.layout_info()["options"] == "two",
          "width=%s" % app.layout_info()["width"])
    rects = check_layout(app, "中 1000")

    root.geometry("%dx%d" % (int(640 * s), app.layout_info()["min_height"] + int(20 * s)))
    pump(root, 0.5)
    check("真实窗口继续收窄 -> 单栏堆叠", app.layout_info()["options"] == "stack",
          "width=%s" % app.layout_info()["width"])
    check("收窄时客户端列表在下方", app.layout_info()["body"] == "below")
    rects = check_layout(app, "窄 640")
    check("窄窗口：配置区上下堆叠", rects["服务端区"][1] > rects["串口区"][1],
          "y=%s vs %s" % (rects["串口区"][1], rects["服务端区"][1]))
    check("窄窗口：客户端列表在日志下方",
          rects["客户端框"][1] >= rects["日志框"][1] + rects["日志框"][3] - 2)
    check("窄窗口：工具栏按钮未被裁掉", rects["末位按钮"][0] + rects["末位按钮"][2] <= app.master_window.winfo_width(),
          str(rects["末位按钮"]))
    check("窄窗口：最小高度被抬高以容纳堆叠的配置区", app.layout_info()["min_height"] > 540 * s,
          "min_height=%s" % app.layout_info()["min_height"])

    # 放大字号后再验一次：字号变化不能把控件挤出窗口
    app.set_zoom(1.4, announce=False)
    root.geometry("%dx%d" % (int(1200 * s), int(760 * s)))
    pump(root, 0.5)
    check_layout(app, "140% 字号")
    app.set_zoom(0.8, announce=False)
    root.geometry("%dx%d" % (int(1200 * s), int(760 * s)))
    pump(root, 0.5)
    check_layout(app, "80% 字号")
    app.zoom_reset()
    root.geometry("%dx%d" % (int(1180 * s), int(720 * s)))
    pump(root, 0.4)
    rects = check_layout(app, "回到 1180")
    check("从窄窗口恢复后比例正常（日志区仍为主区）", rects["日志框"][2] > rects["客户端框"][2],
          "日志 %d / 客户端 %d" % (rects["日志框"][2], rects["客户端框"][2]))
    check("从窄窗口恢复后客户端列表回到右侧",
          rects["客户端框"][0] >= rects["日志框"][0] + rects["日志框"][2] - 2)

    geom = app._current_geometry()
    check("能读取当前窗口几何用于记忆", bool(re.match(r"^\d+x\d+", geom)), geom)

    # ------------------------------------------------------------------
    # 网关功能
    # ------------------------------------------------------------------
    app.var_port.set("loop://")
    app.var_tcp_port.set(str(tcp_port))
    app.var_banner.set("SMOKE")
    app.var_multi.set(True)
    app.var_send_crlf.set(True)
    app.on_start()
    pump(root, 0.6)
    check("点击启动后网关在运行", app.gateway.running, "running=%s" % app.gateway.running)
    check("启动后配置项被禁用", str(app.btn_start["state"]) == "disabled")

    client = socket.create_connection(("127.0.0.1", tcp_port), timeout=3)
    client.settimeout(1.0)
    pump(root, 0.5)
    check("界面显示 1 个客户端", app.gateway.client_count == 1)
    lines = app.client_lines()
    check("客户端列表已刷新", bool(lines) and "127.0.0.1" in lines[0], lines[0] if lines else "(空)")
    check("客户端列表标明权限", bool(lines) and lines[0].startswith("[RW]"), lines[0] if lines else "(空)")

    try:
        first = client.recv(4096)
    except socket.timeout:
        first = b""
    check("客户端收到欢迎语", b"SMOKE" in first, repr(first[:30]))

    # 客户端 -> 串口(回环) -> 广播
    client.sendall(b"ping\r\n")
    pump(root, 0.8)
    try:
        echoed = client.recv(4096)
    except socket.timeout:
        echoed = b""
    check("界面运行时可收发数据", b"ping" in echoed, repr(echoed[:30]))

    log_text = app.txt.get("1.0", "end")
    check("日志窗口有 RX 记录", "ping" in log_text)
    check("日志窗口有系统记录", "已启动" in log_text)

    # 本地输入框发送
    app.var_send_text.set("LOCAL")
    app.on_send()
    pump(root, 0.6)
    try:
        got = client.recv(4096)
    except socket.timeout:
        got = b""
    check("输入框发送到串口并回显给客户端", b"LOCAL" in got, repr(got[:40]))
    check("发送后输入框已清空", app.var_send_text.get() == "")

    # 断开客户端 + 停止
    app.on_kick_all()
    pump(root, 0.4)
    check("断开全部客户端生效", app.gateway.client_count == 0)
    check("断开后客户端列表已清空", app.client_lines() == [])
    app.on_stop()
    pump(root, 0.5)
    check("点击停止后网关已停止", not app.gateway.running)
    check("停止后按钮恢复", str(app.btn_start["state"]) == "normal")

    client.close()
    zoom_at_close = app.zoom
    app.on_close()  # 会保存 config.json（含窗口大小与缩放）
    check("关闭时未抛异常", True)

    if os.path.isfile(default_config_path()):
        try:
            with open(default_config_path(), "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            saved = {}
        check("配置里记住了窗口大小", bool(re.match(r"^\d+x\d+", saved.get("window_geometry", ""))),
              saved.get("window_geometry", ""))
        check("配置里记住了界面缩放", abs(float(saved.get("ui_scale", 0)) - zoom_at_close) < 1e-6,
              str(saved.get("ui_scale")))

    if not had_config and os.path.isfile(default_config_path()):
        os.remove(default_config_path())
        print("（已清理测试生成的 config.json）")

    print("\n结果：%s" % ("全部通过 🎉" if not FAILED else "失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED))))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
