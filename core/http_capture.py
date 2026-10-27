"""用共享 ContextVar 观察 SDK 响应，兼容插件重载和并发请求。"""

from contextlib import contextmanager
from contextvars import ContextVar

from .errors import from_http_response

_STATE_ATTR = "_qqoffice_response_capture"


def _state(http_module):
    state = getattr(http_module, _STATE_ATTR, None)
    if state is None:
        state = {
            "current": ContextVar("qqoffice_http_status", default=None),
            "users": 0,
            "installed": None,
            "previous": None,
        }
        setattr(http_module, _STATE_ATTR, state)
    return state


@contextmanager
def capture_response_status(http_module):
    original = getattr(http_module, "_handle_response", None)
    if original is None:
        yield {}
        return
    state = _state(http_module)
    if state["users"] == 0:
        current = state["current"]

        async def observed(response):
            try:
                result = await original(response)
            except Exception as exc:
                captured = current.get()
                if captured is not None:
                    captured["status"] = response.status
                    captured["headers"] = response.headers
                if (
                    captured is not None
                    and type(exc).__module__ == "botpy.errors"
                    and response.status >= 400
                ):
                    captured["body"] = await response.text()
                    captured["error"] = from_http_response(
                        response.status, captured["body"], response.headers
                    )
                raise
            captured = current.get()
            if captured is not None:
                captured["status"] = response.status
            return result

        state["previous"] = original
        state["installed"] = observed
        http_module._handle_response = observed
    state["users"] += 1
    captured = {}
    token = state["current"].set(captured)
    try:
        yield captured
    finally:
        state["current"].reset(token)
        state["users"] -= 1
        if state["users"] == 0:
            if http_module._handle_response is state["installed"]:
                http_module._handle_response = state["previous"]
            state["installed"] = state["previous"] = None
