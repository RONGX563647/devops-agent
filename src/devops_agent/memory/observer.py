"""记忆中间件 —— 在每次模型调用前做短期裁剪与尾部注入，回复结束后写长期。

位置与 `ProfileNoteMiddleware` 相同：只改本次请求的 messages 副本，不写历史。
失败一律降级为事件：记忆坏了不能让一次对话失败。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, cast

from agentscope.agent import Agent
from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase

from devops_agent.memory import events, health
from devops_agent.memory.runtime import MemoryRuntime, estimate_prefix_tokens
from devops_agent.memory.short_term import trim_messages
from devops_agent.memory.tokens import estimate_tokens
from devops_agent.memory.turns import injected_note, is_user_turn


def _text_of(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _last_user_text(messages: list[Msg]) -> str:
    for msg in reversed(messages):
        if is_user_turn(msg):
            return _text_of(msg)
    return ""


def _last_assistant_text(agent: Agent) -> str:
    for msg in reversed(getattr(agent.state, "context", []) or []):
        if getattr(msg, "role", "") == "assistant":
            return _text_of(msg)
    return ""


def _input_text(inputs: Any) -> str:
    if inputs is None:
        return ""
    if isinstance(inputs, list):
        return _text_of(inputs[-1]) if inputs else ""
    return _text_of(inputs)


def _turn_key(reply: str, messages: list[Msg]) -> str:
    """本轮的稳定标识：`reply_id` 优先，为空时回落到首条消息的摘要。

    不能直接用 `reply_id` 当缓存键：框架在某些路径下不填它，空串会让
    「同一轮内的多次模型调用」与「不同轮」共用同一个键 —— 前者重复召回，
    后者被误判成同一轮而漏注入（SPEC-0013 §18.2 M10）。
    首条消息（系统提示）在同一轮里不变，跨轮也不变，所以只作为
    「这一轮是谁」的弱标识：同轮去重靠它，跨轮是否重注入仍由版本/命中集合判断。
    """
    if reply:
        return reply
    head = _text_of(messages[0]) if messages else ""
    return "h:" + hashlib.sha256(head.encode("utf-8")).hexdigest()[:12]


class MemoryMiddleware(MiddlewareBase):
    """短期裁剪 + 中期/长期注入 + 回合末长期写入。"""

    def __init__(self, runtime: MemoryRuntime) -> None:
        self._runtime = runtime
        self._policy = runtime.policy
        # 同一轮内多次模型调用复用同一份 note；回复结束即清空，不落快照
        self._note_cache: dict[str, str] = {}
        self._assembled_turn = ""

    # --- 模型调用 -----------------------------------------------------
    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        """1) 确定性裁剪；2) 组装并追加记忆 note；3) 交给下一层。"""
        messages = list(input_kwargs.get("messages") or [])
        trimmed, report = trim_messages(messages, self._policy.short_term)
        if report.changed:
            self._runtime.sink.emit(
                events.frame(
                    events.MEMORY_TRIM,
                    layer="short",
                    reason="request_trim",
                    **cast(dict[str, Any], report.as_dict()),
                )
            )
        turn = _turn_key(str(getattr(agent.state, "reply_id", "") or ""), trimmed)
        if turn in self._note_cache:
            note = self._note_cache[turn]
        else:
            note, _meta = await self._assemble(agent, trimmed, turn)
            self._note_cache[turn] = note
        if note:
            trimmed.append(injected_note(note, kind="memory"))
        if self._assembled_turn != turn:
            self._assembled_turn = turn
            tools = list(input_kwargs.get("tools") or [])
            self._emit_assembled(agent, trimmed, tools, note, report.as_dict())
        return await next_handler(messages=trimmed)

    async def _assemble(
        self, agent: Agent, messages: list[Msg], turn: str
    ) -> tuple[str, dict[str, Any]]:
        """召回三层内容并决定这一轮是否真的注入（版本未变则跳过）。"""
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
        card, hits = result.card, result.hits
        marker = self._runtime.marker(card, hits, turn)
        unchanged = self._unchanged(agent, marker, turn)
        note = "" if unchanged else self._runtime.note(card, hits)
        self._remember(agent, marker)
        # 降级优先于 hit/miss：后端挂了却显示「没命中」，等于把故障伪装成正常
        if result.degraded:
            state, reason = "error", result.reason
        elif unchanged:
            state, reason = "skipped", events.REASON_UNCHANGED
        else:
            state, reason = ("hit" if note else "miss"), ("" if note else events.REASON_NO_MEMORY)
        meta = {
            "episodic_version": marker["episodic_version"],
            "hits": len(hits),
            "ids": [hit.id for hit in hits],
            "scores": [round(hit.score, 3) for hit in hits],
            "injected_chars": len(note),
            "query": query[: self._policy.trace.query_preview_chars],
            "degraded": list(result.degraded),
            "errors": dict(result.errors),
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
    def _unchanged(agent: Agent, marker: dict[str, Any], turn: str) -> bool:
        """同一版本、同一批命中且不是同一轮：视为已经注入过，不再重复。"""
        middle = getattr(agent.state, "middle_context", {}) or {}
        previous = middle.get("memory") or {}
        # 老快照只有 reply_id：读不到 turn_key 时回落到它，避免升级后重复注入一轮
        previous_turn = str(previous.get("turn_key", previous.get("reply_id", "")) or "")
        return (
            previous.get("episodic_version") == marker["episodic_version"]
            and previous.get("long_term_hash") == marker["long_term_hash"]
            and previous_turn != turn
        )

    @staticmethod
    def _remember(agent: Agent, marker: dict[str, Any]) -> None:
        """把版本/命中标记写回 middle_context；只存短标记，不存 note 全文。"""
        middle = getattr(agent.state, "middle_context", {}) or {}
        middle["memory"] = marker
        agent.state.middle_context = middle

    def _emit_assembled(
        self,
        agent: Agent,
        messages: list[Msg],
        tools: list[dict[str, Any]],
        note: str,
        dropped: dict[str, int],
    ) -> None:
        """第 0 步的分区视图；Echo/关闭时不发。"""
        if not self._policy.context.enabled or not self._policy.context.assemble_event:
            return
        system_text = _text_of(messages[0]) if messages else ""
        tools_text = json.dumps(tools, ensure_ascii=False)
        budget = self._runtime.budget(agent, estimate_prefix_tokens(system_text, tools))
        payload = {
            "zones": {
                "prefix": {
                    "system_tokens": estimate_tokens(system_text),
                    "tools_tokens": estimate_tokens(tools_text),
                },
                "history": {
                    "messages": len(messages),
                    "chars": sum(len(_text_of(msg)) for msg in messages),
                },
                "tail": {"memory_note_chars": len(note), "injected": bool(note)},
                "output_reserve": budget.output_reserve,
            },
            "budget": budget.as_dict(),
            "injected_chars": len(note),
            "dropped": dropped,
            "estimated": True,
        }
        self._runtime.sink.emit(
            events.frame(events.CONTEXT_ASSEMBLED, **cast(dict[str, Any], payload))
        )

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
