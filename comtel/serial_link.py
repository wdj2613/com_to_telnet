"""串口收发封装（基于 :mod:`comtel.serial_backend`，无第三方依赖）。

* 打开 / 关闭串口，支持 ``COM3``、``loop://``、``socket://host:port``
* 独立读线程，收到数据后回调 ``on_data(bytes)``
* 写操作加锁，线程安全
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .config import ChannelConfig
from .serial_backend import (  # noqa: F401  (对外再导出，方便界面层使用)
    SerialBase,
    SerialException,
    device_from_label,
    list_serial_ports,
    open_serial,
    port_labels,
)


class SerialLink:
    """一条串口链路。"""

    def __init__(
        self,
        cfg: ChannelConfig,
        on_data: Callable[[bytes], None],
        on_error: Callable[[str], None],
        on_state: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        self.cfg = cfg
        self._on_data = on_data
        self._on_error = on_error
        self._on_state = on_state
        self._port: Optional[SerialBase] = None
        self._reader: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._rx_bytes = 0
        self._tx_bytes = 0
        self._opened_at = 0.0

    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._port is not None and self._port.is_open

    @property
    def rx_bytes(self) -> int:
        return self._rx_bytes

    @property
    def tx_bytes(self) -> int:
        return self._tx_bytes

    @property
    def opened_at(self) -> float:
        return self._opened_at

    # ------------------------------------------------------------------
    def open(self) -> None:
        if self.is_open:
            return
        cfg = self.cfg
        if not cfg.port:
            raise ValueError("未选择串口")

        port = open_serial(
            cfg.port,
            baudrate=cfg.baudrate,
            bytesize=cfg.bytesize,
            parity=cfg.parity,
            stopbits=cfg.stopbits,
            flow=cfg.flow,
        )
        port.open()
        self._port = port
        self._stop.clear()
        self._opened_at = time.time()
        self._reader = threading.Thread(target=self._read_loop, args=(port,),
                                        name="serial-reader", daemon=True)
        self._reader.start()
        if self._on_state:
            self._on_state(True, "串口已打开")

    def close(self) -> None:
        self._stop.set()
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.5)
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass
        if self._on_state:
            self._on_state(False, "串口已关闭")

    # ------------------------------------------------------------------
    def write(self, data: bytes) -> int:
        if not data:
            return 0
        port = self._port
        if port is None or not port.is_open:
            raise SerialException("串口未打开")
        with self._write_lock:
            n = port.write(data)
        self._tx_bytes += len(data)
        return n or len(data)

    def reset_input_buffer(self) -> None:
        if self._port is not None:
            try:
                self._port.reset_input_buffer()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _read_loop(self, port: SerialBase) -> None:
        while not self._stop.is_set():
            try:
                chunk = port.read(4096)
            except Exception as exc:  # 设备被拔出、驱动报错等
                if not self._stop.is_set():
                    self._on_error("串口读取失败：%s" % exc)
                    try:
                        self.close()
                    except Exception:
                        pass
                return
            if chunk:
                self._rx_bytes += len(chunk)
                try:
                    self._on_data(chunk)
                except Exception as exc:  # 回调异常不应中断读线程
                    self._on_error("数据处理异常：%s" % exc)
            else:
                # Win32 读为立即返回模式，用很短的等待避免空转
                self._stop.wait(0.005)
