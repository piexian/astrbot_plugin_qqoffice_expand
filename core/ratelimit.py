"""表驱动频控：接口级令牌桶 + 主动消息三级配额。

限速值取自官方文档标注的「接口频率限制」；未知端点走保守默认。
主动消息配额：机器人每分钟 60/30 条（certified_bot 分档）、单关系每分钟
20 条、单关系每天 1000 条——分钟级等待放行，天级直接抛错。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from .errors import QQOfficeAPIError

__all__ = ["RateLimiter", "ENDPOINT_LIMITS", "DEFAULT_LIMIT"]

# endpoint_key → 每允许次数 / 窗口秒
ENDPOINT_LIMITS: dict[str, tuple[int, float]] = {
    "group.send": (100, 1.0),
    "c2c.send": (100, 1.0),
    "guild.send": (100, 1.0),
    "group.recall": (10, 1.0),
    "c2c.recall": (10, 1.0),
    "group.files": (50, 1.0),
    "c2c.files": (50, 1.0),
    "group.upload_prepare": (10, 1.0),
    "group.upload_part_finish": (10, 1.0),
    "interactions.ack": (50, 1.0),
    "group.info": (30, 60.0),
    "group.bot_state": (30, 60.0),
    "group.join_requests": (30, 60.0),
    "group.join_approve": (60, 60.0),
    "group.mute_get": (30, 60.0),
    "group.mute_set": (60, 60.0),
    "group.strategy": (60, 60.0),
    "menu.get": (30, 60.0),
    "menu.put": (5, 60.0),
    "panels.list": (30, 60.0),
    "panels.get": (30, 60.0),
    "panels.write": (10, 60.0),
    "panels.target": (60, 60.0),
    "guild.info": (30, 60.0),
}

DEFAULT_LIMIT = (25, 1.0)
"""未登记端点的保守默认值；官方单独限速的新端点由 429 自动等待兜底。"""

class _Bucket:
    __slots__ = ("count", "window", "events", "lock")

    def __init__(self, count: int, window: float):
        self.count = max(1, int(count))
        self.window = max(0.01, float(window))
        self.events: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> float:
        """等待直到获得配额，返回本次等待秒数。"""
        waited = 0.0
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0] >= self.window:
                    self.events.popleft()
                if len(self.events) < self.count:
                    self.events.append(now)
                    return waited
                sleep_for = self.window - (now - self.events[0]) + 0.01
            await asyncio.sleep(sleep_for)
            waited += sleep_for

class _ProactiveQuota:
    """主动消息三级配额（滑动窗口）。分钟级等待放行，天级直接抛错。"""

    def __init__(self, *, certified: bool, per_minute: int | None = None,
                 per_relation_minute: int = 20, per_relation_day: int = 1000):
        self.robot_minute = per_minute or (60 if certified else 30)
        self.relation_minute = per_relation_minute
        self.relation_day = per_relation_day
        self._robot: deque[float] = deque()
        self._rel_minute: dict[str, deque[float]] = {}
        self._rel_day: dict[str, deque[int]] = {}

    async def consume(self, target_openid: str, *, max_wait: float = 65.0) -> float:
        """占用一个配额；必要时异步等待（分钟级），天级超限抛错。"""
        waited = 0.0
        while True:
            now = time.monotonic()
            self._prune(now)
            if len(self._robot) >= self.robot_minute:
                sleep_for = max(0.05, 60.0 - (now - self._robot[0]))
            else:
                rel_min = self._rel_minute.setdefault(target_openid, deque())
                if len(rel_min) >= self.relation_minute:
                    sleep_for = max(0.05, 60.0 - (now - rel_min[0]))
                else:
                    rel_day = self._rel_day.setdefault(target_openid, deque())
                    if len(rel_day) >= self.relation_day:
                        raise QQOfficeAPIError(
                            40034100,
                            f"单关系主动消息已达每日上限（{self.relation_day} 条/天）",
                        )
                    self._robot.append(now)
                    rel_min.append(now)
                    rel_day.append(int(time.time()))
                    return waited
            if waited + sleep_for > max_wait:
                raise QQOfficeAPIError(
                    40034100,
                    "主动消息每分钟配额等待超时，请降低发送频率",
                )
            await asyncio.sleep(sleep_for)
            waited += sleep_for

    def _prune(self, now: float) -> None:
        while self._robot and now - self._robot[0] >= 60.0:
            self._robot.popleft()
        for dq in self._rel_minute.values():
            while dq and now - dq[0] >= 60.0:
                dq.popleft()
        today = int(time.time()) // 86400
        for dq in self._rel_day.values():
            while dq and dq[0] != today:
                dq.popleft()

class RateLimiter:
    """endpoint_key → 令牌桶；call() 通道按解析出的 endpoint_key 限速。"""

    def __init__(self, *, certified_bot: bool = False, limits: dict | None = None):
        self._buckets: dict[str, _Bucket] = {}
        self._limits = dict(ENDPOINT_LIMITS)
        if limits:
            self._limits.update(limits)
        self.proactive = _ProactiveQuota(certified=certified_bot)
        self.total_waits = 0.0
        """诊断：累计因频控等待的秒数。"""

    def register_limit(self, endpoint_key: str, count: int, window: float) -> None:
        """扩展位：官方新增端点限速时加一行表项。"""
        self._limits[endpoint_key] = (count, window)

    async def acquire(self, endpoint_key: str | None) -> float:
        count, window = self._limits.get(endpoint_key or "", DEFAULT_LIMIT)
        bucket = self._buckets.get(endpoint_key or "")
        if bucket is None or (bucket.count, bucket.window) != (count, window):
            bucket = _Bucket(count, window)
            self._buckets[endpoint_key or ""] = bucket
        waited = await bucket.acquire()
        self.total_waits += waited
        return waited

    async def consume_proactive(self, target_openid: str) -> float:
        return await self.proactive.consume(target_openid)

    def snapshot(self) -> dict:
        return {
            "registered_endpoints": len(self._limits),
            "active_buckets": len(self._buckets),
            "total_rate_wait_seconds": round(self.total_waits, 2),
            "proactive": {
                "robot_per_minute": self.proactive.robot_minute,
                "relation_per_minute": self.proactive.relation_minute,
                "relation_per_day": self.proactive.relation_day,
            },
        }
