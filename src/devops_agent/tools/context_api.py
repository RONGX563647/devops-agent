"""上下文 API —— 这一轮对话真正装了哪些东西，以及这份前缀稳不稳。

轨迹里的「第 0 步」要回答的问题不是「模型说了什么」，而是「模型动手前手里有什么」：
系统提示词、可用工具 schema、用户这条输入。三者都来自装配区，所以放在 tools 包里
和 schema 一起暴露；前端只负责展示，不自己拼一份假的上下文。

两条硬约束：

1. **工具清单与档位无关**。所有档位装配同一个 Toolkit，能不能执行由权限引擎判定；
   按档位裁剪 schema 会让同一份前缀在不同档位下字节不一致，历史缓存直接失效。
2. **给出体积与指纹**。工具 schema 的 JSON 有一万字符，是系统提示的二十多倍；
   只说「工具 12 个」等于把上下文的大头藏起来。指纹用来判断前缀有没有变——
   变了，这一枪必然从缓存里读不到。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter

from devops_agent.governance.profiles import (
    PermissionProfile,
    UnknownProfile,
    default_profile,
    resolve,
)
from devops_agent.observability import repository as repo
from devops_agent.runtime.agent import build_system_prompt, resolve_workdir
from devops_agent.runtime.profile_note import policy_note
from devops_agent.tools.schema import tool_schemas

router = APIRouter(prefix="/api", tags=["context"])
logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """粗略估 token：中文约 1.4 字/token，ASCII/JSON 约 3.6 字符/token。

    只是给界面一个量级（正确口径看模型回的 input_tokens），所以刻意不做
    分词器依赖——多一个 tokenizer 依赖换不来这一位小数的精度。
    系数按实测校准：409 字系统提示 + 10139 字符工具 schema ≈ 3060 token。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk / 1.4 + (len(text) - cjk) / 3.6)


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
    同一份内容算出同一个指纹；顺序本身由 catalog 的注册顺序钉住。
    """
    body = prompt + json.dumps(schemas, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


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


@router.get("/context")
async def get_context(
    session: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """返回系统提示词原文、全量工具 schema 与前缀指纹。"""
    resolved = _profile_for(session, profile)
    schemas = await tool_schemas()
    tools = _tool_rows(schemas)
    prompt = build_system_prompt(resolve_workdir())
    note = policy_note(resolved)
    tools_chars = sum(tool["schema_chars"] for tool in tools)
    prompt_tokens = estimate_tokens(prompt)
    tools_tokens = sum(tool["tokens"] for tool in tools)
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
    }
