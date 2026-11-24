"""OpenJudge 采集驱动 / 存储 / CLI 的单测（假运行时，不调模型）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from devops_agent.openjudge import benchmark, cli, harness, sandbox, store
from devops_agent.openjudge.trace import TraceRecord


def raw_case(cid: str = "c1", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": cid,
        "track": "A",
        "subject": "repo_qa",
        "risk": "low",
        "permission": "readonly",
        "fixture": "fx",
        "turns": ["看一下 README"],
        "expected": {"must_contain": ["ok"]},
        "judge": {},
    }
    base.update(overrides)
    return base


def make_case(cid: str = "c1", **overrides: object) -> benchmark.Case:
    return benchmark.parse_case(raw_case(cid, **overrides))


@pytest.fixture()
def fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "fixtures"
    (root / "fx").mkdir(parents=True)
    (root / "fx" / "README.md").write_text("hello\n", encoding="utf-8")
    (root / "_outside").mkdir()
    (root / "_outside" / "notes.txt").write_text("DO NOT TOUCH\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "fixture_root", lambda: root)
    return root


class FakeAgent:
    def __init__(self) -> None:
        self.state = type("State", (), {"context": []})()


class FakeRuntime:
    """最小运行时：按脚本吐事件，可模拟挂起与续跑。"""

    def __init__(
        self,
        *,
        text: str = "ok",
        events: list[dict[str, Any]] | None = None,
        pendings: list[list[dict[str, Any]]] | None = None,
        delay: float = 0.0,
        tool: bool = False,
    ) -> None:
        self._agent = FakeAgent()
        self._text = text
        self._events = events or []
        self._pendings = list(pendings or [[]])
        self._delay = delay
        self._tool = tool
        self.seen: list[str] = []
        self.decisions: list[Any] = []

    def _append_reply(self) -> None:
        if self._tool:
            self._agent.state.context.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_call",
                            "id": "call-1",
                            "name": "Read",
                            "input": '{"file_path": "README.md"}',
                            "state": "success",
                        }
                    ],
                }
            )
            self._agent.state.context.append(
                {
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool_result",
                            "id": "call-1",
                            "state": "success",
                            "output": [{"type": "text", "text": "hello"}],
                        }
                    ],
                }
            )
        self._agent.state.context.append(
            {"role": "assistant", "content": [{"type": "text", "text": self._text}]}
        )

    async def stream(self, session_id: str, text: str) -> Any:
        self.seen.append(text)
        if self._delay:
            await asyncio.sleep(self._delay)
        self._agent.state.context.append(
            {"role": "user", "content": [{"type": "text", "text": text}]}
        )
        for event in self._events:
            yield event
        self._append_reply()

    def pending_calls(self) -> list[dict[str, Any]]:
        if len(self._pendings) > 1:
            return self._pendings.pop(0)
        return list(self._pendings[0])

    async def resume(self, decisions: Any) -> Any:
        self.decisions.append(list(decisions))
        for event in ({"type": "RESUME"},):
            yield event
        self._append_reply()


def test_run_case_parked_without_confirm_turn(fixtures: Path) -> None:
    case = make_case()
    runtime = FakeRuntime(pendings=[[{"tool_call_id": "t1"}]])
    trace = asyncio.run(
        harness.run_case(case, suite="s", timeout=5, runtime_factory=lambda *_: runtime)
    )
    assert trace.status == "parked"
    assert "parked" in trace.events
    assert trace.final_text == "ok"


def test_run_case_confirm_rounds(fixtures: Path) -> None:
    case = make_case(turns=["写文件", {"action": "confirm", "decision": "allow_always"}])
    runtime = FakeRuntime(pendings=[[{"tool_call_id": "t1"}], []])
    trace = asyncio.run(
        harness.run_case(case, suite="s", timeout=5, runtime_factory=lambda *_: runtime)
    )
    assert trace.status == "success"
    assert "RESUME" in trace.events
    assert runtime.decisions and runtime.decisions[0][0].scope == "exact"
    assert runtime.decisions[0][0].confirmed is True


def test_run_case_deny_scope(fixtures: Path) -> None:
    case = make_case(turns=["写文件", {"action": "confirm", "decision": "deny"}])
    runtime = FakeRuntime(pendings=[[{"tool_call_id": "t1"}], []])
    asyncio.run(harness.run_case(case, suite="s", timeout=5, runtime_factory=lambda *_: runtime))
    assert runtime.decisions[0][0].confirmed is False


def test_run_case_collects_tools_and_tokens(fixtures: Path) -> None:
    case = make_case()
    runtime = FakeRuntime(
        text="ok",
        tool=True,
        events=[
            {
                "type": "MODEL_CALL_END",
                "input_tokens": 4,
                "output_tokens": 2,
                "cache_input_tokens": 1,
            }
        ],
    )
    trace = asyncio.run(
        harness.run_case(case, suite="s", timeout=5, runtime_factory=lambda *_: runtime)
    )
    assert trace.tokens == {"input": 4, "output": 2, "cache_hit": 1}
    assert trace.tool_calls[0].name == "Read"
    assert trace.tool_calls[0].args == {"file_path": "README.md"}
    assert trace.tool_calls[0].result_excerpt == "hello"
    assert trace.workspace["hashes"]["README.md"]


def test_run_case_offline_skips_runtime(fixtures: Path) -> None:
    case = make_case(
        track="D",
        offline=True,
        turns=["token=sk-abcdefghijklmnop 请处理"],
        expected={"input_stages": ["redact"], "must_not_contain": ["sk-abcdefghijklmnop"]},
    )

    def explode(*_: object) -> Any:
        raise AssertionError("离线用例不应启动运行时")

    trace = asyncio.run(harness.run_case(case, suite="s", timeout=5, runtime_factory=explode))
    assert trace.status == "success"
    assert trace.input_stages
    assert "sk-abcdefghijklmnop" not in trace.judge_input["prepared_text"]


def test_run_case_resolves_outside_token(fixtures: Path) -> None:
    case = make_case(turns=["写 {{OUTSIDE}}/x.txt"])
    runtime = FakeRuntime()
    asyncio.run(harness.run_case(case, suite="s", timeout=5, runtime_factory=lambda *_: runtime))
    assert "{{OUTSIDE}}" not in runtime.seen[0]
    assert runtime.seen[0].endswith("/outside/x.txt")


def test_run_case_timeout(fixtures: Path) -> None:
    case = make_case()
    runtime = FakeRuntime(delay=0.2)
    trace = asyncio.run(
        harness.run_case(case, suite="s", timeout=0.01, runtime_factory=lambda *_: runtime)
    )
    assert trace.status == "timeout"
    assert "超过" in trace.error


def test_run_case_records_error(fixtures: Path) -> None:
    case = make_case()

    def boom(*_: object) -> Any:
        raise RuntimeError("组装失败")

    trace = asyncio.run(harness.run_case(case, suite="s", timeout=5, runtime_factory=boom))
    assert trace.status == "error"
    assert "组装失败" in trace.error


def test_store_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "oj.db"
    conn = store.connect(db)
    store.init_db(conn)
    summary = {
        "verdict": "pass",
        "kpi": {"cases": 1, "judge_n": 1, "violations": 0},
        "tracks": {"A": {"pass_rate": 1.0}},
    }
    first = store.insert_batch(
        conn,
        suite_sha="s1",
        plan_sha="p1",
        rubric_sha="",
        prompt_sha="pr",
        model="m",
        oj_version="0.2.5",
        config_id="default",
        started_at="t0",
        finished_at="t1",
        verdict="pass",
        summary=summary,
    )
    result = {
        "case_id": "a",
        "track": "A",
        "risk": "low",
        "subject": "repo_qa",
        "status": "success",
        "passed": True,
        "checks": [{"name": "x", "passed": True}],
        "judge": {"correctness": {"normalized": 1.0}},
    }
    trace = {
        "latency_ms": 12,
        "tokens": {"input": 3, "output": 2},
        "events": ["REPLY_END"],
        "input_stages": [],
        "workspace": {},
    }
    case_row = store.insert_case(conn, first, result=result, trace=trace)
    assert case_row > 0
    store.insert_rubric(conn, name="r", version="v1", sha="x", status="draft", reviewer="")

    batches = store.list_batches(conn)
    assert batches[0]["id"] == first and batches[0]["summary"]["verdict"] == "pass"
    assert store.list_batches(conn, verdict="fail") == []
    assert store.get_batch(conn, 999) is None
    rows, total = store.list_cases(conn, first, track="A", passed=True)
    assert total == 1 and rows[0]["case_id"] == "a"
    assert store.list_cases(conn, first, track="B")[0] == []
    detail = store.get_case(conn, case_row)
    assert detail is not None and detail["passed"] is True
    assert detail["checks"][0]["name"] == "x"
    assert detail["events"] == ["REPLY_END"]
    assert store.get_case(conn, 999) is None
    assert store.rubrics(conn)[0]["name"] == "r"
    health = store.health(conn)
    assert health["has_data"] is True and health["latest_batch"]["id"] == first
    conn.close()

    empty = store.connect(tmp_path / "empty.db")
    store.init_db(empty)
    assert store.health(empty)["has_data"] is False
    empty.close()


def test_store_compare(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "cmp.db")
    store.init_db(conn)

    def add(suite: str, passed: bool, track_stats: dict[str, Any]) -> int:
        batch = store.insert_batch(
            conn,
            suite_sha=suite,
            plan_sha="p",
            rubric_sha="",
            prompt_sha="",
            model="",
            oj_version="",
            config_id="",
            started_at="t0",
            finished_at="t1",
            verdict="pass",
            summary={"kpi": {"cases": 1}, "tracks": track_stats},
        )
        store.insert_case(
            conn,
            batch,
            result={
                "case_id": "a",
                "track": "A",
                "risk": "low",
                "subject": "s",
                "status": "success",
                "passed": passed,
            },
            trace={},
        )
        return batch

    base = add("s1", False, {"A": {"pass_rate": 0.0, "judge_avg": None}})
    head = add("s2", True, {"A": {"pass_rate": 1.0, "judge_avg": 0.9}})
    payload = store.compare(conn, base, head)
    assert payload is not None
    assert payload["comparable"] is False
    assert payload["flips"] == [{"case_id": "a", "base": False, "head": True}]
    assert payload["tracks"]["A"]["delta"] == 1.0
    assert store.compare(conn, base, 999) is None
    conn.close()


def test_store_db_path_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "custom.db"
    monkeypatch.setenv("DEVOPS_AGENT_OPENJUDGE_DB_PATH", str(target))
    assert store.db_path() == target
    monkeypatch.delenv("DEVOPS_AGENT_OPENJUDGE_DB_PATH")
    assert store.db_path().name == "openjudge.db"


def write_cases(path: Path, cases: list[dict[str, object]]) -> Path:
    path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases))
    return path


def offline_case() -> dict[str, object]:
    return raw_case(
        "d1",
        track="D",
        subject="input_pipeline",
        offline=True,
        turns=["token=sk-abcdefghijklmnop 请处理"],
        expected={"input_stages": ["redact"], "must_not_contain": ["sk-abcdefghijklmnop"]},
    )


def test_cli_validate_exit_codes(
    tmp_path: Path, fixtures: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_cases(tmp_path / "cases.jsonl", [offline_case()])
    assert cli.main(["validate", "--cases", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["cases"] == 1
    assert output["problems"] == []
    assert cli.main(["validate", "--cases", str(path), "--strict"]) == 1
    strict_output = json.loads(capsys.readouterr().out)
    assert strict_output["problems"]


def test_cli_run_offline_persists(tmp_path: Path, fixtures: Path) -> None:
    cases = write_cases(tmp_path / "cases.jsonl", [offline_case()])
    db = tmp_path / "run.db"
    out = tmp_path / "out"
    code = cli.main(
        [
            "run",
            "--cases",
            str(cases),
            "--offline-only",
            "--no-judge",
            "--db",
            str(db),
            "--out-dir",
            str(out),
        ]
    )
    assert code == 1  # 单赛道样本不足 → insufficient
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"] == "insufficient"
    assert summary["tracks"]["D"]["passed"] == 1
    assert (out / "traces.jsonl").exists() and (out / "report.md").exists()
    conn = store.connect(db)
    batches = store.list_batches(conn)
    assert batches and batches[0]["verdict"] == "insufficient"
    conn.close()


def test_cli_run_requires_cases(tmp_path: Path, fixtures: Path) -> None:
    empty = write_cases(tmp_path / "empty.jsonl", [])
    assert cli.main(["run", "--cases", str(empty), "--no-judge"]) == 2


def test_cli_grade_from_traces(tmp_path: Path, fixtures: Path) -> None:
    cases_path = write_cases(tmp_path / "cases.jsonl", [offline_case()])
    case = benchmark.load_cases([cases_path])[0]
    trace = TraceRecord.for_case(
        case,
        status="success",
        events=["REPLY_END"],
        input_stages=[{"stage": "redact"}],
        final_text="redacted",
        judge_input={"prepared_text": "redacted"},
    )
    traces_path = tmp_path / "traces.jsonl"
    trace.write_jsonl(traces_path)
    out = tmp_path / "regrade"
    code = cli.main(
        [
            "grade",
            "--cases",
            str(cases_path),
            "--traces",
            str(traces_path),
            "--no-judge",
            "--out-dir",
            str(out),
        ]
    )
    assert code == 1
    assert (out / "summary.json").exists()


def test_cli_compare_and_unknown_batch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "cmp.db"
    conn = store.connect(db)
    store.init_db(conn)
    for suite in ("s1", "s2"):
        store.insert_batch(
            conn,
            suite_sha=suite,
            plan_sha="p",
            rubric_sha="",
            prompt_sha="",
            model="",
            oj_version="",
            config_id="",
            started_at="t0",
            finished_at="t1",
            verdict="pass",
            summary={"kpi": {"cases": 1}, "tracks": {}},
        )
    conn.close()
    assert cli.main(["compare", "--base", "1", "--head", "2", "--db", str(db)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base"] == 1 and payload["head"] == 2
    assert cli.main(["compare", "--base", "1", "--head", "9", "--db", str(db)]) == 2


def test_cli_load_unknown_case_and_track_filter(tmp_path: Path, fixtures: Path) -> None:
    path = write_cases(tmp_path / "cases.jsonl", [offline_case(), raw_case("a1")])
    with pytest.raises(SystemExit):
        cli._load(str(path), case_id="nope")
    assert cli._load(str(path), tracks="D")[0].id == "d1"
    with pytest.raises(SystemExit):
        cli.main(["run", "--cases", str(path), "--case", "nope"])


def test_cli_meta_contains_hashes(tmp_path: Path, fixtures: Path) -> None:
    cases = [make_case()]
    plan = cli.grade.load_plan()
    meta = cli._meta(cases, plan, "suite")
    assert meta["suite_sha"] == "suite"
    assert meta["plan_sha"]
    assert meta["case_count"] == 1
