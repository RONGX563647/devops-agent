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

# spans 的建表语句单独抽出来：老库的 CHECK 里没有 'memory'，SQLite 改不了 CHECK，
# 只能重建表，重建时必须与这里逐字一致，否则新老库结构会漂移。
_SPANS_DDL = """
CREATE TABLE IF NOT EXISTS spans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    type            TEXT NOT NULL CHECK (type IN ('model', 'tool', 'permission', 'memory')),
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
    parent_id       INTEGER,
    ttft_ms         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_spans_run ON spans(run_id);
"""

_SPAN_COPY_COLUMNS = (
    "id",
    "run_id",
    "type",
    "name",
    "start_offset_ms",
    "duration_ms",
    "input",
    "output",
    "tokens",
    "status",
    "error",
    "input_tokens",
    "output_tokens",
    "cache_input_tokens",
    "cache_creation_input_tokens",
    "seq",
    "parent_id",
    "ttft_ms",
)

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
    request_id    TEXT,
    workspace_id  TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_request ON runs(request_id)
    WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runs_query ON runs(domain, status, started_at);
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
    updated_at  TEXT NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT ''
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
-- 中期记忆版本（SPEC-0013）：每次压缩成功写一版，is_active 只保留一条。
CREATE TABLE IF NOT EXISTS session_memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    version         INTEGER NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 0,
    trigger         TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL DEFAULT '',
    artifacts       TEXT NOT NULL DEFAULT '[]',
    pending         TEXT NOT NULL DEFAULT '[]',
    errors          TEXT NOT NULL DEFAULT '[]',
    tokens_before   INTEGER NOT NULL DEFAULT 0,
    tokens_after    INTEGER NOT NULL DEFAULT 0,
    budget          INTEGER NOT NULL DEFAULT 0,
    source_run_ids  TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    expires_at      TEXT,
    workspace_id    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memories_session ON session_memories(session_id, is_active);
-- 长期记忆（local 兜底后端）：按 (scope, scope_id, norm_text) 去重，TTL 由 expires_at 控制。
-- 旧的 `text` 唯一索引保留：它的语义是「同一段原文不重复落库」，与归一化去重互补。
CREATE TABLE IF NOT EXISTS long_term_memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    scope_id   TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'fact',
    text       TEXT NOT NULL,
    score      REAL NOT NULL DEFAULT 0,
    hits       INTEGER NOT NULL DEFAULT 0,
    last_hit_at TEXT,
    norm_text  TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_long_term_unique
    ON long_term_memories(scope, scope_id, text);
CREATE INDEX IF NOT EXISTS idx_long_term_scope
    ON long_term_memories(scope, scope_id, created_at);
"""

# 归一化文本的唯一索引必须建在补列之后：老库里这一列不存在，
# 放进 _SCHEMA 会让 `CREATE INDEX` 先于 `ALTER TABLE` 执行而报错。
_LONG_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_long_term_norm
    ON long_term_memories(scope, scope_id, norm_text);
"""

# 长期表后加的列（SPEC-0016 §8.4）：命中计数、最近使用时间与归一化文本。
# 老库这三列都是 NULL/0，读取时按「未强化过」处理，不影响检索。
_LONG_TERM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("hits", "INTEGER NOT NULL DEFAULT 0"),
    ("last_hit_at", "TEXT"),
    ("norm_text", "TEXT"),
)

# spans 表必须显式包含在 _SCHEMA 里；建表语句是上面单独抽出的 _SPANS_DDL
_SCHEMA = _SCHEMA + _SPANS_DDL

