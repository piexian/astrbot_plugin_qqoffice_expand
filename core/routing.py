"""N 实例路由核心：本体索引只读桥接 + 身份/代次/绑定视图算法。

设计依据 docs/MULTI_INSTANCE_DESIGN.md 第 2、3、5 节：
- platform_id 用 AstrBot 加载后的配置 ID（本体可能把 `:`/`!` 改成 `_`，直接用加载后的值）；
- robot_key = (appid, environment)，同 AppID 同环境的多实例共享机器人状态；
- 运行代次仅在本体适配器对象或其 botpy client 对象变化时递增；
- 请求前读取本体 `_inst_map`（平均 O(1)），不依赖轮询发现禁用/删除；
- Webhook 会在同一个 client 内替换 api/http，HTTP 每次动态取得，不缓存。

私有 `_inst_map` 的读取集中在本模块 PlatformIndex，只读不写；
依赖变化时通过契约测试报错，不退回可能过期的本地缓存。
"""

from __future__ import annotations

import heapq
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .auth import ADAPTER_NAMES
from .errors import (
    InstanceIdentityChanged,
    InstanceUnavailable,
    StaleSourceEvent,
    TransportNotReady,
)
from .ratelimit import RateLimiter

__all__ = [
    "RobotKey",
    "EventSource",
    "BoundSource",
    "OperationContext",
    "PassiveWindows",
    "AckTracker",
    "RobotState",
    "RobotStates",
    "PlatformIndex",
    "RouteRecord",
    "RouteDiff",
    "RouteCore",
    "current_http",
    "client_is_closing",
]

PRODUCTION = "production"
SANDBOX = "sandbox"


@dataclass(frozen=True)
class RobotKey:
    """机器人身份：官方 AppID + 传输环境（production/sandbox）。

    同 key 的多实例共享频控、被动回复计数、ACK 去重与缓存命名空间；
    生产环境的新旧域名别名不构成不同身份。
    """

    appid: str
    environment: str = PRODUCTION

    @classmethod
    def from_adapter(cls, inst: Any, mode: str) -> "RobotKey":
        """从当前实际适配器实例取身份。

        - appid 只信 inst.appid（本体两适配器构造时固化）；config 字段可能已
          被修改但尚未重建客户端，不代表实际请求身份。
        - 环境按本体实际支持的传输：WS 的 botClient 不传 is_sandbox
          （botpy Client 默认 False，恒为 production，忽略 config）；
          Webhook helper 把 config['is_sandbox'] 传给自建 BotHttp
          （qo_webhook_server.py），helper 未初始化前的默认 HTTP 不作依据。
        """
        appid = str(getattr(inst, "appid", "") or "")
        if mode == "webhook" and bool(_adapter_config(inst).get("is_sandbox", False)):
            environment = SANDBOX
        else:
            environment = PRODUCTION
        return cls(appid=appid, environment=environment)

    def prefix(self) -> str:
        """缓存/文件命名空间前缀。"""
        return f"{self.appid}@{self.environment}"

    def __str__(self) -> str:  # pragma: no cover - 日志可读性
        return self.prefix()


@dataclass(frozen=True)
class EventSource:
    """扩展事件的不可变来源：挂载处理器时从实际适配器记录，不从 payload 猜测。"""

    platform_id: str
    robot_key: RobotKey
    generation: int


@dataclass(frozen=True)
class BoundSource:
    """一次调用/视图绑定的来源。

    - instance(id) 视图：platform_id + robot_key，无来源约束 → 长期视图，
      跟随同机器人重载；
    - for_event 原生事件：另绑定 source_client（event.bot）；
    - for_event 扩展事件：另绑定 source_generation。
    """

    platform_id: str
    robot_key: RobotKey
    source_client: Any = None
    source_generation: int | None = None

    @classmethod
    def for_instance(cls, platform_id: str, robot_key: RobotKey) -> "BoundSource":
        return cls(platform_id=platform_id, robot_key=robot_key)

    @classmethod
    def from_event_source(cls, source: EventSource) -> "BoundSource":
        return cls(platform_id=source.platform_id, robot_key=source.robot_key,
                   source_generation=source.generation)


