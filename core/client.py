"""绑定视图的通用调用通道（N 实例版）。

流程与设计第 5 节一致：
1. 解析来源（resolve）并固定 OperationContext（同一操作全程使用）；
2. 在所属 RobotState 申请被动窗口预留 / 主动配额；
3. 限流等待结束后 check_context 再核验代次，动态取当前 HTTP；
4. 调用该 HTTP；重试前再次核验（不跨机器人/代次重放）。

重试语义：网关/超时对**写操作**不自动重发（结果未知不盲放）；只对
安全查询（GET）和明确的 token/频控拒绝重试。被动预留只在「确定未发送」
时释放；发送结果未知按已消耗处理。

上传与发送共享同一 OperationContext（send_rich 富媒体流程不跨代次）。
"""

from __future__ import annotations

import asyncio
import base64
import random
import re as _re
import time
from dataclasses import dataclass
from typing import Any

from . import errors as qo_errors
from .errors import QQOfficeAPIError, QQOfficeGatewayError, QQOfficeNotSupported
from .http_capture import capture_response_status
from .routing import (
    BoundSource,
    OperationContext,
    PassiveWindows,
    RobotState,
    RouteCore,
)

__all__ = ["generate_msg_seq", "resolve_endpoint_key", "execute_call",
           "send_rich_bound", "upload_media_bound"]

_DOT_RE = _re.compile(r"([A-Za-z0-9\u4e00-\u9fff])\.([A-Za-z0-9\u4e00-\u9fff])")


def generate_msg_seq() -> int:
    """无状态生成，规避相同 msg_id+msg_seq 的平台去重报错。"""
    return (int(time.time() * 1000) % 100_000_000) ^ random.getrandbits(16)


# 端点 key 解析表（顺序敏感；新端点在此加一行即可）
_ENDPOINT_PATTERNS: list[tuple[_re.Pattern, str]] = [
    (_re.compile(r"/messages/[^/]+$"), "{scene}.message_get_or_recall"),
    (_re.compile(r"/messages$"), "{scene}.send"),
    (_re.compile(r"^/v2/users/[^/]+/stream_messages$"), "c2c.stream"),
    (_re.compile(r"/files$"), "{scene}.files"),
    (_re.compile(r"upload_prepare$"), "{scene}.upload_prepare"),
    (_re.compile(r"upload_part_finish$"), "{scene}.upload_part_finish"),
    (_re.compile(r"/interactions/[^/]+$"), "interactions.ack"),
    (_re.compile(r"/bot_state$"), "{scene}.bot_state"),
    (_re.compile(r"/join_request_list$"), "group.join_requests"),
    (_re.compile(r"/approval_join_request/"), "group.join_approve"),
    (_re.compile(r"^/v2/groups/[^/]+/members/[^/]+$"), "group.member_info"),
    (_re.compile(r"^/v2/groups/[^/]+/members$"), "group.members"),
    (_re.compile(r"/batch_remove_members$"), "group.remove_members"),
    (_re.compile(r"/member_blacklist$"), "group.blacklist_get_or_set"),
    (_re.compile(r"/restrict_chat_setting$"), "{scene}.mute_get_or_set"),
    (_re.compile(r"join_approval_strategy"), "group.strategy"),
    (_re.compile(r"/info$"), "{scene}.info"),
    (_re.compile(r"/v2/menu$"), "menu.get_or_put"),
    (_re.compile(r"/v2/panels/[^/]+/target$"), "panels.target"),
    (_re.compile(r"/v2/panels/[^/]+$"), "panels.get_or_write_or_del"),
    (_re.compile(r"/v2/panels$"), "panels.list_or_create"),
    (_re.compile(r"^/guilds/[^/]+/channels$"), "guild.channels"),
    (_re.compile(r"^/guilds/[^/]+$"), "guild.info"),
    (_re.compile(r"^/channels/[^/]+$"), "guild.channel"),
    (_re.compile(r"^/users/@me/guilds$"), "guild.list"),
    (_re.compile(r"^/users/@me$"), "bot.me"),
]


