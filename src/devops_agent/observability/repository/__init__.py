"""SQLite 仓储 —— 可观测性数据的唯一读写入口（拆分为子模块后的门面）。

设计约束（ADR-0001）：
- 只用 stdlib sqlite3，零新依赖；
- 上层经 repository 函数访问，业务代码不直接写 SQL；
- 数据库路径由 DEVOPS_AGENT_DB_PATH 覆盖，默认落在已 gitignore 的 workspace/；
- 表间外键 ON DELETE CASCADE，保证删除 run 时 spans / eval_scores 一并清理。

本模块保持原有公共 API 不变：`from devops_agent.observability import repository`
得到的仍是完整的读写入口，`repository.connect` 等名字逐个可用。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from devops_agent.observability.models import (
    EvalDimension,
    EvalScoreRecord,
    InputStageRecord,
    RunRecord,
    SpanRecord,
)

from .db import (
    DB_PATH_ENV,
    connect,
    default_db_path,
    init_db,
    logger,
)
from .inputs import add_input_stages, list_input_stages
from .pending import active_run_for_session, bump_asks, get_pending, park_run, unpark_run
from .runs import (
    count_runs,
    create_run,
    delete_run,
    finish_run,
    get_run,
    list_runs,
    query_runs,
    summary_rows,
)
from .scores import add_score, all_scores, list_scores
from .sessions import delete_session_state, load_session_state, save_session_state
from .settings import (
    MODEL_CONFIG_KEY,
    current_setting_version,
    load_session_model_config,
    load_setting,
    save_session_model_config,
    save_setting,
)
from .spans import add_span, count_spans_by_type, list_spans, span_stats

__all__ = [
    "DB_PATH_ENV",
    "MODEL_CONFIG_KEY",
    "EvalDimension",
    "EvalScoreRecord",
    "InputStageRecord",
    "Path",
    "RunRecord",
    "SpanRecord",
    "active_run_for_session",
    "add_input_stages",
    "add_score",
    "add_span",
    "all_scores",
    "bump_asks",
    "connect",
    "contextmanager",
    "count_runs",
    "count_spans_by_type",
    "create_run",
    "current_setting_version",
    "default_db_path",
    "delete_run",
    "delete_session_state",
    "finish_run",
    "get_pending",
    "get_run",
    "init_db",
    "json",
    "list_input_stages",
    "list_runs",
    "list_scores",
    "list_spans",
    "load_session_model_config",
    "load_session_state",
    "load_setting",
    "logger",
    "logging",
    "os",
    "park_run",
    "query_runs",
    "save_session_model_config",
    "save_session_state",
    "save_setting",
    "span_stats",
    "sqlite3",
    "summary_rows",
    "unpark_run",
]
