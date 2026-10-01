"""网关：把串口链路与 Telnet 服务端粘合起来（一个网关 = 一个通道）。

数据流：

    串口 --> SerialLink --> Gateway._on_serial_data --> 广播给所有客户端
    客户端 --> TelnetServer --> Gateway.on_client_data --> 换行转换 --> 串口

多通道时每个通道各有一个 :class:`Gateway` 实例（见 :mod:`comtel.manager`），
``channel_id`` 会随每个事件一起发出去，界面/命令行据此区分是哪个通道的数据。
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from .config import ChannelConfig
from .serial_link import SerialLink
from .telnet_server import ClientSession, TelnetServer

# 十六进制显示分组
_HEX_GROUP = 16


def hexdump(data: bytes) -> str:
    """把字节串转成 ``41 42 43`` 形式。"""
    return " ".join("%02X" % b for b in data)


def printable(data: bytes, encoding: str = "utf-8") -> str:
    """可打印文本，控制字符转义显示（用于日志/界面）。"""
    text = data.decode(encoding, errors="replace")
    out = []
    for ch in text:
        if ch == "\r":
            out.append("\\r")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch.isprintable():
            out.append(ch)
        else:
            code = ord(ch)
            out.append("\\x%02X" % code if code < 256 else "\\u%04X" % code)
    return "".join(out)


def convert_tx_newline(data: bytes, mode: str) -> bytes:
    """把客户端发来的换行统一成串口设备需要的行结束符。"""
    if mode == "asis":
        return data
    if mode == "cr":
        end = b"\r"
    elif mode == "lf":
        end = b"\n"
    elif mode == "crlf":
        end = b"\r\n"
    elif mode == "crnul":
        end = b"\r\x00"
    else:
        return data
    # 先吃掉组合形式，再处理单独的 CR / LF
    data = data.replace(b"\r\n", b"\n").replace(b"\r\x00", b"\n")
    data = re.sub(rb"[\r\n]", end, data)
    return data


def normalize_rx(data: bytes) -> bytes:
    """把设备端输出的 CR / LF 统一成 CRLF，便于 Telnet 客户端显示。"""
    if not data:
        return data
    return re.sub(rb"\r\n|\r|\n", b"\r\n", data)


class Gateway:
    """串口 <-> Telnet 的运行实例（一个通道）。"""

    def __init__(self, cfg: ChannelConfig, emit: Optional[Callable[..., None]] = None,
                 channel_id: int = 0) -> None:
        self.cfg = cfg
        self.channel_id = channel_id
        self._emit_cb = emit or (lambda event, **payload: None)

        self._lock = threading.RLock()
        self._clients: List[ClientSession] = []
        self._stopping = False
        self._running = False
        self._started_at = 0.0
        self._log_fh = None
        self._log_lock = threading.Lock()
        self._readonly_notice: Dict[int, float] = {}

        self.serial = SerialLink(cfg, self._on_serial_data, self._on_serial_error, self._on_serial_state)
        self.server = TelnetServer(self)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def name(self) -> str:
        return self.cfg.name or "通道"

    def start(self) -> None:
        if self._running:
            return
        self.cfg.normalize()
        self._stopping = False

        self._open_log_file()
        self._emit("log", level="SYS", text="正在启动：%s" % self.cfg.describe())
        try:
            self.serial.open()
        except Exception as exc:
            self._close_log_file()
            self._emit("log", level="ERR", text="串口打开失败：%s" % exc)
            raise
        try:
            self.server.start()
        except Exception as exc:
            self.serial.close()
            self._close_log_file()
            self._emit("log", level="ERR", text="监听端口失败：%s" % exc)
            raise

        self._running = True
        self._started_at = time.time()
        self._emit(
            "log",
            level="SYS",
            text="已启动，监听 %s（%s）"
            % (self.server.address, "Telnet" if self.cfg.telnet_mode == "telnet" else "裸 TCP"),
        )
        self._emit_state()

    def stop(self) -> None:
        if not self._running and not self.server.is_running and not self.serial.is_open:
            return
        self._stopping = True
        self._emit("log", level="SYS", text="正在停止…")
        try:
            self.server.disconnect_all()
        except Exception:
            pass
        try:
            self.server.stop()
        except Exception:
            pass
        try:
            self.serial.close()
        except Exception:
            pass
        self._running = False
        self._stopping = False
        self._emit("log", level="SYS", text="网关已停止")
        self._close_log_file()
        self._emit_state()

    # ------------------------------------------------------------------
    # 客户端管理
    # ------------------------------------------------------------------
    def clients(self) -> List[ClientSession]:
        with self._lock:
            return list(self._clients)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def register_client(self, sock: socket.socket, addr) -> Optional[ClientSession]:
        if self._stopping:
            try:
                sock.close()
            except OSError:
                pass
            return None

        if not self.cfg.allow_multiple:
            with self._lock:
                busy = len(self._clients) > 0
            if busy:
                try:
                    sock.sendall(
                        ("\r\n[拒绝] 已有客户端在线，本网关仅允许单连接。\r\n").encode(
                            self.cfg.encoding, errors="replace"
                        )
                    )
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
                self._emit("log", level="SYS", text="拒绝来自 %s:%d 的连接（已有客户端）" % addr[:2])
                return None

        session = ClientSession(sock, addr, self)
        with self._lock:
            self._clients.append(session)
        self._recompute_write_perms()
        try:
            session.start()
        except OSError:
            session.close()
            return None
        self._emit(
            "log",
            level="SYS",
            text="客户端接入 %s（当前 %d 个，%s）"
            % (session.peer, self.client_count, "可写" if session.writable else "只读"),
        )
        self._emit_clients()
        return session

    def unregister_client(self, session: ClientSession) -> None:
        with self._lock:
            if session in self._clients:
                self._clients.remove(session)
            else:
                return
        self._recompute_write_perms()
        self._readonly_notice.pop(session.cid, None)
        self._emit(
            "log",
            level="SYS",
            text="客户端断开 %s（收 %d 字节 / 发 %d 字节）"
            % (session.peer, session.rx_bytes, session.tx_bytes),
        )
        self._emit_clients()

    def _recompute_write_perms(self) -> None:
        with self._lock:
            sessions = list(self._clients)
        policy = self.cfg.writer_policy
        if policy == "all":
            for s in sessions:
                s.can_write = True
        elif policy == "none":
            for s in sessions:
                s.can_write = False
        else:  # first：最早接入的客户端可写
            oldest = min(sessions, key=lambda s: (s.connected_at, s.cid)) if sessions else None
            for s in sessions:
                s.can_write = s is oldest

    def disconnect_all(self) -> int:
        return self.server.disconnect_all()

    # ------------------------------------------------------------------
    # 数据通路
    # ------------------------------------------------------------------
    def on_client_data(self, session: ClientSession, data: bytes) -> None:
        if not data:
            return
        if self.cfg.show_tx:
            self._emit("log", level="TX", text=printable(data, self.cfg.encoding), raw=data, peer=session.peer)

        allow = session.writable and self.cfg.writer_policy != "none"
        if not allow:
            now = time.time()
            last = self._readonly_notice.get(session.cid, 0.0)
            if now - last > 5.0:
                self._readonly_notice[session.cid] = now
                session.send_text("\r\n[提示] 当前连接为只读，输入不会被发送到串口。\r\n")
            return

        if self.cfg.server_echo and session.echo_enabled:
            try:
                session.send_bytes(normalize_rx(data))
            except OSError:
                pass

        payload = convert_tx_newline(data, self.cfg.tx_newline)
        try:
            self.serial.write(payload)
        except Exception as exc:
            self._emit("log", level="ERR", text="写入串口失败：%s" % exc)
            session.send_text("\r\n[错误] 写入串口失败：%s\r\n" % exc)
            return
        self._emit_stats()

    def send_to_serial(self, text: str, append_newline: bool = True) -> None:
        """GUI/CLI 直接向串口发送文本。"""
        data = text.encode(self.cfg.encoding, errors="replace")
        if append_newline:
            data += convert_tx_newline(b"\n", self.cfg.tx_newline)
        if self.cfg.show_tx:
            self._emit("log", level="TX", text=printable(data, self.cfg.encoding), raw=data, peer="本地")
        self.serial.write(data)
        self._emit_stats()

    def send_to_serial_bytes(self, data: bytes) -> None:
        if self.cfg.show_tx:
            self._emit("log", level="TX", text=printable(data, self.cfg.encoding), raw=data, peer="本地")
        self.serial.write(data)
        self._emit_stats()

    def _on_serial_data(self, chunk: bytes) -> None:
        payload = normalize_rx(chunk) if self.cfg.rx_normalize_crlf else chunk
        if self.cfg.show_rx:
            self._emit("log", level="RX", text=printable(chunk, self.cfg.encoding), raw=chunk, peer="串口")
        for session in self.clients():
            try:
                session.send_bytes(payload)
            except Exception:
                session.close()
        self._emit_stats()

    def _on_serial_error(self, message: str) -> None:
        self._emit("log", level="ERR", text=message)
        self._emit_state()

    def _on_serial_state(self, opened: bool, message: str) -> None:
        self._emit("log", level="SYS", text=message)
        if not opened and self._running and not self._stopping:
            # 串口被拔出/异常关闭：整个网关停掉，避免出现“假在线”。
            threading.Thread(target=self.stop, name="gateway-stop", daemon=True).start()
        self._emit_state()

    # ------------------------------------------------------------------
    # 事件 / 日志
    # ------------------------------------------------------------------
    def _emit(self, event: str, **payload) -> None:
        payload.setdefault("channel", self.channel_id)
        try:
            self._emit_cb(event, **payload)
        except Exception:
            pass
        if event == "log":
            self._write_log_file(payload)

    def _emit_state(self) -> None:
        self._emit(
            "state",
            running=self._running,
            serial_open=self.serial.is_open,
            listen=self.server.address,
            clients=self.client_count,
        )

    def _emit_clients(self) -> None:
        self._emit("clients", clients=self.clients())
        self._emit_state()

    def _emit_stats(self) -> None:
        self._emit("stats", rx=self.serial.rx_bytes, tx=self.serial.tx_bytes, clients=self.client_count)

    # ------------------------------------------------------------------
    def _open_log_file(self) -> None:
        self._close_log_file()
        if not self.cfg.log_to_file or not self.cfg.log_file:
            return
        path = self.cfg.log_file
        if not os.path.isabs(path):
            from .config import app_dir

            path = os.path.join(app_dir(), path)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._log_fh = open(path, "a", encoding="utf-8", errors="replace")
            self._log_fh.write(
                "\n===== %s 启动 %s =====\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), self.cfg.describe())
            )
            self._log_fh.flush()
        except OSError as exc:
            self._log_fh = None
            self._emit("log", level="ERR", text="日志文件打开失败：%s" % exc)

    def _close_log_file(self) -> None:
        fh, self._log_fh = self._log_fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def _write_log_file(self, payload: dict) -> None:
        if self._log_fh is None:
            return
        level = payload.get("level", "SYS")
        text = payload.get("text", "")
        raw = payload.get("raw")
        line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, text)
        if raw and self.cfg.log_hex:
            line += "  | HEX: %s" % hexdump(raw)
        with self._log_lock:
            if self._log_fh is None:
                return
            try:
                self._log_fh.write(line + "\n")
                self._log_fh.flush()
            except OSError:
                pass
