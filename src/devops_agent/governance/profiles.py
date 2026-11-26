"""权限档位 —— 「默认放行到哪一层」的声明式定义。

为什么档位不是直接暴露 5 个 PermissionMode：
PermissionMode 描述的是判定策略（`accept_edits` / `dont_ask`），名字是给实现者看的；
用户需要选的是「能让它干到哪一步」。而且只切 mode、工具集不动的话，有些档位
选了完全没有可观测变化（`accept_edits` 的卖点是工作区内编辑自动放行，但工具集里
压根没有 Write/Edit）。

所以一个档位 = **mode + 工具集 + 越界兜底动作**，三者一起换。

产品定位（必须写在最前面）：**这是防误操作的 UX 层，不是安全隔离。**
它防的是「模型手滑删错目录」，防不住恶意用户。真正的隔离靠容器、只读挂载、
网络策略、非 root 运行。`unrestricted` 的 deny_rules 只是点防御。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from devops_agent.config import load_policy
from devops_agent.tools.catalog import READONLY_TOOL_NAMES, WRITE_TOOL_NAMES

# 只读四件套：shell + 读文件 + 找文件 + 搜内容
READONLY_TOOLS: tuple[str, ...] = READONLY_TOOL_NAMES
# 写入两件套：只在放开档出现
WRITE_TOOLS: tuple[str, ...] = WRITE_TOOL_NAMES

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
    tools: tuple[str, ...]
    # 越界时的兜底动作：deny（直接拒）/ ask（问用户）/ hard_deny（deny_rules 硬拦）
    fallback: str
    note: str
    # 是否把 policy.command_allowlist 作为 allow_rules 注入
    allowlist: bool = False

    @property
    def can_write(self) -> bool:
        """该档位是否带写工具（前端据此渲染「可建/删/改文件」的提示）。

        由工具集求交集，而不是由调用方去 `"write" in tools` 猜名字：工具改名时
        那种探测会静默失真，而这里会跟着 `WRITE_TOOL_NAMES` 一起变。
        """
        return bool(set(self.tools) & set(WRITE_TOOLS))


PROFILES: dict[str, PermissionProfile] = {
    "readonly": PermissionProfile(
        key="readonly",
        label="只读",
        mode="explore",
        tools=READONLY_TOOLS,
        fallback="deny",
        note="只能读文件、跑只读命令；被拦的调用直接拒绝，不打断你。",
    ),
    "workspace": PermissionProfile(
        key="workspace",
        label="工作区可写",
        mode="accept_edits",
        # 说明里承诺「可在工作区内建/删/改文件」，那就必须真给写工具：
        # 只挂只读四件套时，这一档与只读档的唯一差别只剩「越界问不问」，
        # 用户看到的是「切了档什么都没变」。
        tools=READONLY_TOOLS + WRITE_TOOLS,
        fallback="ask",
        note="可在工作区内建/删/改文件；越界操作会先弹卡片问你。",
    ),
    "execute": PermissionProfile(
        key="execute",
        label="可执行",
        mode="accept_edits",
        # 「工作区权限 + 白名单命令」：写工具同样要带，否则白名单里的
        # 构建命令改不了产物，等于只能跑不能干。
        tools=READONLY_TOOLS + WRITE_TOOLS,
        fallback="ask",
        note="工作区权限 + 白名单命令（pytest / npm run 等）；越界仍会先问。",
        allowlist=True,
    ),
    "unrestricted": PermissionProfile(
        key="unrestricted",
        label="全放开",
        mode="bypass",
        tools=READONLY_TOOLS + WRITE_TOOLS,
        fallback="hard_deny",
        note="不再逐次询问，只剩 deny_rules 兜底：挡不住脚本级破坏，别在生产用。",
    ),
    "auto": PermissionProfile(
        key="auto",
        label="无人值守",
        mode="dont_ask",
        # 与工作区档同权限、只是不问人：dont_ask 把「工作区内的写」放行、
        # 把越界降级成拒绝。不给写工具的话，定时任务连产物都存不下来。
        tools=READONLY_TOOLS + WRITE_TOOLS,
        fallback="deny",
        note="给定时任务用：工作区内可读写，越界一律拒绝且不弹确认（因为没人在）。",
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
