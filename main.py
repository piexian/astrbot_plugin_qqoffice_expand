"""astrbot_plugin_qqoffice_expand — QQ 官方机器人扩展能力中台（N 实例版）。

接入方式见 README（get_registered_star 取 star_cls 作为 svc）：
- svc.instance("qq_sales")：明确配置实例 ID 的主动调用视图（创建时即校验
  本体当前实例并固定机器人身份，改绑 AppID 后旧视图明确失败）；
- svc.for_event(event)：从原生/扩展事件绑定来源的视图（持有事件目标，
  send_rich 不必再传 event）；
- 根服务只保留构建器、全局订阅、实例查询与状态，无无来源发送。

注意使用时机：AstrBot 中插件 initialize() 先于平台实例化。svc.instance(id)
要求该平台已在运行索引中；依赖方应在收到 on_plugin_loaded 广播且平台已
就绪后创建视图（或先订阅事件、在事件回调里 for_event）。
"""

from __future__ import annotations

import asyncio
from typing import Callable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .api.c2c import C2CAPI
from .api.group import GroupAPI
from .api.guild import GuildAPI
from .api.manage import ManageAPI
from .core import builders
from .core.auth import ADAPTER_NAMES
from .core.client import execute_call, send_rich_bound, upload_media_bound
from .core.errors import QQOfficeNotSupported, QQOfficeRoutingError
from .core.events import (
    EVENT_ID_SCOPES,
    AdapterPatcher,
    EventBus,
    QQOfficeEvent,
)
from .core.ratelimit import RateLimiter
from .core.refstore import RefStore
from .core.registry import Registry, collect_methods
from .core.routing import (
    BoundSource,
    EventSource,
    RobotKey,
    RobotState,
    RobotStates,
    RouteCore,
    RouteRecord,
)

