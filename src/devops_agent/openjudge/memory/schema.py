"""记忆评测的建表语句（SPEC §11.1）。

与 openjudge 共用同一个 SQLite 文件，但前缀 `oj_memory_` 独立成表：
记忆批次的主键是 `scenario × variant × trial`，与 `oj_cases` 的 `case × trial`
不同；混表会让所有现有查询多一个 suite 分支。
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS oj_memory_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_name TEXT NOT NULL DEFAULT '',
    suite_sha TEXT NOT NULL DEFAULT '',
    plan_sha TEXT NOT NULL DEFAULT '',
    config_sha TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'deterministic',
    oj_version TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'done',
    verdict TEXT NOT NULL DEFAULT 'insufficient',
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS oj_memory_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    overrides_json TEXT NOT NULL DEFAULT '{}',
    config_sha TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS oj_memory_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    case_id TEXT NOT NULL,
    track TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'must_pass',
    variant TEXT NOT NULL,
    trial INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    rule_passed INTEGER NOT NULL DEFAULT 1,
    probe_total INTEGER NOT NULL DEFAULT 0,
    probe_correct INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    probe_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    memory_events_json TEXT NOT NULL DEFAULT '[]',
    trace_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_oj_memory_cases_batch ON oj_memory_cases(batch_id);
CREATE INDEX IF NOT EXISTS idx_oj_memory_cases_run ON oj_memory_cases(run_id);
CREATE TABLE IF NOT EXISTS oj_memory_probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_row_id INTEGER NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    expected_json TEXT NOT NULL DEFAULT '[]',
    answer TEXT NOT NULL DEFAULT '',
    correct INTEGER NOT NULL DEFAULT 0,
    judge_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_oj_memory_probes_case ON oj_memory_probes(case_row_id);
"""
