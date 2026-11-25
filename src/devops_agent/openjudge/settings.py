"""评测的可配置项：被测模型、裁判模型、默认开关。

为什么单独存一份、不复用 `settings['model_config']`：

- **被测模型**要能「只为评测指定一个模型」，而不动日常对话的那份配置。评测跑
  几十条用例会把额度打满是常事，把日常配置改过去再改回来是双倍风险。
- **裁判模型**从来没进过配置层：`grade.judge_model()` 只读环境变量，换一次裁判
  要重启进程。裁判和被测模型同源会系统性放水（同源裁判对自己的回答更宽容），
  所以「能换」本身是评测质量的一部分。

取值优先级（高 → 低）：显式参数 > 本表 > 环境变量 / 全局模型配置。
空字符串表示「不覆盖」，不是「用空模型」—— 否则「跟随默认」无法表达。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Any

from devops_agent.openjudge import store

# 与 service.MAX_CONCURRENCY 保持一致；放在这里会形成 settings <-> service 环。
MAX_CONCURRENCY = 4
MIN_TIMEOUT = 30.0
MAX_TIMEOUT = 900.0

#: 完整视图的字段与默认值。给前端用的形状就是它（前端不自己编默认值）。
DEFAULTS: dict[str, Any] = {
    "target_model": "",
    "judge_model": "",
    "judge": False,
    "concurrency": 2,
    "timeout": 180.0,
}

# 模型名走 OpenAI 兼容网关，形如 `deepseek-v4.1-flash` / `qwen3.8-flash`。
# 允许 `:` `/` 是为了 `provider/model` 与 `model:tag` 两类写法。
_NAME_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")


class SettingsError(ValueError):
    """界面传了不合法的评测配置；由接口层转 422，不落库。"""


def load(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """读当前配置（带默认值）。库不可用时返回默认值，不让读接口 500。"""
    try:
        if conn is None:
            with closing(store.connect()) as own:
                store.init_db(own)
                return load(own)
        row = conn.execute("SELECT payload_json FROM oj_settings WHERE id = 1").fetchone()
    except (sqlite3.Error, OSError):
        return dict(DEFAULTS)
    if row is None:
        return dict(DEFAULTS)
    try:
        stored = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        return dict(DEFAULTS)
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def validate(patch: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化一份 patch；未知字段直接拒绝而不是静默丢弃。

    每个字段一个校验函数（见 `_CHECKERS`）：所有字段的规则互不相关，塞进一个
    `if/elif` 链会让分支数随字段线性增长，也会让「新增一个配置项」变成改动一个
    长函数。未知字段不能静默丢弃 —— 前端笔误（`judge_model_name`）必须报错，
    否则用户会以为改成功了。
    """
    unknown = set(patch) - set(DEFAULTS)
    if unknown:
        raise SettingsError(f"未知字段：{sorted(unknown)}")
    return {key: _CHECKERS[key](key, value) for key, value in patch.items()}


def _model_name(field: str, value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SettingsError(f"{field} 必须是字符串")
    name = value.strip()
    if name and not _NAME_RE.match(name):
        raise SettingsError(f"{field} 含非法字符（只允许字母数字与 . _ : / -，长度 ≤120）")
    return name


def _flag(field: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise SettingsError(f"{field} 必须是布尔值")
    return value


def _concurrency(field: str, value: Any) -> int:
    # bool 是 int 的子类：`{"concurrency": True}` 必须被挡掉，否则会静默变成 1。
    if not isinstance(value, int) or isinstance(value, bool):
        raise SettingsError(f"{field} 必须是整数")
    if not 1 <= value <= MAX_CONCURRENCY:
        raise SettingsError(f"{field} 必须在 1..{MAX_CONCURRENCY} 之间")
    return value


def _timeout(field: str, value: Any) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise SettingsError(f"{field} 必须是数字")
    if not MIN_TIMEOUT <= float(value) <= MAX_TIMEOUT:
        raise SettingsError(f"{field} 必须在 {MIN_TIMEOUT:.0f}..{MAX_TIMEOUT:.0f} 秒之间")
    return float(value)


#: 字段名 → 校验函数。键集合必须与 `DEFAULTS` 一致（测试有断言）。
_CHECKERS: dict[str, Callable[[str, Any], Any]] = {
    "target_model": _model_name,
    "judge_model": _model_name,
    "judge": _flag,
    "concurrency": _concurrency,
    "timeout": _timeout,
}


def save(patch: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """把已校验的 patch 合并进配置并落库，返回合并后的完整视图。"""
    if conn is None:
        with closing(store.connect()) as own:
            store.init_db(own)
            return save(patch, conn=own)
    values = {**load(conn), **validate(patch)}
    conn.execute(
        """
        INSERT INTO oj_settings (id, payload_json, updated_at) VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json,
                                      updated_at = excluded.updated_at
        """,
        (json.dumps(values, ensure_ascii=False, sort_keys=True), store.now_iso()),
    )
    conn.commit()
    return values


def resolved(target_model: str = "", judge_model: str = "") -> dict[str, str]:
    """最终会真正使用的两个模型名（显式参数 > 本表 > env / 全局配置）。

    空字符串意味着「哪儿都没配」—— 由调用方决定是报错（裁判缺模型）还是
    回落到框架默认（被测模型走 `effective_params()`）。
    """
    values = load()
    env_model = _env("DEVOPS_AGENT_MODEL")
    target = target_model.strip() or values["target_model"] or _global_model() or env_model
    judge = (
        judge_model.strip() or values["judge_model"] or _env("DEVOPS_AGENT_JUDGE_MODEL") or target
    )
    return {"target_model": target, "judge_model": judge}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _global_model() -> str:
    """全局模型配置里的 model 名（`/api/model-config` 改的就是它）。"""
    try:
        from devops_agent.model_settings import effective_params

        return str(effective_params().name or "")
    except Exception:  # pragma: no cover - 配置层不可用时不该挡住评测
        return ""