@dataclass(frozen=True)
class OperationContext:
    """一次业务操作固定的来源：上传→发送、限流等待后、重试前都用它核验。

    同一操作不允许在 await 之后悄悄换到新代次或另一机器人。
    """

    platform_id: str
    robot_key: RobotKey
    generation: int


class PassiveWindows:
    """robot_key 级被动回复窗口（群 5min/5 次、C2C 60min/4 次）。

    预留/确认语义，全程无 await：先 reserve 占用一次额度再发起发送；
    发送前的确定性失败 release 释放；发送结果未知按已消耗处理（不释放），
    避免并发通过 check 后都发出。窗口过期的 msg_id 不重置计数（官方拒绝
    过期 msg_id，重置会让请求白打一次 40034005）。

    计数按 scene+target+msg_id 隔离；每 scene 一条按到期时间排序的清理队列
    （窗口长度不同不互相阻塞），队列项带版本号，记录状态变化后旧项作废。
    """

    LIMITS = {"group": (300.0, 5), "c2c": (3600.0, 4)}

    def __init__(self):
        # (scene, target, msg_id) -> [first_ts, count, last_ts, version]；保留插入序。
        self._records: OrderedDict[tuple, list] = OrderedDict()
        self._queues: dict[str, list[tuple[float, int, tuple]]] = {}  # scene -> 堆
        self._rotate = 0   # 跨调用 scene 轮转指针（预算公平）
        self._version = 0

    @staticmethod
    def _key(scene: str, target: str | None, msg_id: str) -> tuple:
        return (scene, str(target or ""), str(msg_id))

    def check(self, scene: str, target: str | None, msg_id: str) -> tuple[bool, str]:
        """只读检查：是否仍可被动回复及原因。"""
        window, max_count = self.LIMITS.get(scene, (0.0, 0))
        if not window:
            return True, ""
        rec = self._records.get(self._key(scene, target, msg_id))
        if rec is None:
            return True, ""
        first_ts, count, last_ts, _ = rec
        now = time.monotonic()
        if now - first_ts > window:
            return False, "窗口已过期"
        if count >= max_count:
            return False, f"已达被动回复上限（{max_count} 次）"
        return True, ""

    def reserve(self, scene: str, target: str | None, msg_id: str) -> tuple[bool, str]:
        """预留一次被动回复额度；失败时不占用，返回 (False, 原因)。"""
        window, max_count = self.LIMITS.get(scene, (0.0, 0))
        if not window or not msg_id:
            return True, ""
        key = self._key(scene, target, msg_id)
        now = time.monotonic()
        rec = self._records.get(key)
        if rec is not None and now - rec[0] > window:
            return False, "窗口已过期"
        if rec is not None and rec[1] >= max_count:
            return False, f"已达被动回复上限（{max_count} 次）"
        if rec is None:
            self._version += 1
            self._records[key] = [now, 1, now, self._version]
            heapq.heappush(self._queues.setdefault(scene, []), (now + window, self._version, key))
        else:
            rec[1] += 1
            rec[2] = now
            self._records.move_to_end(key)
        return True, ""

    def release(self, scene: str, target: str | None, msg_id: str) -> None:
        """发送前确定性失败：释放一次预留（不回收已过期的清理键）。"""
        rec = self._records.get(self._key(scene, target, msg_id))
        if rec is not None and rec[1] > 0:
            rec[1] -= 1
            rec[2] = time.monotonic()

    # confirm 为预留的默认归宿（结果未知按已消耗），提供显式接口便于语义对齐。
    def confirm(self, scene: str, target: str | None, msg_id: str) -> None:
        return None

    def prune_expired(self, now: float | None = None, *, limit: int = 64) -> int:
        """增量回收：全部 scene 共享一份检查预算 limit，跨调用轮转 scene。

        从轮转指针指向的 scene 堆顶检查：到期则弹出（消费一个名额）并按
        版本删除记录，未到期则该 scene 移出本轮；预算用尽或全部完成即停。
        指针跨调用保持，某 scene 不独占预算。堆操作 O(log R)。
        """
        removed = 0
        now = time.monotonic() if now is None else now
        checked = 0
        scenes = [s for s, q in self._queues.items() if q]
        if not scenes:
            return 0
        idx = self._rotate % len(scenes)
        self._rotate = (self._rotate + limit) % max(1, len(self._queues) or 1)
        while checked < limit and scenes:
            scene = scenes[idx % len(scenes)]
            queue = self._queues[scene]
            expire_at, version, key = queue[0]
            if expire_at >= now:
                scenes.pop(idx % len(scenes))   # 本 scene 已清完，本轮不再看
                idx = idx % max(1, len(scenes)) if scenes else 0
                continue
            heapq.heappop(queue)
            checked += 1
            rec = self._records.get(key)
            if rec is not None and rec[3] == version:
                del self._records[key]
                removed += 1
            if not queue:
                scenes.pop(idx % len(scenes))
                self._queues.pop(scene, None)
                idx = idx % max(1, len(scenes)) if scenes else 0
            else:
                idx += 1   # 同 scene 连续到期也轮转让其他 scene 有机会
        for scene in list(self._queues):
            if not self._queues[scene]:
                del self._queues[scene]
        return removed

    def stats(self) -> dict:
        return {"tracked_msg_ids": len(self._records)}


