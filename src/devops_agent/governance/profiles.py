"""权限档位 —— 「默认放行到哪一层」的声明式定义。

为什么档位不是直接暴露 5 个 PermissionMode：
PermissionMode 描述的是判定策略（`accept_edits` / `dont_ask`），名字是给实现者看的；
用户需要选的是「能让它干到哪一步」。

所以一个档位 = **mode + 规则预设（白名单 / 兜底）**。工具集不再随档位变化：
所有档位装配同一个 Toolkit，能不能执行由框架的 PermissionEngine 判定。
这样工具 schema 在档位之间字节一致，切档不会打断前缀缓存。

产品定位（必须写在最前面）：**这是防误操作的 UX 层，不是安全隔离。**
它防的是「模型手滑删错目录」，防不住恶意用户。真正的隔离靠容器、只读挂载、
网络策略、非 root 运行。`unrestricted` 的 deny_rules 只是点防御。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from devops_agent.config import load_policy

# 可用档位白名单：绝不允许客户端通过请求体自行扩容到 unrestricted
AVAILABLE_ENV = "DEVOPS_AGENT_PERMISSION_PROFILES"
DEFAULT_ENV = "DEVOPS_AGENT_DEFAULT_PROFILE"


@dataclass(frozen=True, slots=True)
class PermissionProfile:
    """一个能力档位。"""

    key: str
    label: str
    # 对应 PermissionMode 的取值（保持字符串，避免本模块 import 框架）。
    mode: str
    # 越界时的兜底动作：deny（直接拒）/ ask（问用户）/ hard_deny（deny_rules 硬拦）
    fallback: str
    note: str
    # 供前端提示「这个档位能不能落盘」；实际执行仍由 PermissionEngine 判定
    write: bool = False
    # 是否把 policy.command_allowlist 作为 allow_rules 注入
    allowlist: bool = False


PROFILES: dict[str, PermissionProfile] = {
    "readonly": PermissionProfile(
        key="readonly",
        label="只读",
        mode="explore",
        fallback="deny",
        note="只能读文件、跑只读命令；被拦的调用直接拒绝，不打断你。",
    ),
    "workspace": PermissionProfile(
        key="workspace",
        label="工作区可写",
        mode="accept_edits",
        fallback="ask",
        note="可在工作区内建/删/改文件；越界操作会先弹卡片问你。",
        write=True,
    ),
    "execute": PermissionProfile(
        key="execute",
        label="可执行",
        mode="accept_edits",
        fallback="ask",
        note="工作区权限 + 白名单命令（pytest / npm run 等）；越界仍会先问。",
        write=True,
        allowlist=True,
    ),
    "unrestricted": PermissionProfile(
        key="unrestricted",
        label="全放开",
        mode="bypass",
        fallback="hard_deny",
        write=True,
        note="不再逐次询问，只剩 deny_rules 兜底：挡不住脚本级破坏，别在生产用。",
    ),
    "auto": PermissionProfile(
        key="auto",
        label="无人值守",
        mode="dont_ask",
        fallback="deny",
        note="给定时任务用：越界一律拒绝，不弹确认（因为没人在）。",
    ),
}

# 默认可用集：不含 unrestricted —— 要显式配置才开放
DEFAULT_AVAILABLE: tuple[str, ...] = ("readonly", "workspace", "execute", "auto")


class UnknownProfile(ValueError):
    """请求了不存在或未开放的档位。"""


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def available() -> list[PermissionProfile]:
    """当前部署开放的档位：环境变量 > policy.yaml > 内置默认。"""
    raw = os.environ.get(AVAILABLE_ENV)
    keys = _split(raw) if raw else (load_policy().permission.available or list(DEFAULT_AVAILABLE))
    # 未知名字直接跳过：配置写错了不该让整个服务起不来
    return [PROFILES[key] for key in keys if key in PROFILES]


def default_profile() -> PermissionProfile:
    """默认档位：环境变量 > policy.yaml > workspace；越权配置回落可用集第一项。"""
    name = os.environ.get(DEFAULT_ENV) or load_policy().permission.default_profile
    allowed = {item.key for item in available()}
    if name in allowed:
        return PROFILES[name]
    if allowed:
        return PROFILES[sorted(allowed)[0]]
    return PROFILES["workspace"]


def resolve(name: str | None) -> PermissionProfile:
    """把请求里的档位名解析成档位对象；未开放或不存在一律抛 UnknownProfile。

    不做「静默降级到默认档」：客户端以为自己在放开档、实际被降成只读，
    比直接报错更难排查。
    """
    if not name:
        return default_profile()
    allowed = {item.key for item in available()}
    if name not in allowed:
        raise UnknownProfile(name)
    return PROFILES[name]
