"""全局默认的落盘层：`config/model.local.yaml`。

为什么要多一个文件：界面上的「保存为部署默认」原先只写 settings 表，
换台机器、清掉 workspace 后就没了，也进不了 diff。本文件是这份配置的可读副本，
并且是读路径上的真相（文件优先于 DB）——手改文件同样生效。

三个约束：
1. 原子写：临时文件 + `os.replace`，半截文件不会被读到；
2. 手改生效：按 mtime + size 失效缓存，改完刷新即可，不需要重启进程
   （`load_model()` 的 `@cache` 只覆盖 model.yaml，不覆盖本文件）；
3. 写失败要吵：静默降级会重新制造「以为保存了其实没有」。
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from devops_agent.config import config_dir
from devops_agent.model_settings.bounds import EDITABLE

logger = logging.getLogger(__name__)

LOCAL_NAME = "model.local.yaml"
LOCK_NAME = ".model.local.lock"
# 拿不到锁就重试这么久；单人本地场景，超过这个时长基本是崩溃残留的锁
LOCK_TIMEOUT_S = 2.0
# 崩溃残留的锁文件超过这个年龄就抢过来
STALE_LOCK_S = 30.0

_HEADER = """\
# 由界面「保存为全局默认」自动生成，不要手工调格式（内容可以改）。
# 优先级：会话覆盖 > 本文件 > config/model.yaml > 环境变量 > 框架默认
# 本文件不入库（.gitignore）：它是运行时产物，也是可读的备份点。
"""

# (路径, mtime_ns, size, values)：文件没动就不重复解析；路径进 key，
# 测试切换 CONFIG_DIR 时不会撞上另一个目录的同尺寸文件。
_cache: tuple[str, int, int, dict[str, Any]] | None = None


def local_path() -> Path:
    """落盘文件位置；跟着 `CONFIG_DIR_ENV` 走，测试可整目录隔离。"""
    return config_dir() / LOCAL_NAME


def _coerce(field: str, value: Any) -> Any:
    """只接受面板能写的标量；`max_iters` 手写成按域映射时取 default。"""
    if field == "max_iters" and isinstance(value, dict):
        return value.get("default")
    return value


def load() -> dict[str, Any]:
    """读本文件的覆盖项。不存在 / 损坏 / 非映射都当作「没有覆盖」。"""
    global _cache
    path = local_path()
    try:
        stat = path.stat()
    except OSError:
        _cache = None
        return {}
    signature = (str(path), stat.st_mtime_ns, stat.st_size)
    if _cache is not None and _cache[:3] == signature:
        return dict(_cache[3])
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        logger.warning("%s 读取失败，按无覆盖处理", path, exc_info=True)
        return {}
    section = raw.get("model", raw) if isinstance(raw, dict) else {}
    if not isinstance(section, dict):
        return {}
    values = {
        key: _coerce(key, value)
        for key, value in section.items()
        if key in EDITABLE and value is not None and _coerce(key, value) is not None
    }
    _cache = (*signature, values)
    return dict(values)


def save(values: dict[str, Any]) -> Path:
    """整份重写覆盖项（不是合并）。调用方负责先合并好再传进来。"""
    global _cache
    path = local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {key: value for key, value in values.items() if key in EDITABLE and value is not None}
    body = yaml.safe_dump({"model": clean}, allow_unicode=True, sort_keys=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(_HEADER + body, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
        _cache = None
    return path


def _acquire(path: Path) -> int:
    """O_EXCL 建锁文件；Windows 与 POSIX 语义一致，不需要额外依赖。"""
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > STALE_LOCK_S:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待配置写锁超时：{path}") from None
            time.sleep(0.05)


@contextlib.contextmanager
def guard() -> Iterator[None]:
    """跨进程写锁：读-改-写要串行，否则两个 PATCH 会互相覆盖。

    锁用 O_EXCL 文件实现：单机单人，不值得引入 filelock 依赖；进程被 kill
    留下的锁靠 STALE_LOCK_S 超时清理。写失败时锁一定释放（finally）。
    """
    path = local_path().with_name(LOCK_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _acquire(path)
    try:
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)