PLUGIN_NAME = "astrbot_plugin_qqoffice_expand"


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = dict(config) if config else {}

        self.registry = Registry()
        self.event_bus = EventBus(self.config, logger)
        self.refstore: RefStore | None = None
        self._coordinator: asyncio.Task | None = None
        self._ready_flag = False
        self.patcher: AdapterPatcher | None = None
        self.routes: RouteCore | None = None
        self.states: RobotStates | None = None

        # 能力目录只构建一次：命名空间类上的未绑定函数（视图调用时以自身
        # 命名空间为 self，保留 helper/_scene；见 BoundView._ns）。
        self._build_namespaces()

        self.md = builders.md
        self.kb = builders.Keyboard
        self.btn = builders.btn
        self.reference = builders.reference
        self.md_image = builders.md_image

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        cfg = self.config
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            data_dir = None
        self.refstore = RefStore(
            data_dir, ttl_days=int(cfg.get("ref_ttl_days", 7) or 0),
            max_entries=int(cfg.get("ref_max_entries", 50000) or 50000),
        )
        # robot_key -> 认证主体限额覆盖（template_list 条目；同身份只读一份）。
        certified_map: dict[str, bool] = {}
        for entry in cfg.get("certified_bots") or []:
            if isinstance(entry, dict) and entry.get("appid"):
                certified_map[str(entry["appid"])] = bool(entry.get("certified", False))

        def _make_state(robot_key: RobotKey):
            certified = certified_map.get(
                robot_key.appid, bool(cfg.get("certified_bot", False))
            )
            return RobotState(robot_key, rate_limiter=RateLimiter(certified_bot=certified))

        self.states = RobotStates(factory=_make_state)
        self.routes = RouteCore(
            getattr(self.context, "platform_manager", None), self.states, logger
        )
        self.routes.bind_view_factory(lambda bound: BoundView(self, bound))
        self.patcher = AdapterPatcher(self.routes, self.event_bus, cfg, logger)
        self.patcher.set_inbound_recorder(self._record_inbound)
        self.patcher.install_adapter_hooks()  # 构造期注入，必须先于平台实例化
        self.event_bus.set_ack_caller(self._ack_interaction)
        self.event_bus.bind_routes(self.routes)

        self.patcher.refresh()          # 已在运行的实例立即挂载（含热安装重载）
        self._coordinator = asyncio.create_task(self._coordinator_loop())
        self._ready_flag = True
        logger.info(
            f"[qqoffice_expand] 加载完成（N 实例路由）：当前实例 "
            f"{sorted(self.routes.routes)}，命名方法 {len(self.registry.names())} 个"
        )

    async def terminate(self) -> None:
        self._ready_flag = False
        if self.routes is not None:
            self.routes.deactivate()   # 先拒绝新请求与在途核验，不触碰本体资源
        if self._coordinator and not self._coordinator.done():
            self._coordinator.cancel()
            try:
                await self._coordinator
            except (asyncio.CancelledError, Exception):
                pass
            self._coordinator = None
        if self.event_bus:
            await self.event_bus.stop()   # 取消并等待本插件拥有的事件/ACK 任务
        if self.patcher:
            await self.patcher.stop()     # 还原补丁、卸载构造期钩子、停用旧回调
        if self.refstore:
            self.refstore.close()
        logger.info("[qqoffice_expand] 已卸载（补丁还原、任务取消、refstore 落盘）")

    # ---------------- 实例视图 / 来源绑定 ----------------

    def instance(self, platform_id: str) -> "BoundView":
        """按配置实例 ID 取主动调用视图。

        创建时从本体当前运行索引解析并**立即固定**机器人身份（O(1)）：
        实例不可用直接抛 InstanceUnavailable；此后同 ID 改绑其他 AppID，
        该视图调用抛 InstanceIdentityChanged，需重新创建视图。插件加载早
        于平台实例化时，请在平台加载钩子/事件回调/后台任务中创建视图。
        """
        if self.routes is None:
            raise QQOfficeNotSupported("插件尚未完成 initialize")
        route = self.routes.ensure_current_route(platform_id)   # 本体权威，O(1)
        return BoundView(self, BoundSource.for_instance(platform_id, route.robot_key))

    def for_event(self, event) -> "BoundView":
        """从事件绑定来源视图：原生事件核验 platform_id+event.bot 并携带
        事件目标（send_rich 无需再传 event）；扩展事件用不可变来源。"""
        if self.routes is None:
            raise QQOfficeNotSupported("插件尚未完成 initialize")
        if isinstance(event, QQOfficeEvent):
            source: EventSource | None = getattr(event, "source", None)
            if source is None:
                raise QQOfficeNotSupported(
                    "该扩展事件未携带来源（由旧版挂载产生），请重载本插件以重建挂载"
                )
            view = BoundView(self, BoundSource.from_event_source(source))
            view.set_event_target(*_resolve_ext_event_target(event))
            return view
        platform_id = event.get_platform_id()
        bot = getattr(event, "bot", None)
        if bot is not None:
            # 来源核验必须针对本体当前对象：本地缓存可能落后于本体索引。
            route = self.routes.ensure_current_route(platform_id)
            if route.client is not bot:
                raise QQOfficeRoutingError(
                    f"事件来源 client 已不是 {platform_id!r} 的当前实例"
                )
            robot_key = route.robot_key
        else:
            route = self.routes.ensure_current_route(platform_id)
            robot_key = route.robot_key
        view = BoundView(self, BoundSource(platform_id, robot_key, source_client=bot))
        view.set_event_target(*_resolve_event_target(event))
        return view

    # ---------------- 协调任务 / 清理 ----------------

    COORDINATOR_INTERVAL = 15.0

    def refresh(self) -> None:
        """差异刷新：消费请求侧变化 + 全体扫描（钩子/巡检/订阅共用）。"""
        if self.patcher is None:
            return
        try:
            diff = self.routes.refresh_all()
        except Exception as exc:
            logger.error(f"[qqoffice_expand] 差异刷新失败: {exc!r}")
            return
        try:
            self.patcher.apply_diff(diff)
        except Exception as exc:
            logger.error(f"[qqoffice_expand] 补丁差异应用失败: {exc!r}")
        self._prune_idle_states()

    async def _coordinator_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.COORDINATOR_INTERVAL)
                self.refresh()
        except asyncio.CancelledError:
            raise

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        """本体钩子：平台实例化/重载后触发（无参调用）。只做差异刷新，
        不代表登录完成——传输就绪由请求时 token 判定。"""
        self.refresh()

    def _prune_idle_states(self) -> None:
        """先有界清理过期记录，再判断空闲回收；未过期用量与在途预留保留。"""
        for state in list(self.states):
            state.windows.prune_expired(limit=128)
            state.acks.prune_expired(limit=128)
            state.rate.prune_idle(limit=64)
            if state.idle():
                self.states.discard(state.key)

    # ---------------- 订阅 / 目录 / 状态 ----------------

    def on(self, event_type: str, handler) -> Callable:
        """全局订阅：接收全部实例的该事件。返回解绑闭包。"""
        unsub, is_first = self.event_bus.on(event_type, handler)
        if is_first and self.patcher is not None:
            self.patcher.refresh()
        return unsub

    def on_any(self, handler) -> Callable:
        """全局透传订阅。"""
        return self.event_bus.on_any(handler)

    @property
    def ready(self) -> bool:
        return self._ready_flag

    def _log(self, level: str, msg: str) -> None:
        getattr(logger, level)(f"[qqoffice_expand] {msg}")

    async def wait_ready(self, timeout: float = 60.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while not self._ready_flag:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def _build_namespaces(self) -> None:
        self._ns_types = {
            "group": GroupAPI, "c2c": C2CAPI, "guild": GuildAPI, "manage": ManageAPI,
        }
        self._catalog: dict[str, tuple] = {}
        for prefix, cls in self._ns_types.items():
            ns = cls(None)
            for name, fn in collect_methods(ns).items():
                self._catalog[f"{prefix}.{name}"] = (cls, fn)
                self.registry.register_fn(f"{prefix}.{name}", fn)

    def status(self) -> dict:
        instances = {}
        robots = {}
        if self.routes is not None:
            instances = {pid: r.snapshot() for pid, r in self.routes.routes.items()}
        if self.states is not None:
            robots = {k.prefix(): s.snapshot() for k, s in self.states.items()}
        return {
            "instances": instances,
            "robots": robots,
            "events": self.event_bus.status(),
            "patcher": self.patcher.status() if self.patcher else {},
            "refstore": self.refstore.snapshot() if self.refstore else {},
            "registry_methods": self.registry.names(),
            "config": {k: v for k, v in self.config.items()},
        }

    # ---------------- 引用 / 入库 / ACK ----------------

    def ref_from_event(self, event) -> str | None:
        """取事件消息的 REFIDX；按来源机器人命名空间隔离。"""
        if isinstance(event, QQOfficeEvent):
            source: EventSource | None = getattr(event, "source", None)
            if source is None or not (event.scene and event.message_id):
                return None
            openid = event.group_openid or event.user_openid
            return self.refstore.get_inbound(
                f"{source.robot_key.prefix()}|{event.scene}",
                str(openid), str(event.message_id),
            )
        if self.routes is None:
            return None
        scene, openid, msg_id = _resolve_event_target(event)
        if not (scene and openid and msg_id):
            return None
        try:
            # 原生事件核验来源（event.bot 仍是本体当前实例）；过期来源
            # 不能从新机器人命名空间取引用，也不能把旧 raw_data 写进新机器人。
            route = self.routes.resolve_current(BoundSource(
                event.get_platform_id(), None, source_client=getattr(event, "bot", None)))
        except Exception:
            return None
        namespaced_scene = f"{route.robot_key.prefix()}|{scene}"
        cached = self.refstore.get_inbound(namespaced_scene, openid, str(msg_id))
        if cached is not None:
            return cached
        # 缓存未命中：从本体保留的原始 payload 提取 msg_idx/ref 并入库
        # （本体 PatchedMessage.raw_data 是官方 d；AstrBotMessage.raw_message
        # 即该 message 对象）。不改原生 parser、不接管原生 on_xxx。
        raw_message = getattr(event.message_obj, "raw_message", None)
        raw = getattr(raw_message, "raw_data", None)
        if raw is None and isinstance(raw_message, dict):
            raw = raw_message
        if isinstance(raw, dict) and raw:
            self.refstore.record_inbound(
                namespaced_scene, str(openid), str(msg_id), raw
            )
        return self.refstore.get_inbound(namespaced_scene, openid, str(msg_id))

    def ref_outbound(self, view: "BoundView", scene: str, openid: str) -> str | None:
        """取指定视图机器人最近一条出站消息的 REFIDX。"""
        return self.refstore.get_outbound_latest(f"{view.prefix()}|{scene}", openid)

    def _record_inbound(self, ev: QQOfficeEvent, route: RouteRecord) -> None:
        raw = getattr(ev, "raw", None) or {}
        msg_id = raw.get("id")
        scene = ev.scene or ("group" if ev.group_openid else "c2c" if ev.user_openid else None)
        openid = ev.group_openid or ev.user_openid
        if msg_id and scene and openid:
            self.refstore.record_inbound(
                f"{route.robot_key.prefix()}|{scene}", str(openid), str(msg_id), raw
            )

    async def _ack_interaction(self, view: "BoundView", interaction_id: str,
                               phase=None) -> dict:
        kwargs = {"_phase": phase} if phase is not None else {}
        return await view.manage.interaction_ack(interaction_id, **kwargs)

    def _format_status(self) -> str:
        st = self.status()
        lines = [f"运行实例 {len(st['instances'])} 个，机器人身份 {len(st['robots'])} 个"]
        for pid, r in st["instances"].items():
            lines.append(
                f"  {pid}: {r['adapter']}/{r['mode']} appid={r['appid']} "
                f"代次={r['generation']} 就绪={r['transport_ready']}"
            )
        for key, s in st["robots"].items():
            lines.append(
                f"  身份 {key}: 窗口 {s['windows']['tracked_msg_ids']}，"
                f"ACK {s['acks']['total']}，频控等待 {s['rate']['total_rate_wait_seconds']}s"
            )
        patcher = st["patcher"]
        for c in patcher.get("instances", []):
            lines.append(
                f"  挂载 {c['platform_id']}: connected={c['connected']} "
                f"on_xxx={c['on_mounted']} intents={[b for b in c['intents_applied']]}"
            )
        ev = st["events"]
        lines.append(f"全局订阅: {ev['subscribers'] or '{}'} on_any={ev['any_subscribers']}")
        lines.append(f"实例作用域订阅: {ev.get('scoped_subscribers') or '{}'}")
        lines.append(f"事件到达计数: {ev['counts'] or '{}'} 自动应答 {ev['auto_acked']} 次")
        lines.append(f"命名方法 {len(st['registry_methods'])} 个")
        return "\n".join(lines)

    @filter.command("qqoffice_status")
    async def qqoffice_status(self, event: AstrMessageEvent):
        """查看 QQ 官方扩展能力的实例/挂载/订阅/频控状态。"""
        yield event.plain_result(self._format_status())


# ---------------- 绑定视图 ----------------


class BoundView:
    """绑定来源的调用视图：svc.for_event(event) / svc.instance(id) 的返回值。

    - 身份固定：创建时（for_event）或首次调用时（instance 视图经
      resolve_current）与本体当前实例核验并锁定 robot_key；同 ID 改绑
      其他 AppID 后明确失败，不会转给新机器人。
    - 命名空间：group/c2c/guild/manage 为视图专属实例，_client 指回视图，
      保留各命名空间的 helper/_scene（invoke 时在真实命名空间上调用）。
    - 事件目标：for_event 的视图持有 (scene, openid, msg_id)，send_rich
      无需再传 event。
    """

    def __init__(self, svc: Main, bound: BoundSource):
        self._svc = svc
        self._bound = bound
        self.group = GroupAPI(self)
        self.c2c = C2CAPI(self)
        self.guild = GuildAPI(self)
        self.manage = ManageAPI(self)
        self._ns_map = {GroupAPI: self.group, C2CAPI: self.c2c,
                        GuildAPI: self.guild, ManageAPI: self.manage}
        self._event_target: tuple[str | None, str | None, str | None] = (None, None, None)
        self._event_event_id: str | None = None
        self._event_event_id_source: str | None = None

    # -- 基本属性 --

    @property
    def platform_id(self) -> str:
        return self._bound.platform_id

    @property
    def robot_key(self) -> RobotKey:
        """当前绑定的机器人身份；视图未固定身份时惰性核验一次。"""
        self._ensure_identity()
        return self._bound.robot_key

    def prefix(self) -> str:
        return self.robot_key.prefix()

    def _ensure_identity(self) -> None:
        """身份已在视图创建时固定；此处仅做来源/代次/生命周期的轻量核验。"""
        self._svc.routes.resolve_current(self._bound)

    def set_event_target(self, scene: str | None, openid: str | None,
                         msg_id: str | None, event_id: str | None = None,
                         event_id_source: str | None = None) -> None:
        """事件回复目标：msg_id 为被动回复窗口键；event_id/event_id_source
        为官方 event_id 通道（已按 EVENT_ID_SCOPES 过滤的事件类型）。"""
        self._event_target = (scene, openid, msg_id)
        self._event_event_id = event_id
        self._event_event_id_source = event_id_source

    # -- 调用 --

    async def call(self, method: str, path: str, **kwargs):
        self._ensure_identity()
        return await execute_call(self._svc, self._bound, method, path, **kwargs)

    async def invoke(self, name: str, *args, **kwargs):
        """按注册名调用命名方法（如 view.invoke("group.recall", openid, mid)）。

        目录函数在视图自己的实际命名空间实例上调用（保留 helper/_scene），
        并发安全。
        """
        cls, fn = self._svc._catalog[name]
        ns = self._ns_map[cls]
        result = fn(ns, *args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    def on(self, event_type: str, handler) -> Callable:
        """实例作用域订阅：只接收本视图身份实例的该事件（跟随同身份重载）。

        首个订阅触发一次挂载刷新（按需置位 intents）。"""
        self._ensure_identity()
        unsub, _ = self._svc.event_bus.on_scoped(
            self._bound.platform_id, self._bound.robot_key, event_type, handler
        )
        self._svc.refresh()
        return unsub

    def on_any(self, handler) -> Callable:
        self._ensure_identity()
        unsub = self._svc.event_bus.on_scoped_any(
            self._bound.platform_id, self._bound.robot_key, handler
        )
        self._svc.refresh()
        return unsub

    # -- 便捷发送 --

    async def send_rich(self, *, content: str | None = None,
                        markdown: dict | None = None, keyboard: dict | None = None,
                        reference: dict | None = None, media=None,
                        file_type: int = 1, msg_id: str | None = None,
                        event_id: str | None = None,
                        event_id_source: str | None = None,
                        msg_seq: int | None = None, extra: dict | None = None,
                        on_mutex: str = "drop_reference",
                        scene: str | None = None,
                        target_openid: str | None = None) -> dict:
        """发送富消息。视图来自 for_event 时自动使用事件回复目标；
        视图来自 instance 时需显式传 scene+target_openid（主动消息）。

        上传与发送共用同一 OperationContext（中途重载不跨代次发送）。
        """
        self._ensure_identity()
        ev_scene, ev_openid, ev_msg_id = self._event_target
        if scene is None and target_openid is None and ev_scene:
            scene, target_openid, msg_id = ev_scene, ev_openid, (msg_id or ev_msg_id)
            if event_id is None:
                event_id = getattr(self, "_event_event_id", None)
            if event_id_source is None:
                event_id_source = getattr(self, "_event_event_id_source", None)
        return await send_rich_bound(
            self._svc, self._bound,
            scene=scene, target_openid=target_openid, content=content,
            markdown=markdown, keyboard=keyboard, reference=reference,
            media=media, file_type=file_type, msg_id=msg_id, event_id=event_id,
            event_id_source=event_id_source, msg_seq=msg_seq, extra=extra,
            on_mutex=on_mutex,
        )

    async def upload_media(self, scene: str, openid: str, source,
                           file_type: int = 1, srv_send_msg: bool = False) -> dict:
        self._ensure_identity()
        return await upload_media_bound(
            self._svc, self._bound, scene, openid, source, file_type, srv_send_msg
        )


def _resolve_ext_event_target(
    ev: QQOfficeEvent,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """扩展事件视图的回复目标 (scene, openid, msg_id, event_id, event_id_source)。

    - group/c2c：msg_id=官方消息 id（被动回复窗口，普通消息优先）。
    - event_id 仅在事件类型属于官方允许集合（EVENT_ID_SCOPES，如
      GROUP_ADD_ROBOT/GROUP_MSG_RECEIVE/INTERACTION_CREATE/FRIEND_ADD/
      C2C_MSG_RECEIVE）时才从 payload 最外层事件 id 推断，并携带事件类型
      作为 event_id_source，接受与显式传参相同的 EVENT_ID_SCOPES 校验；
      不在允许集合（如 GROUP_MEMBER_ADD、普通群消息）不伪造 event_id。
    - 频道/频道私信：目标为 channel/guild id，频道消息的 message_id 可作
      msg_id 被动回复（GuildAPI.send 支持 msg_id）。
    - 无目标的事件类型返回全 None，send_rich 需显式给 scene+target。
    """
    scene = ev.scene or ("group" if ev.group_openid else "c2c" if ev.user_openid else None)
    openid = ev.group_openid or ev.user_openid
    if scene in ("guild", "dm") or (ev.guild_id and not openid):
        is_dm = (ev.scene or "").startswith("DIRECT") or "DIRECT_MESSAGE" in ev.type
        target = ev.guild_id if is_dm else ev.channel_id
        msg_id = str(ev.message_id) if ev.message_id else None
        return ("dm" if is_dm else "guild"), target, msg_id, None, None
    if not (scene and openid):
        return None, None, None, None, None
    msg_id = str(ev.message_id) if ev.message_id else None
    event_id = None
    event_id_source = None
    if ev.type in EVENT_ID_SCOPES.get(scene, set()) and ev.payload_id:
        event_id = str(ev.payload_id)
        event_id_source = ev.type
    return scene, str(openid), msg_id, event_id, event_id_source


def _resolve_event_target(event) -> tuple[str | None, str | None, str | None]:
    """(scene, openid, msg_id)；非 QQ 官方平台返回 (None, None, None)。"""
    mo = getattr(event, "message_obj", None)
    platform = getattr(getattr(event, "platform", None), "name", "") or ""
    if platform and platform not in ADAPTER_NAMES:
        return None, None, None
    raw = getattr(mo, "raw_message", None)
    guild_id = raw.get("guild_id") if isinstance(raw, dict) else getattr(raw, "guild_id", None)
    channel_id = raw.get("channel_id") if isinstance(raw, dict) else getattr(raw, "channel_id", None)
    if guild_id:
        # AstrBot 将频道文字消息记为群消息、频道私信记为好友消息，需先看原始来源。
        scene, target = ("guild", channel_id) if getattr(mo, "group_id", None) else ("dm", guild_id)
        return scene, str(target) if target else None, getattr(mo, "message_id", None)
    group_id = getattr(mo, "group_id", None)
    if group_id:
        return "group", str(group_id), getattr(mo, "message_id", None)
    user_id = getattr(getattr(mo, "sender", None), "user_id", None)
    if user_id:
        return "c2c", str(user_id), getattr(mo, "message_id", None)
    return None, None, None
