"""底座层：适配器名称与域名解析（供文档与 botpy 域名改写）。

路径 B（自建 HTTP/token 兜底）已随 N 实例路由移除：请求一律通过本体
适配器当前的 botpy HTTP（token/刷新/关闭全部由本体管理）。
"""

from __future__ import annotations

__all__ = ["ADAPTER_NAMES", "resolve_domain", "apply_domain_to_botpy"]

ADAPTER_NAMES = ("qq_official", "qq_official_webhook")

NEW_DOMAIN = "api.bot.qq.com"
OLD_DOMAIN = "api.sgroup.qq.com"
SANDBOX_DOMAIN = "sandbox.api.sgroup.qq.com"


def resolve_domain(*, prefer_new_domain: bool = False, sandbox: bool = False) -> str:
    if sandbox:
        return SANDBOX_DOMAIN
    return NEW_DOMAIN if prefer_new_domain else OLD_DOMAIN


def apply_domain_to_botpy(domain: str) -> bool:
    """把域名决策应用到 botpy Route（可选的全局域名兜底）。

    botpy 1.2.1 http.py: Route.DOMAIN 为类属性且 url 属性经 self.DOMAIN 读取；
    沙箱走 Route(is_sandbox=True) 的 SANDBOX_DOMAIN，同样在类上改写。
    返回是否实际改动。
    """
    try:
        from botpy.http import Route
    except Exception:  # pragma: no cover
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
