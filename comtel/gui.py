"""Tkinter 图形界面（多通道）。

布局：

    ┌ 工具栏：全部启动 / 全部停止 / 断开全部 / 清空日志 / 保存配置 / 打开日志目录 …… 运行状态 ┐
    ├ 通道区：左侧「通道列表」（状态/名称/串口/监听端口 + 新建/复制/删除/上移/下移）
    │         右侧「所选通道的参数」（串口 / Telnet 服务端 / 转换与日志）+ 该通道自己的启停按钮
    ├ 数据日志（每行带 [通道名] 前缀）+ 已连接客户端（带通道列）
    ├ 发送栏：把内容发到「所选通道」的串口
    └ 状态栏：所有通道汇总统计 + 缩放控件

界面在三种「缩放」下都要正常显示：

1. **系统 DPI 缩放**（Windows 125% / 150%）—— 声明 DPI 感知并按真实 DPI 设置 ``tk scaling``，
   字体清晰、尺寸正确，不会被系统位图拉伸成糊的；
2. **窗口缩放**（拖边框 / 最大化）—— 参数区三栏等宽伸展，窗口变窄时自动重排成两栏、单栏，
   通道列表从左侧挪到参数区上方，客户端列表从右侧挪到日志下方，都不会把控件挤没；
3. **字号缩放**（Ctrl + 滚轮 / Ctrl +/- / 状态栏按钮）—— 界面与日志字号整体缩放并记住。
"""

from __future__ import annotations

import os
import queue
import re
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk
from typing import Dict, List, Optional, Tuple

from . import APP_NAME, __version__
from .config import (
    BAUD_CHOICES,
    BYTESIZE_CHOICES,
    FLOW_CHOICES,
    FLOW_LABELS,
    PARITY_CHOICES,
    PARITY_LABELS,
    STOPBITS_CHOICES,
    TX_NEWLINE_CHOICES,
    TX_NEWLINE_LABELS,
    UI_SCALE_MAX,
    UI_SCALE_MIN,
    WRITER_POLICY_CHOICES,
    WRITER_POLICY_LABELS,
    BridgeConfig,
    ChannelConfig,
)
from .gateway import hexdump
from .manager import ChannelManager
from .serial_link import device_from_label, port_labels
from .ui_scale import apply_tk_scaling, detect_scale, enable_dpi_awareness, pick_family, screen_info

MAX_LOG_LINES = 4000

BASE_UI_FONT = 9      # 界面基准字号（磅）
BASE_LOG_FONT = 9     # 日志基准字号（磅）
ZOOM_STEP = 0.1

UI_FAMILIES = ("Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei", "Segoe UI", "TkDefaultFont")
MONO_FAMILIES = ("Consolas", "Cascadia Mono", "Courier New", "TkFixedFont")

# 默认窗口尺寸（96 DPI 下的像素值，运行时按缩放系数放大）
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 720

# 重排阈值（同样按缩放系数放大）：>= THREE 三栏，>= TWO 两栏，否则单栏堆叠
# 注意：窗口最小宽度取 MIN_WIDTH，必须小于 TWO_COL_WIDTH，否则「单栏堆叠」永远到不了
THREE_COL_WIDTH = 1040
TWO_COL_WIDTH = 700
BODY_SIDE_WIDTH = 900
CHANNEL_SIDE_WIDTH = 900   # 通道列表在左侧的宽度门槛，窄于此则挪到参数区上方
MIN_WIDTH = 620
MIN_HEIGHT = 540

GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?$")


