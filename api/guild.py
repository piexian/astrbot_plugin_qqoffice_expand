"""频道管理与频道私信。契约与权限见 docs/API_COVERAGE.md。

返回官方对象/数组；分页、部分成功与私域权限由调用方按官方契约处理。
频道用户 ID、频道私信 guild_id 均不能与 QQ C2C OpenID 混用。
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
                   msg_id: str | None = None, event_id: str | None = None,
                   msg_seq: int | None = None,
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
        if event_id:
            body["event_id"] = event_id
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

    async def channels(self, guild_id: str, *, extra: dict | None = None) -> list:
        """子频道列表，响应为顶层数组。"""
        return await self._request("GET", "/guilds/{guild_id}/channels",
                                   {"guild_id": guild_id}, extra=extra)

    async def guilds(self, *, before: str | None = None, after: str | None = None,
                     limit: int = 100, extra: dict | None = None) -> list:
        """机器人加入的频道数组（50 QPS），limit 最大 100，before 与 after 同传时 before 优先。"""
        params = {k: v for k, v in (("before", before), ("after", after), ("limit", limit)) if v is not None}
        return await self._request("GET", "/users/@me/guilds", {}, params=params, extra=extra)

    async def channel_create(self, guild_id: str, name: str, type: int, *,
                             extra: dict | None = None) -> dict:
        """创建子频道（私域、管理员）。位置、私密成员等字段通过 extra 传入。"""
        return await self._request("POST", "/guilds/{guild_id}/channels",
                                   {"guild_id": guild_id}, body={"name": name, "type": type}, extra=extra)

    async def channel_update(self, channel_id: str, *, extra: dict) -> dict:
        """修改子频道：name/position/parent_id/private_type/speak_permission。"""
        return await self._request("PATCH", "/channels/{channel_id}",
                                   {"channel_id": channel_id}, extra=extra)

    async def channel_delete(self, channel_id: str, *, extra: dict | None = None) -> dict:
        """删除子频道（私域、管理员）。"""
        return await self._request("DELETE", "/channels/{channel_id}",
                                   {"channel_id": channel_id}, extra=extra)

    async def online_nums(self, channel_id: str, *, extra: dict | None = None) -> dict:
        """音视频或直播子频道在线人数。"""
        return await self._request("GET", "/channels/{channel_id}/online_nums",
                                   {"channel_id": channel_id}, extra=extra)

    async def members(self, guild_id: str, *, after: str = "0", limit: int = 1,
                      extra: dict | None = None) -> list:
        """频道成员数组（私域，limit 1..400）；after 取上页末尾 user.id，空数组结束。"""
        return await self._request("GET", "/guilds/{guild_id}/members", {"guild_id": guild_id},
                                   params={"after": after, "limit": limit}, extra=extra)

    async def member_info(self, guild_id: str, user_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("GET", "/guilds/{guild_id}/members/{user_id}",
                                   {"guild_id": guild_id, "user_id": user_id}, extra=extra)

    async def member_remove(self, guild_id: str, user_id: str, *, add_blacklist: bool = False,
                            delete_history_msg_days: int = 0, extra: dict | None = None) -> dict:
        """移除成员（私域、管理员）。历史消息天数可取 0/3/7/15/30/-1。"""
        return await self._request("DELETE", "/guilds/{guild_id}/members/{user_id}",
                                   {"guild_id": guild_id, "user_id": user_id},
                                   body={"add_blacklist": add_blacklist,
                                         "delete_history_msg_days": delete_history_msg_days}, extra=extra)

    async def roles(self, guild_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("GET", "/guilds/{guild_id}/roles", {"guild_id": guild_id}, extra=extra)

    async def role_create(self, guild_id: str, *, name: str | None = None,
                          color: int | None = None, hoist: int | None = None,
                          extra: dict | None = None) -> dict:
        """创建身份组；name/color/hoist 至少一项，color 为 uint32。"""
        body = {k: v for k, v in (("name", name), ("color", color), ("hoist", hoist)) if v is not None}
        return await self._request("POST", "/guilds/{guild_id}/roles", {"guild_id": guild_id},
                                   body=body, extra=extra)

    async def role_update(self, guild_id: str, role_id: str, *, extra: dict) -> dict:
        """修改身份组 name/color/hoist。"""
        return await self._request("PATCH", "/guilds/{guild_id}/roles/{role_id}",
                                   {"guild_id": guild_id, "role_id": role_id}, extra=extra)

    async def role_delete(self, guild_id: str, role_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("DELETE", "/guilds/{guild_id}/roles/{role_id}",
                                   {"guild_id": guild_id, "role_id": role_id}, extra=extra)

    async def role_members(self, guild_id: str, role_id: str, *, start_index: str = "0",
                           limit: int = 1, extra: dict | None = None) -> dict:
        """身份组成员（私域，limit 1..400），返回 data/next。"""
        return await self._request("GET", "/guilds/{guild_id}/roles/{role_id}/members",
                                   {"guild_id": guild_id, "role_id": role_id},
                                   params={"start_index": start_index, "limit": limit}, extra=extra)

    async def member_role_add(self, guild_id: str, user_id: str, role_id: str, *,
                              channel_id: str | None = None, extra: dict | None = None) -> dict:
        """给成员添加身份组；role_id=5 时必须指定子频道。"""
        return await self._member_role("PUT", guild_id, user_id, role_id, channel_id, extra)

    async def member_role_remove(self, guild_id: str, user_id: str, role_id: str, *,
                                 channel_id: str | None = None, extra: dict | None = None) -> dict:
        return await self._member_role("DELETE", guild_id, user_id, role_id, channel_id, extra)

    async def _member_role(self, method, guild_id, user_id, role_id, channel_id, extra):
        return await self._request(method, "/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
                                   {"guild_id": guild_id, "user_id": user_id, "role_id": role_id},
                                   body={"channel": {"id": channel_id}} if channel_id is not None else None,
                                   extra=extra)

    async def member_permissions(self, channel_id: str, user_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("GET", "/channels/{channel_id}/members/{user_id}/permissions",
                                   {"channel_id": channel_id, "user_id": user_id}, extra=extra)

    async def set_member_permissions(self, channel_id: str, user_id: str, *,
                                     add: str = "0", remove: str = "0", extra: dict | None = None) -> dict:
        """权限位图用字符串；同一位同时 add/remove 时 remove 优先。"""
        return await self._request("PUT", "/channels/{channel_id}/members/{user_id}/permissions",
                                   {"channel_id": channel_id, "user_id": user_id},
                                   body={"add": add, "remove": remove}, extra=extra)

    async def role_permissions(self, channel_id: str, role_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("GET", "/channels/{channel_id}/roles/{role_id}/permissions",
                                   {"channel_id": channel_id, "role_id": role_id}, extra=extra)

    async def set_role_permissions(self, channel_id: str, role_id: str, *,
                                   add: str = "0", remove: str = "0", extra: dict | None = None) -> dict:
        return await self._request("PUT", "/channels/{channel_id}/roles/{role_id}/permissions",
                                   {"channel_id": channel_id, "role_id": role_id},
                                   body={"add": add, "remove": remove}, extra=extra)

    async def mute(self, guild_id: str, *, mute_seconds: str | None = None,
                   mute_end_timestamp: str | None = None, user_ids: list[str] | None = None,
                   extra: dict | None = None) -> dict:
        """全员或批量禁言。user_ids 不传表示全员；批量响应仅列出成功成员。
        时间字段为秒数字符串，'0' 解除；mute_end_timestamp 优先。
        """
        body = {k: v for k, v in (("mute_seconds", mute_seconds),
                ("mute_end_timestamp", mute_end_timestamp), ("user_ids", user_ids)) if v is not None}
        return await self._request("PATCH", "/guilds/{guild_id}/mute", {"guild_id": guild_id},
                                   body=body, extra=extra)

    async def mute_member(self, guild_id: str, user_id: str, *, mute_seconds: str | None = None,
                          mute_end_timestamp: str | None = None, extra: dict | None = None) -> dict:
        body = {k: v for k, v in (("mute_seconds", mute_seconds),
                ("mute_end_timestamp", mute_end_timestamp)) if v is not None}
        return await self._request("PATCH", "/guilds/{guild_id}/members/{user_id}/mute",
                                   {"guild_id": guild_id, "user_id": user_id}, body=body, extra=extra)

    async def message_setting(self, guild_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("GET", "/guilds/{guild_id}/message/setting", {"guild_id": guild_id}, extra=extra)

    async def recall(self, channel_id: str, message_id: str, *, hidetip: bool = False,
                     extra: dict | None = None) -> dict:
        """撤回频道消息；hidetip 控制是否隐藏撤回提示。"""
        return await self._request("DELETE", "/channels/{channel_id}/messages/{message_id}",
                                   {"channel_id": channel_id, "message_id": message_id},
                                   params={"hidetip": str(hidetip).lower()}, extra=extra, query=True)

    async def dm_create(self, recipient_id: str, source_guild_id: str, *, extra: dict | None = None) -> dict:
        """创建频道私信会话，返回用于 dm_send 的 guild_id。"""
        return await self._request("POST", "/users/@me/dms", {},
                                   body={"recipient_id": recipient_id, "source_guild_id": source_guild_id}, extra=extra)

    async def dm_send(self, guild_id: str, content: str | None = None, *,
                      msg_id: str | None = None, event_id: str | None = None,
                      extra: dict | None = None) -> dict:
        """频道私信；guild_id 是私信会话 ID，不能传 C2C 用户 OpenID。"""
        body = dict(extra or {})
        for key, value in (("content", content), ("msg_id", msg_id), ("event_id", event_id)):
            if value is not None:
                body[key] = value
        return await self._client.call("POST", "/dms/{guild_id}/messages",
                                       path_params={"guild_id": guild_id}, json=body,
                                       scene="dm", target_openid=guild_id)

    async def dm_recall(self, guild_id: str, message_id: str, *, hidetip: bool = False,
                         extra: dict | None = None) -> dict:
        """只能撤回机器人自己发送的频道私信。"""
        params = {"hidetip": str(hidetip).lower()}
        params.update(extra or {})
        return await self._client.call("DELETE", "/dms/{guild_id}/messages/{message_id}",
                                       path_params={"guild_id": guild_id, "message_id": message_id},
                                       params=params, scene="dm")

    async def announce_create(self, guild_id: str, *, message_id: str | None = None,
                              channel_id: str | None = None, announces_type: int | None = None,
                              recommend_channels: list[dict] | None = None,
                              extra: dict | None = None) -> dict:
        """消息公告或推荐子频道公告（最多 3 个推荐，整体替换）。"""
        body = {k: v for k, v in (("message_id", message_id), ("channel_id", channel_id),
                ("announces_type", announces_type), ("recommend_channels", recommend_channels)) if v is not None}
        return await self._request("POST", "/guilds/{guild_id}/announces", {"guild_id": guild_id},
                                   body=body, extra=extra)

    async def announce_delete(self, guild_id: str, message_id: str, *, extra: dict | None = None) -> dict:
        """删除公告；message_id='all' 删除推荐子频道公告。"""
        return await self._request("DELETE", "/guilds/{guild_id}/announces/{message_id}",
                                   {"guild_id": guild_id, "message_id": message_id}, extra=extra)

    async def pins(self, channel_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("GET", "/channels/{channel_id}/pins", {"channel_id": channel_id}, extra=extra)

    async def pin_add(self, channel_id: str, message_id: str, *, extra: dict | None = None) -> dict:
        """添加精华消息，最多 20 条。"""
        return await self._request("PUT", "/channels/{channel_id}/pins/{message_id}",
                                   {"channel_id": channel_id, "message_id": message_id}, extra=extra)

    async def pin_remove(self, channel_id: str, message_id: str, *, extra: dict | None = None) -> dict:
        """移除精华消息；message_id='all' 清空。"""
        return await self._request("DELETE", "/channels/{channel_id}/pins/{message_id}",
                                   {"channel_id": channel_id, "message_id": message_id}, extra=extra)

    async def schedules(self, channel_id: str, *, since: int | None = None,
                        extra: dict | None = None) -> list:
        """日程数组，since 为毫秒时间戳，不传时查询当天。"""
        return await self._request("GET", "/channels/{channel_id}/schedules", {"channel_id": channel_id},
                                   params={"since": since} if since is not None else None, extra=extra)

    async def schedule_info(self, channel_id: str, schedule_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("GET", "/channels/{channel_id}/schedules/{schedule_id}",
                                   {"channel_id": channel_id, "schedule_id": schedule_id}, extra=extra)

    async def schedule_create(self, channel_id: str, schedule: dict, *, extra: dict | None = None) -> dict:
        """创建日程，schedule 时间戳为毫秒字符串；每管理员每日 10 次、每频道每日 100 次。"""
        return await self._request("POST", "/channels/{channel_id}/schedules", {"channel_id": channel_id},
                                   body={"schedule": schedule}, extra=extra)

    async def schedule_update(self, channel_id: str, schedule_id: str, schedule: dict, *,
                              extra: dict | None = None) -> dict:
        return await self._request("PATCH", "/channels/{channel_id}/schedules/{schedule_id}",
                                   {"channel_id": channel_id, "schedule_id": schedule_id},
                                   body={"schedule": schedule}, extra=extra)

    async def schedule_delete(self, channel_id: str, schedule_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("DELETE", "/channels/{channel_id}/schedules/{schedule_id}",
                                   {"channel_id": channel_id, "schedule_id": schedule_id}, extra=extra)

    async def reaction_add(self, channel_id: str, message_id: str, emoji_type: int, emoji_id: str, *,
                           extra: dict | None = None) -> dict:
        return await self._reaction("PUT", channel_id, message_id, emoji_type, emoji_id, extra=extra)

    async def reaction_remove(self, channel_id: str, message_id: str, emoji_type: int, emoji_id: str, *,
                              extra: dict | None = None) -> dict:
        return await self._reaction("DELETE", channel_id, message_id, emoji_type, emoji_id, extra=extra)

    async def reaction_users(self, channel_id: str, message_id: str, emoji_type: int, emoji_id: str, *,
                             cookie: str | None = None, limit: int | None = None,
                             extra: dict | None = None) -> dict:
        """返回 users/cookie/is_end；limit 默认 20、最大 50，仅首次请求设置。"""
        params = {k: v for k, v in (("cookie", cookie), ("limit", limit)) if v is not None}
        return await self._reaction("GET", channel_id, message_id, emoji_type, emoji_id, params=params, extra=extra)

    async def _reaction(self, method, channel_id, message_id, emoji_type, emoji_id, *, params=None, extra=None):
        return await self._request(method, "/channels/{channel_id}/messages/{message_id}/reactions/{type}/{id}",
                                   {"channel_id": channel_id, "message_id": message_id,
                                    "type": emoji_type, "id": emoji_id}, params=params, extra=extra)

    async def audio_control(self, channel_id: str, status: int, *, audio_url: str | None = None,
                            text: str | None = None, extra: dict | None = None) -> dict:
        """音频控制（需申请权限）：0开始/1暂停/2继续/3停止，仅开始时传 URL 和文本。"""
        body = {"status": status}
        for key, value in (("audio_url", audio_url), ("text", text)):
            if value is not None:
                body[key] = value
        return await self._request("POST", "/channels/{channel_id}/audio", {"channel_id": channel_id},
                                   body=body, extra=extra)

    async def mic_on(self, channel_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("PUT", "/channels/{channel_id}/mic", {"channel_id": channel_id}, extra=extra)

    async def mic_off(self, channel_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("DELETE", "/channels/{channel_id}/mic", {"channel_id": channel_id}, extra=extra)

    async def threads(self, channel_id: str, *, extra: dict | None = None) -> dict:
        """论坛主题列表（私域），返回 threads/is_finish；保留服务器返回的分页信息。"""
        return await self._request("GET", "/channels/{channel_id}/threads", {"channel_id": channel_id}, extra=extra)

    async def thread_info(self, channel_id: str, thread_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("GET", "/channels/{channel_id}/threads/{thread_id}",
                                   {"channel_id": channel_id, "thread_id": thread_id}, extra=extra)

    async def thread_create(self, channel_id: str, title: str, content: str, format: int = 1, *,
                            extra: dict | None = None) -> dict:
        """创建论坛主题（PUT，私域）。format：1文本/2HTML/3Markdown/4JSON。"""
        return await self._request("PUT", "/channels/{channel_id}/threads", {"channel_id": channel_id},
                                   body={"title": title, "content": content, "format": format}, extra=extra)

    async def thread_delete(self, channel_id: str, thread_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("DELETE", "/channels/{channel_id}/threads/{thread_id}",
                                   {"channel_id": channel_id, "thread_id": thread_id}, extra=extra)

    async def api_permissions(self, guild_id: str, *, extra: dict | None = None) -> dict:
        return await self._request("GET", "/guilds/{guild_id}/api_permission", {"guild_id": guild_id}, extra=extra)

    async def api_permission_demand(self, guild_id: str, channel_id: str, path: str, method: str,
                                    desc: str, *, extra: dict | None = None) -> dict:
        """发送接口授权链接给频道管理员，默认每频道每天 3 条。"""
        return await self._request("POST", "/guilds/{guild_id}/api_permission/demand", {"guild_id": guild_id},
                                   body={"channel_id": channel_id, "api_identify": {"path": path, "method": method},
                                         "desc": desc}, extra=extra)

    async def _request(self, method: str, path: str, path_params: dict, *, body: dict | None = None,
                       params: dict | None = None, extra: dict | None = None, query: bool = False):
        if method == "GET" or query:
            params = {**(params or {}), **(extra or {})}
        else:
            body = {**(body or {}), **(extra or {})}
        return await self._client.call(method, path, path_params=path_params,
                                       params=params or None, json=body, scene="guild")
