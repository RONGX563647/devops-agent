"""上下文 API —— 这一轮对话真正装了哪些东西，以及这份前缀稳不稳。

轨迹里的「第 0 步」要回答的问题不是「模型说了什么」，而是「模型动手前手里有什么」：
系统提示词、可用工具 schema、用户这条输入。三者都来自装配区，所以放在 tools 包里
和 schema 一起暴露；前端只负责展示，不自己拼一份假的上下文。

两条硬约束：

1. **按会话档位返回**。放开档实际挂了 Write/Edit，写死只读四件套会让这张表变成错的。
2. **给出体积与指纹**。工具 schema 的 JSON 有一万字符，是系统提示的二十多倍；
   只说「工具 4 个」等于把上下文的大头藏起来。指纹用来判断前缀有没有变——
   变了，这一枪必然从缓存里读不到。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter

from devops_agent.config import load_memory, load_model
from devops_agent.governance.model import PermissionProfile
from devops_agent.governance.presets import UnknownProfile, default_profile, resolve
from devops_agent.memory.budget import compute_budget
from devops_agent.memory.tokens import estimate_tokens
from devops_agent.observability import repository as repo
from devops_agent.runtime.profile_note import policy_note
from devops_agent.runtime.prompt import build_system_prompt, resolve_workdir
from devops_agent.tools.schema import tool_schemas

router = APIRouter(prefix="/api", tags=["context"])
logger = logging.getLogger(__name__)


def _session_profile_key(session: str | None) -> str:
    """从会话快照里取上次使用的档位；读不到就返回空串（走默认档）。"""
    if not session:
        return ""
    try:
        with repo.connect() as conn:
            repo.init_db(conn)
            saved = repo.load_session_state(conn, session)
    except Exception:  # noqa: BLE001 - 存储故障不该让上下文接口整体失败
        logger.warning("读取会话档位失败: %s", session, exc_info=True)
        return ""
    return saved[1] if saved else ""


def _profile_for(session: str | None, profile: str | None) -> PermissionProfile:
    """确定要用哪个档位：显式参数 > 会话快照 > 默认档。"""
    try:
        return resolve(profile or _session_profile_key(session) or None)
    except UnknownProfile:
        return default_profile()


def _fingerprint(prompt: str, schemas: list[dict[str, Any]]) -> str:
    """前缀指纹：系统提示 + 工具 schema 的稳定哈希，取前 8 位够人眼比较。

    `sort_keys=True` 与真实下发无关（框架按注册顺序序列化），这里只是保证
    同一份内容算出同一个指纹；顺序本身由档位里的 tuple 钉住。
    """
    body = prompt + json.dumps(schemas, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _tool_rows(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 OpenAI 形状的 schema 摊平成一行一个工具，带体积与完整参数定义。"""
    rows: list[dict[str, Any]] = []
    for item in schemas:
        fn = item.get("function", {}) if isinstance(item, dict) else {}
        raw = json.dumps(item, ensure_ascii=False)
        rows.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "schema_chars": len(raw),
                "tokens": estimate_tokens(raw),
                "parameters": fn.get("parameters", {}),
            }
        )
    return rows


def _session_facts(session: str | None) -> dict[str, Any]:
    """快照与记忆的只读事实；读不到时返回零值，不让上下文接口整体失败。"""
    policy = load_memory()
    snapshot: dict[str, Any] = {
        "version": policy.context.snapshot_version,
        "chars": 0,
        "messages": 0,
        "summary_chars": 0,
        "trimmed": False,
    }
    memory: dict[str, Any] = {
        "episodic_version": 0,
        "long_term_entries": 0,
        "backend": "off",
    }
    if not session:
        return {"snapshot": snapshot, "memory": memory}
    try:
        with repo.connect() as conn:
            repo.init_db(conn)
            saved = repo.load_session_state(conn, session)
            active = repo.active_memory(conn, session)
            scope_id = policy.user_id or f"session:{session}"
            memory["long_term_entries"] = repo.count_long_term(
                conn, policy.long_term.scope, scope_id
            )
    except Exception:  # noqa: BLE001 - 存储故障只让这一块显示为零值
        logger.warning("读取上下文事实失败: %s", session, exc_info=True)
        return {"snapshot": snapshot, "memory": memory}
    if active:
        memory["episodic_version"] = int(active["version"])
    if saved:
        _read_snapshot(str(saved[0] or ""), snapshot, memory)
    return {"snapshot": snapshot, "memory": memory}


