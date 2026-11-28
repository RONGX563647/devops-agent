"""配置库（SQLite）的路径、开关与建表。

为什么单独一个库而不是复用 `observability.db`：配置的寿命比轨迹长。清理一次运行
记录、重建一次观测库，都不该把「部署者调过的阈值」一起带走；反过来，配置库小到
可以整份备份 / 传阅，也没必要跟着几百 MB 的 span 一起变重。

`DEVOPS_AGENT_CONFIG_MODE=file` 时整层关闭：测试与 CI 只读 yaml，不会写出一个
运行态文件，也不会因为「上一个用例留下的覆盖」串味。模式只认 `db` / `file`：
拼错（如 `dbb`）不再静默当 file —— 那会让「我明明开了库模式」变成排查半天的怪事。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CONFIG_DB_ENV = "DEVOPS_AGENT_CONFIG_DB"
MODE_ENV = "DEVOPS_AGENT_CONFIG_MODE"

VALID_MODES = ("db", "file")
# 等锁上限：写事务是短事务（毫秒级），10 秒足够让并发请求排队而不是立刻失败
LOCK_TIMEOUT_S = 10.0

_DDL = """
CREATE TABLE IF NOT EXISTS config_entries (
    domain          TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    default_at_write TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL,
    updated_by      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (domain, key)
);
CREATE TABLE IF NOT EXISTS config_domains (
    domain     TEXT PRIMARY KEY,
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    domain    TEXT NOT NULL,
    key       TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    version   INTEGER NOT NULL,
    at        TEXT NOT NULL,
    by        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_config_audit_domain ON config_audit(domain, id DESC);
"""


def mode() -> str:
    """`db`（默认）或 `file`（只用 yaml，测试 / CI 用）。非法值直接报错。"""
    raw = os.environ.get(MODE_ENV, "db").strip().lower() or "db"
    if raw not in VALID_MODES:
        raise ValueError(f"{MODE_ENV}={raw!r} 不合法：只能是 {' / '.join(VALID_MODES)}")
    return raw


def db_enabled() -> bool:
    return mode() == "db"


def db_ready() -> bool:
    """库存在且模式允许 —— 读路径不该为了「看看有没有覆盖」创建一个空文件。"""
    return db_enabled() and db_path().exists()


def db_path() -> Path:
    """配置库路径：环境变量优先，默认 `workspace/config.db`（与运行态同目录）。"""
    override = os.environ.get(CONFIG_DB_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "workspace" / "config.db"


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """打开连接并保证提交/回滚与关闭；行可按列名访问。"""
    target = path if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=LOCK_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """写事务：`BEGIN IMMEDIATE` 先拿写锁，把「读版本 → 校验 → 写」变成一个原子段。

    不用 `connect()` 的原因：它的默认隔离级别是「遇到 DML 才隐式 BEGIN」，读版本
    发生在事务之外，两个写者会同时读到 v1 再各写 v1。`BEGIN IMMEDIATE` 一进来就
    要写锁，后到者排队（最多 `LOCK_TIMEOUT_S`），因此版本号严格单调。
    """
    target = path if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=LOCK_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
    except Exception:
        conn.close()
        raise
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # 事务可能已被 SQLite 自动回滚
            pass
        raise
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


__all__ = [
    "CONFIG_DB_ENV",
    "LOCK_TIMEOUT_S",
    "MODE_ENV",
    "VALID_MODES",
    "connect",
    "db_enabled",
    "db_path",
    "db_ready",
    "init_db",
    "mode",
    "transaction",
]
