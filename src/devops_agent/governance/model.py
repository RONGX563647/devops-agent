"""权限的单一事实来源 —— 档位与 builtin 规则的声明。

为什么规则不在 `config/policy.yaml`（本次重构的核心决策）：

1. 权限规则是**代码契约**：它有跨平台语义（glob 与路径分隔符）、与工具集的耦合
   （`.git` 只在挂了写工具时才有意义）、以及一张必须跑的判定矩阵。yaml 里改一个
   字符串就能让矩阵失真，而没有人复核。
2. 现状已经证明「配置存在但零命中」是最难发现的缺陷：`.git/`、`.env` 两条 deny
   规则在 Write/Edit 上从未命中过（框架用 fnmatch 匹配 file_path，`.git/` 这种
   形态的 pattern 匹配不到任何真实路径），可它们看起来一直在生效 —— 真正的保护
   来自框架内建的 safety-ask，不是这两条规则。
3. Python 常量由 mypy 与单测直接覆盖；yaml 只在加载期报错，报错还不带键路径。

本模块不 import agentscope、不读环境变量、不做 IO：它只有数据。
环境变量解析在 `presets.py`，框架对象编译在 `compile.py`。
"""

from __future__ import annotations

from dataclasses import dataclass

from devops_agent.tools.names import READONLY_TOOL_NAMES, WRITE_TOOL_NAMES

# 只读四件套：shell + 读文件 + 找文件 + 搜内容
READONLY_TOOLS: tuple[str, ...] = READONLY_TOOL_NAMES
# 写入两件套：只在放开档出现
WRITE_TOOLS: tuple[str, ...] = WRITE_TOOL_NAMES


@dataclass(frozen=True, slots=True)
class PermissionProfile:
    """一个能力档位 = mode + 工具集 + 越界兜底动作，三者一起换。"""

    key: str
    label: str
    # 对应 PermissionMode 的取值。保持字符串，避免本模块 import 框架。
    mode: str
    tools: tuple[str, ...]
    # 越界时的兜底动作：deny（直接拒）/ ask（问用户）/ hard_deny（deny_rules 硬拦）
    fallback: str
    note: str
    # 能力边界：readonly 只读且不出工作区 / workspace 工作区可写 / container 不限。
    # 与 framework 的 mode 是两个维度：mode 决定「无规则命中时怎么兜」，scope 决定
    # 「哪些路径算界内」。混在一起就会出现「只读档能读全盘」这种组合。
    scope: str = "workspace"

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
        note="只能读工作区内的文件、跑只读命令；被拦的调用直接拒绝，不打断你。",
        scope="readonly",
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
    ),
    "unrestricted": PermissionProfile(
        key="unrestricted",
        label="全放开",
        mode="bypass",
        tools=READONLY_TOOLS + WRITE_TOOLS,
        fallback="hard_deny",
        note="不再逐次询问，只剩 deny_rules 兜底：挡不住脚本级破坏，别在生产用。",
        scope="container",
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

# 默认档：前端不传档位时用它
DEFAULT_PROFILE_KEY = "workspace"

# 默认可用集：不含 unrestricted —— 要显式配置才开放
DEFAULT_AVAILABLE: tuple[str, ...] = ("readonly", "workspace", "execute", "auto")

# 由严到宽的稳定顺序。默认档配置写错时按它回落，**不能用 sorted()**：
# 字典序首个是 "auto"（无人值守），等于配置错误把用户推向最不想有人盯的档位。
SAFETY_ORDER: tuple[str, ...] = ("readonly", "workspace", "execute", "auto", "unrestricted")

# 白名单属于「档位的能力」，不是「部署的配置」：execute 档存在的理由就是它。
# 前缀形如 "pytest:*"，框架把复合命令拆成子命令逐条匹配，所以
# "pytest; rm -rf /" 里的 rm 匹配不上任何一条，整条不会被放行。
ALLOW_PREFIXES: dict[str, tuple[str, ...]] = {
    "execute": (
        "pytest:*",
        "python -m pytest:*",
        "python -m ruff:*",
        "python -m mypy:*",
        "npm run:*",
        "npm test:*",
        "npm ci:*",
        "npm install:*",
        "npx:*",
        "pnpm:*",
        "yarn:*",
        "make:*",
        "cargo:*",
        "go test:*",
        "go build:*",
    ),
}


@dataclass(frozen=True, slots=True)
class Rule:
    """一条声明式规则；`pattern=None` 表示整个工具。

    `pattern` 的语义由工具决定（Bash 是子串/通配/`:*` 前缀，文件工具是 fnmatch
    glob），所以这里只存字符串，翻译成框架对象在 `compile.py`。
    """

    tool: str
    pattern: str | None
    behavior: str = "deny"
    source: str = "builtin"


# builtin deny：对所有档位无条件装配（框架层 deny 优先于 allow/ask）。
# 这是点防御，不是隔离：Bash 走子串匹配，挡不住 `python -c "..."` 那类构造。
BUILTIN_DENY: tuple[Rule, ...] = (
    Rule(tool="Bash", pattern=".git/"),
    Rule(tool="Bash", pattern=".env"),
    Rule(tool="Write", pattern=".git/"),
    Rule(tool="Edit", pattern=".git/"),
)
