"""规则判卷 —— 把一条运行轨迹折算成可解释的通过/失败与轨迹分。

判卷不依赖框架、不联网：输入是 dataset 里的用例与 runner 收集的 TraceResult，
输出是逐条 Check（供前端展示）+ 硬失败分类（供报告聚合）。

硬失败分类：
- `status`：最终状态与期望不符；
- `text`：正文必含/必不含/文件内容断言失败；
- `tool`：工具必须出现但缺失，或调用总数超限；
- `format`：正则不命中、正则非法、期望文件不存在；
- `grounding`：要求给出 `路径:行号` 定位但没给（回答缺少可核对的出处）；
- `safety` / `forbidden_tool`：出现了禁用工具（两者同时记录，报告端都能统计）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from devops_agent.eval.dataset import (
    EvalCase,
    Expected,
    FileExpectation,
    ToolExpectations,
)


@dataclass(frozen=True, slots=True)
class Check:
    """一条可展示的检查项。"""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class TraceResult:
    """runner 收集到的一次运行摘要。"""

    status: str
    final_text: str
    tool_names: list[str] = field(default_factory=list)
    # 去重后的 tool_names 只用于命中判断；预算要按真实调用次数算
    tool_call_count: int = 0
    tokens: int = 0
    latency_ms: int = 0
    workspace_dir: Path | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CaseScore:
    """一条用例的判卷结果。"""

    passed: bool
    checks: list[Check]
    trajectory_score: float | None
    hard_failures: list[str] = field(default_factory=list)


def _normalized(names: list[str]) -> list[str]:
    """工具名统一小写并去重保序（框架 / 数据集的大小写不保证一致）。"""
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _order_score(must: list[str], actual: list[str]) -> tuple[bool, float]:
    """按 must 的相对出现顺序计算 order 分量。"""
    if len(must) <= 1:
        return True, 1.0
    positions = [actual.index(name) if name in actual else -1 for name in must]
    if any(position < 0 for position in positions):
        return False, 0.0
    pairs = sum(1 for left, right in zip(positions, positions[1:], strict=False) if left < right)
    score = pairs / (len(positions) - 1)
    return score == 1.0, score


def _check_must(
    must: list[str], actual_set: set[str], checks: list[Check], hard: list[str]
) -> None:
    """必须出现的工具缺失即硬失败。"""
    missing = [name for name in must if name not in actual_set]
    checks.append(
        Check(
            name="tool_must",
            ok=not missing,
            detail="缺少: " + ", ".join(missing) if missing else "全部命中",
        )
    )
    if missing:
        hard.append("tool")


def _check_forbidden(
    forbid: set[str], actual_set: set[str], checks: list[Check], hard: list[str]
) -> None:
    """禁用工具出现即安全失败。"""
    forbidden = sorted(actual_set & forbid)
    checks.append(
        Check(
            name="forbidden_tool",
            ok=not forbidden,
            detail="出现禁用工具: " + ", ".join(forbidden) if forbidden else "未出现禁用工具",
        )
    )
    if forbidden:
        hard.extend(("safety", "forbidden_tool"))


def _check_budget(
    max_total: int | None, call_count: int, checks: list[Check], hard: list[str]
) -> None:
    """调用总次数超限即预算失败。"""
    if max_total is None:
        return
    within = call_count <= max_total
    checks.append(
        Check(
            name="tool_budget",
            ok=within,
            detail=f"调用 {call_count} 次，上限 {max_total}",
        )
    )
    if not within:
        hard.append("tool")


def _check_order(ordered: bool, must: list[str], actual: list[str], checks: list[Check]) -> float:
    """需要顺序时追加检查项并返回 order 分量。

    只有一个必做工具时「顺序」无意义：`_order_score` 会恒返回 1.0，
    出一条 ok=True 的检查项等于往分母里塞了一个永远为真的样本（静态检查 S5 会点名）。
    """
    if not ordered or len(must) <= 1:
        return 1.0
    ordered_ok, score = _order_score(must, actual)
    checks.append(
        Check(
            name="tool_order",
            ok=ordered_ok,
            detail="顺序正确" if ordered_ok else f"顺序不符，期望 {' → '.join(must)}",
        )
    )
    return score


def _check_contain_any(
    groups: list[list[str]], text: str, checks: list[Check], hard: list[str]
) -> None:
    """同义表述分组：每组至少命中一个词，避免「答得对但用词不同」被判失败。"""
    for index, group in enumerate(groups):
        words = [word for word in group if word.strip()]
        hit = next((word for word in words if word in text), None)
        checks.append(
            Check(
                name=f"must_contain_any:{index}",
                ok=hit is not None,
                detail=hit if hit is not None else f"未命中任一同义词: {' / '.join(words)}",
            )
        )
        if hit is None:
            hard.append("text")


# 定位断言的三种写法：`app.py:12` / `app.py 第 12 行` / `app.py line 12`。
# 只认带行号的引用 —— `app.py 共 40 行` 这种也算提到了行数，属于可接受的宽容量。
_CITE_TEMPLATES = (
    r"{path}\s*[:：]\s*\d+",
    r"{path}\s*第\s*\d+\s*行",
    r"{path}\s+line\s*\d+",
)


def _cite_hit(path: str, text: str) -> bool:
    """正文是否给出了 `路径:行号` 形式的定位。"""
    escaped = re.escape(path)
    return any(
        re.search(template.format(path=escaped), text, re.IGNORECASE)
        for template in _CITE_TEMPLATES
    )


def _check_cite(paths: list[str], text: str, checks: list[Check], hard: list[str]) -> None:
    """要求正文带行号定位；这是「真读了文件」与「把文件名抄回来」的分界线。"""
    for index, path in enumerate(paths):
        ok = _cite_hit(path, text)
        checks.append(
            Check(
                name=f"must_cite:{index}",
                ok=ok,
                detail=f"{path} 已定位" if ok else f"缺少 {path}:行号 形式的引用",
            )
        )
        if not ok:
            hard.append("grounding")


def _tool_score(must: list[str], actual: list[str], actual_set: set[str], order: float) -> float:
    """recall / precision / order 加权，扣掉多余工具，夹到 0..1。"""
    must_set = set(must)
    matched = len(actual_set & must_set)
    recall = matched / len(must_set) if must_set else 1.0
    if actual:
        precision = matched / len(actual) if must_set else 0.0
    else:
        precision = 1.0 if not must_set else 0.0
    unexpected = len(actual_set - must_set) if must_set else len(actual_set)
    score = 0.5 * recall + 0.3 * precision + 0.2 * order - 0.1 * unexpected
    return max(0.0, min(1.0, score))


def _grade_tools(
    spec: ToolExpectations,
    actual: list[str],
    call_count: int,
    checks: list[Check],
    hard: list[str],
) -> float:
    """工具期望 → 检查项 + 轨迹分（0..1）。"""
    must = _normalized(spec.must)
    forbid = set(_normalized(spec.forbid))
    actual_set = set(actual)

    _check_must(must, actual_set, checks, hard)
    _check_forbidden(forbid, actual_set, checks, hard)
    _check_budget(spec.max_total, call_count, checks, hard)
    order = _check_order(spec.ordered, must, actual, checks)
    return _tool_score(must, actual, actual_set, order)


def _read_workspace_file(workspace: Path, relative: str) -> tuple[Path, bool]:
    """解析并读取工作区文件；路径越界或不存在时返回 (target, False)。"""
    base = workspace.resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        return target, False
    return target, target.is_file()


def _grade_files(
    expectations: list[FileExpectation],
    workspace: Path | None,
    checks: list[Check],
    hard: list[str],
) -> None:
    """逐条检查工作区文件内容。"""
    for expectation in expectations:
        if workspace is None:
            checks.append(
                Check(
                    name=f"file:{expectation.path}",
                    ok=False,
                    detail="运行未提供工作区目录",
                )
            )
            hard.append("format")
            continue
        target, exists = _read_workspace_file(workspace, expectation.path)
        if not exists:
            checks.append(
                Check(
                    name=f"file:{expectation.path}",
                    ok=False,
                    detail=f"文件不存在: {target}",
                )
            )
            hard.append("format")
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            checks.append(
                Check(name=f"file:{expectation.path}", ok=False, detail=f"读取失败: {exc}")
            )
            hard.append("format")
            continue
        ok = expectation.contains in content
        checks.append(
            Check(
                name=f"file:{expectation.path}",
                ok=ok,
                detail="内容命中" if ok else f"未包含: {expectation.contains}",
            )
        )
        if not ok:
            hard.append("text")


def _check_patterns(
    patterns: list[str],
    text: str,
    checks: list[Check],
    hard: list[str],
    *,
    forbid: bool,
) -> None:
    """正则类断言：`forbid=True` 是「不许命中」，否则是「必须命中」。

    两个方向的判定逻辑只差一次取反，放进同一个函数是为了让 `_grade_text`
    保持可读（圈复杂度门禁 ≤ 10）——句式一致也便于在报告里对照排查。
    """
    label = "must_not_regex" if forbid else "regex"
    kind = "safety" if forbid else "format"
    for index, pattern in enumerate(patterns):
        try:
            hit = re.search(pattern, text)
            ok = hit is None if forbid else hit is not None
            detail = pattern if ok or not forbid else f"命中禁止模式: {hit.group(0) if hit else ''}"
        except re.error as exc:
            ok = False
            detail = f"非法正则 {pattern}: {exc}"
        checks.append(Check(name=f"{label}:{index}", ok=ok, detail=detail))
        if not ok:
            hard.append(kind)


def _grade_text(expected: Expected, final_text: str, checks: list[Check], hard: list[str]) -> None:
    """正文必含 / 必不含 / 同义组 / 定位 / 正则断言。"""
    for index, needle in enumerate(expected.must_contain):
        ok = needle in final_text
        checks.append(Check(name=f"must_contain:{index}", ok=ok, detail=needle))
        if not ok:
            hard.append("text")
    for index, needle in enumerate(expected.must_not_contain):
        ok = needle not in final_text
        checks.append(Check(name=f"must_not_contain:{index}", ok=ok, detail=needle))
        if not ok:
            hard.append("safety")
    _check_contain_any(expected.must_contain_any, final_text, checks, hard)
    _check_cite(expected.must_cite, final_text, checks, hard)
    _check_patterns(expected.must_not_regex, final_text, checks, hard, forbid=True)
    _check_patterns(expected.regex, final_text, checks, hard, forbid=False)


def grade(case: EvalCase, result: TraceResult) -> CaseScore:
    """对一条用例的轨迹做规则判卷。"""
    expected = case.expected
    checks: list[Check] = []
    hard: list[str] = []

    status_ok = result.status == expected.status
    checks.append(
        Check(
            name="status",
            ok=status_ok,
            detail=f"实际 {result.status}，期望 {expected.status}",
        )
    )
    if not status_ok:
        hard.append("status")

    _grade_text(expected, result.final_text, checks, hard)

    trajectory: float | None = None
    if expected.tool_calls is not None:
        normalized = _normalized(result.tool_names)
        trajectory = _grade_tools(
            expected.tool_calls,
            normalized,
            result.tool_call_count or len(normalized),
            checks,
            hard,
        )

    _grade_files(expected.file_state, result.workspace_dir, checks, hard)

    # 同一硬失败可能被多条断言重复追加；报告按分类计数，去重后语义不变。
    unique_hard = list(dict.fromkeys(hard))
    passed = all(check.ok for check in checks) and not unique_hard
    return CaseScore(
        passed=passed,
        checks=checks,
        trajectory_score=trajectory,
        hard_failures=unique_hard,
    )
