"""RunRecorder 主体 —— 消费事件 dict 流，聚合出 run + spans 落库。"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from devops_agent.observability import repository as repo
from devops_agent.observability.models import InputStageRecord, RunRecord, SpanRecord
from devops_agent.observability.recorder.timing import (
    _elapsed_ms,
    _iso_to_epoch_ms,
    _pending_t0,
    _work_ms,
    utc_now,
)
from devops_agent.observability.recorder.usage import _as_int, _token_delta

# 单个 span 的入参/返回最长保留字符数。工具返回可能是整份日志，
# 全量落库会让 sqlite 文件无上限膨胀；8k 足够定位问题，超长部分靠 run 的输出兜底。
MAX_SPAN_TEXT = 8_000


class RunRecorder:
    """一次运行的事件记录器：START 建 run，过程中的事件落 span，END 回填汇总。"""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        domain: str = "debug",
        model: str = "",
        request_id: str = "",
        permission: str = "",
        model_params: dict[str, object] | None = None,
    ) -> None:
        self._db_path = db_path or repo.default_db_path()
        self._domain = domain
        self._model = model
        self._request_id = request_id
        self._permission = permission
        # 本轮实际下发的模型参数（只含非默认值）。空 dict 表示「全是默认值」，
        # 老记录读出来是空串，两者在界面上都显示为「未记录」。
        self._model_params = json.dumps(model_params or {}, ensure_ascii=False, sort_keys=True)
        self._run_id: int | None = None
        self._started_at = ""
        self._started_monotonic_ms = 0
        # 单调时钟：created_at 缺失时唯一的耗时依据。_t0 是本 recorder 的起点，
        # _offset_base_ms 是「run 已经跑了多久」（挂起续跑时不为 0）。
        self._t0 = time.perf_counter_ns()
        self._offset_base_ms = 0
        # run 内 span 序号与最近一次模型 span：泳道/列表共用顺序，工具挂到发起它的模型下
        self._seq = 0
        self._last_model_span_id: int | None = None
        self._input_tokens = 0
        self._output_tokens = 0
        # 前缀缓存：命中量（从缓存读）与创建量（本次写入缓存）。两者都按调用累加，
        # 只留最后一次会让命中率被低估「调用次数」倍。
        self._cache_input_tokens = 0
        self._cache_creation_input_tokens = 0
        self._final_text = ""
        self._finished_reason = ""
        self._finished = False
        self._pending: dict[str, dict[str, object]] = {}
        # 模型调用按 reply_id 排队：一轮 ReAct 里同一 reply 会连续调多次模型，
        # 只用 reply_id 当键会让第二次 START 覆盖第一次，落库只剩最后一次。
        self._model_pending: dict[str, deque[dict[str, object]]] = {}
        # 最近的工具调用 id：部分 delta 事件不带 tool_call_id，只能回落到上一个工具
        self._last_tool_key = ""
        # 输入解析阶段在过程中先缓存，finish 时随 run 一起落库（run_id 此时才齐备）
        self._input_stages: list[InputStageRecord] = []

    def start(self, session_id: str, user_input: str) -> int:
        """建立 run 记录，返回 run_id。可重复调用（幂等）。"""
        if self._run_id is not None:
            return self._run_id
        self._started_at = utc_now()
        self._started_monotonic_ms = _iso_to_epoch_ms(self._started_at)
        self._t0 = time.perf_counter_ns()
        self._offset_base_ms = 0
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
            permission=self._permission,
            model_params=self._model_params,
        )
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            self._run_id = repo.create_run(conn, run)
        return self._run_id

    def attach(self, run_id: int, session_id: str) -> int:
        """接管一条已存在的 run（确认后继续跑同一个 run，不新开记录）。

        起点时间沿用库里的 started_at，续跑产生的 span 偏移量才能接上前半段；
        已产出的正文也读回来，`RUN_DONE` 里的 text 才是这一轮累积结果。
        """
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            record = repo.get_run(conn, run_id)
            spans = repo.list_spans(conn, run_id)
        if record is None or record.session_id != session_id:
            raise LookupError(f"run {run_id} 不存在或不属于会话 {session_id}")
        self._run_id = run_id
        self._started_at = record.started_at
        self._started_monotonic_ms = _iso_to_epoch_ms(record.started_at)
        # 续跑：新 span 的偏移要接上前半段，序号也接着已有 span 往下排
        self._offset_base_ms = max(
            0,
            int(datetime.now(UTC).timestamp() * 1000) - self._started_monotonic_ms,
        )
        self._t0 = time.perf_counter_ns()
        self._seq = max((span.seq for span in spans), default=0)
        model_ids = [span.id for span in spans if span.type == "model" and span.id is not None]
        self._last_model_span_id = max(model_ids, default=None)
        self._final_text = record.output
        self._input_tokens = record.input_tokens
        self._output_tokens = record.output_tokens
        self._cache_input_tokens = record.cache_input_tokens
        self._cache_creation_input_tokens = record.cache_creation_input_tokens
        self._permission = record.permission or self._permission
        self._finished = False
        return run_id

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
        if self._run_id is None:
            return
        etype = str(event.get("type", ""))
        handler_name = self._HANDLERS.get(etype)
        if handler_name is None:
            return  # REPLY_START 与所有 delta 类事件无需落库动作
        getattr(self, handler_name)(event)

    def _rel_ms(self, t_ns: int) -> int:
        """单调时钟 → 相对 run 起点的毫秒偏移。"""
        return self._offset_base_ms + max(0, (t_ns - self._t0) // 1_000_000)

    def _span_offset(self, pending: dict[str, object]) -> int:
        """起点偏移：与耗时同一口径——有事件时间戳就用它，缺失才退回单调时钟。

        两个时钟混用会让「起点偏移」和「耗时」落在两条时间轴上，
        span 并集（work_ms）就再也对不上。
        """
        start_at = str(pending.get("start", "") or "")
        if pending.get("has_time") and start_at:
            return _elapsed_ms(self._started_at, start_at)
        return self._rel_ms(_pending_t0(pending))

    def _on_model_start(self, event: dict[str, object]) -> None:
        raw_at = str(event.get("created_at", "") or "")
        reply = str(event.get("reply_id", ""))
        self._model_pending.setdefault(reply, deque()).append(
            {
                "type": "model",
                "name": str(event.get("model_name", "model")),
                "start": raw_at,
                "has_time": bool(raw_at),
                "t0": time.perf_counter_ns(),
            }
        )

    def _on_model_end(self, event: dict[str, object]) -> None:
        in_tok, out_tok, cache_read, cache_write = _token_delta(event)
        self._input_tokens += in_tok
        self._output_tokens += out_tok
        self._cache_input_tokens += cache_read
        self._cache_creation_input_tokens += cache_write
        reply = str(event.get("reply_id", ""))
        pending = self._pop_model(reply)
        if pending is None:
            return  # 没有起点就不造 span：凭空补一条会让调用数虚高
        self._write_span(
            pending,
            str(event.get("created_at", "") or ""),
            time.perf_counter_ns(),
            in_tok + out_tok,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )

    def _pop_model(self, reply: str) -> dict[str, object] | None:
        """按 FIFO 取该 reply 最早一个未闭合的模型起点。"""
        queue = self._model_pending.get(reply)
        if not queue:
            return None
        pending = queue.popleft()
        if not queue:
            self._model_pending.pop(reply, None)
        return pending

    def _on_tool_start(self, event: dict[str, object]) -> None:
        key = str(event.get("tool_call_id", ""))
        raw_at = str(event.get("created_at", "") or "")
        self._last_tool_key = key
        self._pending[key] = {
            "type": "tool",
            "name": str(event.get("tool_call_name", "tool")),
            "start": raw_at,
            "has_time": bool(raw_at),
            "t0": time.perf_counter_ns(),
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
            str(event.get("created_at", "") or ""),
            time.perf_counter_ns(),
            0,
            status="ok" if ok else "error",
        )

    def _on_text_delta(self, event: dict[str, object]) -> None:
        self._final_text += str(event.get("delta", ""))

    def _on_exceed_iters(self, event: dict[str, object]) -> None:
        self._finished_reason = "exceed_max_iters"
        pending = self._pop_model(str(event.get("reply_id", "")))
        if pending is None:
            return
        self._write_span(
            pending,
            str(event.get("created_at", "") or ""),
            time.perf_counter_ns(),
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
        end_ns: int,
        tokens: int,
        *,
        name: str | None = None,
        status: str = "ok",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        """把 pending 中的工具起点配对成一条 span 落库；无起点则忽略。"""
        pending = self._pending.pop(key, None)
        if pending is None:
            return
        self._write_span(
            pending,
            end_at,
            end_ns,
            tokens,
            name=name,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_input_tokens=cache_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
        )

    def _write_span(
        self,
        pending: dict[str, object],
        end_at: str,
        end_ns: int,
        tokens: int,
        *,
        name: str | None = None,
        status: str = "ok",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        """落一条 span：偏移/耗时走单调时钟，工具挂到最近一次模型调用下。"""
        if self._run_id is None:
            return
        start_at = str(pending.get("start", ""))
        is_tool = str(pending.get("type", "model")) == "tool"
        self._seq += 1
        span = SpanRecord(
            id=None,
            run_id=self._run_id,
            type=str(pending.get("type", "model")),
            name=name or str(pending.get("name", "step")),
            start_offset_ms=self._span_offset(pending),
            duration_ms=self._span_duration(pending, start_at, end_at, end_ns),
            input=str(pending.get("input", "")),
            output=str(pending.get("output", "")),
            tokens=tokens,
            status=status,
            error=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_input_tokens=cache_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            seq=self._seq,
            parent_id=self._last_model_span_id if is_tool else None,
        )
        with repo.connect(self._db_path) as conn:
            span_id = repo.add_span(conn, span)
        if not is_tool:
            self._last_model_span_id = span_id

    @staticmethod
    def _span_duration(pending: dict[str, object], start_at: str, end_at: str, end_ns: int) -> int:
        """优先用事件时间戳（真实发生时刻），缺失时退回单调时钟差值。

        created_at 缺失时旧实现把起止都填成 utc_now()，于是每条 span 都是 0ms；
        单调时钟不依赖事件字段，才画得出真实宽度。
        """
        if pending.get("has_time") and end_at:
            measured = _elapsed_ms(start_at, end_at)
            if measured > 0:
                return measured
        t0 = _pending_t0(pending)
        return max(0, (end_ns - t0) // 1_000_000)

    def finish(self, status: str | None = None, error: str | None = None) -> int:
        """回填 run 的终态与汇总，返回 run_id。重复调用幂等。

        error 只在失败时给：把异常原文落库，否则 status=failed 只是个标签，
        刷新后既看不到原因也复现不了（实测出现过 span 全绿却 failed 的情况）。
        """
        if self._run_id is None:
            return -1
        if self._finished:
            return self._run_id
        ended_at = utc_now()
        duration = int(datetime.now(UTC).timestamp() * 1000) - self._started_monotonic_ms
        final_status = status
        if final_status is None:
            final_status = (
                "failed" if self._finished_reason in ("error", "exceed_max_iters") else "success"
            )
        with repo.connect(self._db_path) as conn:
            spans = repo.list_spans(conn, self._run_id)
            # 实测耗时 = 所有 span 区间的并集；墙钟 = 起止差（含用户空闲）。
            # 实测不可能超过墙钟（事件时钟偶尔会比 run 起点更早），超了就以墙钟封顶。
            wall_ms = max(0, duration)
            work_ms = _work_ms(spans, wall_ms)
            repo.add_input_stages(conn, self._run_id, self._input_stages)
            repo.finish_run(
                conn,
                self._run_id,
                status=final_status,
                output=self._final_text,
                ended_at=ended_at,
                duration_ms=max(0, duration),
                total_tokens=self._input_tokens + self._output_tokens,
                health_score=None,  # health_score 由 P2 的健康分模块计算
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                cache_input_tokens=self._cache_input_tokens,
                cache_creation_input_tokens=self._cache_creation_input_tokens,
                work_ms=work_ms,
                wall_ms=wall_ms,
                # 失败时优先用显式传入的异常原文；没传但被标成 failed 时留一句兜底，
                # 免得又出现「有状态没原因、只能靠猜」的记录
                error=error or ("" if final_status == "success" else "未捕获到异常信息"),
            )
        self._input_stages = []
        self._finished = True
        return self._run_id

    @property
    def run_id(self) -> int | None:
        return self._run_id

    def park(self, pending_json: str, expires_at: str) -> bool:
        """把当前 run 标记为等待确认，返回是否成功（已挂起则不重复写）。"""
        if self._run_id is None:
            return False
        with repo.connect(self._db_path) as conn:
            return repo.park_run(
                conn, self._run_id, pending_json=pending_json, expires_at=expires_at
            )

    @property
    def final_text(self) -> str:
        return self._final_text

    @property
    def token_usage(self) -> tuple[int, int]:
        """返回 (input_tokens, output_tokens)。"""
        return self._input_tokens, self._output_tokens

    @property
    def cache_usage(self) -> tuple[int, int]:
        """返回 (cache_input_tokens, cache_creation_input_tokens)，均为全程累加。"""
        return self._cache_input_tokens, self._cache_creation_input_tokens
