"""SQLite 仓储 —— 可观测性数据的唯一读写入口。

设计约束（ADR-0001）：
- 只用 stdlib sqlite3，零新依赖；
- 上层经 repository 函数访问，业务代码不直接写 SQL；
- 数据库路径由 DEVOPS_AGENT_DB_PATH 覆盖，默认落在已 gitignore 的 workspace/；
- 表间外键 ON DELETE CASCADE，保证删除 run 时 spans / eval_scores 一并清理。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from devops_agent.eval.scorer import EvalDimension
from devops_agent.observability.models import (
    EvalScoreRecord,
    InputStageRecord,
    RunRecord,
    SpanRecord,
)

DB_PATH_ENV = "DEVOPS_AGENT_DB_PATH"

# 评测维度 CHECK 与枚举同源：新增维度只改 EvalDimension，DB 约束自动跟进。
_DIMENSIONS_SQL = ", ".join(f"'{dim.value}'" for dim in EvalDimension)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_request ON runs(request_id)
    WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runs_query ON runs(domain, status, started_at);
CREATE TABLE IF NOT EXISTS spans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    type            TEXT NOT NULL CHECK (type IN ('model', 'tool', 'permission')),
    name            TEXT NOT NULL,
    start_offset_ms INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER,
    input           TEXT NOT NULL DEFAULT '',
    output          TEXT NOT NULL DEFAULT '',
    tokens          INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
    error           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_spans_run ON spans(run_id);
CREATE TABLE IF NOT EXISTS input_stages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'skip', 'error')),
    changed     INTEGER NOT NULL DEFAULT 0,
    summary     TEXT NOT NULL DEFAULT '',
    before_chars INTEGER NOT NULL DEFAULT 0,
    after_chars INTEGER NOT NULL DEFAULT 0,
    items       INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    detail      TEXT NOT NULL DEFAULT '',
    failed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_input_stages_run ON input_stages(run_id);
CREATE TABLE IF NOT EXISTS eval_scores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    dimension   TEXT NOT NULL CHECK (dimension IN ({_DIMENSIONS_SQL})),
    score       REAL NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('human', 'judge', 'rule')),
    rater       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    comment     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scores_run ON eval_scores(run_id);
CREATE TABLE IF NOT EXISTS session_states (
    session_id  TEXT PRIMARY KEY,
    state_json  TEXT NOT NULL DEFAULT '',
    permission  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);
"""

# 后加的列（CREATE TABLE IF NOT EXISTS 不会给老库补列，必须 ALTER）。
# parked：该 run 正在等用户确认；用独立列而不是塞进 status，
# 因为 status 的 CHECK 约束在 SQLite 里改不了，加列比重建表安全。
_RUN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("permission", "TEXT NOT NULL DEFAULT ''"),
    ("parked", "INTEGER NOT NULL DEFAULT 0"),
    ("pending_json", "TEXT"),
    ("expires_at", "TEXT"),
    # 本 run 已发出的确认请求次数：模型被拒后换个写法重试会产生新的 tool_call_id，
    # 按 id 去重挡不住，只能按 run 计数兜底
    ("ask_count", "INTEGER NOT NULL DEFAULT 0"),
)


