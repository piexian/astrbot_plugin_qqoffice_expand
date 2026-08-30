"""astrbot_plugin_qqoffice_expand — QQ 官方机器人扩展能力中台。

其他插件接入方式见 README（get_registered_star 取 star_cls 作为 svc）。
"""

from __future__ import annotations

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .api.c2c import C2CAPI
from .api.group import GroupAPI
from .api.guild import GuildAPI
from .api.manage import ManageAPI
from .core import builders
from .core.auth import (
    ADAPTER_NAMES,
    SelfClient,
    apply_domain_to_botpy,
    find_qq_clients,
    find_qq_credentials,
    resolve_domain,
)
from .core.client import QQOfficeClient
from .core.errors import QQOfficeNotSupported
from .core.events import (
    EVENT_ID_SCOPES,
    AdapterPatcher,
    EventBus,
    make_first_subscribe_hook,
)
from .core.ratelimit import RateLimiter
from .core.ready import ReadySignal
from .core.refstore import RefStore
from .core.registry import Registry, collect_methods

PLUGIN_NAME = "astrbot_plugin_qqoffice_expand"

class _UnavailableClient:
    """无适配器且无凭据时的占位客户端：所有调用显式失败，调用方可捕获降级。"""

    mode = "none"

    async def call(self, *args, **kwargs) -> dict:
        raise QQOfficeNotSupported(
            "未发现 QQ 官方平台适配器（qq_official / qq_official_webhook），"
            "且全局配置中无可用 appid/secret（路径 B 不可用）"
        )

    async def upload_media(self, *args, **kwargs) -> dict:
        raise QQOfficeNotSupported("未发现 QQ 官方平台适配器，无法上传富媒体")

    def status(self) -> dict:
        return {"mode": "none"}

