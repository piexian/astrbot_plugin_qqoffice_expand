"""底座层：适配器发现（路径 A）与自建 HTTP（路径 B）。

路径 A 复用适配器的 botpy 客户端——两模式的 token 均由 botpy Token 自动
换取/刷新（ws 经 client.start；webhook 经 BotHttp 注入），本插件不自建鉴权。
路径 B 兜底：httpx 直连，token 从 bots.qq.com/app/getAppAccessToken 换取
（per-appId 缓存 + singleflight + 提前刷新）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import QQOfficeAPIError, from_http_response

__all__ = [
    "ADAPTER_NAMES",
    "QQClientBundle",
    "find_qq_clients",
    "find_qq_credentials",
    "resolve_domain",
    "apply_domain_to_botpy",
    "TokenManager",
    "SelfClient",
    "TOKEN_URL",
]

ADAPTER_NAMES = ("qq_official", "qq_official_webhook")

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
NEW_DOMAIN = "api.bot.qq.com"
OLD_DOMAIN = "api.sgroup.qq.com"
SANDBOX_DOMAIN = "sandbox.api.sgroup.qq.com"


def find_qq_credentials(context: Any) -> list[dict]:
    """从 AstrBot 全局配置里收集 QQ 官方平台的 appid/secret（路径 B 用）。

    适配器未实例化（未启用/加载失败）时也能拿到凭据，让 call() 通道仍可用。
    """
    out: list[dict] = []
    try:
        platforms = context.get_config().get("platform") or []
    except Exception:
        platforms = []
    for p in platforms:
        if not isinstance(p, dict) or str(p.get("type")) not in ADAPTER_NAMES:
            continue
        appid = str(p.get("appid") or "")
        secret = str(p.get("secret") or "")
        if appid and secret:
            out.append({
                "id": str(p.get("id") or appid),
                "appid": appid,
                "secret": secret,
                "enable": bool(p.get("enable", True)),
            })
    return out


def resolve_domain(*, prefer_new_domain: bool = False, sandbox: bool = False) -> str:
    if sandbox:
        return SANDBOX_DOMAIN
    return NEW_DOMAIN if prefer_new_domain else OLD_DOMAIN


def apply_domain_to_botpy(domain: str) -> bool:
    """把域名决策应用到 botpy Route（路径 A 域名兜底的唯一改动点）。

    botpy 1.2.1 http.py: Route.DOMAIN 为类属性且 url 属性经 self.DOMAIN 读取；
    沙箱走 Route(is_sandbox=True) 的 SANDBOX_DOMAIN，同样在类上改写。
    返回是否实际改动。
    """
    try:
        from botpy.http import Route  # noqa: 延迟导入，core 保持零 botpy 顶层依赖
    except Exception:  # pragma: no cover - botpy 缺失时走路径 B
        return False

    changed = False
    if not domain.startswith("sandbox."):
        if getattr(Route, "DOMAIN", None) != domain:
            Route.DOMAIN = domain
            changed = True
    if domain.startswith("sandbox."):
        if getattr(Route, "SANDBOX_DOMAIN", None) != domain:
            Route.SANDBOX_DOMAIN = domain
            changed = True
    return changed


@dataclass
class QQClientBundle:
    """一个 QQ 官方平台适配器实例的封装（路径 A 的操作对象）。"""

    inst: Any
    name: str                      # "qq_official" | "qq_official_webhook"
    mode: str                      # "ws" | "webhook"
    appid: str
    secret: str
    client: Any                    # botpy Client（适配器 botClient）
    instance_id: str = ""

    def get_api(self) -> Any:
        """botpy BotAPI（其 _http 即 BotHttp）。"""
        return getattr(self.client, "api", None)

    def is_connected(self) -> bool:
        try:
            return not bool(self.client.is_closed())
        except Exception:
            return False


def find_qq_clients(platform_manager: Any) -> list[QQClientBundle]:
    """遍历 platform_insts 按 meta().name 匹配 QQ 官方适配器（多实例全收）。

    ⚠️ 不要用 context.get_platform_inst("qq_official")——它按用户配置的实例
    id 匹配（astrbot/core/star/context.py:761-773），实例 id 是用户随便起的。
    """
    bundles: list[QQClientBundle] = []
    insts = list(getattr(platform_manager, "platform_insts", None) or [])
    for inst in insts:
        try:
            meta_name = inst.meta().name
        except Exception:
            continue
        if meta_name not in ADAPTER_NAMES:
            continue
        try:
            cfg = dict(getattr(inst, "config", None) or {})
        except Exception:
            cfg = {}
        appid = str(cfg.get("appid") or "")
        secret = str(cfg.get("secret") or "")
        try:
            client = inst.get_client()
        except Exception:
            client = getattr(inst, "client", None)
        if not client:
            continue
        bundles.append(
            QQClientBundle(
                inst=inst,
                name=meta_name,
                mode="webhook" if meta_name.endswith("_webhook") else "ws",
                appid=appid,
                secret=secret,
                client=client,
                instance_id=str(cfg.get("id") or getattr(inst, "id", "") or meta_name),
            )
        )
    return bundles


class TokenManager:
    """路径 B 的 token 管理：per-appId 缓存 + singleflight + 提前刷新。"""

    def __init__(self, appid: str, secret: str, timeout: float = 15.0):
        self.appid = appid
        self.secret = secret
        self.timeout = timeout
        self._token: str | None = None
        self._expire_at: float = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._token = None
        self._expire_at = 0.0

    @property
    def state(self) -> dict:
        remaining = max(0.0, self._expire_at - time.monotonic())
        return {"cached": bool(self._token), "remaining_seconds": round(remaining, 1)}

    async def get_token(self) -> str:
        # 提前刷新，避免并发下重复打 token 端点
        if self._token and self._expire_at - time.monotonic() > min(300.0, self._expire_at / 3.0):
            return self._token
        async with self._lock:
            if self._token and self._expire_at - time.monotonic() > min(300.0, self._expire_at / 3.0):
                return self._token
            await self._fetch()
            return self._token  # type: ignore[return-value]

    async def _fetch(self) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                TOKEN_URL,
                json={"appId": self.appid, "clientSecret": self.secret},
                headers={"Content-Type": "application/json"},
            )
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            err = from_http_response(resp.status_code, body, dict(resp.headers))
            if err:
                raise err
            token = str((body or {}).get("access_token") or "")
            if not token:
                raise QQOfficeAPIError(resp.status_code, f"token 响应缺少 access_token: {body}", raw=body)
            expires_in = float((body or {}).get("expires_in") or 7200)
            self._token = token
            self._expire_at = time.monotonic() + expires_in


class SelfClient:
    """路径 B：httpx 自建 HTTP，与 botpy 版本解耦。"""

    def __init__(
        self,
        appid: str,
        secret: str,
        *,
        domain: str = OLD_DOMAIN,
        timeout: float = 30.0,
        upload_timeout: float = 120.0,
    ):
        self.appid = appid
        self.secret = secret
        self.domain = domain
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.tokens = TokenManager(appid, secret)
        self._http: Any = None

    async def _ensure_http(self):
        import httpx

        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=f"https://{self.domain}",
                timeout=self.timeout,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any, dict]:
        """返回 (status, body(已尝试 json 解析), headers)。非 2xx 不在此抛错。"""
        client = await self._ensure_http()
        token = await self.tokens.get_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.appid,
        }
        resp = await client.request(
            method.upper(),
            path,
            params=params or None,
            json=json,
            headers=headers,
            timeout=timeout,
        )
        try:
            body: Any = resp.json()
        except Exception:
            body = resp.text
        return resp.status_code, body, dict(resp.headers)
