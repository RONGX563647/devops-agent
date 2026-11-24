"""提示词三类指纹的单测。

锁两个方向的缺陷（同一函数曾同时承担两种职责）：
- 用 `<workdir>` 渲染 → 同一 prompt_sha 下真实字节因平台而异，前缀缓存不能跨机
  复用，但指纹看不出来；
- 档位说明（每次请求都下发）不在指纹里 → 改了它 prompt_sha 不变，
  「同 sha 分数变了」会被误判成噪声。
"""

from __future__ import annotations

import pytest

import devops_agent.runtime.agent as agent_mod
import devops_agent.runtime.profile_note as note_mod
from devops_agent.eval import report as report_mod
from devops_agent.governance.profiles import PROFILES
from devops_agent.runtime.agent import (
    PROMPT_VERSION,
    profile_note_sha,
    prompt_bytes_sha,
    prompt_factors,
    prompt_fingerprint,
)


@pytest.fixture()
def linux_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_mod, "shell_name", lambda: "/bin/sh")


# ------------------------------------------------------------- 语义指纹


def test_semantic_fingerprint_is_platform_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一份代码在 Windows 与 Linux 上必须得到同一个语义指纹。

    做法：把 `shell_name` 换成两个**明确不同**的假值再比对。
    不能拿「真实平台」和「/bin/sh」比 —— 在 Linux 上这两个本来就相同，
    测试会变成恒真（CI 在 ubuntu 上跑就是这样挂的）。
    `_render_system_prompt` 用 `<shell>` 占位符，所以换 shell 不该改变语义指纹。
    """
    monkeypatch.setattr(agent_mod, "shell_name", lambda: "cmd.exe (Windows)")
    one = prompt_fingerprint()
    monkeypatch.setattr(agent_mod, "shell_name", lambda: "/bin/sh")
    other = prompt_fingerprint()
    assert one == other


def test_bytes_fingerprint_differs_across_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    """字节指纹必须反映平台差异——这正是「缓存能否跨机复用」的判据。

    同样用两个明确的假值，避免在 Linux 上「真平台 == /bin/sh」导致恒真。
    """
    monkeypatch.setattr(agent_mod, "shell_name", lambda: "cmd.exe (Windows)")
    one = prompt_bytes_sha("E:/repo")
    monkeypatch.setattr(agent_mod, "shell_name", lambda: "/bin/sh")
    other = prompt_bytes_sha("E:/repo")
    assert one != other


def test_semantic_fingerprint_ignores_real_workdir() -> None:
    """`prompt_fingerprint` 不含 workdir，所以不同机器同值。"""
    assert prompt_fingerprint() == prompt_fingerprint()
    assert "<workdir>" in agent_mod._render_system_prompt("<workdir>", "<shell>")


def test_version_bumped_to_v3() -> None:
    """指纹语义变了，版本必须递增，否则新旧批次会被当成可比。"""
    assert PROMPT_VERSION == "v3"


# ------------------------------------------------------------- 字节指纹


def test_bytes_fingerprint_tracks_workdir() -> None:
    """不同 workdir 的下发字节不同。"""
    assert prompt_bytes_sha("E:/a") != prompt_bytes_sha("E:/b")


def test_bytes_fingerprint_matches_actual_prompt() -> None:
    """字节指纹必须等于 build_system_prompt 的 sha256（它是同一份内容）。"""
    import hashlib

    from devops_agent.runtime.agent import build_system_prompt

    workdir = "E:/code/devops-agent"
    expected = hashlib.sha256(build_system_prompt(workdir).encode("utf-8")).hexdigest()
    assert prompt_bytes_sha(workdir) == expected


# --------------------------------------------------------- 档位说明指纹


def test_profile_note_sha_changes_with_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    """改档位说明的文案，指纹必须变 —— 否则「同 prompt_sha 分数变了」无法归因。"""
    before = profile_note_sha("workspace")
    original = note_mod._WRITE_RULES["workspace"]
    monkeypatch.setitem(note_mod._WRITE_RULES, "workspace", "本会话可以随便改任何文件。")
    after = profile_note_sha("workspace")
    assert before != after
    assert original  # 保底：原值非空


def test_profile_note_sha_differs_across_profiles() -> None:
    """不同档位说明不同，指纹也不同。"""
    hashes = {key: profile_note_sha(key) for key in PROFILES}
    assert len(set(hashes.values())) == len(hashes)


def test_profile_note_sha_unknown_profile_is_stable() -> None:
    """未知档位不崩，返回空说明的指纹。"""
    assert profile_note_sha("does-not-exist") == profile_note_sha("also-missing")


# ------------------------------------------------------------ prompt_factors


def test_prompt_factors_has_three_keys() -> None:
    factors = prompt_factors("E:/code/devops-agent")
    assert set(factors) == {"prompt_sha", "prompt_bytes_sha", "shell_platform"}
    assert factors["prompt_sha"] == prompt_fingerprint()
    assert factors["prompt_bytes_sha"] == prompt_bytes_sha("E:/code/devops-agent")


# -------------------------------------------------------- 可比性归因


def _report(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "n": 40,
        "pass_rate": 0.9,
        "hard_failures": {},
        "gates": [],
        "metrics": [],
        "suite_sha": "s",
        "grader_version": "v2",
        "prompt_sha": "p",
        "model": "m",
        "profile": "per-case",
        "judge_model": "j",
        "judge_scope": "all",
        "prompt_bytes_sha": "bytes-win",
        "shell_platform": "cmd.exe (Windows)",
    }
    base.update(over)
    return base


def test_platform_only_change_does_not_block_and_is_not_attributed_to_prompt() -> None:
    """只有平台/字节差异：不阻断对比，但也不能说成「可归因到提示词」。"""
    compat = report_mod.compatibility(
        _report(prompt_bytes_sha="bytes-lin", shell_platform="/bin/sh"),
        _report(),
    )
    assert compat["level"] == "clean"
    assert set(compat["changed"]) == {"prompt_bytes_sha", "shell_platform"}
    assert "缓存不能跨机复用" in compat["reasons"][0]
    assert "可归因到提示词" not in compat["reasons"][0]


def test_semantic_change_is_attributed_to_prompt() -> None:
    compat = report_mod.compatibility(_report(prompt_sha="p2"), _report())
    assert compat["level"] == "clean"
    assert compat["changed"] == ["prompt_sha"]
    assert "可归因到提示词" in compat["reasons"][0]


def test_informational_factors_are_not_blocking() -> None:
    assert report_mod._INFORMATIONAL_FACTORS == {"prompt_bytes_sha", "shell_platform"}
    assert not report_mod._INFORMATIONAL_FACTORS & report_mod._BLOCKING_FACTORS


def test_platform_change_plus_prompt_change_is_confounded() -> None:
    """平台与语义同时变 -> 混杂，不能归因到单一因子。"""
    compat = report_mod.compatibility(_report(prompt_sha="p2", shell_platform="/bin/sh"), _report())
    assert compat["level"] == "confounded"
