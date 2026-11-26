"""敏感路径防线的行为测试：声明式规则、工具中间件、bypass 档兜底。

三层各自都要有自己的断言 —— 只测「规则存在」会漏掉零命中（`.git/` 那两条
deny 规则就是这样在生产里静默失效了很久），只测中间件会漏掉引擎层可审的声明。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import Edit, Glob, Grep, Read, ToolChunk, Write

from devops_agent.governance import paths
from devops_agent.governance.compile import build_engine, compile_context
from devops_agent.governance.decide import bash_decision
from devops_agent.governance.model import DANGER_COMMANDS, INTERPRETER_FLAGS, PROFILES
from devops_agent.tools.catalog import build_toolkit_for
from devops_agent.tools.guard import SensitivePathGuard

_WORKDIR = Path.cwd()


def decide(profile_key: str, tool: Any, payload: dict[str, Any], workdir: Path) -> str:
    engine = build_engine(PROFILES[profile_key], workdir=workdir)
    return asyncio.run(engine.check_permission(tool, payload)).behavior.value


# ---------------------------------------------------------------- 名单与归一化


def _covered_by_our_lists(name: str) -> bool:
    """框架的 danger 名是否被我们的名单覆盖（按文件 / 父目录 / basename 三种方式）。"""
    if name in paths.SENSITIVE_FILES:
        return True
    head = name.split("/", 1)[0]
    if head in paths.SENSITIVE_DIRS or head in paths.WRITE_ONLY_DIRS:
        return True
    return name.rsplit("/", 1)[-1] in paths.SENSITIVE_FILES


def test_sensitive_lists_cover_framework_defaults() -> None:
    """框架的 danger 清单是同行评审过的，我们只能是它的超集。

    这条断言的作用不是炫耀一致性，而是**在框架加名字时把测试打红**：
    没有它，升级 AgentScope 后新增的 `.env.production` 之类会静默漏掉。
    """
    writer = Write()
    assert all(_covered_by_our_lists(name) for name in writer.dangerous_files)
    assert all(_covered_by_our_lists(name) for name in writer.dangerous_directories)


@pytest.mark.parametrize(
    "raw",
    [
        ".env",
        "./.env",
        "sub/../.env",
        ".ENV",
        ".env.local",
        "server.pem",
        "deploy.key",
        "id_rsa",
        "credentials",
        ".npmrc",
        "C:\\repo\\.env",
    ],
)
def test_any_spelling_of_a_secret_is_denied(raw: str) -> None:
    """拼写变体必须全中：大小写、`..`、反斜杠、相对/绝对 —— 一条 glob 做不到这些。"""
    verdict = paths.check(raw, base=_WORKDIR, policy=paths.build_policy([_WORKDIR]))
    assert verdict.denied, raw


@pytest.mark.parametrize("raw", [".git/config", "/repo/.git/config", "C:\\repo\\.git\\config"])
def test_git_dir_is_denied_regardless_of_separator(raw: str) -> None:
    verdict = paths.check(raw, base=_WORKDIR, policy=paths.build_policy())
    assert verdict.denied and "git" in verdict.reason, raw


@pytest.mark.parametrize("raw", ["app.py", "app.env", "README.md", "src/main.py", ".gitignore"])
def test_ordinary_paths_are_not_denied(raw: str) -> None:
    """`.env` 的裸子串曾把 `cat app.env` 误判成读密钥；归一化后按 basename 精确比对。"""
    assert not paths.check(raw, base=_WORKDIR, policy=paths.build_policy()).denied, raw


# ---------------------------------------------------------------- 引擎层（声明式）


def test_engine_denies_sensitive_reads_in_every_profile(tmp_path: Path) -> None:
    for key in PROFILES:
        assert decide(key, Read(), {"file_path": str(tmp_path / ".env")}, tmp_path) == "deny", key


def test_engine_denies_sensitive_writes_in_every_profile(tmp_path: Path) -> None:
    target = str(tmp_path / ".git" / "config")
    for key in PROFILES:
        assert decide(key, Write(), {"file_path": target, "content": "x"}, tmp_path) == "deny", key
        assert (
            decide(
                key,
                Edit(),
                {"file_path": target, "old_string": "a", "new_string": "b"},
                tmp_path,
            )
            == "deny"
        ), key


def test_engine_denies_grep_inside_git_dir(tmp_path: Path) -> None:
    for key in PROFILES:
        payload = {"pattern": "token", "path": str(tmp_path / ".git")}
        assert decide(key, Grep(), payload, tmp_path) == "deny", key
        assert decide(key, Glob(), {"path": str(tmp_path / ".git")}, tmp_path) == "deny", key


def test_ordinary_files_still_work(tmp_path: Path) -> None:
    assert (
        decide(
            "workspace", Write(), {"file_path": str(tmp_path / "a.py"), "content": "x"}, tmp_path
        )
        == "allow"
    )
    assert decide("readonly", Read(), {"file_path": str(tmp_path / "a.py")}, tmp_path) == "allow"


# ---------------------------------------------------------------- bypass 档兜底


def test_danger_commands_denied_only_in_bypass(tmp_path: Path) -> None:
    """bypass 跳过框架 safety-ask：这一档必须自己 deny，别档留 ASK。"""
    for command in DANGER_COMMANDS[:2]:
        assert bash_decision(PROFILES["unrestricted"], command, tmp_path) == "deny"
        assert bash_decision(PROFILES["workspace"], command, tmp_path) == "ask"


def test_interpreter_flags_denied_only_in_bypass(tmp_path: Path) -> None:
    """`python -c` 在别档是 ASK（人确认），在 bypass 档必须拒：那里没人可问。

    `python -c "open(chr(46)+...)"` 这类构造写法能绕开子串匹配，所以这一档不能只
    靠 `.env` 子串规则。
    """
    for flag in INTERPRETER_FLAGS:
        assert bash_decision(PROFILES["unrestricted"], f"{flag} pass", tmp_path) == "deny", flag


def test_container_rules_do_not_leak_into_workspace(tmp_path: Path) -> None:
    context = compile_context(PROFILES["workspace"])
    patterns = [rule.rule_content for rule in context.deny_rules.get("Bash", [])]
    assert "rm -rf" not in patterns
    assert "python -c" not in patterns


def test_allowlist_excludes_remote_fetchers(tmp_path: Path) -> None:
    """`npx` / `pnpm` / `yarn` 会去远端拉包，`make` 跑任意 Makefile 目标：都不预放行。"""
    for command in ("npx some-pkg", "pnpm dlx some-pkg", "yarn some-pkg", "make deploy"):
        assert bash_decision(PROFILES["execute"], command, tmp_path) == "ask", command
    assert bash_decision(PROFILES["execute"], "pytest -q", tmp_path) == "allow"


# ---------------------------------------------------------------- 中间件


def _guard(tmp_path: Path, scope: str = "workspace") -> SensitivePathGuard:
    return SensitivePathGuard(workdir=tmp_path, scope=scope)


def test_guard_verdicts(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    assert guard.verdict("Read", {"file_path": ".env"}).denied
    assert guard.verdict("Write", {"file_path": ".git/config", "content": "x"}).denied
    assert guard.verdict("Grep", {"pattern": "AWS", "path": ".ssh"}).denied
    assert not guard.verdict("Read", {"file_path": "app.py"}).denied
    assert not guard.verdict("Read", {"file_path": "app.env"}).denied


def test_guard_readonly_bounds_file_tools(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escape.txt"
    assert _guard(tmp_path, "readonly").verdict("Read", {"file_path": str(outside)}).denied
    assert not _guard(tmp_path, "workspace").verdict("Read", {"file_path": str(outside)}).denied


def test_guard_matches_bash_tokens(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    assert guard.verdict("Bash", {"command": "cat .env"}).denied
    assert guard.verdict("Bash", {"command": "cp .env /tmp/x"}).denied
    assert guard.verdict("Bash", {"command": "grep -r token .ssh/"}).denied
    assert not guard.verdict("Bash", {"command": "cat app.env"}).denied
    assert not guard.verdict("Bash", {"command": "ls -la"}).denied


async def _drain(agen: AsyncGenerator[ToolChunk, None]) -> list[ToolChunk]:
    return [chunk async for chunk in agen]


def test_guard_short_circuits_denied_call(tmp_path: Path) -> None:
    """命中即返回 DENIED 且**不调用**工具：否则拒绝变成了「先执行再报错」。"""
    guard = _guard(tmp_path)

    class FakeTool:
        name = "Read"

    async def boom(**kwargs: Any) -> AsyncGenerator[ToolChunk, None]:
        raise AssertionError("被拒绝的调用不应进入工具")
        yield ToolChunk(content=[])  # pragma: no cover - 语法占位

    chunks = asyncio.run(_drain(guard.on_tool_call(FakeTool(), {"file_path": ".env"}, boom)))
    assert len(chunks) == 1
    assert chunks[0].state == ToolResultState.DENIED
    assert "不要重试" in chunks[0].content[0].text


def test_guard_injects_grep_exclusions(tmp_path: Path) -> None:
    """Grep 不整体拒绝，只把敏感文件排除掉 —— 拒搜索太粗，漏内容才是事故。"""
    guard = _guard(tmp_path)
    seen: dict[str, Any] = {}

    class FakeGrep:
        name = "Grep"

    async def handler(**kwargs: Any) -> AsyncGenerator[ToolChunk, None]:
        seen.update(kwargs)
        yield ToolChunk(content=[TextBlock(text="ok")])

    chunks = asyncio.run(_drain(guard.on_tool_call(FakeGrep(), {"pattern": "AWS"}, handler)))
    assert chunks[0].content[0].text == "ok"
    assert "!.env" in seen["glob"]
    assert "!.pem" in seen["glob"] or "!*.pem" in seen["glob"]


def test_toolkit_mounts_guard_by_default(tmp_path: Path) -> None:
    """守卫必须是默认行为：靠调用方记得传 = 迟早漏挂一条路。"""
    toolkit = build_toolkit_for(("read",), tmp_path)
    tool = asyncio.run(toolkit.get_tool("Read"))
    assert any(isinstance(item, SensitivePathGuard) for item in tool._middlewares)
