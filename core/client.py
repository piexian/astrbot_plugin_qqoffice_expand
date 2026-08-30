"""通用 call 通道（扩展位核心），路径 A/B 在此收敛。

内建：端点频控、主动消息配额、无状态 msg_seq、被动窗口自动降级（群 5min/5
次、C2C 60min/4 次）、401/11244 清 token 重试一次、429/40034100/40034101
等待重试、HTML 网关页识别、错误标准化、出站 ref_idx 入引用索引、群文本
点号替换（可选）。超时按路径自适应：含 /files、upload_ 的 120s，其余 30s。
"""

from __future__ import annotations

import asyncio
import base64
import random
import re as _re
import time
from collections import deque
from typing import Any

from . import errors as qo_errors
from .auth import QQClientBundle, SelfClient
from .errors import QQOfficeAPIError, QQOfficeGatewayError, QQOfficeNotSupported
from .ratelimit import RateLimiter
from .refstore import RefStore

__all__ = ["QQOfficeClient", "PassiveWindowTracker", "generate_msg_seq"]

DEFAULT_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 120.0  # 上传类接口平台侧耗时，普通 30s 会误超时

_DOT_RE = _re.compile(r"([A-Za-z0-9\u4e00-\u9fff])\.([A-Za-z0-9\u4e00-\u9fff])")

def generate_msg_seq() -> int:
    """无状态生成，规避相同 msg_id+msg_seq 的平台去重报错。"""
    return (int(time.time() * 1000) % 100_000_000) ^ random.getrandbits(16)

class PassiveWindowTracker:
    """per msg_id 被动回复窗口与次数（官方限制：群 5min/5 次、C2C 60min/4 次）。"""

    LIMITS = {"group": (300.0, 5), "c2c": (3600.0, 4)}
    _GC_TTL = 3600.0

    def __init__(self):
        self._records: dict[str, tuple[float, int, float, str]] = {}

    def check(self, scene: str, msg_id: str) -> tuple[bool, str]:
        """(是否仍可被动回复, 原因)。"""
        window, max_count = self.LIMITS.get(scene, (0.0, 0))
        if not window:
            return True, ""
        rec = self._records.get(msg_id)
        if rec is None:
            return True, ""
        first_ts, count, last_ts, _ = rec
        now = time.monotonic()
        if now - first_ts > window:
            return False, "窗口已过期"
        if count >= max_count:
            return False, f"已达被动回复上限（{max_count} 次）"
        return True, ""

    def record(self, scene: str, msg_id: str) -> None:
        if scene not in self.LIMITS or not msg_id:
            return
        now = time.monotonic()
        rec = self._records.get(msg_id)
        if rec is None or now - rec[0] > self.LIMITS[scene][0]:
            self._records[msg_id] = (now, 1, now, scene)
        else:
            self._records[msg_id] = (rec[0], rec[1] + 1, now, scene)
        self._gc()

    def _gc(self) -> None:
        if len(self._records) < 512:
            return
        now = time.monotonic()
        for k in [k for k, v in self._records.items() if now - v[2] > self._GC_TTL]:
            self._records.pop(k, None)

    def stats(self) -> dict:
        return {"tracked_msg_ids": len(self._records)}

# 端点 key 解析表（顺序敏感；新端点在此加一行即可）
_ENDPOINT_PATTERNS: list[tuple[_re.Pattern, str]] = [
    (_re.compile(r"/messages/\{[^}]+\}$"), "{scene}.recall"),
    (_re.compile(r"/messages$"), "{scene}.send"),
    (_re.compile(r"/files$"), "{scene}.files"),
    (_re.compile(r"upload_prepare$"), "group.upload_prepare"),
    (_re.compile(r"upload_part_finish$"), "group.upload_part_finish"),
    (_re.compile(r"/interactions/\{[^}]+\}$"), "interactions.ack"),
    (_re.compile(r"/bot_state$"), "{scene}.bot_state"),
    (_re.compile(r"/join_request_list$"), "group.join_requests"),
    (_re.compile(r"/approval_join_request/"), "group.join_approve"),
    (_re.compile(r"/restrict_chat_setting$"), "{scene}.mute_get_or_set"),
    (_re.compile(r"join_approval_strategy"), "group.strategy"),
    (_re.compile(r"/info$"), "{scene}.info"),
    (_re.compile(r"/v2/menu$"), "menu.get_or_put"),
    (_re.compile(r"/v2/panels/\{[^}]+\}/target$"), "panels.target"),
    (_re.compile(r"/v2/panels/\{[^}]+\}$"), "panels.get_or_write_or_del"),
    (_re.compile(r"/v2/panels$"), "panels.list_or_create"),
]

