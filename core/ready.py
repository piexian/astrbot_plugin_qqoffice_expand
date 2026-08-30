"""就绪信号与依赖方等待原语。

依赖方**不应**在 initialize() 里阻塞等待本插件：AstrBot 的插件加载循环是
串行 await initialize() 的，阻塞会卡死后续插件（包括本插件）加载。
推荐的接入方式是框架广播 `@filter.on_plugin_loaded()`（本插件 initialize
成功后框架即广播，svc.ready 同步已置位）；本模块的轮询原语只供后台任务
场景与离线测试使用。
"""

from __future__ import annotations

import asyncio
import time

__all__ = ["ReadySignal", "wait_for_star"]


class ReadySignal:
    def __init__(self):
        self._event = asyncio.Event()
        self._created_at = time.monotonic()

    def set(self) -> None:
        self._event.set()

    @property
    def is_ready(self) -> bool:
        return self._event.is_set()

    @property
    def waited_seconds(self) -> float:
        return round(time.monotonic() - self._created_at, 2)

    async def wait(self, timeout: float | None = None) -> bool:
        """等待就绪；超时返回 False。"""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True


async def wait_for_star(get_meta, *, timeout: float = 60.0, interval: float = 0.5):
    """轮询 get_meta() 直到目标插件就绪（activated + star_cls.ready），超时返回 None。

    覆盖三种状态：未加载（meta 为 None）、已实例化但 initialize 未完成/失败
    （ready=False）、就绪。非本插件的 star_cls 没有 ready 标志，视为未就绪。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            meta = get_meta()
        except Exception:
            meta = None
        if meta is not None and getattr(meta, "activated", False):
            star = getattr(meta, "star_cls", None)
            if star is not None and getattr(star, "ready", False):
                return star
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(interval)
