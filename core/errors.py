"""错误标准化层：官方错误码 → 带排查建议（advice）的可捕获异常。

advice 依据官方错误码排查建议整理。
调用方按异常类型降级：QQOfficeNotSupported=场景不支持，
QQOfficeGatewayError=平台侧异常可重试，UploadDailyLimitExceeded=当日上传超限。
"""

from __future__ import annotations

import json as _json
import re as _re

__all__ = [
    "QQOfficeError",
    "QQOfficeAPIError",
    "QQOfficeNotSupported",
    "QQOfficeGatewayError",
    "UploadDailyLimitExceeded",
    "ERROR_ADVICE",
    "from_http_response",
    "from_exception",
    "extract_biz_code",
]

# 官方错误码 → 排查建议
ERROR_ADVICE: dict[int, str] = {
    11244: "token 失效（bizCode 11244）：已自动清 token 重试一次；若仍失败请检查 appId/secret 与机器人状态",
    22006: "消息类型与内容不匹配：检查 msg_type 与 content 是否对应",
    40034005: "被动回复 msg_id 已过期：请在收到消息后尽快回复，或改用主动消息",
    40034008: "markdown 参数有空值：确保所有 Markdown 模板参数都有值",
    40034009: "markdown 参数含换行符：移除模板参数中的换行符",
    40034010: "模板参数中不能含有 markdown 语法：参数使用纯文本",
    40034011: "无效的 markdown 内容：检查 Markdown 语法",
    40034024: "msg_id 无效或越权：检查 msg_id 是否正确、是否属于当前会话",
    40034025: "event_id 无效：检查 event_id 是否来自受支持事件（群侧 INTERACTION_CREATE/GROUP_ADD_ROBOT/GROUP_MSG_RECEIVE；C2C 侧 INTERACTION_CREATE/C2C_MSG_RECEIVE/FRIEND_ADD）",
    40034100: "主动消息配额超限：等待下一分钟后自动重试，或减少发送频率",
    40034101: "单关系主动消息超限：等待后自动重试，或减少对该用户/群的推送",
    40054007: "文本长度超限：使用 client.chunk_text() 分块发送",
    40054010: "不允许发送 URL：群文本中「字母/汉字.字母」易被误判为链接，可开启 dot_replace 配置或手动把点替换为下划线",
    40034026: "event_id 已过期：请在收到事件后尽快回复",
    40034027: "该事件不支持回复消息：确认事件类型是否支持回复",
    40034122: "召回消息已达区间上限：30 天 4 周期各 1 条",
    40034123: "该消息不支持召回",
    40034128: "被动回复时间或次数超限：改用主动消息",
    40062003: "撤回无权限：需机器人为群管理员或仅撤回自己的消息",
    40064004: "已超出消息撤回时限（2 分钟）",
    11253: "应用无接口访问权限：该接口仅白名单机器人可用，联系平台运营申请",
    40093001: "分片确认未就绪（平台异步）：属正常现象，需固定 1s 间隔持续重试（client 的 chunked 兜底已内置）",
    40093002: "当日上传容量超限（每日累计 2G）：请明日再试或改用外链 URL",
    630001: "interaction 参数无效：检查请求参数",
    630002: "interaction 获取 appid 失败：检查 Authorization Header",
    630003: "appid 与 interaction_id 不匹配：确认使用了正确的 Bot Token",
    630004: "interaction 写入失败：稍后重试",
    630005: "interaction 读取失败：稍后重试",
    630006: "interaction 获取 header appid 失败：检查请求 Header",
    630007: "interaction 数据过大：减小请求体",
    630008: "interaction 预处理失败：检查请求参数",
    40030006: "指令面板不存在：确认 panel_id",
    40030008: "URL 格式错误：确认链接以 https:// 开头",
    40030009: "面板操作进行中：稍后重试（并发冲突）",
    40030011: "生效场景不合法：scope 仅支持 c2c/group/channel/dm",
    40030012: "生效范围不合法：target_type 仅支持 all/specific；channel/dm 仅支持 all",
    40030013: "超出数量限制：减少请求数量",
    40030014: "菜单类型不合法：type 仅支持 switch/send_message/link/menu",
    40030015: "面板元素类型不合法：type 仅支持 command/link",
    40030016: "必填字段缺失",
    40030017: "操作类型不合法：op 仅支持 add/del",
    40030018: "当前场景不支持此操作",
    40030020: "内容存在安全风险：修改菜单/面板内容后重试",
    40030021: "全局面板不支持该操作：改用 target_type=specific",
}

RATE_LIMITED_CODES = frozenset({429, 40034100, 40034101})
TOKEN_EXPIRED_CODES = frozenset({11244})
# 官方 biz 码形态：4003xxxx / 4005xxxx / 4009300x（8 位）、220xx、63000x、11244
_CODE_RE = _re.compile(r"\b(11244|220\d\d|4003\d{4}|4005\d{4}|4009300[12]|63000\d)\b")