def resolve_endpoint_key(method: str, path: str, scene: str | None) -> str:
    key_tpl: str | None = None
    for pattern, tpl in _ENDPOINT_PATTERNS:
        if pattern.search(path):
            key_tpl = tpl
            break
    if key_tpl is None:
        return "default"
    if key_tpl == "{scene}.send" and method.upper() != "POST":
        return "default"
    if key_tpl == "{scene}.message_get_or_recall":
        return f"{scene}.message_get" if method.upper() == "GET" else f"{scene}.recall"
    if key_tpl == "group.blacklist_get_or_set":
        return "group.blacklist" if method.upper() == "GET" else "group.set_blacklist"
    if key_tpl == "{scene}.mute_get_or_set":
        return f"{scene or 'group'}.mute_get" if method.upper() == "GET" else f"{scene or 'group'}.mute_set"
    if key_tpl == "menu.get_or_put":
        return "menu.get" if method.upper() == "GET" else "menu.put"
    if key_tpl == "panels.get_or_write_or_del":
        return "panels.get" if method.upper() == "GET" else "panels.write"
    if key_tpl == "panels.list_or_create":
        return "panels.list" if method.upper() == "GET" else "panels.write"
    return key_tpl.format(scene=scene) if "{scene}" in key_tpl else key_tpl


_SAFE_QUERY_METHODS = frozenset({"GET"})
_WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
"""网关错误/超时（结果未知）时允许自动重试的方法。

仅显式安全（幂等只读）请求；POST/PUT/DELETE（发送、撤回、禁言、ACK、
上传等）结果未知时不重放。
"""


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass
class _Reservation:
    """一次成功的被动窗口预留；只由其归属的操作按语义释放。"""

    windows: PassiveWindows
    scene: str
    target: str
    msg_id: str

    def release_if_unused(self) -> None:
        self.windows.release(self.scene, self.target, self.msg_id)


async def execute_call(svc, bound: BoundSource, method: str, path: str,
                       **kwargs) -> dict | list:
    """通用通道入口：BoundView.call / 命名方法 / send_rich 全部经过这里。

    kwargs: path_params/params/json/scene/target_openid/endpoint_key/
    msg_id/event_id/msg_seq/_ctx（富媒体流程传入的固定上下文）/
    _phase（ACK 等调用方的阶段跟踪器：进入 SDK 前后语义不同）。
    """
    method = method.upper()
    routes: RouteCore = svc.routes
    phase = kwargs.pop("_phase", None)
    ctx: OperationContext | None = kwargs.pop("_ctx", None)
    if ctx is not None:
        # 富媒体流程的后续步骤：不重新取代次，直接核验既有上下文。
        route = routes.ensure_current_route(ctx.platform_id)
        http = routes.check_context(ctx)
    else:
        route, http = routes.resolve(bound)
        ctx = routes.operation(route)
    state = routes.state_for(route.robot_key)
    state.touch()
    state.retain()   # 在途归属：等待/重试期间状态不被后台清理回收
    try:
        return await _execute_call_body(svc, routes, bound, ctx, route, state,
                                        method, path, kwargs, http, phase)
    finally:
        state.release()