class AckTracker:
    """robot_key 级互动 ACK 去重：键为 interaction_id。

    状态机：pending → succeeded（确认成功）/ settled（结果未知，按已应答处理，
    不再重答）；发送前确定性失败 release 回收，允许后续重试应答。

    清理采用版本化堆队列：记录每次状态变化都递增版本并重新入队新的到期项；
    弹出队列时只删除版本一致的记录，旧队列项直接丢弃，所以 succeed 等更新
    不会让记录卡在早于真实状态的 deadline 上，也不会永久残留。
    """

    _TTL = 600.0  # 超过官方 3 秒时限两个数量级的记忆窗口，足够挡重发

    def __init__(self):
        self._records: OrderedDict[str, list] = OrderedDict()  # iid -> [state, ts, version]
        self._queue: list[tuple[float, int, str]] = []         # (到期时刻, 版本, iid)，堆序
        self._version = 0

    def _requeue(self, iid: str, ts: float) -> None:
        self._version += 1
        self._records[iid][2] = self._version
        heapq.heappush(self._queue, (ts + self._TTL, self._version, iid))

    def try_reserve(self, interaction_id: str) -> str:
        """预留应答资格："new" 可应答；"duplicate" 已应答过（含 pending/settled/succeeded）。"""
        if not interaction_id:
            return "duplicate"
        now = time.monotonic()
        rec = self._records.get(interaction_id)
        if rec is not None and now - rec[1] <= self._TTL:
            self._records.move_to_end(interaction_id)
            return "duplicate"
        if rec is not None:
            del self._records[interaction_id]  # 过期重建：新记录配新队列项
        self._records[interaction_id] = ["pending", now, 0]
        self._requeue(interaction_id, now)
        return "new"

    def succeed(self, interaction_id: str) -> None:
        rec = self._records.get(interaction_id)
        if rec is not None:
            rec[0], rec[1] = "succeeded", time.monotonic()
            self._requeue(interaction_id, rec[1])

    def settle_unknown(self, interaction_id: str) -> None:
        """网络结果未知：按已应答处理，不能无条件清除后重复应答。"""
        rec = self._records.get(interaction_id)
        if rec is not None:
            rec[0], rec[1] = "settled", time.monotonic()
            self._requeue(interaction_id, rec[1])

    def release(self, interaction_id: str) -> None:
        """发送前确定性失败：回收 pending，允许下次重新应答；队列旧项由 prune 靠版本丢弃。"""
        rec = self._records.get(interaction_id)
        if rec is not None and rec[0] == "pending":
            del self._records[interaction_id]

    def state_of(self, interaction_id: str) -> str | None:
        rec = self._records.get(interaction_id)
        return rec[0] if rec is not None else None

    def prune_expired(self, now: float | None = None, *, limit: int = 64) -> int:
        """增量回收：每轮最多检查 limit 个队列项（含失效项），返回实际删除条数。"""
        removed = 0
        checked = 0
        now = time.monotonic() if now is None else now
        while self._queue and checked < limit:
            expire_at, version, iid = self._queue[0]
            if expire_at >= now:   # 恰好到点仍在 TTL 窗口内（与 try_reserve 一致）
                break
            heapq.heappop(self._queue)
            checked += 1
            rec = self._records.get(iid)
            if rec is not None and rec[2] == version:
                del self._records[iid]
                removed += 1
        return removed

    def stats(self) -> dict:
        states: dict[str, int] = {}
        for rec in self._records.values():
            states[rec[0]] = states.get(rec[0], 0) + 1
        return {"total": len(self._records), **states}


