"""轨迹采集：fixture → 输入管线 → AgentScope 运行时 → TraceRecord。

只负责「跑一次并如实记录」，不做任何判分。三条不变量：

1. **每个用例一个全新运行时**：会话历史、权限规则、挂起状态都不跨用例，
   否则前一条的 allow 规则会让后一条的越权用例假通过；
2. **工具入参从 Agent 状态取**：事件流只有工具名，判 `tool_args` 与
   ToolParameterCheck 必须拿到 `ToolCallBlock.input` 的原文；
3. **超时/挂起/异常写进 status**：一条坏用例不能中断整批，也不能被当成通过。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from devops_agent.input.pipeline import prepare
from devops_agent.openjudge import sandbox
from devops_agent.openjudge.benchmark import Case
from devops_agent.openjudge.trace import ToolCallRecord, TraceRecord, prompt_sha
from devops_agent.runtime.confirm import ConfirmDecision
from devops_agent.runtime.prompt import prompt_bytes_sha

DEFAULT_TIMEOUT = 180.0
_MAX_CONFIRM_ROUNDS = 3
_RESULT_EXCERPT = 600

# 越权用例要让模型写到工作区之外。不能把相对路径 `../outside/x` 直接写进题面：
# 实测模型会把它当成 `/tmp/outside/x`（少算一层目录）而写到真实根目录下。
# 题面用占位符，采集时替换成临时目录的绝对路径；file_state 仍按相对路径断言，
# 两边都指向同一个文件，且临时目录随用例结束一起删除。
_OUTSIDE_TOKEN = "{{OUTSIDE}}"


def _resolve_outside(text: str, outside: Path) -> str:
    """把题面里的越权目录占位符换成本次运行的真实绝对路径。"""
    return text.replace(_OUTSIDE_TOKEN, outside.as_posix())


def _json_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        return value if isinstance(value, dict) else {"_raw": value}
    return {}


def _block_type(block: Any) -> str:
    return str(getattr(block, "type", "") or (block.get("type") if isinstance(block, dict) else ""))


def _field(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _result_text(output: Any) -> str:
    """把 ToolResultBlock.output（块数组 / 字符串）压成一段可存档的文本。"""
    if isinstance(output, str):
        return output
    texts: list[str] = []
    if isinstance(output, list):
        for item in output:
            text = _field(item, "text", None)
            if isinstance(text, str) and text:
                texts.append(text)
            elif isinstance(item, str):
                texts.append(item)
    return "\n".join(texts)


def _context(runtime: Any) -> list[Any]:
    """读 Agent 的消息上下文；假运行时没有该结构时返回空列表。"""
    agent = getattr(runtime, "_agent", None)
    state = getattr(agent, "state", None)
    context = getattr(state, "context", None)
    return list(context) if isinstance(context, list) else []


def _absorb_block(block: Any, trace: TraceRecord, pending: dict[str, ToolCallRecord]) -> None:
    """单个内容块：工具调用建记录，工具结果回填对应记录。"""
    kind = _block_type(block)
    if kind in ("tool_call", "tool_use"):
        call_id = str(_field(block, "id", "") or "")
        record = ToolCallRecord(
            name=str(_field(block, "name", "") or ""),
            args=_json_args(_field(block, "input", {})),
            state=str(_field(block, "state", "") or ""),
        )
        pending[call_id] = record
        trace.tool_calls.append(record)
    elif kind == "tool_result":
        call_id = str(_field(block, "id", "") or "")
        found = pending.get(call_id)
        if found is not None:
            found.state = str(_field(block, "state", found.state) or found.state)
            found.result_excerpt = _result_text(_field(block, "output", ""))[:_RESULT_EXCERPT]


def _collect(
    runtime: Any, start: int, trace: TraceRecord, pending: dict[str, ToolCallRecord]
) -> int:
    """从第 start 条消息起增量抽取工具调用与结果，返回新的游标。"""
    context = _context(runtime)
    for message in context[start:]:
        content = _field(message, "content", [])
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            _absorb_block(block, trace, pending)
    return len(context)


def _final_text(runtime: Any) -> str:
    """最后一条 assistant 消息的全部文本块（拼起来就是用户看到的回答）。"""
    text = ""
    for message in _context(runtime):
        if str(_field(message, "role", "")) != "assistant":
            continue
        content = _field(message, "content", [])
        blocks = content if isinstance(content, list) else [content]
        parts = [str(_field(block, "text", "")) for block in blocks if _block_type(block) == "text"]
        joined = "".join(part for part in parts if part)
        if joined:
            text = joined
    return text


def _absorb(events: list[dict[str, Any]], trace: TraceRecord) -> None:
    """把一轮事件并入轨迹：事件名、tokens（MODEL_CALL_END 才有）。"""
    for event in events:
        trace.events.append(str(event.get("type", "")))
        if event.get("type") == "MODEL_CALL_END":
            trace.tokens["input"] = trace.tokens.get("input", 0) + int(
                event.get("input_tokens", 0) or 0
            )
            trace.tokens["output"] = trace.tokens.get("output", 0) + int(
                event.get("output_tokens", 0) or 0
            )
            trace.tokens["cache_hit"] = trace.tokens.get("cache_hit", 0) + int(
                event.get("cache_input_tokens", 0) or 0
            )


def _decisions(pending: list[dict[str, Any]], decision: str) -> list[ConfirmDecision]:
    confirmed = decision != "deny"
    scope = "exact" if decision == "allow_always" else "once"
    return [
        ConfirmDecision(
            tool_call_id=str(item.get("tool_call_id", "")),
            confirmed=confirmed,
            scope=scope,
        )
        for item in pending
    ]


def _record_stages(prepared: Any, trace: TraceRecord) -> None:
    trace.input_stages = [
        {
            "stage": stage.stage,
            "status": stage.status,
            "changed": stage.changed,
            "summary": stage.summary,
            "before": stage.before,
            "after": stage.after,
            "failed": stage.failed,
        }
        for stage in prepared.stages
    ]


async def _drive(
    runtime: Any, case: Case, trace: TraceRecord, prepared: Any, cursor: int, outside: Path
) -> None:
    """按 turns 驱动：用户输入 → 事件 → 挂起 → 确认续跑。

    1. 逐轮取用户输入，先把 `{{OUTSIDE}}` 换成本次运行的绝对路径再送进运行时；
    2. 每轮结束用游标增量抽取工具调用与结果，不重复扫历史消息；
    3. 运行时挂起时：后面还有 confirm 轮就续跑，没有就标 `parked` 提前收尾；
    4. 续跑最多 `_MAX_CONFIRM_ROUNDS` 轮，防止模型反复要授权把批次拖死。
    """
    pending: dict[str, ToolCallRecord] = {}
    turns = list(case.turns)
    index = 0
    while index < len(turns):
        turn = turns[index]
        if turn.is_confirm:
            # 没有挂起时的确认轮是无意义数据，跳过而不是假装续跑
            index += 1
            continue
        trace.turns.append({"user": turn.text, "final": ""})
        text = _resolve_outside(turn.text, outside)
        events = [event async for event in runtime.stream(f"oj-{trace.run_id}", text)]
        _absorb(events, trace)
        cursor = _collect(runtime, cursor, trace, pending)
        index += 1
        rounds = 0
        while rounds < _MAX_CONFIRM_ROUNDS:
            awaiting = runtime.pending_calls()
            if not awaiting:
                break
            if index >= len(turns) or not turns[index].is_confirm:
                trace.events.append("parked")
                trace.status = "parked"
                trace.final_text = _final_text(runtime)
                trace.turns[-1]["final"] = trace.final_text
                return
            trace.events.append("RESUME")
            resumed = [
                event async for event in runtime.resume(_decisions(awaiting, turns[index].decision))
            ]
            _absorb(resumed, trace)
            cursor = _collect(runtime, cursor, trace, pending)
            index += 1
            rounds += 1
        trace.turns[-1]["final"] = _final_text(runtime)
    trace.final_text = _final_text(runtime)


async def run_case(
    case: Case,
    *,
    suite: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    runtime_factory: Callable[[Path, Case], Any] | None = None,
) -> TraceRecord:
    """跑一条用例并返回轨迹；任何失败都落在 status/error 上，不向外抛。

    1. 复制 fixture 到临时工作区，并给越权用例准备同级 `outside/` 目录；
    2. 先跑输入管线并留档 —— 离线用例不产生模型回答，管线产物就是唯一证据；
    3. 非离线用例才建运行时，`wait_for` 兜住超时；
    4. 无论成败都算 diff、记延迟，最后删掉临时目录。
    """
    workdir = sandbox.copy_fixture(case.fixture)
    outside = workdir.parent / "outside"
    trace = TraceRecord.for_case(case, suite_sha=suite)
    trace.model = os.environ.get("DEVOPS_AGENT_MODEL", "")
    trace.prompt_sha = prompt_sha(prompt_bytes_sha(workdir))
    before = sandbox.snapshot(workdir)
    started = time.perf_counter()
    try:
        prepared = prepare(_resolve_outside(case.prompt, outside), workspace_root=workdir)
        _record_stages(prepared, trace)
        # 输入管线的最终文本是 D 赛道「密钥是否真被脱敏」的唯一证据：
        # 离线用例不产生模型回答，必须把管线产物一起存档。
        trace.judge_input = {
            "prepared_text": prepared.text[:8000],
            "references": [{"kind": ref.kind, "value": ref.value} for ref in prepared.references],
        }
        if not case.offline:
            runtime = (
                runtime_factory(workdir, case)
                if runtime_factory is not None
                else _default_runtime(workdir, case)
            )
            await asyncio.wait_for(_drive(runtime, case, trace, prepared, 0, outside), timeout)
    except TimeoutError:
        trace.status = "timeout"
        trace.error = f"超过 {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001 - 单条用例失败不能中断整批
        trace.status = "error"
        trace.error = f"{type(exc).__name__}: {exc}"
    finally:
        after = sandbox.snapshot(workdir)
        trace.workspace = _workspace_state(before, after, workdir, case)
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        sandbox.cleanup(workdir)
    return trace


def _workspace_state(
    before: dict[str, bytes], after: dict[str, bytes], workdir: Path, case: Case
) -> dict[str, Any]:
    """工作区终态：变更清单、diff、哈希，以及断言涉及文件的正文快照。"""
    states: dict[str, Any] = {}
    for item in case.expected.file_state:
        raw = sandbox.read_state(workdir, item.path)
        states[item.path] = {
            "exists": raw is not None,
            "text": (raw or b"").decode("utf-8", errors="replace")[:65536],
            "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else "",
        }
    return {
        "changed": sandbox.changed_files(before, after),
        "diff": sandbox.diff_text(before, after),
        "hashes": sandbox.hashes(after),
        "states": states,
    }


def _default_runtime(workdir: Path, case: Case) -> Any:
    """真实运行时工厂：延迟 import，离线测试不需要装 AgentScope。"""
    from devops_agent.governance.profiles import resolve
    from devops_agent.runtime.assembly import build_agentscope_runtime

    return build_agentscope_runtime(workdir=workdir, profile=resolve(case.permission))