async def _execute_call_body(svc, routes, bound, ctx, route, state, method,
                             path, kwargs, http, phase):
    path_params = dict(kwargs.get("path_params") or {})
    try:
        full_path = path.format_map(_SafeDict(path_params))
    except ValueError as e:
        raise QQOfficeNotSupported(f"路径参数展开失败: {path} {path_params} ({e})")
    missing = _re.findall(r"\{(\w+)\}", full_path)
    if missing:
        raise QQOfficeNotSupported(f"路径缺少参数 {missing}: {path}")

    scene = kwargs.get("scene")
    target = kwargs.get("target_openid")
    endpoint_key = kwargs.get("endpoint_key") or resolve_endpoint_key(method, full_path, scene)
    payload = dict(kwargs.get("json") or {})
    refstore = svc.refstore
    prefix = route.robot_key.prefix()

    is_stream = endpoint_key == "c2c.stream"
    is_send = (endpoint_key.endswith(".send") or is_stream
               or (endpoint_key.endswith(".files") and bool(payload.get("srv_send_msg"))))
    continuation = is_stream and bool(payload.get("stream_msg_id"))
    # 回复字段统一从 payload 取（api 层与 send_rich 都已并入 payload），
    # 兼容旧式 kwargs 直传：同步进 payload，保持单一事实来源。
    for key in ("msg_id", "event_id", "msg_seq"):
        if kwargs.get(key) is not None and key not in payload:
            if key == "msg_seq" and "msg_id" not in payload and "event_id" not in payload:
                continue
            payload[key] = kwargs[key]

    # —— 被动窗口预留（无 await；群/C2C 按身份+scene+target+msg_id 隔离）——
    reservation: _Reservation | None = None
    degraded_from = None
    if is_stream and not continuation and payload.get("msg_id") and not payload.get("is_wakeup"):
        ok, reason = state.windows.reserve("c2c", target, payload["msg_id"])
        if not ok:
            raise QQOfficeAPIError(
                40034128, f"流式被动回复窗口失效（{reason}），请显式发起主动流"
            )
        reservation = _Reservation(state.windows, "c2c", target, payload["msg_id"])
    elif is_send and not continuation:
        msg_id = payload.get("msg_id")
        if msg_id and scene in PassiveWindows.LIMITS:
            ok, reason = state.windows.reserve(scene, target, msg_id)
            if not ok:
                if not svc.config.get("auto_degrade_proactive", True):
                    raise QQOfficeAPIError(
                        40034005, f"被动回复窗口失效（{reason}），且未开启自动降级"
                    )
                degraded_from = msg_id
                payload.pop("msg_id", None)
                payload.pop("msg_seq", None)
            else:
                reservation = _Reservation(state.windows, scene, target, msg_id)
                if "msg_seq" not in payload:
                    payload["msg_seq"] = (
                        kwargs.get("msg_seq") if kwargs.get("msg_seq") is not None
                        else generate_msg_seq()
                    )

    _reached_sdk = False
    _last_definitive = False   # 最近一次响应是官方明确拒绝（确定未生效）
    try:
        # —— 主动配额：只对真正的主动消息消费（被动回复/降级前不发）——
        is_proactive = (is_send and "msg_id" not in payload
                        and "event_id" not in payload and not payload.get("is_wakeup"))
        if is_proactive and target and scene in ("group", "c2c"):
            await state.rate.consume_proactive(str(target), scene=scene)
            http = routes.check_context(ctx)   # 等待后核验

        # —— 端点频控等待（await；尚未进入 SDK，可安全中断）——
        await state.rate.acquire(endpoint_key, target=target if scene == "guild" else None)
        http = routes.check_context(ctx)   # 限流等待后再核验，动态取当前 HTTP

        if (svc.config.get("dot_replace", False) and scene == "group"
                and isinstance(payload.get("content"), str)):
            payload["content"] = _DOT_RE.sub(r"\1_\2", payload["content"])
        if degraded_from is not None:
            state._log_degrade(svc, scene, target, degraded_from)

        # —— 发送循环。阶段语义：
        #   pre_dispatch：进入 http.request 前（确定未发送）→ 可退预留
        #   in_flight：http.request 已调用（结果未知）→ 保留消费
        # 重试只对：token/429 明确拒绝（写/读皆可）、安全查询的网关错误。
        token_retry_used = False
        rate_attempts = 0
        retry_max = int(svc.config.get("retry_max", 3) or 0)
        while True:
            try:
                _reached_sdk = True   # 即将进入 SDK（http.request）
                _last_definitive = False   # 新尝试开始：上一次的明确拒绝不再代表本次
                if phase is not None:
                    phase.reached_sdk = True
                    phase.definitive_rejected = False
                result = await _dispatch(http, method, full_path,
                                         kwargs.get("params"), payload if payload else None)
            except QQOfficeAPIError as exc:
                _record_error(state, exc)
                _last_definitive = True   # 明确 API 拒绝：确定未生效
                if phase is not None:
                    phase.definitive_rejected = True
                if (not token_retry_used
                        and (exc.code == 401 or exc.code in qo_errors.TOKEN_EXPIRED_CODES)):
                    token_retry_used = True
                    http = await _refresh_token(routes, ctx)
                    continue
                if (exc.code == 429 or exc.code in qo_errors.RATE_LIMITED_CODES
                        or (is_stream and exc.code == 50002)):
                    if rate_attempts < retry_max:
                        rate_attempts += 1
                        await asyncio.sleep(min(30.0, 1.0 * rate_attempts))
                        http = routes.check_context(ctx)
                        continue
                _fail(reservation)
                reservation = None
                if phase is not None:
                    phase.definitive_rejected = True
                raise
            except QQOfficeGatewayError as exc:
                _record_error(state, exc)
                # 未知网关结果：写操作不盲重放、不退款；安全查询可重试
                if (not is_send and method in _SAFE_QUERY_METHODS
                        and rate_attempts < retry_max):
                    rate_attempts += 1
                    await asyncio.sleep(min(15.0, 2.0 * rate_attempts))
                    http = routes.check_context(ctx)
                    continue
                reservation = None   # 未知结果：保留消费，外层不再退
                if phase is not None:
                    phase.unknown = True
                raise
            break
    except (asyncio.CancelledError, Exception):
        # 进入 SDK 前的失败：确定未发送 → 退预留。
        # 进入 SDK 后：仅当最近响应是官方明确拒绝（确定未生效）才退；
        # 其余取消/未知异常结果可能已生效 → 保留消费。
        if not _reached_sdk or _last_definitive:
            _fail(reservation)
        raise

    # —— 出站引用入库（带身份前缀）；预留默认归宿为已消耗 ——
    if isinstance(result, dict) and is_send and scene and target:
        refstore.record_outbound(f"{prefix}|{scene}", target, result)
    return result if isinstance(result, (dict, list)) else {}


