"""QQ 官方扩展的 SDK v1 服务门面，接入方式见 README。"""

from __future__ import annotations

import asyncio
import uuid

from . import builders

__all__ = [
    "SERVICE_API_VERSION",
    "FEATURES",
    "PluginServiceError",
    "ServiceClosedError",
    "QQOfficeService",
]

SERVICE_API_VERSION = 1

# 试点固定能力标识（共享约定第 5 节）；features 表示实现支持，
# 不代表账号已获平台权限或当前可执行。
FEATURES: tuple[str, ...] = (
    "qq.instance",
    "qq.events",
    "qq.rich_message",
    "qq.group",
    "qq.c2c",
    "qq.guild",
    "qq.manage",
)

# 实例快照允许输出的安全字段白名单（RouteRecord.snapshot 的子集；
# 不含 token/secret/config 与任何内部对象引用）。
_SAFE_INSTANCE_FIELDS = (
    "platform_id",
    "adapter",
    "mode",
    "appid",
    "environment",
    "generation",
    "transport_ready",
    "closing",
)


class PluginServiceError(RuntimeError):
    """SDK 门面基础错误：稳定 ``code``，调用方按 code 降级（不吞成成功）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ServiceClosedError(PluginServiceError):
    """服务正在关闭或已关闭（重载/卸载后旧服务永久失效）。"""

    def __init__(self, message: str = "qqoffice_expand 服务已关闭，请重新获取服务对象"):
        super().__init__("service_closed", message)


class QQOfficeService:
    """由插件托管生命周期的 QQ 官方扩展公开服务。"""

    api_version = SERVICE_API_VERSION

    def __init__(self, plugin):
        self._plugin = plugin
        self.instance_id = uuid.uuid4().hex  # 每次插件加载唯一；重载后改变
        self._state = "initializing"
        self._reason: str | None = None
        # 构建器为纯函数/类（产出普通 dict/str），不携带服务状态；
        # 关闭后仍可用于本地组装 payload（发送会经视图再次核验来源）。
        self.md = builders.md
        self.kb = builders.Keyboard
        self.btn = builders.btn
        self.reference = builders.reference
        self.md_image = builders.md_image

    # ---------------- 状态 ----------------

    @property
    def state(self) -> str:
        """当前服务状态：initializing/ready/unavailable/closing/closed。"""
        return self._state

    def get_status(self) -> dict:
        """本地状态快照（同步、无网络、不刷新路由）。

        ``ready`` 当且仅当 state 为 ready；根服务就绪不代表平台连接可用，
        平台可用性见 ``instances`` 实例快照（只含安全字段）。
        """
        return {
            "api_version": self.api_version,
            "instance_id": self.instance_id,
            "state": self._state,
            "ready": self._state == "ready",
            "reason": self._reason,
            "instances": self._instance_snapshots(),
        }

    def capabilities(self) -> dict:
        """能力声明（本地、无副作用；实现支持 ≠ 账号已获平台权限）。"""
        return {"api_version": self.api_version, "features": list(FEATURES)}

    async def wait_ready(self, timeout: float | None = None) -> dict:
        """等待根服务就绪；成功返回 get_status 同形快照。

        - 超时抛 TimeoutError（调用方应显式设置有界超时；None 表示不设界，
          由调用方自行承担）。
        - closing/closed 抛 PluginServiceError(code="service_closed")。
        - unavailable（initialize 失败）不会自动恢复，将一直等到超时。
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.0, timeout)
        while True:
            status = self.get_status()
            if status["ready"]:
                return status
            if status["state"] in ("closing", "closed"):
                raise ServiceClosedError()
            if deadline is not None and loop.time() >= deadline:
                raise TimeoutError(
                    f"qqoffice_expand 服务在 {timeout}s 内未就绪"
                    f"（state={status['state']} reason={status['reason']}）"
                )
            await asyncio.sleep(0.05)

    def _instance_snapshots(self) -> dict[str, dict]:
        """读取本地路由记录；实际调用仍须核验本体当前实例。"""
        routes = getattr(self._plugin, "routes", None)
        if routes is None:
            return {}
        snapshots: dict[str, dict] = {}
        for pid, record in list(routes.routes.items()):
            try:
                full = record.snapshot()
            except Exception:
                continue  # 单实例异常不阻塞状态查询
            snapshots[pid] = {k: full[k] for k in _SAFE_INSTANCE_FIELDS if k in full}
        return snapshots

    # ---------------- 生命周期钩子（由插件本体调用） ----------------

    def mark_ready(self) -> None:
        self._state, self._reason = "ready", None

    def mark_unavailable(self, reason: str) -> None:
        self._state, self._reason = "unavailable", reason

    def mark_closing(self) -> None:
        """terminate 一开始调用：立即拒绝新业务（在途操作由路由层收尾）。"""
        self._state, self._reason = "closing", "service_closed"

    def mark_closed(self) -> None:
        """terminate 清理完成后调用：旧服务永久失效；get_status 仍可查询。"""
        self._state, self._reason = "closed", "service_closed"

    # ---------------- 业务委托 ----------------

    def _check_open(self) -> None:
        if self._state in ("closing", "closed"):
            raise ServiceClosedError()

    def instance(self, platform_id: str):
        """按配置实例 ID 取主动调用视图（身份创建时固定；语义见 README）。"""
        self._check_open()
        return self._plugin.instance(platform_id)

    def for_event(self, event):
        """从事件绑定来源视图（原生事件核验来源；扩展事件用不可变来源）。"""
        self._check_open()
        return self._plugin.for_event(event)

    def on(self, event_type: str, handler):
        """全局订阅全部实例的该事件；返回解绑闭包（消费者 terminate 时解绑）。"""
        self._check_open()
        return self._plugin.on(event_type, handler)

    def on_any(self, handler):
        """全局透传订阅；返回解绑闭包。"""
        self._check_open()
        return self._plugin.on_any(handler)

    def ref_from_event(self, event) -> str | None:
        """取事件消息的 REFIDX（按来源机器人命名空间隔离）。"""
        self._check_open()
        return self._plugin.ref_from_event(event)

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return (
            f"<QQOfficeService api_version={self.api_version} "
            f"instance_id={self.instance_id} state={self._state}>"
        )

    # 注意：不定义 __getattr__，不透传任意属性；config/HTTP/路由器/令牌
    # 均不可经门面访问，根服务亦无 send_rich / group（必须先绑定视图）。
