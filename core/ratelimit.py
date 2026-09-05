"""表驱动频控：接口级令牌桶 + 主动消息三级配额。

限速值取自官方文档标注的「接口频率限制」；未知端点走保守默认。
主动消息配额按 C2C/群聊分别计数，单关系每分钟 20 条、每天 1000 条。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque

from .errors import QQOfficeAPIError

__all__ = ["RateLimiter", "ENDPOINT_LIMITS", "DEFAULT_LIMIT"]

# endpoint_key → 每允许次数 / 窗口秒
ENDPOINT_LIMITS: dict[str, tuple[int, float]] = {
    "group.send": (100, 1.0),
    "c2c.send": (100, 1.0),
    "guild.send": (5, 1.0),  # 按子频道分别计数
    "group.recall": (10, 1.0),
    "c2c.recall": (10, 1.0),
    "group.files": (50, 1.0),
    "c2c.files": (50, 1.0),
    "c2c.stream": (50, 1.0),
    "c2c.upload_prepare": (10, 1.0),
    "c2c.upload_part_finish": (10, 1.0),
    "group.upload_prepare": (10, 1.0),
    "group.upload_part_finish": (10, 1.0),
    "interactions.ack": (50, 1.0),
    "group.info": (30, 60.0),
    "group.bot_state": (30, 60.0),
    "group.join_requests": (30, 60.0),
    "group.join_approve": (60, 60.0),
    "group.members": (60, 60.0),
    "group.member_info": (30, 60.0),
    "group.remove_members": (30, 60.0),
    "group.blacklist": (30, 60.0),
    "group.set_blacklist": (60, 60.0),
    "group.mute_get": (30, 60.0),
    "group.mute_set": (60, 60.0),
    "group.strategy": (60, 60.0),
    "menu.get": (30, 60.0),
    "menu.put": (5, 60.0),
    "panels.list": (30, 60.0),
    "panels.get": (30, 60.0),
    "panels.write": (10, 60.0),
    "panels.target": (60, 60.0),
    "guild.info": (50, 1.0),
    "guild.channels": (50, 1.0),
    "guild.channel": (50, 1.0),
    "guild.list": (50, 1.0),
    "bot.me": (50, 1.0),
}

DEFAULT_LIMIT = (25, 1.0)
"""未登记端点的保守默认值；官方单独限速的新端点由 429 自动等待兜底。"""

class _Bucket:
    __slots__ = ("count", "window", "events", "lock", "_users")

    def __init__(self, count: int, window: float):
        self.count = max(1, int(count))
        self.window = max(0.01, float(window))
        self.events: deque[float] = deque()
        self.lock = asyncio.Lock()
        self._users = 0   # 在途等待者/使用者；>0 时不得被后台清理

    async def acquire(self) -> float:
        """等待直到获得配额，返回本次等待秒数。

        进入即登记使用计数（try/finally 归还）：限流等待期间本桶不会被
        后台清理回收重建，避免同窗口双请求放行。
        """
        self._users += 1
        try:
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
        finally:
            self._users -= 1

    def idle(self) -> bool:
        if self._users:
            return False   # 仍有等待者/在途使用：不回收
        if not self.events:
            return True
        return time.monotonic() - self.events[-1] >= self.window

class _ProactiveQuota:
    """主动消息三级配额（滑动窗口）。分钟级等待放行，天级直接抛错。

    每日关系用量用 (day, count) 计数；关系记录用可轮转有序映射保存，
    请求路径只裁剪当前目标，闲置键由 prune_idle 增量回收。
    """

    def __init__(self, *, certified: bool, scene: str = "group", per_minute: int | None = None,
                 per_relation_minute: int = 20, per_relation_day: int = 1000):
        self.robot_second = (10 if certified else 5) if scene == "c2c" else None
        self.robot_minute = per_minute or (None if scene == "c2c" and certified else (60 if certified else 30))
        self.relation_minute = per_relation_minute
        self.relation_day = per_relation_day
        self._robot: deque[float] = deque()
        self._robot_second: deque[float] = deque()
        self._rel_minute: OrderedDict[str, deque[float]] = OrderedDict()
        self._rel_day: OrderedDict[str, list[int]] = OrderedDict()   # [day, count]
        self._rotate = 0   # minute/day 交替指针（跨调用，预算公平）
        self._cursor = 0                                             # 轮转清理指针

    def _prune_target(self, target_openid: str, now: float, today: int) -> None:
        """请求热路径：只裁剪当前目标的时间窗口（摊还 O(1)）。"""
        dq = self._rel_minute.get(target_openid)
        if dq is not None:
            while dq and now - dq[0] >= 60.0:
                dq.popleft()
            if not dq:
                self._rel_minute.pop(target_openid, None)
        day_rec = self._rel_day.get(target_openid)
        if day_rec is not None and day_rec[0] != today:
            del self._rel_day[target_openid]

    def _prune_robot(self, now: float) -> None:
        while self._robot_second and now - self._robot_second[0] >= 1.0:
            self._robot_second.popleft()
        while self._robot and now - self._robot[0] >= 60.0:
            self._robot.popleft()

    async def consume(self, target_openid: str, *, max_wait: float = 65.0) -> float:
        """占用一个配额；必要时异步等待（分钟级），天级超限抛错。"""
        waited = 0.0
        while True:
            now = time.monotonic()
            today = int(time.time()) // 86400
            self._prune_robot(now)
            self._prune_target(target_openid, now, today)
            if self.robot_second and len(self._robot_second) >= self.robot_second:
                sleep_for = max(0.01, 1.0 - (now - self._robot_second[0]))
            elif self.robot_minute and len(self._robot) >= self.robot_minute:
                sleep_for = max(0.05, 60.0 - (now - self._robot[0]))
            else:
                rel_min = self._rel_minute.setdefault(target_openid, deque())
                if len(rel_min) >= self.relation_minute:
                    sleep_for = max(0.05, 60.0 - (now - rel_min[0]))
                else:
                    day_rec = self._rel_day.get(target_openid)
                    if day_rec is None:
                        day_rec = self._rel_day[target_openid] = [today, 0]
                    if day_rec[0] == today and day_rec[1] >= self.relation_day:
                        raise QQOfficeAPIError(
                            40034100,
                            f"单关系主动消息已达每日上限（{self.relation_day} 条/天）",
                        )
                    self._robot.append(now)
                    self._robot_second.append(now)
                    rel_min.append(now)
                    day_rec[1] += 1
                    self._rel_minute.move_to_end(target_openid)
                    self._rel_day.move_to_end(target_openid)
                    return waited
            if waited + sleep_for > max_wait:
                raise QQOfficeAPIError(
                    40034100,
                    "主动消息每分钟配额等待超时，请降低发送频率",
                )
            await asyncio.sleep(sleep_for)
            waited += sleep_for

    def prune_idle(self, *, limit: int = 64) -> int:
        """后台增量回收：minute/day 两张有序表跨调用交替，共享检查预算。

        每次从交替指针指向的表 pop 队首检查（消费一个名额）：过期则回收
        /清窗口，未过期重插尾部轮转。两表各清各的、指针跨调用保持，
        不会永远先吃某一表（有活跃 minute 目标时 day 仍能轮到）。
        顺带裁剪机器人级队列。检查数（含未删除）≤ limit。
        """
        self._prune_robot(time.monotonic())
        removed = 0
        today = int(time.time()) // 86400
        now = time.monotonic()
        checked = 0
        use_minute = bool(self._rotate & 1)
        while checked < limit:
            table = self._rel_minute if use_minute else self._rel_day
            if not table:
                if not (self._rel_minute or self._rel_day):
                    break
                use_minute = not use_minute
                self._rotate += 1
                continue
            checked += 1
            self._rotate += 1
            if use_minute:
                key, dq = self._rel_minute.popitem(last=False)
                day_rec = self._rel_day.get(key)
                minute_expired = not dq or now - dq[-1] >= 60.0
                if day_rec is not None and day_rec[0] != today and minute_expired:
                    self._rel_day.pop(key, None)
                    removed += 1          # 整体回收，不重插
                elif minute_expired:
                    pass                   # 只清窗口，保留日计数
                else:
                    self._rel_minute[key] = dq   # 未过期：重插尾部（轮转）
                    self._rel_minute.move_to_end(key)
            else:
                key, day_rec = self._rel_day.popitem(last=False)
                if day_rec[0] != today:
                    self._rel_minute.pop(key, None)
                    removed += 1          # 旧日计数：整体回收
                else:
                    self._rel_day[key] = day_rec   # 当日：重插尾部（轮转）
                    self._rel_day.move_to_end(key)
            use_minute = not use_minute
        return removed

    def idle(self) -> bool:
        """O(1) 结构空判定：有界 prune 后仍非空则保留，交后续轮次清完。

        不遍历关系表（避免隐藏 O(R)）；当日额度未过期时表非空，状态不回收。
        """
        return (not self._robot and not self._robot_second
                and not self._rel_minute and not self._rel_day)

class RateLimiter:
    """endpoint_key → 令牌桶；call() 通道按解析出的 endpoint_key 限速（每身份一套）。"""

    def __init__(self, *, certified_bot: bool = False, limits: dict | None = None):
        self._buckets: "OrderedDict[str | tuple[str, str], _Bucket]" = OrderedDict()
        self._limits = dict(ENDPOINT_LIMITS)
        if limits:
            self._limits.update(limits)
        self.proactive = _ProactiveQuota(certified=certified_bot)
        self.c2c_proactive = _ProactiveQuota(certified=certified_bot, scene="c2c")
        self._budget_cursor = 0   # 余数预算的轮转起点
        self.total_waits = 0.0
        """诊断：累计因频控等待的秒数。"""

    def register_limit(self, endpoint_key: str, count: int, window: float) -> None:
        """扩展位：官方新增端点限速时加一行表项。"""
        self._limits[endpoint_key] = (count, window)

    async def acquire(self, endpoint_key: str | None, *, target: str | None = None) -> float:
        count, window = self._limits.get(endpoint_key or "", DEFAULT_LIMIT)
        key = (endpoint_key, target) if endpoint_key == "guild.send" and target else endpoint_key or ""
        bucket = self._buckets.get(key)
        if bucket is None or (bucket.count, bucket.window) != (count, window):
            bucket = _Bucket(count, window)
            self._buckets[key] = bucket
        waited = await bucket.acquire()
        self.total_waits += waited
        return waited

    async def consume_proactive(self, target_openid: str, *, scene: str = "group") -> float:
        quota = self.c2c_proactive if scene == "c2c" else self.proactive
        return await quota.consume(target_openid)

    def idle(self) -> bool:
        return (not self._buckets
                and self.proactive.idle() and self.c2c_proactive.idle())

    def prune_idle(self, *, limit: int = 64) -> int:
        """增量回收：总检查预算 limit 在桶与两个配额表间分配（总和≤limit）。

        基础预算 limit//3，余数 limit%3 按跨调用 cursor 依次加 1（避免
        limit=1 长期饿死某组）；预算 0 的组本轮跳过。消费的是检查名额
        （含未删除项）；桶轮转未过期重插尾部。有等待者/在途使用的桶不
        回收（见 _Bucket.idle）。
        """
        removed = 0
        base, extra = divmod(max(0, limit), 3)
        budgets = [base, base, base]
        for i in range(extra):
            budgets[(self._budget_cursor + i) % 3] += 1
        self._budget_cursor = (self._budget_cursor + extra) % 3

        # 桶
        checked = 0
        while checked < budgets[0]:
            if not self._buckets:
                break
            key, bucket = self._buckets.popitem(last=False)
            checked += 1
            if bucket.idle():
                removed += 1            # 闲置桶：不重插
                continue
            self._buckets[key] = bucket  # 活跃桶：重插尾部（轮转）
            self._buckets.move_to_end(key)
        # 两个配额表
        if budgets[1] > 0:
            removed += self.proactive.prune_idle(limit=budgets[1])
        if budgets[2] > 0:
            removed += self.c2c_proactive.prune_idle(limit=budgets[2])
        return removed

    def snapshot(self) -> dict:
        return {
            "registered_endpoints": len(self._limits),
            "active_buckets": len(self._buckets),
            "total_rate_wait_seconds": round(self.total_waits, 2),
            "c2c_proactive": {
                "robot_per_second": self.c2c_proactive.robot_second,
                "robot_per_minute": self.c2c_proactive.robot_minute,
            },
            "proactive": {
                "robot_per_minute": self.proactive.robot_minute,
                "relation_per_minute": self.proactive.relation_minute,
                "relation_per_day": self.proactive.relation_day,
            },
        }
