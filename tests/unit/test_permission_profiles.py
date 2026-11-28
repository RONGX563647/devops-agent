"""权限层测试：档位定义、可用集、档位→权限上下文 / 工具集的映射。

契约强度约定：判定一律交给真实引擎跑（`PermissionEngine.check_permission`），
不比对常量表 —— 只有实判才能证明「只读档真的写不进去」，而不是「我们以为它写不进去」。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agentscope.permission import (
    PermissionBehavior,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)

from devops_agent.governance.compile import WORKDIRS_ENV, build_engine, compile_context
from devops_agent.governance.decide import bash_decision
from devops_agent.governance.model import ALLOW_PREFIXES, PROFILES
from devops_agent.governance.presets import (
    AVAILABLE_ENV,
    DEFAULT_ENV,
    UnknownProfile,
    available,
    default_profile,
    resolve,
)
from devops_agent.tools.catalog import (
    READONLY_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    build_toolkit_for,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """档位相关环境变量必须逐测隔离：presets 的结果按 env 原文缓存。"""
    for name in (AVAILABLE_ENV, DEFAULT_ENV, WORKDIRS_ENV):
        monkeypatch.delenv(name, raising=False)


def test_profiles_bind_mode_tools_and_fallback() -> None:
    """档位是「mode + 工具集 + 兜底动作」的三元组，缺一项就不成档位。"""
    assert PROFILES["readonly"].mode == "explore"
    assert PROFILES["readonly"].fallback == "deny"
    assert PROFILES["workspace"].mode == "accept_edits"
    assert PROFILES["workspace"].fallback == "ask"
    assert PROFILES["auto"].mode == "dont_ask"
    assert PROFILES["unrestricted"].fallback == "hard_deny"


def test_every_profile_mode_is_a_framework_mode() -> None:
    """mode 是本模块里唯一的字符串，写错必须在这一层就被框架枚举挡住。"""
    for key, profile in PROFILES.items():
        assert PermissionMode(profile.mode) in set(PermissionMode), key


def test_scope_declares_capability_boundary() -> None:
    """scope 与 mode 是两个维度：只读档的限制不能只靠 mode 表达。"""
    assert PROFILES["readonly"].scope == "readonly"
    assert PROFILES["workspace"].scope == "workspace"
    assert PROFILES["unrestricted"].scope == "container"


def test_writable_profiles_mount_write_tools() -> None:
    """说明里承诺「可在工作区内建/删/改文件」的档位，必须真的挂上写工具。

    只挂只读四件套时，workspace / execute 与只读档的唯一差别只剩「越界问不问」，
    用户看到的就是「切了档什么都没变」——这个断言就是防它再退回去。
    """
    for key in ("workspace", "execute", "auto", "unrestricted"):
        assert set(PROFILES[key].tools) - set(READONLY_TOOL_NAMES) == set(WRITE_TOOL_NAMES), key
        assert PROFILES[key].can_write is True, key
    # 只读档不给写工具：这是工具集层面的硬保证，不依赖 mode 是否被改错
    assert "write" not in PROFILES["readonly"].tools
    assert "edit" not in PROFILES["readonly"].tools
    assert PROFILES["readonly"].can_write is False


def test_only_readonly_profile_denies_writes(tmp_path: Path) -> None:
    """判定交给真实引擎：只读档写不进去，其余档在工作区内可写、越界按兜底处理。"""
    from agentscope.tool import LocalBackend, Write

    inside = str(tmp_path / "sub" / "out.txt")
    outside = str(tmp_path.parent / "escape.txt")

    def decide(profile_key: str, target: str) -> str:
        engine = PermissionEngine(compile_context(PROFILES[profile_key], workdir=tmp_path))
        # agentscope 2.x 的 LocalBackend 不接受 workdir；判定只看 permission
        # context 里登记的 working_directories，与 backend 的 cwd 无关。
        tool = Write(backend=LocalBackend())
        decision = asyncio.run(engine.check_permission(tool, {"file_path": target, "content": "x"}))
        return decision.behavior.value

    assert decide("readonly", inside) == "deny"
    for key in ("workspace", "execute", "auto", "unrestricted"):
        assert decide(key, inside) == "allow", key
    # 越界：workspace / execute 问一句，auto 直接拒，unrestricted 放行
    assert decide("workspace", outside) == "ask"
    assert decide("execute", outside) == "ask"
    assert decide("auto", outside) == "deny"
    assert decide("unrestricted", outside) == "allow"


def test_workdir_is_registered_so_writes_count_as_in_workspace(tmp_path: Path) -> None:
    """workdir 必须登记进 working_directories。

    框架判「在不在工作区内」用的是 `os.getcwd() + working_directories`，而部署时
    真正的工作区常由 DEVOPS_AGENT_WORKDIR 指到别处。不登记的话，accept_edits 档
    写文件会被当成「工作区外」，每次都要确认——用户看到的就是「选了可写还要问」。
    """
    context = compile_context(PROFILES["workspace"], workdir=tmp_path)
    assert str(tmp_path.resolve()) in context.working_directories


def test_extra_workdirs_come_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """附加工作目录原来是 policy.yaml 的空列表（死配置），现在只认环境变量。"""
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setenv(WORKDIRS_ENV, os.pathsep.join([str(extra), str(tmp_path / "gone")]))
    context = compile_context(PROFILES["workspace"], workdir=tmp_path)
    assert str(extra.resolve()) in context.working_directories
    assert str((tmp_path / "gone").resolve()) in context.working_directories


def test_available_respects_env_and_skips_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置写错名字时跳过而不是让服务起不来。"""
    monkeypatch.setenv(AVAILABLE_ENV, "readonly, bogus ,workspace")
    assert [item.key for item in available()] == ["readonly", "workspace"]


