"""本地目录准入 —— 白名单 + realpath 是「引用本地目录」唯一的防线。

没有容器隔离时，接受任意路径等于把宿主文件系统交给 Agent 工具。所以：
1. `allow_local_import` 默认关；
2. 路径必须 realpath 后落在某个白名单根之下（符号链接逃逸因此自然失效）；
3. 拒绝 `/`、用户 home 根与含 `..` 的原始串。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from devops_agent.workspace import errors


def _real_roots(allowed_roots: Sequence[str]) -> list[Path]:
    roots: list[Path] = []
    for raw in allowed_roots:
        if not str(raw).strip():
            continue
        roots.append(Path(raw).expanduser().resolve())
    return roots


def _within(target: Path, roots: Sequence[Path]) -> bool:
    return any(target == root or target.is_relative_to(root) for root in roots)


def resolve_local_root(raw: str, allowed_roots: Sequence[str]) -> Path:
    """校验并返回本地目录的 realpath；不合法一律抛 `path_invalid`。

    先 expanduser 再 resolve(strict=True)：目录不存在、权限不足都会在这里失败，
    不会等到 Agent 真正执行命令时才报错。
    """
    text = str(raw or "").strip()
    if not text:
        raise errors.WorkspaceError(errors.PATH_INVALID, "路径不能为空")
    # 原始串里的 `..` 一律拒：realpath 虽能化解，但「看起来在跑出白名单」本身
    # 就是调用方想绕路的信号，不该靠归一化兜底。
    if ".." in Path(text).parts:
        raise errors.WorkspaceError(errors.PATH_INVALID, "路径不允许包含 ..")
    try:
        target = Path(text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise errors.WorkspaceError(errors.PATH_INVALID, f"路径不可用: {exc}") from exc
    if not target.is_dir():
        raise errors.WorkspaceError(errors.PATH_INVALID, "必须是目录")
    if target == Path(target.anchor):
        raise errors.WorkspaceError(errors.PATH_INVALID, "不允许引用文件系统根目录")
    if target == Path.home().resolve():
        raise errors.WorkspaceError(errors.PATH_INVALID, "不允许引用用户 home 根目录")
    roots = _real_roots(allowed_roots)
    if not roots:
        raise errors.WorkspaceError(
            errors.LOCAL_DISABLED, "未配置 allowed_local_roots，本地导入不可用"
        )
    if not _within(target, roots):
        raise errors.WorkspaceError(
            errors.PATH_INVALID, "路径不在 allowed_local_roots 白名单内", path=str(target)
        )
    return target


def browse_children(
    raw: str,
    allowed_roots: Sequence[str],
    *,
    limit: int = 200,
) -> tuple[Path, list[dict[str, str]]]:
    """列出白名单内某个目录的子目录（只给名字与路径，不读文件内容）。

    空 `raw` 回落到第一个白名单根；每个子目录都要 realpath 后仍在白名单内，
    否则一个指向外部的符号链接就能把目录名与真实路径泄漏出去。
    """
    roots = _real_roots(allowed_roots)
    if not roots:
        raise errors.WorkspaceError(
            errors.LOCAL_DISABLED, "未配置 allowed_local_roots，目录浏览不可用"
        )
    base = resolve_local_root(raw or str(roots[0]), allowed_roots)
    items: list[dict[str, str]] = []
    try:
        children = sorted(base.iterdir(), key=lambda path: path.name.lower())
    except OSError as exc:
        raise errors.WorkspaceError(errors.PATH_INVALID, f"目录不可读: {exc}") from exc
    for child in children:
        if child.name.startswith(".") or not child.is_dir():
            continue
        try:
            real = child.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not _within(real, roots):
            continue
        items.append({"name": child.name, "path": str(real)})
        if len(items) >= limit:
            break
    return base, items


def real_roots(allowed_roots: Sequence[str]) -> list[str]:
    """白名单根的 realpath 列表（跳过空串）；界面用它做「一键跳转」的起点。"""
    return [str(root) for root in _real_roots(allowed_roots)]


def parent_within(base: Path, allowed_roots: Sequence[str]) -> str | None:
    """`base` 的上级目录，仅当它仍在白名单内时给出。

    「上一级」由后端判定而不是前端拼路径：白名单根的父目录必须返回 None，
    否则界面会给出一个点了必然 400 的按钮。
    """
    roots = _real_roots(allowed_roots)
    parent = base.parent
    if parent == base:  # 盘符根 / 文件系统根，没有上级
        return None
    try:
        real = parent.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return str(real) if _within(real, roots) else None


def default_managed_root() -> Path:
    """受管克隆根：仓库根的 `workspace/workspaces`（已 gitignore）。"""
    return Path(__file__).resolve().parents[3] / "workspace" / "workspaces"


def managed_root(configured: str) -> Path:
    """受管目录根：配置优先，相对路径按仓库根解析，缺省回落到 gitignore 目录。"""
    text = str(configured or "").strip()
    if not text:
        return default_managed_root()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(path))


__all__ = [
    "browse_children",
    "default_managed_root",
    "managed_root",
    "parent_within",
    "real_roots",
    "resolve_local_root",
]
