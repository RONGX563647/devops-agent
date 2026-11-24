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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCENARIO_PREFIX = "scenario:"
SCENARIOS: tuple[str, ...] = ("common", "variant", "edge", "adversarial")

# 判卷口径版本：改了检查项的判定语义就递增，并同步 manifest.grader_version。
# 没有它的时候，「同一 prompt_sha 分数变了」无法区分是提示词变了还是判卷变严了。
GRADER_VERSION = "v2"

# 用例对哪个被测因子敏感 —— 报告按它分组，避免把「配置恒真」混进「提示词因果」。
SUBJECTS: tuple[str, ...] = ("prompt", "tool_desc", "profile", "model", "capability")

# 配比目标（比例区间），允许 ±5pp 绝对容差。
RATIO_TARGETS: dict[str, tuple[float, float]] = {
    "common": (0.60, 0.70),
    "variant": (0.20, 0.30),
    "edge": (0.05, 0.10),
    "adversarial": (0.05, 0.10),
}
RATIO_TOLERANCE = 0.05

# 风险分层：门禁按层判定，不再用一根全局通过率。
# core / boundary / high_risk / regression = 40% / 30% / 20% / 10%。
RISK_LAYERS: tuple[str, ...] = ("core", "boundary", "high_risk", "regression")
RISK_TARGETS: dict[str, tuple[float, float]] = {
    "core": (0.35, 0.45),
    "boundary": (0.25, 0.35),
    "high_risk": (0.15, 0.25),
    "regression": (0.05, 0.15),
}

# 评测范围：端到端结果、轨迹、单组件。
SCOPES: tuple[str, ...] = ("e2e", "trajectory", "component")

# judge 维度白名单：与 judge.py 的 JUDGE_DIMENSIONS 同源，但这里刻意不 import，
# 避免数据集校验反向依赖网络模块。
ALLOWED_JUDGE_DIMENSIONS = frozenset(
    {"correctness", "coherence", "relevance", "objectivity", "clarity", "conciseness"}
)


class _StrictModel(BaseModel):
    """所有数据集模型共享的严格配置：未知字段直接报错。"""

    model_config = ConfigDict(extra="forbid")


class ToolExpectations(_StrictModel):
    """工具轨迹期望。

    `forbid` 只应列出「该档工具集里确实有、但这次不该用」的工具。
    列一个该档根本装配的工具，断言恒真，等于没测（静态检查 S5 会直接点名）。

    为什么没有「入参断言」：AgentScope 的 `ToolCallStartEvent` 只带
    `tool_call_name`，入参不在事件流里。按入参断言就得自己再发一条事件，
    那是另一个改动；在此之前不做「看起来能测入参、实际永远匹配空串」的假字段。
    """

    must: list[str] = Field(default_factory=list)
    forbid: list[str] = Field(default_factory=list)
    max_total: int | None = None
    ordered: bool = False


class FileExpectation(_StrictModel):
    """工作区文件期望：路径相对于用例工作目录。"""

    path: str
    contains: str


class Expected(_StrictModel):
    """一条用例期望的最终状态与断言。

    断言分工：
    - `must_contain`：必须原样出现的事实值（命令名、异常类名、配置键）；
    - `must_contain_any`：同义表述分组，每组命中一个即可 —— 避免「答得对但用词不同」被判失败；
    - `must_cite`：必须给出 `路径:行号` 形式的定位，区分「真读了」和「猜的」；
    - `tool_calls`：轨迹断言，`must_call` / `must_not_call` 看的是入参而不只是工具名。
    """

    status: str = "success"
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    must_contain_any: list[list[str]] = Field(default_factory=list)
    must_not_regex: list[str] = Field(default_factory=list)
    must_cite: list[str] = Field(default_factory=list)
    regex: list[str] = Field(default_factory=list)
    tool_calls: ToolExpectations | None = None
    file_state: list[FileExpectation] = Field(default_factory=list)


class JudgeSpec(_StrictModel):
    """裁判配置：rubric 名称与需要打分的维度。"""

    rubric: str = "peem.v1"
    dimensions: list[str] = Field(default_factory=list)