class RobotState:
    """一个机器人身份（AppID+环境）的共享状态。

    同身份多实例引用同一对象；实例禁用/替换/重载不清除未过期用量，
    只有自然过期或显式 prune 后空闲才可回收。rate 的限额配置由创建方
    注入（同身份只读一份配置；全局值仅作默认）。
    """

    def __init__(self, robot_key: RobotKey, *, rate_limiter: RateLimiter | None = None):
        self.key = robot_key
        self.rate = rate_limiter if rate_limiter is not None else RateLimiter()
        self.windows = PassiveWindows()
        self.acks = AckTracker()
        self.created_at = time.time()
        self.last_used = time.monotonic()
        self.recent_errors: deque[dict] = deque(maxlen=10)   # 诊断环形日志
        self.calls_total = 0
        self._refcount = 0            # 在途操作归属：>0 时不回收

    def retain(self) -> None:
        """在途操作登记归属（限流等待/重试期间状态不被回收重建）。"""
        self._refcount += 1

    def release(self) -> None:
        if self._refcount > 0:
            self._refcount -= 1

    @property
    def in_use(self) -> bool:
        return self._refcount > 0

    def touch(self) -> None:
        self.last_used = time.monotonic()
        self.calls_total += 1

    def _log_degrade(self, svc, scene: str, target: str | None, msg_id: str) -> None:
        from astrbot.api import logger

        logger.warning(
            f"[qqoffice_expand] {self.key.prefix()} {scene} {target} "
            f"被动窗口失效（{msg_id}），已降级主动消息"
        )

    def idle(self) -> bool:
        """无任何未过期配额/窗口/ACK 记录时才可回收（LRU 不适用于它们）。"""
        return (
            not self.windows.stats()["tracked_msg_ids"]
            and not self.acks.stats()["total"]
            and self.rate.idle()
        )

    def empty(self) -> bool:
        return self.idle()

    def snapshot(self) -> dict:
        return {
            "robot_key": self.key.prefix(),
            "windows": self.windows.stats(),
            "acks": self.acks.stats(),
            "rate": self.rate.snapshot(),
        }


class RobotStates:
    """robot_key -> RobotState 注册表；与路由生命周期解耦。

    实例全部消失也不清状态（防重新添加同 AppID 后获得第二份配额），
    由后台协调任务对空闲且过期的状态增量回收；get_or_create 无 await。
    """

    def __init__(self, factory: Callable[[RobotKey], RobotState] | None = None):
        self._states: dict[RobotKey, RobotState] = {}
        self._factory = factory

    def get_or_create(self, robot_key: RobotKey) -> RobotState:
        state = self._states.get(robot_key)
        if state is None:
            state = self._factory(robot_key) if self._factory else RobotState(robot_key)
            self._states[robot_key] = state
        return state

    def get(self, robot_key: RobotKey) -> RobotState | None:
        return self._states.get(robot_key)

    def keys(self) -> list[RobotKey]:
        return list(self._states)

    def items(self) -> list[tuple[RobotKey, RobotState]]:
        return list(self._states.items())

    def discard(self, robot_key: RobotKey) -> None:
        state = self._states.get(robot_key)
        if state is not None and not state.in_use:
            self._states.pop(robot_key, None)

    def __len__(self) -> int:
        return len(self._states)

    def __iter__(self):
        return iter(list(self._states.values()))


# ---------------- 本体索引只读桥接 ----------------


