"""OpenJudge 的建表语句：DDL 与访问逻辑分开，改表不必翻半个 store。

`calibration.SCHEMA` 拼在末尾：校准表与批次表在同一个库里、同一批迁移中生效。
老库补列见 `migrations.py`——新建库与迁移必须同时改，否则新库有列、老库没有。
"""

from __future__ import annotations

from devops_agent.openjudge import calibration

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS oj_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suite_sha TEXT NOT NULL,
    refs_sha TEXT NOT NULL DEFAULT '',
    plan_sha TEXT NOT NULL DEFAULT '',
    rubric_sha TEXT NOT NULL DEFAULT '',
    prompt_sha TEXT NOT NULL DEFAULT '',
    calibration_id INTEGER,
    model TEXT NOT NULL DEFAULT '',
    oj_version TEXT NOT NULL DEFAULT '',
    config_id TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'done',
    verdict TEXT NOT NULL DEFAULT 'insufficient',
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS oj_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    case_id TEXT NOT NULL,
    track TEXT NOT NULL,
    risk TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    passed INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    checks_json TEXT NOT NULL DEFAULT '[]',
    judge_json TEXT NOT NULL DEFAULT '{}',
    trace_json TEXT NOT NULL DEFAULT '{}',
    events_json TEXT NOT NULL DEFAULT '[]',
    input_stages_json TEXT NOT NULL DEFAULT '[]',
    workspace_json TEXT NOT NULL DEFAULT '{}',
    judge_norm REAL,
    rule_passed INTEGER,
    violations INTEGER,
    -- 第几次试验：多次试验时同一 case_id 会有多行，靠它区分。
    trial INTEGER NOT NULL DEFAULT 0,
    -- 用例分层随结果落库：明细表要能标出「必过集失败」，而不只是汇总知道。
    tier TEXT NOT NULL DEFAULT 'must_pass'
);
CREATE INDEX IF NOT EXISTS idx_oj_cases_batch ON oj_cases(batch_id);
CREATE TABLE IF NOT EXISTS oj_rubrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    reviewer TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS oj_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'pending',
    track TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    judge INTEGER NOT NULL DEFAULT 0,
    concurrency INTEGER NOT NULL DEFAULT 1,
    -- 每条用例重复次数：任务详情要能回答「这次跑了几遍」。
    trials INTEGER NOT NULL DEFAULT 1,
    batch_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
-- 单飞：任何时刻只允许一个未完成任务。部分唯一索引在多进程下同样生效，
-- 内存标志位做不到这一点（uvicorn 多 worker 时会各放行一个）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_oj_runs_active
    ON oj_runs(status) WHERE status IN ('pending', 'running');
-- 评测的可配置项（被测模型 / 裁判模型 / 默认开关）。单行表：配置是整体，不是集合。
CREATE TABLE IF NOT EXISTS oj_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""
    + calibration.SCHEMA
)