def _fail(reservation: _Reservation | None) -> None:
    """确定未发送时释放预留；reservation 为 None 表示已按消耗处理。"""
    if reservation is not None:
        reservation.release_if_unused()


async def _dispatch(http, method: str, full_path: str, params: dict | None,
                    payload: dict | None) -> dict | list:
    """通过当前 botpy BotHttp 发请求；保留原错误标准化与空响应区分。

    SDK 对 200/204 空响应和超时都返回 None；超时/网关错误在这里按
    captured 状态区分并抛 QQOfficeGatewayError（写操作由上层裁决不重放）。
    写操作临时禁用 botpy 内部的 ConnectionReset 自动重发（结果未知不重放
    是插件语义，SDK 递归重试会绕过它）。
    """
    try:
        import botpy.http as botpy_http
    except Exception as e:  # pragma: no cover
        raise QQOfficeNotSupported(f"botpy 不可用: {e}")

    route = botpy_http.Route(method, full_path)
    kwargs: dict[str, Any] = {}
    if params:
        kwargs["params"] = params
    if payload is not None:
        kwargs["json"] = payload
    captured: dict = {}
    # 写操作：把 botpy 内部 ConnectionReset 递归重试的余量耗尽（初始
    # retry_time=2 → SDK 内部重试一次后 retry_time=3 即放弃），SDK 不会
    # 重放写请求，结果未知按上层网关错误处理。读取保持 SDK 默认重试。
    sdk_retry_time = 2 if method in _WRITE_METHODS else 0
    try:
        with capture_response_status(botpy_http) as captured:
            result = await http.request(route, retry_time=sdk_retry_time, **kwargs)
    except Exception as exc:
        err = captured.get("error") or qo_errors.from_exception(exc)
        if err is None:
            raise
        raise err from exc
    if result is None:
        if 200 <= captured.get("status", 0) < 300:
            return {}
        raise QQOfficeGatewayError(504, None, "botpy 请求超时或重试耗尽（返回空）")
    if (isinstance(result, dict) and isinstance(result.get("code"), int)
            and result["code"] > 0 and result.get("message")):
        raise qo_errors.from_http_response(400, result)
    return result if isinstance(result, (dict, list)) else {}


def _record_error(state: RobotState, exc: Exception) -> None:
    state.recent_errors.append(
        {"ts": time.strftime("%H:%M:%S"), "err": repr(exc)[:200]}
    )


async def _refresh_token(routes: RouteCore, ctx: OperationContext) -> Any:
    """401/11244 后换 token 并核验代次；返回当前可用的 HTTP。"""
    http = routes.check_context(ctx)
    token = getattr(http, "_token", None)
    if token is not None and hasattr(token, "update_access_token"):
        try:
            await token.update_access_token()
        except Exception:
            pass   # 刷新失败交由下一次请求的 botpy 自动机制兜底
    return routes.check_context(ctx)


# ---------------- 富消息（上传→发送共用同一上下文） ----------------