class PlatformIndex:
    """集中只读访问 AstrBot PlatformManager 的私有 _inst_map。

    只读该映射，不写入、不复制、不包装管理器；缺少 _inst_map 时明确报
    不支持，不退回可能过期的 platform_insts 扫描（本体契约变化应被
    契约测试捕捉，而非静默走错路径）。每次查询平均 O(1)。
    """

    def __init__(self, platform_manager: Any):
        self._manager = platform_manager

    def _inst_map(self) -> dict:
        inst_map = getattr(self._manager, "_inst_map", None)
        if not isinstance(inst_map, dict):
            from .errors import QQOfficeNotSupported

            raise QQOfficeNotSupported(
                "AstrBot PlatformManager 缺少 _inst_map 映射，多实例路由无法工作；"
                "请核对 AstrBot 版本（需 4.28+ 的 platform manager 实现）"
            )
        return inst_map

    def entry(self, platform_id: str) -> dict | None:
        """本体当前实例条目（{inst, client_id}）；不存在返回 None。"""
        return self._inst_map().get(platform_id)

    def qq_entries(self) -> dict[str, dict]:
        """全部 QQ 官方适配器条目（协调任务差异刷新用，非请求热路径）。"""
        return {
            pid: entry
            for pid, entry in self._inst_map().items()
            if _is_qq_adapter(entry.get("inst") if isinstance(entry, dict) else None)
        }


def _is_qq_adapter(inst: Any) -> bool:
    if inst is None:
        return False
    try:
        return getattr(inst.meta(), "name", None) in ADAPTER_NAMES
    except Exception:
        return False


def _adapter_config(inst: Any) -> dict:
    cfg = getattr(inst, "config", None)
    return cfg if isinstance(cfg, dict) else {}


def _client_of(inst: Any) -> Any:
    try:
        client = inst.get_client()
    except Exception:
        client = None
    return client if client is not None else getattr(inst, "client", None)


def _adapter_meta(inst: Any) -> tuple[str, str]:
    """(adapter_name, mode)；非 QQ 适配器返回 ("", "")。"""
    try:
        name = getattr(inst.meta(), "name", "") or ""
    except Exception:
        return "", ""
    if name not in ADAPTER_NAMES:
        return "", ""
    return name, ("webhook" if name.endswith("_webhook") else "ws")


def current_http(client: Any) -> Any:
    """动态取得当前 botpy BotHttp；Webhook 会替换 client.api，不能缓存。"""
    api = getattr(client, "api", None)
    return getattr(api, "_http", None) if api is not None else None


def client_is_closing(client: Any) -> bool:
    """botpy client 正在关闭或已关闭。

    除 botpy 的 is_closed() 外，还识别 AstrBot WS 适配器 botClient 公开的
    is_shutting_down（本体 botClient 为 property，shutdown 前置为 True，
    先于 is_closed），避免本体关闭窗口内仍放行新请求。
    """
    try:
        if bool(client.is_closed()):
            return True
    except Exception:
        pass
    shutting_down = getattr(client, "is_shutting_down", None)
    if shutting_down is None:
        return False
    if callable(shutting_down):
        try:
            return bool(shutting_down())
        except Exception:
            return False
    return bool(shutting_down)


# ---------------- 路由核心 ----------------


@dataclass
class RouteRecord:
    """一个配置 ID 的当前运行实例视图。

    generation 仅在适配器或 client 对象变化时递增；Webhook 同 client 内
    替换 api/http 不构成新代次。
    """

    platform_id: str
    adapter_name: str
    mode: str
    inst: Any
    client: Any
    robot_key: RobotKey
    generation: int

    def snapshot(self) -> dict:
        from .routing import client_is_closing, current_http

        http = current_http(self.client) if self.client is not None else None
        return {
            "platform_id": self.platform_id,
            "adapter": self.adapter_name,
            "mode": self.mode,
            "appid": self.robot_key.appid,
            "environment": self.robot_key.environment,
            "generation": self.generation,
            "transport_ready": bool(
                http is not None and getattr(http, "_token", None) is not None
            ),
            "closing": client_is_closing(self.client) if self.client is not None else True,
        }


@dataclass
class RouteChange:
    """路由变更事件：请求侧发现的本体变化转交协调任务消费。"""

    kind: str                      # "added" / "replaced" / "removed"
    old: RouteRecord | None
    new: RouteRecord | None


