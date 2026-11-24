"""会话级模型参数覆盖与部署默认（settings KV）。

与整份快照分开读写：快照是 AgentState 的大对象，参数覆盖是十几个小键，
分开存才能在「只改一个温度」时不动对话历史。
"""

from __future__ import annotations

import json
import sqlite3

from .db import logger

MODEL_CONFIG_KEY = "model_config"


def save_session_model_config(
    conn: sqlite3.Connection, session_id: str, config: dict[str, object], *, updated_at: str
) -> None:
    """写会话级参数覆盖；空 dict 等价于清空。会话行不存在时一并建出来。"""
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True) if config else ""
    conn.execute(
        """INSERT INTO session_states (session_id, state_json, permission, updated_at, model_config)
           VALUES (?, '', '', ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             model_config=excluded.model_config,
             updated_at=excluded.updated_at""",
        (session_id, updated_at, payload),
    )


def load_session_model_config(conn: sqlite3.Connection, session_id: str) -> dict[str, object]:
    """读会话级覆盖；没有、为空串或 JSON 损坏时返回空 dict（当作无覆盖）。"""
    row = conn.execute(
        "SELECT model_config FROM session_states WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        return {}
    raw = str(row["model_config"] or "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("会话 %s 的 model_config 不是合法 JSON，按无覆盖处理", session_id)
        return {}
    return data if isinstance(data, dict) else {}


def load_setting(conn: sqlite3.Connection, key: str) -> tuple[dict[str, object], int] | None:
    """读一条 KV 配置，返回 (值, 版本号)；不存在返回 None。"""
    row = conn.execute("SELECT value, version FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(str(row["value"] or "{}"))
    except json.JSONDecodeError:
        logger.warning("settings[%s] 不是合法 JSON，按空配置处理", key)
        data = {}
    return (data if isinstance(data, dict) else {}), int(row["version"] or 1)


def save_setting(
    conn: sqlite3.Connection,
    key: str,
    value: dict[str, object],
    *,
    expected_version: int | None,
    updated_at: str,
    updated_by: str = "local",
) -> int | None:
    """写一条 KV 配置，返回新版本号；版本不匹配返回 None（调用方回 409）。

    乐观并发与「跨进程失效信号」共用 version 一列：另一个进程读到的版本变了
    就知道该重载，不需要额外的通知机制。
    """
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if expected_version is None:
        conn.execute(
            """INSERT INTO settings (key, value, version, updated_at, updated_by)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, version=settings.version+1,
                 updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
            (key, payload, updated_at, updated_by),
        )
    else:
        # 一条语句覆盖两种情形：没有记录时插入（version=0 → 1），
        # 有记录且版本匹配时更新。版本不匹配则 WHERE 不成立、影响 0 行。
        cur = conn.execute(
            """INSERT INTO settings (key, value, version, updated_at, updated_by)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, version=settings.version+1,
                 updated_at=excluded.updated_at, updated_by=excluded.updated_by
               WHERE settings.version = ?""",
            (key, payload, updated_at, updated_by, expected_version),
        )
        if cur.rowcount == 0:
            return None
    row = conn.execute("SELECT version FROM settings WHERE key=?", (key,)).fetchone()
    return int(row["version"]) if row is not None else None


def current_setting_version(conn: sqlite3.Connection, key: str) -> int:
    """只看版本号；没有记录时为 0（前端拿它做乐观并发的第一次写入）。"""
    saved = load_setting(conn, key)
    return saved[1] if saved is not None else 0
