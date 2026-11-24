"""可解性预检 —— 确定性判定「这道题在当前档位/资产下有没有可能通过」。

拆自 `lint.py`：静态 lint 查的是题库与提示词的一致性，预检查的是
「这条用例是否结构性不可赢」，两者在线上的处置不同（前者改题库，后者改档位/资产）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from devops_agent.eval.dataset import EvalCase, Suite
from devops_agent.governance.profiles import PROFILES, PermissionProfile

# 与 lint.py 同一口径：仓库根下的 eval/ 目录。
_EVAL_ROOT = Path(__file__).resolve().parents[3] / "eval"


def run_preflight(cases: list[EvalCase], fixture_root: Path | None) -> list[str]:
    """可解性预检：确定性判定「这道题在当前档位/资产下有没有可能通过」。

    不做完整 silver 重放（那需要脚本执行器），但覆盖了本轮实测最常见的
    三种不可赢结构：引用了不存在的文件、must 工具不在档位工具集、
    fixture 目录缺失。任一命中都说明失败与模型无关，批次应判 `invalid`。
    """
    mixed_risk = any(case.risk is not None for case in cases)
    problems: list[str] = []
    for case in cases:
        problems.extend(_preflight_case(case, fixture_root, mixed_risk))
    return problems


def has_credentials() -> bool:
    """运行真实模型评测的必要条件（key 本身不打印）。"""
    return bool(os.environ.get("DEVOPS_AGENT_API_KEY"))


def preflight_blocked(suite: Suite, fixture_root: Path) -> bool:
    """可解性预检：用例在当前档位/资产下不可能通过时，整批判 invalid。"""
    blocking = run_preflight(suite.cases, fixture_root)
    if not blocking:
        return False
    print("错误: 批次预检未通过，判定 invalid（不计入模型质量）:", file=sys.stderr)
    for item in blocking[:20]:
        print(f"  - {item}", file=sys.stderr)
    return True


def _preflight_case(case: EvalCase, fixture_root: Path | None, mixed_risk: bool) -> list[str]:
    """单条用例的全部可解性问题。拆开是为了让每个函数圈复杂度 ≤10。"""
    where = f"case:{case.id}"
    profile = PROFILES.get(case.permission)
    if profile is None:
        return [f"{where}: 未知档位 {case.permission}"]
    problems = _preflight_tools(case, profile, where)
    if mixed_risk and case.risk is None:
        problems.append(f"{where}: 题库混用 risk 与无 risk 用例")
    problems.extend(_preflight_rubric(case, where))
    problems.extend(_preflight_fixture(case, fixture_root, where))
    return problems


def _preflight_tools(case: EvalCase, profile: PermissionProfile, where: str) -> list[str]:
    """must 工具必须在该档位真正挂载的工具集里。"""
    tools = {name.strip().lower() for name in profile.tools}
    calls = case.expected.tool_calls
    return [
        f"{where}: must 工具 {name} 不在 {case.permission} 档工具集"
        for name in (calls.must if calls else [])
        if name.strip().lower() not in tools
    ]


def _preflight_rubric(case: EvalCase, where: str) -> list[str]:
    """配了裁判的用例，rubric 文件必须存在。"""
    if case.judge is None:
        return []
    rubric = _EVAL_ROOT / "rubrics" / f"{case.judge.rubric}.md"
    if rubric.is_file():
        return []
    return [f"{where}: rubric 不存在 {case.judge.rubric}"]


def _preflight_fixture(case: EvalCase, fixture_root: Path | None, where: str) -> list[str]:
    """fixture 目录必须存在；must_cite 指向的文件必须已存在（或由用例创建）。"""
    if not case.fixture or fixture_root is None:
        return []
    root = fixture_root / case.fixture
    if not root.is_dir():
        return [f"{where}: fixture 目录不存在 {case.fixture}"]
    created = {item.path.replace("\\", "/") for item in case.expected.file_state}
    return [
        f"{where}: must_cite 指向不存在的文件 {path}"
        for path in case.expected.must_cite
        if path not in created and not (root / path).is_file()
    ]