class OracleSpec(_StrictModel):
    """确定性 oracle：不依赖裁判即可判定的关键事实。

    `critical_facts` 必须出现在最终回答里；`forbidden_facts` 一个都不许出现
    （密钥、canary、注入串）。这两类断言进入 answer / safety 维度，
    与 LLM 裁判完全解耦，也是门禁在裁判未校准时唯一可信的部分。
    """

    critical_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)


class EvalCase(_StrictModel):
    """一条评测用例。"""

    id: str
    tags: list[str]
    input: str
    permission: str = "readonly"
    web_search: bool = False
    fixture: str | None = None
    timeout_s: int = 120
    # 这条用例主要对哪个因子敏感；报告按它分组算通过率。
    subject: str = "capability"
    expected: Expected
    judge: JudgeSpec | None = None
    # 风险分层：决定门禁用哪条阈值，也决定高危用例是否 n=1 也阻断。
    risk: str | None = None
    # 这条用例覆盖哪些评测范围；缺省按 e2e 处理。
    scopes: list[str] = Field(default_factory=list)
    # 确定性 oracle：关键事实与禁止出现的内容。
    oracle: OracleSpec | None = None
    # `tool_calls.must` 里的工具是否必须**真正执行**（被权限拒绝不算命中）。
    #
    # 默认 True：`must: ["Bash"]` 的意图几乎总是「跑起来」，而 readonly 档下
    # `pytest` 会被拒——只看调用名会让「被拒」和「跑成功」同分。
    # 置 False 仅用于「无论是否执行都必须拒绝」的用例（越权、无人值守档）。
    require_executed: bool = True
    # 这条用例「按题面要求动作」时会发出的代表性命令。
    #
    # 它不参与判卷，只供静态检查 S8 用真实权限引擎预演：命令在该档位下是否可达。
    # 没有这个字段时，`permission: readonly` + `must: ["Bash"]` + 期望「跑起来」
    # 这类结构上不可能通过的用例，只能靠跑一次模型才发现。
    command_hint: str | None = None


class ThresholdSpec(_StrictModel):
    """单个门禁阈值：达标线 + 最小样本数。

    `min_samples` 不是可选项：`judge_correctness` 只有 3 条用例配了裁判时，
    直接拿 4.0 判会把「没测够」错报成「质量差」。
    """

    min: float
    min_samples: int = 0


class Manifest(_StrictModel):
    """用例集清单：版本、指纹、计数与门禁阈值。"""

    suite: str
    version: str
    sha256: str
    counts: dict[str, int]
    min_cases_for_gate: int = 40
    # 旧版写的是裸数字（`{"pass_rate": 0.8}`），加载时归一成 ThresholdSpec。
    thresholds: dict[str, ThresholdSpec] = Field(default_factory=dict)
    grader_version: str = ""
    # v3 风险分层：计数、各层通过率下限、各层最小样本。
    risk_counts: dict[str, int] = Field(default_factory=dict)
    risk_thresholds: dict[str, float] = Field(default_factory=dict)
    min_cases_per_risk: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_thresholds(cls, data: Any) -> Any:
        """把 `{"pass_rate": 0.8}` 归一成 `{"pass_rate": {"min": 0.8}}`。"""
        if not isinstance(data, dict):
            return data
        raw = data.get("thresholds")
        if not isinstance(raw, dict):
            return data
        upgraded = {
            key: (
                {"min": value}
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else value
            )
            for key, value in raw.items()
        }
        return {**data, "thresholds": upgraded}


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
    """单条用例的规范化 JSON：字段排序、紧凑分隔符、保留中文。

    v3 新增的 `risk / scopes / oracle` 在取默认值时不参与指纹：
    否则给模型加几个可选字段就会改写全部 v2 用例的 sha，
    历史批次会集体变成「不可比」。
    """
    data = case.model_dump(mode="json")
    if not case.risk:
        data.pop("risk", None)
    if not case.scopes:
        data.pop("scopes", None)
    if case.oracle is None:
        data.pop("oracle", None)
    return json.dumps(
        data,
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
