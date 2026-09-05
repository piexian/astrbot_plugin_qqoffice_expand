"""群聊场景的官方接口封装：一个方法对应一个官方端点。

所有方法保留 extra: dict 原样并入请求体，官方加新字段时调用方直接透传、本插件零改动。
openid 均为群 OpenID；message_id 为群消息 ID。
"""

from __future__ import annotations

from typing import Any
from .media import MediaAPI

__all__ = ["GroupAPI"]

class GroupAPI(MediaAPI):
    _scene = "group"

    def __init__(self, client):
        self._client = client

    async def send(self, openid: str, content: str, *, msg_id: str | None = None,
                   event_id: str | None = None, msg_seq: int | None = None,
                   extra: dict | None = None) -> dict:
        """发送群文本消息（msg_type=0）。富消息用视图的 send_rich。"""
        body: dict[str, Any] = {"msg_type": 0, "content": content}
        body.update(extra or {})
        return await self._client.call(
            "POST", "/v2/groups/{group_openid}/messages",
            path_params={"group_openid": openid}, json=body,
            scene="group", target_openid=openid,
            msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
        )

    async def recall(self, openid: str, message_id: str, *, extra: dict | None = None) -> dict:
        """撤回群消息（2 分钟内；管理员可撤他人）。"""
        return await self._client.call(
            "DELETE", "/v2/groups/{group_openid}/messages/{message_id}",
            path_params={"group_openid": openid, "message_id": message_id},
            json=extra, scene="group", target_openid=openid,
        )

    async def upload(self, openid: str, source, file_type: int = 1, *,
                     srv_send_msg: bool = False) -> dict:
        """上传群富媒体（1图/2视频/3语音/4文件），返回含 file_info。"""
        return await self._client.upload_media("group", openid, source, file_type, srv_send_msg)

    async def info(self, openid: str, *, extra: dict | None = None) -> dict:
        """获取群信息（30 QPM，注意缓存）。"""
        return await self._client.call(
            "GET", "/v2/groups/{group_openid}/info",
            path_params={"group_openid": openid}, params=extra,
            scene="group", target_openid=openid,
        )

    async def bot_state(self, openid: str, *, extra: dict | None = None) -> dict:
        """机器人在群内的状态（可发消息性等）。"""
        return await self._client.call(
            "GET", "/v2/groups/{group_openid}/bot_state",
            path_params={"group_openid": openid}, params=extra,
            scene="group", target_openid=openid,
        )

    async def set_mute(self, openid: str, members: list[dict], *, extra: dict | None = None) -> dict:
        """设置群禁言。members 每项 {op: add/update/del, member_openid, mute_expire_at?}，
        单次 ≤10 人；机器人需群管理员。"""
        body: dict[str, Any] = {"members": members}
        body.update(extra or {})
        return await self._client.call(
            "POST", "/v2/groups/{group_openid}/restrict_chat_setting",
            path_params={"group_openid": openid}, json=body,
            scene="group", target_openid=openid,
        )

    async def mute_member(self, openid: str, member_openid: str, expire_at: str) -> dict:
        """禁言单个成员（mute_expire_at 为 RFC3339，如 2026-08-30T11:23:05+08:00，≤30 天）。"""
        return await self.set_mute(openid, [
            {"op": "add", "member_openid": member_openid, "mute_expire_at": expire_at}
        ])

    async def unmute_member(self, openid: str, member_openid: str) -> dict:
        """解除单个成员禁言。"""
        return await self.set_mute(openid, [
            {"op": "del", "member_openid": member_openid, "mute_expire_at": ""}
        ])

    async def get_mute(self, openid: str, *, extra: dict | None = None) -> dict:
        """查询群禁言状态（全员模式 + 成员禁言列表）。"""
        return await self._client.call(
            "GET", "/v2/groups/{group_openid}/restrict_chat_setting",
            path_params={"group_openid": openid}, params=extra,
            scene="group", target_openid=openid,
        )

    async def members(self, openid: str, *, cursor: str = "",
                      extra: dict | None = None) -> dict:
        """群成员列表（内邀，60 QPM）。每页最多 30 人，next_cursor 为空时结束。"""
        params: dict[str, Any] = dict(extra or {})
        if cursor:
            params["cursor"] = cursor
        return await self._client.call(
            "GET", "/v2/groups/{group_openid}/members",
            path_params={"group_openid": openid}, params=params or None,
            scene="group", target_openid=openid,
        )

    async def member_info(self, openid: str, member_openid: str, *,
                          extra: dict | None = None) -> dict:
        """指定群成员信息（内邀，30 QPM），含 member_role、username、joined_at。"""
        return await self._client.call(
            "GET", "/v2/groups/{group_openid}/members/{member_openid}",
            path_params={"group_openid": openid, "member_openid": member_openid},
            params=extra, scene="group", target_openid=openid,
        )

    async def remove_members(self, openid: str, member_openids: list[str], *,
                             add_to_member_blacklist: bool = False,
                             extra: dict | None = None) -> dict:
        """批量移除（内邀，30 QPM，≤20 人）。移除成功仍可能有拉黑失败名单。"""
        body = {"member_openids": member_openids,
                "add_to_member_blacklist": add_to_member_blacklist}
        body.update(extra or {})
        return await self._client.call(
            "POST", "/v2/groups/{group_openid}/batch_remove_members",
            path_params={"group_openid": openid}, json=body,
            scene="group", target_openid=openid,
        )

    async def blacklist(self, openid: str, *, cursor: str = "", limit: int | None = None,
                        extra: dict | None = None) -> dict:
        """群黑名单（内邀，30 QPM）。limit 默认 20、最大 100，返回 users/next_cursor。"""
        params: dict[str, Any] = dict(extra or {})
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return await self._client.call(
            "GET", "/v2/groups/{group_openid}/member_blacklist",
            path_params={"group_openid": openid}, params=params or None,
            scene="group", target_openid=openid,
        )

    async def set_blacklist(self, openid: str, op: str, member_openids: list[str], *,
                            extra: dict | None = None) -> dict:
        """增删黑名单（内邀，60 QPM）：add/del，≤20 人；add 的目标须已不在群中。
        返回 fail_openids，调用方应检查部分失败。
        """
        body = {"op": op, "member_openids": member_openids}
        body.update(extra or {})
        return await self._client.call(
            "POST", "/v2/groups/{group_openid}/member_blacklist",
            path_params={"group_openid": openid}, json=body,
            scene="group", target_openid=openid,
        )

    async def join_requests(self, openid: str, *, cursor: str = "", limit: int | None = None,
                            extra: dict | None = None) -> dict:
        """拉取入群申请列表（30 QPM）。返回 {list: [JoinRequest], next_cursor}。"""
        params: dict[str, Any] = dict(extra or {})
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return await self._client.call(
            "GET", "/v2/groups/{group_openid}/join_request_list",
            path_params={"group_openid": openid}, params=params or None,
            scene="group", target_openid=openid,
        )

    async def join_approve(self, openid: str, member_openid: str, *, op: str = "approve",
                           join_request_id: str | None = None, reject_reason: str | None = None,
                           add_to_member_blacklist: bool | None = None,
                           extra: dict | None = None) -> dict:
        """审批入群申请：approve/decline；decline 可带理由与拉黑。"""
        body: dict[str, Any] = {"op": op}
        if join_request_id:
            body["join_request_id"] = join_request_id
        if reject_reason:
            body["reject_reason"] = reject_reason
        if add_to_member_blacklist is not None:
            body["add_to_member_blacklist"] = add_to_member_blacklist
        body.update(extra or {})
        return await self._client.call(
            "POST", "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
            path_params={"group_openid": openid, "member_openid": member_openid},
            json=body, scene="group", target_openid=openid,
        )

    async def strategy_list(self, *, cursor: str = "", limit: int | None = None,
                            extra: dict | None = None) -> dict:
        params = dict(extra or {})
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return await self._client.call(
            "GET", "/v2/groups/join_approval_strategy", params=params or None, scene="group",
        )

    async def strategy_create(self, *, group_openids: list[str] | None = None,
                              group_ids: list | None = None, is_enable: str | None = None,
                              expire_at: str | None = None, remark: str | None = None,
                              extra: dict | None = None) -> dict:
        """创建策略：group_openids 与 group_ids 二选一（互斥），各最多 100 个；
        一个机器人最多 20 个策略。"""
        body: dict[str, Any] = dict(extra or {})
        if group_openids:
            body["group_openids"] = group_openids
        if group_ids:
            body["group_ids"] = group_ids
        if is_enable:
            body["is_enable"] = is_enable
        if expire_at:
            body["expire_at"] = expire_at
        if remark:
            body["remark"] = remark
        return await self._client.call(
            "POST", "/v2/groups/join_approval_strategy", json=body, scene="group",
        )

    async def strategy_update(self, strategy_id: str, *, is_enable: str | None = None,
                              expire_at: str | None = None, remark: str | None = None,
                              group_action: dict | None = None,
                              extra: dict | None = None) -> dict:
        """修改策略（PATCH）：group_action 为 {"op": "add|del", "group_openids": [...]
        或 "group_ids": [...]}，群标识形式须与创建时一致；单次只能 add 或 del。"""
        body: dict[str, Any] = dict(extra or {})
        for k, v in (("is_enable", is_enable), ("expire_at", expire_at), ("remark", remark)):
            if v is not None:
                body[k] = v
        if group_action:
            body["group_action"] = group_action
        return await self._client.call(
            "PATCH", "/v2/groups/join_approval_strategy/{strategy_id}",
            path_params={"strategy_id": strategy_id}, json=body, scene="group",
        )

    async def strategy_delete(self, strategy_id: str, *, extra: dict | None = None) -> dict:
        return await self._client.call(
            "DELETE", "/v2/groups/join_approval_strategy/{strategy_id}",
            path_params={"strategy_id": strategy_id}, json=extra, scene="group",
        )

    async def strategy_execute(self, strategy_id: str, *, extra: dict | None = None) -> dict:
        """手动执行一次策略。"""
        return await self._client.call(
            "POST", "/v2/groups/join_approval_strategy/{strategy_id}/execute",
            path_params={"strategy_id": strategy_id}, json=extra or {}, scene="group",
        )

    async def strategy_whitelist(self, strategy_id: str, op: str, whitelist_users: list[str],
                                 *, extra: dict | None = None) -> dict:
        """增删策略白名单 QQ 号（op: add/del，单次 ≤10000）。"""
        body: dict[str, Any] = {"op": op, "whitelist_users": whitelist_users}
        body.update(extra or {})
        return await self._client.call(
            "POST", "/v2/groups/join_approval_strategy/{strategy_id}/whitelist_users",
            path_params={"strategy_id": strategy_id}, json=body, scene="group",
        )
