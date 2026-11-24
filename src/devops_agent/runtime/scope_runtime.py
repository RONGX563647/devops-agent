"""真实运行时：把 AgentScope 的 Agent 适配到 AgentRuntime 协议。

事件对象只在本模块内存在，越过边界的只有普通 dict —— 这是框架隔离约束
在流式路径上的落点。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agentscope.agent import Agent
from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionRule
from agentscope.tool import Toolkit, ToolMiddlewareBase

from devops_agent.governance.policy import build_permission_context
from devops_agent.governance.profiles import PermissionProfile, default_profile
from devops_agent.model_settings import ModelParams
from devops_agent.runtime.chat_model import MODEL_WIRE_KEYS, build_chat_model
from devops_agent.runtime.compression import CompressionSink, context_budget
from devops_agent.runtime.confirm import (
    ConfirmDecision,
    effective_scope,
    pending_call,
    rule_content_for,
)
from devops_agent.runtime.events import event_to_dict
from devops_agent.runtime.profile_note import ProfileNoteMiddleware
from devops_agent.runtime.prompt import resolve_workdir
from devops_agent.tools.catalog import build_toolkit_for

# 快照里记录「这份状态是按哪个档位装配的」的键。
# 必须用档位 key 而不是 permission_context.mode 判断：workspace 与 execute
# 的 mode 都是 accept_edits，只比 mode 时切档等于什么都没变（白名单永不装配），
# 反向切回来又会把上一档点「本会话允许」攒下的规则带进新档。
PROFILE_KEY = "permission_profile"


def _copy_permission_context(target: PermissionContext, fresh: PermissionContext) -> None:
    """把新档位的判定字段逐项写回旧对象（不整体替换）。

    Agent 构造时 `PermissionEngine(self.state.permission_context)` 持有的是
    对象引用；换对象后引擎仍按旧档判定，界面却显示新档。旧档攒下的
    allow_rules 随赋值丢弃，与「切档即换信任级别」一致。
    """
    target.mode = fresh.mode
    target.working_directories = fresh.working_directories
    target.allow_rules = fresh.allow_rules
    target.deny_rules = fresh.deny_rules
    target.ask_rules = fresh.ask_rules


class AgentScopeRuntime:
    """真实 Agent 运行时：把 Agent.reply_stream 适配到 AgentRuntime 协议。"""

    def __init__(
        self,
        agent: Agent,
        *,
        profile: PermissionProfile | None = None,
        workdir: Path | None = None,
        note_middleware: ProfileNoteMiddleware | None = None,
        toolkit_middlewares: Sequence[ToolMiddlewareBase] | None = None,
        compression_sink: CompressionSink | None = None,
        web_search: bool = False,
        params: ModelParams | None = None,
    ) -> None:
        self._agent = agent
        self._profile = profile if profile is not None else default_profile()
        self._workdir = workdir if workdir is not None else resolve_workdir()
        # 热替换要保住这两件装配期的依赖：换 toolkit 时密钥注入不能丢，
        # 换档位时末尾的档位说明必须同步改，否则「切了但模型还按旧档写」。
        self._note_middleware = note_middleware
        self._toolkit_middlewares = list(toolkit_middlewares or [])
        # 压缩钩子没有事件通道，只能由运行时把中间件攒下的帧 drain 进流里。
        # Echo/测试替身没有 sink 时为 None，事件序列保持原样。
        self._compression_sink = compression_sink
        self._web_search = web_search
        # 当前生效的模型参数：热替换与 run 记录都以它为准，不从 agent 上反推
        # （反推要先猜「哪个字段对应哪个参数」，猜错就是静默错报）。
        self._params = params

    @property
    def profile(self) -> PermissionProfile:
        return self._profile

    @property
    def web_search(self) -> bool:
        """这一轮到底联不联网（已解析部署默认）。

        会话注册表要靠它记录「实际生效值」：请求里省略该字段时不能记成 False，
        否则下一轮显式传 False 与「沿用默认」会被当成同一个配置，开关切换失效。
        """
        return self._web_search

    def apply_profile(
        self,
        profile: PermissionProfile,
        *,
        web_search: bool | None = None,
    ) -> None:
        """会话中切档：原地热替换，当前流不中断，下一次判定用新档位。

        **不能整体替换 state.permission_context**：Agent 构造时
        `self._engine = PermissionEngine(self.state.permission_context)` 持有的是
        对象引用；换对象后引擎仍按旧档判定，界面却显示新档。只能逐字段写回
        （见 `_copy_permission_context`）。

        旧档攒下的 allow_rules 随这次赋值丢弃，与「切档即换信任级别」一致。
        toolkit 整体换（Agent 每次调用现取 schema），但必须重挂密钥中间件；
        模型只在联网开关真的变化时重建，避免无谓丢掉前缀缓存。
        """
        # 1) 权限上下文逐字段写回：引擎持有的是旧对象的引用
        fresh = build_permission_context(profile=profile, workdir=self._workdir)
        _copy_permission_context(self._agent.state.permission_context, fresh)
        # 2) 工具按新档位重装，密钥注入中间件与档位说明同步更新
        self._agent.toolkit = self._rebuild_toolkit(profile)
        if self._note_middleware is not None:
            self._note_middleware.set_profile(profile)
        # 3) 联网开关真的变化时才重建模型（带上会话参数）
        self._maybe_rebuild_model(web_search)
        # 4) 快照记下本轮按哪个档装配，运行时档位同步
        self._agent.state.middle_context[PROFILE_KEY] = profile.key
        self._profile = profile

    def _rebuild_toolkit(self, profile: PermissionProfile) -> Toolkit:
        """按档位重装工具；密钥注入中间件必须一并重挂，否则工具边界不再脱敏。"""
        return build_toolkit_for(
            profile.tools,
            self._workdir,
            middlewares=self._toolkit_middlewares or None,
        )

    def _maybe_rebuild_model(self, web_search: bool | None) -> None:
        """只在联网开关真的变化时重建模型，避免无谓丢掉前缀缓存。

        重建必须带上会话级参数：省略 params 会回落到部署默认，用户面板里设的
        temperature / context_size / max_iters 会被这个开关静默重置。
        开关换了末尾说明也要跟着换，否则检索结果已在上下文里，模型却仍按
        「没有联网能力」作答。
        """
        if web_search is None or web_search == self._web_search:
            return
        self._agent.model = build_chat_model(enable_search=web_search, params=self._params)
        self._web_search = web_search
        if self._note_middleware is not None:
            self._note_middleware.set_web_search(web_search)

    # --- 模型参数热替换 -------------------------------------------------
    def _model_changed(self, params: ModelParams) -> bool:
        """模型对象是否需要重建：只有决定「怎么发请求」的那几个键算数。"""
        current = self._params
        if current is None:
            return True
        return any(getattr(current, key) != getattr(params, key) for key in MODEL_WIRE_KEYS)

    def apply_model_params(self, params: ModelParams) -> None:
        """会话中改模型参数：原地热替换，不丢对话历史、不打断当前流。

        只有模型侧参数（名字 / 采样 / 窗口 / HTTP 重试）变了才重建模型对象 ——
        重建会丢掉已建立的连接与重试计数，为此白付一次代价不值得。
        压缩比例与迭代上限直接写回配置对象，下一轮推理前就会用上。
        """
        if self._model_changed(params):
            self._agent.model = build_chat_model(enable_search=self._web_search, params=params)
        self._agent.context_config = self._agent.context_config.model_copy(
            update={
                "trigger_ratio": params.trigger_ratio,
                "reserve_ratio": params.reserve_ratio,
                "context_buffer_ratio": params.context_buffer_ratio,
            }
        )
        self._agent.react_config.max_iters = params.max_iters
        self._agent.model_config.max_retries = params.agent_max_retries
        self._params = params

    def model_params_record(self) -> dict[str, Any]:
        """本轮实际下发的参数快照（只含非默认值），写进 runs.model_params。"""
        return self._params.record() if self._params is not None else {}

    @staticmethod
    def _make_msg(session_id: str, user_input: str) -> Msg:
        return Msg(
            name=session_id,
            content=[TextBlock(type="text", text=user_input, id="input", created_at="")],
            role="user",
        )

    def _drain_compression(self) -> list[dict[str, Any]]:
        """取走压缩钩子攒下的帧；没有 sink 的运行时返回空列表。"""
        return self._compression_sink.drain() if self._compression_sink is not None else []

    def _budget_frame(self) -> dict[str, Any] | None:
        """上下文预算帧：只在挂了压缩钩子的真实运行时发。

        阈值由框架配置算出（trigger_ratio × context_size），前端拿它画占用率；
        Echo/测试替身没有模型窗口概念，发出去只会是假的 128000。
        """
        return context_budget(self._agent) if self._compression_sink is not None else None

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        """消费 AgentScope reply_stream，逐个转成框架无关的 dict 往外吐。

        事件对象只在本模块内存在，越过边界的只有普通 dict：
        这是框架隔离约束在流式路径上的延续。
        压缩帧没有框架事件可依附，在每个框架事件之后按顺序补发。
        """
        budget = self._budget_frame()
        if budget is not None:
            yield budget
        async for event in self._agent.reply_stream(self._make_msg(session_id, user_input)):
            yield event_to_dict(event)
            for frame in self._drain_compression():
                yield frame

    # --- 挂起与恢复 ---------------------------------------------------
    def snapshot(self) -> str:
        """把 AgentState 序列化成 JSON：会话隔离、刷新恢复、跨实例续跑都靠它。"""
        return json.dumps(
            self._agent.state.model_dump(mode="json"), ensure_ascii=False, default=str
        )

    def awaiting_blocks(self) -> list[ToolCallBlock]:
        """当前仍等待用户确认的 tool_call（权威副本，含原始 input）。"""
        return list(self._agent.state.get_awaiting_tool_calls(self._agent.name))

    def pending_calls(self) -> list[dict[str, Any]]:
        """给前端展示的精简数组（不含可回传的完整块）。"""
        return [
            asdict(pending_call(block.model_dump(mode="json"))) for block in self.awaiting_blocks()
        ]

    @staticmethod
    def _rules_for(decision: ConfirmDecision, block: ToolCallBlock) -> list[PermissionRule] | None:
        """按作用域生成要沉淀的规则；高危命令强制降级为「只这一次」。"""
        if not decision.confirmed:
            return None
        scope = effective_scope(block.name, block.input, decision.scope)
        content = rule_content_for(scope, block.name, block.input)
        if content is None:
            return None
        return [
            PermissionRule(
                tool_name=block.name,
                rule_content=content,
                behavior=PermissionBehavior.ALLOW,
                source="user",
            )
        ]

    def confirm_event(self, decisions: Sequence[ConfirmDecision]) -> UserConfirmResultEvent:
        """把前端的决策组装成框架事件。

        命中的 tool_call 直接从挂起状态里取：客户端只给了 id，
        命令原文与工具名都以服务端这份为准，改不了。
        """
        blocks = {block.id: block for block in self.awaiting_blocks()}
        results = [
            ConfirmResult(
                confirmed=decision.confirmed,
                tool_call=blocks[decision.tool_call_id],
                rules=self._rules_for(decision, blocks[decision.tool_call_id]),
            )
            for decision in decisions
            if decision.tool_call_id in blocks
        ]
        if not results:
            raise ValueError("没有匹配的待确认调用")
        # 框架 2.0.10 移除了 `AgentState.get_reply_message()`，改为直接读 `reply_id`。
        reply_id = getattr(self._agent.state, "reply_id", "") or ""
        return UserConfirmResultEvent(
            reply_id=reply_id,
            confirm_results=results,
        )

    async def resume(self, decisions: Sequence[ConfirmDecision]) -> AsyncIterator[dict[str, Any]]:
        """带着用户决策从挂起点继续，照常往外吐事件 dict。

        不重发 CONTEXT_BUDGET：同一会话的窗口与阈值不变，前端已持有。
        但压缩仍可能在续跑时触发，所以照常 drain。
        """
        async for event in self._agent.reply_stream(self.confirm_event(decisions)):
            yield event_to_dict(event)
            for frame in self._drain_compression():
                yield frame
