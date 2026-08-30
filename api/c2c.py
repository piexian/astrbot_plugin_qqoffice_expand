"""单聊（C2C）场景的官方接口封装：一个方法对应一个官方端点。

C2C 与群在被动窗口/输入状态/上传端点等维度行为均不同，独立成模块。
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["C2CAPI", "InputNotifyHandle"]

_INPUT_KEEPALIVE_INTERVAL = 50.0  # input_second 上限 60s，每 50s 续一次


class InputNotifyHandle:
    """「输入中」状态的 keepalive 句柄；用完必须 cancel()。"""

    def __init__(self, task: asyncio.Task):
        self._task = task

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    def done(self) -> bool:
        return self._task is None or self._task.done()


class C2CAPI:
    def __init__(self, client):
        self._client = client

    async def send(self, openid: str, content: str, *, msg_id: str | None = None,
                   event_id: str | None = None, msg_seq: int | None = None,
                   extra: dict | None = None) -> dict:
        """发送单聊文本消息（msg_type=0）。富消息用 svc.send_rich。
        被动窗口 60 分钟/4 次（与群 5 分钟/5 次不同）。"""
        body: dict[str, Any] = {"msg_type": 0, "content": content}
        body.update(extra or {})
        return await self._client.call(
            "POST", "/v2/users/{user_openid}/messages",
            path_params={"user_openid": openid}, json=body,
            scene="c2c", target_openid=openid,
            msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
        )

    async def recall(self, openid: str, message_id: str, *, extra: dict | None = None) -> dict:
        """撤回单聊消息。"""
        return await self._client.call(
            "DELETE", "/v2/users/{user_openid}/messages/{message_id}",
            path_params={"user_openid": openid, "message_id": message_id},
            json=extra, scene="c2c", target_openid=openid,
        )

    async def upload(self, openid: str, source, file_type: int = 1, *,
                     srv_send_msg: bool = False) -> dict:
        """上传单聊富媒体（1图/2视频/3语音/4文件）；语音自动剥 AMR 头。"""
        return await self._client.upload_media("c2c", openid, source, file_type, srv_send_msg)

    async def input_notify(self, openid: str, *, seconds: int = 60, input_type: int = 1,
                           msg_id: str | None = None, keepalive: bool = False,
                           extra: dict | None = None) -> dict | InputNotifyHandle:
        """发送「输入中」状态（msg_type=6，input_second ≤60）。

        keepalive=True 时每 50s 自动续发，返回可 cancel 的句柄；
        False 时只发一次，返回原始响应。
        """
        body: dict[str, Any] = {
            "msg_type": 6,
            "input_notify": {"input_type": input_type, "input_second": min(60, int(seconds))},
        }
        body.update(extra or {})

        async def _once() -> dict:
            return await self._client.call(
                "POST", "/v2/users/{user_openid}/messages",
                path_params={"user_openid": openid}, json=dict(body),
                scene="c2c", target_openid=openid, msg_id=msg_id,
            )

        if not keepalive:
            return await _once()

        async def _loop():
            while True:
                try:
                    await _once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 续发失败不打断循环，下个周期重试
                    pass
                await asyncio.sleep(_INPUT_KEEPALIVE_INTERVAL)

        return InputNotifyHandle(asyncio.create_task(_loop()))

    async def wakeup(self, openid: str, content: str, *, extra: dict | None = None) -> dict:
        """互动召回消息（is_wakeup=true）：30 天 4 周期各 1 条；
        与 msg_id/event_id/msg_seq 互斥，纯主动消息。"""
        body: dict[str, Any] = {"msg_type": 0, "content": content, "is_wakeup": True}
        body.update(extra or {})
        body.pop("msg_id", None)
        body.pop("event_id", None)
        body.pop("msg_seq", None)
        return await self._client.call(
            "POST", "/v2/users/{user_openid}/messages",
            path_params={"user_openid": openid}, json=body,
            scene="c2c", target_openid=openid,
        )
