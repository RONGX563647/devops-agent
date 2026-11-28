"""RunRecorder 主体 —— 消费事件 dict 流，聚合出 run + spans 落库。"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from devops_agent.observability import repository as repo
from devops_agent.observability.models import InputStageRecord, RunRecord
from devops_agent.observability.recorder.audit import WebAccessLog
from devops_agent.observability.recorder.input_stage import input_stage_record
from devops_agent.observability.recorder.memory import MEMORY_SPAN_NAMES, memory_span
from devops_agent.observability.recorder.sink import SpanSink
from devops_agent.observability.recorder.timing import (
    _iso_to_epoch_ms,
    _work_ms,
    utc_now,
)
from devops_agent.observability.recorder.usage import _token_delta

# 单个 span 的入参/返回最长保留字符数。工具返回可能是整份日志，
# 全量落库会让 sqlite 文件无上限膨胀；8k 足够定位问题，超长部分靠 run 的输出兜底。
MAX_SPAN_TEXT = 8_000


class RunRecorder(SpanSink):
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
        workspace_id: str = "",
    ) -> None:
        self._db_path = db_path or repo.default_db_path()
        self._domain = domain
        self._model = model
        self._request_id = request_id
        self._permission = permission
        # 本轮所属工作区（SPEC-0021）：空串 = 未绑定，读侧必须显示「未记录」
        self._workspace_id = workspace_id
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
        # 事件里带回来的异常原文：REPLY_END 常常只有 finished_reason=error，
        # 不在这里接住，finish() 就只能写一句「未捕获到异常信息」
        self._run_error = ""
        self._finished = False
        self._pending: dict[str, dict[str, object]] = {}
        # 模型调用按 reply_id 排队：一轮 ReAct 里同一 reply 会连续调多次模型，
        # 只用 reply_id 当键会让第二次 START 覆盖第一次，落库只剩最后一次。
        self._model_pending: dict[str, deque[dict[str, object]]] = {}
        # 最近的工具调用 id：部分 delta 事件不带 tool_call_id，只能回落到上一个工具
        self._last_tool_key = ""
        # 输入解析阶段在过程中先缓存，finish 时随 run 一起落库（run_id 此时才齐备）
        self._input_stages: list[InputStageRecord] = []
        # 本轮网络获取的决策（net / 缺口 / 通路）：同样在 finish 时一次写库
        self._web_access = WebAccessLog()

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
            workspace_id=self._workspace_id,
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
        "THINKING_BLOCK_DELTA": "_on_thinking_delta",
        "EXCEED_MAX_ITERS": "_on_exceed_iters",
        "REPLY_END": "_on_reply_end",
        "INPUT_PARSE_STAGE": "_on_input_stage",
        **{name: "_on_memory" for name in MEMORY_SPAN_NAMES},
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
            # 失败原因必须逐条落库：只写 status=error 的话，事后只看得到一个红点，
            # 「Command failed / Permission denied」这些现场信息全丢了
            error=None if ok else str(event.get("error") or "") or None,
        )

    def _on_text_delta(self, event: dict[str, object]) -> None:
        self._mark_first_token(event)
        self._final_text += str(event.get("delta", ""))

    def _on_thinking_delta(self, event: dict[str, object]) -> None:
        """思考增量只用于计算首 token 延迟，正文不并入最终输出。"""
        self._mark_first_token(event)

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
        # 事件自带的异常原文优先：框架在内部收口时不会把 exc 抛给调用方
        if not self._run_error:
            self._run_error = str(event.get("error") or "")

    def _on_input_stage(self, event: dict[str, object]) -> None:
        """缓存一条输入解析阶段；全量保留（含 skip），供事后审计与复现。"""
        self._input_stages.append(
            input_stage_record(event, run_id=self._run_id or 0, seq=len(self._input_stages))
        )

    def _on_memory(self, event: dict[str, object]) -> None:
        """记忆动作落一条 span；跳过态也留痕，回答「为什么没记忆」。"""
        if self._run_id is None:
            return
        span = memory_span(
            event,
            run_id=self._run_id,
            seq=self._seq + 1,
            start_offset_ms=self._rel_ms(time.perf_counter_ns()),
        )
        if span is None:
            return
        self._seq += 1
        with repo.connect(self._db_path) as conn:
            repo.add_span(conn, span)

    def _finish_terminal_fields(self, status: str | None, error: str | None) -> tuple[str, str]:
        """终态与失败原因：显式 status 优先，否则按结束原因推断；原因缺失时兜底一句。"""
        final_status = status
        if final_status is None:
            final_status = (
                "failed" if self._finished_reason in ("error", "exceed_max_iters") else "success"
            )
        reason = error or self._run_error or (
            ""
            if final_status == "success"
            # 连事件都没带原文时，至少把结束原因写出来：含糊的「未捕获」等于没记
            else f"本轮以 {self._finished_reason or 'error'} 结束，事件未携带异常详情"
        )
        return final_status, reason

    def _finish_usage_fields(self) -> tuple[int, int, int, int, int]:
        """汇总本轮 token 与缓存用量（全程累加值）：total/input/output/cache/cache_write。"""
        return (
            self._input_tokens + self._output_tokens,
            self._input_tokens,
            self._output_tokens,
            self._cache_input_tokens,
            self._cache_creation_input_tokens,
        )

    def finish(self, status: str | None = None, error: str | None = None) -> int:
        """回填 run 的终态与汇总，返回 run_id。重复调用幂等。

        error 只在失败时给：把异常原文落库，否则 status=failed 只是个标签，
        刷新后既看不到原因也复现不了（实测出现过 span 全绿却 failed 的情况）。
        """
        # 1. 未建 run 或已收尾：直接返回，保持幂等
        if self._run_id is None:
            return -1
        if self._finished:
            return self._run_id
        # 2. 确定终态与失败原因（失败时必须有可读原因）
        ended_at = utc_now()
        duration = int(datetime.now(UTC).timestamp() * 1000) - self._started_monotonic_ms
        final_status, reason = self._finish_terminal_fields(status, error)
        total_tokens, in_tokens, out_tokens, cache_in, cache_write = self._finish_usage_fields()
        with repo.connect(self._db_path) as conn:
            # 3. 双口径耗时：work 是 span 并集，wall 是起止差（含空闲），work 以 wall 封顶
            spans = repo.list_spans(conn, self._run_id)
            wall_ms = max(0, duration)
            work_ms = _work_ms(spans, wall_ms)
            # 4. 先落输入阶段，再用终态、正文与用量回填 run 记录
            repo.add_input_stages(conn, self._run_id, self._input_stages)
            repo.finish_run(
                conn,
                self._run_id,
                status=final_status,
                output=self._final_text,
                ended_at=ended_at,
                duration_ms=max(0, duration),
                total_tokens=total_tokens,
                health_score=None,  # health_score 由 P2 的健康分模块计算
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cache_input_tokens=cache_in,
                cache_creation_input_tokens=cache_write,
                work_ms=work_ms,
                wall_ms=wall_ms,
                error=reason,
            )
        # 5. 网络获取决策单独一条 UPDATE（不改 finish_run 的 SQL，减少回归面）
        self._web_access.flush(self._db_path, self._run_id)
        # 6. 清空过程缓存并标记收尾，避免重复落库
        self._input_stages = []
        self._finished = True
        return self._run_id

    def note_web_access(self, **fields: object) -> None:
        """记录一项网络获取决策（可多次调用，后者覆盖同名键；finish 时一次落库）。"""
        self._web_access.note(**fields)

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
