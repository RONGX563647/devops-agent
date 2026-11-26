"""rubric 冻结状态与判分注入。

rubric 的指纹、冻结检查和 prompt 注入与 Grader 构造分开：
前者是「评分标准是否可信」，后者才关心「怎么把标准交给 LLM」。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devops_agent.openjudge.grade import Plan


def parse_rubric_ref(ref: str) -> tuple[str, str]:
    name, _sep, version = ref.partition("@")
    if not name:
        from devops_agent.openjudge.grade import ConfigError

        raise ConfigError(f"rubric 引用缺少名称：{ref}")
    return name, version or "v1"


def rubric_sha(plan: Plan) -> str:
    import hashlib

    from devops_agent.openjudge import rubric

    refs = sorted({spec.rubric for spec in plan.judges.values() if spec.rubric})
    if not refs:
        return ""
    parts = []
    for ref in refs:
        name, version = parse_rubric_ref(ref)
        parts.append(f"{name}@{version}:{rubric.spec_sha(name, version)}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def rubric_gate(plan: Plan) -> dict[str, Any]:
    """判分 rubric 的冻结状态；只做展示，能不能判分由 _build_grader 决定。"""
    from devops_agent.openjudge import rubric

    refs = sorted({spec.rubric for spec in plan.judges.values() if spec.rubric})
    if not refs:
        return {
            "ref": "", "name": "", "version": "", "sha": "",
            "status": "none", "reviewer": "", "ready": True,
        }
    name, version = parse_rubric_ref(refs[0])
    try:
        meta = rubric.load(name, version)["meta"]
    except (OSError, ValueError):
        return {
            "ref": refs[0], "name": name, "version": version, "sha": "",
            "status": "missing", "reviewer": "", "ready": False,
        }
    return {
        "ref": refs[0],
        "name": name,
        "version": version,
        "sha": rubric.spec_sha(name, version),
        "status": str(meta.get("status") or "draft"),
        "reviewer": str(meta.get("reviewer") or ""),
        "ready": str(meta.get("status")) == "frozen" and bool(meta.get("reviewer")),
    }


def frozen_rubric_items(ref: str) -> list[dict[str, Any]]:
    from devops_agent.openjudge import rubric
    from devops_agent.openjudge.grade import ConfigError

    name, version = parse_rubric_ref(ref)
    if not rubric.frozen(name, version):
        raise ConfigError(f"rubric 未冻结，不能参与判分：{ref}")
    return list(rubric.load(name, version)["rubrics"])


def rubric_template(base: Any, items: list[dict[str, Any]]) -> Any:
    from openjudge.models.schema.prompt_template import PromptTemplate

    block = "\n".join(f"- {item.get('text', item)}" for item in items)
    messages: dict[Any, list[Any]] = {}
    for language, original in base.messages.items():
        copied = []
        for message in original:
            clone = message.model_copy(deep=True)
            if clone.role == "user":
                clone.content += f"\n\n<审核过的评分细则>\n{block}\n</审核过的评分细则>"
            copied.append(clone)
        messages[language] = copied
    return PromptTemplate(messages=messages)
