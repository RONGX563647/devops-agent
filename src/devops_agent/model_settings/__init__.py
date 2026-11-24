"""模型参数的解析、校验与派生值 —— 全仓库唯一决定「参数取哪个值」的地方。

优先级（高 → 低）：

    会话覆盖（session_states.model_config）
      > 部署默认（settings['model_config']，前端改、持久化）
      > config/model.yaml
      > 环境变量（仅 model 名）
      > 框架默认（context_size / 重试 / ContextConfig 比例）

**任何地方需要参数，只能走 `resolve()`**，不要自己读 yaml 或 env ——
第二处读取就会出现第二份默认值，而「这个 128000 是谁设的」正是本模块要回答的问题：
每个值都带 `Source`，前端据此画来源徽标。

三个刻意的取舍：

1. `None` 表示「用下层默认」，不是「设成 null 值」。patch 里给 null 等于删除覆盖项，
   否则「把温度改回默认」和「把温度设成 0」在界面上分不开。
2. 推算值（压缩阈值、最坏尝试次数）由后端算好给前端，前端不重复实现口径 ——
   健康分 v1 就是两边各算一套然后对不上。
3. 校验只在这里做。前端可以提前提示，但服务端是唯一裁判。

实现按职责拆在 `bounds` / `resolve` / `payload` / `params`，
此文件只做门面 re-export，对外接口与原单文件模块保持一致。
"""

from __future__ import annotations

from devops_agent.model_settings.bounds import (
    BOUNDS,
    EDITABLE,
    READONLY,
    REASONING_EFFORTS,
    Bound,
    InvalidConfig,
    apply_patch,
    bound_payload,
    bounds_payload,
    validate,
)
from devops_agent.model_settings.params import (
    ModelParams,
    effective_params,
    params_sha,
)
from devops_agent.model_settings.payload import derived, payload
from devops_agent.model_settings.resolve import (
    MODEL_ENV,
    SETTING_KEY,
    STORED_TTL_S,
    Resolved,
    Source,
    invalidate,
    resolve,
    sources_of,
    stored_defaults,
    stored_version,
    values_of,
)

__all__ = [
    "BOUNDS",
    "EDITABLE",
    "MODEL_ENV",
    "READONLY",
    "REASONING_EFFORTS",
    "SETTING_KEY",
    "STORED_TTL_S",
    "Bound",
    "InvalidConfig",
    "ModelParams",
    "Resolved",
    "Source",
    "apply_patch",
    "bound_payload",
    "bounds_payload",
    "derived",
    "effective_params",
    "invalidate",
    "params_sha",
    "payload",
    "resolve",
    "sources_of",
    "stored_defaults",
    "stored_version",
    "validate",
    "values_of",
]