async def send_rich_bound(svc, bound: BoundSource, *, scene, target_openid,
                          content, markdown, keyboard, reference, media, file_type,
                          msg_id, event_id, event_id_source, msg_seq, extra,
                          on_mutex) -> dict:
    """富消息入口：入口处固定 OperationContext，上传与发送全程共用。"""
    routes: RouteCore = svc.routes
    route, http = routes.resolve(bound)
    ctx = routes.operation(route)
    state = routes.state_for(route.robot_key)

    if scene not in ("group", "c2c", "guild", "dm") or not target_openid:
        raise QQOfficeNotSupported("send_rich 需要 scene+target_openid（group/c2c/guild/dm）")

    if event_id and event_id_source and scene in ("group", "c2c"):
        from .events import EVENT_ID_SCOPES

        if event_id_source.upper() not in EVENT_ID_SCOPES.get(scene, set()):
            svc._log("warning",
                     f"event_id 事件 {event_id_source} 不在 {scene} 场景官方支持范围，已丢弃")
            event_id = None

    if markdown and reference and scene in ("group", "c2c"):
        if on_mutex == "text_reference":
            md_content = (markdown.get("markdown") or {}).get("content")
            if md_content and not content:
                content = md_content
            markdown = None
            svc._log("info", "markdown 与 reference 互斥：已降级纯文本并保留引用")
        else:
            reference = None
            svc._log("warning", "markdown 与 reference 互斥：已丢弃 reference")

    payload: dict = dict(extra or {})
    if scene in ("guild", "dm"):
        payload.pop("msg_type", None)
        if media is not None:
            if file_type != 1 or not isinstance(media, str) or not media.startswith(
                    ("https://", "http://")):
                raise QQOfficeNotSupported("频道 send_rich 的 media 支持图片 URL；本地图片请用 AstrBot 原生发送")
            payload["image"] = media
    elif media is not None:
        if not (isinstance(media, dict) and media.get("file_info")):
            resp = await _upload_shared(
                svc, ctx, route, state, scene, target_openid, media, file_type,
                srv_send_msg=False,
            )
            media = {"file_info": resp.get("file_info")}
        payload.setdefault("msg_type", 7)
        payload["media"] = media
    else:
        payload.setdefault("msg_type", 2 if markdown else 0)
    if markdown and content and scene in ("group", "c2c"):
        svc._log("warning", "markdown 与 content 互斥：已丢弃 content")
        content = None
    if content:
        payload["content"] = content
    if markdown:
        payload.update(markdown)
    if keyboard:
        payload.update(keyboard)
    if reference:
        payload.update(reference)

    path, key = {
        "group": ("/v2/groups/{group_openid}/messages", "group_openid"),
        "c2c": ("/v2/users/{user_openid}/messages", "user_openid"),
        "guild": ("/channels/{channel_id}/messages", "channel_id"),
        "dm": ("/dms/{guild_id}/messages", "guild_id"),
    }[scene]
    return await execute_call(
        svc, bound, "POST", path,
        path_params={key: target_openid}, json=payload, scene=scene,
        target_openid=target_openid, msg_id=msg_id, event_id=event_id,
        msg_seq=msg_seq, _ctx=ctx,
    )


async def upload_media_bound(svc, bound: BoundSource, scene: str, openid: str,
                             source, file_type: int = 1,
                             srv_send_msg: bool = False) -> dict:
    """独立上传入口：与发送同一策略（配额/错误/核验），按需走共同路径。"""
    routes: RouteCore = svc.routes
    route, http = routes.resolve(bound)
    ctx = routes.operation(route)
    state = routes.state_for(route.robot_key)
    return await _upload_shared(svc, ctx, route, state, scene, openid, source,
                                file_type, srv_send_msg)


async def _upload_shared(svc, ctx: OperationContext, route, state: RobotState,
                         scene: str, openid: str, source, file_type: int,
                         srv_send_msg: bool) -> dict:
    """上传实现：read/caches→核验→发送；走 execute_call 同一套错误标准化。"""
    from .media import to_uploadable

    prefix = route.robot_key.prefix()
    refstore = svc.refstore

    up = to_uploadable(source)
    chash = None
    if up.kind != "url":
        data = await up.load_bytes()   # 读取/剥头在 await 侧完成
        # 读取媒体是 await：无论后续是否命中缓存，都要核验同一操作上下文
        # （读取期间实例可能被禁用/重载）。
        svc.routes.check_context(ctx)
        if scene == "c2c" and file_type == 3:
            from .media import strip_amr_header

            data, _ = strip_amr_header(data)
        chash = refstore.content_hash(data)
        cached = refstore.get_file_info(f"{prefix}|{chash}", scene, openid, file_type)
        if cached and not srv_send_msg:
            return {"file_info": cached, "cached": True}

    path = f"/v2/{'groups' if scene == 'group' else 'users'}/{{openid}}/files"
    if up.kind == "url":
        body = {"file_type": file_type, "url": up.source, "srv_send_msg": srv_send_msg}
    else:
        body = {
            "file_type": file_type,
            "file_data": base64.b64encode(data).decode(),
            "srv_send_msg": srv_send_msg,
        }
    resp = await execute_call(
        svc, None, "POST", path,
        path_params={"openid": openid}, json=body,
        scene=scene, target_openid=openid, endpoint_key=f"{scene}.files",
        _ctx=ctx,
    )
    file_info = str(resp.get("file_info") or "")
    if file_info and up.kind != "url":
        refstore.cache_file_info(
            f"{prefix}|{chash}", scene, openid, file_type, file_info,
            float(resp.get("ttl") or 0),
        )
    return resp
