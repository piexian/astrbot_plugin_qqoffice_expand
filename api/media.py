"""群聊与 C2C 共用的分片上传控制接口；预签名 URL 的 PUT 由调用方完成。"""

from typing import Any


class MediaAPI:
    _scene: str

    async def upload_prepare(self, openid: str, *, file_type: int, file_size: int | str,
                             file_name: str, md5: str, sha1: str, md5_10m: str,
                             extra: dict | None = None) -> dict:
        """准备分片（10 QPS）。md5_10m 是前 10002432 字节 MD5。
        返回 upload_id、parts（含预签名 URL）、block_size 与 upload_config。
        """
        body = {"file_type": file_type, "file_size": str(file_size), "file_name": file_name,
                "md5": md5, "sha1": sha1, "md5_10m": md5_10m}
        body.update(extra or {})
        return await self._media_call(openid, "upload_prepare", body)

    async def upload_part_finish(self, openid: str, upload_id: str, part_index: int, *,
                                 block_size: int | str, md5: str,
                                 extra: dict | None = None) -> dict:
        """预签名 URL 的 PUT 成功后确认该片（10 QPS），block_size 为实际分片字节数。"""
        body = {"upload_id": upload_id, "part_index": part_index,
                "block_size": str(block_size), "md5": md5}
        body.update(extra or {})
        return await self._media_call(openid, "upload_part_finish", body)

    async def upload_complete(self, openid: str, upload_id: str, *, file_type: int = 1,
                              file_name: str | None = None, srv_send_msg: bool = False,
                              extra: dict | None = None) -> dict:
        """全部分片确认后合并，返回 file_info；srv_send_msg=True 直接发送主动消息。"""
        body: dict[str, Any] = {"upload_id": upload_id, "file_type": file_type,
                                "srv_send_msg": srv_send_msg}
        if file_name is not None:
            body["file_name"] = file_name
        body.update(extra or {})
        return await self._media_call(openid, "files", body)

    async def _media_call(self, openid: str, suffix: str, body: dict) -> dict:
        prefix = "groups" if self._scene == "group" else "users"
        return await self._client.call(
            "POST", f"/v2/{prefix}/{{openid}}/{suffix}",
            path_params={"openid": openid}, json=body,
            scene=self._scene, target_openid=openid,
        )
