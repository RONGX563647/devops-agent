"""权限档位 —— 「能碰到哪一层」与「什么时候停下来问」两个维度的组合。

为什么拆成两维
--------------
以前一个档位捆绑了四件事（mode + 工具集 + 命令白名单 + 越界兜底），于是
「工作区可写但不问」这种最普通的需求表达不出来：可选四档里没有一档能做到，
用户只能反复点确认。Codex CLI 把这件事拆成 `sandbox_mode` 与 `approval_policy`
两个互不干涉的配置项，本模块照此拆成：

- `scope`：能力范围。决定**工具集**（只读四件套 / 加写工具）与规则的生效下限。
- `approvals`：打扰程度。**只决定框架 mode（兜底动作）**，不改工具集、不改规则集。

`approvals` 不动工具集是有意为之：系统提示与工具 schema 逐字节不变，所以
「把打扰程度调高一点」不会击穿前缀缓存（实测跨档命中 95%+）。

判据是 `key`（`scope+approvals`），不是 `mode`
---------------------------------------------
`workspace+on-risk` 与 `workspace+never` 的 mode 不同，但 `cautious` 与
`container+always` 的 mode 都是 `default` —— 只比 mode 会漏掉「同 mode 不同档」，
表现为切了档却什么都没变，以及旧档攒的 allow 规则串到新档。`runtime/agent.py`
的 `_state_for` 因此比的是 `profile.key`。

产品定位（必须写在最前面）：**这是防误操作的 UX 层，不是安全隔离。**
它防的是「模型手滑删错目录」，防不住恶意用户。真正的隔离靠容器、只读挂载、
网络策略、非 root 运行。`container` 档的 deny 规则只是点防御。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from devops_agent.config import load_policy
from devops_agent.tools.catalog import READONLY_TOOL_NAMES, WRITE_TOOL_NAMES

# 只读四件套：shell + 读文件 + 找文件 + 搜内容
READONLY_TOOLS: tuple[str, ...] = READONLY_TOOL_NAMES
# 写入两件套：只读档不含，其余档位都含
WRITE_TOOLS: tuple[str, ...] = WRITE_TOOL_NAMES

# 可用档位白名单：绝不允许客户端通过请求体自行扩容到 container
AVAILABLE_ENV = "DEVOPS_AGENT_PERMISSION_PROFILES"
DEFAULT_ENV = "DEVOPS_AGENT_DEFAULT_PROFILE"

# (scope, approvals) → 框架 PermissionMode。不在表里的组合不可实现，直接留白：
# `readonly+always` / `readonly+on-risk` 没有意义 —— `default` 模式下 `mkdir`
# 是 ask，批准一次就能写，破坏只读语义；`explore` 的越界又是 deny，没有中间态。
# 「只读」就是「只读」，不编一个看起来完整的格子。
MODES: dict[tuple[str, str], str] = {
    ("readonly", "never"): "explore",
    ("workspace", "always"): "default",
    ("workspace", "on-risk"): "accept_edits",
    ("workspace", "never"): "dont_ask",
    ("container", "always"): "default",
    ("container", "on-risk"): "bypass",
    ("container", "never"): "bypass",
}

# 打扰程度的三个取值，顺序即从「最谨慎」到「最安静」，前端按这个顺序展示。
APPROVALS: tuple[str, ...] = ("always", "on-risk", "never")


@dataclass(frozen=True, slots=True)
class PermissionProfile:
    """一个权限档位 = 能力范围 + 打扰程度。"""

    label: str
    scope: str
    approvals: str
    tools: tuple[str, ...]
    note: str = ""
    # 预设名（`workspace` / `cautious` …）：前端请求体、DB 记录、规则列表用它。
    # 与 `key`（组合串）分开：前者是「用户选了什么」，后者是「编译成什么」。
    preset: str = ""

    def __post_init__(self) -> None:
        if (self.scope, self.approvals) not in MODES:
            raise ValueError(f"不可实现的档位组合: {self.scope}+{self.approvals}")

    @property
    def key(self) -> str:
        """重建判据。任一维度变化都必须重建权限上下文。"""
        return f"{self.scope}+{self.approvals}"

    @property
    def mode(self) -> str:
        """框架 PermissionMode 的字符串形态（保持本模块不 import 框架）。"""
        return MODES[(self.scope, self.approvals)]


PROFILES: dict[str, PermissionProfile] = {
    "readonly": PermissionProfile(
        label="只读",
        preset="readonly",
        scope="readonly",
        approvals="never",
        tools=READONLY_TOOLS,
        note="只读浏览：不下发写工具，越界命令直接拒绝，不打断你。",
    ),
    "workspace": PermissionProfile(
        label="工作区可写",
        preset="workspace",
        scope="workspace",
        approvals="on-risk",
        tools=READONLY_TOOLS + WRITE_TOOLS,
        note="工作区可读写；测试/构建命令直接执行；git 与其他命令逐次询问。",
    ),
    "cautious": PermissionProfile(
        label="逐个确认",
        preset="cautious",
        scope="workspace",
        approvals="always",
        tools=READONLY_TOOLS + WRITE_TOOLS,
        note="有写能力，但每个动作都确认 —— 连之前放行过的规则也会再问一次。",
    ),
    "unattended": PermissionProfile(
        label="无人值守",
        preset="unattended",
        scope="workspace",
        approvals="never",
        tools=READONLY_TOOLS + WRITE_TOOLS,
        note="定时任务用：读与测试/构建直接执行，其余直接拒绝，不弹确认（没人在）。",
    ),
    "container": PermissionProfile(
        label="全放开",
        preset="container",
        scope="container",
        approvals="never",
        tools=READONLY_TOOLS + WRITE_TOOLS,
        note="不逐次询问，只剩内置 deny 与高危命令询问；挡不住脚本级破坏，别在生产用。",
    ),
}

# 弃用别名：老会话的 localStorage、旧 run 记录的 permission 列里还是这些名字。
# 直接 403 会让「上次还能用的会话」突然打不开，所以映射到新档并记一条 warning。
ALIASES: dict[str, str] = {
    "execute": "workspace",
    "auto": "unattended",
    "unrestricted": "container",
}

# 默认可用集：不含 container —— 它跳过逐次确认，要显式配置才开放
DEFAULT_AVAILABLE: tuple[str, ...] = ("readonly", "workspace", "cautious", "unattended")


class UnknownProfile(ValueError):
    """请求了不存在或未开放的档位。"""


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _configured_names() -> list[str]:
    raw = os.environ.get(AVAILABLE_ENV)
    return _split(raw) if raw else (load_policy().permission.available or list(DEFAULT_AVAILABLE))


def normalize(name: str) -> str | None:
    """把档位名归一成预设名：别名、组合串（`workspace+on-risk`）都认。"""
    if name in PROFILES:
        return name
    if name in ALIASES:
        return ALIASES[name]
    for preset, profile in PROFILES.items():
        if profile.key == name:
            return preset
    return None


def available_names() -> list[str]:
    """当前部署开放的预设名，顺序固定（前端请求体里用的就是这个名字）。"""
    resolved = {preset for key in _configured_names() if (preset := normalize(key)) is not None}
    return [preset for preset in PROFILES if preset in resolved]


def available() -> list[PermissionProfile]:
    """当前部署开放的档位；未知名字跳过，配置写错不该让整个服务起不来。"""
    return [PROFILES[preset] for preset in available_names()]


def default_profile() -> PermissionProfile:
    """默认档位：环境变量 > policy.yaml > workspace；越权配置回落可用集第一项。"""
    name = os.environ.get(DEFAULT_ENV) or load_policy().permission.default_profile
    names = available_names()
    preset = normalize(name)
    if preset is not None and preset in set(names):
        return PROFILES[preset]
    return PROFILES[names[0]] if names else PROFILES["workspace"]


def resolve(name: str | None) -> PermissionProfile:
    """把请求里的档位名解析成档位对象；未开放或不存在一律抛 UnknownProfile。

    不做「静默降级到默认档」：客户端以为自己在放开档、实际被降成只读，
    比直接报错更难排查。别名是唯一例外 —— 那是同一个档改名，不是降级。
    """
    if not name:
        return default_profile()
    preset = normalize(name)
    if preset is None or preset not in set(available_names()):
        raise UnknownProfile(name)
    return PROFILES[preset]