# 工作区（SPEC-0021）：一工作区一代码库。三张表追加式引入，
# 老库由 init_db 幂等建表；session_states / runs / session_memories 只补列。
# `source` 的 CHECK 里必须有 worktree：本地仓库的工作区落的是 git worktree，
# 既不是原地引用也不是克隆。SQLite 改不了 CHECK，老库走 _ensure_repository_sources。
_WORKSPACE_REPO_DDL = """CREATE TABLE {if_not_exists}{table} (
    workspace_id   TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    source         TEXT NOT NULL CHECK (source IN ('local','git','worktree')),
    managed        INTEGER NOT NULL DEFAULT 0,
    remote_url     TEXT NOT NULL DEFAULT '',
    branch         TEXT NOT NULL DEFAULT '',
    base_commit    TEXT NOT NULL DEFAULT '',
    credential_ref TEXT NOT NULL DEFAULT '',
    detected_git   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);"""

# 建表用 IF NOT EXISTS；重建表（迁移）不能带它，否则会静默跳过
_WORKSPACE_REPO_SQL = _WORKSPACE_REPO_DDL.format(
    if_not_exists="IF NOT EXISTS ", table="workspace_repositories"
)
_WORKSPACE_REPO_REBUILD_SQL = _WORKSPACE_REPO_DDL.format(
    if_not_exists="", table="workspace_repositories"
)

_WORKSPACE_DDL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT 'local',
    root_path       TEXT NOT NULL,
    default_profile TEXT NOT NULL DEFAULT 'workspace',
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','archived')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_name
    ON workspaces(name) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_root
    ON workspaces(root_path) WHERE status = 'active';

{_WORKSPACE_REPO_SQL}

