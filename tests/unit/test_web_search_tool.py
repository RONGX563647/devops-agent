"""联网检索工具：后端解析、预算、轨迹投影与装配。

这些用例覆盖 SPEC-0011 §9 的验收断言，重点在两处容易退化的地方：
1. 超限与失败**仍要产出一次 tool_result** —— 否则轨迹里看不出「模型想搜但被拦住」；
2. 工具必须真的进 toolkit —— 只测 `render` 不测装配，等于没验证「模型能调用」。
"""

from __future__ import annotations

import asyncio

import pytest

from devops_agent.openjudge.metrics import SEARCH_TOOL, search_stats
from devops_agent.openjudge.trace import ToolCallRecord, TraceRecord
from devops_agent.tools.catalog import build_toolkit_for
from devops_agent.tools.names import SEARCH_TOOL_NAME
from devops_agent.tools.web_search import (
    MAX_SEARCH_CALLS,
    UNTRUSTED_BANNER,
    SearchHit,
    WebSearchTool,
    parse_hits,
    search_status,
)


class FakeBackend:
    """可控后端：记录 query，返回预置结果或抛异常。"""

    name = "fake"

    def __init__(self, hits: list[SearchHit] | None = None, error: Exception | None = None) -> None:
        self.hits = hits or []
        self.error = error
        self.queries: list[str] = []

    async def search(self, query: str, *, max_results: int) -> list[SearchHit]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.hits


def _hit(index: int) -> SearchHit:
    return SearchHit(
        title=f"标题{index}",
        url=f"https://example.com/{index}",
        snippet=f"摘要{index}",
        fetched_at="2026-11-26",
    )


def test_parse_hits_accepts_three_shapes() -> None:
    """裸数组 / results / data 都要认：不同搜索服务的包装层不一样。"""
    rows = [{"title": "t", "url": "https://a.dev/1", "snippet": "s"}]
    assert len(parse_hits(rows)) == 1
    assert len(parse_hits({"results": rows})) == 1
    assert len(parse_hits({"data": rows})) == 1


def test_parse_hits_drops_unsafe_urls() -> None:
    """没有可引用来源的摘要不进上下文：`javascript:` 这类 URL 是脚本注入面。"""
    rows = [
        {"title": "ok", "url": "https://a.dev/1", "snippet": "s"},
        {"title": "js", "url": "javascript:alert(1)", "snippet": "s"},
        {"title": "file", "url": "file:///etc/passwd", "snippet": "s"},
        {"title": "none", "snippet": "s"},
    ]
    hits = parse_hits(rows)
    assert [hit.url for hit in hits] == ["https://a.dev/1"]


def test_render_puts_untrusted_banner_first() -> None:
    """不可信声明必须是工具输出的第一屏，不能靠 system prompt 里一句话。"""
    tool = WebSearchTool(FakeBackend([_hit(1)]))
    text = asyncio.run(tool.run("q"))
    lines = text.splitlines()
    assert lines[0].startswith("[web_search] backend=fake hits=1")
    assert lines[1] == UNTRUSTED_BANNER
    assert "[1] 标题1 — https://example.com/1" in text


def test_empty_result_is_explicit() -> None:
    """空结果要明说「搜了但没结果」，返回空串会让模型以为工具坏了。"""
    tool = WebSearchTool(FakeBackend([]))
    text = asyncio.run(tool.run("什么都没有"))
    assert "hits=0" in text
    assert "未检索到结果" in text
    assert "不要编造来源" in text


def test_budget_blocks_sixth_call_but_still_returns_text() -> None:
    """超限不抛异常：仍然产出一条 tool_result，轨迹里才看得见这次尝试。"""
    backend = FakeBackend([_hit(1)])
    tool = WebSearchTool(backend)
    for _ in range(MAX_SEARCH_CALLS):
        asyncio.run(tool.run("q"))
    text = asyncio.run(tool.run("q"))
    assert "skipped=budget-exhausted" in text
    assert "本轮检索次数已用尽" in text
    # 第 6 次没有真的发出去
    assert len(backend.queries) == MAX_SEARCH_CALLS


