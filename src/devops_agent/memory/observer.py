"""记忆中间件 —— 在每次模型调用前做短期裁剪与尾部注入，回复结束后写长期。

位置与 `ProfileNoteMiddleware` 相同：只改本次请求的 messages 副本，不写历史。
失败一律降级为事件：记忆坏了不能让一次对话失败。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, cast

from agentscope.agent import Agent
from agentscope.message import Msg, UserMsg
from agentscope.middleware import MiddlewareBase

from devops_agent.memory import events
from devops_agent.memory.budget import Budget
from devops_agent.memory.runtime import MemoryRuntime, estimate_prefix_tokens
from devops_agent.memory.short_term import trim_messages
from devops_agent.memory.tokens import clip_to_tokens, estimate_tokens

# reply_id 为空时也把同一轮内的多次模型调用归到同一个 note 缓存键
_NO_REPLY = "__no_reply__"


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
        if msg.role == "user" and msg.name not in ("system", "user"):
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


class MemoryMiddleware(MiddlewareBase):
    """短期裁剪 + 中期/长期注入 + 回合末长期写入。"""

    def __init__(self, runtime: MemoryRuntime) -> None:
        self._runtime = runtime
        self._policy = runtime.policy
        # 同一 reply 内多次模型调用复用同一份 note；回复结束即清空，不落快照
        self._note_cache: dict[str, str] = {}
        self._assembled_key = ""

    # --- 模型调用 -----------------------------------------------------
    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        """1) 算预算并确定性裁剪；2) 组装并追加记忆 note；3) 交给下一层。"""
        messages = list(input_kwargs.get("messages") or [])
        tools = list(input_kwargs.get("tools") or [])
        system_text = _text_of(messages[0]) if messages else ""
        budget = self._runtime.budget(agent, estimate_prefix_tokens(system_text, tools))
        trimmed, report = trim_messages(
            messages, self._policy.short_term, max_tokens=budget.short_term
        )
        if report.changed:
            self._runtime.sink.emit(
                events.frame(
                    events.MEMORY_TRIM,
                    layer="short",
                    reason="request_trim",
                    **cast(dict[str, Any], report.as_dict()),
                )
            )
        reply = str(getattr(agent.state, "reply_id", "") or "")
        cache_key = reply or _NO_REPLY
        if cache_key in self._note_cache:
            note = self._note_cache[cache_key]
        else:
            note = await self._assemble(agent, trimmed, reply, budget)
            self._note_cache[cache_key] = note
        if note:
            trimmed.append(UserMsg(name="system", content=note))
        if cache_key != self._assembled_key:
            self._assembled_key = cache_key
            self._emit_assembled(trimmed, tools, note, report.as_dict(), budget)
        return await next_handler(messages=trimmed)

    async def _assemble(self, agent: Agent, messages: list[Msg], reply: str, budget: Budget) -> str:
        """召回三层内容并按预算装配 note。

        注入内容不写回历史，所以「版本没变」不等于「模型还记得」：每轮都要重新注入，
        跨轮去重会让记忆卡从后续请求里消失。去重只发生在同一 reply 的多次模型调用内
        （由调用方的 `_note_cache` 负责）。
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
        card, hits = await self._runtime.recall(query)
        marker = self._runtime.marker(card, hits, reply)
        self._remember(agent, marker)
        note = ""
        if budget.history_max <= 0:
            state, reason = "skipped", "budget_exhausted"
        else:
            raw = self._runtime.note(card, hits)
            note = clip_to_tokens(raw, budget.episodic + budget.long_term)
            if note:
                state, reason = "hit", ""
            elif raw:
                state, reason = "skipped", "budget_exhausted"
            else:
                state, reason = "miss", "no_memory"
        self._runtime.sink.emit(
            events.frame(
                events.MEMORY_RETRIEVE_END,
                layer="long",
                state=state,
                reason=reason,
                episodic_version=marker["episodic_version"],
                hits=len(hits),
                ids=[hit.id for hit in hits],
                scores=[round(hit.score, 3) for hit in hits],
                injected_chars=len(note),
                query=query[: self._policy.trace.query_preview_chars],
            )
        )
        return note

    @staticmethod
    def _remember(agent: Agent, marker: dict[str, Any]) -> None:
        """把版本/命中标记写回 middle_context；只存短标记，不存 note 全文。"""
        middle = getattr(agent.state, "middle_context", {}) or {}
        middle["memory"] = marker
        agent.state.middle_context = middle

    def _emit_assembled(
        self,
        messages: list[Msg],
        tools: list[dict[str, Any]],
        note: str,
        dropped: dict[str, int],
        budget: Budget,
    ) -> None:
        """第 0 步的分区视图；Echo/关闭时不发。预算复用 on_model_call 算好的那份。"""
        if not self._policy.context.enabled or not self._policy.context.assemble_event:
            return
        system_text = _text_of(messages[0]) if messages else ""
        tools_text = json.dumps(tools, ensure_ascii=False)
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
            reply = str(getattr(agent.state, "reply_id", "") or "")
            self._note_cache.pop(reply or _NO_REPLY, None)
            self._note_cache.pop(_NO_REPLY, None)
            self._assembled_key = ""
            try:
                await self._runtime.write_turn(
                    _input_text(input_kwargs.get("inputs")), _last_assistant_text(agent)
                )
            except Exception:  # noqa: BLE001 - 写入失败只影响记忆
                pass