CREATE TABLE IF NOT EXISTS workspace_sessions (
    session_id   TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    bound_at     TEXT NOT NULL,
    bound_by     TEXT NOT NULL DEFAULT '',
    -- 用户改名后的标题：空串表示沿用「最早一条输入」，不复制一份标题进来
    title        TEXT NOT NULL DEFAULT '',
    -- 归档时间：会话不删，只是从默认视图里收起来
    archived_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_workspace_sessions_ws ON workspace_sessions(workspace_id);
"""

# 占位符在拼接时替换：让建表与「重建表迁移」用同一份 DDL
_WORKSPACE_SQL = _WORKSPACE_DDL.replace("{_WORKSPACE_REPO_SQL}", _WORKSPACE_REPO_SQL)
_SCHEMA = _SCHEMA + _WORKSPACE_SQL

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
    # 网络获取决策（JSON）：谁开了检索、预抓取缺口、走的哪条通路
    ("web_access", "TEXT NOT NULL DEFAULT ''"),
    # 工作区归属（SPEC-0021）：回答「这轮 run 用了哪个代码库」。
    # 空串 = 老记录或未绑定工作区，读取方不得当成某个默认工作区。
    ("workspace_id", "TEXT NOT NULL DEFAULT ''"),
)

# session_states 上后加的列：会话级模型参数覆盖（JSON；空串 = 无覆盖）。
# 必须落库：SessionRegistry 是 LRU，会话被回收后从 DB 重建，
# 覆盖只在内存里的话一次淘汰就丢了。
# workspace_id 是绑定审计列：改变归属的唯一入口是 workspace_sessions，
# 这里只跟着写，避免「快照说 A、绑定表说 B」的第二套真相。
_SESSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("model_config", "TEXT NOT NULL DEFAULT ''"),
    ("workspace_id", "TEXT NOT NULL DEFAULT ''"),
)

# workspace_sessions 上后加的列：改名与归档都挂在绑定行上，
# 因为「会话」本身没有独立记录（唯一真相是 runs 的 session_id）。
_WORKSPACE_SESSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("title", "TEXT NOT NULL DEFAULT ''"),
    ("archived_at", "TEXT"),
)

# session_memories 上后加的列：仅审计（中期仍按 session_id 隔离，不跨会话读）。
_MEMORY_COLUMNS: tuple[tuple[str, str], ...] = (("workspace_id", "TEXT NOT NULL DEFAULT ''"),)

# spans 上后加的列：逐调用的 token 分拆与缓存命中，用来回答「哪一步开始全冷」。
_SPAN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_creation_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    # run 内单调序号与父 span：列表与泳道共用同一份顺序，工具能追溯到发起它的模型
    ("seq", "INTEGER NOT NULL DEFAULT 0"),
    ("parent_id", "INTEGER"),
    # 首 token 延迟：只对模型 span 有意义，老库补列后是 0，统计时必须排除
    ("ttft_ms", "INTEGER NOT NULL DEFAULT 0"),
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
    """建表（幂等），给老库补列，并把 spans 的 type CHECK 升级到含 memory。"""
    conn.executescript(_SCHEMA)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """按 PRAGMA 结果补列。已存在的列跳过，重复执行安全。"""
    _ensure_columns(conn, "runs", _RUN_COLUMNS)
    # 先补列再重建：重建时 INSERT ... SELECT 需要两端列集一致
    _ensure_columns(conn, "spans", _SPAN_COLUMNS)
    _ensure_span_types(conn)
    _ensure_columns(conn, "session_states", _SESSION_COLUMNS)
    _ensure_columns(conn, "session_memories", _MEMORY_COLUMNS)
    _ensure_columns(conn, "workspace_sessions", _WORKSPACE_SESSION_COLUMNS)
    # 老库的 source CHECK 只有 local/git：新增 worktree 要重建表
    _ensure_repository_sources(conn)

    _ensure_columns(conn, "long_term_memories", _LONG_TERM_COLUMNS)
    # 索引在建表语句之后、补列之前都建不了：老库没有 norm_text。
    # 用 execute 而不是 executescript：后者会先隐式提交，破坏调用方的事务
    conn.execute(_LONG_INDEX_SQL.strip().rstrip(";"))


def _ensure_repository_sources(conn: sqlite3.Connection) -> None:
    """老库的 `workspace_repositories.source` CHECK 只有 local/git：重建表放开 worktree。

    与 `_ensure_span_types` 同一套办法（SQLite 改不了 CHECK）：新建、复制、改名，
    整个动作在调用方的事务里，失败回滚不留半张表。`source` 已含 worktree 时是空操作。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='workspace_repositories'"
    ).fetchone()
    if row is None or "'worktree'" in str(row["sql"] or ""):
        return
    columns = (
        "workspace_id, source, managed, remote_url, branch, base_commit, "
        "credential_ref, detected_git, created_at"
    )
    conn.execute("ALTER TABLE workspace_repositories RENAME TO workspace_repositories_legacy")
    conn.execute(_WORKSPACE_REPO_REBUILD_SQL)
    conn.execute(
        f"INSERT INTO workspace_repositories ({columns}) SELECT {columns} "
        "FROM workspace_repositories_legacy"
    )
    conn.execute("DROP TABLE workspace_repositories_legacy")


def _ensure_span_types(conn: sqlite3.Connection) -> None:
    """老库的 spans.type CHECK 不含 memory：重建表并原样复制旧行。

    SQLite 不支持修改 CHECK，只能新建、复制、改名。整个动作在调用方的
    事务里执行；失败会回滚，不会留下半张表。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='spans'"
    ).fetchone()
    if row is None or "'memory'" in str(row["sql"] or ""):
        return
    conn.execute("ALTER TABLE spans RENAME TO spans_legacy")
    # 索引跟随旧表改名，名字会冲突，先删掉再建
    conn.execute("DROP INDEX IF EXISTS idx_spans_run")
    # 逐条执行（不用 executescript：它会先隐式提交，破坏事务原子性）
    for statement in _SPANS_DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
    columns = ", ".join(_SPAN_COPY_COLUMNS)
    conn.execute(f"INSERT INTO spans ({columns}) SELECT {columns} FROM spans_legacy")
    conn.execute("DROP TABLE spans_legacy")


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    """给指定表补齐缺失的列（表名与 DDL 都是模块内常量，不含外部输入）。"""
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
