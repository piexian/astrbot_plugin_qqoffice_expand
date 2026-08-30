"""富消息构建器。

产物都是普通 dict/str，直接并入 payload 的 extra 透传。
markdown × message_reference 互斥的裁决在 send_rich（main.py）里做。
"""

from __future__ import annotations

from typing import Any

from .media import image_size

__all__ = ["md", "btn", "Keyboard", "reference", "md_image"]


def md(content: str | None = None, *, template_id: str | None = None,
       params: dict[str, Any] | list | None = None, **extra) -> dict:
    """构建 markdown 段。

    - 自定义：md("# 标题\\n正文")
    - 模板：md(template_id="101993071_1658748972", params={"title": "xx", "img": ["url1"]})
      params 值可为标量或列表（统一成 {key, values:[...]}）。
    - 附加字段（如 force_verify_image_resource=True）用关键字传入。
    """
    body: dict[str, Any] = dict(extra)
    if template_id:
        body["custom_template_id"] = template_id
        if params:
            body["params"] = _normalize_params(params)
    elif content is not None:
        body["content"] = content
    else:
        raise ValueError("md() 需要 content 或 template_id")
    return {"markdown": body}


def _normalize_params(params: dict[str, Any] | list) -> list[dict]:
    if isinstance(params, list):
        return params  # 已是官方形态 [{key, values}]
    out = []
    for key, value in params.items():
        values = value if isinstance(value, list) else [value]
        out.append({"key": key, "values": [str(v) for v in values]})
    return out


def btn(label: str, data: str, *, type: int = 2, permission_type: int = 2,
        specify_user_ids: list[str] | None = None, specify_role_ids: list[str] | None = None,
        visited_label: str | None = None, style: int | None = None,
        enter: bool | None = None, reply: bool | None = None, anchor: int | None = None,
        unsupport_tips: str | None = None, button_id: str | None = None,
        **extra) -> dict:
    """构建单按钮 dict（官方 Button 结构）。

    type: 0跳转 1回调 2指令；permission_type: 0指定用户 1管理员 2所有人。
    """
    render: dict[str, Any] = {"label": label}
    render["visited_label"] = visited_label or label
    if style is not None:
        render["style"] = style
    action: dict[str, Any] = {"type": type, "data": data}
    permission: dict[str, Any] = {"type": permission_type}
    if specify_user_ids:
        permission["specify_user_ids"] = specify_user_ids
    if specify_role_ids:
        permission["specify_role_ids"] = specify_role_ids
    action["permission"] = permission
    for k, v in (("enter", enter), ("reply", reply), ("anchor", anchor),
                 ("unsupport_tips", unsupport_tips)):
        if v is not None:
            action[k] = v
    button: dict[str, Any] = dict(extra)
    if button_id:
        button["id"] = button_id
    button["render_data"] = render
    button["action"] = action
    return button


class Keyboard:
    """链式构建 keyboard 段：kb().row(b1, b2).row(b3).build()。"""

    def __init__(self):
        self._rows: list[dict] = []
        self._template_id: str | None = None

    def template(self, template_id: str) -> "Keyboard":
        """使用平台预设键盘模板（与自定义 rows 互斥）。"""
        self._template_id = template_id
        return self

    def row(self, *buttons: dict) -> "Keyboard":
        if buttons:
            self._rows.append({"buttons": list(buttons)})
        return self

    def build(self) -> dict:
        if self._template_id:
            return {"keyboard": {"id": self._template_id}}
        return {"keyboard": {"content": {"rows": self._rows}}}


def reference(ref_id: str | None = None, **extra) -> dict:
    """构建 message_reference 段。

    ref_id 即 REFIDX_xxx 引用索引：
    - 引用收到的消息：从事件 message_scene.ext 的 msg_idx 取（svc.ref_from_event）
    - 引用机器人自己发的消息：从发送响应 ext_info.ref_idx 取（refstore）
    """
    if not ref_id:
        raise ValueError("reference() 需要 REFIDX 索引（先从事件或出站响应里记录）")
    body: dict[str, Any] = {"message_id": ref_id}
    body.update(extra)
    return {"message_reference": body}


def md_image(url: str, *, width: int | None = None, height: int | None = None,
             source=None) -> str:
    """markdown 图片语法：强制 `![#Wpx #Hpx](url)`（不带显式尺寸会渲染异常）。

    尺寸优先级：显式 width/height > source（bytes/已加载 Uploadable）解析 > 默认 512x512。
    source 是远程 url 字符串时不下载（官方会转存），按默认尺寸。
    """
    if width is None or height is None:
        w = h = None
        data = None
        if source is not None:
            if isinstance(source, (bytes, bytearray)):
                data = bytes(source)
            else:
                data = getattr(source, "loaded_bytes", lambda: None)()
        if data:
            w, h = image_size(data)
        w = width or w or 512
        h = height or h or 512
        width, height = int(w), int(h)
    return f"![#{int(width)}px #{int(height)}px]({url})"