def test_available_defaults_include_unrestricted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AVAILABLE_ENV, raising=False)
    keys = [item.key for item in available()]
    assert "unrestricted" in keys
    assert "workspace" in keys


def test_available_env_can_still_narrow_unrestricted(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认开放全放开档，但部署方可显式收窄，不改代码即可回到原来的安全边界。"""
    monkeypatch.setenv(AVAILABLE_ENV, "readonly,workspace,execute,auto")
    assert "unrestricted" not in [item.key for item in available()]


def test_default_profile_falls_back_to_strictest_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认档不可用时回落**最严可用档**，不是字典序首个。

    旧实现回落 `sorted(allowed)[0]`：可用集是 {auto, readonly, workspace} 时字典序
    首个是 `auto`（无人值守）—— 配置写错反而把用户推向最不想有人盯的档位。
    """
    monkeypatch.setenv(AVAILABLE_ENV, "auto,unrestricted,workspace")
    monkeypatch.setenv(DEFAULT_ENV, "nope")
    assert default_profile().key == "workspace"

    monkeypatch.setenv(AVAILABLE_ENV, "auto,readonly")
    assert default_profile().key == "readonly"


def test_default_profile_honors_env_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AVAILABLE_ENV, "readonly,workspace,auto")
    monkeypatch.setenv(DEFAULT_ENV, "auto")
    assert default_profile().key == "auto"


def test_resolve_rejects_unknown_and_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AVAILABLE_ENV, "readonly,workspace")
    assert resolve(None).key == "workspace"
    assert resolve("readonly").key == "readonly"
    with pytest.raises(UnknownProfile):
        resolve("unrestricted")  # 存在但未开放
    with pytest.raises(UnknownProfile):
        resolve("nope")


