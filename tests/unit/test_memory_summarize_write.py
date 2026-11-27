"""长期写入的摘要化与去重（SPEC-0016 §8 / §10 A32–A33）。

长期层存的是**结论**，不是回合原文：原话里塞满寒暄与过程话术，会把检索的词面
信号稀释掉；而「同一件事换个说法」在原文去重下永远算两条，条目只增不减。

这一组钉三件事：该写的写进来、不该写的别进来、写进来的要短且不重复。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from devops_agent.config import load_memory
from devops_agent.memory import events
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink
from devops_agent.memory.summarize import kind_of, similar, summarize
from devops_agent.observability import repository as repo

MAX_CHARS = load_memory().long_term.summary_max_chars
#: `MemoryRuntime` 的 scope_id 规则：有 user_id 用 user_id，否则 `session:<id>`
SCOPE_ID = "session:s1"


def _runtime(tmp_path: Path) -> MemoryRuntime:
    return MemoryRuntime("s1", load_memory(), MemorySink(), tmp_path / "obs.db")


def _rows(tmp_path: Path, scope: str = "session") -> list[dict[str, Any]]:
    with repo.connect(tmp_path / "obs.db") as conn:
        repo.init_db(conn)
        return repo.list_long_term(conn, scope=scope, scope_id=SCOPE_ID)


def _write(report: MemoryRuntime, user_text: str, assistant_text: str = "") -> dict[str, Any]:
    return asyncio.run(report.write_turn(user_text, assistant_text))


def test_greeting_is_not_stored(tmp_path: Path) -> None:
    """A32 之一：寒暄不写 —— 写进去的寒暄会在后面每一轮被召回。"""
    result = _write(_runtime(tmp_path), "你好", "你好，有什么可以帮你？")
    assert result["skipped"] is True
    assert result["reason"] == "courtesy"
    assert _rows(tmp_path) == []


def test_short_sentence_with_path_is_stored(tmp_path: Path) -> None:
    """A32 之二：短句但带路径/命令 —— 短不等于没信息，别按长度一刀切。"""
    result = _write(_runtime(tmp_path), "入口在哪 scripts/start_pay.sh", "app.py:120，先跑迁移。")
    assert result["stored"] is True
    text = _rows(tmp_path)[0]["text"]
    assert "start_pay.sh" in text or "app.py" in text


def test_long_turn_is_summarized_within_limit(tmp_path: Path) -> None:
    """A32 之三：1000 字原文 → 条目 ≤240 字，且留下的是结论句而不是开头寒暄。"""
    filler = "先看了一下日志，又问了同事，中间还试了几次重启，过程就跳过了。"
    user_text = "服务早上开始内存一直涨。" + filler * 20 + "这个先记一下。"
    answer = (
        filler * 10
        + "结论：OOM 根因是 pandas 一次读全量 CSV，改成 chunksize=10000 后稳定。"
        + filler * 10
    )
    assert len(user_text) + len(answer) >= 1000

    result = _write(_runtime(tmp_path), user_text, answer)
    assert result["stored"] is True
    text = _rows(tmp_path)[0]["text"]
    assert len(text) <= MAX_CHARS
    assert "chunksize" in text  # 抽到了中段的结论句，不是原文前缀


def test_synonym_rewrite_merges_into_one_row(tmp_path: Path) -> None:
    """A33：同义改写（3-gram ≥0.8）只留一行，`hits=2` 说明「又遇到一次」。"""
    first = "结论：OOM 根因是 pandas 一次读全量 CSV，改成 chunksize=10000 后内存稳定在 800MB。"
    second = first[:-1] + "上下。"
    assert similar(first, second)  # 夹具前提：确实判为同一条

    report = _runtime(tmp_path)
    _write(report, first)
    result = _write(report, second)

    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert result["deduped"] is True
    assert rows[0]["hits"] == 2
    assert len(rows[0]["text"]) >= len(first)  # 保留更长的那份，信息不减


def test_summarize_keeps_signal_sentence_and_drops_lead_pronoun(tmp_path: Path) -> None:
    """§8.2：路径/命令句优先，抽出来的句子去掉句首代词（离开上下文也能读）。"""
    raw = "这个先这样吧。那么该配置在 config/memory.yaml 里，改完重启。另外那个事情后面再说。"
    body, degraded = summarize(raw, max_chars=MAX_CHARS)
    assert degraded is False
    assert "config/memory.yaml" in body
    assert not body.startswith("那么")


def test_summarize_falls_back_on_text_without_sentences() -> None:
    """整段没有句读：抽句失效、只能硬截断 —— 必须标降级，不能假装摘好了。"""
    body, degraded = summarize("没有句号的超长描述" * 30, max_chars=40)
    assert degraded is True
    assert len(body) <= 40


def test_kind_of_classifies_by_signal() -> None:
    """§8.3：约定/禁止 → decision；多命令或编号步骤 → procedure；其余 fact。"""
    assert kind_of("日志脱敏默认开启，不许写进长期记忆。") == "decision"
    assert kind_of("1. 先 build 镜像 2. 再 kubectl apply") == "procedure"
    assert kind_of("先 make test 再 ruff check") == "procedure"
    # 带数字/缩写但不是命令：不能被误分类成 procedure（会白拿 1.1 倍权重）
    assert kind_of("网关限流阈值 200 QPS。") == "fact"


def test_eviction_prefers_least_recently_used(tmp_path: Path) -> None:
    """§8.4：淘汰按 `COALESCE(last_hit_at, created_at)`，常用的旧条目不该被挤掉。"""
    policy = load_memory().long_term.model_copy(update={"max_entries": 3})
    db_path = tmp_path / "obs.db"
    with repo.connect(db_path) as conn:
        repo.init_db(conn)
        for number, text in enumerate(("最早的条目 AA", "第二条条目 BB", "第三条条目 CC"), start=1):
            repo.save_long_term(
                conn,
                scope=policy.scope,
                scope_id="s1",
                kind="fact",
                text=text,
                ttl_days=policy.ttl_days,
                max_entries=policy.max_entries,
                norm_text=f"seed-{number}",
            )
        # 最早那条被用过一次：它不该成为「最久未用」的那个
        oldest = repo.list_long_term(conn, scope=policy.scope, scope_id="s1")[-1]
        assert oldest["text"] == "最早的条目 AA"
        repo.touch_long_term(conn, [int(oldest["id"])])
        # 再写一条并把上限压到 2：被挤掉的应该是「最久未用」的 BB，不是 AA
        repo.save_long_term(
            conn,
            scope=policy.scope,
            scope_id="s1",
            kind="fact",
            text="第四条条目 DD",
            ttl_days=policy.ttl_days,
            max_entries=2,
            norm_text="seed-4",
        )
        kept = {row["text"] for row in repo.list_long_term(conn, scope=policy.scope, scope_id="s1")}

    assert len(kept) == 2
    assert "最早的条目 AA" in kept
    assert "第二条条目 BB" not in kept  # 真正最久未用的先出


def test_model_summarize_mode_reports_degraded(tmp_path: Path) -> None:
    """`summarize: model` 还没接模型通道：走抽取式并如实报 degraded。"""
    policy = load_memory()
    policy = policy.model_copy(
        update={"long_term": policy.long_term.model_copy(update={"summarize": "model"})}
    )
    runtime = MemoryRuntime("s1", policy, MemorySink(), tmp_path / "obs.db")
    _write(runtime, "入口在 scripts/start_pay.sh，先跑迁移再起 gunicorn。")
    frames = runtime.sink.drain()
    ends = [frame for frame in frames if frame["type"] == events.MEMORY_WRITE_END]
    assert ends and ends[-1]["state"] == "degraded"
    assert ends[-1]["reason"] == events.REASON_SUMMARIZE_MODEL_UNAVAILABLE
