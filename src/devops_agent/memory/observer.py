"""记忆中间件 —— 在每次模型调用前做短期裁剪与尾部注入，回复结束后写长期。

位置与 `ProfileNoteMiddleware` 相同：只改本次请求的 messages 副本，不写历史。
失败一律降级为事件：记忆坏了不能让一次对话失败。

SPEC-0016 之后这里多了两件事：

1. **预算执行**：把 `history_limit` 传给裁剪，未超预算时原样返回（S0）；
2. **前缀缓存指纹**：每轮报 `prefix_fingerprint` / `history_changed_at` /
   `history_changed_by`，让「缓存为什么失效」在轨迹里可归因，而不是靠猜。
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any, cast

from agentscope.agent import Agent
from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase

from devops_agent.memory import calibration, events, health
from devops_agent.memory.runtime import MemoryRuntime, estimate_prefix_tokens
from devops_agent.memory.short_term import STAGE_REASON, trim_messages
from devops_agent.memory.summarize import normalize
from devops_agent.memory.tokens import count_messages, estimate_tokens, message_prose, message_text
from devops_agent.memory.tokens import default as tokenizer
from devops_agent.memory.turns import injected_note, is_user_turn


def _text_of(msg: Any) -> str:
    return message_text(msg)


def _prompt_tokens(usage: Any) -> int:
    """从框架的 ChatUsage 取「这次 prompt 实际用了多少 token」，缺字段按 0。"""
    if usage is None:
        return 0
    total = 0
    for name in ("input_tokens", "cache_input_tokens", "cache_creation_input_tokens"):
        total += int(getattr(usage, name, 0) or 0)
    return total


def _last_user_text(messages: list[Msg]) -> str:
    for msg in reversed(messages):
        if is_user_turn(msg):
            return _text_of(msg)
    return ""


def _last_assistant_text(agent: Agent) -> str:
    """最后一条助手消息的**正文**：工具入参/结果不算「说过的话」（见 `message_prose`）。"""
    for msg in reversed(getattr(agent.state, "context", []) or []):
        if getattr(msg, "role", "") == "assistant":
            return message_prose(msg)
    return ""


def _input_text(inputs: Any) -> str:
    if inputs is None:
        return ""
    if isinstance(inputs, list):
        return _text_of(inputs[-1]) if inputs else ""
    return _text_of(inputs)


#: 命中正文短于此长度就不做「已在上下文」判定：短串（"用 ruff"）到处都能子串
#: 命中，按它过滤会把真正有用的短结论一起丢掉。
_ECHO_MIN_CHARS = 24


def _context_blob(messages: list[Any]) -> str:
    """本轮请求里已有的历史文本（归一化后），用于识别「召回把自己说过的话还回来」。"""
    return normalize("\n".join(_text_of(msg) for msg in messages))


def _already_said(text: str, blob: str) -> bool:
    """这条记忆的每一句都已经在本轮上下文里吗。

    逐句判定而不是整串判定：ReAct 回合里用户问题与最终结论之间隔着工具调用与
    工具结果，整条记忆的拼接串不会作为连续子串出现在历史里，于是「复读自己」的
    过滤在最有价值的回合（带工具的那种）恰好失效 —— 实测就是这么漏的。

    短于 `_ECHO_MIN_CHARS` 的句子不参与判定：短串到处都能子串命中，会误杀。
    一句都没剩下时判「不是复读」：宁可多注入一条，也不吞掉真结论。
    """
    segments = [normalize(line) for line in text.splitlines()]
    meaningful = [seg for seg in segments if len(seg) >= _ECHO_MIN_CHARS]
    if not meaningful:
        return False
    return all(seg in blob for seg in meaningful)


def _drop_echoes(hits: list[Any], blob: str) -> list[Any]:
    """剔掉已经在本轮上下文里的命中（`blob` 传原文即可，内部自己归一化）。

    长期层为保住可检索性存的是「用户原话 + 结论」，而上一轮这段话本来就在历史
    里：原样注入等于让模型复读自己，还白占 note 预算（实测「召回块里是我上一轮
    的开场白」就是这种形态）。历史被预算裁掉之后同一条会重新生效 —— 那才是
    长期层该起作用的时刻。
    """
    body_blob = normalize(blob)
    if not body_blob:
        return hits
    return [hit for hit in hits if not _already_said(str(getattr(hit, "text", "")), body_blob)]


def _retrieve_outcome(
    *,
    note: str,
    has_card: bool,
    kept: int,
    requested: int,
    degraded: tuple[str, ...],
    degraded_reason: str,
) -> tuple[str, str]:
    """召回结果的 (state, reason)；分支顺序就是优先级，集中在一处才看得出语义。

    降级优先于 hit/miss（后端挂了却显示「没命中」等于把故障伪装成正常）；
    「命中的全在历史里」与「额度被压到 0」都必须与「确实没有记忆」区分开，
    否则评测会把去重与预算问题一起统计成召回失败。
    """
    if degraded:
        return "error", degraded_reason
    if note:
        return "hit", ""
    if requested and not kept:
        return "miss", events.REASON_ALREADY_IN_CONTEXT
    if has_card or kept:
        return "skipped", events.REASON_BUDGET_EXHAUSTED
    return "miss", events.REASON_NO_MEMORY


def _turn_key(reply: str, messages: list[Msg]) -> str:
    """本轮的稳定标识：`reply_id` 优先，为空时回落到首条消息的摘要。

    不能直接用 `reply_id` 当缓存键：框架在某些路径下不填它，空串会让
    「同一轮内的多次模型调用」与「不同轮」共用同一个键 —— 前者重复召回，
    后者被误判成同一轮而漏注入（SPEC-0013 §18.2 M10）。
    """
    if reply:
        return reply
    head = _text_of(messages[0]) if messages else ""
    return "h:" + hashlib.sha256(head.encode("utf-8")).hexdigest()[:12]


def _tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("name", tool.get("function", {}).get("name", "")))


def _canonical_tools(tools: list[dict[str, Any]]) -> str:
    """把 tools 归一成「稳定字符串」。

    `json.dumps(sort_keys=True)` 只排 dict 的键，不排 list 里的元素，所以工具
    列表换个顺序就会算出不同指纹 —— 但真实前缀里工具的顺序确实变了，指纹
    跟着变才对。问题出在测试与调用方常按不同顺序传同一批工具：为了区分
    「顺序真的变了」与「只是同一批工具换了个排列」，这里按名字排序后再序列化，
    并把名字并入 body，名字集合变化同样会换指纹。
    """
    ordered = sorted(tools, key=_tool_name)
    return json.dumps(ordered, ensure_ascii=False, sort_keys=True, default=str)


def prefix_fingerprint(system_text: str, tools: list[dict[str, Any]]) -> str:
    """V1 的可校验形式：系统提示 + 工具名序 + 工具 schema 的指纹。

    只对「内容集合」敏感、对「传入顺序」不敏感：同一批工具无论是注册顺序
    还是权限过滤后的排列，都指向同一个可复用前缀（真实前缀由框架按名序
    重建）。名字列表与完整 schema 都进 body，所以增删工具或改参数会让指纹变。
    """
    names = sorted(_tool_name(tool) for tool in tools)
    body = system_text + "|" + ",".join(names) + "|" + _canonical_tools(tools)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _message_key(msg: Msg) -> str:
    return json.dumps(msg.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, default=str)


def _first_diff(previous: list[str] | None, current: list[str]) -> int:
    """与上一轮逐条比较的第一个差异下标；`-1` 表示没变。

    首轮没有「上一轮」，按未变处理（`prefix_reused=false` 已说明这是冷启动）。
    """
    if previous is None:
        return -1
    for index, (left, right) in enumerate(zip(previous, current, strict=False)):
        if left != right:
            return index
    return len(previous) if len(previous) != len(current) else -1


class MemoryMiddleware(MiddlewareBase):
    """短期裁剪 + 中期/长期注入 + 回合末长期写入。"""

    def __init__(self, runtime: MemoryRuntime) -> None:
        self._runtime = runtime
        self._policy = runtime.policy
        # 同一轮内多次模型调用复用同一份 note；回复结束即清空，不落快照
        self._note_cache: dict[str, str] = {}
        self._assembled_turn = ""
        # 估算对账每轮只做一次：多轮累计会掩盖单次口径错误
        self._audited_turn = ""
        # 前缀缓存观察点：上一轮的指纹与逐条消息键
        self._last_fingerprint = ""
        self._last_history: list[str] | None = None

    # --- 模型调用 -----------------------------------------------------
    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        """1) 按预算裁剪；2) 观察缓存前缀；3) 组装并追加 note；4) 交给下一层。"""
        messages = list(input_kwargs.get("messages") or [])
        tools = list(input_kwargs.get("tools") or [])
        system_text = _text_of(messages[0]) if messages else ""
        prefix = estimate_prefix_tokens(system_text, tools)
        budget = self._runtime.budget(agent, prefix)
        trimmed, report = trim_messages(messages, self._policy.short_term, budget.history_limit)
        self._emit_trim(report)
        cache = self._observe_cache(system_text, tools, trimmed, report, prefix)
        turn = _turn_key(str(getattr(agent.state, "reply_id", "") or ""), trimmed)
        note = await self._note_for(agent, trimmed, turn, budget)
        if note:
            trimmed.append(injected_note(note, kind="memory"))
        self._emit_assembled_once(
            agent, turn, self._payload(messages, trimmed, tools, note, report, budget, cache)
        )
        result = await next_handler(messages=trimmed)
        return self._audit_once(result, trimmed, tools, turn, prefix)

    def _observe_cache(
        self,
        system_text: str,
        tools: list[dict[str, Any]],
        history: list[Msg],
        report: Any,
        prefix: int,
    ) -> dict[str, Any]:
        """三段指纹：前缀是否复用、历史从哪一条开始变、是谁改的（SPEC-0016 §6.2）。"""
        fingerprint = prefix_fingerprint(system_text, tools)
        keys = [_message_key(msg) for msg in history]
        changed_at = _first_diff(self._last_history, keys)
        reused = bool(self._last_fingerprint) and fingerprint == self._last_fingerprint
        self._last_history = keys
        self._last_fingerprint = fingerprint
        reusable = history if changed_at < 0 else history[:changed_at]
        return {
            "prefix_fingerprint": fingerprint,
            "prefix_reused": reused,
            "history_changed_at": changed_at,
            "history_changed_by": STAGE_REASON.get(report.stage, "none"),
            "cached_prefix_tokens": prefix + count_messages(reusable),
            "estimated": True,
        }

    def _emit_trim(self, report: Any) -> None:
        """只在真的裁剪了才发帧，避免每轮刷一条空事件。"""
        if not report.changed:
            return
        self._runtime.sink.emit(
            events.frame(
                events.MEMORY_TRIM,
                layer="short",
                reason=events.REASON_REQUEST_TRIM,
                **cast(dict[str, Any], report.as_dict()),
            )
        )

    async def _note_for(self, agent: Agent, messages: list[Msg], turn: str, budget: Any) -> str:
        """同一轮内复用同一份 note；命中缓存就不再算一次召回。"""
        if turn in self._note_cache:
            return self._note_cache[turn]
        note, _meta = await self._assemble(agent, messages, turn, budget)
        self._note_cache[turn] = note
        return note

    def _emit_assembled_once(self, agent: Agent, turn: str, payload: dict[str, Any]) -> None:
        """装配视图每轮只发一次：同一轮的后续模型调用不再重复。"""
        if self._assembled_turn == turn:
            return
        self._assembled_turn = turn
        if not self._policy.context.enabled or not self._policy.context.assemble_event:
            return
        self._runtime.sink.emit(events.frame(events.CONTEXT_ASSEMBLED, **payload))

    def _audit_once(
        self,
        result: Any,
        messages: list[Msg],
        tools: list[dict[str, Any]],
        turn: str,
        prefix: int,
    ) -> Any:
        """估算对账：流式时用量只在最后一帧，包一层才能读到；只报数不改行为。"""
        if not self._policy.context.estimate_audit:
            return result
        if inspect.isasyncgen(result):
            return self._audited_stream(result, messages, tools, turn, prefix)
        self._emit_estimate_delta(result, messages, turn, prefix)
        return result

    async def _audited_stream(
        self,
        stream: AsyncIterator[Any],
        messages: list[Msg],
        tools: list[dict[str, Any]],
        turn: str,
        prefix: int,
    ) -> AsyncIterator[Any]:
        """透传模型流，在最后一帧之后对账一次估算与实测。"""
        last: Any = None
        async for chunk in stream:
            last = chunk
            yield chunk
        self._emit_estimate_delta(last, messages, turn, prefix)

    def _emit_estimate_delta(
        self, response: Any, messages: list[Msg], turn: str, prefix: int
    ) -> None:
        """把「估算 vs 实测」发成一帧，并喂给漂移修正。

        `calibration.observe` 只改本进程的估算系数；`state` 说明这次改动是
        噪声、修正，还是「换了模型族却没改配置」。
        """
        if self._audited_turn == turn:
            return
        actual = _prompt_tokens(getattr(response, "usage", None))
        if actual <= 0:
            return
        estimated = prefix + count_messages(messages, apply_drift=False)
        if estimated <= 0:
            return
        self._audited_turn = turn
        state, alpha = calibration.observe(estimated, actual)
        self._runtime.sink.emit(
            events.frame(
                events.CONTEXT_ESTIMATE_DELTA,
                estimated=estimated,
                actual=actual,
                ratio=round(actual / estimated, 3),
                state=state,
                alpha=round(alpha, 4),
                family=tokenizer().family,
            )
        )

    async def _assemble(
        self, agent: Agent, messages: list[Msg], turn: str, budget: Any
    ) -> tuple[str, dict[str, Any]]:
        """召回三层内容并装配 note。

        注入内容不写回历史，所以「版本没变」不等于「模型还记得」：每轮都要重新
        注入。此前按 `(episodic_version, long_term_hash)` 跨轮去重，第二轮起记忆卡
        就从请求里消失，模型只看到「我记得过」这件事发生过。
        去重只保留同一轮内多次模型调用复用（`_note_cache`）。
        """
        query = _last_user_text(messages)
        self._runtime.sink.emit(
            events.frame(
                events.MEMORY_RETRIEVE_START,
                layer="long",
                scope=self._policy.long_term.scope,
                backend=self._runtime.backend_name,
            )
        )
        result = await self._runtime.recall(query)
        card = result.card
        # 召回结果先与「本轮已有的历史」对账：已说过的话不再注入一遍
        hits = _drop_echoes(result.hits, _context_blob(messages))
        dropped = len(result.hits) - len(hits)
        marker = self._runtime.marker(card, hits, turn)
        note = self._runtime.note(card, hits, note_max_chars=self._runtime.note_limit(budget))
        state, reason = _retrieve_outcome(
            note=note,
            has_card=card is not None,
            kept=len(hits),
            requested=len(result.hits),
            degraded=result.degraded,
            degraded_reason=result.reason,
        )
        meta = {
            "episodic_version": marker["episodic_version"],
            "hits": len(hits),
            "ids": [hit.id for hit in hits],
            "scores": [round(hit.score, 3) for hit in hits],
            "kinds": [hit.kind for hit in hits],
            "echo_dropped": dropped,
            "injected_chars": len(note),
            "query": query[: self._policy.trace.query_preview_chars],
            "degraded": list(result.degraded),
            "errors": dict(result.errors),
            "retrieval": self._policy.long_term.retrieval,
        }
        self._runtime.sink.emit(
            events.frame(
                events.MEMORY_RETRIEVE_END,
                layer="long",
                state=state,
                reason=reason,
                **meta,
            )
        )
        return note, meta

    @staticmethod
    def _payload(
        original: list[Msg],
        messages: list[Msg],
        tools: list[dict[str, Any]],
        note: str,
        report: Any,
        budget: Any,
        cache: dict[str, Any],
    ) -> dict[str, Any]:
        """装配视图：分区 + 预算 + 缓存三段，字段名与 SPEC-0016 §6.2 一致。"""
        system_tokens = estimate_tokens(_text_of(original[0])) if original else 0
        return {
            "zones": {
                "prefix": {
                    "system_tokens": system_tokens,
                    "tools_tokens": estimate_tokens(json.dumps(tools, ensure_ascii=False)),
                },
                "history": {
                    "messages": len(messages),
                    "chars": sum(len(_text_of(msg)) for msg in messages),
                    "tokens": report.tokens_after,
                },
                "tail": {"memory_note_chars": len(note), "injected": bool(note)},
                "output_reserve": budget.output_reserve,
            },
            "budget": budget.as_dict(),
            "injected_chars": len(note),
            "dropped": report.as_dict(),
            "cache": cache,
            "estimated": True,
        }

    # --- 回合结束 -----------------------------------------------------
    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """透传回复事件；结束后写长期候选并清掉本轮 note 缓存。"""
        try:
            async for event in next_handler():
                yield event
        finally:
            # 整轮清空而不是按 reply 删：`reply_id` 可能为空，按它删会留下脏缓存
            self._note_cache.clear()
            self._assembled_turn = ""
            self._audited_turn = ""
            try:
                await self._runtime.write_turn(
                    _input_text(input_kwargs.get("inputs")), _last_assistant_text(agent)
                )
            except Exception as exc:  # noqa: BLE001 - 写入失败只影响记忆
                # 降级是设计，静默不是：写失败必须留一帧，否则记忆停摆没人看得见
                health.note_failure("turn", events.REASON_TURN_WRITE_FAILED)
                self._runtime.sink.emit(
                    events.frame(
                        events.MEMORY_WRITE_END,
                        layer="turn",
                        state="error",
                        reason=events.REASON_TURN_WRITE_FAILED,
                        exc_type=type(exc).__name__,
                        exc_msg=str(exc)[:200],
                    )
                )
