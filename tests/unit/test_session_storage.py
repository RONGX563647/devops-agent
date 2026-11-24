"""存储层测试：老库补列、挂起态迁移、会话快照、跨线程写。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from devops_agent.observability import repository as repo
from devops_agent.observability.recorder import RunRecorder

_OLD_RUNS = """
CREATE TABLE runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    domain        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    input         TEXT NOT NULL,
    output        TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    duration_ms   INTEGER,
    model         TEXT NOT NULL DEFAULT '',
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    health_score  REAL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    request_id    TEXT
);
"""


def test_init_db_adds_columns_to_old_database(tmp_path: Path) -> None:
    """既有部署的库不会重建表，新增列必须靠 ALTER 补齐。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_RUNS)
    conn.execute(
        "INSERT INTO runs (session_id, domain, status, input, started_at) "
        "VALUES ('s0', 'debug', 'success', 'legacy', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    with repo.connect(path) as live:
        repo.init_db(live)
        repo.init_db(live)  # 幂等：第二次不能因为列已存在而失败
        columns = {row["name"] for row in live.execute("PRAGMA table_info(runs)")}
        legacy = repo.get_run(live, 1)

    assert {"permission", "parked", "pending_json", "expires_at", "ask_count"} <= columns
    # 失败原因列同样是给老库补的：缺了它，历史 run 永远只有 status 没有原因
    assert "error" in columns
    # 老数据保留，新列取默认值
    assert legacy is not None and legacy.input == "legacy"
    assert legacy.permission == ""
    assert legacy.error == ""


def test_unpark_is_conditional_and_expiry_aware(tmp_path: Path) -> None:
    path = tmp_path / "park.db"
    with repo.connect(path) as conn:
        repo.init_db(conn)
        run_id = repo.create_run(
            conn,
            repo.RunRecord(
                id=None,
                session_id="s1",
                domain="debug",
                status="running",
                input="x",
                output="",
                started_at="2026-01-01T00:00:00+00:00",
                ended_at=None,
                duration_ms=None,
                model="",
                total_tokens=0,
                health_score=None,
            ),
        )
        assert repo.park_run(
            conn, run_id, pending_json="{}", expires_at="2027-01-01T00:00:00+00:00"
        )
        # 已挂起时再挂一次是 no-op
        assert not repo.park_run(
            conn, run_id, pending_json="{}", expires_at="2027-01-01T00:00:00+00:00"
        )
        assert repo.unpark_run(conn, run_id, now="2026-06-01T00:00:00+00:00")
        # 重复解除返回 False —— confirm 的幂等就靠这一条
        assert not repo.unpark_run(conn, run_id, now="2026-06-01T00:00:00+00:00")


def test_unpark_refuses_expired(tmp_path: Path) -> None:
    path = tmp_path / "expired.db"
    with repo.connect(path) as conn:
        repo.init_db(conn)
        run_id = repo.create_run(
            conn,
            repo.RunRecord(
                id=None,
                session_id="s1",
                domain="debug",
                status="running",
                input="x",
                output="",
                started_at="2026-01-01T00:00:00+00:00",
                ended_at=None,
                duration_ms=None,
                model="",
                total_tokens=0,
                health_score=None,
            ),
        )
        repo.park_run(conn, run_id, pending_json="{}", expires_at="2026-01-01T00:10:00+00:00")
        assert not repo.unpark_run(conn, run_id, now="2026-06-01T00:00:00+00:00")


def test_session_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with repo.connect(path) as conn:
        repo.init_db(conn)
        assert repo.load_session_state(conn, "s1") is None
        repo.save_session_state(
            conn, "s1", state_json='{"a": 1}', permission="workspace", updated_at="t1"
        )
        repo.save_session_state(
            conn, "s1", state_json='{"a": 2}', permission="execute", updated_at="t2"
        )
        assert repo.load_session_state(conn, "s1") == ('{"a": 2}', "execute")
        assert repo.delete_session_state(conn, "s1")
        assert not repo.delete_session_state(conn, "s1")


def test_recorder_survives_thread_switch(tmp_path: Path) -> None:
    """挂起 → 确认跨了两个请求/线程：长连接会抛 sqlite 线程错误，短连接不会。"""
    path = tmp_path / "thread.db"
    recorder = RunRecorder(path)
    run_id = recorder.start("s1", "hi")

    def finish_in_thread() -> None:
        recorder.finish(status="success")

    worker = threading.Thread(target=finish_in_thread)
    worker.start()
    worker.join()

    with repo.connect(path) as conn:
        record = repo.get_run(conn, run_id)
    assert record is not None
    assert record.status == "success"
