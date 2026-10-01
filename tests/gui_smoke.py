#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GUI 冒烟测试：真正创建 Tk 窗口、点「启动」、连客户端、发数据、点「停止」。

覆盖三块：

1. 界面缩放相关行为：DPI 识别、字号缩放、窗口变窄时的自动重排；
2. 单通道完整功能：启动 → 客户端接入 → 收发 → 断开 → 停止；
3. 多通道：新建通道（自动分配端口/日志）、两个通道同时运行互不串台、
   端口冲突检查、只看当前通道、删除通道、配置保存。

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

from comtel import gui as gui_module  # noqa: E402
from comtel.config import BridgeConfig, ChannelConfig, default_config_path  # noqa: E402
from comtel.console import setup_console  # noqa: E402
from comtel.gui import BridgeApp  # noqa: E402
from comtel.ui_scale import STATE as DPI_STATE  # noqa: E402

setup_console()
FAILED = []

# 界面里的弹窗在自测环境下会阻塞，这里全部换成记录器
DIALOGS = []
ANSWERS = []
gui_module.messagebox.showerror = lambda title, msg="", **kw: DIALOGS.append(("error", title, msg))
gui_module.messagebox.showwarning = lambda title, msg="", **kw: DIALOGS.append(("warning", title, msg))
gui_module.messagebox.showinfo = lambda title, msg="", **kw: DIALOGS.append(("info", title, msg))
gui_module.messagebox.askyesno = lambda title, msg="", **kw: (ANSWERS.append((title, msg)), True)[1]


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


