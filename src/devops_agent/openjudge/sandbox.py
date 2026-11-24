"""用例沙箱：把 fixture 复制到临时目录，采集工作区终态与 diff。

为什么必须复制：轨迹要复用（同一批次判多次），而用例会写文件；直接在
fixture 上跑，第二条用例看到的就是第一条的结果，失败也会「自己修好」。
临时目录在采集完 diff 后即删除，仓库里只留 fixture 本身。
"""

from __future__ import annotations

import difflib
import hashlib
import shutil
import tempfile
from pathlib import Path

# 只保留文本文件的 diff，二进制/大文件只记哈希，避免轨迹爆炸
_TEXT_LIMIT = 200_000
_DIFF_LIMIT = 6000

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}


def fixture_root() -> Path:
    """fixture 目录：src/devops_agent/openjudge/sandbox.py → 仓库根 / eval。"""
    return Path(__file__).resolve().parents[3] / "eval" / "openjudge" / "fixtures"


def resolve_fixture(name: str) -> Path:
    path = fixture_root() / name
    if not path.is_dir():
        raise FileNotFoundError(f"fixture 不存在: {name}")
    return path


def copy_fixture(name: str, base: Path | None = None) -> Path:
    """复制 fixture 到临时工作区，并额外准备同级 `outside/` 受控目录。

    `outside/` 用于越权用例：它在工作区之外，但仍在临时根目录内，
    用例结束后随父目录一起删除，不会碰到真实文件。
    """
    source = resolve_fixture(name)
    parent = Path(tempfile.mkdtemp(prefix="oj-", dir=str(base) if base else None))
    workdir = parent / "work"
    shutil.copytree(source, workdir)
    outside = fixture_root() / "_outside"
    if outside.is_dir():
        shutil.copytree(outside, parent / "outside")
    return workdir


def cleanup(workdir: Path) -> None:
    """删除临时工作区及其父目录（copy_fixture 建的 oj-* 目录）。"""
    shutil.rmtree(workdir.parent, ignore_errors=True)


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def snapshot(root: Path) -> dict[str, bytes]:
    """读一遍工作区全部文件内容（fixture 很小，直接进内存换 diff 精度）。"""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in _iter_files(root)}


def hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def changed_files(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    """新增 / 修改 / 删除的文件名（排序稳定，供断言和前端展示）。"""
    names = sorted(set(before) | set(after))
    return [name for name in names if before.get(name) != after.get(name)]


def diff_text(before: dict[str, bytes], after: dict[str, bytes]) -> str:
    """统一 diff：只渲染文本文件，超长截断，二进制用「变化了」占位。"""
    chunks: list[str] = []
    for name in changed_files(before, after):
        old = before.get(name, b"")
        new = after.get(name, b"")
        if len(old) > _TEXT_LIMIT or len(new) > _TEXT_LIMIT or b"\0" in old + new:
            chunks.append(f"--- {name}\n+++ {name}\n（二进制或超大文件，已省略内容）")
            continue
        try:
            old_text = old.decode("utf-8").splitlines(keepends=True)
            new_text = new.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            chunks.append(f"--- {name}\n+++ {name}\n（非 UTF-8，已省略内容）")
            continue
        chunks.extend(
            difflib.unified_diff(old_text, new_text, fromfile=f"a/{name}", tofile=f"b/{name}")
        )
    text = "".join(chunks)
    return text[:_DIFF_LIMIT]


def read_state(workdir: Path, path: str) -> bytes | None:
    """按用例里的相对路径读工作区文件；允许 `../` 读用例目录外的受控文件。"""
    target = (workdir / path).resolve()
    try:
        return target.read_bytes()
    except (OSError, ValueError):
        return None
