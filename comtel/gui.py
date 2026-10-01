"""Tkinter 图形界面。

界面在三种「缩放」下都要正常显示：

1. **系统 DPI 缩放**（Windows 125% / 150%）—— 声明 DPI 感知并按真实 DPI 设置 ``tk scaling``，
   字体清晰、尺寸正确，不会被系统位图拉伸成糊的；
2. **窗口缩放**（拖边框 / 最大化）—— 配置区三栏等宽伸展，窗口变窄时自动重排成两栏、单栏，
   客户端列表也会从右侧挪到日志下方，不会把控件挤没；
3. **字号缩放**（Ctrl + 滚轮 / Ctrl +/- / 状态栏按钮）—— 界面与日志字号整体缩放并记住。
"""

from __future__ import annotations

import os
import queue
import re
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk
from typing import Dict, List, Optional

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
)
from .gateway import Gateway, hexdump
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
MIN_WIDTH = 620
MIN_HEIGHT = 540

GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?$")


class BridgeApp(ttk.Frame):
    def __init__(self, master: tk.Tk, cfg: BridgeConfig) -> None:
        enable_dpi_awareness()
        super().__init__(master, padding=8)
        self.master_window = master
        self.cfg = cfg
        self.gateway = Gateway(cfg, self._on_event)
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self._port_map = {}
        self._log_lines = 0
        self._closing = False

        # ---- 缩放相关状态 ----
        self.dpi_scale = detect_scale(master)          # 1.0 = 96 DPI
        apply_tk_scaling(master, self.dpi_scale)       # 必须先于任何字体/控件创建
        self.zoom = min(UI_SCALE_MAX, max(UI_SCALE_MIN, float(cfg.ui_scale or 1.0)))
        self.ui_font = None
        self.log_font = None
        self._layout_mode = ""                          # three | two | stack
        self._body_narrow = None

        self._build_vars()
        self._build_ui()
        self._apply_fonts()
        self._apply_metrics()
        self._sync_widgets_from_config()
        self._install_bindings()
        self._restore_geometry()
        self.refresh_ports()

        self.pack(fill="both", expand=True)
        master.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(60, self._drain_events)
        self.after(80, lambda: self._reflow(self.master_window.winfo_width()))
        self._append_line("SYS", "%s v%s 已就绪，选择串口后点击「启动」。" % (APP_NAME, __version__))
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
        self.var_port = tk.StringVar()
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
        self.var_timestamps = tk.BooleanVar(value=cfg.timestamps)
        self.var_hex = tk.BooleanVar(value=cfg.log_hex)
        self.var_autoscroll = tk.BooleanVar(value=cfg.autoscroll)
        self.var_log_file = tk.BooleanVar(value=cfg.log_to_file)
        self.var_log_path = tk.StringVar(value=cfg.log_file)

        self.var_send_text = tk.StringVar()
        self.var_send_hex = tk.BooleanVar(value=False)
        self.var_send_crlf = tk.BooleanVar(value=True)
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

        # ---------- 配置区（会自动重排：三栏 / 两栏 / 单栏） ----------
        self.opts = ttk.Frame(self)
        self.opts.grid(row=0, column=0, sticky="ew")

        serial_box = ttk.LabelFrame(self.opts, text="串口", padding=6)
        serial_box.grid(row=0, column=0, sticky="nsew")

        ttk.Label(serial_box, text="串口").grid(row=0, column=0, sticky="w")
        self.cmb_port = ttk.Combobox(serial_box, textvariable=self.var_port, width=28)
        self.cmb_port.grid(row=0, column=1, columnspan=3, sticky="we", padx=4, pady=2)
        self.btn_refresh = ttk.Button(serial_box, text="刷新", width=6, command=self.refresh_ports)
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
        self.chk_ts = ttk.Checkbutton(opt_box, text="时间戳", variable=self.var_timestamps)
        self.chk_ts.grid(row=4, column=0, sticky="w", padx=4)
        self.chk_hex = ttk.Checkbutton(opt_box, text="十六进制", variable=self.var_hex)
        self.chk_hex.grid(row=4, column=1, sticky="w", padx=4)
        self.chk_scroll = ttk.Checkbutton(opt_box, text="自动滚动", variable=self.var_autoscroll)
        self.chk_scroll.grid(row=5, column=0, sticky="w", padx=4)

        self.chk_logfile = ttk.Checkbutton(opt_box, text="写日志文件", variable=self.var_log_file)
        self.chk_logfile.grid(row=5, column=1, sticky="w", padx=4)
        self.ent_logpath = ttk.Entry(opt_box, textvariable=self.var_log_path, width=22)
        self.ent_logpath.grid(row=6, column=0, columnspan=2, sticky="we", padx=4, pady=2)

        self.opt_boxes = (serial_box, net_box, opt_box)

        # ---------- 操作按钮 ----------
        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=6)
        self.btn_start = ttk.Button(bar, text="▶ 启动", width=11, command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■ 停止", width=11, command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
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

        # ---------- 主体：日志 + 客户端列表 ----------
        self.body = ttk.Frame(self)
        self.body.grid(row=2, column=0, sticky="nsew")

        self.log_box = ttk.LabelFrame(self.body, text="数据日志", padding=4)
        self.log_box.grid(row=0, column=0, sticky="nsew")
        self.log_box.rowconfigure(0, weight=1)
        self.log_box.columnconfigure(0, weight=1)

        self.txt = tk.Text(
            self.log_box, wrap="char", background="#101418", foreground="#d0d0d0",
            insertbackground="#d0d0d0", height=18, width=20, relief="flat", padx=4, pady=2,
        )
        self.txt.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.log_box, orient="vertical", command=self.txt.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=scroll.set)
        self.txt.tag_configure("RX", foreground="#5fd75f")
        self.txt.tag_configure("TX", foreground="#5fb8ff")
        self.txt.tag_configure("SYS", foreground="#c8a45c")
        self.txt.tag_configure("ERR", foreground="#ff6b6b")
        self.txt.tag_configure("TIME", foreground="#6a7a8a")
        self.txt.tag_configure("HEX", foreground="#8a8a8a")
        self.txt.configure(state="disabled")

        self.cli_box = ttk.LabelFrame(self.body, text="已连接客户端", padding=4)
        self.cli_box.grid(row=0, column=1, sticky="nsew")
        self.cli_box.rowconfigure(0, weight=1)
        self.cli_box.columnconfigure(0, weight=1)
        self.lst_clients = ttk.Treeview(
            self.cli_box, columns=("peer", "mode", "rx", "tx"), show="headings",
            height=16, selectmode="browse",
        )
        self.lst_clients.heading("peer", text="客户端")
        self.lst_clients.heading("mode", text="权限")
        self.lst_clients.heading("rx", text="接收")
        self.lst_clients.heading("tx", text="发送")
        # stretch=False：列宽固定，否则 Treeview 会随着窗口变宽不断“长胖”，
        # 反过来把日志区挤小（grid 会参考控件的期望宽度）
        self.lst_clients.column("peer", anchor="w", stretch=False)
        self.lst_clients.column("mode", anchor="center", stretch=False)
        self.lst_clients.column("rx", anchor="e", stretch=False)
        self.lst_clients.column("tx", anchor="e", stretch=False)
        self.lst_clients.grid(row=0, column=0, sticky="nsew")
        cli_scroll = ttk.Scrollbar(self.cli_box, orient="vertical", command=self.lst_clients.yview)
        cli_scroll.grid(row=0, column=1, sticky="ns")
        self.lst_clients.configure(yscrollcommand=cli_scroll.set)

        # ---------- 发送栏 ----------
        send = ttk.Frame(self)
        send.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(send, text="发送到串口").pack(side="left")
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
        self.lst_clients.column("peer", width=px(150), minwidth=px(90))
        self.lst_clients.column("mode", width=px(56), minwidth=px(46))
        self.lst_clients.column("rx", width=px(72), minwidth=px(56))
        self.lst_clients.column("tx", width=px(72), minwidth=px(56))
        self.master_window.minsize(int(round(MIN_WIDTH * self.dpi_scale)),
                                   int(round(MIN_HEIGHT * self.dpi_scale)))

    def layout_info(self) -> Dict[str, object]:
        """当前布局状态（也方便自测断言）。"""
        return {
            "options": self._layout_mode or "?",
            "body": "below" if self._body_narrow else "right",
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
        """按当前宽度决定配置区栏数与客户端列表位置。"""
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
            self._arrange_options(mode)

        narrow = width < BODY_SIDE_WIDTH * s
        if narrow != self._body_narrow or force:
            self._body_narrow = narrow
            self._arrange_body(narrow)
        self._set_toolbar_compact(width < 820 * s)
        self._wrap_hints(width, mode, s)
        self._update_min_height()

    def _wrap_hints(self, width: int, mode: str, s: float) -> None:
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

    def _update_min_height(self) -> None:
        """配置区堆叠后会更高 —— 同步抬高窗口最小高度，避免控件被裁掉。

        只在数值变化时调用 ``minsize``，否则会和 ``<Configure>`` 事件互相触发。
        """
        self.update_idletasks()
        body_min = int(170 * self.dpi_scale)
        if self._body_narrow:
            body_min += self.cli_box.winfo_reqheight()
        needed = (
            self.opts.winfo_reqheight()
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
            (self.btn_kick, "断开全部", "断开"),
            (self.btn_clear, "清空日志", "清空"),
            (self.btn_save, "保存配置", "保存"),
            (self.btn_logdir, "打开日志目录", "日志目录"),
        )
        for button, full, short in pairs:
            button.configure(text=short if compact else full)
        self.btn_start.configure(text="▶" if compact else "▶ 启动", width=4 if compact else 11)
        self.btn_stop.configure(text="■" if compact else "■ 停止", width=4 if compact else 11)

    def _arrange_options(self, mode: str) -> None:
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
            self.body.columnconfigure(1, weight=0, minsize=int(370 * self.scale()))
            self.log_box.grid(row=0, column=0, columnspan=1, sticky="nsew", padx=(0, 6), pady=0)
            self.cli_box.grid(row=0, column=1, columnspan=1, sticky="nsew", padx=0, pady=0)
            self.lst_clients.configure(height=16)
        self.body.update_idletasks()

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
    # 配置 <-> 控件
    # ==================================================================
    def refresh_ports(self) -> None:
        labels = port_labels()
        self._port_map = {}
        display: List[str] = []
        for label in labels:
            device = device_from_label(label)
            self._port_map[label] = device
            self._port_map[device] = device
            display.append(label)
        current = self.var_port.get().strip()
        self.cmb_port.configure(values=display)
        if current and current not in display:
            # 允许手输：COM3 / loop:// / socket://host:port
            device = device_from_label(current)
            display.insert(0, current)
            self._port_map[current] = device
            self.cmb_port.configure(values=display)
        if not self.var_port.get() and labels:
            self.var_port.set(labels[0])
        self._append_line("SYS", "检测到 %d 个串口。" % len(labels))

    def _sync_widgets_from_config(self) -> None:
        for widget in (self.cmb_parity, self.cmb_stopbits, self.cmb_flow, self.cmb_mode,
                       self.cmb_policy, self.cmb_txnl):
            widget.configure(state="readonly")

    def _collect_config(self) -> BridgeConfig:
        cfg = self.cfg
        raw_port = self.var_port.get().strip()
        cfg.port = self._port_map.get(raw_port, device_from_label(raw_port))

        try:
            cfg.baudrate = int(float(self.var_baud.get()))
        except ValueError:
            raise ValueError("波特率必须是数字")
        try:
            cfg.listen_port = int(self.var_tcp_port.get())
        except ValueError:
            raise ValueError("监听端口必须是数字")
        if not (0 < cfg.listen_port < 65536):
            raise ValueError("监听端口需在 1-65535 之间")

        cfg.bytesize = int(self.var_bytesize.get())
        cfg.parity = {v: k for k, v in PARITY_LABELS.items()}.get(self.var_parity.get(), "N")
        cfg.stopbits = float(self.var_stopbits.get())
        cfg.flow = {v: k for k, v in FLOW_LABELS.items()}.get(self.var_flow.get(), "none")
        cfg.listen_host = self.var_host.get().strip() or "0.0.0.0"
        cfg.telnet_mode = "raw" if self.var_mode.get() == "裸 TCP" else "telnet"
        cfg.allow_multiple = bool(self.var_multi.get())
        cfg.writer_policy = {v: k for k, v in WRITER_POLICY_LABELS.items()}.get(self.var_policy.get(), "first")
        cfg.server_echo = bool(self.var_server_echo.get())
        cfg.banner = self.var_banner.get()
        cfg.tx_newline = {v: k for k, v in TX_NEWLINE_LABELS.items()}.get(self.var_tx_nl.get(), "cr")
        cfg.rx_normalize_crlf = bool(self.var_rx_norm.get())
        cfg.encoding = self.var_encoding.get().strip() or "utf-8"
        cfg.show_rx = bool(self.var_show_rx.get())
        cfg.show_tx = bool(self.var_show_tx.get())
        cfg.timestamps = bool(self.var_timestamps.get())
        cfg.log_hex = bool(self.var_hex.get())
        cfg.autoscroll = bool(self.var_autoscroll.get())
        cfg.log_to_file = bool(self.var_log_file.get())
        cfg.log_file = self.var_log_path.get().strip() or "logs/bridge.log"
        cfg.ui_scale = self.zoom
        cfg.normalize()
        return cfg

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        readonly = "readonly" if enabled else "disabled"
        for widget in (self.cmb_port, self.btn_refresh):
            widget.configure(state=state)
        for widget in (self.cmb_baud, self.cmb_bytesize, self.ent_host, self.ent_port,
                       self.ent_banner, self.ent_logpath, self.cmb_encoding):
            widget.configure(state=state)
        for widget in (self.cmb_parity, self.cmb_stopbits, self.cmb_flow, self.cmb_mode,
                       self.cmb_policy, self.cmb_txnl):
            widget.configure(state=readonly)

    # ==================================================================
    # 动作
    # ==================================================================
    def on_start(self) -> None:
        try:
            cfg = self._collect_config()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.master_window)
            return
        if not cfg.port:
            messagebox.showerror("参数错误", "请选择或输入串口，例如 COM3。", parent=self.master_window)
            return
        try:
            cfg.save()
        except OSError:
            pass
        try:
            self.gateway.start()
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc), parent=self.master_window)
            return
        self._set_controls_enabled(False)
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_kick.configure(state="normal")
        self.btn_send.configure(state="normal")

    def on_stop(self) -> None:
        self.btn_stop.configure(state="disabled")
        self.btn_kick.configure(state="disabled")
        self.btn_send.configure(state="disabled")
        self.gateway.stop()
        self._set_controls_enabled(True)
        self.btn_start.configure(state="normal")

    def on_kick_all(self) -> None:
        n = self.gateway.disconnect_all()
        self._append_line("SYS", "已断开 %d 个客户端。" % n)

    def on_send(self) -> None:
        text = self.var_send_text.get()
        if not text:
            return
        try:
            if self.var_send_hex.get():
                payload = bytes.fromhex(text.replace(",", " ").replace("0x", " "))
                self.gateway.send_to_serial_bytes(payload)
            else:
                self.gateway.send_to_serial(text, append_newline=self.var_send_crlf.get())
        except ValueError as exc:
            messagebox.showerror("HEX 格式错误", "无法解析十六进制内容：%s" % exc, parent=self.master_window)
            return
        except Exception as exc:
            self._append_line("ERR", "发送失败：%s" % exc)
            return
        self.var_send_text.set("")

    def on_save_config(self) -> None:
        try:
            cfg = self._collect_config()
            cfg.window_geometry = self._current_geometry()
            path = cfg.save()
        except (ValueError, OSError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.master_window)
            return
        self._append_line("SYS", "配置已保存：%s" % path)

    def on_open_log_dir(self) -> None:
        from .config import app_dir

        path = self.var_log_path.get().strip() or "logs"
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
                if event == "log":
                    level = payload.get("level", "SYS")
                    if level == "RX" and not self.var_show_rx.get():
                        continue
                    if level == "TX" and not self.var_show_tx.get():
                        continue
                    self._append_line(
                        level,
                        payload.get("text", ""),
                        raw=payload.get("raw"),
                        peer=payload.get("peer"),
                    )
                elif event == "state":
                    self._update_state(payload)
                elif event == "clients":
                    self._update_clients(payload.get("clients", []))
                elif event == "stats":
                    self.var_counters.set(
                        "RX %s / TX %s | 客户端 %d"
                        % (self._human(payload.get("rx", 0)), self._human(payload.get("tx", 0)),
                           payload.get("clients", 0))
                    )
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

    def _update_state(self, payload: dict) -> None:
        running = payload.get("running")
        if running:
            self.var_status.set("运行中 · %s" % payload.get("listen", ""))
            self.lbl_status.configure(foreground="#0a6")
        else:
            self.var_status.set("已停止")
            self.lbl_status.configure(foreground="#a33")
            if self.btn_start["state"] == "disabled" and not self.gateway.running:
                self._set_controls_enabled(True)
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self.btn_kick.configure(state="disabled")
                self.btn_send.configure(state="disabled")

    def _update_clients(self, clients) -> None:
        self.lst_clients.delete(*self.lst_clients.get_children())
        for session in clients:
            self.lst_clients.insert(
                "", "end",
                values=(session.peer, "可写" if session.writable else "只读",
                        self._human(session.rx_bytes), self._human(session.tx_bytes)),
            )

    def client_lines(self) -> List[str]:
        """客户端列表的文本形式（自测用）。"""
        lines = []
        for iid in self.lst_clients.get_children():
            peer, mode, rx, tx = self.lst_clients.item(iid, "values")
            lines.append("[%s] %s  (收%s/发%s)" % ("RW" if mode == "可写" else "RO", peer, rx, tx))
        return lines

    def _append_line(self, level: str, text: str, raw: Optional[bytes] = None, peer: Optional[str] = None) -> None:
        stamp = time.strftime("%H:%M:%S") if self.var_timestamps.get() else ""
        prefix = ""
        if stamp:
            prefix += stamp + " "
        if peer:
            prefix += "%s -> %s | " % (peer, "串口" if level in ("TX",) else "客户端")
        elif level in ("SYS", "ERR"):
            prefix += "[%s] " % level

        self.txt.configure(state="normal")
        if prefix:
            self.txt.insert("end", prefix, ("TIME",))
        self.txt.insert("end", text + "\n", (level,))
        if raw and self.var_hex.get():
            self.txt.insert("end", "        HEX: %s\n" % hexdump(raw), ("HEX",))
        self._log_lines += 1 + (1 if (raw and self.var_hex.get()) else 0)
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
            cfg = self._collect_config()
            cfg.window_geometry = self._current_geometry()
            cfg.save()
        except Exception:
            pass
        try:
            self.gateway.stop()
        except Exception:
            pass
        self.master_window.destroy()


def run_gui(config_path: str = "") -> int:
    enable_dpi_awareness()
    cfg = BridgeConfig.load(config_path)
    root = tk.Tk()
    root.title("%s v%s —— 串口转 Telnet" % (APP_NAME, __version__))
    BridgeApp(root, cfg)
    root.mainloop()
    return 0
