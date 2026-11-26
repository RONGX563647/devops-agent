"""轨迹记录：一次执行的稳定快照（用例、工具入参、事件、工作区 diff）。

轨迹是执行与判分的唯一接口：判分只读 TraceRecord，不再回头驱动 Agent。
因此这里必须把**判分需要的一切**都固化下来（入参而不只是工具名、事件顺序、
输入管线各阶段、diff、tokens），否则换 Grader 就得重跑模型。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devops_agent.openjudge.benchmark import Case


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def prompt_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class ToolCallRecord:
    """一次工具调用：name + 解析后的入参 + 终态 + 结果摘要。"""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    state: str = ""
    result_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args": self.args,
            "state": self.state,
            "result_excerpt": self.result_excerpt,
        }


def _calls_from(raw: dict[str, Any]) -> list[ToolCallRecord]:
    return [
        ToolCallRecord(
            name=str(item.get("name", "")),
            args=dict(item.get("args") or {}),
            state=str(item.get("state", "")),
            result_excerpt=str(item.get("result_excerpt", "")),
        )
        for item in raw.get("tool_calls", [])
    ]


@dataclass(slots=True)
class TraceRecord:
    """一条用例的执行轨迹（SPEC §5 的字段超集，空值也保留 key）。"""

    case_id: str
    run_id: str = field(default_factory=new_run_id)
    #: 第几次试验（从 0 开始）；trials=1 时恒为 0。
    trial: int = 0
    config_id: str = "default"
    suite_sha: str = ""
    prompt_sha: str = ""
    model: str = ""
    status: str = "success"
    turns: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    input_stages: list[dict[str, Any]] = field(default_factory=list)
    workspace: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""
    tokens: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    error: str = ""
    track: str = ""
    risk: str = ""
    subject: str = ""
    permission: str = ""
    judge_input: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_case(cls, case: Case, **kwargs: Any) -> TraceRecord:
        return cls(
            case_id=case.id,
            track=case.track,
            risk=case.risk,
            subject=case.subject,
            permission=case.permission,
            config_id=case.config_id,
            **kwargs,
        )

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "trial": self.trial,
            "config_id": self.config_id,
            "suite_sha": self.suite_sha,
            "prompt_sha": self.prompt_sha,
            "model": self.model,
            "status": self.status,
            "track": self.track,
            "risk": self.risk,
            "subject": self.subject,
            "permission": self.permission,
            "turns": self.turns,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "events": self.events,
            "input_stages": self.input_stages,
            "workspace": self.workspace,
            "final_text": self.final_text,
            "tokens": self.tokens,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "judge_input": self.judge_input,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TraceRecord:
        return cls(
            case_id=str(raw.get("case_id", "")),
            run_id=str(raw.get("run_id", "")),
            trial=int(raw.get("trial", 0) or 0),
            config_id=str(raw.get("config_id", "default")),
            suite_sha=str(raw.get("suite_sha", "")),
            prompt_sha=str(raw.get("prompt_sha", "")),
            model=str(raw.get("model", "")),
            status=str(raw.get("status", "success")),
            turns=list(raw.get("turns") or []),
            tool_calls=_calls_from(raw),
            events=[str(x) for x in raw.get("events", [])],
            input_stages=list(raw.get("input_stages") or []),
            workspace=dict(raw.get("workspace") or {}),
            final_text=str(raw.get("final_text", "")),
            tokens={str(k): int(v) for k, v in (raw.get("tokens") or {}).items()},
            latency_ms=int(raw.get("latency_ms", 0)),
            error=str(raw.get("error", "")),
            track=str(raw.get("track", "")),
            risk=str(raw.get("risk", "")),
            subject=str(raw.get("subject", "")),
            permission=str(raw.get("permission", "")),
            judge_input=dict(raw.get("judge_input") or {}),
        )

    def write_jsonl(self, path: Path) -> None:
        """追加一行轨迹；父目录不存在时创建。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.to_dict(), ensure_ascii=False) + "\n")


def load_traces(path: Path) -> list[TraceRecord]:
    if not path.exists():
        return []
    traces: list[TraceRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            traces.append(TraceRecord.from_dict(json.loads(line)))
    return traces
