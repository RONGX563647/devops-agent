"""数据库连接、路径与建表迁移。

设计约束（ADR-0001）：
- 只用 stdlib sqlite3，零新依赖；
- 数据库路径由 DEVOPS_AGENT_DB_PATH 覆盖，默认落在已 gitignore 的 workspace/；
- 表间外键 ON DELETE CASCADE，保证删除 run 时 spans / eval_scores 一并清理。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from devops_agent.observability.models import EvalDimension, RunStatus

logger = logging.getLogger(__name__)

DB_PATH_ENV = "DEVOPS_AGENT_DB_PATH"

# 评测维度 CHECK 与枚举同源：新增维度只改 EvalDimension，DB 约束自动跟进。
_DIMENSIONS_SQL = ", ".join(f"'{dim.value}'" for dim in EvalDimension)
# 运行状态同理：DDL 的 CHECK 在 SQLite 里改不了，只能靠「代码与建表同源」防漂移。
# 注意已存在的库不会自动放宽 CHECK —— 加状态要走 `_RUN_COLUMNS` 的加列路线。
_STATUS_SQL = ", ".join(f"'{status.value}'" for status in RunStatus)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    domain        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ({_STATUS_SQL})),
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
    cache_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    work_ms       INTEGER NOT NULL DEFAULT 0,
    wall_ms       INTEGER NOT NULL DEFAULT 0,
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
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    seq             INTEGER NOT NULL DEFAULT 0,
    parent_id       INTEGER
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
-- 通用 KV 配置表：当前只放 model_config，以后 health/gates 的旋钮下放也复用它。
-- 用 KV 而不是建 model_settings 表，是为了不再为每个配置域加一张表。
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
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
    # 缓存命中/写入：老库补列后旧记录读出来是 0，界面据此显示「无数据」而不是 0%
    ("cache_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_creation_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    # 双口径耗时：实测（span 并集）与墙钟（起止差），差值即用户空闲
    ("work_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("wall_ms", "INTEGER NOT NULL DEFAULT 0"),
    # 失败原因原文：只记 status=failed 时，「37 个 span 全 ok 却失败」这种
    # 情况无法复盘，日志一滚就彻底丢了
    ("error", "TEXT NOT NULL DEFAULT ''"),
    # 本轮实际下发的模型参数快照（只含非默认值）。参数可以随时改，
    # 没有快照的话「参数变化点前后」的健康分与评测批次会混在一起算。
    ("model_params", "TEXT NOT NULL DEFAULT ''"),
)

# session_states 上后加的列：会话级模型参数覆盖（JSON；空串 = 无覆盖）。
# 必须落库：SessionRegistry 是 LRU，会话被回收后从 DB 重建，
# 覆盖只在内存里的话一次淘汰就丢了。
_SESSION_COLUMNS: tuple[tuple[str, str], ...] = (("model_config", "TEXT NOT NULL DEFAULT ''"),)

# spans 上后加的列：逐调用的 token 分拆与缓存命中，用来回答「哪一步开始全冷」。
_SPAN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_creation_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    # run 内单调序号与父 span：列表与泳道共用同一份顺序，工具能追溯到发起它的模型
    ("seq", "INTEGER NOT NULL DEFAULT 0"),
    ("parent_id", "INTEGER"),
)


def default_db_path() -> Path:
    """数据库路径：环境变量优先，默认 workspace/observability.db。"""
    override = os.environ.get(DB_PATH_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[4] / "workspace" / "observability.db"


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
    _ensure_columns(conn, "runs", _RUN_COLUMNS)
    _ensure_columns(conn, "spans", _SPAN_COLUMNS)
    _ensure_columns(conn, "session_states", _SESSION_COLUMNS)


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    """给指定表补齐缺失的列（表名与 DDL 都是模块内常量，不含外部输入）。"""
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
