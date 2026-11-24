"""token / 缓存用量统计 —— 从事件里取数并累加。"""

from __future__ import annotations


def _as_int(value: object) -> int:
    """事件里的数字字段可能是 object，安全转 int，失败回退 0。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _token_delta(event: dict[str, object]) -> tuple[int, int, int, int]:
    """从 MODEL_CALL_END 事件取出四类 token 增量：输入 / 输出 / 缓存读 / 缓存写。"""
    return (
        _as_int(event.get("input_tokens", 0)),
        _as_int(event.get("output_tokens", 0)),
        _as_int(event.get("cache_input_tokens", 0)),
        _as_int(event.get("cache_creation_input_tokens", 0)),
    )