def _read_snapshot(state_json: str, snapshot: dict[str, Any], memory: dict[str, Any]) -> None:
    """解析快照的版本、消息数与注入标记；坏 JSON 只记录体积。"""
    snapshot["chars"] = len(state_json)
    if not state_json:
        return
    try:
        parsed = json.loads(state_json)
    except ValueError:
        return
    middle = parsed.get("middle_context") or {}
    snapshot["version"] = int(middle.get("snapshot_version", 1))
    snapshot["messages"] = len(parsed.get("context") or [])
    snapshot["summary_chars"] = len(str(parsed.get("summary") or ""))
    memory["episodic_version"] = int(
        (middle.get("memory") or {}).get("episodic_version", memory["episodic_version"])
    )


@router.get("/context")
async def get_context(
    session: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """返回系统提示词原文、按档位装配的工具 schema 与前缀指纹。"""
    resolved = _profile_for(session, profile)
    schemas = await tool_schemas(resolved.tools)
    tools = _tool_rows(schemas)
    prompt = build_system_prompt(resolve_workdir())
    note = policy_note(resolved)
    tools_chars = sum(tool["schema_chars"] for tool in tools)
    prompt_tokens = estimate_tokens(prompt)
    tools_tokens = sum(tool["tokens"] for tool in tools)
    policy = load_memory()
    settings = load_model()
    budget = compute_budget(
        context_size=settings.context_size,
        trigger_ratio=settings.trigger_ratio,
        reserve_ratio=settings.reserve_ratio,
        max_tokens=settings.max_tokens,
        prefix_tokens=prompt_tokens + tools_tokens,
        short_term_ratio=policy.budget.short_term_ratio,
        episodic_ratio=policy.budget.episodic_ratio,
        long_term_ratio=policy.budget.long_term_ratio,
        headroom_ratio=policy.budget.headroom_ratio,
        episodic_enabled=policy.episodic.enabled,
        long_term_enabled=policy.long_term.enabled,
    )
    facts = _session_facts(session)
    return {
        "profile": resolved.key,
        "profile_label": resolved.label,
        "system_prompt": prompt,
        "system_prompt_chars": len(prompt),
        "system_prompt_tokens": prompt_tokens,
        "tools": tools,
        "tools_chars": tools_chars,
        "tools_tokens": tools_tokens,
        # 前缀 = 系统提示 + 工具 schema：这两段稳定，后面的历史才可能命中缓存
        "prefix_tokens": prompt_tokens + tools_tokens,
        # 档位约束不占前缀：它跟着每次请求追加在消息末尾，切档不会让历史失效
        "policy_note": note,
        "policy_note_tokens": estimate_tokens(note),
        "prefix_fingerprint": _fingerprint(prompt, schemas),
        "workdir": str(resolve_workdir()),
        # 第 0 步：分区、预算与记忆事实（SPEC-0013 §5.9）
        "zones": {
            "prefix": {
                "system_tokens": prompt_tokens,
                "tools_tokens": tools_tokens,
                "system_fingerprint": _short_hash(prompt),
                "tools_fingerprint": _short_hash(
                    json.dumps(schemas, ensure_ascii=False, sort_keys=True)
                ),
            },
            "history": {
                "messages": facts["snapshot"]["messages"],
                "summary_chars": facts["snapshot"]["summary_chars"],
            },
            "tail": {"policy_note_tokens": estimate_tokens(note)},
            "output_reserve": budget.output_reserve,
        },
        "budget": budget.as_dict(),
        "memory": facts["memory"],
        "snapshot": facts["snapshot"],
    }
