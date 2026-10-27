"""botpy Token 的跨重载 singleflight 与失效代次校验。"""

from __future__ import annotations

import asyncio
import time
import weakref
from contextvars import ContextVar

_STATE_ATTR = "_qqoffice_token_guard"


class _Snapshot:
    __slots__ = ("entry", "revision", "value")

    def __init__(self, entry, token):
        self.entry = entry
        self.revision = entry["revision"]
        self.value = token.access_token

    def __repr__(self):
        return "<TokenSnapshot>"


def _state(token_class):
    state = getattr(token_class, _STATE_ATTR, None)
    if state is None:
        state = {
            "owners": set(),
            "tokens": weakref.WeakKeyDictionary(),
            "patch": None,
            "checking": ContextVar("qqoffice_token_check", default=None),
        }
        setattr(token_class, _STATE_ATTR, state)
    return state


def _entry(state, token):
    entry = state["tokens"].get(token)
    if entry is None:
        entry = {"revision": 0, "task": None, "waiters": 0}
        state["tokens"][token] = entry
    return entry


def token_snapshot(token):
    """返回不含可打印凭据的代次快照；不支持的离线桩返回 None。"""
    state = getattr(type(token), _STATE_ATTR, None)
    if state is None or not hasattr(token, "access_token"):
        return None
    return _Snapshot(_entry(state, token), token)


def _expired(token):
    return token.access_token is None or int(time.time()) >= token.expires_in


async def _refresh(token, state, update, *, expected=None, check=False):
    entry = _entry(state, token)
    if expected is not None and (
        expected.entry is not entry
        or expected.revision != entry["revision"]
        or expected.value != token.access_token
    ):
        return
    task = entry["task"]
    if task is not None and task.cancelling():
        # 新调用不继承上一批等待者取消的刷新；先等其关闭资源。
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            pass
        if entry["task"] is task:
            entry["task"] = None
        return await _refresh(token, state, update, expected=expected, check=check)
    if task is None:
        if check and not _expired(token):
            return

        async def run():
            await update(token)
            entry["revision"] += 1

        task = asyncio.create_task(run())
        entry["task"] = task
    entry["waiters"] += 1
    try:
        await asyncio.shield(task)
    finally:
        entry["waiters"] -= 1
        if entry["waiters"] == 0 and entry["task"] is task:
            # 最后一个调用者取消时，资源释放必须随调用一起完成。
            if not task.done():
                task.cancel()
            try:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                try:
                    task.result()
                except BaseException:
                    pass
            finally:
                if entry["task"] is task:
                    entry["task"] = None


async def refresh_access_token(token, expected=None):
    """合并强制刷新；True 表示已处理，False 表示调用方应走桩兼容分支。"""
    state = getattr(type(token), _STATE_ATTR, None)
    if state is None or not hasattr(token, "access_token"):
        return False
    patch = state["patch"]
    if patch is None:
        return False
    await _refresh(token, state, patch["update"], expected=expected)
    return True


class TokenRefreshGuard:
    """按实例持有 SDK 类补丁，最后一个实例退出时恢复原方法。"""

    def __init__(self):
        self._owner = object()
        self._state = None
        self._token_class = None

    def install(self):
        if self._state is not None:
            return
        try:
            from botpy.robot import Token
        except (ImportError, AttributeError):
            return
        if not callable(getattr(Token, "check_token", None)) or not callable(
            getattr(Token, "update_access_token", None)
        ):
            return
        state = _state(Token)
        if not state["owners"]:
            check = Token.check_token
            update = Token.update_access_token
            patch = {"active": True, "check": check, "update": update}

            async def guarded_check(token):
                if not patch["active"]:
                    return await check(token)
                context = state["checking"].set(token)
                try:
                    return await check(token)
                finally:
                    state["checking"].reset(context)

            async def guarded_update(token):
                if not patch["active"]:
                    return await update(token)
                return await _refresh(
                    token, state, update, check=state["checking"].get() is token
                )

            patch["guarded_check"] = guarded_check
            patch["guarded_update"] = guarded_update
            state["patch"] = patch
            Token.check_token = guarded_check
            Token.update_access_token = guarded_update
        state["owners"].add(self._owner)
        self._state = state
        self._token_class = Token

    async def stop(self):
        state, token_class = self._state, self._token_class
        if state is None:
            return
        self._state = self._token_class = None
        state["owners"].discard(self._owner)
        if state["owners"]:
            return
        patch = state["patch"]
        state["patch"] = None
        patch["active"] = False
        if token_class.check_token is patch["guarded_check"]:
            token_class.check_token = patch["check"]
        if token_class.update_access_token is patch["guarded_update"]:
            token_class.update_access_token = patch["update"]
        # SDK 调用者仍可能在等刷新；卸载不取消它们拥有的工作。
        tasks = [
            entry["task"]
            for entry in list(state["tokens"].values())
            if entry["task"] is not None
        ]
        if tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks), return_exceptions=True
            )