_UPLOAD_DAILY_LIMIT_CODE = 40093002

class QQOfficeError(Exception):
    """本插件全部异常的基类。"""

class QQOfficeAPIError(QQOfficeError):
    """官方接口返回的业务错误。"""

    def __init__(self, code: int, message: str, raw=None, advice: str | None = None):
        self.code = int(code)
        self.message = message
        self.raw = raw
        self.advice = advice or ERROR_ADVICE.get(self.code)
        text = f"[QQOffice {self.code}] {message}"
        if self.advice:
            text += f"（建议：{self.advice}）"
        super().__init__(text)

class QQOfficeNotSupported(QQOfficeError):
    """当前平台/场景不支持该能力，调用方应据此降级。"""

class QQOfficeGatewayError(QQOfficeError):
    """平台侧网关异常（HTML 错误页/5xx 文本响应），属可重试错误。code = HTTP 状态码。"""

    def __init__(self, status: int, trace_id: str | None = None, body_hint: str = ""):
        self.code = int(status)
        self.status = status
        self.trace_id = trace_id
        text = f"平台网关异常（HTTP {status}），稍后重试"
        if trace_id:
            text += f"，x-tps-trace-id={trace_id}（排障时提供给官方）"
        if body_hint:
            text += f"；响应片段：{body_hint}"
        super().__init__(text)

class UploadDailyLimitExceeded(QQOfficeAPIError):
    """当日上传容量超限（40093002）。"""

def extract_biz_code(text: str) -> int | None:
    """从错误文本/响应体中提取官方 biz 错误码。"""
    m = _CODE_RE.search(text or "")
    return int(m.group(1)) if m else None

def from_http_response(status: int, body, headers=None) -> QQOfficeAPIError | QQOfficeGatewayError | None:
    """把一次非 2xx 的 HTTP 响应归一为标准异常；2xx 正常响应返回 None。

    body 可以是已解析的 JSON（dict/list）或原始文本。
    """
    headers = headers or {}
    trace_id = headers.get("x-tps-trace-id") if hasattr(headers, "get") else None

    # 网关/CDN 会返回 HTML 错误页，按 JSON 解析会崩
    if isinstance(body, str):
        stripped = body.strip()
        if stripped.startswith("<") or "text/html" in str(headers.get("content-type", "")):
            hint = _re.sub(r"\s+", " ", stripped[:120])
            return QQOfficeGatewayError(status, trace_id, hint)
        try:
            body = _json.loads(stripped)
        except (ValueError, TypeError):
            if status >= 500 or status == 429:
                return QQOfficeGatewayError(status, trace_id, stripped[:120])
            return QQOfficeAPIError(status, stripped[:200], raw=body)

    if isinstance(body, dict):
        code = body.get("code")
        message = str(body.get("message") or body.get("msg") or "")
        if code is not None:
            return _build(status, int(code), message, body)
        # 部分接口错误体只有 message
        embedded = extract_biz_code(message)
        if embedded is not None:
            return _build(status, embedded, message, body)
        return QQOfficeAPIError(status, message or f"HTTP {status}", raw=body)

    return QQOfficeAPIError(status, f"HTTP {status}", raw=body)

def _build(status: int, code: int, message: str, raw) -> QQOfficeAPIError | QQOfficeGatewayError:
    if code == _UPLOAD_DAILY_LIMIT_CODE:
        return UploadDailyLimitExceeded(code, message or "当日上传容量超限", raw=raw)
    if status == 429 or code in RATE_LIMITED_CODES:
        return QQOfficeAPIError(code or 429, message or "触发频控", raw=raw)
    return QQOfficeAPIError(code or status, message or f"HTTP {status}", raw=raw)

def from_exception(exc: Exception) -> QQOfficeAPIError | QQOfficeGatewayError | None:
    """把 botpy/底层库抛出的异常归一为标准异常；非目标异常返回 None。

    botpy 非 2xx 时抛 RuntimeError 子类（ServerError/ForbiddenError/...），
    异常文本携带官方错误码，按类名 + 文本反解。
    """
    if isinstance(exc, QQOfficeError):
        return exc
    text = str(exc)
    code = extract_biz_code(text)
    lower = text.lower()
    if code == _UPLOAD_DAILY_LIMIT_CODE:
        return UploadDailyLimitExceeded(code, text, raw=exc)
    # botpy.errors 均为 RuntimeError 子类，按类名映射状态码（不 isinstance，避免 core 依赖 botpy）
    cls = type(exc).__name__
    status_map = {
        "AuthenticationFailedError": 401,
        "ForbiddenError": 403,
        "NotFoundError": 404,
        "MethodNotAllowedError": 405,
        "ServerError": 500,
    }
    status = status_map.get(cls)
    if "text/html" in lower or (text.lstrip().startswith("<") and len(text) < 2000):
        return QQOfficeGatewayError(status or 502, None, text[:120])
    if code is not None or status is not None:
        return _build(status or 500, code or 0, text, exc)
    return None