def resolve_endpoint_key(method: str, path: str, scene: str | None) -> str:
    key_tpl: str | None = None
    for pattern, tpl in _ENDPOINT_PATTERNS:
        if pattern.search(path):
            key_tpl = tpl
            break
    if key_tpl is None:
        return "default"
    if key_tpl == "{scene}.mute_get_or_set":
        return f"{scene or 'group'}.mute_get" if method.upper() == "GET" else f"{scene or 'group'}.mute_set"
    if key_tpl == "menu.get_or_put":
        return "menu.get" if method.upper() == "GET" else "menu.put"
    if key_tpl == "panels.get_or_write_or_del":
        return "panels.get" if method.upper() == "GET" else "panels.write"
    if key_tpl == "panels.list_or_create":
        return "panels.list" if method.upper() == "GET" else "panels.write"
    if key_tpl == "{scene}.mute_get" :
        return key_tpl
    return key_tpl.format(scene=scene) if "{scene}" in key_tpl else key_tpl

class QQOfficeClient:
    """面向其他插件的通用请求通道；路径 A/B 细节全部在此收敛。"""

    def __init__(
        self,
        *,
        bundle: QQClientBundle | None = None,
        self_client: SelfClient | None = None,
        rate_limiter: RateLimiter,
        refstore: RefStore,
        config: dict | None = None,
        logger=None,
    ):
        if (bundle is None) == (self_client is None):
            raise ValueError("bundle 与 self_client 必须二选一")
        self.bundle = bundle
        self.self_client = self_client
        self.limiter = rate_limiter
        self.refstore = refstore
        self.config = dict(config or {})
        self.logger = logger
        self.mode = "adapter" if bundle is not None else "self"
        self.windows = PassiveWindowTracker()
        self.recent_errors: deque[dict] = deque(maxlen=10)
        self.calls_total = 0

    @property
    def appid(self) -> str:
        return self.bundle.appid if self.bundle else (self.self_client.appid if self.self_client else "")

    def _log(self, level: str, msg: str) -> None:
        if self.logger:
            getattr(self.logger, level)(f"[qqoffice_expand] {msg}")

    def _record_error(self, err: Exception) -> None:
        self.recent_errors.append({"ts": time.strftime("%H:%M:%S"), "err": repr(err)[:200]})

    async def call(
        self,
        method: str,
        path: str,
        *,
        path_params: dict | None = None,
        params: dict | None = None,
        json: dict | None = None,
        scene: str | None = None,
        target_openid: str | None = None,
        endpoint_key: str | None = None,
        msg_id: str | None = None,
        event_id: str | None = None,
        msg_seq: int | None = None,
        timeout: float | None = None,
    ) -> dict:
        """调用任意官方端点，返回原始响应 dict（错误抛 QQOfficeAPIError 系）。"""
        method = method.upper()
        path_params = dict(path_params or {})
        try:
            full_path = path.format_map(_SafeDict(path_params))
        except ValueError as e:
            raise QQOfficeNotSupported(f"路径参数展开失败: {path} {path_params} ({e})")
        missing = _re.findall(r"\{(\w+)\}", full_path)
        if missing:
            raise QQOfficeNotSupported(f"路径缺少参数 {missing}: {path}")

        endpoint_key = endpoint_key or resolve_endpoint_key(method, full_path, scene)
        self.calls_total += 1

        payload = dict(json or {})
        is_send = endpoint_key.endswith(".send")
        degraded = False
        if is_send:
            payload, degraded = await self._prepare_message_payload(
                payload, scene, target_openid, msg_id, event_id, msg_seq
            )
            if degraded:
                self._log("warning", f"被动窗口失效，已降级为主动消息: target={target_openid}")

        await self.limiter.acquire(endpoint_key)

        if timeout is None:
            timeout = UPLOAD_TIMEOUT if ("/files" in full_path or "upload_" in full_path) else DEFAULT_TIMEOUT

        token_retry_used = False
        rate_attempts = 0
        retry_max = int(self.config.get("retry_max", 3) or 0)
        while True:
            try:
                result = await self._dispatch(method, full_path, params, payload if payload else None, timeout)
            except QQOfficeAPIError as exc:
                self._record_error(exc)
                if not token_retry_used and (exc.code == 401 or exc.code in qo_errors.TOKEN_EXPIRED_CODES):
                    token_retry_used = True
                    await self._refresh_token()
                    continue
                if exc.code == 429 or exc.code in qo_errors.RATE_LIMITED_CODES:
                    if rate_attempts < retry_max:
                        rate_attempts += 1
                        await asyncio.sleep(min(30.0, 1.0 * rate_attempts))
                        continue
                raise
            except QQOfficeGatewayError as exc:
                self._record_error(exc)
                if rate_attempts < retry_max:
                    rate_attempts += 1
                    await asyncio.sleep(min(15.0, 2.0 * rate_attempts))
                    continue
                raise
            break

        if isinstance(result, dict):
            refstore = self.refstore
            if is_send and scene and target_openid:
                refstore.record_outbound(scene, target_openid, result)
            if is_send:
                used_msg_id = payload.get("msg_id")
                if used_msg_id:
                    self.windows.record(scene or "group", used_msg_id)
        return result if isinstance(result, dict) else {}

    async def _prepare_message_payload(
        self,
        payload: dict,
        scene: str | None,
        target_openid: str | None,
        msg_id: str | None,
        event_id: str | None,
        msg_seq: int | None,
    ) -> tuple[dict, bool]:
        """补 msg_seq、处理被动窗口降级、点号替换；返回 (payload, 是否降级)。"""
        payload = dict(payload)
        degraded = False

        # 被动通道：msg_id（窗口内）或 event_id（官方支持事件范围由调用方保证）
        if msg_id and scene in PassiveWindowTracker.LIMITS:
            ok, reason = self.windows.check(scene, msg_id)
            if not ok:
                if self.config.get("auto_degrade_proactive", True):
                    msg_id = None
                    degraded = True
                else:
                    raise QQOfficeAPIError(40034005, f"被动回复窗口失效（{reason}），且未开启自动降级")
        if msg_id:
            payload["msg_id"] = msg_id
            payload.setdefault("msg_seq", msg_seq or generate_msg_seq())
        elif event_id:
            payload["event_id"] = event_id

        # 纯主动消息才消耗配额（被动回复不受主动配额限制）
        if "msg_id" not in payload and "event_id" not in payload and not payload.get("is_wakeup"):
            if target_openid:
                await self.limiter.consume_proactive(str(target_openid))

        if (
            self.config.get("dot_replace", False)
            and scene == "group"
            and isinstance(payload.get("content"), str)
        ):
            payload["content"] = _DOT_RE.sub(r"\1_\2", payload["content"])
        return payload, degraded

    async def _dispatch(self, method: str, full_path: str, params: dict | None,
                        payload: dict | None, timeout: float) -> dict:
        if self.bundle is not None:
            return await self._dispatch_adapter(method, full_path, params, payload, timeout)
        return await self._dispatch_self(method, full_path, params, payload, timeout)

    async def _dispatch_adapter(self, method: str, full_path: str, params: dict | None,
                                payload: dict | None, timeout: float) -> dict:
        try:
            from botpy.http import Route  # 延迟导入，保持 core 零顶层 botpy 依赖
        except Exception as e:  # pragma: no cover
            raise QQOfficeNotSupported(f"botpy 不可用，路径 A 失效: {e}")

        api = self.bundle.get_api()
        http = getattr(api, "_http", None)
        if http is None:
            raise QQOfficeNotSupported("适配器客户端缺少 _http（botpy BotHttp），无法发起请求")
        route = Route(method, full_path)
        kwargs: dict[str, Any] = {"timeout": timeout}
        if params:
            kwargs["params"] = params
        if payload is not None:
            kwargs["json"] = payload
        try:
            result = await http.request(route, **kwargs)
        except Exception as exc:  # botpy 抛 RuntimeError 子类
            err = qo_errors.from_exception(exc)
            if err is None:
                raise
            raise err from exc
        # botpy 在超时/重试耗尽时静默返回 None（http.py:156-186）
        if result is None:
            raise QQOfficeGatewayError(504, None, "botpy 请求超时或重试耗尽（返回空）")
        return result if isinstance(result, dict) else {"raw": result}

    async def _dispatch_self(self, method: str, full_path: str, params: dict | None,
                             payload: dict | None, timeout: float) -> dict:
        assert self.self_client is not None
        status, body, headers = await self.self_client.request(
            method, full_path, params=params, json=payload, timeout=timeout
        )
        # 官方偶发 200 + 错误体（code>0 且带 message）
        if status < 400 and isinstance(body, dict):
            code = body.get("code")
            if isinstance(code, int) and code > 0 and body.get("message"):
                raise qo_errors.from_http_response(400, body, headers)
        if status >= 400:
            err = qo_errors.from_http_response(status, body, headers)
            if err:
                raise err
        return body if isinstance(body, dict) else {}

    async def _refresh_token(self) -> None:
        """401/11244 后强制换 token。"""
        if self.self_client is not None:
            self.self_client.tokens.invalidate()
            return
        try:
            token = getattr(self.bundle.get_api()._http, "_token", None)
            if token is not None and hasattr(token, "update_access_token"):
                await token.update_access_token()
        except Exception as exc:  # 刷新失败交由下一次请求的 botpy 自动机制兜底
            self._log("warning", f"token 主动刷新失败（交由 botpy 自动机制）: {exc}")

    async def upload_media(
        self,
        scene: str,
        openid: str,
        source,
        file_type: int = 1,
        srv_send_msg: bool = False,
    ) -> dict:
        """上传富媒体，返回含 file_info 的原始响应。

        file_type: 1=图片 2=视频 3=语音 4=文件。URL 直传不缓存；
        file_data 路径按 内容hash:场景:openid:file_type 缓存。
        """
        from .media import to_uploadable

        up = to_uploadable(source)
        prefix = "groups" if scene == "group" else "users"
        path = f"/v2/{prefix}/{{openid}}/files"

        if up.kind == "url":
            return await self.call(
                "POST", path,
                path_params={"openid": openid},
                json={"file_type": file_type, "url": up.source, "srv_send_msg": srv_send_msg},
                scene=scene, target_openid=openid,
                endpoint_key=f"{scene}.files",
            )

        data = await up.load_bytes()
        chash = self.refstore.content_hash(data)
        cached = self.refstore.get_file_info(chash, scene, openid, file_type)
        if cached:
            return {"file_info": cached, "cached": True}

        if scene == "c2c" and file_type == 3:
            from .media import strip_amr_header

            data, _ = strip_amr_header(data)
            chash = self.refstore.content_hash(data)

        resp = await self.call(
            "POST", path,
            path_params={"openid": openid},
            json={
                "file_type": file_type,
                "file_data": base64.b64encode(data).decode(),
                "srv_send_msg": srv_send_msg,
            },
            scene=scene, target_openid=openid,
            endpoint_key=f"{scene}.files",
        )
        file_info = str(resp.get("file_info") or "")
        if file_info:
            self.refstore.cache_file_info(
                chash, scene, openid, file_type, file_info, float(resp.get("ttl") or 0)
            )
        return resp

    @staticmethod
    def chunk_text(text: str, limit: int = 1400) -> list[str]:
        """文本分块（40054007 文本超限）：优先按换行边界切。"""
        text = text or ""
        if len(text) <= limit:
            return [text] if text else []
        chunks: list[str] = []
        buf = ""
        for para in text.split("\n"):
            while len(para) > limit:
                if buf:
                    chunks.append(buf)
                    buf = ""
                chunks.append(para[:limit])
                para = para[limit:]
            if len(buf) + len(para) + 1 > limit:
                chunks.append(buf)
                buf = para
            else:
                buf = f"{buf}\n{para}" if buf else para
        if buf:
            chunks.append(buf)
        return chunks

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "appid": self.appid,
            "calls_total": self.calls_total,
            "windows": self.windows.stats(),
            "rate": self.limiter.snapshot(),
            "refstore": self.refstore.snapshot(),
            "recent_errors": list(self.recent_errors),
        }

class _SafeDict(dict):
    """format_map 时未提供的占位符保留原样，交由后续校验报错。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
