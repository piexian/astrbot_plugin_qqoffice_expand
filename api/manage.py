"""全局管理接口封装：自定义菜单 / 指令面板 / 互动应答 / 素材图片。全部为 bot 级管理动作。"""

from __future__ import annotations

from typing import Any

__all__ = ["ManageAPI"]

class ManageAPI:
    def __init__(self, client):
        self._client = client

    async def me(self, *, extra: dict | None = None) -> dict:
        """机器人自身信息（50 QPS），保留 share_url、welcome_msg 等原始字段。"""
        return await self._client.call("GET", "/users/@me", params=extra)

    async def interaction_ack(self, interaction_id: str, code: int = 0,
                              *, extra: dict | None = None, _phase=None) -> dict:
        """PUT /interactions/{interaction_id}：按钮/快捷菜单回调必须在 3 秒内
        应答，同一 id 只能应答一次。code: 0成功 1失败 2频繁 3重复 4无权限 5仅管理员。

        重复调用抛 QQOfficeDuplicateAck；自定义 code 应关闭自动应答。
        _phase 为内部参数：自动 ACK 的阶段跟踪器（进入 SDK 置位），调用方无需传。
        """
        body: dict[str, Any] = {"code": code}
        body.update(extra or {})
        kwargs = dict(
            path_params={"interaction_id": interaction_id},
            json=body, endpoint_key="interactions.ack",
        )
        if _phase is not None:
            kwargs["_phase"] = _phase
        return await self._client.call("PUT", "/interactions/{interaction_id}", **kwargs)

    async def menu_get(self, *, extra: dict | None = None) -> dict:
        """查询当前自定义菜单（30 QPM）。未设置时 menu 字段为空。"""
        return await self._client.call(
            "GET", "/v2/menu", params=extra, endpoint_key="menu.get",
        )

    async def menu_put(self, items: list[dict], *, extra: dict | None = None) -> dict:
        """全量覆盖自定义菜单（5 QPM）。items 每项：
        {type: switch/send_message/link/menu, name, send_message?/link?/sub_menu_items?/switch?}；
        最多 10 项，name ≤10 字符（汉字算 2），子菜单 ≤5 项且不支持 menu 类型。"""
        body: dict[str, Any] = {"menu": {"items": items}}
        body["menu"].update(extra or {})
        return await self._client.call(
            "PUT", "/v2/menu", json=body, endpoint_key="menu.put",
        )

    async def panel_list(self, scope: str, *, cursor: str = "", limit: int | None = None,
                         extra: dict | None = None) -> dict:
        """分页查询面板列表。scope: c2c/group/channel/dm（必填，否则 40030011）。"""
        params: dict[str, Any] = {"scope": scope}
        params.update(extra or {})
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return await self._client.call(
            "GET", "/v2/panels", params=params, endpoint_key="panels.list",
        )

    async def panel_create(self, scope: str, panel: dict, *, target_type: str | None = None,
                           user_openids: list[str] | None = None,
                           group_openids: list[str] | None = None,
                           extra: dict | None = None) -> dict:
        """创建面板。channel/dm 仅支持 target_type=all；specific 仅 c2c/group，
        关联对象单次 ≤20。panel: {items: [PanelItem], remark?}，单面板 ≤20 个元素。"""
        body: dict[str, Any] = {"scope": scope, "panel": panel}
        body.update(extra or {})
        if target_type:
            body["target_type"] = target_type
        if user_openids:
            body["user_openids"] = user_openids
        if group_openids:
            body["group_openids"] = group_openids
        return await self._client.call(
            "POST", "/v2/panels", json=body, endpoint_key="panels.write",
        )

    async def panel_get(self, panel_id: str, *, extra: dict | None = None) -> dict:
        """查询面板详情（含关联的 user/group openid 列表，≤1000 条）。"""
        return await self._client.call(
            "GET", "/v2/panels/{panel_id}",
            path_params={"panel_id": panel_id}, params=extra,
            endpoint_key="panels.get",
        )

    async def panel_update(self, panel_id: str, panel: dict, *, extra: dict | None = None) -> dict:
        """覆盖面板内容与备注（不影响已关联对象）。"""
        body: dict[str, Any] = {"panel": panel}
        body.update(extra or {})
        return await self._client.call(
            "PUT", "/v2/panels/{panel_id}",
            path_params={"panel_id": panel_id}, json=body,
            endpoint_key="panels.write",
        )

    async def panel_delete(self, panel_id: str, *, extra: dict | None = None) -> dict:
        """删除面板。"""
        return await self._client.call(
            "DELETE", "/v2/panels/{panel_id}",
            path_params={"panel_id": panel_id}, json=extra,
            endpoint_key="panels.write",
        )

    async def panel_target(self, panel_id: str, op: str, *,
                           user_openids: list[str] | None = None,
                           group_openids: list[str] | None = None,
                           extra: dict | None = None) -> dict:
        """增删面板关联对象（op: add/del，单次 ≤20）；channel/dm 全局面板不支持。"""
        body: dict[str, Any] = {"op": op}
        body.update(extra or {})
        if user_openids:
            body["user_openids"] = user_openids
        if group_openids:
            body["group_openids"] = group_openids
        return await self._client.call(
            "PUT", "/v2/panels/{panel_id}/target",
            path_params={"panel_id": panel_id}, json=body,
            endpoint_key="panels.target",
        )

    async def material_prepare(self, file_name: str, file_size: int, content_type: str,
                               md5: str, sha1: str, *, extra: dict | None = None) -> dict:
        """申请素材图片上传，返回 upload_id 与预签名 PUT URL（任务 4 小时内有效）。"""
        body: dict[str, Any] = {"file_name": file_name, "file_size": file_size,
                                "content_type": content_type, "md5": md5, "sha1": sha1}
        body.update(extra or {})
        return await self._client.call(
            "POST", "/v2/media-images/prepare", json=body, endpoint_key="material.api",
        )

    async def material_complete(self, upload_id: str, md5: str,
                                *, extra: dict | None = None) -> dict:
        """完成素材上传（md5 为 COS ETag/文件 MD5），返回 image_id 与 qqbot-media:// 引用。"""
        body: dict[str, Any] = {"upload_id": upload_id, "md5": md5}
        body.update(extra or {})
        return await self._client.call(
            "POST", "/v2/media-images/complete", json=body, endpoint_key="material.api",
        )

    async def material_list(self, *, cursor: str = "", extra: dict | None = None) -> dict:
        """有效素材图片列表（按上传时间倒序，next_cursor 为空即末页）。"""
        params: dict[str, Any] = dict(extra or {})
        if cursor:
            params["cursor"] = cursor
        return await self._client.call(
            "GET", "/v2/media-images", params=params or None, endpoint_key="material.api",
        )

    async def material_delete(self, image_id: str, *, extra: dict | None = None) -> dict:
        """删除素材图片并释放配额；已发送消息不受影响。"""
        return await self._client.call(
            "DELETE", f"/v2/media-images/{image_id}/delete",
            json=extra, endpoint_key="material.api",
        )

    async def material_upload(self, source) -> dict:
        """素材图片一步上传（本地文件/bytes/base64）：prepare→PUT→complete。

        返回 {image_id, url(qqbot-media://), ttl, preview_url}。
        """
        return await self._client.material_upload(source)