def default_db_path() -> Path:
    """数据库路径：环境变量优先，默认 workspace/observability.db。"""
    override = os.environ.get(DB_PATH_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "workspace" / "observability.db"


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """打开连接并保证事务提交/回滚与关闭。"""
    path = db_path if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL：SSE 边写 span 与前端并发读 runs 时不互相阻塞；busy_timeout 吞掉瞬时锁竞争
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    """建表（幂等），并给老库补齐后加的列。"""
    conn.executescript(_SCHEMA)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """按 PRAGMA 结果补列。已存在的列跳过，重复执行安全。"""
    existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(runs)")}
    for name, ddl in _RUN_COLUMNS:
        if name not in existing:
            # 列名与 DDL 都是模块内常量，不含外部输入
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")


def create_run(conn: sqlite3.Connection, run: RunRecord) -> int:
    """插入一条运行记录，返回 id。

    幂等：若 request_id 非空且已存在，返回已有 id，不重复插入（防前端重试）。
    """
    if run.request_id:
        existing = conn.execute(
            "SELECT id FROM runs WHERE request_id=?", (run.request_id,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
    cur = conn.execute(
        """INSERT INTO runs
           (session_id, domain, status, input, output, started_at, ended_at,
            duration_ms, model, total_tokens, health_score,
            input_tokens, output_tokens, request_id, permission)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run.session_id,
            run.domain,
            run.status,
            run.input,
            run.output,
            run.started_at,
            run.ended_at,
            run.duration_ms,
            run.model,
            run.total_tokens,
            run.health_score,
            run.input_tokens,
            run.output_tokens,
            run.request_id or None,
            run.permission,
        ),
    )
    assert cur.lastrowid is not None  # INSERT 成功时 sqlite3 保证有 lastrowid
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    output: str,
    ended_at: str,
    duration_ms: int,
    total_tokens: int,
    health_score: float | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """运行结束时回填结果与聚合指标（含 token 分拆）。"""
    conn.execute(
        """UPDATE runs SET status=?, output=?, ended_at=?, duration_ms=?,
           total_tokens=?, health_score=?, input_tokens=?, output_tokens=? WHERE id=?""",
        (
            status,
            output,
            ended_at,
            duration_ms,
            total_tokens,
            health_score,
            input_tokens,
            output_tokens,
            run_id,
        ),
    )


def add_span(conn: sqlite3.Connection, span: SpanRecord) -> int:
    """插入一个步骤 span，返回自增 id。"""
    cur = conn.execute(
        """INSERT INTO spans
           (run_id, type, name, start_offset_ms, duration_ms, input, output,
            tokens, status, error, input_tokens, output_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            span.run_id,
            span.type,
            span.name,
            span.start_offset_ms,
            span.duration_ms,
            span.input,
            span.output,
            span.tokens,
            span.status,
            span.error,
            span.input_tokens,
            span.output_tokens,
        ),
    )
    assert cur.lastrowid is not None  # INSERT 成功时 sqlite3 保证有 lastrowid
    return int(cur.lastrowid)


def add_input_stages(conn: sqlite3.Connection, run_id: int, stages: list[InputStageRecord]) -> int:
    """批量写入一次运行的全部解析阶段，返回写入条数。

    全量落库（包括 skip 的「无改动」阶段）：界面只展示有用的那几条，
    但审计与复现需要完整的处理链，否则无法回答「当时到底做了什么」。
    """
    if not stages:
        return 0
    conn.executemany(
        """INSERT INTO input_stages
           (run_id, seq, stage, label, status, changed, summary,
            before_chars, after_chars, items, duration_ms, detail, failed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                run_id,
                stage.seq,
                stage.stage,
                stage.label,
                stage.status,
                1 if stage.changed else 0,
                stage.summary,
                stage.before,
                stage.after,
                stage.items,
                stage.duration_ms,
                stage.detail,
                stage.failed,
            )
            for stage in stages
        ],
    )
    return len(stages)


def list_input_stages(conn: sqlite3.Connection, run_id: int) -> list[InputStageRecord]:
    """按执行顺序列出一条运行的解析阶段。"""
    rows = conn.execute(
        "SELECT * FROM input_stages WHERE run_id=? ORDER BY seq, id", (run_id,)
    ).fetchall()
    return [_input_stage_from_row(row) for row in rows]


def add_score(conn: sqlite3.Connection, score: EvalScoreRecord) -> int:
    """插入一条质量评分，返回自增 id。"""
    cur = conn.execute(
        """INSERT INTO eval_scores
           (run_id, dimension, score, source, rater, created_at, comment)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            score.run_id,
            score.dimension,
            score.score,
            score.source,
            score.rater,
            score.created_at,
            score.comment,
        ),
    )
    assert cur.lastrowid is not None  # INSERT 成功时 sqlite3 保证有 lastrowid
    return int(cur.lastrowid)


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        session_id=row["session_id"],
        domain=row["domain"],
        status=row["status"],
        input=row["input"],
        output=row["output"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_ms=row["duration_ms"],
        model=row["model"],
        total_tokens=row["total_tokens"],
        health_score=row["health_score"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        request_id=row["request_id"],
        permission=row["permission"] or "",
    )


def _span_from_row(row: sqlite3.Row) -> SpanRecord:
    return SpanRecord(
        id=row["id"],
        run_id=row["run_id"],
        type=row["type"],
        name=row["name"],
        start_offset_ms=row["start_offset_ms"],
        duration_ms=row["duration_ms"],
        input=row["input"],
        output=row["output"],
        tokens=row["tokens"],
        status=row["status"],
        error=row["error"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
    )


def _input_stage_from_row(row: sqlite3.Row) -> InputStageRecord:
    return InputStageRecord(
        id=row["id"],
        run_id=row["run_id"],
        seq=row["seq"],
        stage=row["stage"],
        label=row["label"],
        status=row["status"],
        changed=bool(row["changed"]),
        summary=row["summary"],
        before=row["before_chars"],
        after=row["after_chars"],
        items=row["items"],
        duration_ms=row["duration_ms"],
        detail=row["detail"],
        failed=row["failed"],
    )


def _score_from_row(row: sqlite3.Row) -> EvalScoreRecord:
    return EvalScoreRecord(
        id=row["id"],
        run_id=row["run_id"],
        dimension=row["dimension"],
        score=row["score"],
        source=row["source"],
        rater=row["rater"],
        created_at=row["created_at"],
        comment=row["comment"],
    )


def get_run(conn: sqlite3.Connection, run_id: int) -> RunRecord | None:
    """按 id 取运行记录，不存在返回 None。"""
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run_from_row(row) if row is not None else None


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[RunRecord]:
    """按开始时间倒序列出运行记录。"""
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_run_from_row(row) for row in rows]


def list_spans(conn: sqlite3.Connection, run_id: int) -> list[SpanRecord]:
    """按时间偏移列出一次运行的全部步骤。"""
    rows = conn.execute(
        "SELECT * FROM spans WHERE run_id=? ORDER BY start_offset_ms, id", (run_id,)
    ).fetchall()
    return [_span_from_row(row) for row in rows]


def list_scores(conn: sqlite3.Connection, run_id: int) -> list[EvalScoreRecord]:
    """列出一次运行的全部评分。"""
    rows = conn.execute(
        "SELECT * FROM eval_scores WHERE run_id=? ORDER BY created_at, id", (run_id,)
    ).fetchall()
    return [_score_from_row(row) for row in rows]


def delete_run(conn: sqlite3.Connection, run_id: int) -> bool:
    """删除一条运行及其关联数据，返回是否命中。"""
    cur = conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
    return cur.rowcount > 0


def _run_filters(
    domain: str | None,
    status: str | None,
    since: str | None,
    until: str | None,
) -> tuple[str, list[object]]:
    """构造 runs 筛选 WHERE 子句与参数（参数化，防注入）。"""
    clauses: list[str] = []
    params: list[object] = []
    if domain:
        clauses.append("domain=?")
        params.append(domain)
    if status:
        clauses.append("status=?")
        params.append(status)
    if since:
        clauses.append("started_at>=?")
        params.append(since)
    if until:
        clauses.append("started_at<=?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_runs(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RunRecord]:
    """按条件筛选运行记录，倒序分页。"""
    where, params = _run_filters(domain, status, since, until)
    rows = conn.execute(
        "SELECT * FROM runs" + where + " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_run_from_row(row) for row in rows]


def count_runs(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """按条件统计运行记录数（配合 query_runs 做分页）。"""
    where, params = _run_filters(domain, status, since, until)
    row = conn.execute("SELECT COUNT(*) AS n FROM runs" + where, params).fetchone()
    return int(row["n"])


def summary_rows(conn: sqlite3.Connection) -> list[RunRecord]:
    """取出所有 run 的指标字段供聚合（不含 input/output 大文本）。"""
    rows = conn.execute(
        """SELECT id, session_id, domain, status, started_at, ended_at, duration_ms,
                  model, total_tokens, health_score, input_tokens, output_tokens, request_id,
                  input, output, permission
           FROM runs"""
    ).fetchall()
    return [_run_from_row(row) for row in rows]


def count_spans_by_type(conn: sqlite3.Connection, span_type: str) -> tuple[int, int]:
    """统计某类 span 的总数与失败数，返回 (总数, 失败数)。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS f "
        "FROM spans WHERE type=?",
        (span_type,),
    ).fetchone()
    return int(row["n"]), int(row["f"] or 0)


def all_scores(conn: sqlite3.Connection) -> list[EvalScoreRecord]:
    """取出全部评分供趋势聚合。"""
    rows = conn.execute("SELECT * FROM eval_scores ORDER BY created_at, id").fetchall()
    return [_score_from_row(row) for row in rows]


# --- 挂起与恢复 ---------------------------------------------------------
# 一条 run 在等用户确认时被标记 parked。所有状态迁移都用「带前置状态的条件
# UPDATE」完成：影响 0 行即说明状态已被别人改过，调用方据此返回 409，
# 这样双击「允许」或两个标签页同时确认都不会重复执行。


def park_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    pending_json: str,
    expires_at: str,
) -> bool:
    """把 run 标记为等待确认；已处于挂起态时返回 False（幂等）。"""
    cur = conn.execute(
        "UPDATE runs SET parked=1, pending_json=?, expires_at=? WHERE id=? AND parked=0",
        (pending_json, expires_at, run_id),
    )
    return cur.rowcount > 0


def unpark_run(conn: sqlite3.Connection, run_id: int, *, now: str) -> bool:
    """解除挂起；已解除或已过期返回 False。

    过期判定放在 SQL 里一起做，避免「先查再改」的竞态窗口。
    """
    cur = conn.execute(
        """UPDATE runs SET parked=0, pending_json=NULL, expires_at=NULL
           WHERE id=? AND parked=1 AND (expires_at IS NULL OR expires_at > ?)""",
        (run_id, now),
    )
    return cur.rowcount > 0


def get_pending(conn: sqlite3.Connection, run_id: int) -> dict[str, object] | None:
    """取挂起信息：run_id / session_id / pending_json / expires_at / parked。"""
    row = conn.execute(
        """SELECT id, session_id, parked, pending_json, expires_at, permission
           FROM runs WHERE id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "run_id": int(row["id"]),
        "session_id": str(row["session_id"]),
        "parked": bool(row["parked"]),
        "pending_json": row["pending_json"],
        "expires_at": row["expires_at"],
        "permission": str(row["permission"] or ""),
    }


def active_run_for_session(conn: sqlite3.Connection, session_id: str) -> int | None:
    """同一会话是否已有未结束的 run（含挂起）。"""
    row = conn.execute(
        "SELECT id FROM runs WHERE session_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def bump_asks(conn: sqlite3.Connection, run_id: int) -> int:
    """把本 run 的确认次数 +1 并返回新值。"""
    conn.execute("UPDATE runs SET ask_count=ask_count+1 WHERE id=?", (run_id,))
    row = conn.execute("SELECT ask_count FROM runs WHERE id=?", (run_id,)).fetchone()
    return int(row["ask_count"]) if row is not None else 0


# --- 会话状态 -----------------------------------------------------------


def save_session_state(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    state_json: str,
    permission: str,
    updated_at: str,
) -> None:
    """整份覆盖写会话快照（AgentState 的 JSON）。"""
    conn.execute(
        """INSERT INTO session_states (session_id, state_json, permission, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             state_json=excluded.state_json,
             permission=excluded.permission,
             updated_at=excluded.updated_at""",
        (session_id, state_json, permission, updated_at),
    )


def load_session_state(conn: sqlite3.Connection, session_id: str) -> tuple[str, str] | None:
    """读取会话快照，返回 (state_json, permission)；没有则 None。"""
    row = conn.execute(
        "SELECT state_json, permission FROM session_states WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return str(row["state_json"] or ""), str(row["permission"] or "")


def delete_session_state(conn: sqlite3.Connection, session_id: str) -> bool:
    """删除会话快照，返回是否命中。"""
    cur = conn.execute("DELETE FROM session_states WHERE session_id=?", (session_id,))
    return cur.rowcount > 0
