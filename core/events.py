"""EventBus + 适配器协调挂载（N 实例版）。

时序硬前置：插件加载早于平台实例化，构造期类级包装适配器 __init__ 注入
扩展 intents（先于 botpy session['intent'] 快照）；实例级 parser/on_xxx
挂载由 RouteCore 差异驱动（on_platform_loaded、请求侧先发现、低频巡检
三路合流到协调刷新），不再各自轮询。

① intents 位：构造期注入 + 4013/4014 拒断自愈，按运行实例隔离（denied
   位归代次，不再全局）；
② parsers：补 botpy 1.2.1 缺失的群成员 parser（类级，带所有权标记），
   实例级包装现有 state 的扩展事件 parser（记录 dict[state] -> {event ->
   (original, wrapper)}）；
③ client.on_xxx：ws_dispatch 动态 getattr，setattr 即注册；事件携带不可变
   EventSource，订阅分全局/实例作用域，单事件只查相关索引。

INTERACTION_CREATE 的 type=11/12 自动 PUT /interactions/{id} code=0
（3 秒时限、同 id 一次）；应答经来源视图（AckTracker 预留/终态语义）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .routing import EventSource, RouteCore

_RAW_EVENT: "object | None" = None  # 延迟创建的 ContextVar，见 _raw_event()

__all__ = ["QQOfficeEvent", "EventBus", "AdapterPatcher", "EVENT_SPECS",
           "INTENT_BITS", "make_first_subscribe_hook"]


def _raw_event():
    global _RAW_EVENT
    if _RAW_EVENT is None:
        from contextvars import ContextVar
        _RAW_EVENT = ContextVar("qqoffice_raw_event", default=None)
    return _RAW_EVENT


@dataclass(frozen=True)
class EventSpec:
    name: str              # 官方事件名（大写）
    intent: int | None     # 需要置位的 intents bit；None=已在适配器订阅范围
    needs_parser: bool     # botpy 1.2.1 是否缺失 parser
    group: str             # free8 / parser25 / intents24 / intents26 / intents27 / guild_p2
    scene: str             # group / c2c / guild / dm / both


EVENT_SPECS: dict[str, EventSpec] = {
    "GROUP_ADD_ROBOT":    EventSpec("GROUP_ADD_ROBOT", None, False, "free8", "group"),
    "GROUP_DEL_ROBOT":    EventSpec("GROUP_DEL_ROBOT", None, False, "free8", "group"),
    "GROUP_MSG_RECEIVE":  EventSpec("GROUP_MSG_RECEIVE", None, False, "free8", "group"),
    "GROUP_MSG_REJECT":   EventSpec("GROUP_MSG_REJECT", None, False, "free8", "group"),
    "FRIEND_ADD":         EventSpec("FRIEND_ADD", None, False, "free8", "c2c"),
    "FRIEND_DEL":         EventSpec("FRIEND_DEL", None, False, "free8", "c2c"),
    "C2C_MSG_RECEIVE":    EventSpec("C2C_MSG_RECEIVE", None, False, "free8", "c2c"),
    "C2C_MSG_REJECT":     EventSpec("C2C_MSG_REJECT", None, False, "free8", "c2c"),
    "GROUP_JOIN_REQUEST": EventSpec("GROUP_JOIN_REQUEST", None, True, "parser25", "group"),
    "GROUP_MEMBER_ADD":    EventSpec("GROUP_MEMBER_ADD", 1 << 24, True, "intents24", "group"),
    "GROUP_MEMBER_REMOVE": EventSpec("GROUP_MEMBER_REMOVE", 1 << 24, True, "intents24", "group"),
    "INTERACTION_CREATE":  EventSpec("INTERACTION_CREATE", 1 << 26, False, "intents26", "both"),
    "MESSAGE_AUDIT_PASS":  EventSpec("MESSAGE_AUDIT_PASS", 1 << 27, False, "intents27", "guild"),
    "MESSAGE_AUDIT_REJECT": EventSpec("MESSAGE_AUDIT_REJECT", 1 << 27, False, "intents27", "guild"),
    "MESSAGE_REACTION_ADD":    EventSpec("MESSAGE_REACTION_ADD", 1 << 10, False, "guild_p2", "guild"),
    "MESSAGE_REACTION_REMOVE": EventSpec("MESSAGE_REACTION_REMOVE", 1 << 10, False, "guild_p2", "guild"),
    "GUILD_CREATE":      EventSpec("GUILD_CREATE", 1 << 0, False, "guild_p2", "guild"),
    "GUILD_UPDATE":      EventSpec("GUILD_UPDATE", 1 << 0, False, "guild_p2", "guild"),
    "GUILD_DELETE":      EventSpec("GUILD_DELETE", 1 << 0, False, "guild_p2", "guild"),
    "CHANNEL_CREATE":    EventSpec("CHANNEL_CREATE", 1 << 0, False, "guild_p2", "guild"),
    "CHANNEL_UPDATE":    EventSpec("CHANNEL_UPDATE", 1 << 0, False, "guild_p2", "guild"),
    "CHANNEL_DELETE":    EventSpec("CHANNEL_DELETE", 1 << 0, False, "guild_p2", "guild"),
    "GUILD_MEMBER_ADD":    EventSpec("GUILD_MEMBER_ADD", 1 << 1, False, "guild_p2", "guild"),
    "GUILD_MEMBER_UPDATE": EventSpec("GUILD_MEMBER_UPDATE", 1 << 1, False, "guild_p2", "guild"),
    "GUILD_MEMBER_REMOVE": EventSpec("GUILD_MEMBER_REMOVE", 1 << 1, False, "guild_p2", "guild"),
    "MESSAGE_CREATE": EventSpec("MESSAGE_CREATE", 1 << 9, False, "guild_p2", "guild"),
    "MESSAGE_DELETE": EventSpec("MESSAGE_DELETE", 1 << 9, False, "guild_p2", "guild"),
    "DIRECT_MESSAGE_DELETE": EventSpec("DIRECT_MESSAGE_DELETE", None, False, "guild_p2", "dm"),
    "PUBLIC_MESSAGE_DELETE": EventSpec("PUBLIC_MESSAGE_DELETE", None, False, "guild_p2", "guild"),
}

for _bit, _names in (
    (28, ("FORUM_THREAD_CREATE", "FORUM_THREAD_UPDATE", "FORUM_THREAD_DELETE",
          "FORUM_POST_CREATE", "FORUM_POST_DELETE", "FORUM_REPLY_CREATE",
          "FORUM_REPLY_DELETE", "FORUM_PUBLISH_AUDIT_RESULT")),
    (29, ("AUDIO_START", "AUDIO_FINISH", "AUDIO_ON_MIC", "AUDIO_OFF_MIC")),
    (19, ("AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER", "AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT")),
    (18, ("OPEN_FORUM_THREAD_CREATE", "OPEN_FORUM_THREAD_UPDATE", "OPEN_FORUM_THREAD_DELETE",
          "OPEN_FORUM_POST_CREATE", "OPEN_FORUM_POST_DELETE", "OPEN_FORUM_REPLY_CREATE", "OPEN_FORUM_REPLY_DELETE")),
):
    for _name in _names:
        EVENT_SPECS[_name] = EventSpec(_name, 1 << _bit, _name in ("AUDIO_ON_MIC", "AUDIO_OFF_MIC"), "guild_p2", "guild")
del _bit, _names, _name

INTENT_BITS = {"interaction": 1 << 26, "message_audit": 1 << 27, "guild_member": 1 << 24,
               "group_member": 1 << 24, "guild_members": 1 << 1,
               "guild_message_reactions": 1 << 10, "guilds": 1 << 0,
               "guild_messages": 1 << 9, "forums": 1 << 28, "audio_action": 1 << 29,
               "open_forum_event": 1 << 18, "audio_or_live_channel_member": 1 << 19}

# 插件可能追加的全部 intents 位（与 AstrBot 基础位 1<<30/1<<25/1<<12 无交集）
_PLUGIN_INTENT_BITS = 0
for _s in EVENT_SPECS.values():
    _PLUGIN_INTENT_BITS |= _s.intent or 0
del _s

_INTENTS_REJECT_CODES = (4013, 4014)

EVENT_ID_SCOPES = {
    "group": {"INTERACTION_CREATE", "GROUP_ADD_ROBOT", "GROUP_MSG_RECEIVE"},
    "c2c": {"INTERACTION_CREATE", "C2C_MSG_RECEIVE", "FRIEND_ADD"},
}

# 主通道消息事件：适配器自有 on_xxx，绝不覆盖（普通消息管线归本体）
_NATIVE_MESSAGE_EVENTS = {
    "at_message_create", "message_create", "direct_message_create",
    "group_at_message_create", "c2c_message_create", "group_message_create",
}


def _field(obj: Any, name: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    value = getattr(obj, name, None)
    if value is not None:
        return value
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return d.get(name, default)
    return default


@dataclass
class QQOfficeEvent:
    """归一后的官方事件。raw 尽量为官方 d 的原始 dict；source 由挂载层绑定。"""

    type: str                     # 官方事件名（大写），如 GROUP_MEMBER_ADD
    name: str                     # 小写 dispatch 名，如 group_member_add
    raw: dict                     # payload.d（包装对象拿不到时为字段拼凑）
    payload_id: str | None        # 事件最外层 id（event_id 被动通道用）
    scene: str | None
    user_openid: str | None = None
    group_openid: str | None = None
    member_openid: str | None = None
    guild_id: str | None = None
    channel_id: str | None = None
    interaction_id: str | None = None
    timestamp: str | None = None
    user_id: str | None = None     # 频道用户 ID，与 QQ member_openid 分开
    message_id: str | None = None
    received_at: float = field(default_factory=time.time)
    obj: Any = None               # 原始 botpy 包装对象
    source: EventSource | None = None   # 不可变来源（挂载时绑定，不可变）

    @property
    def is_interaction(self) -> bool:
        return self.type == "INTERACTION_CREATE"


def normalize_event(event_name: str, obj: Any) -> QQOfficeEvent:
    """把 botpy dispatch 的对象/字典归一为 QQOfficeEvent。"""
    upper = event_name.upper()
    if isinstance(obj, dict):
        inner = obj.get("d")
        raw = {**obj, **inner} if isinstance(inner, dict) else dict(obj)
    else:
        raw = {}
    if not raw:
        author = _field(obj, "author")
        raw = {
            "id": _field(obj, "id"),
            "group_openid": _field(obj, "group_openid"),
            "user_openid": _field(obj, "user_openid"),
            "op_member_openid": _field(obj, "op_member_openid")
            or _field(author, "member_openid") if author else _field(obj, "op_member_openid"),
            "guild_id": _field(obj, "guild_id"),
            "channel_id": _field(obj, "channel_id"),
        }
        data_obj = _field(obj, "data")
        if data_obj is not None:
            resolved_obj = _field(data_obj, "resolved")
            resolved = {
                k: v
                for k, v in {
                    "button_data": _field(resolved_obj, "button_data"),
                    "button_id": _field(resolved_obj, "button_id"),
                    "message_id": _field(resolved_obj, "message_id"),
                }.items()
                if v is not None
            }
            raw["data"] = {"resolved": resolved}
            data_type = _field(data_obj, "type")
            if data_type is not None:
                raw["data"]["type"] = data_type
        raw = {k: v for k, v in raw.items() if v is not None}
        for key in ("type", "scene", "timestamp", "member_openid", "group_member_openid",
                    "user_id", "message_id", "audit_id", "emoji", "target", "user"):
            value = _field(obj, key)
            if value is not None:
                raw[key] = value

    scene = None
    group_openid = raw.get("group_openid") or _field(obj, "group_openid")
    user_openid = (
        raw.get("user_openid") or raw.get("openid")
        or _field(obj, "user_openid") or _field(obj, "openid")
    )
    member_openid = (
        raw.get("member_openid")
        or raw.get("group_member_openid")
        or raw.get("op_member_openid")
        or _field(obj, "op_member_openid")
    )
    deleted_message = raw.get("message") if "MESSAGE_DELETE" in upper else None
    author = raw.get("author") or _field(deleted_message, "author") or _field(obj, "author")
    guild_id = raw.get("guild_id") or _field(deleted_message, "guild_id") or _field(obj, "guild_id")
    channel_id = raw.get("channel_id") or _field(deleted_message, "channel_id") or _field(obj, "channel_id")
    if upper in ("GUILD_CREATE", "GUILD_UPDATE", "GUILD_DELETE"):
        guild_id = guild_id or raw.get("id")
    if upper in ("CHANNEL_CREATE", "CHANNEL_UPDATE", "CHANNEL_DELETE"):
        channel_id = channel_id or raw.get("id")
    if group_openid:
        scene = "group"
    elif user_openid:
        scene = "c2c"
    elif guild_id or channel_id:
        scene = "dm" if upper.startswith("DIRECT_MESSAGE_") else "guild"

    interaction_id = raw.get("id") if upper == "INTERACTION_CREATE" else None
    return QQOfficeEvent(
        type=upper,
        name=event_name,
        raw=raw,
        payload_id=(
            (obj.get("id") if isinstance(obj.get("d"), dict) else raw.get("id"))
            if isinstance(obj, dict)
            else (_field(obj, "event_id") or _field(obj, "id"))
        ),
        scene=scene,
        user_openid=user_openid or _field(author, "user_openid") if author else user_openid,
        group_openid=group_openid,
        member_openid=member_openid or _field(author, "member_openid") if author else member_openid,
        guild_id=guild_id,
        channel_id=channel_id,
        interaction_id=interaction_id,
        timestamp=raw.get("timestamp"),
        user_id=raw.get("user_id") or _field(raw.get("user"), "id") or _field(author, "id"),
        message_id=raw.get("message_id") or _field(deleted_message, "id") or _field(raw.get("target"), "id")
        or (raw.get("id") if "MESSAGE_DELETE" in upper or upper == "MESSAGE_CREATE" else None),
        obj=obj,
    )


class EventBus:
    """扩展事件分发：全局订阅 + 实例作用域订阅，单事件只合并相关索引（O(H)）。"""

    def __init__(self, config: dict | None = None, logger=None):
        self.config = dict(config or {})
        self.logger = logger
        self._subs: dict[str, list[Callable]] = {}
        self._any: list[Callable] = []
        self._scoped: dict[tuple[str, str, str], list[Callable]] = {}   # (pid, appid, EVENT)
        self._scoped_any: dict[tuple[str, str], list[Callable]] = {}    # (pid, appid)
        self._scoped_bits: dict[tuple[str, str], int] = {}  # (pid, prefix) -> 按需位缓存
        self._tasks: set[asyncio.Task] = set()
        self._ack_caller: Callable[[Any, str], Any] | None = None  # (view, iid)
        self._routes: RouteCore | None = None
        self._stopping = False   # 卸载后不再投递/应答
        self.counts: dict[str, int] = {}
        self.auto_ack_total = 0

    async def stop(self) -> None:
        """卸载：停用投递并取消/等待本插件拥有的任务。"""
        self._stopping = True
        tasks = [t for t in self._tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.counts: dict[str, int] = {}
        self.auto_ack_total = 0

    # -- 订阅 --

    def on(self, event_type: str, handler: Callable) -> tuple[Callable, bool]:
        """全局订阅（大小写不敏感）。返回 (解绑闭包, 是否新增首订阅)。"""
        key = event_type.upper()
        is_first = key not in self._subs or not self._subs[key]
        self._subs.setdefault(key, []).append(handler)
        if EVENT_SPECS.get(key) is None and self.logger:
            self.logger.warning(
                f"[qqoffice_expand] 事件 {key} 不在已知清单（on_any 仍会收到）；"
                f"官方新增事件请同步 EVENT_SPECS"
            )

        def _unsub():
            handlers = self._subs.get(key)
            if handlers and handler in handlers:
                handlers.remove(handler)

        return _unsub, is_first

    def on_any(self, handler: Callable) -> Callable:
        """全局透传订阅。"""
        self._any.append(handler)

        def _unsub():
            if handler in self._any:
                self._any.remove(handler)

        return _unsub

    def on_scoped(self, platform_id: str, robot_key, event_type: str,
                  handler: Callable) -> tuple[Callable, bool]:
        """实例作用域订阅：按 (配置 ID, 机器人身份) 绑定。

        同 ID 重载（同身份）跟随生效；同 ID 改绑其他 AppID 不转移。
        返回 (解绑闭包, 是否该键首个订阅)。"""
        prefix = robot_key.prefix()
        key = (platform_id, prefix, event_type.upper())
        is_first = key not in self._scoped or not self._scoped[key]
        self._scoped.setdefault(key, []).append(handler)
        self._reindex_scoped(platform_id, prefix)

        def _unsub():
            handlers = self._scoped.get(key)
            if handlers and handler in handlers:
                handlers.remove(handler)
                self._reindex_scoped(platform_id, prefix)

        return _unsub, is_first

    def on_scoped_any(self, platform_id: str, robot_key, handler: Callable) -> Callable:
        self._scoped_any.setdefault((platform_id, robot_key.prefix()), []).append(handler)

        def _unsub():
            handlers = self._scoped_any.get((platform_id, robot_key.prefix()))
            if handlers and handler in handlers:
                handlers.remove(handler)

        return _unsub

    def scoped_intent_bits(self, platform_id: str, robot_prefix: str | None = None) -> int:
        """该实例**当前非空**作用域订阅需要的 intents 位（按需置位用）。

        按 (platform_id, 机器人身份前缀) 索引：单实例 O(E) 查表，不遍历
        全部 S 个订阅；同 ID 改绑新身份不继承旧订阅权限。
        """
        if robot_prefix is None:
            bits = 0
            for (pid, _prefix), per in self._scoped_bits.items():
                if pid == platform_id:
                    bits |= per
            return bits
        return self._scoped_bits.get((platform_id, robot_prefix), 0)

    def _reindex_scoped(self, pid: str, prefix: str) -> None:
        """重算一个 (pid, prefix) 键的按需位缓存。

        只扫描该身份下的至多 E 个事件键（不遍历全部 S 个订阅），
        每键 handlers 长度即订阅者数。
        """
        bits = 0
        for event_type in EVENT_SPECS:
            handlers = self._scoped.get((pid, prefix, event_type))
            if handlers:
                spec = EVENT_SPECS.get(event_type)
                if spec is not None and spec.intent:
                    bits |= spec.intent
        # 未登记事件（不在 EVENT_SPECS 的类型无 intent 位，无需入索引）
        if bits:
            self._scoped_bits[(pid, prefix)] = bits
        else:
            self._scoped_bits.pop((pid, prefix), None)

    def bind_routes(self, routes: RouteCore) -> None:
        """注入路由核心（自动 ACK 需要按来源建视图）。"""
        self._routes = routes

    def set_ack_caller(self, caller: Callable[[Any, str], Any]) -> None:
        self._ack_caller = caller

    # -- 分发 --

    async def emit(self, ev: QQOfficeEvent) -> None:
        if self._stopping:
            return
        self.counts[ev.type] = self.counts.get(ev.type, 0) + 1
        if ev.is_interaction and self.config.get("interaction_auto_ack", True):
            self._auto_ack(ev)
        source = ev.source
        handlers = list(self._subs.get(ev.type, [])) + list(self._any)
        if source is not None:
            handlers += self._scoped.get(
                (source.platform_id, source.robot_key.prefix(), ev.type), []
            )
            handlers += self._scoped_any.get(
                (source.platform_id, source.robot_key.prefix()), []
            )
        for handler in handlers:
            task = asyncio.create_task(self._safe_call(handler, ev))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _safe_call(self, handler, ev: QQOfficeEvent) -> None:
        try:
            result = handler(ev)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.logger:
                self.logger.error(f"[qqoffice_expand] 事件处理器异常 ({ev.type}): {exc!r}")

    # -- 自动 ACK --

    def _auto_ack(self, ev: QQOfficeEvent) -> None:
        """从事件来源路由应答；预留/终态语义见 AckTracker。"""
        interaction_type = ev.raw.get("type") or _field(ev.raw.get("data"), "type")
        if interaction_type not in (11, 12):
            return
        iid = ev.interaction_id or (ev.raw or {}).get("id")
        source = ev.source
        if not iid or source is None or self._ack_caller is None or self._routes is None:
            if self.logger:
                self.logger.warning(
                    "[qqoffice_expand] INTERACTION_CREATE 到达但缺少来源/应答通道，无法自动应答"
                )
            return
        route = self._routes.route_of(source.platform_id)
        if route is None or route.generation != source.generation:
            if self.logger:
                self.logger.info(
                    f"[qqoffice_expand] 互动 {iid} 来自已失效代次，跳过自动应答"
                )
            return
        state = self._routes.state_for(source.robot_key)

        async def _do_ack():
            if state.acks.try_reserve(str(iid)) != "new":
                return  # 已应答过 / 并发重复
            try:
                view = _make_source_view(self._routes, source)
            except asyncio.CancelledError:
                state.acks.release(str(iid))   # 未发送，回收资格
                raise
            except Exception as exc:   # 源视图构造失败 = 确定未发送
                state.acks.release(str(iid))
                if self.logger:
                    self.logger.error(f"[qqoffice_expand] 互动应答失败: {exc!r}")
                return
            phase = _AckPhase()
            try:
                await asyncio.wait_for(
                    self._ack_caller(view, str(iid), phase), timeout=2.5
                )
            except asyncio.TimeoutError:
                # 超时按已应答处理（settle_unknown）：不能清除后重答同一 id。
                if not phase.reached_sdk:
                    state.acks.release(str(iid))   # 尚未发出：可重答
                else:
                    state.acks.settle_unknown(str(iid))
                if self.logger:
                    self.logger.error(f"[qqoffice_expand] 互动应答超时（3 秒时限）: {iid}")
            except asyncio.CancelledError:
                if phase.definitive_rejected or not phase.reached_sdk:
                    state.acks.release(str(iid))   # 明确拒绝或未发送：可重答
                else:
                    state.acks.settle_unknown(str(iid))   # 结果未知：保留
                raise
            except Exception as exc:
                if phase.definitive_rejected or not phase.reached_sdk:
                    state.acks.release(str(iid))   # 明确拒绝或未发送：可重答
                else:
                    state.acks.settle_unknown(str(iid))   # 结果未知：保留
                if self.logger:
                    self.logger.error(f"[qqoffice_expand] 互动应答失败: {exc!r}")
            else:
                if phase.reached_sdk and phase.unknown:
                    state.acks.settle_unknown(str(iid))
                else:
                    state.acks.succeed(str(iid))
                    self.auto_ack_total += 1

        task = asyncio.create_task(_do_ack())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def status(self) -> dict:
        return {
            "event_types": sorted(self._subs),
            "subscribers": {k: len(v) for k, v in self._subs.items() if v},
            "any_subscribers": len(self._any),
            "scoped_subscribers": {
                f"{pid}|{appid}:{evt}": len(v)
                for (pid, appid, evt), v in self._scoped.items() if v
            },
            "scoped_any_subscribers": {
                f"{pid}|{appid}": len(v) for (pid, appid), v in self._scoped_any.items() if v
            },
            "counts": dict(self.counts),
            "auto_acked": self.auto_ack_total,
        }


class _AckPhase:
    """一次自动 ACK 的阶段跟踪：由发送通道按实际阶段置位。

    reached_sdk：已进入 SDK（结果可能生效）；unknown：结果未知（网关/超时）；
    definitive_rejected：官方明确拒绝（确定未生效，可重答）。
    """

    def __init__(self):
        self.reached_sdk = False
        self.unknown = False
        self.definitive_rejected = False


def _make_source_view(routes: RouteCore, source: EventSource):
    """按事件来源构造绑定视图（main.BoundView 由调用方注入避免循环导入）。"""
    factory = getattr(routes, "view_factory", None)
    if factory is None:
        raise RuntimeError("路由核心未注入视图工厂")
    from .routing import BoundSource

    return factory(BoundSource.from_event_source(source))


# ---------------- 适配器协调挂载 ----------------


@dataclass
class _InstancePatch:
    """一个运行代次的补丁记录（按代次隔离，不跨实例共享）。"""

    route: Any                          # RouteRecord
    on_added: list[str] = field(default_factory=list)
    on_handlers: dict[str, Any] = field(default_factory=dict)
    # state -> event_key -> (original, wrapper)；仅本插件包装的键
    parser_patches: dict[int, dict[str, tuple[Any, Any]]] = field(default_factory=dict)
    intents_applied: list[int] = field(default_factory=list)
    owned_mask: int = 0                 # 本插件在该实例上真正新增的位（恢复/降级只动它）
    ctor_mask: int = 0                  # 构造期注入的位（含 adapter.intents.value 改写）
    adapter_obj: Any = None             # 构造期被改写 intents.value 的适配器对象
    verdict: Any = None                 # None | "ok" | ("missing", bits)
    denials: int = 0                    # 本代次收到的网关拒断次数
    denied_bits: int = 0                # 本实例曾被拒授权的位（进程内按代次隔离）
    class_parser_keys: set[str] = field(default_factory=set)  # 实例上来自类级补丁的键


class AdapterPatcher:
    """由 RouteCore 差异驱动的实例挂载器：intents 注入 / parser 补丁 / on_xxx。"""

    def __init__(self, routes: RouteCore, bus: EventBus, config: dict | None = None,
                 logger=None):
        self.routes = routes
        self.bus = bus
        self.config = dict(config or {})
        self.logger = logger
        self._patches: dict[int, _InstancePatch] = {}   # id(route) -> record
        self._by_platform: dict[str, int] = {}          # platform_id -> id(route)
        self._class_parser_names: set[str] = set()
        self._class_parser_funcs: dict[str, Any] = {}   # name -> 实际创建函数（身份比较）
        self._hooks_installed = False
        self._inbound_recorder: Callable[[Any, Any], None] | None = None
        self._states_by_id: dict[int, Any] = {}   # id(state) -> state（恢复补丁用）
        self._stopping = False
        # 构造期注入的逐 client 记录：id(client) -> {"client", "adapter", "owned", "denied"}
        # 挂载时收编本 client 并删除条目；卸载/拒断各还各的。
        self._ctor_pending: dict[int, dict] = {}

    # -- 生命周期 --

    def set_inbound_recorder(self, recorder: Callable[[Any, Any], None]) -> None:
        self._inbound_recorder = recorder

    def install_adapter_hooks(self) -> None:
        """类级包装适配器 __init__ 与 WS on_closed（构造期 intents / 拒断自愈）。

        标记 _CTOR_MARK 存 {owner, previous}；owner 是安装它的 patcher。
        - 无标记：正常包裹当前链顶；
        - 标记 owner 是自己且链顶是自己的 wrapper：幂等返回；
        - 标记属于已停用 patcher 或链顶已是第三方：重新包裹当前链顶
          （旧 wrapper 已停用透传，不会双重注入）。
        """
        try:
            from astrbot.core.platform.sources.qqofficial import (
                qqofficial_platform_adapter as qoa,
            )
        except Exception as exc:
            self._log("warning", f"适配器模块导入失败，构造期 intents 注入不可用: {exc!r}")
            return
        cls = getattr(qoa, "QQOfficialPlatformAdapter", None)
        if cls is not None and self._may_install_hook(cls, self._CTOR_MARK):
            orig_init = cls.__init__
            patcher = self

            def _patched_init(adapter, *args, __orig=orig_init, **kwargs):
                if patcher._stopping:
                    # 插件已卸载但第三方仍持有本 wrapper：透传本体原行为。
                    __orig(adapter, *args, **kwargs)
                    return
                __orig(adapter, *args, **kwargs)
                patcher._inject_construction_intents(adapter)

            _patched_init._qqoffice_hook_owner = self
            setattr(cls, self._CTOR_MARK,
                    {"owner": self, "previous": orig_init, "installed": _patched_init})
            cls.__init__ = _patched_init
            self._hooks_installed = True
        ws_cls = getattr(qoa, "ManagedBotWebSocket", None)
        if ws_cls is not None and self._may_install_hook(ws_cls, self._ONCLOSED_MARK):
            orig_on_closed = ws_cls.on_closed
            patcher = self

            async def _patched_on_closed(ws, code, msg, __orig=orig_on_closed):
                if not patcher._stopping:
                    patcher._heal_intents_reject(ws, code, msg)
                await __orig(ws, code, msg)

            _patched_on_closed._qqoffice_hook_owner = self
            setattr(ws_cls, self._ONCLOSED_MARK,
                    {"owner": self, "previous": orig_on_closed,
                     "installed": _patched_on_closed})
            ws_cls.on_closed = _patched_on_closed
            self._hooks_installed = True

    def _may_install_hook(self, cls: Any, mark: str) -> bool:
        """判断本 patcher 能否在 cls 上安装 hook（不覆盖活跃的他方所有权）。"""
        meta = getattr(cls, mark, None)
        if not isinstance(meta, dict):
            return True   # 无标记（或第三方遗留的无关属性）：可安装
        owner = meta.get("owner")
        if owner is self:
            return False   # 已由本 patcher 安装：幂等
        # 标记属于别的 patcher：仅当其已停用时接管
        return bool(getattr(owner, "_stopping", True))

    def uninstall_adapter_hooks(self) -> None:
        try:
            from astrbot.core.platform.sources.qqofficial import (
                qqofficial_platform_adapter as qoa,
            )
        except Exception:
            return
        # 先停用：第三方闭包若仍持有本 wrapper，调用时透传本体原行为。
        self._stopping = True
        cls = getattr(qoa, "QQOfficialPlatformAdapter", None)
        self._teardown_hook(cls, self._CTOR_MARK, "__init__")
        ws_cls = getattr(qoa, "ManagedBotWebSocket", None)
        self._teardown_hook(ws_cls, self._ONCLOSED_MARK, "on_closed")
        self._hooks_installed = False

    def _teardown_hook(self, cls: Any, mark: str, attr: str) -> None:
        """卸载自己的 hook：链顶**确实是本插件安装的 wrapper**（对象身份
        比较，防 functools.wraps 复制标签）则还原保存的前驱并清标记；
        链顶是第三方则保留第三方属性，但清理自己持有的标记元数据，
        让后续新插件实例能重新包裹当前第三方 wrapper。"""
        if cls is None or not hasattr(cls, mark):
            return
        meta = getattr(cls, mark, None)
        if isinstance(meta, dict) and meta.get("owner") is not self:
            return   # 标记属于别的 patcher：不动别人的元数据
        current = getattr(cls, attr, None)
        installed = meta.get("installed") if isinstance(meta, dict) else None
        if installed is not None and current is installed:
            previous = meta.get("previous") if isinstance(meta, dict) else None
            if previous is not None:
                setattr(cls, attr, previous)
        # 无论链顶是谁（第三方 wraps 复制了标签也一样），自己的标记已无用
        if not isinstance(meta, dict) or meta.get("owner") is self:
            try:
                delattr(cls, mark)
            except AttributeError:
                pass

    _CTOR_MARK = "_qqoffice_orig_init"
    _ONCLOSED_MARK = "_qqoffice_orig_on_closed"

    # -- 差异应用 --

    def refresh(self) -> None:
        """全量差异刷新（无变化时零改动）。"""
        self.apply_diff(self.routes.refresh_all())

    def apply_diff(self, diff) -> None:
        """消费已折叠的路由差异：恢复旧补丁、挂载最终代次、清理消失实例。

        diff 中的 replaced 是 (协调已知旧代次, 最终当前代次) 单条折叠；
        中间代次不出现在 diff 中，也不挂载。恢复按路由对象定位，
        同 pid 过时记录不会误删当前代次。
        """
        for old in diff.removed:
            self._unmount_instance(old)
        for old, _new in diff.replaced:
            self._unmount_instance(old)
        # 旧 wrapper 恢复后再挂最终代次，保证同 state 顺序正确
        for _old, new in diff.replaced:
            self._mount_instance(new)
        for added in diff.added:
            self._mount_instance(added)
        for route in diff.unchanged:
            record = self._patches.get(self._by_platform.get(route.platform_id, -1))
            if record is not None:
                self._refresh_instance(record)   # 延迟出现的 state / 换 API / intents 校验
            else:
                self._mount_instance(route)

    # -- intents --

    def _intent_enabled(self, spec: EventSpec) -> bool:
        if spec.intent is None:
            return False
        if spec.intent == 1 << 24:
            return bool(self.config.get("enable_group_member_events", True))
        if spec.group == "guild_p2":
            return bool(self.bus._subs.get(spec.name))
        return True

    def _is_our_class_parser(self, key: str, func_or_method: Any) -> bool:
        """对象身份比较：该函数/方法是否是本插件为 key 创建的类级 parser。

        创建的闭包函数名是 _parse（挂到 ConnectionState 属性后函数名不变），
        不能按 __name__ 推测；第三方 @wraps 会复制标签，也不能用属性判断。
        """
        func = getattr(func_or_method, "__func__", func_or_method)
        ours = getattr(self, "_class_parser_funcs", {}).get(key)
        return func is ours

    def _boot_bits(self, record: _InstancePatch | None = None) -> int:
        """构造期应注入的位：启用事件全集减去该实例被拒授权的位。

        构造发生在本体把实例放进 _inst_map 之前，无法也绝不在此 resolve
        路由；实例作用域订阅的按需位由挂载后 _apply_intents 补齐。
        """
        denied = record.denied_bits if record is not None else 0
        bits = 0
        for spec in EVENT_SPECS.values():
            if spec.intent and self._intent_enabled(spec):
                bits |= spec.intent
        return bits & ~denied

    def _inject_construction_intents(self, adapter) -> None:
        try:
            client = getattr(adapter, "client", None)
            intents = getattr(client, "intents", None)
            if client is None or not isinstance(intents, int):
                return
            bits = self._boot_bits()
            newly = bits & ~intents
            if not newly:
                return
            client.intents = intents | newly
            base_value = getattr(getattr(adapter, "intents", None), "value", None)
            if isinstance(base_value, int):
                adapter.intents.value = base_value | newly
            # 逐 client 记录本插件真正新增的位（挂载时收编进 patch.owned_mask）
            per = self._ctor_pending.setdefault(id(client), {
                "client": client, "adapter": adapter, "owned": 0, "denied": 0,
            })
            per["owned"] |= newly
            self._log("info", f"构造期注入扩展 intents 位 0x{newly:08x}")
        except Exception as exc:
            self._log("error", f"构造期注入 intents 失败: {exc!r}")

    def _apply_intents(self, patch: _InstancePatch) -> None:
        """挂载期补位：需求 = 启用功能全集 + 本实例 scoped 订阅需求。

        只操作本插件新增的位（owned_mask）；本体/其他来源已存在的位不记入
        也不恢复。曾在本实例被拒的位不再申请。
        """
        if patch.route.mode != "ws":
            return
        client = patch.route.client
        intents = getattr(client, "intents", None)
        if not isinstance(intents, int):
            self._log("warning", f"client.intents 属性异常，跳过置位: {patch.route.platform_id}")
            return
        denied = patch.denied_bits
        scoped_bits = self.bus.scoped_intent_bits(
            patch.route.platform_id, patch.route.robot_key.prefix()
        )
        needed = 0
        for spec in EVENT_SPECS.values():
            if spec.intent and (self._intent_enabled(spec) or (spec.intent & scoped_bits)):
                needed |= spec.intent
        # 收编本 client 的构造期记录：owned 位入账、denied 位并入拒断记忆
        pending = self._ctor_pending.pop(id(patch.route.client), None)
        if pending is not None:
            owned = pending["owned"] & ~pending["denied"]
            patch.owned_mask |= owned     # 构造期位已在 client 上：只转移所有权
            patch.ctor_mask |= owned      # 这部分位当初也改写了 adapter.intents.value
            patch.adapter_obj = pending.get("adapter")
            patch.denied_bits |= pending["denied"]
            denied = patch.denied_bits
        wanted = needed & ~denied
        newly = wanted & ~intents & ~patch.owned_mask   # 本体当前没有且未持有 → 新增
        bit = 1
        while bit <= wanted:
            if wanted & bit and bit not in patch.intents_applied:
                patch.intents_applied.append(bit)
            bit <<= 1
        if newly:
            intents |= newly
            patch.owned_mask |= newly
            client.intents = intents
            listed = []
            v = newly
            while v:
                listed.append(hex(v & -v))
                v &= v - 1
            self._log("info", f"适配器 {patch.route.platform_id} 追加 intents 位: {listed}")
        self._verify_session_intents(patch)

    def _collect_sessions(self, client) -> tuple[list[dict], list[dict]]:
        active: list[dict] = []
        for ws in getattr(client, "_active_websockets", None) or ():
            session = getattr(ws, "_session", None)
            if isinstance(session, dict):
                active.append(session)
        conn = getattr(client, "_connection", None)
        pending = [s for s in (getattr(conn, "_session_list", None) or [])
                   if isinstance(s, dict)]
        return active, pending

    def _verify_session_intents(self, patch: _InstancePatch) -> None:
        required = 0
        for bit in patch.intents_applied:
            required |= bit
        required &= ~patch.denied_bits
        if not required:
            return
        active, pending = self._collect_sessions(patch.route.client)
        for session in pending:
            if isinstance(session.get("intent"), int):
                session["intent"] |= required
        if not active:
            return
        missing = 0
        for session in active:
            current = session.get("intent")
            if isinstance(current, int):
                missing |= required & ~current
        if not missing:
            if patch.verdict != "ok":
                patch.verdict = "ok"
                self._log("info",
                          f"适配器 {patch.route.platform_id} 会话 identify 已含扩展位 "
                          f"0x{required:08x}")
            return
        state = ("missing", missing)
        if patch.verdict == state:
            return
        patch.verdict = state
        self._log("warning",
                  f"适配器 {patch.route.platform_id} 会话 identify 缺少扩展位 "
                  f"0x{missing:08x}：请重载一次该 QQ 官方平台适配器")

    def _heal_intents_reject(self, ws, code: int, msg: Any) -> None:
        """identify 被网关 4013/4014 拒断：剔位自愈，按运行实例隔离。

        只剔除本插件为该实例新增的位（applied 位 + 未 applied 的插件位
        中的启用候选），不碰本体与其他来源的位；其他实例不受影响。
        """
        if code not in _INTENTS_REJECT_CODES or not _PLUGIN_INTENT_BITS:
            return
        client = getattr(ws, "_client", None)
        patch = None
        for record in self._patches.values():
            if record.route.client is client:
                patch = record
                break
        pending = self._ctor_pending.get(id(client))
        # 本插件为该 client 新增的位（含构造期同时改写 adapter.value 的部分）
        if patch is not None:
            strip = patch.owned_mask | patch.ctor_mask
            adapter_obj = patch.adapter_obj
            pid = patch.route.platform_id
        elif pending is not None:
            strip = pending["owned"]
            adapter_obj = pending.get("adapter")
            pid = "pending"
        else:
            strip = 0              # 无本插件 owned（本体自有位被拒）：不剔不记忆
            adapter_obj = None
            pid = "no-plugin-bits"
        session = getattr(ws, "_session", None)
        if strip:
            if isinstance(session, dict) and isinstance(session.get("intent"), int):
                session["intent"] &= ~strip
            if client is not None and isinstance(getattr(client, "intents", None), int):
                client.intents = client.intents & ~strip
            # 构造期改写的 adapter.intents.value 同步还原（保持本体原位不变）
            base_value = getattr(getattr(adapter_obj, "intents", None), "value", None)
            if (adapter_obj is not None and isinstance(base_value, int)
                    and (pending is not None
                         or (patch is not None and patch.ctor_mask & strip))):
                ctor_strip = strip if pending is not None else (strip & patch.ctor_mask)
                if ctor_strip:
                    try:
                        adapter_obj.intents.value = base_value & ~ctor_strip
                    except Exception:
                        pass
        if patch is not None:
            patch.denied_bits |= strip
            patch.owned_mask &= ~strip
            patch.ctor_mask &= ~strip   # adapter 侧已还原，所有权随之注销
            patch.denials += 1
        elif pending is not None:
            pending["denied"] |= strip
            pending["owned"] &= ~strip
        self._log("error",
                  f"实例 {pid} identify 被网关拒断（close={code}）：已剔除扩展位降级，"
                  f"其他实例不受影响；请到开放平台开通权限后重载插件与适配器")

    # -- parser 补丁 --

    def _patch_parsers_class(self) -> None:
        """为之后创建的 ConnectionState 补 SDK 缺失的 parser（带所有权标记）。

        保存实际创建函数引用（name -> func）：第三方 @wraps 会复制
        _qqoffice_owner 标签，身份比较必须用对象本体而非属性。
        """
        try:
            from botpy.connection import ConnectionState
        except Exception:
            return
        for spec in EVENT_SPECS.values():
            if not spec.needs_parser:
                continue
            attr = f"parse_{spec.name.lower()}"
            if hasattr(ConnectionState, attr):
                continue
            name = spec.name.lower()

            def _parse(self_state, payload, _name=name):  # noqa: N805
                self_state._dispatch(_name, payload)

            _parse._qqoffice_owner = self
            setattr(ConnectionState, attr, _parse)
            self._class_parser_names.add(name)
            self._class_parser_funcs[name] = _parse

    def _get_states(self, route) -> list:
        """该实例当前的 WS/Webhook ConnectionState 列表（Webhook 可能延迟出现）。"""
        states = []
        client = route.client
        conn = getattr(client, "_connection", None)
        state = getattr(conn, "state", None)
        if state is not None:
            states.append(state)
        helper = getattr(getattr(route, "inst", None), "webhook_helper", None)
        connection = getattr(helper, "_connection", None)
        state = getattr(connection, "state", None)
        if state is not None and all(state is not cur for cur in states):
            states.append(state)
        return states

    def _patch_parsers_instance(self, patch: _InstancePatch) -> None:
        """包装实例 state 的扩展事件 parser；记录 dict[state][event] = (original, wrapper)。"""
        for state in self._get_states(patch.route):
            self._states_by_id[id(state)] = state
            parsers = getattr(state, "parsers", None)
            if not isinstance(parsers, dict):
                continue
            known = patch.parser_patches.setdefault(id(state), {})
            for spec in EVENT_SPECS.values():
                key = spec.name.lower()
                existing = known.get(key)
                original = parsers.get(key)
                if existing is not None:
                    _, wrapper = existing
                    if parsers.get(key) is wrapper:
                        continue             # 稳定：无新 wrapper
                    if original is None and not spec.needs_parser:
                        continue
                if original is None and not spec.needs_parser:
                    continue
                if original is not None and self._is_our_class_parser(key, original):
                    # 类级补丁被新 state 自动收集：不再包 wrapper，但登记为
                    # 本插件拥有的实例键（卸载时移除）。
                    if key not in known:
                        patch.class_parser_keys.add(key)
                        known[key] = (original, original)
                    continue

                def _parse(payload, _state=state, _key=key, _original=original):
                    if _original is None and self._stopping:
                        return   # 已卸载且无本体原 parser：不再主动分发扩展事件
                    token = _raw_event().set((_key, payload))
                    try:
                        if _original is not None:
                            return _original(payload)
                        return _state._dispatch(_key, payload)
                    finally:
                        _raw_event().reset(token)

                parsers[key] = _parse
                known[key] = (original, _parse)

    def _unmount_instance(self, route) -> None:
        # 以路由对象身份定位补丁；同 pid 的过时记录不会误移除当前代次。
        pid_key = id(route)
        patch = self._patches.pop(pid_key, None)
        if patch is None:
            return
        if self._by_platform.get(route.platform_id) == pid_key:
            self._by_platform.pop(route.platform_id, None)
        client = patch.route.client
        for attr in patch.on_added:
            try:
                if getattr(client, attr, None) is patch.on_handlers[attr]:
                    delattr(client, attr)
            except AttributeError:
                pass
        # 恢复 parser：只改回仍是自己 wrapper 的属性；失效 wrapper 停用透传
        for state_key, per_event in patch.parser_patches.items():
            for key, (original, wrapper) in per_event.items():
                state = self._states_by_id.get(state_key)
                if state is None:
                    continue
                parsers = getattr(state, "parsers", None)
                if not isinstance(parsers, dict):
                    continue
                current = parsers.get(key)
                is_class_parser = (key in patch.class_parser_keys
                                   or (original is not None
                                       and self._is_our_class_parser(key, original)))
                if current is wrapper:
                    if original is None or is_class_parser:
                        # 原本不存在，或原本就是本插件类级 parser：卸载即移除
                        parsers.pop(key, None)
                    else:
                        parsers[key] = original
                elif key in patch.class_parser_keys and current is not None:
                    # 当前是本插件类级 parser 的实例引用（新 state 自动收集）：
                    # 身份比较（wraps 复制标签不可信）；类属性由 stop() 统一还原。
                    if self._is_our_class_parser(key, current):
                        parsers.pop(key, None)
        # 恢复 intents：只移除本插件新增的位（owned_mask），不动本体/其他来源；
        # 构造期改写的 adapter.intents.value 一并还原（各还各的）。
        if patch.owned_mask:
            try:
                intents = getattr(client, "intents", None)
                if isinstance(intents, int):
                    client.intents = intents & ~patch.owned_mask
            except Exception:
                pass
            adapter_obj = patch.adapter_obj
            base_value = getattr(getattr(adapter_obj, "intents", None), "value", None)
            if (adapter_obj is not None and isinstance(base_value, int)
                    and patch.ctor_mask):
                try:
                    adapter_obj.intents.value = base_value & ~patch.ctor_mask
                except Exception:
                    pass
        # 释放该实例引用过的 state 记录（重载后不残留旧 state）
        for state_key in list(patch.parser_patches):
            self._states_by_id.pop(state_key, None)
        self._log("info", f"已卸载实例补丁: {patch.route.platform_id}")

    def _mount_instance(self, route) -> None:
        # 同 pid 旧代次未卸载时先恢复（防 _by_platform 指针覆盖导致失联）
        stale = self._patches.get(self._by_platform.get(route.platform_id, -1))
        if stale is not None and stale.route is not route:
            self._unmount_instance(stale.route)
        pid_key = id(route)
        patch = self._patches.get(pid_key)
        if patch is None:
            patch = _InstancePatch(route=route)
            self._patches[pid_key] = patch
            self._by_platform[route.platform_id] = pid_key
            self._track_state_refs(route)
        self._patch_parsers_class()
        self._patch_parsers_instance(patch)
        self._apply_intents(patch)
        self._mount_handlers(patch)
        self._bind_event_sources(patch)

    def _refresh_instance(self, patch: _InstancePatch) -> None:
        """unchanged 实例：检查延迟出现的 state / 同 client 换 API；
        intents 校验幂等重跑（稳定状态零新增位，真值告警按 verdict 去重）。"""
        self._patch_parsers_instance(patch)
        self._mount_handlers(patch)
        self._apply_intents(patch)

    def _track_state_refs(self, route) -> None:
        for state in self._get_states(route):
            self._states_by_id[id(state)] = state

    def _bind_event_sources(self, patch: _InstancePatch) -> None:
        """把 on_xxx handler 与来源代次绑定（闭包捕获 route）。"""
        route = patch.route
        source = self.routes.source_of(route)
        for attr, handler in patch.on_handlers.items():
            handler._qqoffice_source = source   # 供 handler 闭包使用

    # -- on_xxx 挂载 --

    def _mount_handlers(self, patch: _InstancePatch) -> None:
        for spec in EVENT_SPECS.values():
            attr = f"on_{spec.name.lower()}"
            if hasattr(patch.route.client, attr):
                continue   # 适配器自有 on_xxx（普通消息）绝不覆盖
            if attr in patch.on_handlers:
                handler = patch.on_handlers[attr]
            else:
                handler = self._make_handler(patch, spec)
                setattr(patch.route.client, attr, handler)
                patch.on_added.append(attr)
                patch.on_handlers[attr] = handler

    def _make_handler(self, patch: _InstancePatch, spec: EventSpec):
        bus = self.bus
        route = patch.route
        source = self.routes.source_of(route)
        inbound = self._inbound_recorder

        async def _handler(*args):
            if bus._stopping:
                return   # 已卸载：旧 wrapper 被第三方持有时也不再投递
            obj = args[0] if args else None
            captured = _raw_event().get()
            payload = captured[1] if captured and captured[0] == spec.name.lower() else obj
            ev = normalize_event(spec.name.lower(), payload)
            ev.obj = obj
            ev.source = source
            if spec.scene in ("group", "c2c", "guild", "dm") and ev.scene is None:
                ev.scene = spec.scene
            # O(1) 本体来源核验先行：本体索引已无此实例或已换对象（删除/
            # 重载尚未巡检）时，不入库、不投递。
            try:
                entry = self.routes.index.entry(route.platform_id)
            except Exception:
                entry = None
            current_inst = entry.get("inst") if isinstance(entry, dict) else None
            if current_inst is not route.inst:
                return
            if inbound is not None:
                try:
                    inbound(ev, route)
                except Exception:
                    pass
            await bus.emit(ev)

        return _handler

    def _log(self, level: str, msg: str) -> None:
        if self.logger:
            getattr(self.logger, level)(f"[qqoffice_expand] {msg}")

    # -- 卸载 --

    async def stop(self) -> None:
        """插件卸载：恢复全部实例补丁、类级 parser、构造期钩子。"""
        self._stopping = True
        for patch in list(self._patches.values()):
            self._unmount_instance(patch.route)
        self._patches.clear()
        self._by_platform.clear()
        # 还原所有仍 pending 的构造期注入（各还各的；已收编的由 owned_mask 处理）
        for per in self._ctor_pending.values():
            strip = per["owned"]
            client = per["client"]
            try:
                intents = getattr(client, "intents", None)
                if isinstance(intents, int) and strip:
                    client.intents = intents & ~strip
            except Exception:
                pass
            adapter = per.get("adapter")
            base_value = getattr(getattr(adapter, "intents", None), "value", None)
            adapter_obj_intents = getattr(adapter, "intents", None)
            if adapter is not None and isinstance(base_value, int) and strip:
                try:
                    adapter_obj_intents.value = base_value & ~strip
                except Exception:
                    pass
        self._ctor_pending.clear()   # 释放对历次重载 client 的强引用
        if self._class_parser_names:
            try:
                from botpy.connection import ConnectionState

                for name in self._class_parser_names:
                    ours = getattr(self, "_class_parser_funcs", {}).get(name)
                    try:
                        attr = f"parse_{name}"
                        current = getattr(ConnectionState, attr, None)
                        # 身份比较：第三方 wraps 副本不算本插件所有
                        if ours is not None and getattr(
                                current, "__func__", current) is ours:
                            delattr(ConnectionState, attr)
                    except AttributeError:
                        pass
            except Exception:
                pass
            self._class_parser_names.clear()
            self._class_parser_funcs.clear()
        self._states_by_id.clear()
        self.uninstall_adapter_hooks()

    def status(self) -> dict:
        instances = []
        for patch in self._patches.values():
            route = patch.route
            instances.append({
                "platform_id": route.platform_id,
                "adapter": route.adapter_name,
                "mode": route.mode,
                "generation": route.generation,
                "connected": getattr(route.client, "_connection", None) is not None
                if route.mode == "ws" else bool(self._get_states(route)),
                "on_mounted": len(patch.on_added),
                "intents_applied": [hex(b) for b in patch.intents_applied],
                "denied_bits": hex(patch.denied_bits),
                "verdict": patch.verdict,
            })
        return {"instances": instances,
                "class_parsers": sorted(self._class_parser_names),
                "ctor_hook": self._hooks_installed}


def make_first_subscribe_hook(patcher: AdapterPatcher) -> Callable[[str], None]:
    """兼容入口：首个订阅触发一次全量刷新。"""
    def _hook(event_type: str) -> None:
        patcher.refresh()

    return _hook
