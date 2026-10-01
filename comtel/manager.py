"""多通道管理：一个通道 = 一个串口 + 一个 Telnet 监听端口。

* :class:`Channel` —— 一个通道的运行实例（内部包一个 :class:`~comtel.gateway.Gateway`）
* :class:`ChannelManager` —— 按配置维护通道、单独/批量启停、汇总统计

几个关键行为：

1. **故障隔离**：某个通道启动失败（串口打不开、端口被占）不影响其它通道，失败原因会明确
   报到对应通道上；
2. **端口查重**：同一个监听地址端口不允许两个通道同时用 —— Windows 上带 SO_REUSEADDR 的
   第二个绑定会**静默成功**（连接随机落到其中一个），所以必须提前挡掉；
   同一个串口被两个通道使用只提示、不阻断（回环口除外）；
3. **独立启停**：可以只停一个通道，其余通道继续跑。
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .config import ChannelConfig
from .gateway import Gateway


class Channel:
    """一个通道的运行实例。"""

    def __init__(self, cid: int, cfg: ChannelConfig, emit: Optional[Callable[..., None]] = None) -> None:
        self.id = cid
        self.cfg = cfg
        self._emit_cb = emit or (lambda event, **payload: None)
        self.gateway = Gateway(cfg, self._forward, channel_id=cid)

    # ------------------------------------------------------------------
    def _forward(self, event: str, **payload) -> None:
        payload["channel"] = self.id
        self._emit_cb(event, **payload)

    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.cfg.name or ("通道%d" % self.id)

    @property
    def port(self) -> str:
        return self.cfg.port

    @property
    def running(self) -> bool:
        return self.gateway.running

    @property
    def serial_open(self) -> bool:
        return self.gateway.serial.is_open

    @property
    def listen(self) -> str:
        if self.gateway.server.is_running:
            return self.gateway.server.address
        return "%s:%d" % (self.cfg.listen_host, self.cfg.listen_port)

    @property
    def client_count(self) -> int:
        return self.gateway.client_count

    @property
    def rx_bytes(self) -> int:
        return self.gateway.serial.rx_bytes

    @property
    def tx_bytes(self) -> int:
        return self.gateway.serial.tx_bytes

    @property
    def clients(self) -> list:
        return self.gateway.clients()

    # ------------------------------------------------------------------
    def start(self) -> None:
        self.gateway.start()

    def stop(self) -> None:
        self.gateway.stop()

    def disconnect_all(self) -> int:
        return self.gateway.disconnect_all()

    def send_text(self, text: str, append_newline: bool = True) -> None:
        self.gateway.send_to_serial(text, append_newline=append_newline)

    def send_bytes(self, data: bytes) -> None:
        self.gateway.send_to_serial_bytes(data)

    def status_text(self) -> str:
        if self.running:
            return "运行中 · %s · 客户端 %d" % (self.listen, self.client_count)
        return "未启动"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<Channel %d %s %s>" % (self.id, self.name, self.cfg.port or "-")


class ChannelManager:
    """一组通道的集合与生命周期管理。"""

    def __init__(self, emit: Optional[Callable[..., None]] = None) -> None:
        self._emit_cb = emit or (lambda event, **payload: None)
        self._lock = threading.RLock()
        self._channels: List[Channel] = []
        self._next_id = 1

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _emit(self, event: str, **payload) -> None:
        try:
            self._emit_cb(event, **payload)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 通道集合
    # ------------------------------------------------------------------
    def channels(self) -> List[Channel]:
        with self._lock:
            return list(self._channels)

    def channel(self, cid: int) -> Optional[Channel]:
        for item in self.channels():
            if item.id == cid:
                return item
        return None

    def index_of(self, cid: int) -> int:
        for i, item in enumerate(self.channels()):
            if item.id == cid:
                return i
        return -1

    def __len__(self) -> int:
        return len(self.channels())

    def sync(self, cfgs: Iterable[ChannelConfig]) -> List[Channel]:
        """按当前配置对齐通道实例。

        * 配置对象没换过的通道（界面里改了参数但对象还是同一个）保留原实例与 id；
        * 新配置对象 -> 新建通道实例；
        * 配置里被删掉的通道 -> 先停掉再移除。

        返回对齐后的通道列表（顺序与 ``cfgs`` 一致）。
        """
        cfgs = list(cfgs)
        with self._lock:
            old = {id(ch.cfg): ch for ch in self._channels}
            kept: List[Channel] = []
            for cfg in cfgs:
                channel = old.pop(id(cfg), None)
                if channel is None:
                    channel = Channel(self._next_id, cfg, self._emit_cb)
                    self._next_id += 1
                kept.append(channel)
            removed = list(old.values())
            self._channels = kept
            result = list(kept)
        for channel in removed:
            try:
                channel.stop()
            except Exception:
                pass
        return result

    def running(self) -> List[Channel]:
        return [ch for ch in self.channels() if ch.running]

    def running_ids(self) -> List[int]:
        return [ch.id for ch in self.running()]

    def is_anything_running(self) -> bool:
        return any(ch.running for ch in self.channels())

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    def start_channels(self, channels: Optional[Iterable[Channel]] = None) -> Dict[int, str]:
        """逐个启动，返回 ``{通道 id: 失败原因}``（启动成功的通道不出现在返回值里）。"""
        targets = list(self.channels() if channels is None else channels)
        failures: Dict[int, str] = {}

        taken: Dict[Tuple[str, int], str] = {}
        for channel in self.running():
            taken[channel.cfg.endpoint()] = channel.name

        for channel in targets:
            if channel.running:
                continue
            endpoint = channel.cfg.endpoint()
            if endpoint in taken:
                reason = "监听端口 %s:%d 已被通道「%s」占用" % (
                    channel.cfg.listen_host, channel.cfg.listen_port, taken[endpoint])
                failures[channel.id] = reason
                self._emit("log", level="ERR", channel=channel.id, text="启动失败：%s" % reason)
                continue
            if not channel.cfg.port:
                reason = "未选择串口"
                failures[channel.id] = reason
                self._emit("log", level="ERR", channel=channel.id, text="启动失败：%s" % reason)
                continue
            try:
                channel.start()
            except Exception as exc:      # 串口打不开 / 端口被别的程序占用
                failures[channel.id] = str(exc)
                continue
            taken[endpoint] = channel.name
        return failures

    def start_all(self) -> Dict[int, str]:
        return self.start_channels()

    def start(self, cid: int) -> None:
        channel = self.channel(cid)
        if channel is None:
            raise KeyError("通道不存在：%s" % cid)
        failures = self.start_channels([channel])
        if failures:
            raise RuntimeError(failures.get(cid, "启动失败"))

    def stop_channels(self, channels: Optional[Iterable[Channel]] = None) -> int:
        targets = list(self.channels() if channels is None else channels)
        count = 0
        for channel in targets:
            if not channel.running and not channel.serial_open:
                continue
            try:
                channel.stop()
                count += 1
            except Exception:
                pass
        return count

    def stop_all(self) -> int:
        return self.stop_channels()

    def stop(self, cid: int) -> bool:
        channel = self.channel(cid)
        if channel is None:
            return False
        return bool(self.stop_channels([channel]))

    def disconnect_all(self, cid: Optional[int] = None) -> int:
        targets = self.channels() if cid is None else [self.channel(cid)]
        count = 0
        for channel in targets:
            if channel is None:
                continue
            try:
                count += channel.disconnect_all()
            except Exception:
                pass
        return count

    # ------------------------------------------------------------------
    def totals(self) -> Dict[str, int]:
        channels = self.channels()
        return {
            "rx": sum(ch.rx_bytes for ch in channels),
            "tx": sum(ch.tx_bytes for ch in channels),
            "clients": sum(ch.client_count for ch in channels),
            "running": sum(1 for ch in channels if ch.running),
            "total": len(channels),
        }