def test_resolve_is_stable_within_one_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一请求内反复解析必须同源：旧实现每次重读 env，两次可能不一致。"""
    monkeypatch.setenv(AVAILABLE_ENV, "readonly,workspace")
    assert resolve(None) is resolve(None)
    assert available() == available()


def test_builtin_deny_injected_in_every_mode() -> None:
    """builtin deny（.git/、.env）对所有档位无条件生效。

    旧实现只给 hard_deny 档装配，导致 auto(dont_ask) 对工作区内的 .env 直接
    allow —— 无人值守档反而能读密钥文件。框架 deny 优先于 allow/ask，
    所以五档都必须带上这层硬约束。
    """
    for key in ("readonly", "workspace", "execute", "unrestricted", "auto"):
        context = compile_context(PROFILES[key])
        assert context.deny_rules, f"{key} 档缺少 builtin deny"
        bash_deny = [str(rule.rule_content) for rule in context.deny_rules.get("Bash", [])]
        assert any(".env" in pattern for pattern in bash_deny), f"{key} 档的 Bash 未禁 .env"
        assert any(".git" in pattern for pattern in bash_deny), f"{key} 档的 Bash 未禁 .git"
        # 引擎实判：auto(dont_ask) 在旧实现下对工作区内的 .env 直接 allow，
        # 这是最需要锁死的一档。
        assert bash_decision(PROFILES[key], "cat .env", Path.cwd()) == "deny"


def test_context_for_unrestricted_carries_profile_deny_rules() -> None:
    context = compile_context(PROFILES["unrestricted"])
    assert context.mode == PermissionMode.BYPASS
    contents = [str(rule.rule_content) for rule in context.deny_rules.get("Bash", [])]
    assert any(".git" in pattern for pattern in contents)


def test_context_for_execute_injects_command_allowlist() -> None:
    context = compile_context(PROFILES["execute"])
    assert context.mode == PermissionMode.ACCEPT_EDITS
    prefixes = [rule.rule_content for rule in context.allow_rules.get("Bash", [])]
    assert any(str(prefix).endswith(":*") for prefix in prefixes)
    assert "pytest:*" in prefixes


def test_whitelist_belongs_to_execute_only() -> None:
    """白名单是 execute 档的能力，不是部署配置：别的档位一条都不该有。"""
    for key in PROFILES:
        context = compile_context(PROFILES[key])
        if key == "execute":
            assert context.allow_rules.get("Bash")
        else:
            assert not context.allow_rules, key
    assert set(ALLOW_PREFIXES) == {"execute"}


def test_custom_deny_appends_to_builtin() -> None:
    """传自定义 deny 是追加而非替换：内置 .git/、.env 必须还在。

    旧实现 `deny_rules if deny_rules is not None else ...` 会让调用方一传
    就整体顶掉 builtin deny——而那是所有档位唯一的点防御兜底。
    """
    whole_tool = PermissionRule(
        tool_name="Bash",
        rule_content=None,
        behavior=PermissionBehavior.DENY,
        source="caller",
    )
    context = compile_context(PROFILES["auto"], extra_deny={"Bash": [whole_tool]})
    raw = [rule.rule_content for rule in context.deny_rules["Bash"]]
    assert any(pattern and ".env" in pattern for pattern in raw)
    assert any(pattern and ".git" in pattern for pattern in raw)
    assert None in raw  # caller 的整工具拒绝也保留


def test_custom_deny_overrides_same_rule_only() -> None:
    """同 (tool, content, behavior) 的规则以 caller 为准，其余内置规则不受牵连。"""
    override = PermissionRule(
        tool_name="Write",
        rule_content=".env",
        behavior=PermissionBehavior.DENY,
        source="caller",
    )
    context = compile_context(PROFILES["workspace"], extra_deny={"Write": [override]})
    env_rules = [rule for rule in context.deny_rules["Write"] if rule.rule_content == ".env"]
    assert len(env_rules) == 1
    assert env_rules[0].source == "caller"
    assert any(rule.rule_content == ".git" for rule in context.deny_rules["Write"])
    # 未被 Bash 覆盖的其他 builtin（Write/Edit 的 .git/）同样保留
    assert context.deny_rules["Write"]


def test_build_engine_returns_engine(tmp_path: Path) -> None:
    assert isinstance(build_engine(PROFILES["workspace"], workdir=tmp_path), PermissionEngine)


def test_bash_decision_reuses_engine(tmp_path: Path) -> None:
    """lint 批量跑题时不能每题重建引擎：同一 (档位, workdir) 必须命中缓存。"""
    from devops_agent.governance import decide

    decide._engine_for.cache_clear()
    first = bash_decision(PROFILES["workspace"], "ls", tmp_path)
    second = bash_decision(PROFILES["workspace"], "ls", tmp_path)
    assert first == second == "allow"
    assert decide._engine_for.cache_info().hits >= 1


def test_build_toolkit_for_mounts_profile_tools(tmp_path: Path) -> None:
    def registered(names: tuple[str, ...]) -> set[str]:
        toolkit = build_toolkit_for(names, tmp_path)
        return {asyncio.run(toolkit.get_tool(name.capitalize())).name for name in names}

    assert registered(PROFILES["readonly"].tools) == {"Bash", "Read", "Glob", "Grep"}
    assert {"Write", "Edit"} <= registered(PROFILES["workspace"].tools)


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
