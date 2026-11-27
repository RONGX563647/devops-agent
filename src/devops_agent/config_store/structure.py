"""配置值的结构工具：点号路径的压平 / 还原与深合并。

单独成文件有两个原因：存取层的「合流」与「只存差异」都靠这几个纯函数，
而它们与 SQLite、pydantic 都无关，可以单独测；同时把 `store.py` 压回 500 行门禁内。

约定：列表整体算一个叶子（`allowed_ports` 是一个值，不展开成 `allowed_ports.0`），
否则「加一个端口」会变成「新增一个键」，恢复默认时也删不干净。
"""

from __future__ import annotations

import json
from typing import Any

_MISSING = object()

MISSING = _MISSING


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """把嵌套 dict 压成点号路径；列表与标量算叶子。"""
    if not isinstance(value, dict):
        return {prefix: value}
    out: dict[str, Any] = {}
    for key, item in value.items():
        out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    return out


def unflatten(pairs: dict[str, Any]) -> dict[str, Any]:
    """点号路径还原成嵌套 dict。"""
    out: dict[str, Any] = {}
    for path, value in pairs.items():
        node = out
        parts = path.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
    return out


def merge(base: dict[str, Any], layer: dict[str, Any]) -> dict[str, Any]:
    """深合并（只对 dict 递归），返回新对象，不改入参。"""
    out = dict(base)
    for key, value in layer.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = merge(current, value)
        else:
            out[key] = value
    return out


def dict_prefixes(defaults: dict[str, Any], prefix: str = "") -> set[str]:
    """值是 dict 的路径集合：这些路径下允许自定义子键（如 max_iters.delivery）。"""
    out: set[str] = set()
    for key, value in defaults.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.add(path)
    return out


def maybe_json(raw: object) -> Any:
    """审计表里存的是 JSON 文本；解析失败就原样返回，别把坏数据吞掉。"""
    if raw is None:
        return None
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError:
        return str(raw)


__all__ = ["MISSING", "dict_prefixes", "flatten", "maybe_json", "merge", "unflatten"]