def connect(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    client.settimeout(1.0)
    return client


def recv_some(client: socket.socket) -> bytes:
    try:
        return client.recv(4096)
    except socket.timeout:
        return b""


def widget_rects(app) -> dict:
    """关键控件相对窗口左上角的位置与尺寸（真正布局后的像素值）。"""
    win = app.master_window
    ox, oy = win.winfo_rootx(), win.winfo_rooty()
    widgets = {
        "串口区": app.opt_boxes[0],
        "服务端区": app.opt_boxes[1],
        "转换区": app.opt_boxes[2],
        "通道列表": app.chan_box,
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
    """控件必须在窗口内、尺寸正常、参数区三块互不重叠（缩放时不被裁剪/重叠）。"""
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
    cfg = BridgeConfig(channels=[
        ChannelConfig(port="loop://", listen_host="127.0.0.1", listen_port=tcp_port, log_to_file=False),
    ])
    root = tk.Tk()
    root.withdraw()
    app = BridgeApp(root, cfg)
    pump(root, 0.3)
    check("界面构建成功", app.winfo_exists() == 1)
    check("界面按配置建出 1 个通道面板", len(app.panes) == 1 and app.pane(0) is not None)

    # ------------------------------------------------------------------
    # 缩放相关的界面行为
    # ------------------------------------------------------------------
    info = app.layout_info()
    scale = app.dpi_scale
    check("识别到 DPI 缩放系数", info["dpi"] >= 1.0, "dpi=%.2f（%s）" % (info["dpi"], DPI_STATE["dpi_aware"]))
    check("窗口最小尺寸按 DPI 放大", info["min_width"] >= int(round(620 * scale)),
          "min_width=%s dpi=%.2f" % (info["min_width"], scale))

    # 布局引擎：按宽度选三栏 / 两栏 / 单栏，通道列表在左 / 在上
    s = app.scale()
    app._reflow(int(1200 * s))
    check("宽窗口：参数区三栏并排", app.layout_info()["options"] == "three")
    check("宽窗口：通道列表在左侧", app.layout_info()["channels"] == "side")
    check("宽窗口：客户端列表在右侧", app.layout_info()["body"] == "right")
    app._reflow(int(800 * s))
    check("中等宽度：参数区变两栏", app.layout_info()["options"] == "two")
    app._reflow(int(640 * s))
    check("窄窗口：参数区纵向堆叠", app.layout_info()["options"] == "stack")
    check("窄窗口：通道列表挪到参数区上方", app.layout_info()["channels"] == "top")
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
    for _ in range(12):
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
    check("宽窗口：通道列表在参数区左侧", rects["通道列表"][0] + rects["通道列表"][2] <= rects["串口区"][0] + 2,
          "通道 x=%s w=%s / 参数 x=%s" % (rects["通道列表"][0], rects["通道列表"][2], rects["串口区"][0]))
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
    check("收窄时通道列表在参数区上方", app.layout_info()["channels"] == "top")
    rects = check_layout(app, "窄 640")
    check("窄窗口：参数区上下堆叠", rects["服务端区"][1] > rects["串口区"][1],
          "y=%s vs %s" % (rects["串口区"][1], rects["服务端区"][1]))
    check("窄窗口：通道列表在参数区上方",
          rects["通道列表"][1] + rects["通道列表"][3] <= rects["串口区"][1] + 2,
          "通道 y=%s h=%s / 参数 y=%s" % (rects["通道列表"][1], rects["通道列表"][3], rects["串口区"][1]))
    check("窄窗口：客户端列表在日志下方",
          rects["客户端框"][1] >= rects["日志框"][1] + rects["日志框"][3] - 2)
    check("窄窗口：工具栏按钮未被裁掉", rects["末位按钮"][0] + rects["末位按钮"][2] <= app.master_window.winfo_width(),
          str(rects["末位按钮"]))
    check("窄窗口：最小高度被抬高以容纳堆叠的参数区", app.layout_info()["min_height"] > 540 * s,
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
    # 单通道功能
    # ------------------------------------------------------------------
    pane0 = app.pane(0)
    pane0.var_port.set("loop://")
    pane0.var_tcp_port.set(str(tcp_port))
    pane0.var_banner.set("SMOKE-A")
    pane0.var_multi.set(True)
    app.var_send_crlf.set(True)
    app.on_start()
    pump(root, 0.6)
    check("点击启动后通道在运行", app.gateway.running, "running=%s" % app.gateway.running)
    check("启动后该通道的配置项被禁用", str(pane0.btn_start["state"]) == "disabled")
    check("启动后按钮变为可停止", str(pane0.btn_stop["state"]) == "normal")
    check("通道列表状态显示运行中", app.lst_channels.item("0", "values")[0].startswith("●"),
          str(app.lst_channels.item("0", "values")))
    check("状态栏汇总显示运行通道数", "运行中 1/1 通道" in app.var_status.get(), app.var_status.get())

    client = connect(tcp_port)
    pump(root, 0.5)
    check("界面显示 1 个客户端", app.gateway.client_count == 1)
    lines = app.client_lines()
    check("客户端列表已刷新", bool(lines) and "127.0.0.1" in lines[0], lines[0] if lines else "(空)")
    check("客户端列表标明权限", bool(lines) and "[RW]" in lines[0], lines[0] if lines else "(空)")
    check("客户端列表带通道名", bool(lines) and app.pane(0).var_name.get() in lines[0],
          lines[0] if lines else "(空)")

    first = recv_some(client)
    check("客户端收到欢迎语", b"SMOKE-A" in first, repr(first[:30]))

    # 客户端 -> 串口(回环) -> 广播
    client.sendall(b"ping\r\n")
    pump(root, 0.8)
    echoed = recv_some(client)
    check("界面运行时可收发数据", b"ping" in echoed, repr(echoed[:30]))

    log_text = app.txt.get("1.0", "end")
    check("日志窗口有 RX 记录", "ping" in log_text)
    check("日志窗口有系统记录", "已启动" in log_text)
    check("日志里带通道名前缀", "[%s]" % pane0.var_name.get() in log_text, pane0.var_name.get())

    # 本地输入框发送
    app.var_send_text.set("LOCAL")
    app.on_send()
    pump(root, 0.6)
    got = recv_some(client)
    check("输入框发送到串口并回显给客户端", b"LOCAL" in got, repr(got[:40]))
    check("发送后输入框已清空", app.var_send_text.get() == "")

    # 断开客户端 + 停止
    app.on_kick_all()
    pump(root, 0.4)
    check("断开全部客户端生效", app.gateway.client_count == 0)
    check("断开后客户端列表已清空", app.client_lines() == [])
    app.on_stop()
    pump(root, 0.5)
    check("点击停止后通道已停止", not app.gateway.running)
    check("停止后按钮恢复", str(pane0.btn_start["state"]) == "normal")
    check("停止后列表状态回到未启动", app.lst_channels.item("0", "values")[0].startswith("○"),
          str(app.lst_channels.item("0", "values")))
    client.close()

    # ------------------------------------------------------------------
    # 多通道
    # ------------------------------------------------------------------
    count_before = len(app.panes)
    app.on_add_channel()
    pump(root, 0.2)
    check("新建通道后通道数 +1", len(app.panes) == count_before + 1, "%d -> %d" % (count_before, len(app.panes)))
    pane1 = app.pane(1)
    check("新通道名字自动区分", pane1.var_name.get() != pane0.var_name.get(),
          "%s / %s" % (pane0.var_name.get(), pane1.var_name.get()))
    check("新通道监听端口自动分配且不与已有通道重复",
          pane1.var_tcp_port.get() != pane0.var_tcp_port.get(),
          "%s / %s" % (pane0.var_tcp_port.get(), pane1.var_tcp_port.get()))
    check("新通道有独立日志文件", pane1.var_log_path.get().endswith("%s.log" % pane1.var_name.get()),
          pane1.var_log_path.get())
    check("通道列表里有 2 行", len(app.lst_channels.get_children()) == 2)
    check("发送栏跟着当前通道走", pane1.var_name.get() in app.var_send_to.get(), app.var_send_to.get())

    port_b = free_port()
    pane1.var_port.set("loop://")
    pane1.var_tcp_port.set(str(port_b))
    pane1.var_banner.set("SMOKE-B")
    pane0.var_port.set("loop://")
    pane0.var_tcp_port.set(str(tcp_port))
    pane0.var_banner.set("SMOKE-A")

    failures = app.start_all()
    pump(root, 0.8)
    check("全部启动成功", not failures, str(failures))
    check("两个通道都在运行", len(app.manager.running()) == 2,
          str([ch.cfg.name for ch in app.manager.running()]))

    client_a = connect(tcp_port)
    client_b = connect(port_b)
    pump(root, 0.6)
    check("两个通道各收到 1 个客户端",
          app.manager.channels()[0].client_count == 1 and app.manager.channels()[1].client_count == 1,
          "%d / %d" % (app.manager.channels()[0].client_count, app.manager.channels()[1].client_count))
    both = app.client_lines()
    check("客户端列表同时显示两个通道的客户端", len(both) == 2, str(both))
    check("客户端列表能区分通道", len({line.split("]")[0] for line in both}) == 2, str(both))

    hello_a, hello_b = recv_some(client_a), recv_some(client_b)
    check("A 通道客户端收到自己的欢迎语", b"SMOKE-A" in hello_a, repr(hello_a[:30]))
    check("B 通道客户端收到自己的欢迎语", b"SMOKE-B" in hello_b, repr(hello_b[:30]))

    client_a.sendall(b"ONLY-A\r\n")
    pump(root, 0.8)
    got_a, got_b = recv_some(client_a), recv_some(client_b)
    check("A 通道数据回到 A 客户端", b"ONLY-A" in got_a, repr(got_a[:30]))
    check("A 通道数据不会串到 B 通道", b"ONLY-A" not in got_b, repr(got_b[:30]))

    app.var_only_current.set(True)
    app.select_channel(1)
    pump(root, 0.3)
    check("只看当前通道时客户端列表只剩该通道",
          len(app.client_lines()) == 1 and pane1.var_name.get() in app.client_lines()[0],
          str(app.client_lines()))
    app.var_only_current.set(False)
    app._update_clients()
    pump(root, 0.2)
    check("取消过滤后恢复显示两个通道的客户端", len(app.client_lines()) == 2, str(app.client_lines()))

    # 端口冲突：界面里的检查 + 启动整体拒绝
    DIALOGS.clear()
    pane0.var_tcp_port.set(str(port_b))
    errors, warnings = app.check_conflicts()
    check("界面能检查出端口冲突", bool(errors), str(errors))
    failures = app.start_all()
    pump(root, 0.3)
    check("有端口冲突时整体拒绝启动", -1 in failures, str(failures))
    check("冲突时原有通道不受影响", len(app.manager.running()) == 2,
          str([ch.cfg.name for ch in app.manager.running()]))
    app.on_start_all()          # 走带弹窗的那条路径（弹窗在自测里被替换成记录器）
    check("冲突会弹出提示框", any(kind == "error" for kind, _t, _m in DIALOGS), str(DIALOGS[-1:]))
    pane0.var_tcp_port.set(str(tcp_port))

    # 保存配置（含两个通道）
    DIALOGS.clear()
    app.on_save_config()
    pump(root, 0.2)
    if os.path.isfile(default_config_path()):
        with open(default_config_path(), "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    else:
        saved = {}
    check("配置里保存成多通道结构", saved.get("version") == 2 and len(saved.get("channels", [])) == 2,
          "version=%s / %s 个通道" % (saved.get("version"), len(saved.get("channels", []))))
    check("配置里保存了每个通道的串口与端口",
          any(ch.get("port") == "loop://" and ch.get("listen_port") == port_b
              for ch in saved.get("channels", [])))
    check("配置里保存了上次选中的通道", isinstance(saved.get("selected_channel"), int),
          str(saved.get("selected_channel")))

    # 删除通道
    app.select_channel(1)
    pump(root, 0.2)
    app.on_delete_channel()
    pump(root, 0.4)
    check("删除通道后只剩 1 个", len(app.panes) == 1, str(len(app.panes)))
    check("删除正在运行的通道会先把它停掉", len(app.manager.running()) == 1,
          str([ch.cfg.name for ch in app.manager.running()]))
    check("删除后通道列表也只剩 1 行", len(app.lst_channels.get_children()) == 1)
    check("只剩一个通道时删除按钮禁用", str(app.btn_del["state"]) == "disabled")

    client_b.close()
    app.on_stop_all()
    pump(root, 0.5)
    check("全部停止后没有通道在运行", not app.manager.running(), str(app.manager.running()))
    check("全部停止后按钮恢复", str(app.btn_start_all["state"]) == "normal")

    client_a.close()
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
        check("关闭后配置仍是多通道结构", saved.get("version") == 2 and bool(saved.get("channels")),
              str(saved.get("version")))

    if not had_config and os.path.isfile(default_config_path()):
        os.remove(default_config_path())
        print("（已清理测试生成的 config.json）")

    print("\n结果：%s" % ("全部通过 🎉" if not FAILED else "失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED))))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
