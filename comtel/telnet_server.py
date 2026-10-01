"""Telnet / 裸 TCP 服务端。

设计要点：
- ``socketserver.ThreadingTCPServer``，每个客户端一个线程；
- 支持多个客户端同时连接，串口数据向所有客户端广播；
- Telnet 选项协商（IAC）只做保守处理：接受 SGA/BINARY/NAWS，拒绝其余；
- 0xFF 在 Telnet 模式下自动转义（IAC IAC）。
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from typing import Callable, List, Optional, Tuple

# Telnet 控制字节
IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240
GA = 249
OPT_ECHO = 1
OPT_SGA = 3
OPT_BINARY = 0
OPT_NAWS = 31

# 协商状态机
_ST_DATA = 0
_ST_IAC = 1
_ST_OPT = 2
_ST_SB = 3
_ST_SB_IAC = 4


class ClientSession:
    """一个已接受的 Telnet/TCP 客户端。"""

    _next_id = 1
    _id_lock = threading.Lock()

    def __init__(self, sock: socket.socket, addr: Tuple[str, int], gateway) -> None:
        with ClientSession._id_lock:
            self.cid = ClientSession._next_id
            ClientSession._next_id += 1
        self.sock = sock
        self.addr = addr
        self.gateway = gateway
        self.cfg = gateway.cfg
        self.connected_at = time.time()
        self.last_rx = time.time()
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.echo_enabled = False
        self.window_size: Optional[Tuple[int, int]] = None
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._state = _ST_DATA

    # ------------------------------------------------------------------
    @property
    def peer(self) -> str:
        return "%s:%d" % (self.addr[0], self.addr[1])

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def writable(self) -> bool:
        return bool(getattr(self, "can_write", False))

    # ------------------------------------------------------------------
    def start(self) -> None:
        mode = self.cfg.telnet_mode
        if mode == "telnet":
            # 开回显时声明 WILL ECHO（客户端若不同意会回 DONT ECHO）；
            # 关回显时声明 WONT ECHO，让客户端自己做本地回显。
            if self.cfg.server_echo:
                self.echo_enabled = True
                self._raw_send(bytes([IAC, WILL, OPT_ECHO]))
            else:
                self._raw_send(bytes([IAC, WONT, OPT_ECHO]))
            self._raw_send(bytes([IAC, WILL, OPT_SGA]))
        banner = self.build_banner()
        if banner:
            self.send_text(banner)

    def build_banner(self) -> str:
        if not self.cfg.banner:
            return ""
        text = self.cfg.banner
        text = text.replace("\\r", "\r").replace("\\n", "\n")
        text = text.replace("{port}", self.cfg.port or "")
        text = text.replace("{baud}", str(self.cfg.baudrate))
        text = text.replace("{peer}", self.peer)
        text = text.replace("{writable}", "读写" if self.writable else "只读")
        if "\\n" not in self.cfg.banner and "\n" not in self.cfg.banner:
            text += "\r\n"
        return text

    # ------------------------------------------------------------------
    def send_text(self, text: str) -> None:
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        data = text.encode(self.cfg.encoding, errors="replace")
        self.send_bytes(data)

    def send_bytes(self, data: bytes) -> None:
        """向客户端发送应用数据（Telnet 模式下自动转义 IAC）。"""
        if not data or self.closed:
            return
        if self.cfg.telnet_mode == "telnet":
            data = data.replace(b"\xff", b"\xff\xff")
        try:
            self._raw_send(data)
        except OSError:
            self.close()

    def _raw_send(self, data: bytes) -> None:
        with self._send_lock:
            if self._closed.is_set():
                return
            self.sock.sendall(data)
            self.tx_bytes += len(data)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.gateway.unregister_client(self)

    # ------------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        """处理一段来自客户端的数据：剥离 Telnet 控制序列后交给网关。"""
        self.last_rx = time.time()
        self.rx_bytes += len(data)
        if self.cfg.telnet_mode != "telnet":
            self.gateway.on_client_data(self, data)
            return

        payload = bytearray()
        for byte in data:
            state = self._state
            if state == _ST_DATA:
                if byte == IAC:
                    self._state = _ST_IAC
                else:
                    payload.append(byte)
            elif state == _ST_IAC:
                if byte == IAC:  # 转义的 0xFF
                    payload.append(IAC)
                    self._state = _ST_DATA
                elif byte in (DO, DONT, WILL, WONT):
                    self._pending_cmd = byte
                    self._state = _ST_OPT
                elif byte == SB:
                    self._sb = bytearray()
                    self._state = _ST_SB
                else:  # 单字节命令（GA/NOP...）直接忽略
                    self._state = _ST_DATA
            elif state == _ST_OPT:
                self._handle_option(getattr(self, "_pending_cmd", DO), byte)
                self._state = _ST_DATA
            elif state == _ST_SB:
                if byte == IAC:
                    self._state = _ST_SB_IAC
                else:
                    getattr(self, "_sb", bytearray()).append(byte)
            elif state == _ST_SB_IAC:
                if byte == SE:
                    self._handle_subnegotiation(bytes(getattr(self, "_sb", b"")))
                    self._state = _ST_DATA
                elif byte == IAC:
                    getattr(self, "_sb", bytearray()).append(IAC)
                    self._state = _ST_SB
                else:
                    self._state = _ST_SB

        if payload:
            self.gateway.on_client_data(self, bytes(payload))

    def _handle_option(self, cmd: int, opt: int) -> None:
        try:
            if cmd == DO:
                if opt in (OPT_SGA, OPT_BINARY):
                    self._raw_send(bytes([IAC, WILL, opt]))
                elif opt == OPT_ECHO and self.cfg.server_echo:
                    self.echo_enabled = True
                    self._raw_send(bytes([IAC, WILL, OPT_ECHO]))
                else:
                    self._raw_send(bytes([IAC, WONT, opt]))
            elif cmd == WILL:
                if opt in (OPT_SGA, OPT_BINARY, OPT_NAWS):
                    self._raw_send(bytes([IAC, DO, opt]))
                else:
                    self._raw_send(bytes([IAC, DONT, opt]))
            elif cmd == DONT:
                # 客户端拒绝了我们声明的选项
                if opt == OPT_ECHO:
                    self.echo_enabled = False
            # WONT：客户端停用某选项，无需回应
        except OSError:
            self.close()

    def _handle_subnegotiation(self, data: bytes) -> None:
        if not data:
            return
        opt = data[0]
        body = data[1:]
        if opt == OPT_NAWS and len(body) >= 4:
            self.window_size = ((body[0] << 8) | body[1], (body[2] << 8) | body[3])
        # 其它子协商（终端类型等）忽略


class _TelnetServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, addr, handler, gateway):  # noqa: D107
        self.gateway = gateway
        super().__init__(addr, handler)

    def handle_error(self, request, client_address):  # noqa: D102
        exc = None
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError)):
            return
        super().handle_error(request, client_address)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):  # noqa: D102
        gateway = self.server.gateway
        session = gateway.register_client(self.request, self.client_address)
        if session is None:
            return
        sock = self.request
        sock.settimeout(0.5)
        try:
            while not session.closed and not gateway.stopping:
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                session.feed(data)
        finally:
            session.close()


class TelnetServer:
    """监听端口并把客户端事件转给网关。"""

    def __init__(self, gateway) -> None:
        self.gateway = gateway
        self.cfg = gateway.cfg
        self._server: Optional[_TelnetServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def address(self) -> str:
        if self._server is None:
            return ""
        host, port = self._server.server_address[:2]
        return "%s:%d" % (host, port)

    def start(self) -> None:
        if self._server is not None:
            return
        host = self.cfg.listen_host or "0.0.0.0"
        server = _TelnetServer((host, self.cfg.listen_port), _Handler, self.gateway)
        server.timeout = 0.5
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="telnet-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def disconnect_all(self) -> int:
        """断开所有客户端，返回断开的数量。"""
        sessions: List[ClientSession] = list(self.gateway.clients())
        for session in sessions:
            session.close()
        return len(sessions)
