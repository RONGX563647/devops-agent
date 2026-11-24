"""权限模型测试：两维档位、六段管线、判定矩阵与三条不变量。

判定一律走真实 `PermissionEngine`（只造工具对象，不碰模型），因为框架的
求值顺序（deny → ask → allow）是我们必须依赖的前提：手写断言矩阵容易
和框架实现悄悄分叉，那正是「点了允许还在问」这类问题的来源。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agentscope.permission import PermissionEngine
from agentscope.tool import Bash, Write

from devops_agent.governance.policy import build_permission_context, compile_context
from devops_agent.governance.profiles import (
    ALIASES,
    AVAILABLE_ENV,
    DEFAULT_ENV,
    MODES,
    PROFILES,
    UnknownProfile,
    available_names,
    default_profile,
    normalize,
    resolve,
)
from devops_agent.governance.rules import (
    BEHAVIORS,
    BUILTIN_DENY,
    SCOPE_ORDER,
    Rule,
    buckets,
    builtin_rules,
    rule_prefix,
    split_segments,
    strip_wrappers,
)
from devops_agent.tools.catalog import READONLY_TOOL_NAMES, WRITE_TOOL_NAMES, build_toolkit_for

PRESETS = ("readonly", "workspace", "cautious", "unattended", "container")


def _behave(profile_name: str, command: str) -> str:
    """跑一次真实判定：返回 allow / ask / deny。"""
    profile = PROFILES[profile_name]
    engine = PermissionEngine(compile_context(profile))
    bash = Bash(cwd=".")
    return asyncio.run(engine.check_permission(bash, {"command": command})).behavior.value


# --- 两维定义 --------------------------------------------------------------


def test_presets_are_two_dimensional_and_map_to_five_modes() -> None:
    """五个预设正好覆盖框架的五个 mode，没有一格是「选了完全没变化」。"""
    modes = {name: PROFILES[name].mode for name in PRESETS}
    assert modes == {
        "readonly": "explore",
        "workspace": "accept_edits",
        "cautious": "default",
        "unattended": "dont_ask",
        "container": "bypass",
    }
    # 每个组合都必须能在映射表里查到，否则 __post_init__ 会直接拒绝
    for name in PRESETS:
        profile = PROFILES[name]
        assert (profile.scope, profile.approvals) in MODES
        assert profile.key == f"{profile.scope}+{profile.approvals}"


def test_preset_combos_are_unique() -> None:
    """`key` 是重建判据：两个预设共用一个组合会让「切了档却不重建」。"""
    keys = [PROFILES[name].key for name in PRESETS]
    assert len(keys) == len(set(keys))


def test_scope_decides_tools_and_approvals_do_not() -> None:
    """能力范围决定工具集；打扰程度只决定兜底动作，不动工具集。"""
    for name in ("workspace", "cautious", "unattended"):
        assert set(PROFILES[name].tools) - set(READONLY_TOOL_NAMES) == set(WRITE_TOOL_NAMES)
    assert PROFILES["readonly"].tools == READONLY_TOOL_NAMES
    # 同 scope 的三个预设工具集逐字节相同 —— 这就是「调打扰程度不击穿缓存」的依据
    assert PROFILES["workspace"].tools == PROFILES["cautious"].tools == PROFILES["unattended"].tools


def test_unimplementable_combo_is_rejected() -> None:
    """`readonly+always` 在映射表里刻意留白：只读语义与「什么都问」不可兼得。"""
    with pytest.raises(ValueError, match="不可实现的档位组合"):
        type(PROFILES["workspace"])(label="x", scope="readonly", approvals="always", tools=())


# --- 可用集与解析 ----------------------------------------------------------


def test_available_respects_env_and_skips_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置写错名字时跳过而不是让服务起不来。"""
    monkeypatch.setenv(AVAILABLE_ENV, "readonly, bogus ,workspace")
    assert available_names() == ["readonly", "workspace"]


def test_available_defaults_without_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AVAILABLE_ENV, raising=False)
    names = available_names()
    assert "container" not in names
    assert names == ["readonly", "workspace", "cautious", "unattended"]


