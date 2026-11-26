"""输入解析阶段 → 记录对象；recorder 只负责追加与序号。"""

from __future__ import annotations

from devops_agent.observability.models import InputStageRecord
from devops_agent.observability.recorder.usage import _as_int


def input_stage_record(event: dict[str, object], *, run_id: int, seq: int) -> InputStageRecord:
    """全量保留（含 skip），供事后审计与复现。"""
    return InputStageRecord(
        id=None,
        run_id=run_id,
        seq=seq,
        stage=str(event.get("stage", "")),
        label=str(event.get("stage_label", "")),
        status=str(event.get("state", "ok")),
        changed=bool(event.get("changed", False)),
        summary=str(event.get("summary", "")),
        before=_as_int(event.get("before", 0)),
        after=_as_int(event.get("after", 0)),
        items=_as_int(event.get("items", 0)),
        duration_ms=_as_int(event.get("ms", 0)),
        detail=str(event.get("detail", "")),
        failed=_as_int(event.get("failed", 0)),
    )
