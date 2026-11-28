"""配置的异常类型 —— 单独成模块，存取层与合流层都要抛，不能互相 import。

四个都对应一种**必须让调用方看见**的失败；接口层按类型映射状态码：

| 异常 | 含义 | HTTP |
|---|---|---|
| `UnknownDomain` | 域 key 拼错 | 404 |
| `InvalidConfig` | 键或值不合法 | 400 |
| `ConfigConflict` | 版本冲突（别人先改过） | 409 |
| `ConfigUnavailable` | 配置库坏了 / 不可写 | 503 |

反例是「读库异常 → 当成没有覆盖」：那样界面会显示代码默认值，看起来一切正常。
读路径**必须**降级（不能因为配置坏了拦住对话），但降级要留痕（`config_store.health`）
并且写路径**必须**拒绝 —— 悄悄写入一个坏库比拒绝更难查。
"""

from __future__ import annotations

from typing import Any


class UnknownDomain(KeyError):
    """域不存在：调用方拼错了 key，不能静默按空配置处理。"""


class InvalidConfig(ValueError):
    """值或键不合法：接口层转 400，附字段名让界面高亮那一行。"""


class ConfigConflict(Exception):
    """版本冲突：别人先改过。带当前快照，前端可直接覆盖显示。"""

    def __init__(self, version: int, payload: dict[str, Any]) -> None:
        super().__init__(f"配置版本冲突：当前 v{version}")
        self.version = version
        self.payload = payload


class ConfigUnavailable(RuntimeError):
    """配置库不可用：读路径已降级为空覆盖，写路径直接拒绝，接口层转 503。"""


__all__ = ["ConfigConflict", "ConfigUnavailable", "InvalidConfig", "UnknownDomain"]
