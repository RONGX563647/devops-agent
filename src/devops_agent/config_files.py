"""配置文件目录与 YAML 读取 —— 只依赖标准库与 pyyaml 的叶子模块。

为什么单独成文件：`config.py` 要 import `config_store` 的存取层，而存取层也要读
`config/*.yaml` 当基线。若存取层再 import `config.py` 就成环。把「目录在哪、
yaml 怎么读」这两件事摘出来，两边都能用，且都不需要知道对方的模型。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import yaml

CONFIG_DIR_ENV = "DEVOPS_AGENT_CONFIG_DIR"


def config_dir() -> Path:
    """返回配置文件目录，环境变量优先（测试 / 部署可整目录隔离）。"""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "config"


def read_yaml(name: str) -> dict[str, Any]:
    """读一份顶层为映射的 yaml；缺失返回空 dict，顶层不是映射直接报错。

    缺失不报错是刻意的：wheel 不带 config/，写错配置不该让服务起不来；
    但顶层结构错误要吵（`- a` 这种列表根几乎一定是编辑事故）。
    """
    path = config_dir() / name
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 顶层必须是映射")
    return cast(dict[str, Any], raw)


__all__ = ["CONFIG_DIR_ENV", "config_dir", "read_yaml"]
