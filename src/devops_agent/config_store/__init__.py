"""配置中心：把分散的 config/*.yaml 收进一张可 diff 的覆盖表（SQLite）。

分层（高 → 低）：环境变量白名单 > config.db 覆盖 > config/*.yaml 基线 > 代码默认。

三条不变量：

1. **只存差异**：写进来的值若与下层相同，直接删行 —— 库里出现的每一行都是
   「有人刻意改过」，界面上不用再猜「这行是不是历史残留」。
2. **默认值只在代码里**：pydantic 字段默认值就是默认层，升级代码改默认不需要迁移。
3. **yaml 仍是仓库基线**：它可评审、可 diff、可在没有数据库的环境（CI / 测试）
   独立工作；DB 只压在上面，不会把 yaml 吞掉。

这里不 import `api`：`config.py` 会 import 本包的存取层，若 `__init__` 顺带把
FastAPI 路由也拉进来，配置加载就凭空依赖 Web 框架。路由由装配根显式挂。
"""

from __future__ import annotations

from devops_agent.config_store.registry import BY_KEY, DOMAINS, DomainSpec
from devops_agent.config_store.schema import CONFIG_DB_ENV, MODE_ENV, db_path, mode
from devops_agent.config_store.store import (
    ConfigConflict,
    InvalidConfig,
    UnknownDomain,
    audit,
    bounds,
    effective,
    export_yaml,
    index,
    invalidate,
    patch,
    reset,
    resolved,
)

__all__ = [
    "BY_KEY",
    "CONFIG_DB_ENV",
    "DOMAINS",
    "MODE_ENV",
    "ConfigConflict",
    "DomainSpec",
    "InvalidConfig",
    "UnknownDomain",
    "audit",
    "bounds",
    "db_path",
    "effective",
    "export_yaml",
    "index",
    "invalidate",
    "mode",
    "patch",
    "reset",
    "resolved",
]
