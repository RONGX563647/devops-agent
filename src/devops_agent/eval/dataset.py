"""评测数据集模型与校验 —— 用例、清单、指纹的唯一事实来源。

数据集是纯数据层：不 import 框架，不跑模型。CLI / runner / API 都从
这里拿到强类型对象，避免把 JSONL 的原始 dict 散落到各处。

校验策略：
- 结构错误由 Pydantic 在加载时抛出（`extra="forbid"`，拼错的字段不会被静默忽略）；
- 语义错误（id 重复、配比、指纹、计数、未知 judge 维度）由 `validate_suite`
  收集成字符串列表，方便 CLI 一次性全部打印。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCENARIO_PREFIX = "scenario:"
SCENARIOS: tuple[str, ...] = ("common", "variant", "edge", "adversarial")

# 配比目标（比例区间），允许 ±5pp 绝对容差。
RATIO_TARGETS: dict[str, tuple[float, float]] = {
    "common": (0.60, 0.70),
    "variant": (0.20, 0.30),
    "edge": (0.05, 0.10),
    "adversarial": (0.05, 0.10),
}
RATIO_TOLERANCE = 0.05

# judge 维度白名单：与 judge.py 的 JUDGE_DIMENSIONS 同源，但这里刻意不 import，
# 避免数据集校验反向依赖网络模块。
ALLOWED_JUDGE_DIMENSIONS = frozenset(
    {"correctness", "coherence", "relevance", "objectivity", "clarity", "conciseness"}
)


class _StrictModel(BaseModel):
    """所有数据集模型共享的严格配置：未知字段直接报错。"""

    model_config = ConfigDict(extra="forbid")


class ToolExpectations(_StrictModel):
    """工具轨迹期望。"""

    must: list[str] = Field(default_factory=list)
    forbid: list[str] = Field(default_factory=list)
    max_total: int | None = None
    ordered: bool = False


class FileExpectation(_StrictModel):
    """工作区文件期望：路径相对于用例工作目录。"""

    path: str
    contains: str


class Expected(_StrictModel):
    """一条用例期望的最终状态与断言。"""

    status: str = "success"
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    regex: list[str] = Field(default_factory=list)
    tool_calls: ToolExpectations | None = None
    file_state: list[FileExpectation] = Field(default_factory=list)


class JudgeSpec(_StrictModel):
    """裁判配置：rubric 名称与需要打分的维度。"""

    rubric: str = "peem.v1"
    dimensions: list[str] = Field(default_factory=list)


class EvalCase(_StrictModel):
    """一条评测用例。"""

    id: str
    tags: list[str]
    input: str
    permission: str = "readonly"
    web_search: bool = False
    fixture: str | None = None
    timeout_s: int = 120
    expected: Expected
    judge: JudgeSpec | None = None


class Manifest(_StrictModel):
    """用例集清单：版本、指纹、计数与门禁阈值。"""

    suite: str
    version: str
    sha256: str
    counts: dict[str, int]
    min_cases_for_gate: int = 40
    thresholds: dict[str, float] = Field(default_factory=dict)


class Suite(_StrictModel):
    """清单 + 用例的完整数据集。"""

    manifest: Manifest
    cases: list[EvalCase]


def manifest_path_for(path: Path) -> Path:
    """按约定推导清单路径：`x.v1.jsonl` → `x.v1.manifest.json`。"""
    return path.with_name(path.stem + ".manifest.json")


def load_suite(path: str | Path) -> Suite:
    """读取同名清单与 JSONL 用例。

    JSONL 每行一个用例，空行跳过；结构错误带上行号重新抛出，
    否则一个缩进错误会表现成「数据集为空」。
    """
    suite_path = Path(path)
    manifest_path = manifest_path_for(suite_path)
    manifest = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    cases: list[EvalCase] = []
    for lineno, line in enumerate(suite_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            cases.append(EvalCase.model_validate_json(stripped))
        except ValidationError as exc:
            raise ValueError(f"{suite_path}:{lineno}: {exc}") from exc
    return Suite(manifest=manifest, cases=cases)


def _canonical(case: EvalCase) -> str:
    """单条用例的规范化 JSON：字段排序、紧凑分隔符、保留中文。"""
    return json.dumps(
        case.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def suite_sha256(cases: list[EvalCase]) -> str:
    """按 id 排序后拼接规范化 JSON，再取 sha256（与文件顺序无关）。"""
    ordered = sorted(cases, key=lambda case: case.id)
    payload = "\n".join(_canonical(case) for case in ordered)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scenario_of(case: EvalCase) -> str | None:
    """取用例的 `scenario:*` 标签值；没有则返回 None。"""
    for tag in case.tags:
        if tag.startswith(SCENARIO_PREFIX):
            return tag[len(SCENARIO_PREFIX) :]
    return None


def _has_assertion(expected: Expected) -> bool:
    """是否至少有一条可判定的断言（非默认状态也算一条）。"""
    return bool(
        expected.status != "success"
        or expected.must_contain
        or expected.must_not_contain
        or expected.regex
        or expected.tool_calls is not None
        or expected.file_state
    )


def _case_tag_errors(case: EvalCase) -> list[str]:
    """标签必须有值、非空且带 scenario:*。"""
    if not case.tags:
        return [f"{case.id}: tags 不能为空"]
    if any(not tag.strip() for tag in case.tags):
        return [f"{case.id}: tags 不能包含空字符串"]
    if scenario_of(case) is None:
        return [f"{case.id}: 缺少 scenario:* 标签"]
    return []


def _case_errors(case: EvalCase) -> list[str]:
    """单个用例的标签、断言与 judge 维度校验。"""
    errors = _case_tag_errors(case)
    if not _has_assertion(case.expected):
        errors.append(f"{case.id}: expected 至少需要一条断言")
    if case.judge is not None:
        unknown = sorted(set(case.judge.dimensions) - ALLOWED_JUDGE_DIMENSIONS)
        if unknown:
            errors.append(f"{case.id}: 未知 judge 维度: {', '.join(unknown)}")
    return errors


def _counts_errors(suite: Suite, actual_counts: Counter[str]) -> list[str]:
    """manifest 声明的场景计数必须与实际一致。"""
    if dict(actual_counts) == dict(suite.manifest.counts):
        return []
    return [
        f"manifest.counts 与实际不一致: 期望 {suite.manifest.counts}, 实际 {dict(actual_counts)}"
    ]


def _ratio_errors(n: int, actual_counts: Counter[str]) -> list[str]:
    """场景配比必须落在目标区间（含容差）内。"""
    errors: list[str] = []
    for scenario, (low, high) in RATIO_TARGETS.items():
        share = actual_counts.get(scenario, 0) / n
        if not (low - RATIO_TOLERANCE <= share <= high + RATIO_TOLERANCE):
            errors.append(
                f"scenario:{scenario} 占比 {share:.1%} 超出 "
                f"[{low:.0%}, {high:.0%}] ±{RATIO_TOLERANCE:.0%}"
            )
    return errors


def validate_suite(suite: Suite) -> list[str]:
    """返回语义校验错误；`warning:` 开头的条目只提示、不阻断。

    少于 `min_cases_for_gate` 时只给 warning，且跳过配比硬校验 ——
    小样本下百分比抖动没有意义，强行要求配比只会逼人凑数。
    """
    errors: list[str] = []
    seen: set[str] = set()
    for case in suite.cases:
        if case.id in seen:
            errors.append(f"duplicate case id: {case.id}")
        seen.add(case.id)
        errors.extend(_case_errors(case))

    actual_sha = suite_sha256(suite.cases)
    if suite.manifest.sha256 != actual_sha:
        errors.append(f"manifest.sha256 不匹配: 期望 {suite.manifest.sha256}, 实际 {actual_sha}")

    actual_counts = Counter(
        scenario for case in suite.cases if (scenario := scenario_of(case)) is not None
    )
    errors.extend(_counts_errors(suite, actual_counts))

    n = len(suite.cases)
    if n < suite.manifest.min_cases_for_gate:
        errors.append(
            f"warning: 用例数 {n} < min_cases_for_gate "
            f"{suite.manifest.min_cases_for_gate}，不参与发布门禁"
        )
    else:
        errors.extend(_ratio_errors(n, actual_counts))
    return errors


def split_warnings(messages: list[str]) -> tuple[list[str], list[str]]:
    """把校验输出拆成 (errors, warnings)，CLI 据此决定退出码。"""
    errors = [item for item in messages if not item.startswith("warning:")]
    warnings = [item for item in messages if item.startswith("warning:")]
    return errors, warnings
