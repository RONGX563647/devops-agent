"""事件记录器 —— 消费事件 dict 流，聚合出 run + spans 落库。

职责边界：
- 输入是普通 dict（由 runtime/events.py 转译），完全不 import agentscope；
- 自己开启连接、管理事务；每个 SSE 请求对应一个 Recorder 实例；
- 用 reply_id 把零散事件配对成 span（START 记起点，END 落一条 span）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from devops_agent.observability import repository as repo
from devops_agent.observability.models import InputStageRecord, RunRecord, SpanRecord

# 单个 span 的入参/返回最长保留字符数。工具返回可能是整份日志，
# 全量落库会让 sqlite 文件无上限膨胀；8k 足够定位问题，超长部分靠 run 的输出兜底。
MAX_SPAN_TEXT = 8_000


def utc_now() -> str:
    """当前 UTC 时间（ISO 8601），供 recorder 打点。"""
    return datetime.now(UTC).isoformat()


def _elapsed_ms(start: str, end: str) -> int:
    """两个 ISO 时间串的毫秒差；解析失败或时区不一致回退 0，不让埋点拖垮主流程。"""
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        return max(0, int(delta.total_seconds() * 1000))
    except (ValueError, TypeError):  # TypeError：offset-naive 与 offset-aware 混减
        return 0


def _as_int(value: object) -> int:
    """事件里的数字字段可能是 object，安全转 int，失败回退 0。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


