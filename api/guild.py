"""频道（guild）场景的官方接口封装（P2 起步集）。

频道消息发送走 /channels/{channel_id}/messages（embed/ark/md 为频道特有段）。
官方 wiki 已下线的频道管理接口（成员/身份组/禁言/公告/精华/日程/论坛）暂不
封装，避免编造路径；需全新增补时先核对官方文档再封装。
"""

from __future__ import annotations

from typing import Any

__all__ = ["GuildAPI"]


class GuildAPI:
    def __init__(self, client):
        self._client = client

    async def send(self, channel_id: str, *, content: str | None = None,
                   embed: dict | None = None, ark: dict | None = None,
                   markdown: dict | None = None, keyboard: dict | None = None,
                   message_reference: dict | None = None,
                   msg_id: str | None = None, msg_seq: int | None = None,
                   extra: dict | None = None) -> dict:
        """发送频道消息。文本与 embed/ark 互斥场景以官方校验为准；
        msg_id 传事件消息 id 即为被动回复。"""
        body: dict[str, Any] = dict(extra or {})
        if content is not None:
            body["content"] = content
        if embed is not None:
            body["embed"] = embed
        if ark is not None:
            body["ark"] = ark
        if markdown is not None:
            body["markdown"] = markdown
        if keyboard is not None:
            body["keyboard"] = keyboard
        if message_reference is not None:
            body["message_reference"] = message_reference
        if msg_id:
            body["msg_id"] = msg_id
            body.setdefault("msg_seq", msg_seq or 1)
        return await self._client.call(
            "POST", "/channels/{channel_id}/messages",
            path_params={"channel_id": channel_id}, json=body,
            scene="guild", target_openid=channel_id,
        )

    async def guild_info(self, guild_id: str, *, extra: dict | None = None) -> dict:
        """获取频道信息。"""
        return await self._client.call(
            "GET", "/guilds/{guild_id}",
            path_params={"guild_id": guild_id}, params=extra,
            scene="guild",
        )

    async def channel_info(self, channel_id: str, *, extra: dict | None = None) -> dict:
        """获取子频道信息。"""
        return await self._client.call(
            "GET", "/channels/{channel_id}",
            path_params={"channel_id": channel_id}, params=extra,
            scene="guild",
        )
