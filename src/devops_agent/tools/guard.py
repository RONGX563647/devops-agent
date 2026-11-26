"""敏感路径守卫 —— 权限引擎判不到的那一半（工具中间件，命令式）。

为什么需要这一层（实测见 docs/specs/0010）：permission 引擎拿到的是「一串
`rule_content` + 工具自己实现的 `match_rule`」，语义被钉死成子串或 fnmatch：
`.git/config` 的相对形式所有 glob 候选都漏、`.ENV` 因大小写全漏、Bash 只有子串
匹配可被构造写法绕过。中间件拿到的是**结构化入参 + 真实文件系统**，是唯一能做
realpath 归一化的位置。两层叠加才是完整防线，缺任何一层都有洞：

- 只留声明式：Read/Grep 读 `.env`（五档全 allow）、bypass 档写 `.git/config`；
- 只留中间件：没有可审的声明，出问题时不知道拦了什么。

挂载点固定在 `tools/catalog.build_toolkit_for`：漏挂一次就有一条绕过路径，
「安全默认」必须是默认，而不是调用方记得传。

分工边界：Bash 的 AST 级危险命令分析仍由框架的 `Bash.check_permissions` 负责
（它返回 ASK，dont_ask 档会转 DENY）；这里只做「命令里出现敏感路径 token」的拦截。
"""

from __future__ import annotations

import os
import shlex
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk, ToolMiddlewareBase

from devops_agent.governance import paths

# 各工具里承载路径的参数名；Bash 走命令分词，不在这里
_PATH_PARAM: dict[str, str] = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "path",
    "Grep": "path",
}
_WRITE_TOOLS = frozenset({"Write", "Edit"})

_DENIED = (
    "权限策略拒绝：{reason}。这是策略性拦截，不要重试同一调用——"
    "换一个路径或方案，并如实说明被拦的原因。"
)


class SensitivePathGuard(ToolMiddlewareBase):
    """在工具执行前做一次路径归一化判定，命中即返回 DENIED 且不调用工具。"""

    def __init__(self, *, workdir: str | Path, scope: str = "workspace") -> None:
        self._workdir = Path(os.path.realpath(str(workdir)))
        self._policy = paths.build_policy([self._workdir])
        # 只有只读档把「工作区内」当成硬边界；其余档位的边界由 permission 的
        # working_directories + 兜底动作表达（ask / deny），不必在这里重复。
        self._within_root = scope == "readonly"

    async def on_tool_call(  # type: ignore[override]
        self,
        tool: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        """判定 → 放行（Grep 顺带注入排除）或直接返回 DENIED。"""
        verdict = self.verdict(tool.name, input_kwargs)
        if verdict.denied:
            yield ToolChunk(
                content=[TextBlock(text=_DENIED.format(reason=verdict.reason))],
                state=ToolResultState.DENIED,
            )
            return
        if tool.name == "Grep":
            # 不拒绝整个搜索，只排除敏感文件：拒绝对「搜一个关键词」来说太粗，
            # 而结果里带出 .env 内容才是真正要防的。
            input_kwargs = dict(input_kwargs)
            input_kwargs["glob"] = " ".join(
                part for part in (input_kwargs.get("glob") or "", *paths.GREP_EXCLUSIONS) if part
            )
        async for chunk in next_handler(**input_kwargs):
            yield chunk

    def verdict(self, tool_name: str, input_kwargs: dict[str, Any]) -> paths.Verdict:
        """对一次工具调用做判定（独立成方法，便于不起完整 toolkit 的单测）。"""
        if tool_name == "Bash":
            return self._bash_verdict(str(input_kwargs.get("command") or ""))
        param = _PATH_PARAM.get(tool_name)
        if param is None:
            return paths.ALLOW
        raw = str(input_kwargs.get(param) or ".")
        return paths.check(
            raw,
            base=self._workdir,
            policy=self._policy,
            write=tool_name in _WRITE_TOOLS,
            within_root=self._within_root,
        )

    def _bash_verdict(self, command: str) -> paths.Verdict:
        """逐 token 判定命令里出现的路径。

        只做敏感路径判定，不做工作区边界：`/bin/sh`、`/usr/bin/env` 这类绝对路径
        是正常执行的一部分，按根边界判会把只读档直接打废。真正的边界靠容器/沙箱，
        这在 `governance/model.py` 的产品定位里已经写清。
        """
        for token in tokens(command):
            verdict = paths.check(
                token,
                base=self._workdir,
                policy=self._policy,
                write=False,
            )
            if verdict.denied:
                return verdict
        return paths.ALLOW


def tokens(command: str) -> list[str]:
    """命令分词：优先 shlex（正确处理引号），引号不闭合时退回朴素切分。

    AST 级分析（`$(...)`、 subshell、重定向链）由框架的 Bash 工具负责，这里只要
    「能被当成路径字面量的词」——`shlex` 已经把它们原样切出来了。
    """
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.split()
    return [part for part in parts if part and not part.startswith("-")]


def build_guard(workdir: str | Path, scope: str) -> SensitivePathGuard:
    """装配入口：`build_toolkit_for` 与档案装配共用同一份构造参数。"""
    return SensitivePathGuard(workdir=workdir, scope=scope)