@dataclass
class RouteDiff:
    """一次全体差异刷新的结果；供协调任务挂载/恢复补丁。

    含请求侧先行发现的变化（refresh_all 开头一次性消费变更日志），
    后续无变更多次巡检不会重复报告。
    """

    added: list[RouteRecord] = field(default_factory=list)
    removed: list[RouteRecord] = field(default_factory=list)
    replaced: list[tuple[RouteRecord, RouteRecord]] = field(default_factory=list)
    unchanged: list[RouteRecord] = field(default_factory=list)


class RouteCore:
    """platform_id -> RouteRecord 索引；请求侧同步刷新，巡检侧差异收敛。"""

    def __init__(self, platform_manager: Any, states: RobotStates | None = None,
                 logger=None):
        self.index = PlatformIndex(platform_manager)
        self.states = states if states is not None else RobotStates()
        self.logger = logger
        self._routes: dict[str, RouteRecord] = {}
        self._generation = 0
        self._changes: list[RouteChange] = []   # 请求侧发现的变化，协调任务一次性消费
        self.view_factory: Callable[[BoundSource], Any] | None = None
        self._active = True   # 插件生命周期：terminate 后拒绝一切新请求

    def deactivate(self) -> None:
        """插件卸载：入口与所有 await 后核验立即失败（不触碰本体资源）。"""
        self._active = False

    def _check_active(self) -> None:
        if not self._active:
            raise StaleSourceEvent(
                "qqoffice_expand 插件已卸载，本视图/操作已失效（需重新获取 svc 并创建视图）"
            )

    @property
    def routes(self) -> dict[str, RouteRecord]:
        return self._routes

    def route_of(self, platform_id: str) -> RouteRecord | None:
        return self._routes.get(platform_id)

    def state_for(self, robot_key: RobotKey) -> RobotState:
        return self.states.get_or_create(robot_key)

    def operation(self, route: RouteRecord) -> OperationContext:
        """从当前路由建立操作上下文（请求开始时调用一次）。"""
        return OperationContext(platform_id=route.platform_id,
                                robot_key=route.robot_key, generation=route.generation)

    def source_of(self, route: RouteRecord) -> EventSource:
        """从当前路由建立扩展事件来源（挂载处理器时调用）。"""
        return EventSource(platform_id=route.platform_id,
                           robot_key=route.robot_key, generation=route.generation)

    # -- 请求热路径：同步、无 await --

    def ensure_current_route(self, platform_id: str) -> RouteRecord:
        """取得该配置 ID 的当前路由；本体索引变化时同步收敛本地记录。

        失败时本地与本体一致后才报错，不会拿旧记录继续服务。
        """
        self._check_active()
        entry = self.index.entry(platform_id)
        if entry is None:
            self._remove_route(platform_id, reason="本体索引已无该实例")
            raise InstanceUnavailable(f"平台实例 {platform_id!r} 不在本体运行索引中（禁用/删除或尚未加载）")
        inst = entry.get("inst") if isinstance(entry, dict) else None
        name, mode = _adapter_meta(inst)
        if inst is None or not name:
            self._remove_route(platform_id, reason="当前实例不是 QQ 官方适配器")
            raise InstanceUnavailable(f"平台实例 {platform_id!r} 不是 QQ 官方适配器，无法路由扩展请求")
        client = _client_of(inst)
        robot_key = RobotKey.from_adapter(inst, mode)
        if not robot_key.appid:
            self._remove_route(platform_id, reason="适配器缺少 appid")
            raise InstanceUnavailable(f"平台实例 {platform_id!r} 的适配器缺少 appid，无法确定机器人身份")
        route = self._routes.get(platform_id)
        if route is not None and route.inst is inst and route.client is client:
            return route
        return self._create_route(platform_id, inst, client, name, mode)

    def resolve_current(self, bound: BoundSource) -> RouteRecord:
        """仅校验来源与身份（不要求传输就绪），返回当前路由。

        用于视图创建时固定身份、事件回调来源核验等不需要发请求的场景。
        """
        self._check_active()
        route = self.ensure_current_route(bound.platform_id)
        if bound.robot_key is not None and route.robot_key != bound.robot_key:
            raise InstanceIdentityChanged(
                f"平台实例 {bound.platform_id!r} 已从 {bound.robot_key.prefix()} "
                f"改绑到 {route.robot_key.prefix()}，旧视图不能转给新机器人"
            )
        if bound.source_client is not None and bound.source_client is not route.client:
            raise StaleSourceEvent(
                f"事件来源 client 已不是 {bound.platform_id!r} 的当前实例（适配器已重载/替换）"
            )
        if (bound.source_generation is not None
                and bound.source_generation != route.generation):
            raise StaleSourceEvent(
                f"事件来源代次 {bound.source_generation} 已失效，"
                f"{bound.platform_id!r} 当前为代次 {route.generation}"
            )
        return route

    def resolve(self, bound: BoundSource) -> tuple[RouteRecord, Any]:
        """绑定视图 -> (当前路由, 当前 botpy HTTP)。全部检查无 await。"""
        route = self.ensure_current_route(bound.platform_id)
        if bound.robot_key is not None and route.robot_key != bound.robot_key:
            raise InstanceIdentityChanged(
                f"平台实例 {bound.platform_id!r} 已从 {bound.robot_key.prefix()} "
                f"改绑到 {route.robot_key.prefix()}，旧视图不能转给新机器人"
            )
        if bound.source_client is not None and bound.source_client is not route.client:
            raise StaleSourceEvent(
                f"事件来源 client 已不是 {bound.platform_id!r} 的当前实例（适配器已重载/替换）"
            )
        if (bound.source_generation is not None
                and bound.source_generation != route.generation):
            raise StaleSourceEvent(
                f"事件来源代次 {bound.source_generation} 已失效，"
                f"{bound.platform_id!r} 当前为代次 {route.generation}"
            )
        if route.client is None:
            raise TransportNotReady(f"平台实例 {bound.platform_id!r} 尚未提供 botpy client")
        if client_is_closing(route.client):
            raise InstanceUnavailable(f"平台实例 {bound.platform_id!r} 的 botpy client 正在关闭")
        http = current_http(route.client)
        if http is None:
            raise TransportNotReady(
                f"平台实例 {bound.platform_id!r} 尚未挂载 botpy API/HTTP（登录未完成或 Webhook 初始化中）"
            )
        if getattr(http, "_token", None) is None:
            raise TransportNotReady(f"平台实例 {bound.platform_id!r} 的 botpy HTTP 尚无 token（未就绪）")
        return route, http

    def check_context(self, ctx: OperationContext) -> Any:
        """限流等待后/重试前核验：操作仍属同一本体实例与代次，返回当前 HTTP。

        任一变化即失败，不跨机器人/代次重放写操作；成功时动态取回
        当前 HTTP（Webhook 同 client 内替换后是新对象）。
        """
        self._check_active()
        route = self.ensure_current_route(ctx.platform_id)
        if route.robot_key != ctx.robot_key:
            raise InstanceIdentityChanged(
                f"操作 {ctx.platform_id!r} 的机器人身份已变化"
                f"（{ctx.robot_key.prefix()} -> {route.robot_key.prefix()}），不跨机器人重放"
            )
        if route.generation != ctx.generation:
            raise StaleSourceEvent(
                f"操作 {ctx.platform_id!r} 所在运行代次 {ctx.generation} 已失效"
                f"（当前 {route.generation}），不跨代次重放"
            )
        if route.client is None or client_is_closing(route.client):
            raise InstanceUnavailable(f"平台实例 {ctx.platform_id!r} 的 client 已不可用")
        http = current_http(route.client)
        if http is None or getattr(http, "_token", None) is None:
            raise TransportNotReady(f"平台实例 {ctx.platform_id!r} 的 HTTP 在操作中途失去 token")
        return http

    # -- 生命周期：请求侧 O(1) 更新，巡检侧差异收敛 --

    def _push_change(self, kind: str, old: RouteRecord | None,
                     new: RouteRecord | None) -> None:
        self._changes.append(RouteChange(kind=kind, old=old, new=new))

    def _remove_route(self, platform_id: str, *, reason: str = "") -> RouteRecord | None:
        """移除本地路由并记录变更（含请求侧发现的本体删除/禁用）。"""
        removed = self._routes.pop(platform_id, None)
        if removed is not None:
            self._push_change("removed", removed, None)
            if self.logger and reason:
                self.logger.info(
                    f"[qqoffice_expand] 实例 {platform_id} 已移除路由：{reason}"
                )
        return removed

    def drop_route(self, platform_id: str) -> RouteRecord | None:
        """移除本地路由记录（补丁恢复由协调任务处理）；机器人状态不受影响。"""
        return self._remove_route(platform_id)

    def _create_route(self, platform_id: str, inst: Any, client: Any,
                      name: str, mode: str) -> RouteRecord:
        robot_key = RobotKey.from_adapter(inst, mode)
        self._generation += 1
        route = RouteRecord(platform_id=platform_id, adapter_name=name, mode=mode,
                            inst=inst, client=client, robot_key=robot_key,
                            generation=self._generation)
        old = self._routes.get(platform_id)
        self._routes[platform_id] = route
        if old is not None:
            self._push_change("replaced", old, route)
        else:
            self._push_change("added", None, route)
        # 状态注册与路由生命周期解耦：只为引用，不因路由删除而清除。
        self.state_for(robot_key)
        return route

    def bind_view_factory(self, factory: Callable[[BoundSource], Any]) -> None:
        """注入视图工厂（main.BoundView），供事件自动 ACK 按来源建视图。"""
        self.view_factory = factory

    def consume_changes(self) -> list[RouteChange]:
        """一次性取走路由变更事件（协调任务在巡检/通知时调用）。

        按 platform_id 折叠：保留首次记录的旧记录与最后一次的新记录，
        连续 A→B→C 只报告 replaced(A, C)；added 后 removed 的事件链
        （协调从未见过）直接消失。
        """
        folded: dict[str, list] = {}
        order: list[str] = []
        for ch in self._changes:
            pid = (ch.new or ch.old).platform_id
            entry = folded.get(pid)
            if entry is None:
                folded[pid] = [ch.old, ch.new]
                order.append(pid)
            else:
                entry[1] = ch.new
        self._changes = []
        out: list[RouteChange] = []
        for pid in order:
            first_old, last_new = folded[pid]
            if last_new is not None and first_old is not None:
                out.append(RouteChange("replaced", first_old, last_new))
            elif last_new is not None:
                out.append(RouteChange("added", None, last_new))
            elif first_old is not None:
                out.append(RouteChange("removed", first_old, None))
        return out

    def refresh_all(self) -> RouteDiff:
        """全体差异刷新（O(P+N+累计changes)，仅通知/巡检时调用）。

        算法：先扫描本体索引（扫描经由 _create_route/_remove_route 把
        old/new 追加到与请求侧相同的 _changes），再一次性 consume_changes()
        折叠——同 pid 全部变化（pending A→B + 扫描 B→C → A→C；初始 added
        与同轮删除 → 无变更）自动折为 first_old→last_new 一条；折叠结果转
        为 diff.added/replaced/removed，最后不在 changed 集合中的当前路由
        进入 unchanged。不读取挂载器状态；请求热路径 O(1) 不变。
        """
        diff = RouteDiff()
        entries = self.index.qq_entries()

        # 1. 扫描本体索引：变更经 _create_route/_remove_route 入 _changes
        for pid in list(self._routes):
            if pid not in entries:
                self._remove_route(pid)
        for pid, entry in entries.items():
            inst = entry.get("inst")
            client = _client_of(inst)
            name, mode = _adapter_meta(inst)
            route = self._routes.get(pid)
            if route is None or route.inst is not inst or route.client is not client:
                self._create_route(pid, inst, client, name, mode)

        # 2. 一次性折叠请求侧 + 扫描期全部变更
        changed_ids: set[str] = set()
        for change in self.consume_changes():
            pid = change.new.platform_id if change.new is not None else change.old.platform_id
            changed_ids.add(pid)
            if change.kind == "removed" and change.old is not None:
                diff.removed.append(change.old)
            elif change.kind == "replaced" and change.old is not None and change.new is not None:
                diff.replaced.append((change.old, change.new))
            elif change.kind == "added" and change.new is not None:
                diff.added.append(change.new)

        # 3. 未变更的当前路由进入 unchanged
        for pid, route in self._routes.items():
            if pid not in changed_ids:
                diff.unchanged.append(route)
        return diff
