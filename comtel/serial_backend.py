"""串口后端：只用 Python 标准库（ctypes 调 Win32 API），无需 pyserial。

支持三种设备：

* ``COM3`` / ``\\\\.\\COM10`` —— 真实串口（Win32 CreateFile + DCB）
* ``loop://``               —— 进程内回环口，用于自测与演示（写入什么就读到什么）
* ``socket://host:port``    —— 串口服务器 / TCP-串口转换器（透传原始字节）

对外统一接口：``open() / close() / write() / read() / in_waiting``。
"""

from __future__ import annotations

import ctypes
import re
import socket
import threading
import time
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = hasattr(ctypes, "WinDLL")

# ----------------------------------------------------------------------
# 设备枚举
# ----------------------------------------------------------------------


def _natural_key(device: str):
    m = re.search(r"(\d+)$", device)
    return (0, int(m.group(1))) if m else (1, device)


def _registry_ports() -> List[str]:
    """从注册表 SERIALCOMM 读取本机串口名（最可靠的基础列表）。"""
    try:
        import winreg
    except ImportError:
        return []
    names: List[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            index = 0
            while True:
                try:
                    _name, value, _type = winreg.EnumValue(key, index)
                except OSError:
                    break
                names.append(str(value))
                index += 1
    except OSError:
        return []
    return names


def _setupapi_ports() -> Dict[str, str]:
    """用 SetupAPI 拿串口的友好名称，例如 ``USB-SERIAL CH340 (COM3)``。

    先枚举 ``Ports (COM & LPT)`` 类；若没有结果（某些虚拟串口驱动会注册到
    其它类），再全类兜底扫描一次。整个函数不允许抛异常。
    """
    if not IS_WINDOWS:
        return {}
    try:
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    except OSError:
        return {}

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("ClassGuid", GUID),
            ("DevInst", wintypes.DWORD),
            ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
        ]

    DIGCF_PRESENT = 0x00000002
    DIGCF_ALLCLASSES = 0x00000004
    SPDRP_DEVICEDESC = 0x00000000
    SPDRP_FRIENDLYNAME = 0x0000000C

    try:
        setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
        setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD
        ]
        setupapi.SetupDiEnumDeviceInfo.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)
        ]
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
        setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_byte),
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
    except Exception:
        return {}

    def scan(guid, flags: int) -> Dict[str, str]:
        found: Dict[str, str] = {}
        try:
            handle = setupapi.SetupDiGetClassDevsW(guid, None, None, flags)
        except Exception:
            return found
        if not handle or handle == wintypes.HANDLE(-1).value:
            return found
        try:
            index = 0
            while index < 4096:
                info = SP_DEVINFO_DATA()
                info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
                if not setupapi.SetupDiEnumDeviceInfo(handle, index, ctypes.byref(info)):
                    break
                index += 1
                text = ""
                for prop in (SPDRP_FRIENDLYNAME, SPDRP_DEVICEDESC):
                    buf = ctypes.create_string_buffer(1024)
                    needed = wintypes.DWORD(0)
                    if setupapi.SetupDiGetDeviceRegistryPropertyW(
                        handle, ctypes.byref(info), prop, None,
                        ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)), 1024,
                        ctypes.byref(needed),
                    ):
                        text = buf.value.decode("utf-16-le", errors="replace").strip()
                    if text:
                        break
                match = re.search(r"\((COM\d+)\)", text, re.IGNORECASE)
                if match:
                    device = match.group(1).upper()
                    found[device] = text[: match.start()].strip(" -") or text
        except Exception:
            pass
        finally:
            try:
                setupapi.SetupDiDestroyDeviceInfoList(handle)
            except Exception:
                pass
        return found

    # {4D36E978-E325-11CE-BFC1-08002BE10318} = Ports (COM & LPT)
    ports_class = GUID(
        0x4D36E978, 0xE325, 0x11CE,
        (ctypes.c_ubyte * 8)(0xBF, 0xC1, 0x08, 0x00, 0x2B, 0xE1, 0x03, 0x18),
    )
    found = scan(ctypes.byref(ports_class), DIGCF_PRESENT)
    if not found:
        found = scan(None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    return found


def list_serial_ports() -> List[Dict[str, str]]:
    """返回 [{device, description, hwid}, ...]，按 COM 序号排序。"""
    friendly = _setupapi_ports()
    devices = set(_registry_ports()) | set(friendly.keys())
    items = []
    for device in sorted({d.upper() for d in devices}, key=_natural_key):
        items.append(
            {
                "device": device,
                "description": friendly.get(device, "串口设备" if IS_WINDOWS else ""),
                "hwid": "",
            }
        )
    return items


def port_labels() -> List[str]:
    labels = []
    for item in list_serial_ports():
        desc = item["description"]
        labels.append("%s — %s" % (item["device"], desc) if desc else item["device"])
    return labels


def device_from_label(label: str) -> str:
    if not label:
        return ""
    for sep in (" — ", " - "):
        if sep in label:
            return label.split(sep, 1)[0].strip()
    return label.strip()


# ----------------------------------------------------------------------
# 后端实现
# ----------------------------------------------------------------------


class SerialException(IOError):
    """串口打开/读写失败。"""


class SerialBase:
    """串口后端接口。"""

    port = ""
    baudrate = 9600
    bytesize = 8
    parity = "N"
    stopbits = 1
    flow = "none"

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def write(self, data: bytes) -> int:
        raise NotImplementedError

    def read(self, size: int = 1) -> bytes:
        raise NotImplementedError

    @property
    def in_waiting(self) -> int:
        return 0

    @property
    def is_open(self) -> bool:
        raise NotImplementedError

    def reset_input_buffer(self) -> None:
        pass


class LoopbackSerial(SerialBase):
    """进程内回环：写进去的字节会立刻被读出来（自测/演示用）。"""

    def __init__(self, port: str = "loop://", **params) -> None:
        self.port = port
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._open = False
        for key, value in params.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False
        self.reset_input_buffer()

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, data: bytes) -> int:
        if not self._open:
            raise SerialException("串口未打开")
        with self._lock:
            self._buffer.extend(data)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if not self._open:
            raise SerialException("串口未打开")
        with self._lock:
            if not self._buffer:
                return b""
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
        return chunk

    @property
    def in_waiting(self) -> int:
        return len(self._buffer)

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._buffer.clear()

    def feed(self, data: bytes) -> None:
        """模拟设备主动发送数据。"""
        with self._lock:
            self._buffer.extend(data)


