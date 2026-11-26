"""OpenJudge SQLite 的幂等迁移。

`SCHEMA` 里的 `CREATE TABLE IF NOT EXISTS` 不会给已经存在的老库补列，
所以每次开库都要按 PRAGMA 现状补一次；重复执行必须是安全的。
"""

from __future__ import annotations

import sqlite3


def migrate_case_columns(conn: sqlite3.Connection) -> None:
    """老库补列：先查 PRAGMA 再 ALTER，重复执行安全。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(oj_cases)").fetchall()}
    for name, kind in (
        ("judge_norm", "REAL"),
        ("rule_passed", "INTEGER"),
        ("violations", "INTEGER"),
    ):
        if name not in existing:
            conn.execute(f"ALTER TABLE oj_cases ADD COLUMN {name} {kind}")
    conn.commit()
