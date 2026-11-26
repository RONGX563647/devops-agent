"""本部署开放哪些档位 —— 环境变量 > 内置默认。

`policy.yaml` 里的 `available` / `default_profile` 已删除：同一份可用集在 yaml 与
代码里各写一遍（逐字相同）是纯冗余，改一处忘另一处就出现「前端列出的档位不是
真正开放的档位」。

宽容策略（沿用旧行为）：配置里写了不认识的名字 → 跳过该名字，不让服务起不来；
但请求里要一个未开放/不存在的档位 → 抛 `UnknownProfile`，**不静默降级**：
客户端以为自己在放开档、实际被降成只读，比直接报错更难排查。
"""

from __future__ import annotations

import os
from functools import lru_cache

from devops_agent.governance.model import (
    DEFAULT_AVAILABLE,
    DEFAULT_PROFILE_KEY,
    PROFILES,
    SAFETY_ORDER,
    PermissionProfile,
)

# 可用档位白名单：绝不允许客户端通过请求体自行扩容到 unrestricted
AVAILABLE_ENV = "DEVOPS_AGENT_PERMISSION_PROFILES"
DEFAULT_ENV = "DEVOPS_AGENT_DEFAULT_PROFILE"


class UnknownProfile(ValueError):
    """请求了不存在或未开放的档位。"""


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=8)
def _keys_from(raw: str | None) -> tuple[str, ...]:
    """解析可用档位名；缓存键就是环境变量原文。

    按输入缓存而不是缓存「当前结果」：同一请求内反复调用得到同一答案（旧实现每次
    重读 env，两次调用可能不一致），而测试改 env 后换个键自然重算。
    """
    names = _split(raw) if raw else list(DEFAULT_AVAILABLE)
    # 未知名字直接跳过：配置写错了不该让整个服务起不来
    return tuple(name for name in names if name in PROFILES)


def available_keys() -> tuple[str, ...]:
    """当前部署开放的档位 key，保持配置里的顺序。"""
    return _keys_from(os.environ.get(AVAILABLE_ENV))


def available() -> list[PermissionProfile]:
    """当前部署开放的档位。"""
    return [PROFILES[key] for key in available_keys()]


def default_profile() -> PermissionProfile:
    """默认档位：env > 内置默认；配置越权时回落**最严可用档**。

    旧实现回落 `sorted(allowed)[0]`，字典序首个是 `auto`（无人值守）—— 配置写错
    反而把用户推向最不想有人盯的档位。`SAFETY_ORDER` 是这个回落的唯一依据。
    """
    name = os.environ.get(DEFAULT_ENV) or DEFAULT_PROFILE_KEY
    allowed = available_keys()
    if name in allowed:
        return PROFILES[name]
    for key in SAFETY_ORDER:
        if key in allowed:
            return PROFILES[key]
    return PROFILES["readonly"]


def resolve(name: str | None) -> PermissionProfile:
    """把请求里的档位名解析成档位对象；未开放或不存在一律抛 `UnknownProfile`。"""
    if not name:
        return default_profile()
    if name not in available_keys():
        raise UnknownProfile(name)
    return PROFILES[name]