class RunRecorder:
    """一次运行的事件记录器：START 建 run，过程中的事件落 span，END 回填汇总。"""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        domain: str = "debug",
        model: str = "",
        request_id: str = "",
    ) -> None:
        self._db_path = db_path
        self._domain = domain
        self._model = model
        self._request_id = request_id
        self._run_id: int | None = None
        self._started_at = ""
        self._started_monotonic_ms = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._final_text = ""
        self._finished_reason = ""
        self._pending: dict[str, dict[str, object]] = {}
        # 最近的工具调用 id：部分 delta 事件不带 tool_call_id，只能回落到上一个工具
        self._last_tool_key = ""
        # 输入解析阶段在过程中先缓存，finish 时随 run 一起落库（run_id 此时才齐备）
        self._input_stages: list[InputStageRecord] = []
        self._conn: sqlite3.Connection | None = None

    def start(self, session_id: str, user_input: str) -> int:
        """建立 run 记录，返回 run_id。可重复调用（幂等）。"""
        if self._run_id is not None:
            return self._run_id
        self._started_at = utc_now()
        self._started_monotonic_ms = int(datetime.now(UTC).timestamp() * 1000)
        path = self._db_path or repo.default_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        repo.init_db(self._conn)
        run = RunRecord(
            id=None,
            session_id=session_id,
            domain=self._domain,
            status="running",
            input=user_input,
            output="",
            started_at=self._started_at,
            ended_at=None,
            duration_ms=None,
            model=self._model,
            total_tokens=0,
            health_score=None,
            request_id=self._request_id,
        )
        self._run_id = repo.create_run(self._conn, run)
        self._conn.commit()
        return self._run_id

    # 事件分派表：类型 → 处理方法。新增事件类型只需加一个 _on_* 方法并注册在此。
    _HANDLERS = {
        "MODEL_CALL_START": "_on_model_start",
        "MODEL_CALL_END": "_on_model_end",
        "TOOL_CALL_START": "_on_tool_start",
        "TOOL_CALL_DELTA": "_on_tool_args_delta",
        "TOOL_CALL_END": "_on_tool_end",
        "TOOL_RESULT_TEXT_DELTA": "_on_tool_result_delta",
        "TOOL_RESULT_END": "_on_tool_end",
        "TEXT_BLOCK_DELTA": "_on_text_delta",
        "EXCEED_MAX_ITERS": "_on_exceed_iters",
        "REPLY_END": "_on_reply_end",
        "INPUT_PARSE_STAGE": "_on_input_stage",
    }

    def consume(self, event: dict[str, object]) -> None:
        """消费一个事件 dict：查分派表调用对应处理器，未知事件忽略。"""
        if self._conn is None or self._run_id is None:
            return
        etype = str(event.get("type", ""))
        handler_name = self._HANDLERS.get(etype)
        if handler_name is None:
            return  # REPLY_START 与所有 delta 类事件无需落库动作
        getattr(self, handler_name)(event)

    @staticmethod
    def _event_time(event: dict[str, object]) -> str:
        return str(event.get("created_at", "")) or utc_now()

    def _on_model_start(self, event: dict[str, object]) -> None:
        self._pending[str(event.get("reply_id", ""))] = {
            "type": "model",
            "name": str(event.get("model_name", "model")),
            "start": self._event_time(event),
        }

    def _on_model_end(self, event: dict[str, object]) -> None:
        in_tok = _as_int(event.get("input_tokens", 0))
        out_tok = _as_int(event.get("output_tokens", 0))
        self._input_tokens += in_tok
        self._output_tokens += out_tok
        self._close_span(
            str(event.get("reply_id", "")),
            self._event_time(event),
            in_tok + out_tok,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    def _on_tool_start(self, event: dict[str, object]) -> None:
        key = str(event.get("tool_call_id", ""))
        self._last_tool_key = key
        self._pending[key] = {
            "type": "tool",
            "name": str(event.get("tool_call_name", "tool")),
            "start": self._event_time(event),
            "input": "",
            "output": "",
        }

    def _on_tool_args_delta(self, event: dict[str, object]) -> None:
        """累积工具入参：span 里没有入参，事后就回答不了「它到底执行了什么」。"""
        self._append_tool_text(event, "input")

    def _on_tool_result_delta(self, event: dict[str, object]) -> None:
        """累积工具返回，与入参分开存放，前端可以按页签分别渲染。"""
        self._append_tool_text(event, "output")

    def _append_tool_text(self, event: dict[str, object], field: str) -> None:
        """把一条增量拼到对应工具 span 的 input/output 上，超长时保留尾部。"""
        key = str(event.get("tool_call_id", "")) or self._last_tool_key
        pending = self._pending.get(key)
        if pending is None or pending.get("type") != "tool":
            return
        merged = str(pending.get(field, "")) + str(event.get("delta", ""))
        pending[field] = merged[-MAX_SPAN_TEXT:]

    def _on_tool_end(self, event: dict[str, object]) -> None:
        state = event.get("state")
        ok = str(event.get("type")) == "TOOL_CALL_END" or str(state).lower() in (
            "ok",
            "success",
            "succeeded",
        )
        self._close_span(
            str(event.get("tool_call_id", "")),
            self._event_time(event),
            0,
            status="ok" if ok else "error",
        )

    def _on_text_delta(self, event: dict[str, object]) -> None:
        self._final_text += str(event.get("delta", ""))

    def _on_exceed_iters(self, event: dict[str, object]) -> None:
        self._finished_reason = "exceed_max_iters"
        self._close_span(
            str(event.get("reply_id", "")),
            self._event_time(event),
            0,
            name="exceed_max_iters",
        )

    def _on_reply_end(self, event: dict[str, object]) -> None:
        self._finished_reason = str(event.get("finished_reason", "")) or "completed"

    def _on_input_stage(self, event: dict[str, object]) -> None:
        """缓存一条输入解析阶段；全量保留（含 skip），供事后审计与复现。"""
        self._input_stages.append(
            InputStageRecord(
                id=None,
                run_id=self._run_id or 0,
                seq=len(self._input_stages),
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
        )

    def _close_span(
        self,
        key: str,
        end_at: str,
        tokens: int,
        *,
        name: str | None = None,
        status: str = "ok",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """把 pending 中的起点配对成一条 span 落库；无起点则忽略。"""
        if self._conn is None or self._run_id is None:
            return
        pending = self._pending.pop(key, None)
        if pending is None:
            return
        start = str(pending.get("start", ""))
        offset = _elapsed_ms(self._started_at, start)
        duration = _elapsed_ms(start, end_at)
        span = SpanRecord(
            id=None,
            run_id=self._run_id,
            type=str(pending.get("type", "model")),
            name=name or str(pending.get("name", "step")),
            start_offset_ms=offset,
            duration_ms=duration,
            input=str(pending.get("input", "")),
            output=str(pending.get("output", "")),
            tokens=tokens,
            status=status,
            error=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        repo.add_span(self._conn, span)
        self._conn.commit()

    def finish(self, status: str | None = None) -> int:
        """回填 run 的终态与汇总，关闭连接，返回 run_id。"""
        if self._conn is None or self._run_id is None:
            return -1
        ended_at = utc_now()
        duration = int(datetime.now(UTC).timestamp() * 1000) - self._started_monotonic_ms
        final_status = status
        if final_status is None:
            final_status = (
                "failed" if self._finished_reason in ("error", "exceed_max_iters") else "success"
            )
        repo.add_input_stages(self._conn, self._run_id, self._input_stages)
        repo.finish_run(
            self._conn,
            self._run_id,
            status=final_status,
            output=self._final_text,
            ended_at=ended_at,
            duration_ms=max(0, duration),
            total_tokens=self._input_tokens + self._output_tokens,
            health_score=None,  # health_score 由 P2 的健康分模块计算
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )
        self._conn.commit()
        self._conn.close()
        self._conn = None
        return self._run_id

    @property
    def run_id(self) -> int | None:
        return self._run_id

    @property
    def final_text(self) -> str:
        return self._final_text

    @property
    def token_usage(self) -> tuple[int, int]:
        """返回 (input_tokens, output_tokens)。"""
        return self._input_tokens, self._output_tokens
