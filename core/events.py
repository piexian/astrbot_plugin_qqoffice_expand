"""EventBus + 适配器延迟挂载。

时序硬前置：插件加载早于平台实例化，initialize() 时适配器尚不存在——后台
轮询 platform_insts 延迟挂载，适配器重载后自动重挂。

patch 收敛在 botpy client/ConnectionState 一层（websocket 与 webhook 的回调
都经 connection.parser[event] + client.ws_dispatch），不碰 qo_webhook_server：
① intents 位：仅 ws identify 时生效，已连接需重载平台；群成员 1<<24 在
  botpy Intents 无标志位，只能 client.intents |= bit，合并而非覆盖。
② parsers：botpy 1.2.1 缺 group_member_add/remove/join_request——类级
  setattr（webhook 每次回调懒建新 ConnectionState 实例，只有类级能被自动
  注册）+ 运行中 ws 实例的 state.parsers 直补。
③ client.on_xxx：ws_dispatch 是动态 getattr，setattr 即注册。

INTERACTION_CREATE 先自动 PUT /interactions/{id} code=0（3 秒时限、同 id
一次）再分发，interaction_auto_ack 可关。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .auth import QQClientBundle, find_qq_clients

__all__ = ["QQOfficeEvent", "EventBus", "AdapterPatcher", "EVENT_SPECS", "INTENT_BITS"]

@dataclass(frozen=True)
class EventSpec:
    name: str              # 官方事件名（大写）
    intent: int | None     # 需要置位的 intents bit；None=已在适配器订阅范围
    needs_parser: bool     # botpy 1.2.1 是否缺失 parser
    group: str             # free8 / parser25 / intents24 / intents26 / intents27 / guild_p2
    scene: str             # group / c2c / guild / both

EVENT_SPECS: dict[str, EventSpec] = {
    # —— 第一组：免改 intents、botpy 有 parser（只做第 3 点）——
    "GROUP_ADD_ROBOT":    EventSpec("GROUP_ADD_ROBOT", None, False, "free8", "group"),
    "GROUP_DEL_ROBOT":    EventSpec("GROUP_DEL_ROBOT", None, False, "free8", "group"),
    "GROUP_MSG_RECEIVE":  EventSpec("GROUP_MSG_RECEIVE", None, False, "free8", "group"),
    "GROUP_MSG_REJECT":   EventSpec("GROUP_MSG_REJECT", None, False, "free8", "group"),
    "FRIEND_ADD":         EventSpec("FRIEND_ADD", None, False, "free8", "c2c"),
    "FRIEND_DEL":         EventSpec("FRIEND_DEL", None, False, "free8", "c2c"),
    "C2C_MSG_RECEIVE":    EventSpec("C2C_MSG_RECEIVE", None, False, "free8", "c2c"),
    "C2C_MSG_REJECT":     EventSpec("C2C_MSG_REJECT", None, False, "free8", "c2c"),
    # —— 第二组：免改 intents（1<<25 已订阅）、需补 parser ——
    "GROUP_JOIN_REQUEST": EventSpec("GROUP_JOIN_REQUEST", None, True, "parser25", "group"),
    # —— 第三组：需改 intents ——
    "GROUP_MEMBER_ADD":    EventSpec("GROUP_MEMBER_ADD", 1 << 24, True, "intents24", "group"),
    "GROUP_MEMBER_REMOVE": EventSpec("GROUP_MEMBER_REMOVE", 1 << 24, True, "intents24", "group"),
    "INTERACTION_CREATE":  EventSpec("INTERACTION_CREATE", 1 << 26, False, "intents26", "both"),
    "MESSAGE_AUDIT_PASS":  EventSpec("MESSAGE_AUDIT_PASS", 1 << 27, False, "intents27", "guild"),
    "MESSAGE_AUDIT_REJECT": EventSpec("MESSAGE_AUDIT_REJECT", 1 << 27, False, "intents27", "guild"),
    # —— P2 频道组 ——
    "MESSAGE_REACTION_ADD":    EventSpec("MESSAGE_REACTION_ADD", 1 << 10, False, "guild_p2", "guild"),
    "MESSAGE_REACTION_REMOVE": EventSpec("MESSAGE_REACTION_REMOVE", 1 << 10, False, "guild_p2", "guild"),
    "GUILD_CREATE":      EventSpec("GUILD_CREATE", 1 << 0, False, "guild_p2", "guild"),
    "GUILD_UPDATE":      EventSpec("GUILD_UPDATE", 1 << 0, False, "guild_p2", "guild"),
    "GUILD_DELETE":      EventSpec("GUILD_DELETE", 1 << 0, False, "guild_p2", "guild"),
    "CHANNEL_CREATE":    EventSpec("CHANNEL_CREATE", 1 << 0, False, "guild_p2", "guild"),
    "CHANNEL_UPDATE":    EventSpec("CHANNEL_UPDATE", 1 << 0, False, "guild_p2", "guild"),
    "CHANNEL_DELETE":    EventSpec("CHANNEL_DELETE", 1 << 0, False, "guild_p2", "guild"),
}

INTENT_BITS = {"interaction": 1 << 26, "message_audit": 1 << 27, "guild_member": 1 << 24,
               "guild_message_reactions": 1 << 10, "guilds": 1 << 0}

# event_id 被动回复的官方支持范围
EVENT_ID_SCOPES = {
    "group": {"INTERACTION_CREATE", "GROUP_ADD_ROBOT", "GROUP_MSG_RECEIVE"},
    "c2c": {"INTERACTION_CREATE", "C2C_MSG_RECEIVE", "FRIEND_ADD"},
}

def _field(obj: Any, name: str, default=None):
    """从 botpy 包装对象或 dict 里防御式取字段。"""
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
    """归一后的官方事件。raw 尽量为官方 d 的原始 dict。"""

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
    received_at: float = field(default_factory=time.time)
    obj: Any = None               # 原始 botpy 包装对象（想摸原始字段时用）

    @property
    def is_interaction(self) -> bool:
        return self.type == "INTERACTION_CREATE"

def normalize_event(event_name: str, obj: Any) -> QQOfficeEvent:
    """把 botpy dispatch 的对象/字典归一为 QQOfficeEvent。"""
    upper = event_name.upper()
    if isinstance(obj, dict):
        inner = obj.get("d")
        # 网关全量 payload（{op,id,t,d}）容错展平；d 的字段优先（msg_id 等在 d 里）
        raw = {**obj, **inner} if isinstance(inner, dict) else dict(obj)
    else:
        raw = {}
    if not raw:
        # botpy 包装对象：__slots__ 常驻，从已知属性拼 raw
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
        raw = {k: v for k, v in raw.items() if v is not None}

    scene = None
    group_openid = raw.get("group_openid") or _field(obj, "group_openid")
    user_openid = raw.get("user_openid") or _field(obj, "user_openid")
    member_openid = (
        raw.get("group_member_openid")
        or raw.get("op_member_openid")
        or _field(obj, "op_member_openid")
    )
    author = raw.get("author") or _field(obj, "author")
    if group_openid:
        scene = "group"
    elif user_openid:
        scene = "c2c"
    elif raw.get("guild_id") or _field(obj, "guild_id"):
        scene = "guild"

    interaction_id = raw.get("id") if upper == "INTERACTION_CREATE" else None
    return QQOfficeEvent(
        type=upper,
        name=event_name,
        raw=raw,
        payload_id=_field(obj, "id") if not isinstance(obj, dict) else raw.get("id"),
        scene=scene,
        user_openid=user_openid or _field(author, "user_openid") if author else user_openid,
        group_openid=group_openid,
        member_openid=member_openid or _field(author, "member_openid") if author else member_openid,
        guild_id=raw.get("guild_id") or _field(obj, "guild_id"),
        channel_id=raw.get("channel_id") or _field(obj, "channel_id"),
        interaction_id=interaction_id,
        timestamp=raw.get("timestamp"),
        obj=obj,
    )

class EventBus:
    def __init__(self, config: dict | None = None, logger=None,
                 on_first_subscribe: Callable[[str], None] | None = None):
        self.config = dict(config or {})
        self.logger = logger
        self._subs: dict[str, list[Callable]] = {}
        self._any: list[Callable] = []
        self._tasks: set[asyncio.Task] = set()
        self._acked: dict[str, float] = {}
        self._ack_caller: Callable[[str], Any] | None = None
        self._on_first_subscribe = on_first_subscribe
        self.counts: dict[str, int] = {}
        self.auto_ack_total = 0

    def on(self, event_type: str, handler: Callable) -> Callable:
        """订阅官方事件（大小写不敏感）。返回解绑闭包。

        未登记的事件类型也接受：登记进表并触发 intents 补挂，
        但能否真正到达取决于网关 parser（见模块 docstring）。
        """
        key = event_type.upper()
        spec = EVENT_SPECS.get(key)
        first = key not in self._subs or not self._subs[key]
        self._subs.setdefault(key, []).append(handler)
        if first and self._on_first_subscribe:
            try:
                self._on_first_subscribe(key)
            except Exception:
                pass
        if spec is None and self.logger:
            self.logger.warning(
                f"[qqoffice_expand] 事件 {key} 不在已知清单（on_any 仍会收到）；"
                f"官方新增事件请同步 EVENT_SPECS"
            )

        def _unsub():
            handlers = self._subs.get(key)
            if handlers and handler in handlers:
                handlers.remove(handler)

        return _unsub

    def on_any(self, handler: Callable) -> Callable:
        """订阅全部已挂载事件（含无专属订阅者的）。返回解绑闭包。"""
        self._any.append(handler)

        def _unsub():
            if handler in self._any:
                self._any.remove(handler)

        return _unsub

    def set_ack_caller(self, caller: Callable[[str], Any] | None) -> None:
        """注入互动应答执行器（main 侧绑定 manage.interaction_ack）。"""
        self._ack_caller = caller

    async def emit(self, ev: QQOfficeEvent) -> None:
        self.counts[ev.type] = self.counts.get(ev.type, 0) + 1
        if ev.is_interaction and self.config.get("interaction_auto_ack", True):
            self._auto_ack(ev)
        handlers = list(self._subs.get(ev.type, [])) + list(self._any)
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

    def _auto_ack(self, ev: QQOfficeEvent) -> None:
        iid = ev.interaction_id or (ev.raw or {}).get("id")
        if not iid:
            return
        now = time.monotonic()
        if iid in self._acked:  # 同一 id 只应答一次
            return
        self._acked[iid] = now
        if len(self._acked) > 2048:  # 防增长：清理 10 分钟前的记录
            for k in [k for k, ts in self._acked.items() if now - ts > 600]:
                self._acked.pop(k, None)
        if self._ack_caller is None:
            if self.logger:
                self.logger.warning("[qqoffice_expand] INTERACTION_CREATE 到达但尚无可用客户端，无法自动应答")
            return

        async def _do_ack():
            try:
                await asyncio.wait_for(self._ack_caller(str(iid)), timeout=2.5)
                self.auto_ack_total += 1
            except asyncio.TimeoutError:
                if self.logger:
                    self.logger.error(f"[qqoffice_expand] 互动应答超时（3 秒时限）: {iid}")
            except Exception as exc:
                if self.logger:
                    self.logger.error(f"[qqoffice_expand] 互动应答失败: {exc!r}")

        task = asyncio.create_task(_do_ack())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def status(self) -> dict:
        return {
            "event_types": sorted(self._subs),
            "subscribers": {k: len(v) for k, v in self._subs.items() if v},
            "any_subscribers": len(self._any),
            "counts": dict(self.counts),
            "auto_acked": self.auto_ack_total,
        }

class AdapterPatcher:
    """后台轮询 platform_insts，发现 QQ 官方适配器后挂载 intents/parsers/on_xxx。"""

    POLL_INTERVAL = 5.0

    def __init__(self, platform_manager: Any, bus: EventBus, config: dict | None = None, logger=None):
        self._platform_manager = platform_manager
        self.bus = bus
        self.config = dict(config or {})
        self.logger = logger
        self._task: asyncio.Task | None = None
        self._patched: dict[int, dict] = {}   # id(client) → 挂载记录（诊断/卸载）
        self._class_parser_names: set[str] = set()
        self._warned_reload: set[int] = set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        await self._unmount_all()

    async def _poll_loop(self) -> None:
        while True:
            try:
                bundles = find_qq_clients(self._platform_manager)
                live_ids = {id(b.client) for b in bundles}
                # 平台重载后旧 client 对象不再存在：清理其挂载记录
                for stale_id in [cid for cid in self._patched if cid not in live_ids]:
                    self._patched.pop(stale_id, None)
                for bundle in bundles:
                    try:
                        self.ensure_patched(bundle)
                    except Exception as exc:
                        self._log("error", f"挂载适配器 {bundle.instance_id} 失败: {exc!r}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("error", f"轮询适配器异常: {exc!r}")
            await asyncio.sleep(self.POLL_INTERVAL)

    def ensure_patched(self, bundle: QQClientBundle) -> dict:
        client = bundle.client
        record = self._patched.get(id(client))
        if record is None:
            record = {"bundle": bundle, "on_added": [], "parser_keys": [],
                      "intents_applied": [], "original_intents": None}
            self._patched[id(client)] = record

        self._patch_parsers_class()
        self._patch_parsers_instance(client, record)
        self._apply_intents(bundle, record, [
            spec for spec in EVENT_SPECS.values()
            if self._intent_enabled(spec)
        ])
        self._mount_handlers(client, record)
        return record

    def _intent_enabled(self, spec: EventSpec) -> bool:
        if spec.intent is None:
            return False
        if spec.intent == 1 << 24:
            return bool(self.config.get("enable_group_member_events", True))
        if spec.intent in (1 << 10, 1 << 0):
            # P2 频道组：有订阅者才置位
            return bool(self.bus._subs.get(spec.name))
        return True  # interaction 1<<26 / audit 1<<27 默认置位

    def _patch_parsers_class(self) -> None:
        """类级补 parser：webhook 模式每次回调懒建新 ConnectionState 实例，
        只有类级 setattr 能被其 inspect.getmembers 自动注册。"""
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
                self_state._dispatch(_name, payload.get("d", {}))

            setattr(ConnectionState, attr, _parse)
            self._class_parser_names.add(name)

    def _patch_parsers_instance(self, client, record: dict) -> None:
        """运行中的 ws 客户端：实例 state.parsers 直补（类级对已有实例无效）。"""
        state = self._get_state(client)
        if state is None:
            return
        parsers = getattr(state, "parsers", None)
        if not isinstance(parsers, dict):
            return
        for spec in EVENT_SPECS.values():
            if not spec.needs_parser:
                continue
            key = spec.name.lower()
            if key in parsers:
                continue

            def _parse(payload, _state=state, _key=key):
                _state._dispatch(_key, payload.get("d", {}))

            parsers[key] = _parse
            if key not in record["parser_keys"]:
                record["parser_keys"].append(key)

    @staticmethod
    def _get_state(client) -> Any:
        """botpy Client._connection（ConnectionSession）.state。"""
        conn = getattr(client, "_connection", None)
        return getattr(conn, "state", None) if conn is not None else None

    def _apply_intents(self, bundle: QQClientBundle, record: dict, specs: list[EventSpec]) -> None:
        if bundle.mode != "ws":
            return
        client = bundle.client
        intents = getattr(client, "intents", None)
        if not isinstance(intents, int):
            self._log("warning", f"client.intents 属性异常（{type(intents)}），跳过 intents 置位")
            return
        if record["original_intents"] is None:
            record["original_intents"] = intents
        newly: list[int] = []
        for spec in specs:
            if spec.intent and spec.intent not in record["intents_applied"]:
                if not intents & spec.intent:
                    intents |= spec.intent  # 合并而非覆盖
                    newly.append(spec.intent)
                record["intents_applied"].append(spec.intent)
        if newly:
            client.intents = intents
            self._log("info", f"适配器 {bundle.instance_id} 追加 intents 位: {newly}")
        if newly and self._looks_connected(client) and id(client) not in self._warned_reload:
            self._warned_reload.add(id(client))
            self._log(
                "warning",
                f"适配器 {bundle.instance_id} 已在会话中：新 intents 位仅在下次 identify 生效，"
                f"请在管理面板重载 QQ 官方平台适配器（或重启 AstrBot）；"
                f"刚安装本插件后未重启即出现此提示时，重启一次 AstrBot 即可",
            )

    @staticmethod
    def _looks_connected(client) -> bool:
        try:
            return not bool(client.is_closed())
        except Exception:
            return False

    def _mount_handlers(self, client, record: dict) -> None:
        for spec in EVENT_SPECS.values():
            attr = f"on_{spec.name.lower()}"
            if hasattr(client, attr):
                # 适配器自有 on_xxx（5 个消息事件）绝不覆盖
                continue
            handler = self._make_handler(spec)
            setattr(client, attr, handler)
            record["on_added"].append(attr)

    def _make_handler(self, spec: EventSpec):
        bus = self.bus

        async def _handler(*args):
            obj = args[0] if args else None
            ev = normalize_event(spec.name.lower(), obj)
            if spec.scene in ("group", "c2c") and ev.scene is None:
                ev.scene = spec.scene
            # 入站 msg_idx 入库（recorder 由 main 注入）
            if self._inbound_recorder is not None:
                try:
                    self._inbound_recorder(ev)
                except Exception:
                    pass
            await bus.emit(ev)

        return _handler

    _inbound_recorder: Callable[[QQOfficeEvent], None] | None = None

    def set_inbound_recorder(self, recorder: Callable[[QQOfficeEvent], None] | None) -> None:
        """main 注入：把入站 msg_idx 写进 refstore。"""
        self._inbound_recorder = recorder

    async def _unmount_all(self) -> None:
        for record in list(self._patched.values()):
            client = record["bundle"].client
            for attr in record["on_added"]:
                try:
                    delattr(client, attr)
                except AttributeError:
                    pass
            state = self._get_state(client)
            parsers = getattr(state, "parsers", None) if state is not None else None
            if isinstance(parsers, dict):
                for key in record["parser_keys"]:
                    parsers.pop(key, None)
            if record["original_intents"] is not None:
                try:
                    client.intents = record["original_intents"]
                except Exception:
                    pass
        if self._class_parser_names:
            try:
                from botpy.connection import ConnectionState

                for name in self._class_parser_names:
                    try:
                        delattr(ConnectionState, f"parse_{name}")
                    except AttributeError:
                        pass
            except Exception:
                pass
            self._class_parser_names.clear()
        self._patched.clear()
        self._warned_reload.clear()

    def status(self) -> dict:
        out = []
        for record in self._patched.values():
            bundle = record["bundle"]
            out.append({
                "instance_id": bundle.instance_id,
                "adapter": bundle.name,
                "mode": bundle.mode,
                "connected": self._looks_connected(bundle.client),
                "on_mounted": len(record["on_added"]),
                "instance_parsers_added": record["parser_keys"],
                "intents_applied": [hex(b) for b in record["intents_applied"]],
            })
        return {"clients": out, "class_parsers": sorted(self._class_parser_names)}

    def _log(self, level: str, msg: str) -> None:
        if self.logger:
            getattr(self.logger, level)(f"[qqoffice_expand] {msg}")

# 首订阅时的 intents 补挂入口（EventBus.on_first_subscribe 回调目标）
def make_first_subscribe_hook(patcher: AdapterPatcher) -> Callable[[str], None]:
    def _hook(event_type: str) -> None:
        spec = EVENT_SPECS.get(event_type)
        if spec is None or spec.intent is None:
            return
        for record in list(patcher._patched.values()):
            bundle = record["bundle"]
            if spec.intent not in record["intents_applied"]:
                patcher._apply_intents(bundle, record, [spec])

    return _hook