def test_default_profile_falls_back_when_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认档被配成未开放档时必须回落，而不是把一个越权档当默认。"""
    monkeypatch.setenv(AVAILABLE_ENV, "readonly")
    monkeypatch.setenv(DEFAULT_ENV, "container")
    assert default_profile().preset == "readonly"


def test_resolve_accepts_alias_and_combo_but_rejects_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """别名与组合串都认（老会话、旧 run 记录），未开放仍显式拒绝。"""
    monkeypatch.setenv(AVAILABLE_ENV, "readonly,workspace")
    assert resolve(None).preset == "workspace"
    assert resolve("readonly").preset == "readonly"
    assert resolve("execute").preset == "workspace"  # 弃用别名
    assert resolve("workspace+on-risk").preset == "workspace"  # 组合串
    assert normalize("auto") == "unattended"
    assert normalize("unrestricted") == "container"
    with pytest.raises(UnknownProfile):
        resolve("container")  # 存在但未开放
    with pytest.raises(UnknownProfile):
        resolve("nope")


# --- 判定矩阵（这就是需求说明书）------------------------------------------


@pytest.mark.parametrize(
    ("command", "readonly", "workspace", "cautious", "unattended", "container"),
    [
        ("ls -la", "allow", "allow", "allow", "allow", "allow"),
        ("git status", "allow", "allow", "allow", "allow", "allow"),
        # 白名单命令是 project 规则，不是档位属性；always 档降级为 ask
        ("pytest -q", "deny", "allow", "ask", "allow", "allow"),
        ("npm test", "deny", "allow", "ask", "allow", "allow"),
        # git add / commit 不进白名单：改仓库历史值得逐次确认
        ("git commit -m x", "deny", "ask", "ask", "deny", "allow"),
        # 高危命令命中 ③ 层显式 ask，bypass 下也不会被放行成 allow
        ("rm -rf build", "deny", "ask", "ask", "deny", "ask"),
        ("cd /repo && rm -rf build", "deny", "ask", "ask", "deny", "ask"),
        # ① 层内置 deny：任何档位都拒
        ("cat .env", "deny", "deny", "deny", "deny", "deny"),
        ("cat .git/config", "deny", "deny", "deny", "deny", "deny"),
    ],
)
def test_decision_matrix(
    command: str,
    readonly: str,
    workspace: str,
    cautious: str,
    unattended: str,
    container: str,
) -> None:
    expected = {
        "readonly": readonly,
        "workspace": workspace,
        "cautious": cautious,
        "unattended": unattended,
        "container": container,
    }
    actual = {name: _behave(name, command) for name in expected}
    assert actual == expected, command


def test_write_tool_matrix() -> None:
    """写工具按能力范围下发：只读档直接拒，其余档至少到 ask 之前。"""
    write = Write()
    results = {}
    for name in PRESETS:
        engine = PermissionEngine(compile_context(PROFILES[name]))
        decision = asyncio.run(
            engine.check_permission(write, {"file_path": "a.py", "content": "x"})
        )
        results[name] = decision.behavior.value
    assert results == {
        "readonly": "deny",
        "workspace": "allow",
        "cautious": "ask",  # always：连写文件都确认
        "unattended": "allow",
        "container": "allow",
    }


# --- 三条不变量 ------------------------------------------------------------


def test_invariant_builtin_deny_survives_bypass() -> None:
    """不变量 1：deny 永远先于 allow，且在 bypass / dont_ask 下也不失效。"""
    context = compile_context(PROFILES["container"])
    assert context.mode.value == "bypass"
    denied = {rule.rule_content for rule in context.deny_rules.get("Bash", [])}
    assert {".git/", ".env", ".ssh/", ".aws/", "rm -rf /"} <= denied
    assert _behave("container", "cat .env") == "deny"


def test_invariant_allow_cannot_override_explicit_ask() -> None:
    """不变量 2 的反面：显式 ask（③ 层）不可被 allow 覆盖（框架顺序保证）。"""
    rules = [
        Rule(tool="Bash", pattern="rm:*", behavior="allow", scope="container"),
        *builtin_rules(),
    ]
    context = compile_context(PROFILES["container"], rules)
    engine = PermissionEngine(context)
    decision = asyncio.run(engine.check_permission(Bash(cwd="."), {"command": "rm -rf build"}))
    assert decision.behavior.value == "ask"


def test_invariant_allow_overrides_scope_default() -> None:
    """不变量 2 的正面：allow 能盖过 ⑥ 层档位默认（否则沉淀规则永远不生效）。"""
    rules = [Rule(tool="Bash", pattern="git commit:*", behavior="allow", scope="workspace")]
    context = compile_context(PROFILES["workspace"], rules)
    engine = PermissionEngine(context)
    decision = asyncio.run(engine.check_permission(Bash(cwd="."), {"command": "git commit -m x"}))
    assert decision.behavior.value == "allow"


def test_invariant_always_demotes_allow_to_ask_without_dropping() -> None:
    """不变量 3：always 档把 allow 降级为 ask，但规则本身不丢。"""
    rules = [Rule(tool="Bash", pattern="pytest:*", behavior="allow", scope="workspace")]
    parts = buckets(rules, "workspace", "always")
    assert parts["allow"] == []
    assert [rule.pattern for rule in parts["ask"]] == ["pytest:*"]


def test_rules_respect_scope_floor() -> None:
    """不变量 4：在 workspace 批准的规则在 readonly 不生效、在 container 生效。"""
    rules = [Rule(tool="Bash", pattern="git commit:*", behavior="allow", scope="workspace")]
    readonly = PermissionEngine(compile_context(PROFILES["readonly"], rules))
    container = PermissionEngine(compile_context(PROFILES["container"], rules))
    command = {"command": "git commit -m x"}
    assert asyncio.run(readonly.check_permission(Bash(cwd="."), command)).behavior.value == "deny"
    assert asyncio.run(container.check_permission(Bash(cwd="."), command)).behavior.value == "allow"
    assert SCOPE_ORDER["readonly"] < SCOPE_ORDER["workspace"] < SCOPE_ORDER["container"]


def test_builtin_deny_is_always_present() -> None:
    """缺陷 6 的回归：以前 deny 只挂在 hard_deny 档，等于最需要防护的档最松。"""
    for name in PRESETS:
        context = compile_context(PROFILES[name])
        assert context.deny_rules, name
        assert ".env" in {rule.rule_content for rule in context.deny_rules.get("Bash", [])}
    assert {rule.pattern for rule in BUILTIN_DENY} >= {".env", ".git/"}


# --- shell 解析（prefix 能用的前提）----------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git commit -m 'x && y'", "git commit:*"),
        ("git add .", "git add:*"),
        ("pytest -q tests/unit", "pytest:*"),
        ("python -m pytest -q", "python -m pytest:*"),
        ("timeout 30 npm test", "npm test:*"),
        ("nohup make build", "make:*"),
        # 多段命令一律不沉淀：只批准了前半句，却会顺带放行后半句
        ("cd /repo && rm -rf build", None),
        ("git status; rm -rf dist", None),
        # 破坏性命令不进沉淀池
        ("rm -rf build", None),
        ("sudo apt install x", None),
    ],
)
def test_rule_prefix(command: str, expected: str | None) -> None:
    assert rule_prefix(command) == expected


def test_split_segments_respects_quotes() -> None:
    assert split_segments("echo 'a && b' && ls") == ["echo 'a && b'", "ls"]
    assert split_segments("a | b || c; d") == ["a", "b", "c", "d"]


def test_strip_wrappers_unwraps_nested() -> None:
    assert strip_wrappers(["timeout", "30", "npm", "test"]) == ["npm", "test"]
    assert strip_wrappers(["nice", "-n", "10", "make"]) == ["make"]
    assert strip_wrappers(["env", "A=1", "python", "x.py"]) == ["python", "x.py"]


# --- 其它 ------------------------------------------------------------------


def test_context_without_profile_still_reads_yaml() -> None:
    """不带档位时保持旧行为（脚本 / 单测直接建引擎的场景）。"""
    context = build_permission_context()
    assert context.mode.value == "dont_ask"
    # 内置 deny 仍然并入：它是底线，不随调用方式变化
    assert context.deny_rules


def test_behavior_and_scope_constants_are_exhaustive() -> None:
    assert set(BEHAVIORS) == {"allow", "ask", "deny"}
    assert set(SCOPE_ORDER) == {"readonly", "workspace", "container"}
    assert set(ALIASES.values()) <= set(PROFILES)


def test_build_toolkit_for_rejects_unknown_tool(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知工具"):
        build_toolkit_for(("bash", "teleport"), tmp_path)


def test_build_toolkit_for_passes_middlewares_to_every_tool(tmp_path: Path) -> None:
    """密钥注入挂在每个 ToolBase 上：漏挂一个就有一条绕过注入的路。"""
    middleware = MagicMock()
    names = READONLY_TOOL_NAMES + WRITE_TOOL_NAMES
    toolkit = build_toolkit_for(names, tmp_path, [middleware])
    for name in names:
        tool = asyncio.run(toolkit.get_tool(name.capitalize()))
        assert middleware in tool._middlewares, name