def test_reset_reopens_budget() -> None:
    """预算按 run 计：一轮用尽不该让整个会话后面都不能搜。"""
    tool = WebSearchTool(FakeBackend([_hit(1)]))
    for _ in range(MAX_SEARCH_CALLS):
        asyncio.run(tool.run("q"))
    tool.reset()
    text = asyncio.run(tool.run("q"))
    assert "hits=1" in text


def test_failure_is_reported_not_raised() -> None:
    """后端炸了不能打断整轮对话：检索失败是一种结果，不是异常终止。"""
    tool = WebSearchTool(FakeBackend(error=RuntimeError("boom")))
    text = asyncio.run(tool.run("q"))
    assert "note=error=RuntimeError" in text
    assert "hits=0" in text


def test_empty_query_short_circuits() -> None:
    """空 query 不发请求：省一次计费，也避免后端把它当成匹配全部。"""
    backend = FakeBackend([_hit(1)])
    text = asyncio.run(WebSearchTool(backend).run("   "))
    assert "note=empty-query" in text
    assert backend.queries == []


def test_tool_is_registered_only_when_asked() -> None:
    """装配层是「能力是否真的存在」的唯一证据：只看 render 不算验证。"""

    def names(extra: dict[str, object] | None) -> set[str]:
        toolkit = build_toolkit_for(
            (SEARCH_TOOL_NAME,),
            "/tmp",
            extra=extra,  # type: ignore[arg-type]
        )
        return {schema["function"]["name"] for schema in asyncio.run(toolkit.get_tool_schemas())}

    tool = WebSearchTool(FakeBackend([]))
    assert names({SEARCH_TOOL_NAME: tool.as_function()}) == {"web_search"}
    # 不传 extra 时必须装配失败（未知名），而不是静默少一个工具
    with pytest.raises(ValueError):
        names(None)


def test_tool_description_discourages_local_use() -> None:
    """工具描述要写「什么时候别用」：第一误用是拿它搜 grep 能解决的事。"""
    tool = WebSearchTool(FakeBackend([]))
    description = tool.as_function().description
    assert "本地代码" in description
    assert "不要用本工具" in description


def test_status_reports_reason_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有后端时必须给出原因：前端据此置灰，而不是留一个点不动的按钮。"""
    monkeypatch.delenv("DEVOPS_AGENT_SEARCH_ENDPOINT", raising=False)
    monkeypatch.delenv("DEVOPS_AGENT_BASE_URL", raising=False)
    monkeypatch.delenv("DEVOPS_AGENT_API_KEY", raising=False)
    status = search_status()
    assert status["configured"] is False
    assert "DEVOPS_AGENT_SEARCH_ENDPOINT" in status["reason"]


def test_status_prefers_http_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """配了 endpoint 就必须走 http：provider 是过渡实现，不能长期停在二次调用上。"""
    monkeypatch.setenv("DEVOPS_AGENT_SEARCH_ENDPOINT", "https://search.internal/api")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    status = search_status()
    assert status["configured"] is True
    assert status["backend"] == "http"


def test_search_stats_projects_from_tool_calls() -> None:
    """检索统计只读已有轨迹，不额外采样 —— 这是工具化最直接的收益。"""
    trace = TraceRecord(case_id="A-1")
    trace.tool_calls = [
        ToolCallRecord(
            name="web_search",
            args={"query": "python 3.12 release"},
            state="success",
            result_excerpt="[web_search] backend=http hits=3 elapsed=120ms",
        ),
        ToolCallRecord(
            name="web_search",
            args={"query": "nothing"},
            state="success",
            result_excerpt="[web_search] backend=http hits=0 elapsed=80ms",
        ),
        ToolCallRecord(
            name="read",
            args={"file_path": "app.py"},
            state="success",
            result_excerpt="...",
        ),
    ]
    stats = search_stats([trace])
    assert stats["calls"] == 2
    assert stats["empty"] == 1
    assert stats["cases"] == ["A-1"]
    assert stats["queries"] == ["python 3.12 release", "nothing"]


def test_search_tool_name_is_single_source() -> None:
    """openjudge 不 import tools，只能复制字符串；这条断言保证两边不会悄悄分叉。"""
    assert SEARCH_TOOL == SEARCH_TOOL_NAME
