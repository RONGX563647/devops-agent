"""配置中心：把运行期部署策略收进一张可 diff 的覆盖表（SQLite）。

分层（高 → 低）：环境变量白名单 > config.db 覆盖 > config/*.yaml 基线 > 代码默认。

三条不变量：

1. **只存差异**：写进来的值若与下层相同，直接删行 —— 库里出现的每一行都是
   「有人刻意改过」，界面上不用再猜「这行是不是历史残留」。
2. **默认值只在代码里**：pydantic 字段默认值就是默认层，升级代码改默认不需要迁移。
3. **yaml 是可选的评审基线**：仓库里不再保留与代码默认同值的部署 yaml（那是第二份
   默认值），但 `config/<域>.yaml` 仍会被读取 —— 想在 PR 里 diff 一份基线时把文件
   加回来即可，无数据库的环境（CI / 测试）也照常工作。

失败语义（与记忆层同构）：**读降级、写拒绝**。读库失败时按「没有覆盖」继续服务，
但会记进 `health` 并由 `/health` 暴露；写库失败直接抛 `ConfigUnavailable`（503），
不往坏库里塞数据。

这里不 import `api`：`config.py` 会 import 本包的存取层，若 `__init__` 顺带把
FastAPI 路由也拉进来，配置加载就凭空依赖 Web 框架。路由由装配根显式挂。
"""

from __future__ import annotations

from devops_agent.config_store import health as health
from devops_agent.config_store.registry import BY_KEY, DOMAINS, DomainSpec
from devops_agent.config_store.schema import (
    CONFIG_DB_ENV,
    MODE_ENV,
    VALID_MODES,
    db_path,
    mode,
)
from devops_agent.config_store.store import (
    ConfigConflict,
    ConfigUnavailable,
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
    "VALID_MODES",
    "ConfigConflict",
    "ConfigUnavailable",
    "DomainSpec",
    "InvalidConfig",
    "UnknownDomain",
    "audit",
    "bounds",
    "db_path",
    "effective",
    "export_yaml",
    "health",
    "index",
    "invalidate",
    "mode",
    "patch",
    "reset",
    "resolved",
]