class SocketSerial(SerialBase):
    """连接到串口服务器（TCP 透传）。"""

    def __init__(self, host: str, tcp_port: int, **params) -> None:
        self.host = host
        self.tcp_port = tcp_port
        self.port = "socket://%s:%d" % (host, tcp_port)
        self._sock: Optional[socket.socket] = None
        for key, value in params.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def open(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.tcp_port), timeout=5)
        except OSError as exc:
            raise SerialException("连接 %s 失败：%s" % (self.port, exc))
        sock.setblocking(False)
        self._sock = sock

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def write(self, data: bytes) -> int:
        sock = self._sock
        if sock is None:
            raise SerialException("串口未打开")
        try:
            sock.setblocking(True)
            sock.settimeout(5)
            sock.sendall(data)
            sock.setblocking(False)
        except OSError as exc:
            raise SerialException("写入 %s 失败：%s" % (self.port, exc))
        return len(data)

    def read(self, size: int = 1) -> bytes:
        sock = self._sock
        if sock is None:
            raise SerialException("串口未打开")
        try:
            return sock.recv(size)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise SerialException("读取 %s 失败：%s" % (self.port, exc))

    @property
    def in_waiting(self) -> int:
        return 0


if IS_WINDOWS:

    class _DCB(ctypes.Structure):
        _fields_ = [
            ("DCBlength", wintypes.DWORD),
            ("BaudRate", wintypes.DWORD),
            ("Flags", wintypes.DWORD),  # 位域打包：fBinary/fParity/.../fRtsControl
            ("wReserved", wintypes.WORD),
            ("XonLim", wintypes.WORD),
            ("XoffLim", wintypes.WORD),
            ("ByteSize", wintypes.BYTE),
            ("Parity", wintypes.BYTE),
            ("StopBits", wintypes.BYTE),
            ("XonChar", ctypes.c_char),
            ("XoffChar", ctypes.c_char),
            ("ErrorChar", ctypes.c_char),
            ("EofChar", ctypes.c_char),
            ("EvtChar", ctypes.c_char),
            ("wReserved1", wintypes.WORD),
        ]

    class _COMMTIMEOUTS(ctypes.Structure):
        _fields_ = [
            ("ReadIntervalTimeout", wintypes.DWORD),
            ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
            ("ReadTotalTimeoutConstant", wintypes.DWORD),
            ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
            ("WriteTotalTimeoutConstant", wintypes.DWORD),
        ]

    class _COMSTAT(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("cbInQue", wintypes.DWORD),
            ("cbOutQue", wintypes.DWORD),
        ]

    _PARITY_VALUE = {"N": 0, "O": 1, "E": 2, "M": 3, "S": 4}
    _STOPBITS_VALUE = {1: 0, 1.5: 1, 2: 2}

    class Win32Serial(SerialBase):
        """真实串口（CreateFile + SetCommState + ReadFile/WriteFile）。"""

        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
        PURGE_RXABORT = 0x0002
        PURGE_RXCLEAR = 0x0008
        PURGE_TXABORT = 0x0001
        PURGE_TXCLEAR = 0x0004

        def __init__(self, port: str, **params) -> None:
            self.port = port
            self._handle = None
            self._kernel32 = None
            for key, value in params.items():
                if hasattr(self, key):
                    setattr(self, key, value)

        # --------------------------------------------------------------
        def _api(self):
            if self._kernel32 is None:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CreateFileW.restype = wintypes.HANDLE
                k32.CreateFileW.argtypes = [
                    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
                ]
                k32.GetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(_DCB)]
                k32.SetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(_DCB)]
                k32.SetCommTimeouts.argtypes = [wintypes.HANDLE, ctypes.POINTER(_COMMTIMEOUTS)]
                k32.SetupComm.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
                k32.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                k32.ReadFile.argtypes = [
                    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                ]
                k32.WriteFile.argtypes = [
                    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                ]
                k32.ClearCommError.argtypes = [
                    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_COMSTAT)
                ]
                k32.CloseHandle.argtypes = [wintypes.HANDLE]
                k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
                self._kernel32 = k32
            return self._kernel32

        @staticmethod
        def _device_path(port: str) -> str:
            name = port.strip()
            if name.upper().startswith("\\\\.\\"):
                return name
            return "\\\\.\\" + name

        @staticmethod
        def _dcb_flags(flow: str, parity: str) -> int:
            """按 DCB 的位域布局手工拼 flags。"""
            DTR_ENABLE, DTR_HANDSHAKE = 1, 2
            RTS_ENABLE, RTS_HANDSHAKE = 1, 2
            flags = 1 << 0  # fBinary
            if parity != "N":
                flags |= 1 << 1  # fParity
            if flow == "rtscts":
                flags |= 1 << 2  # fOutxCtsFlow
            if flow == "dsrdtr":
                flags |= 1 << 3  # fOutxDsrFlow
            dtr = DTR_HANDSHAKE if flow == "dsrdtr" else DTR_ENABLE
            flags |= (dtr & 0x3) << 4
            if flow == "xonxoff":
                flags |= 1 << 8  # fOutX
                flags |= 1 << 9  # fInX
            rts = RTS_HANDSHAKE if flow == "rtscts" else RTS_ENABLE
            flags |= (rts & 0x3) << 12
            return flags

        # --------------------------------------------------------------
        def open(self) -> None:
            if self._handle is not None:
                return
            k32 = self._api()
            handle = k32.CreateFileW(
                self._device_path(self.port),
                self.GENERIC_READ | self.GENERIC_WRITE,
                0, None, self.OPEN_EXISTING, 0, None,
            )
            if handle == self.INVALID_HANDLE_VALUE or not handle:
                err = ctypes.get_last_error()
                raise SerialException(
                    "打开 %s 失败：%s" % (
                        self.port,
                        ctypes.WinError(err).strerror if err else "未知错误（端口被占用或不存在）",
                    )
                )
            self._handle = handle
            try:
                k32.SetupComm(handle, 8192, 8192)
                dcb = _DCB()
                dcb.DCBlength = ctypes.sizeof(_DCB)
                if not k32.GetCommState(handle, ctypes.byref(dcb)):
                    raise SerialException("GetCommState 失败：%s" % ctypes.WinError(ctypes.get_last_error()))
                dcb.BaudRate = int(self.baudrate)
                dcb.ByteSize = int(self.bytesize)
                dcb.Parity = _PARITY_VALUE.get(self.parity, 0)
                dcb.StopBits = _STOPBITS_VALUE.get(float(self.stopbits), 0)
                dcb.Flags = self._dcb_flags(self.flow, self.parity)
                dcb.XonChar = b"\x11"
                dcb.XoffChar = b"\x13"
                dcb.XonLim = 2048
                dcb.XoffLim = 512
                if not k32.SetCommState(handle, ctypes.byref(dcb)):
                    raise SerialException("SetCommState 失败：%s" % ctypes.WinError(ctypes.get_last_error()))

                timeouts = _COMMTIMEOUTS()
                # ReadIntervalTimeout=MAXDWORD + 两个总超时为 0 => ReadFile 立即返回
                timeouts.ReadIntervalTimeout = 0xFFFFFFFF
                timeouts.ReadTotalTimeoutMultiplier = 0
                timeouts.ReadTotalTimeoutConstant = 0
                timeouts.WriteTotalTimeoutMultiplier = 0
                timeouts.WriteTotalTimeoutConstant = 5000
                if not k32.SetCommTimeouts(handle, ctypes.byref(timeouts)):
                    raise SerialException("SetCommTimeouts 失败：%s" % ctypes.WinError(ctypes.get_last_error()))
                k32.PurgeComm(handle, self.PURGE_RXCLEAR | self.PURGE_TXCLEAR)
            except Exception:
                self.close()
                raise

        def close(self) -> None:
            handle, self._handle = self._handle, None
            if handle:
                try:
                    self._api().CloseHandle(handle)
                except Exception:
                    pass

        @property
        def is_open(self) -> bool:
            return self._handle is not None

        def write(self, data: bytes) -> int:
            handle = self._handle
            if handle is None:
                raise SerialException("串口未打开")
            k32 = self._api()
            total = 0
            offset = 0
            while offset < len(data):
                chunk = data[offset: offset + 4096]
                written = wintypes.DWORD(0)
                ok = k32.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None)
                if not ok:
                    raise SerialException("写串口失败：%s" % ctypes.WinError(ctypes.get_last_error()))
                if written.value == 0:
                    raise SerialException("写串口超时（设备未就绪）")
                total += written.value
                offset += written.value
            return total

        def read(self, size: int = 1) -> bytes:
            handle = self._handle
            if handle is None:
                raise SerialException("串口未打开")
            buf = ctypes.create_string_buffer(max(1, size))
            read = wintypes.DWORD(0)
            ok = self._api().ReadFile(handle, buf, max(1, size), ctypes.byref(read), None)
            if not ok:
                raise SerialException("读串口失败：%s" % ctypes.WinError(ctypes.get_last_error()))
            return buf.raw[: read.value]

        @property
        def in_waiting(self) -> int:
            handle = self._handle
            if handle is None:
                return 0
            stat = _COMSTAT()
            errors = wintypes.DWORD(0)
            if not self._api().ClearCommError(handle, ctypes.byref(errors), ctypes.byref(stat)):
                return 0
            return int(stat.cbInQue)

        def reset_input_buffer(self) -> None:
            handle = self._handle
            if handle:
                self._api().PurgeComm(handle, self.PURGE_RXCLEAR)


def open_serial(port: str, **params) -> SerialBase:
    """按设备名创建后端对象：``COM3`` / ``loop://`` / ``socket://host:port``。"""
    name = (port or "").strip()
    if not name:
        raise SerialException("未指定串口")
    lower = name.lower()
    if lower.startswith("loop"):
        return LoopbackSerial(name, **params)
    if lower.startswith("socket://") or lower.startswith("tcp://"):
        rest = name.split("://", 1)[1]
        if ":" not in rest:
            raise SerialException("socket:// 需要 host:port 形式，例如 socket://192.168.1.10:4001")
        host, _, port_text = rest.rpartition(":")
        try:
            tcp_port = int(port_text)
        except ValueError:
            raise SerialException("socket:// 端口必须是数字：%s" % name)
        return SocketSerial(host, tcp_port, **params)
    if not IS_WINDOWS:
        raise SerialException("当前系统不支持直接串口，请用 socket:// 或 loop://")
    return Win32Serial(name, **params)
