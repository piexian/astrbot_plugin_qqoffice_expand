"""区分 botpy 1.2.1 的成功空响应与超时返回 None。

只在本插件请求存续期间观察 SDK 响应，不改变原响应处理器的返回值；
ContextVar 将并发请求的 HTTP 状态隔离，最后一个请求退出时恢复补丁。
"""

from contextlib import contextmanager
from contextvars import ContextVar

from .errors import from_http_response

_current: ContextVar[dict | None] = ContextVar("qqoffice_http_status", default=None)
_users = 0
_installed = None
_previous = None


@contextmanager
def capture_response_status(http_module):
    global _users, _installed, _previous
    if _users == 0:
        original = http_module._handle_response

        async def observed(response):
            try:
                result = await original(response)
            except Exception as exc:
                current = _current.get()
                # SDK 的 HTTP 异常只保留 message，原始业务 code 会丢失。
                # 此时 SDK 已读完正文，aiohttp.text() 复用缓存，不发起第二次请求。
                if current is not None and type(exc).__module__ == "botpy.errors" and response.status >= 400:
                    current["error"] = from_http_response(response.status, await response.text(), response.headers)
                raise
            current = _current.get()
            if current is not None:
                current["status"] = response.status
            return result

        _previous = original
        _installed = observed
        http_module._handle_response = observed
    _users += 1
    captured = {}
    token = _current.set(captured)
    try:
        yield captured
    finally:
        _current.reset(token)
        _users -= 1
        if _users == 0:
            if http_module._handle_response is _installed:
                http_module._handle_response = _previous
            _installed = _previous = None
