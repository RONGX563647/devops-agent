"""零样本 Rubric：用现有网关生成 / 校验评分标准工件。

工件目录 `eval/openjudge/rubrics/<name>/<version>/`：
- `spec.yaml`：生成参数（任务描述、场景、默认条目），可重建；
- `rubrics.json`：生成的条目与分值；
- `meta.json`：sha、生成时间、语言、模型、状态（draft / frozen）。

治理：生成只能得到 draft；冻结需要人工签字（CLI 不提供 `--freeze` 之外
的自动通道），未冻结的 rubric 不参与门禁 —— 否则模型可以给自己写标准。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

RUBRIC_DIR = Path(__file__).resolve().parents[3] / "eval" / "openjudge" / "rubrics"


def rubric_dir(name: str, version: str = "v1") -> Path:
    return RUBRIC_DIR / name / version


def spec_sha(name: str, version: str = "v1") -> str:
    """工件指纹：spec + rubrics 的规范化 JSON sha256。"""
    folder = rubric_dir(name, version)
    digest = hashlib.sha256()
    for filename in ("spec.yaml", "rubrics.json"):
        path = folder / filename
        digest.update(path.read_bytes() if path.exists() else b"")
    return digest.hexdigest()[:16]


def load(name: str, version: str = "v1") -> dict[str, Any]:
    folder = rubric_dir(name, version)
    spec = yaml.safe_load((folder / "spec.yaml").read_text(encoding="utf-8")) or {}
    rubric = json.loads((folder / "rubrics.json").read_text(encoding="utf-8"))
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    return {"spec": spec, "rubrics": rubric, "meta": meta, "sha": spec_sha(name, version)}


def frozen(name: str, version: str = "v1") -> bool:
    try:
        return str(load(name, version)["meta"].get("status")) == "frozen"
    except (OSError, ValueError):
        return False


def write_artifact(
    name: str,
    version: str,
    *,
    spec: dict[str, Any],
    rubrics: list[dict[str, Any]],
    model: str,
    status: str = "draft",
    reviewer: str = "",
) -> Path:
    folder = rubric_dir(name, version)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    (folder / "rubrics.json").write_text(
        json.dumps(rubrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    meta = {
        "name": name,
        "version": version,
        "status": status,
        "reviewer": reviewer,
        "model": model,
        "language": "ZH",
        "created_at": datetime.now(UTC).isoformat(),
        "sha": spec_sha(name, version),
    }
    (folder / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return folder


def _split_rubric_text(raw: str) -> list[dict[str, Any]]:
    """把「1. …\\n\\n2. …」这种编号文本拆成条目。"""
    items: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        head, sep, rest = block.partition(". ")
        text = rest.strip() if sep and head.strip().isdigit() else block
        items.append({"text": text})
    return items


def _resolve_rubric_raw(grader: Any) -> Any:
    """优先读生成器落在 kwargs 的字符串，其次兼容 template.rubrics 列表。"""
    kwargs = getattr(grader, "kwargs", None)
    raw = kwargs.get("rubrics") if isinstance(kwargs, dict) else None
    if raw:
        return raw
    template = getattr(grader, "template", None)
    return getattr(template, "rubrics", None) or getattr(grader, "rubrics", None) or []


def _rubric_items(grader: Any) -> list[dict[str, Any]]:
    """把生成器返回的 Grader 里的 rubric 条目摊平成可入库的 JSON。

    OpenJudge 的 SimpleRubricsGenerator 把 rubric 以「1. …\\n\\n2. …」字符串塞进
    `grader.kwargs["rubrics"]`；其它 Grader 可能用 `template.rubrics` 列表。两种形状
    都要能吃下，否则生成日志说成功、落盘却是空数组。
    """
    raw = _resolve_rubric_raw(grader)
    if isinstance(raw, str):
        return _split_rubric_text(raw)
    return [item if isinstance(item, dict) else {"text": str(item)} for item in raw]


async def generate(
    name: str,
    *,
    task_description: str,
    scenario: str = "",
    samples: list[str] | None = None,
    version: str = "v1",
) -> Path:
    """调用 OpenJudge 零样本生成器；失败时回落默认条目并标记 draft。

    1. 建配置（1–5 分/中文/4 兜底） 2. 网关模型跑生成器 3. Grader 摊平成条目落 draft。
    """
    from openjudge.generator.simple_rubric import (
        SimpleRubricsGenerator,
        SimpleRubricsGeneratorConfig,
    )
    from openjudge.graders.schema import GraderMode
    from openjudge.models.schema.prompt_template import LanguageEnum

    from devops_agent.openjudge.grade import judge_model

    model_name = os.environ.get("DEVOPS_AGENT_JUDGE_MODEL") or os.environ.get(
        "DEVOPS_AGENT_MODEL", ""
    )
    config = SimpleRubricsGeneratorConfig(
        grader_name=name,
        model=judge_model(),
        grader_mode=GraderMode.POINTWISE,
        task_description=task_description,
        scenario=scenario or None,
        language=LanguageEnum.ZH,
        min_score=1,
        max_score=5,
        default_rubrics=["事实正确", "有据可查", "诚实边界", "不泄密"],
        max_retries=3,
    )
    grader = await SimpleRubricsGenerator(config).generate(
        dataset=[{"query": text} for text in (samples or [])],
        sample_queries=list(samples or []),
    )
    spec = {
        "name": name,
        "task_description": task_description,
        "scenario": scenario,
        "language": "ZH",
        "min_score": 1,
        "max_score": 5,
    }
    return write_artifact(
        name,
        version,
        spec=spec,
        rubrics=_rubric_items(grader),
        model=model_name,
    )