class ChannelPane(ttk.Frame):
    """一个通道的参数面板（对应左侧列表里的一行）。"""

    def __init__(self, master: tk.Widget, app: "BridgeApp", cfg: ChannelConfig) -> None:
        super().__init__(master)
        self.app = app
        self.cfg = cfg
        self._build_vars()
        self._build_ui()
        self.sync_from_config()

    # ==================================================================
    def _build_vars(self) -> None:
        cfg = self.cfg
        self.var_name = tk.StringVar(value=cfg.name)
        self.var_port = tk.StringVar(value=cfg.port)
        self.var_baud = tk.StringVar(value=str(cfg.baudrate))
        self.var_bytesize = tk.StringVar(value=str(cfg.bytesize))
        self.var_parity = tk.StringVar(value=PARITY_LABELS.get(cfg.parity, "None"))
        self.var_stopbits = tk.StringVar(value=str(cfg.stopbits))

        self.var_flow = tk.StringVar(value=FLOW_LABELS.get(cfg.flow, "无"))
        self.var_host = tk.StringVar(value=cfg.listen_host)
        self.var_tcp_port = tk.StringVar(value=str(cfg.listen_port))
        self.var_mode = tk.StringVar(value="Telnet" if cfg.telnet_mode == "telnet" else "裸 TCP")
        self.var_multi = tk.BooleanVar(value=cfg.allow_multiple)
        self.var_policy = tk.StringVar(value=WRITER_POLICY_LABELS.get(cfg.writer_policy, ""))
        self.var_server_echo = tk.BooleanVar(value=cfg.server_echo)
        self.var_banner = tk.StringVar(value=cfg.banner)

        self.var_tx_nl = tk.StringVar(value=TX_NEWLINE_LABELS.get(cfg.tx_newline, "CR (\\r)"))
        self.var_rx_norm = tk.BooleanVar(value=cfg.rx_normalize_crlf)
        self.var_encoding = tk.StringVar(value=cfg.encoding)

        self.var_show_rx = tk.BooleanVar(value=cfg.show_rx)
        self.var_show_tx = tk.BooleanVar(value=cfg.show_tx)
        self.var_hex = tk.BooleanVar(value=cfg.log_hex)
        self.var_log_file = tk.BooleanVar(value=cfg.log_to_file)
        self.var_log_path = tk.StringVar(value=cfg.log_file)
        self.var_state = tk.StringVar(value="未启动")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ---------- 头部：名称 + 本通道启停 + 状态 ----------
        self.header = ttk.Frame(self)
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(self.header, text="通道名").pack(side="left")
        self.ent_name = ttk.Entry(self.header, textvariable=self.var_name, width=12)
        self.ent_name.pack(side="left", padx=(4, 10))
        self.btn_start = ttk.Button(self.header, text="▶ 启动", width=9, command=self.app.on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(self.header, text="■ 停止", width=9, command=self.app.on_stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.lbl_state = ttk.Label(self.header, textvariable=self.var_state, foreground="#666666")
        self.lbl_state.pack(side="left", padx=6)

        # ---------- 参数区（会自动重排：三栏 / 两栏 / 单栏） ----------
        self.opts = ttk.Frame(self)
        self.opts.grid(row=1, column=0, sticky="nsew")

        serial_box = ttk.LabelFrame(self.opts, text="串口", padding=6)
        serial_box.grid(row=0, column=0, sticky="nsew")

        ttk.Label(serial_box, text="串口").grid(row=0, column=0, sticky="w")
        self.cmb_port = ttk.Combobox(serial_box, textvariable=self.var_port, width=28)
        self.cmb_port.grid(row=0, column=1, columnspan=3, sticky="we", padx=4, pady=2)
        self.btn_refresh = ttk.Button(serial_box, text="刷新", width=6, command=self.app.refresh_ports)
        self.btn_refresh.grid(row=0, column=4, padx=(2, 0))
        serial_box.columnconfigure(1, weight=1)

        ttk.Label(serial_box, text="波特率").grid(row=1, column=0, sticky="w")
        self.cmb_baud = ttk.Combobox(
            serial_box, textvariable=self.var_baud, values=[str(b) for b in BAUD_CHOICES], width=10
        )
        self.cmb_baud.grid(row=1, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(serial_box, text="数据位").grid(row=1, column=2, sticky="e")
        self.cmb_bytesize = ttk.Combobox(
            serial_box, textvariable=self.var_bytesize, values=[str(b) for b in BYTESIZE_CHOICES], width=5
        )
        self.cmb_bytesize.grid(row=1, column=3, sticky="w", padx=4, pady=2)

        ttk.Label(serial_box, text="校验").grid(row=2, column=0, sticky="w")
        self.cmb_parity = ttk.Combobox(
            serial_box, textvariable=self.var_parity, values=[PARITY_LABELS[p] for p in PARITY_CHOICES], width=10,
            state="readonly",
        )
        self.cmb_parity.grid(row=2, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(serial_box, text="停止位").grid(row=2, column=2, sticky="e")
        self.cmb_stopbits = ttk.Combobox(
            serial_box, textvariable=self.var_stopbits,
            values=[str(s) for s in STOPBITS_CHOICES], width=5, state="readonly",
        )
        self.cmb_stopbits.grid(row=2, column=3, sticky="w", padx=4, pady=2)

        ttk.Label(serial_box, text="流控").grid(row=3, column=0, sticky="w")
        self.cmb_flow = ttk.Combobox(
            serial_box, textvariable=self.var_flow, values=[FLOW_LABELS[f] for f in FLOW_CHOICES], width=16,
            state="readonly",
        )
        self.cmb_flow.grid(row=3, column=1, columnspan=3, sticky="we", padx=4, pady=2)

        # ---------- 服务端 ----------
        net_box = ttk.LabelFrame(self.opts, text="Telnet 服务端", padding=6)
        net_box.grid(row=0, column=1, sticky="nsew")
        net_box.columnconfigure(1, weight=1)

        ttk.Label(net_box, text="监听地址").grid(row=0, column=0, sticky="w")
        self.ent_host = ttk.Entry(net_box, textvariable=self.var_host, width=14)
        self.ent_host.grid(row=0, column=1, sticky="we", padx=4, pady=2)

        ttk.Label(net_box, text="端口").grid(row=0, column=2, sticky="e")
        self.ent_port = ttk.Entry(net_box, textvariable=self.var_tcp_port, width=8)
        self.ent_port.grid(row=0, column=3, sticky="w", padx=4, pady=2)

        ttk.Label(net_box, text="模式").grid(row=1, column=0, sticky="w")
        self.cmb_mode = ttk.Combobox(
            net_box, textvariable=self.var_mode, values=["Telnet", "裸 TCP"], width=10, state="readonly"
        )
        self.cmb_mode.grid(row=1, column=1, sticky="w", padx=4, pady=2)

        self.chk_multi = ttk.Checkbutton(net_box, text="允许多客户端", variable=self.var_multi)
        self.chk_multi.grid(row=1, column=2, columnspan=2, sticky="w", padx=4)

        ttk.Label(net_box, text="写入权限").grid(row=2, column=0, sticky="w")
        self.cmb_policy = ttk.Combobox(
            net_box, textvariable=self.var_policy,
            values=[WRITER_POLICY_LABELS[p] for p in WRITER_POLICY_CHOICES],
            width=28, state="readonly",
        )
        self.cmb_policy.grid(row=2, column=1, columnspan=3, sticky="we", padx=4, pady=2)

        self.chk_echo = ttk.Checkbutton(net_box, text="服务端回显（部分客户端需要）", variable=self.var_server_echo)
        self.chk_echo.grid(row=3, column=0, columnspan=4, sticky="w", padx=4)

        ttk.Label(net_box, text="欢迎语").grid(row=4, column=0, sticky="w")
        self.ent_banner = ttk.Entry(net_box, textvariable=self.var_banner, width=32)
        self.ent_banner.grid(row=4, column=1, columnspan=3, sticky="we", padx=4, pady=2)
        self.lbl_hint = ttk.Label(
            net_box,
            text="占位符：{port} {baud} {peer} {writable}；空则不发欢迎语。可用 \\r\\n 换行。",
            foreground="#666666",
            wraplength=320,
        )
        self.lbl_hint.grid(row=5, column=0, columnspan=4, sticky="w", padx=4)

        # ---------- 转换 / 日志选项 ----------
        opt_box = ttk.LabelFrame(self.opts, text="转换与日志", padding=6)
        opt_box.grid(row=0, column=2, sticky="nsew")
        opt_box.columnconfigure(1, weight=1)

        ttk.Label(opt_box, text="客户端→串口 换行").grid(row=0, column=0, sticky="w")
        self.cmb_txnl = ttk.Combobox(
            opt_box, textvariable=self.var_tx_nl, values=[TX_NEWLINE_LABELS[m] for m in TX_NEWLINE_CHOICES],
            width=14, state="readonly",
        )
        self.cmb_txnl.grid(row=0, column=1, sticky="w", padx=4, pady=2)

        self.chk_rxnorm = ttk.Checkbutton(opt_box, text="串口→客户端 归一化 CRLF", variable=self.var_rx_norm)
        self.chk_rxnorm.grid(row=1, column=0, columnspan=2, sticky="w", padx=4)

        ttk.Label(opt_box, text="编码").grid(row=2, column=0, sticky="w")
        self.cmb_encoding = ttk.Combobox(
            opt_box, textvariable=self.var_encoding, values=["utf-8", "gbk", "ascii", "latin-1"], width=10
        )
        self.cmb_encoding.grid(row=2, column=1, sticky="w", padx=4, pady=2)

        self.chk_show_rx = ttk.Checkbutton(opt_box, text="显示接收 (RX)", variable=self.var_show_rx)
        self.chk_show_rx.grid(row=3, column=0, sticky="w", padx=4)
        self.chk_show_tx = ttk.Checkbutton(opt_box, text="显示发送 (TX)", variable=self.var_show_tx)
        self.chk_show_tx.grid(row=3, column=1, sticky="w", padx=4)

        self.chk_logfile = ttk.Checkbutton(opt_box, text="写日志文件", variable=self.var_log_file)
        self.chk_logfile.grid(row=4, column=0, sticky="w", padx=4)
        self.chk_hex = ttk.Checkbutton(opt_box, text="十六进制", variable=self.var_hex)
        self.chk_hex.grid(row=4, column=1, sticky="w", padx=4)
        self.ent_logpath = ttk.Entry(opt_box, textvariable=self.var_log_path, width=22)
        self.ent_logpath.grid(row=5, column=0, columnspan=2, sticky="we", padx=4, pady=2)

        self.opt_boxes = (serial_box, net_box, opt_box)

    # ==================================================================
    # 配置 <-> 控件
    # ==================================================================
    def sync_from_config(self) -> None:
        cfg = self.cfg
        self.var_name.set(cfg.name)
        self.var_port.set(cfg.port)
        self.var_baud.set(str(cfg.baudrate))
        self.var_bytesize.set(str(cfg.bytesize))
        self.var_parity.set(PARITY_LABELS.get(cfg.parity, "None"))
        self.var_stopbits.set(str(cfg.stopbits))
        self.var_flow.set(FLOW_LABELS.get(cfg.flow, "无"))
        self.var_host.set(cfg.listen_host)
        self.var_tcp_port.set(str(cfg.listen_port))
        self.var_mode.set("Telnet" if cfg.telnet_mode == "telnet" else "裸 TCP")
        self.var_multi.set(cfg.allow_multiple)
        self.var_policy.set(WRITER_POLICY_LABELS.get(cfg.writer_policy, ""))
        self.var_server_echo.set(cfg.server_echo)
        self.var_banner.set(cfg.banner)
        self.var_tx_nl.set(TX_NEWLINE_LABELS.get(cfg.tx_newline, "CR (\\r)"))
        self.var_rx_norm.set(cfg.rx_normalize_crlf)
        self.var_encoding.set(cfg.encoding)
        self.var_show_rx.set(cfg.show_rx)
        self.var_show_tx.set(cfg.show_tx)
        self.var_hex.set(cfg.log_hex)
        self.var_log_file.set(cfg.log_to_file)
        self.var_log_path.set(cfg.log_file)

    def collect(self, strict: bool = False) -> Optional[str]:
        """把界面上的值收回 ``self.cfg``。

        ``strict=True`` 时非法输入直接抛 :class:`ValueError`；否则只是返回错误说明，
        并把非法字段保持成原来的值（切通道、关窗口时不至于被一个笔误卡住）。
        """
        cfg = self.cfg
        problems: List[str] = []

        name = self.var_name.get().strip()
        cfg.name = name or cfg.name

        cfg.port = self.app.resolve_port(self.var_port.get().strip())

        text = self.var_baud.get().strip()
        try:
            cfg.baudrate = int(float(text))
        except ValueError:
            problems.append("波特率必须是数字")

        text = self.var_tcp_port.get().strip()
        try:
            port = int(text)
        except ValueError:
            problems.append("监听端口必须是数字")
        else:
            if 0 < port < 65536:
                cfg.listen_port = port
            else:
                problems.append("监听端口需在 1-65535 之间")

        try:
            cfg.bytesize = int(self.var_bytesize.get())
        except ValueError:
            problems.append("数据位必须是数字")

        cfg.parity = {v: k for k, v in PARITY_LABELS.items()}.get(self.var_parity.get(), cfg.parity)
        try:
            cfg.stopbits = float(self.var_stopbits.get())
        except ValueError:
            problems.append("停止位必须是数字")
        cfg.flow = {v: k for k, v in FLOW_LABELS.items()}.get(self.var_flow.get(), cfg.flow)
        cfg.listen_host = self.var_host.get().strip() or "0.0.0.0"
        cfg.telnet_mode = "raw" if self.var_mode.get() == "裸 TCP" else "telnet"
        cfg.allow_multiple = bool(self.var_multi.get())
        cfg.writer_policy = {v: k for k, v in WRITER_POLICY_LABELS.items()}.get(self.var_policy.get(), cfg.writer_policy)
        cfg.server_echo = bool(self.var_server_echo.get())
        cfg.banner = self.var_banner.get()
        cfg.tx_newline = {v: k for k, v in TX_NEWLINE_LABELS.items()}.get(self.var_tx_nl.get(), cfg.tx_newline)
        cfg.rx_normalize_crlf = bool(self.var_rx_norm.get())
        cfg.encoding = self.var_encoding.get().strip() or cfg.encoding
        cfg.show_rx = bool(self.var_show_rx.get())
        cfg.show_tx = bool(self.var_show_tx.get())
        cfg.log_hex = bool(self.var_hex.get())
        cfg.log_to_file = bool(self.var_log_file.get())
        cfg.log_file = self.var_log_path.get().strip() or cfg.log_file
        cfg.normalize()

        if not problems:
            return None
        detail = "；".join(problems)
        if strict:
            raise ValueError("通道「%s」：%s" % (cfg.name, detail))
        return "通道「%s」：%s" % (cfg.name, detail)

    def set_port_values(self, values: List[str]) -> None:
        current = self.var_port.get()
        self.cmb_port.configure(values=values)
        if current and current not in values:
            values = values + [current]
            self.cmb_port.configure(values=values)

    # ==================================================================
    # 状态 / 可用性
    # ==================================================================
    def set_running(self, running: bool, status_text: str = "") -> None:
        self.var_state.set(status_text or ("运行中" if running else "未启动"))
        color = "#0a6" if running else "#666666"
        self.lbl_state.configure(foreground=color)
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.set_enabled(not running)

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        readonly = "readonly" if enabled else "disabled"
        for widget in (self.cmb_port, self.btn_refresh, self.ent_name):
            widget.configure(state=state)
        for widget in (self.cmb_baud, self.cmb_bytesize, self.ent_host, self.ent_port,
                       self.ent_banner, self.ent_logpath, self.cmb_encoding):
            widget.configure(state=state)
        for widget in (self.cmb_parity, self.cmb_stopbits, self.cmb_flow, self.cmb_mode,
                       self.cmb_policy, self.cmb_txnl):
            widget.configure(state=readonly)

    def summary(self) -> str:
        return "%s · %s:%s" % (self.var_port.get() or "(未选串口)",
                               self.var_host.get().strip() or "0.0.0.0",
                               self.var_tcp_port.get().strip())

    # ==================================================================
    # 自适应布局
    # ==================================================================
    def arrange(self, mode: str) -> None:
        for i in range(3):
            self.opts.columnconfigure(i, weight=0, uniform="")
            self.opts.rowconfigure(i, weight=0)
        boxes = self.opt_boxes

        if mode == "three":
            for i in range(3):
                self.opts.columnconfigure(i, weight=1, uniform="opts")
            self.opts.rowconfigure(0, weight=1)
            for i, box in enumerate(boxes):
                box.grid_configure(row=0, column=i, columnspan=1, sticky="nsew",
                                   padx=(0, 6) if i < 2 else 0, pady=0)
        elif mode == "two":
            self.opts.columnconfigure(0, weight=1, uniform="opts")
            self.opts.columnconfigure(1, weight=1, uniform="opts")
            self.opts.rowconfigure(0, weight=1)
            self.opts.rowconfigure(1, weight=1)
            boxes[0].grid_configure(row=0, column=0, columnspan=1, sticky="nsew", padx=(0, 6), pady=(0, 6))
            boxes[1].grid_configure(row=0, column=1, columnspan=1, sticky="nsew", padx=0, pady=(0, 6))
            boxes[2].grid_configure(row=1, column=0, columnspan=2, sticky="nsew", padx=0, pady=0)
        else:  # stack：窗口很窄时纵向堆叠，保证每个控件都完整可见
            self.opts.columnconfigure(0, weight=1)
            for i, box in enumerate(boxes):
                box.grid_configure(row=i, column=0, columnspan=1, sticky="ew",
                                   padx=0, pady=(0, 6) if i < 2 else 0)

    def wrap_hints(self, width: int, mode: str, s: float) -> None:
        """提示文字按当前栏宽换行 —— 否则它会把整个窗口顶宽，窄窗口直接被撑爆。"""
        if mode == "three":
            wrap = int(max(160, width / 3.0 - 30 * s))
        elif mode == "two":
            wrap = int(max(160, width / 2.0 - 30 * s))
        else:
            wrap = int(max(160, width - 40 * s))
        if wrap != getattr(self, "_hint_wrap", 0):
            self._hint_wrap = wrap
            self.lbl_hint.configure(wraplength=wrap)


class BridgeApp(ttk.Frame):
    def __init__(self, master: tk.Tk, cfg: BridgeConfig) -> None:
        enable_dpi_awareness()
        super().__init__(master, padding=8)
        self.master_window = master
        cfg.normalize()
        self.cfg = cfg
        self.manager = ChannelManager(self._on_event)
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.panes: List[ChannelPane] = []
        self.current = min(max(0, int(cfg.selected_channel)), len(cfg.channels) - 1)
        self._port_map: Dict[str, str] = {}
        self._log_lines = 0
        self._closing = False
        self._chan_state: Dict[int, dict] = {}
        self._client_cache: Dict[int, list] = {}
        self._selecting = False

        # ---- 缩放相关状态 ----
        self.dpi_scale = detect_scale(master)          # 1.0 = 96 DPI
        apply_tk_scaling(master, self.dpi_scale)       # 必须先于任何字体/控件创建
        self.zoom = min(UI_SCALE_MAX, max(UI_SCALE_MIN, float(cfg.ui_scale or 1.0)))
        self.ui_font = None
        self.log_font = None
        self._layout_mode = ""                          # three | two | stack
        self._body_narrow = None
        self._list_top = None

        self._build_vars()
        self._build_ui()
        self._apply_fonts()
        self._apply_metrics()
        self._install_bindings()
        self._restore_geometry()

        for channel_cfg in cfg.channels:
            self._create_pane(channel_cfg)
        self._sync_manager()
        self._show_pane(self.current)
        self._refresh_channels()
        self.refresh_ports()
        self._update_actions()

        self.pack(fill="both", expand=True)
        master.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(60, self._drain_events)
        self.after(80, lambda: self._reflow(self.master_window.winfo_width()))
        self._append_line("SYS", "%s v%s 已就绪：可配置多个通道，每个通道把一路串口发布到一个 Telnet 端口。"
                          % (APP_NAME, __version__))
        self._append_line(
            "SYS",
            "界面缩放 %d%%（DPI %d%%）· Ctrl+滚轮 / Ctrl+加减 可缩放界面。"
            % (round(self.zoom * 100), round(self.dpi_scale * 100)),
        )

    # ==================================================================
    # 界面构建
    # ==================================================================
    def _build_vars(self) -> None:
        cfg = self.cfg
        self.var_timestamps = tk.BooleanVar(value=cfg.timestamps)
        self.var_autoscroll = tk.BooleanVar(value=cfg.autoscroll)
        self.var_only_current = tk.BooleanVar(value=False)

        self.var_send_text = tk.StringVar()
        self.var_send_hex = tk.BooleanVar(value=False)
        self.var_send_crlf = tk.BooleanVar(value=True)
        self.var_send_to = tk.StringVar(value="发送到串口")
        self.var_status = tk.StringVar(value="未启动")
        self.var_counters = tk.StringVar(value="RX 0 B / TX 0 B | 客户端 0")
        self.var_zoom = tk.StringVar(value="%d%%" % round(self.zoom * 100))

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        self.columnconfigure(0, weight=1)
        # 只有主体区吸收多余高度；同时保证它不会被压缩到看不见
        self.rowconfigure(2, weight=1, minsize=int(180 * self.dpi_scale))

        # ---------- 工具栏 ----------
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.btn_start_all = ttk.Button(bar, text="▶ 全部启动", width=11, command=self.on_start_all)
        self.btn_start_all.pack(side="left")
        self.btn_stop_all = ttk.Button(bar, text="■ 全部停止", width=11, command=self.on_stop_all,
                                       state="disabled")
        self.btn_stop_all.pack(side="left", padx=4)
        self.btn_kick = ttk.Button(bar, text="断开全部", command=self.on_kick_all, state="disabled")
        self.btn_kick.pack(side="left", padx=4)
        self.btn_clear = ttk.Button(bar, text="清空日志", command=self.clear_log)
        self.btn_clear.pack(side="left", padx=4)
        self.btn_save = ttk.Button(bar, text="保存配置", command=self.on_save_config)
        self.btn_save.pack(side="left", padx=4)
        self.btn_logdir = ttk.Button(bar, text="打开日志目录", command=self.on_open_log_dir)
        self.btn_logdir.pack(side="left", padx=4)
        self.lbl_status = ttk.Label(bar, textvariable=self.var_status, foreground="#0a6")
        self.lbl_status.pack(side="right")
        self.toolbar = bar

        # ---------- 通道区：左列表 + 右详情 ----------
        self.cfg_area = ttk.Frame(self)
        self.cfg_area.grid(row=1, column=0, sticky="nsew")

        self.chan_box = ttk.LabelFrame(self.cfg_area, text="通道", padding=4)
        self.chan_box.grid(row=0, column=0, sticky="nsew")
        self.chan_box.columnconfigure(0, weight=1)
        self.chan_box.rowconfigure(0, weight=1)

        self.lst_channels = ttk.Treeview(
            self.chan_box, columns=("state", "name", "port", "listen"), show="headings",
            height=7, selectmode="browse",
        )
        self.lst_channels.heading("state", text="状态")
        self.lst_channels.heading("name", text="名称")
        self.lst_channels.heading("port", text="串口")
        self.lst_channels.heading("listen", text="监听端口")
        for column in ("state", "name", "port", "listen"):
            self.lst_channels.column(column, anchor="w", stretch=False)
        self.lst_channels.tag_configure("running", foreground="#0a7a4a")
        self.lst_channels.tag_configure("stopped", foreground="#777777")
        self.lst_channels.tag_configure("failed", foreground="#c0392b")
        self.lst_channels.grid(row=0, column=0, sticky="nsew")
        chan_scroll = ttk.Scrollbar(self.chan_box, orient="vertical", command=self.lst_channels.yview)
        chan_scroll.grid(row=0, column=1, sticky="ns")
        self.lst_channels.configure(yscrollcommand=chan_scroll.set)

        chan_btns = ttk.Frame(self.chan_box)
        chan_btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.btn_add = ttk.Button(chan_btns, text="＋ 新建", width=8, command=self.on_add_channel)
        self.btn_add.pack(side="left")
        self.btn_copy = ttk.Button(chan_btns, text="⧉ 复制", width=8, command=self.on_copy_channel)
        self.btn_copy.pack(side="left", padx=4)
        self.btn_del = ttk.Button(chan_btns, text="✕ 删除", width=8, command=self.on_delete_channel)
        self.btn_del.pack(side="left")
        self.btn_up = ttk.Button(chan_btns, text="↑ 上移", width=8, command=lambda: self.on_move_channel(-1))
        self.btn_up.pack(side="left", padx=4)
        self.btn_down = ttk.Button(chan_btns, text="↓ 下移", width=8, command=lambda: self.on_move_channel(1))
        self.btn_down.pack(side="left")

        self.detail = ttk.Frame(self.cfg_area)
        self.detail.grid(row=0, column=1, sticky="nsew")
        self.detail.rowconfigure(0, weight=1)
        self.detail.columnconfigure(0, weight=1)

        # ---------- 主体：日志 + 客户端列表 ----------
        self.body = ttk.Frame(self)
        self.body.grid(row=2, column=0, sticky="nsew")

        self.log_box = ttk.LabelFrame(self.body, text="数据日志", padding=4)
        self.log_box.grid(row=0, column=0, sticky="nsew")
        self.log_box.rowconfigure(1, weight=1)
        self.log_box.columnconfigure(0, weight=1)

        log_opts = ttk.Frame(self.log_box)
        log_opts.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        ttk.Checkbutton(log_opts, text="时间戳", variable=self.var_timestamps).pack(side="left")
        ttk.Checkbutton(log_opts, text="自动滚动", variable=self.var_autoscroll).pack(side="left", padx=6)
        ttk.Checkbutton(log_opts, text="只看当前通道", variable=self.var_only_current,
                        command=self._update_clients).pack(side="left", padx=6)

        self.txt = tk.Text(
            self.log_box, wrap="char", background="#101418", foreground="#d0d0d0",
            insertbackground="#d0d0d0", height=18, width=20, relief="flat", padx=4, pady=2,
        )
        self.txt.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.log_box, orient="vertical", command=self.txt.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=scroll.set)
        self.txt.tag_configure("RX", foreground="#5fd75f")
        self.txt.tag_configure("TX", foreground="#5fb8ff")
        self.txt.tag_configure("SYS", foreground="#c8a45c")
        self.txt.tag_configure("ERR", foreground="#ff6b6b")
        self.txt.tag_configure("TIME", foreground="#6a7a8a")
        self.txt.tag_configure("HEX", foreground="#8a8a8a")
        self.txt.tag_configure("CHAN", foreground="#b48ead")
        self.txt.configure(state="disabled")

        self.cli_box = ttk.LabelFrame(self.body, text="已连接客户端", padding=4)
        self.cli_box.grid(row=0, column=1, sticky="nsew")
        self.cli_box.rowconfigure(0, weight=1)
        self.cli_box.columnconfigure(0, weight=1)
        self.lst_clients = ttk.Treeview(
            self.cli_box, columns=("channel", "peer", "mode", "rx", "tx"), show="headings",
            height=16, selectmode="browse",
        )
        self.lst_clients.heading("channel", text="通道")
        self.lst_clients.heading("peer", text="客户端")
        self.lst_clients.heading("mode", text="权限")
        self.lst_clients.heading("rx", text="接收")
        self.lst_clients.heading("tx", text="发送")
        # stretch=False：列宽固定，否则 Treeview 会随着窗口变宽不断“长胖”，
        # 反过来把日志区挤小（grid 会参考控件的期望宽度）
        for column in ("channel", "peer", "mode", "rx", "tx"):
            self.lst_clients.column(column, anchor="w", stretch=False)
        self.lst_clients.grid(row=0, column=0, sticky="nsew")
        cli_scroll = ttk.Scrollbar(self.cli_box, orient="vertical", command=self.lst_clients.yview)
        cli_scroll.grid(row=0, column=1, sticky="ns")
        self.lst_clients.configure(yscrollcommand=cli_scroll.set)

        # ---------- 发送栏 ----------
        send = ttk.Frame(self)
        send.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.lbl_sendto = ttk.Label(send, textvariable=self.var_send_to)
        self.lbl_sendto.pack(side="left")
        self.ent_send = ttk.Entry(send, textvariable=self.var_send_text)
        self.ent_send.pack(side="left", fill="x", expand=True, padx=6)
        self.ent_send.bind("<Return>", lambda _e: self.on_send())
        ttk.Checkbutton(send, text="HEX", variable=self.var_send_hex).pack(side="left")
        ttk.Checkbutton(send, text="追加换行", variable=self.var_send_crlf).pack(side="left", padx=4)
        self.btn_send = ttk.Button(send, text="发送", command=self.on_send, state="disabled")
        self.btn_send.pack(side="left")
        self.send_bar = send

        # ---------- 状态栏（含缩放控件） ----------
        status = ttk.Frame(self)
        status.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(status, textvariable=self.var_counters).pack(side="left")
        self.lbl_zoom = ttk.Label(status, textvariable=self.var_zoom, width=6, anchor="center")
        ttk.Button(status, text="A＋", width=4, command=self.zoom_in).pack(side="right")
        ttk.Button(status, text="A－", width=4, command=self.zoom_out).pack(side="right", padx=4)
        self.lbl_zoom.pack(side="right")
        ttk.Button(status, text="重置", width=6, command=self.zoom_reset).pack(side="right", padx=4)
        self.status_bar = status

        # 通道列表选中事件
        self.lst_channels.bind("<<TreeviewSelect>>", self._on_channel_selected)

    # ==================================================================
    # 缩放 / 自适应布局
    # ==================================================================
    def scale(self) -> float:
        """当前的综合缩放系数（DPI × 用户字号缩放）。"""
        return self.dpi_scale * self.zoom

    def _scaled_font_size(self, base: int) -> int:
        return max(7, int(round(base * self.zoom)))

    def _apply_fonts(self) -> None:
        """按 DPI + 用户缩放重建字体，并让 ttk 控件一起跟随。"""
        ui_size = self._scaled_font_size(BASE_UI_FONT)
        log_size = self._scaled_font_size(BASE_LOG_FONT)
        ui_family = pick_family(self.master_window, UI_FAMILIES, "TkDefaultFont")
        mono_family = pick_family(self.master_window, MONO_FAMILIES, "TkFixedFont")

        if self.ui_font is None or self.log_font is None:
            self.ui_font = tkfont.Font(root=self.master_window, family=ui_family, size=ui_size)
            self.log_font = tkfont.Font(root=self.master_window, family=mono_family, size=log_size)
        else:
            self.ui_font.configure(family=ui_family, size=ui_size)
            self.log_font.configure(family=mono_family, size=log_size)

        style = ttk.Style(self.master_window)
        style.configure(".", font=self.ui_font)
        style.configure("Treeview", font=self.ui_font)
        style.configure("Treeview.Heading", font=self.ui_font)
        style.configure("Treeview", rowheight=int(round(ui_size * self.dpi_scale * 2.15)))

        self.txt.configure(font=self.log_font)
        self.var_zoom.set("%d%%" % round(self.zoom * 100))

    def _apply_metrics(self) -> None:
        """按缩放调整像素尺寸（列宽、最小宽度、窗口下限）。"""
        s = self.scale()
        px = lambda value: int(round(value * s))  # noqa: E731
        self.lst_clients.column("channel", width=px(80), minwidth=px(60))
        self.lst_clients.column("peer", width=px(150), minwidth=px(90))
        self.lst_clients.column("mode", width=px(56), minwidth=px(46))
        self.lst_clients.column("rx", width=px(72), minwidth=px(56))
        self.lst_clients.column("tx", width=px(72), minwidth=px(56))
        self.lst_channels.column("state", width=px(74), minwidth=px(60))
        self.lst_channels.column("name", width=px(96), minwidth=px(70))
        self.lst_channels.column("port", width=px(150), minwidth=px(90))
        self.lst_channels.column("listen", width=px(120), minwidth=px(90))
        self.master_window.minsize(int(round(MIN_WIDTH * self.dpi_scale)),
                                   int(round(MIN_HEIGHT * self.dpi_scale)))

    def layout_info(self) -> Dict[str, object]:
        """当前布局状态（也方便自测断言）。"""
        return {
            "options": self._layout_mode or "?",
            "body": "below" if self._body_narrow else "right",
            "channels": "top" if self._list_top else "side",
            "selected": self.current,
            "count": len(self.panes),
            "dpi": round(self.dpi_scale, 2),
            "zoom": round(self.zoom, 2),
            "ui_font": int(self.ui_font.cget("size")) if self.ui_font else 0,
            "log_font": int(self.log_font.cget("size")) if self.log_font else 0,
            "width": int(self.master_window.winfo_width()),
            "min_width": int(self.master_window.minsize()[0]),
            "min_height": int(self.master_window.minsize()[1]),
        }

    # ---- 字号缩放 ----
    def zoom_in(self) -> None:
        self.set_zoom(self.zoom + ZOOM_STEP)

    def zoom_out(self) -> None:
        self.set_zoom(self.zoom - ZOOM_STEP)

    def zoom_reset(self) -> None:
        self.set_zoom(1.0)

    def set_zoom(self, value: float, announce: bool = True) -> None:
        value = round(min(UI_SCALE_MAX, max(UI_SCALE_MIN, float(value))), 2)
        if abs(value - self.zoom) < 1e-6:
            return
        self.zoom = value
        self.cfg.ui_scale = value
        self._apply_fonts()
        self._apply_metrics()
        self._reflow(self.master_window.winfo_width(), force=True)
        if announce:
            self._append_line("SYS", "界面缩放已调整为 %d%%。" % round(value * 100))

    # ---- 重排 ----
    def _reflow(self, width: Optional[int] = None, force: bool = False) -> None:
        """按当前宽度决定参数区栏数、通道列表与客户端列表的位置。"""
        if width is None or width <= 1:
            width = self.master_window.winfo_width()
        if width <= 1:
            width = int(DEFAULT_WIDTH * self.dpi_scale)

        s = self.scale()
        if width >= THREE_COL_WIDTH * s:
            mode = "three"
        elif width >= TWO_COL_WIDTH * s:
            mode = "two"
        else:
            mode = "stack"

        if mode != self._layout_mode or force:
            self._layout_mode = mode
            for pane in self.panes:
                pane.arrange(mode)

        list_top = width < CHANNEL_SIDE_WIDTH * s
        if list_top != self._list_top or force:
            self._list_top = list_top
            self._arrange_channels(list_top)

        narrow = width < BODY_SIDE_WIDTH * s
        if narrow != self._body_narrow or force:
            self._body_narrow = narrow
            self._arrange_body(narrow)

        self._set_toolbar_compact(width < 900 * s)
        for pane in self.panes:
            pane.wrap_hints(width, mode, s)
        self._update_min_height()

    def _arrange_channels(self, list_top: bool) -> None:
        """通道列表：宽窗口放左侧，窄窗口挪到参数区上方。"""
        self.chan_box.grid_forget()
        self.detail.grid_forget()
        self.cfg_area.columnconfigure(0, weight=1, minsize=0)
        self.cfg_area.columnconfigure(1, weight=1, minsize=0)
        self.cfg_area.rowconfigure(0, weight=0, minsize=0)
        self.cfg_area.rowconfigure(1, weight=0, minsize=0)

        if list_top:
            self.cfg_area.rowconfigure(1, weight=1)
            self.chan_box.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
            self.detail.grid(row=1, column=0, columnspan=2, sticky="nsew")
            self.lst_channels.configure(height=4)
        else:
            self.cfg_area.columnconfigure(0, weight=0, minsize=int(360 * self.scale()))
            self.cfg_area.rowconfigure(0, weight=1)
            self.chan_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            self.detail.grid(row=0, column=1, sticky="nsew")
            self.lst_channels.configure(height=7)
        self.cfg_area.update_idletasks()

    def _arrange_body(self, narrow: bool) -> None:
        log_min = int(130 * self.dpi_scale)
        # 先整块摘掉再重新摆放：跨列/换行共存时 grid 会留下旧的列比例，
        # 不清理的话「窄 -> 宽」切回来日志和客户端列表宽度是歪的。
        self.log_box.grid_forget()
        self.cli_box.grid_forget()
        self.body.columnconfigure(0, weight=1, minsize=0)
        self.body.columnconfigure(1, weight=0, minsize=0)
        self.body.rowconfigure(0, weight=1, minsize=log_min)
        self.body.rowconfigure(1, weight=0, minsize=0)
        if narrow:
            # 客户端列表放到日志下方，避免把日志挤成一条缝
            self.log_box.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 6))
            self.cli_box.grid(row=1, column=0, columnspan=2, sticky="ew", padx=0, pady=0)
            self.lst_clients.configure(height=5)
        else:
            self.body.columnconfigure(1, weight=0, minsize=int(400 * self.scale()))
            self.log_box.grid(row=0, column=0, columnspan=1, sticky="nsew", padx=(0, 6), pady=0)
            self.cli_box.grid(row=0, column=1, columnspan=1, sticky="nsew", padx=0, pady=0)
            self.lst_clients.configure(height=16)
        self.body.update_idletasks()

    def _update_min_height(self) -> None:
        """参数区堆叠后会更高 —— 同步抬高窗口最小高度，避免控件被裁掉。

        只在数值变化时调用 ``minsize``，否则会和 ``<Configure>`` 事件互相触发。
        """
        self.update_idletasks()
        body_min = int(170 * self.dpi_scale)
        if self._body_narrow:
            body_min += self.cli_box.winfo_reqheight()

        detail_min = 0
        pane = self.pane()
        if pane is not None:
            detail_min = pane.header.winfo_reqheight() + pane.opts.winfo_reqheight()
        if self._list_top:
            detail_min += self.chan_box.winfo_reqheight()

        needed = (
            detail_min
            + self.toolbar.winfo_reqheight()
            + self.send_bar.winfo_reqheight()
            + self.status_bar.winfo_reqheight()
            + body_min
        )
        needed = max(int(round(MIN_HEIGHT * self.dpi_scale)), needed)
        # 不要让最小高度超过屏幕，否则小屏幕上窗口比屏幕还高
        info = screen_info(self.master_window)
        needed = min(needed, max(int(round(MIN_HEIGHT * self.dpi_scale)), info["height"] - int(60 * self.dpi_scale)))
        if needed != getattr(self, "_min_height", 0):
            self._min_height = needed
            self.master_window.minsize(int(round(MIN_WIDTH * self.dpi_scale)), needed)

    def _set_toolbar_compact(self, compact: bool) -> None:
        """窗口窄的时候把按钮文字缩短，避免被裁掉。"""
        if compact == getattr(self, "_toolbar_compact", None):
            return
        self._toolbar_compact = compact
        pairs = (
            (self.btn_start_all, "▶ 全部启动", "▶ 全启"),
            (self.btn_stop_all, "■ 全部停止", "■ 全停"),
            (self.btn_kick, "断开全部", "断开"),
            (self.btn_clear, "清空日志", "清空"),
            (self.btn_save, "保存配置", "保存"),
            (self.btn_logdir, "打开日志目录", "日志目录"),
            (self.btn_add, "＋ 新建", "＋新建"),
            (self.btn_copy, "⧉ 复制", "复制"),
            (self.btn_del, "✕ 删除", "删除"),
            (self.btn_up, "↑ 上移", "↑"),
            (self.btn_down, "↓ 下移", "↓"),
        )
        for button, full, short in pairs:
            button.configure(text=short if compact else full)

    def _install_bindings(self) -> None:
        win = self.master_window
        win.bind("<Configure>", self._on_root_configure)
        win.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        for seq in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            win.bind(seq, lambda _e: (self.zoom_in(), "break")[1])
        for seq in ("<Control-minus>", "<Control-KP_Subtract>"):
            win.bind(seq, lambda _e: (self.zoom_out(), "break")[1])
        for seq in ("<Control-Key-0>", "<Control-KP_0>"):
            win.bind(seq, lambda _e: (self.zoom_reset(), "break")[1])

    def _on_root_configure(self, event: tk.Event) -> None:
        if event.widget is not self.master_window:
            return
        self._reflow(event.width)

    def _on_ctrl_wheel(self, event: tk.Event) -> str:
        if event.delta > 0:
            self.zoom_in()
        elif event.delta < 0:
            self.zoom_out()
        return "break"

    # ---- 窗口大小记忆 ----
    def _restore_geometry(self) -> None:
        win = self.master_window
        info = screen_info(win)
        min_w = int(round(MIN_WIDTH * self.dpi_scale))
        min_h = int(round(MIN_HEIGHT * self.dpi_scale))

        match = GEOMETRY_RE.match((self.cfg.window_geometry or "").strip())
        if match:
            width = max(min_w, min(int(match.group(1)), info["vwidth"]))
            height = max(min_h, min(int(match.group(2)), info["vheight"]))
            x = int(match.group(3)) if match.group(3) else None
            y = int(match.group(4)) if match.group(4) else None
            on_screen = (
                x is not None and y is not None
                and info["vx"] - 8 <= x <= info["vx"] + info["vwidth"] - 120
                and info["vy"] - 8 <= y <= info["vy"] + info["vheight"] - 80
            )
            if on_screen:
                win.geometry("%dx%d+%d+%d" % (width, height, x, y))
            else:
                win.geometry("%dx%d" % (width, height))
                self._center(width, height)
        else:
            width = min(int(DEFAULT_WIDTH * self.dpi_scale), max(min_w, info["width"] - 60))
            height = min(int(DEFAULT_HEIGHT * self.dpi_scale), max(min_h, info["height"] - 100))
            win.geometry("%dx%d" % (width, height))
            self._center(width, height)

        win.minsize(min_w, min_h)
        if self.cfg.ui_scale and abs(self.cfg.ui_scale - self.zoom) > 1e-6:
            self.cfg.ui_scale = self.zoom

    def _center(self, width: int, height: int) -> None:
        info = screen_info(self.master_window)
        x = max(0, info["vx"] + (info["width"] - width) // 2)
        y = max(0, info["vy"] + max(0, (info["height"] - height) // 3))
        self.master_window.geometry("+%d+%d" % (x, y))

    def _current_geometry(self) -> str:
        """关闭时记住窗口大小；最大化/最小化状态不记（避免下次以怪尺寸启动）。"""
        win = self.master_window
        try:
            state = win.state()
        except tk.TclError:
            return self.cfg.window_geometry or ""
        if state in ("zoomed", "iconic"):
            return self.cfg.window_geometry or ""
        if win.winfo_width() <= 1 or win.winfo_height() <= 1:
            return self.cfg.window_geometry or ""
        geom = win.geometry()
        if GEOMETRY_RE.match(geom):
            return geom
        return "%dx%d" % (win.winfo_width(), win.winfo_height())

    # ==================================================================
    # 通道管理（界面）
    # ==================================================================
    def pane(self, index: Optional[int] = None) -> Optional[ChannelPane]:
        index = self.current if index is None else index
        if 0 <= index < len(self.panes):
            return self.panes[index]
        return None

    @property
    def opt_boxes(self):
        """当前通道的三个参数框（自测里按位置断言布局用）。"""
        pane = self.pane()
        return pane.opt_boxes if pane is not None else ()

    @property
    def gateway(self):
        """当前通道的网关实例（方便自测/发送栏取用）。"""
        channels = self.manager.channels()
        if 0 <= self.current < len(channels):
            return channels[self.current].gateway
        return None

    def _create_pane(self, cfg: ChannelConfig) -> ChannelPane:
        pane = ChannelPane(self.detail, self, cfg)
        pane.grid(row=0, column=0, sticky="nsew")
        pane.grid_remove()
        self.panes.append(pane)
        return pane

    def _show_pane(self, index: int) -> None:
        index = min(max(0, index), len(self.panes) - 1)
        self.current = index
        for i, pane in enumerate(self.panes):
            if i == index:
                pane.grid(row=0, column=0, sticky="nsew")
            else:
                pane.grid_remove()
        pane = self.pane()
        if pane is not None:
            pane.arrange(self._layout_mode or "three")
            self.var_send_to.set("发送到 [%s] 串口" % (pane.var_name.get() or "通道"))
            self._update_send_state()
        self._update_clients()

    def _sync_manager(self) -> None:
        """把界面上的通道配置同步给管理器（保留通道 id）。"""
        self.cfg.channels = [pane.cfg for pane in self.panes]
        self.manager.sync(self.cfg.channels)

    def _cid_of_index(self, index: int) -> Optional[int]:
        channels = self.manager.channels()
        if 0 <= index < len(channels):
            return channels[index].id
        return None

    def _index_of_cid(self, cid: Optional[int]) -> int:
        if cid is None:
            return -1
        return self.manager.index_of(cid)

    def _channel_name(self, cid: Optional[int]) -> str:
        index = self._index_of_cid(cid)
        if 0 <= index < len(self.panes):
            return self.panes[index].cfg.name
        return "通道"

    def _refresh_channels(self) -> None:
        """重建左侧通道列表。"""
        self._selecting = True
        try:
            self.lst_channels.delete(*self.lst_channels.get_children())
            for index in range(len(self.panes)):
                self.lst_channels.insert("", "end", iid=str(index), values=self._row_values(index),
                                         tags=(self._row_tag(index),))
            if self.panes:
                self.lst_channels.selection_set(str(self.current))
                self.lst_channels.see(str(self.current))
        finally:
            self._selecting = False

    def _row_values(self, index: int) -> tuple:
        pane = self.panes[index]
        cid = self._cid_of_index(index)
        state = self._chan_state.get(cid, {}) if cid is not None else {}
        if state.get("running"):
            text = "● 运行中"
        elif state.get("failed"):
            text = "! 启动失败"
        else:
            text = "○ 未启动"
        host = pane.var_host.get().strip() or "0.0.0.0"
        port = pane.var_tcp_port.get().strip() or "-"
        return (text, pane.var_name.get() or "(未命名)", pane.var_port.get() or "(未选)", "%s:%s" % (host, port))

    def _row_tag(self, index: int) -> str:
        cid = self._cid_of_index(index)
        state = self._chan_state.get(cid, {}) if cid is not None else {}
        if state.get("running"):
            return "running"
        if state.get("failed"):
            return "failed"
        return "stopped"

    def _update_row(self, index: int) -> None:
        iid = str(index)
        if self.lst_channels.exists(iid):
            self.lst_channels.item(iid, values=self._row_values(index), tags=(self._row_tag(index),))

    def _on_channel_selected(self, _event=None) -> None:
        if self._selecting:
            return
        selection = self.lst_channels.selection()
        if not selection:
            return
        try:
            index = int(selection[0])
        except ValueError:
            return
        if index == self.current:
            return
        self.select_channel(index)

    def select_channel(self, index: int) -> None:
        """切换通道：先把当前通道的编辑收回配置，再显示新通道。"""
        current = self.pane()
        if current is not None:
            problem = current.collect(strict=False)
            if problem:
                self._append_line("ERR", "%s（该字段保持原值）" % problem)
            self._update_row(self.current)
        self._show_pane(index)
        self._refresh_channels()
        self._reflow(self.master_window.winfo_width(), force=True)

    def on_add_channel(self) -> None:
        self._collect_all(strict=False)
        channel_cfg = self.cfg.new_channel()
        self.cfg.channels.append(channel_cfg)
        self._create_pane(channel_cfg)
        self._sync_manager()
        self._show_pane(len(self.panes) - 1)
        self._refresh_channels()
        self._reflow(self.master_window.winfo_width(), force=True)
        self._update_actions()
        self._append_line("SYS", "已新建通道「%s」：监听 %s:%d，日志 %s。"
                          % (channel_cfg.name, channel_cfg.listen_host, channel_cfg.listen_port,
                             channel_cfg.log_file))

    def on_copy_channel(self) -> None:
        pane = self.pane()
        if pane is None:
            return
        pane.collect(strict=False)
        channel_cfg = self.cfg.new_channel(copy_from=pane.cfg)
        self.cfg.channels.append(channel_cfg)
        self._create_pane(channel_cfg)
        self._sync_manager()
        self._show_pane(len(self.panes) - 1)
        self._refresh_channels()
        self._reflow(self.master_window.winfo_width(), force=True)
        self._update_actions()
        self._append_line("SYS", "已复制出通道「%s」：监听 %s:%d（串口仍为 %s，请按需修改）。"
                          % (channel_cfg.name, channel_cfg.listen_host, channel_cfg.listen_port,
                             channel_cfg.port or "未选"))

    def on_delete_channel(self) -> None:
        if len(self.panes) <= 1:
            messagebox.showinfo("无法删除", "至少保留一个通道。", parent=self.master_window)
            return
        pane = self.pane()
        if pane is None:
            return
        cid = self._cid_of_index(self.current)
        channel = self.manager.channel(cid) if cid is not None else None
        if channel is not None and channel.running:
            if not messagebox.askyesno("通道正在运行",
                                       "通道「%s」正在运行，删除会先停止它。继续？" % channel.name,
                                       parent=self.master_window):
                return
            try:
                channel.stop()
            except Exception:
                pass
        if not messagebox.askyesno("删除通道", "确定删除通道「%s」？" % pane.var_name.get(),
                                   parent=self.master_window):
            return
        index = self.current
        self.panes[index].destroy()
        del self.panes[index]
        del self.cfg.channels[index]
        self._client_cache.pop(cid, None)
        self._chan_state.pop(cid, None)
        self._sync_manager()
        self._show_pane(min(index, len(self.panes) - 1))
        self._refresh_channels()
        self._reflow(self.master_window.winfo_width(), force=True)
        self._update_actions()
        self._update_clients()
        self._append_line("SYS", "通道已删除。")

    def on_move_channel(self, delta: int) -> None:
        index = self.current
        target = index + delta
        if not (0 <= target < len(self.panes)):
            return
        self._collect_all(strict=False)
        self.panes[index], self.panes[target] = self.panes[target], self.panes[index]
        self.cfg.channels[index], self.cfg.channels[target] = self.cfg.channels[target], self.cfg.channels[index]
        self._sync_manager()
        self._show_pane(target)
        self._refresh_channels()
        self._update_actions()

    # ==================================================================
    # 配置 <-> 控件
    # ==================================================================
    def resolve_port(self, text: str) -> str:
        """把下拉框里的显示文本转成设备名（COM3 / loop:// / socket://host:port）。"""
        if not text:
            return ""
        return self._port_map.get(text, device_from_label(text))

    def refresh_ports(self) -> None:
        labels = port_labels()
        self._port_map = {}
        display: List[str] = []
        for label in labels:
            device = device_from_label(label)
            self._port_map[label] = device
            self._port_map[device] = device
            display.append(label)
        for pane in self.panes:
            pane.set_port_values(display)
        pane = self.pane()
        if pane is not None and not pane.var_port.get() and labels:
            pane.var_port.set(labels[0])
        self._append_line("SYS", "检测到 %d 个串口。" % len(labels))

    def _collect_all(self, strict: bool = False) -> None:
        """把所有通道的编辑收回配置（``self.cfg``）。"""
        problems: List[str] = []
        for pane in self.panes:
            problem = pane.collect(strict=strict)
            if problem:
                problems.append(problem)
        self.cfg.channels = [pane.cfg for pane in self.panes]
        self.cfg.timestamps = bool(self.var_timestamps.get())
        self.cfg.autoscroll = bool(self.var_autoscroll.get())
        self.cfg.ui_scale = self.zoom
        self.cfg.selected_channel = self.current
        self.cfg.normalize()
        for problem in problems:
            self._append_line("ERR", "%s（该字段保持原值）" % problem)
        if problems and strict:
            raise ValueError("\n".join(problems))

    def check_conflicts(self) -> Tuple[List[str], List[str]]:
        """端口冲突检查（保存/启动前都跑一遍）。"""
        self._collect_all(strict=False)
        return self.cfg.conflicts()

    # ==================================================================
    # 动作
    # ==================================================================
    def start_channel(self, index: Optional[int] = None) -> Optional[str]:
        """启动一个通道，返回错误说明；成功返回 ``None``。"""
        index = self.current if index is None else index
        try:
            self._collect_all(strict=True)
        except ValueError as exc:
            return str(exc)
        errors, warnings = self.cfg.conflicts()
        for text in warnings:
            self._append_line("ERR", "提示：%s" % text)
        if errors:
            return "；".join(errors)
        self._sync_manager()
        channels = self.manager.channels()
        if not (0 <= index < len(channels)):
            return "通道不存在"
        channel = channels[index]
        failures = self.manager.start_channels([channel])
        self._record_failures(failures)
        self._refresh_channels()
        self._update_actions()
        if not failures:
            return None
        return failures.get(channel.id, "启动失败")

    def start_all(self) -> Dict[int, str]:
        """启动全部通道。返回失败表：``-1`` 表示整体校验没过，其余为通道 id。"""
        try:
            self._collect_all(strict=True)
        except ValueError as exc:
            return {-1: str(exc)}
        errors, warnings = self.cfg.conflicts()
        for text in warnings:
            self._append_line("ERR", "提示：%s" % text)
        if errors:
            return {-1: "；".join(errors)}
        self._sync_manager()
        failures = self.manager.start_all()
        self._record_failures(failures)
        self._refresh_channels()
        self._update_actions()
        return failures

    def on_start(self, event=None) -> None:
        pane = self.pane()
        if pane is not None and not pane.var_port.get().strip():
            messagebox.showerror("参数错误", "请选择或输入串口，例如 COM3。", parent=self.master_window)
            return
        error = self.start_channel()
        if error:
            messagebox.showerror("启动失败", error, parent=self.master_window)

    def on_start_all(self) -> None:
        failures = self.start_all()
        if -1 in failures:
            messagebox.showerror("启动失败", failures[-1], parent=self.master_window)
            return
        if not failures:
            return
        lines = []
        for cid, reason in failures.items():
            lines.append("「%s」：%s" % (self._channel_name(cid), reason))
        messagebox.showwarning("部分通道启动失败",
                               "以下通道没能启动（其余通道已正常运行）：\n\n" + "\n".join(lines),
                               parent=self.master_window)

    def on_stop(self) -> None:
        cid = self._cid_of_index(self.current)
        if cid is None:
            return
        self.manager.stop(cid)
        self._refresh_channels()
        self._update_actions()

    def on_stop_all(self) -> None:
        self.manager.stop_all()
        self._refresh_channels()
        self._update_actions()

    def on_kick_all(self) -> None:
        n = self.manager.disconnect_all()
        self._append_line("SYS", "已断开 %d 个客户端。" % n)

    def on_send(self) -> None:
        text = self.var_send_text.get()
        if not text:
            return
        channel = None
        channels = self.manager.channels()
        if 0 <= self.current < len(channels):
            channel = channels[self.current]
        if channel is None or not channel.running:
            messagebox.showwarning("通道未启动", "当前通道还没有启动，无法发送。", parent=self.master_window)
            return
        try:
            if self.var_send_hex.get():
                payload = bytes.fromhex(text.replace(",", " ").replace("0x", " "))
                channel.send_bytes(payload)
            else:
                channel.send_text(text, append_newline=self.var_send_crlf.get())
        except ValueError as exc:
            messagebox.showerror("HEX 格式错误", "无法解析十六进制内容：%s" % exc, parent=self.master_window)
            return
        except Exception as exc:
            self._append_line("ERR", "发送失败：%s" % exc)
            return
        self.var_send_text.set("")

    def on_save_config(self) -> None:
        pane = self.pane()
        if pane is not None:
            pane.collect(strict=False)
        try:
            self._collect_all(strict=True)
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.master_window)
            return

        errors, warnings = self.cfg.conflicts()
        try:
            self.cfg.window_geometry = self._current_geometry()
            path = self.cfg.save()
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.master_window)
            return

        self._refresh_channels()
        self._append_line("SYS", "配置已保存：%s（%d 个通道）" % (path, len(self.panes)))
        for text in warnings:
            self._append_line("ERR", "提示：%s" % text)
        if errors:
            for text in errors:
                self._append_line("ERR", "端口冲突：%s" % text)
            messagebox.showwarning(
                "端口冲突",
                "配置已保存，但发现端口冲突：\n\n%s\n\n启动前必须先改掉（否则 Windows 上两个通道会抢同一个端口）。"
                % "\n".join(errors),
                parent=self.master_window,
            )

    def on_open_log_dir(self) -> None:
        from .config import app_dir

        pane = self.pane()
        path = (pane.var_log_path.get().strip() if pane is not None else "") or "logs"
        if not os.path.isabs(path):
            path = os.path.join(app_dir(), path)
        path = os.path.dirname(path) or path
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            self._append_line("ERR", "打开目录失败：%s" % exc)

    def clear_log(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        self._log_lines = 0

    # ==================================================================
    # 事件循环
    # ==================================================================
    def _on_event(self, event: str, **payload) -> None:
        """网关线程回调 —— 只入队，界面在 Tk 线程里更新。"""
        self.events.put((event, payload))

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                cid = payload.get("channel")
                index = self._index_of_cid(cid)
                if event == "log":
                    level = payload.get("level", "SYS")
                    pane = self.panes[index] if 0 <= index < len(self.panes) else None
                    if level == "RX" and pane is not None and not pane.cfg.show_rx:
                        continue
                    if level == "TX" and pane is not None and not pane.cfg.show_tx:
                        continue
                    self._append_line(
                        level,
                        payload.get("text", ""),
                        raw=payload.get("raw"),
                        peer=payload.get("peer"),
                        channel=self._channel_name(cid),
                        hex_enabled=bool(pane.cfg.log_hex) if pane is not None else False,
                    )
                elif event == "state":
                    self._chan_state[cid] = payload
                    if index >= 0:
                        self._update_row(index)
                        self._update_pane_state(index)
                    self._update_actions()
                    self._update_status()
                elif event == "clients":
                    self._client_cache[cid] = payload.get("clients", [])
                    self._update_clients()
                elif event == "stats":
                    self._update_counters()
        except queue.Empty:
            pass
        if not self._closing:
            self.after(60, self._drain_events)

    @staticmethod
    def _human(size: int) -> str:
        if size < 1024:
            return "%d B" % size
        if size < 1024 * 1024:
            return "%.1f KB" % (size / 1024.0)
        return "%.1f MB" % (size / 1048576.0)

    def _update_pane_state(self, index: int) -> None:
        if not (0 <= index < len(self.panes)):
            return
        pane = self.panes[index]
        cid = self._cid_of_index(index)
        state = self._chan_state.get(cid, {}) if cid is not None else {}
        running = bool(state.get("running"))
        if running:
            channel = self.manager.channel(cid) if cid is not None else None
            clients = channel.client_count if channel is not None else state.get("clients", 0)
            pane.set_running(True, "运行中 · %s · 客户端 %d" % (state.get("listen", ""), clients))
        else:
            failed = state.get("failed")
            pane.set_running(False, "启动失败：%s" % failed if failed else "未启动")
        if index == self.current:
            self._update_send_state()

    def _update_status(self) -> None:
        totals = self.manager.totals()
        if totals["running"]:
            running = self.manager.running()
            listen = "、".join("%s:%d" % (ch.cfg.listen_host, ch.cfg.listen_port) for ch in running)
            self.var_status.set("运行中 %d/%d 通道 · %s" % (totals["running"], totals["total"], listen))
            self.lbl_status.configure(foreground="#0a6")
        else:
            self.var_status.set("未启动")
            self.lbl_status.configure(foreground="#a33")
        self._update_counters()

    def _update_counters(self) -> None:
        totals = self.manager.totals()
        self.var_counters.set(
            "RX %s / TX %s | 客户端 %d | 运行 %d/%d 通道"
            % (self._human(totals["rx"]), self._human(totals["tx"]), totals["clients"],
               totals["running"], totals["total"])
        )

    def _update_send_state(self) -> None:
        channel = None
        channels = self.manager.channels()
        if 0 <= self.current < len(channels):
            channel = channels[self.current]
        running = bool(channel is not None and channel.running)
        self.btn_send.configure(state="normal" if running else "disabled")

    def _update_actions(self) -> None:
        channels = self.manager.channels()
        running = [ch for ch in channels if ch.running]
        idle = [ch for ch in channels if not ch.running]
        self.btn_start_all.configure(state="normal" if idle else "disabled")
        self.btn_stop_all.configure(state="normal" if running else "disabled")
        self.btn_kick.configure(state="normal" if running else "disabled")
        self.btn_del.configure(state="normal" if len(self.panes) > 1 else "disabled")
        if 0 <= self.current < len(self.panes):
            self._update_pane_state(self.current)
        self._update_status()

    def _record_failures(self, failures: Dict[int, str]) -> None:
        """把启动失败原因记到通道状态里，列表上会显示「! 启动失败」。"""
        for cid, reason in failures.items():
            if cid is None or cid < 0:
                continue
            state = self._chan_state.setdefault(cid, {})
            state["failed"] = reason
            state["running"] = False
        for channel in self.manager.channels():
            if channel.running:
                self._chan_state.setdefault(channel.id, {}).pop("failed", None)

    def _update_clients(self) -> None:
        only_current = bool(self.var_only_current.get())
        current_cid = self._cid_of_index(self.current)
        self.lst_clients.delete(*self.lst_clients.get_children())
        for cid in sorted(self._client_cache, key=lambda value: (value is None, value)):
            if only_current and cid != current_cid:
                continue
            name = self._channel_name(cid)
            for session in self._client_cache.get(cid, []):
                self.lst_clients.insert(
                    "", "end",
                    values=(name, session.peer, "可写" if session.writable else "只读",
                            self._human(session.rx_bytes), self._human(session.tx_bytes)),
                )

    def client_lines(self) -> List[str]:
        """客户端列表的文本形式（自测用）。"""
        lines = []
        for iid in self.lst_clients.get_children():
            channel, peer, mode, rx, tx = self.lst_clients.item(iid, "values")
            lines.append("[%s][%s] %s  (收%s/发%s)"
                         % (channel, "RW" if mode == "可写" else "RO", peer, rx, tx))
        return lines

    def _append_line(self, level: str, text: str, raw: Optional[bytes] = None,
                     peer: Optional[str] = None, channel: str = "", hex_enabled: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S") if self.var_timestamps.get() else ""
        prefix = ""
        if stamp:
            prefix += stamp + " "
        if channel and level in ("RX", "TX", "ERR"):
            prefix += "[%s] " % channel
        if peer:
            prefix += "%s -> %s | " % (peer, "串口" if level in ("TX",) else "客户端")
        elif level in ("SYS", "ERR"):
            prefix += "[%s] " % level

        self.txt.configure(state="normal")
        if prefix:
            self.txt.insert("end", prefix, ("TIME",))
        self.txt.insert("end", text + "\n", (level,))
        if raw and hex_enabled:
            self.txt.insert("end", "        HEX: %s\n" % hexdump(raw), ("HEX",))
        self._log_lines += 1 + (1 if (raw and hex_enabled) else 0)
        if self._log_lines > MAX_LOG_LINES:
            self.txt.delete("1.0", "%d.0" % (self._log_lines - MAX_LOG_LINES + 1))
            self._log_lines = MAX_LOG_LINES
        if self.var_autoscroll.get():
            self.txt.see("end")
        self.txt.configure(state="disabled")

    # ==================================================================
    def on_close(self) -> None:
        self._closing = True
        try:
            self._collect_all(strict=False)
            self.cfg.window_geometry = self._current_geometry()
            self.cfg.save()
        except Exception:
            pass
        try:
            self.manager.stop_all()
        except Exception:
            pass
        self.master_window.destroy()


def run_gui(config_path: str = "") -> int:
    enable_dpi_awareness()
    cfg = BridgeConfig.load(config_path)
    root = tk.Tk()
    root.title("%s v%s —— 串口转 Telnet（多通道）" % (APP_NAME, __version__))
    BridgeApp(root, cfg)
    root.mainloop()
    return 0
