"""命名方法注册表：表驱动，新接口 = 加一行表项。

api/ 各场景模块实例化后，其公开方法自动以 "group.recall" 这样的全名注册进
Registry；供 svc.invoke("group.recall", ...) 泛化调用与 qqoffice_status
诊断列出能力清单。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import QQOfficeNotSupported

__all__ = ["Registry", "MethodMeta", "collect_methods"]


@dataclass
class MethodMeta:
    scene: str = ""          # group / c2c / guild / manage
    endpoint: str = ""       # 官方端点简述
    note: str = ""
    tags: list[str] = field(default_factory=list)


class Registry:
    def __init__(self):
        self._methods: dict[str, tuple[Callable, MethodMeta]] = {}

    def register_fn(self, name: str, fn: Callable, meta: MethodMeta | None = None) -> None:
        """同名重复注册视为重绑定（目录在构造时注册一次，方法为未绑定函数）。"""
        self._methods[name] = (fn, meta or MethodMeta())

    def get(self, name: str) -> tuple[Callable, MethodMeta]:
        entry = self._methods.get(name)
        if entry is None:
            known = "\n".join(sorted(self._methods)) or "（空）"
            raise QQOfficeNotSupported(f"未知方法 {name}。已注册：\n{known}")
        return entry

    def names(self) -> list[str]:
        return sorted(self._methods)

    def catalog(self) -> dict[str, MethodMeta]:
        return {name: meta for name, (_, meta) in self._methods.items()}

    async def invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        fn, _ = self.get(name)
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


def collect_methods(obj: Any) -> dict[str, Callable]:
    """收集对象类上的公开方法（未绑定函数）。

    目录只构建一次；调用时以「视图代理」为 self 调用（见 main.BoundView.invoke），
    代理把 self._client 指到具体视图，避免共享命名空间实例的可变绑定。
    """
    out: dict[str, Callable] = {}
    for attr in dir(obj):
        if attr.startswith("_"):
            continue
        fn = getattr(type(obj), attr, None)
        if callable(fn) and inspect.isfunction(fn):
            out[attr] = fn
    return out