class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = dict(config) if config else {}

        self.registry = Registry()
        self.event_bus = EventBus(self.config, logger)
        self.patcher: AdapterPatcher | None = None

        self.refstore: RefStore | None = None
        self.limiter: RateLimiter | None = None
        self._ready = ReadySignal()
        self.clients: dict[str, QQOfficeClient] = {}      # 适配器实例（路径 A）
        self.self_clients: list[QQOfficeClient] = []      # 自建 HTTP（路径 B）
        self.primary: QQOfficeClient | _UnavailableClient = _UnavailableClient()

        self.group = GroupAPI(self.primary)
        self.c2c = C2CAPI(self.primary)
        self.guild = GuildAPI(self.primary)
        self.manage = ManageAPI(self.primary)

        self.md = builders.md
        self.kb = builders.Keyboard
        self.btn = builders.btn
        self.reference = builders.reference
        self.md_image = builders.md_image

        self._watch_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        cfg = self.config
        domain = resolve_domain(
            prefer_new_domain=bool(cfg.get("prefer_new_domain", False)),
            sandbox=bool(cfg.get("sandbox", False)),
        )
        apply_domain_to_botpy(domain)

        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            data_dir = None
        self.refstore = RefStore(
            data_dir, ttl_days=int(cfg.get("ref_ttl_days", 7) or 0)
        )
        self.limiter = RateLimiter(certified_bot=bool(cfg.get("certified_bot", False)))

        # 路径 B：凭据取自全局平台配置，适配器未启用时也常备
        for cred in find_qq_credentials(self.context):
            self_client = SelfClient(cred["appid"], cred["secret"], domain=domain)
            self.self_clients.append(
                QQOfficeClient(
                    self_client=self_client, rate_limiter=self.limiter,
                    refstore=self.refstore, config=cfg, logger=logger,
                )
            )

        self.patcher = AdapterPatcher(
            getattr(self.context, "platform_manager", None),
            self.event_bus, cfg, logger,
        )
        self.patcher.set_inbound_recorder(self._record_inbound)
        self.event_bus.set_ack_caller(self._ack_interaction)
        self.event_bus._on_first_subscribe = make_first_subscribe_hook(self.patcher)

        self._refresh_clients()
        self.patcher.start()
        self._watch_task = asyncio.create_task(self._watch_clients())
        self._ready.set()
        logger.info(
            f"[qqoffice_expand] 加载完成（ready=True，框架将广播 OnPluginLoadedEvent）："
            f"primary={self.primary.mode}，命名方法 {len(self.registry.names())} 个，域名 {domain}"
        )

    async def terminate(self) -> None:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.patcher:
            await self.patcher.stop()
        for client in self.self_clients:
            if client.self_client is not None:
                await client.self_client.close()
        if self.refstore:
            self.refstore.close()
        logger.info("[qqoffice_expand] 已卸载（patch 还原、任务取消、refstore 落盘）")

    def _refresh_clients(self) -> None:
        """发现新适配器实例即建路径 A 客户端；primary 优先适配器，其次自建。"""
        pm = getattr(self.context, "platform_manager", None)
        for bundle in find_qq_clients(pm) if pm else []:
            if bundle.instance_id in self.clients:
                continue
            self.clients[bundle.instance_id] = QQOfficeClient(
                bundle=bundle, rate_limiter=self.limiter,
                refstore=self.refstore, config=self.config, logger=logger,
            )
            logger.info(f"[qqoffice_expand] 发现适配器实例: {bundle.instance_id} ({bundle.name}/{bundle.mode})")

        adapter_clients = [c for c in self.clients.values() if c.mode == "adapter"]
        chosen = adapter_clients[0] if adapter_clients else (
            self.self_clients[0] if self.self_clients else self.primary
        )
        if chosen is not self.primary:
            self.primary = chosen
            self._build_namespaces()

    def _build_namespaces(self) -> None:
        client = self.primary
        self.group = GroupAPI(client)
        self.c2c = C2CAPI(client)
        self.guild = GuildAPI(client)
        self.manage = ManageAPI(client)
        for prefix, ns in (
            ("group", self.group), ("c2c", self.c2c),
            ("guild", self.guild), ("manage", self.manage),
        ):
            for name, fn in collect_methods(ns).items():
                self.registry.register_fn(f"{prefix}.{name}", fn)

    async def _watch_clients(self) -> None:
        """适配器可能在插件之后实例化/重载，轮询保持客户端与挂载新鲜。"""
        while True:
            await asyncio.sleep(10)
            self._refresh_clients()

    async def call(self, *args, **kwargs) -> dict:
        """通用 call 通道，签名见 core/client.py。"""
        return await self.primary.call(*args, **kwargs)

    async def invoke(self, name: str, *args, **kwargs):
        """按注册名调用命名方法，如 svc.invoke("group.recall", openid, mid)。"""
        return await self.registry.invoke(name, *args, **kwargs)

    def on(self, event_type: str, handler) -> callable:
        """订阅官方扩展事件；返回解绑闭包。"""
        return self.event_bus.on(event_type, handler)

    def on_any(self, handler) -> callable:
        """订阅全部已挂载事件（透传通道）。"""
        return self.event_bus.on_any(handler)

    @property
    def ready(self) -> bool:
        """initialize 是否已完成。框架 OnPluginLoadedEvent 广播时本值必为 True，
        依赖方据此区分「已实例化但未就绪」与「可直接绑定」。"""
        return self._ready.is_ready

    async def wait_ready(self, timeout: float = 60.0) -> bool:
        """等待本插件就绪；仅供后台任务场景轮询使用（见 core/ready.py 说明）。"""
        return await self._ready.wait(timeout)

    @staticmethod
    def _resolve_event_target(event: AstrMessageEvent) -> tuple[str | None, str | None, str | None]:
        """(scene, openid, msg_id)；非 QQ 官方平台返回 (None, None, None)。"""
        mo = getattr(event, "message_obj", None)
        platform = getattr(getattr(event, "platform", None), "name", "") or ""
        if platform and platform not in ADAPTER_NAMES:
            return None, None, None
        group_id = getattr(mo, "group_id", None)
        if group_id:
            return "group", str(group_id), getattr(mo, "message_id", None)
        user_id = getattr(getattr(mo, "sender", None), "user_id", None)
        if user_id:
            return "c2c", str(user_id), getattr(mo, "message_id", None)
        return None, None, None

    async def send_rich(
        self,
        event: AstrMessageEvent | None = None,
        *,
        scene: str | None = None,
        target_openid: str | None = None,
        content: str | None = None,
        markdown: dict | None = None,
        keyboard: dict | None = None,
        reference: dict | None = None,
        media=None,
        file_type: int = 1,
        msg_id: str | None = None,
        event_id: str | None = None,
        event_id_source: str | None = None,
        msg_seq: int | None = None,
        extra: dict | None = None,
        on_mutex: str = "drop_reference",
    ) -> dict:
        """发送富消息：markdown / keyboard / reference / media 直走 call()，
        绕开 AstrBot 消息序列化对 keyboard/reference 的丢弃。

        - event 与 scene+target_openid 二选一；前者被动回复（msg_id 自动补），
          后者纯主动消息。
        - markdown × message_reference 互斥：on_mutex="drop_reference"
          （默认，丢引用保 markdown）或 "text_reference"（markdown 降级纯文本保引用）。
        - event_id 需官方支持事件（群：INTERACTION_CREATE/GROUP_ADD_ROBOT/
          GROUP_MSG_RECEIVE；C2C：INTERACTION_CREATE/C2C_MSG_RECEIVE/FRIEND_ADD）；
          传 event_id_source 时校验，越界即丢弃并告警。
        - media 传 url/base64/本地路径（自动上传换 file_info）或
          {"file_info": ...}（已上传直传）。
        """
        if event is not None:
            ev_scene, ev_openid, ev_msg_id = self._resolve_event_target(event)
            if ev_scene is None:
                raise QQOfficeNotSupported(
                    f"当前事件平台不是 QQ 官方适配器（{ADAPTER_NAMES}），send_rich 不可用"
                )
            scene = scene or ev_scene
            target_openid = target_openid or ev_openid
            msg_id = msg_id or ev_msg_id
        if scene not in ("group", "c2c") or not target_openid:
            raise QQOfficeNotSupported(
                "send_rich 需要 scene+target_openid（group/c2c）；频道场景请用 svc.guild.send"
            )

        if event_id and event_id_source:
            allowed = EVENT_ID_SCOPES.get(scene, set())
            if event_id_source.upper() not in allowed:
                logger.warning(
                    f"[qqoffice_expand] event_id 事件 {event_id_source} 不在 {scene} 场景官方支持范围，已丢弃"
                )
                event_id = None

        if markdown and reference:
            if on_mutex == "text_reference":
                md_content = (markdown.get("markdown") or {}).get("content")
                if md_content and not content:
                    content = md_content
                markdown = None
                logger.info("[qqoffice_expand] markdown 与 reference 互斥：已降级纯文本并保留引用")
            else:
                reference = None
                logger.warning("[qqoffice_expand] markdown 与 reference 互斥：已丢弃 reference（on_mutex 可改）")

        payload: dict = dict(extra or {})
        if media is not None:
            if not (isinstance(media, dict) and media.get("file_info")):
                resp = await self.primary.upload_media(scene, target_openid, media, file_type)
                media = {"file_info": resp.get("file_info")}
            payload.setdefault("msg_type", 7)
            payload["media"] = media
        else:
            payload.setdefault("msg_type", 2 if markdown else 0)
        if content:
            payload["content"] = content
        if markdown:
            payload.update(markdown)
        if keyboard:
            payload.update(keyboard)
        if reference:
            payload.update(reference)

        path = ("/v2/groups/{group_openid}/messages" if scene == "group"
                else "/v2/users/{user_openid}/messages")
        key = "group_openid" if scene == "group" else "user_openid"
        return await self.primary.call(
            "POST", path, path_params={key: target_openid}, json=payload,
            scene=scene, target_openid=target_openid,
            msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
        )

    def ref_from_event(self, event: AstrMessageEvent) -> str | None:
        """取收到的消息的 REFIDX（引用它作为被动引用回复用）。"""
        scene, openid, msg_id = self._resolve_event_target(event)
        if not (scene and openid and msg_id):
            return None
        return self.refstore.get_inbound(scene, openid, str(msg_id))

    def ref_outbound(self, scene: str, openid: str) -> str | None:
        """取机器人最近一条出站消息的 REFIDX（引用自己的消息用）。"""
        return self.refstore.get_outbound_latest(scene, openid)

    def _record_inbound(self, ev) -> None:
        raw = getattr(ev, "raw", None) or {}
        msg_id = raw.get("id")
        scene = ev.scene or ("group" if ev.group_openid else "c2c" if ev.user_openid else None)
        openid = ev.group_openid or ev.user_openid
        if msg_id and scene and openid:
            self.refstore.record_inbound(scene, str(openid), str(msg_id), raw)

    async def _ack_interaction(self, interaction_id: str) -> dict:
        return await self.manage.interaction_ack(interaction_id)

    async def upload_media(self, scene: str, openid: str, source, file_type: int = 1) -> dict:
        """上传富媒体，返回含 file_info 的原始响应。"""
        return await self.primary.upload_media(scene, openid, source, file_type)

    def status(self) -> dict:
        primary = self.primary
        return {
            "primary_mode": primary.mode,
            "adapter_clients": {k: c.status() for k, c in self.clients.items()},
            "self_clients": len(self.self_clients),
            "events": self.event_bus.status(),
            "patcher": self.patcher.status() if self.patcher else {},
            "registry_methods": self.registry.names(),
            "config": {k: v for k, v in self.config.items()},
        }

    def _format_status(self) -> str:
        st = self.status()
        lines = [
            "QQOffice Expand 状态",
            f"primary 通道: {st['primary_mode']}",
        ]
        for iid, c in st["adapter_clients"].items():
            lines.append(
                f"  适配器 {iid}: mode={c['mode']} appid={c.get('appid', '')} "
                f"调用 {c['calls_total']} 次, 频控等待 {c['rate']['total_rate_wait_seconds']}s"
            )
        lines.append(f"路径 B 自建客户端: {st['self_clients']} 个")
        patcher = st["patcher"]
        for c in patcher.get("clients", []):
            lines.append(
                f"  挂载 {c['instance_id']}: connected={c['connected']} "
                f"on_xxx={c['on_mounted']} intents={[b for b in c['intents_applied']]}"
            )
        lines.append(f"类级补 parser: {patcher.get('class_parsers', [])}")
        ev = st["events"]
        lines.append(f"事件订阅: {ev['subscribers'] or '{}'} on_any={ev['any_subscribers']}")
        lines.append(f"事件到达计数: {ev['counts'] or '{}'} 自动应答 {ev['auto_acked']} 次")
        lines.append(f"命名方法 {len(st['registry_methods'])} 个")
        return "\n".join(lines)

    @filter.command("qqoffice_status")
    async def qqoffice_status(self, event: AstrMessageEvent):
        """查看 QQ 官方扩展能力的适配器发现/挂载/订阅/频控状态。"""
        yield event.plain_result(self._format_status())
