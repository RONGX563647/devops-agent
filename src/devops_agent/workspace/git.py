"""Git 适配 —— 只走受控子进程，不写 `.git/config` 的凭据。

四条约束（SPEC-0021 §5.3 / §12）：
1. 远端 URL 只接受 https / ssh / `git@host:path`，拒绝 `file://` 与本地路径；
2. 凭据只经临时 `GIT_ASKPASS` 注入，不落 argv、不写 credential.helper；
3. clone 后 `core.hooksPath=/dev/null`，不让仓库钩子执行任意代码；
4. 所有子进程都带超时与输出截断，失败转成 `WorkspaceError`。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from devops_agent.workspace import errors
from devops_agent.workspace.models import RepoInfo

_TIMEOUT_ENV = "DEVOPS_AGENT_GIT_TIMEOUT"
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  *sername*) echo x-access-token ;;
  *) printf '%s\\n' "$DEVOPS_AGENT_GIT_SECRET" ;;
esac
"""
_REMOTE_RE = re.compile(r"^git@[^:\s]+:[^\s]+$")


def git_available() -> bool:
    """部署环境是否装了 git；缺失时 Git 导入降级为不可用。"""
    return shutil.which("git") is not None


def validate_remote_url(url: str) -> str:
    """只放行 https / ssh / `git@host:path`；其余（含 file://、本地路径）一律拒。"""
    text = str(url or "").strip()
    if not text:
        raise errors.WorkspaceError(errors.SOURCE_INVALID, "remote_url 不能为空")
    if text.startswith("https://") or text.startswith("ssh://"):
        return text
    if _REMOTE_RE.match(text):
        return text
    raise errors.WorkspaceError(
        errors.SOURCE_INVALID, "只支持 https:// / ssh:// / git@host:path 三种远端形态"
    )


def _env_with_secret(secret: str, askpass: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if askpass is not None:
        env["GIT_ASKPASS"] = str(askpass)
        env["DEVOPS_AGENT_GIT_SECRET"] = secret
    return env


def _askpass(secret: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", prefix="devops-agent-askpass-", suffix=".sh", delete=False, encoding="utf-8"
    )
    handle.write(_ASKPASS_SCRIPT)
    handle.close()
    path = Path(handle.name)
    path.chmod(0o700)
    return path


def _run(
    args: list[str], *, timeout: int, secret: str = "", askpass: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """跑一个 git 子进程；超时 / 缺失命令都转成 `git` 错误码。"""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_env_with_secret(secret, askpass),
        )
    except FileNotFoundError as exc:
        raise errors.WorkspaceError(errors.GIT_FAILED, "部署环境缺少 git") from exc
    except subprocess.TimeoutExpired as exc:
        raise errors.WorkspaceError(errors.GIT_FAILED, f"git 超时（{timeout}s）") from exc


def _tail(text: str, limit: int = 800) -> str:
    return (text or "").strip()[-limit:]


def detect(root: Path, *, timeout: int = 10) -> RepoInfo:
    """探测目录本身是不是 git 工作区，并取当前分支与 HEAD commit。"""
    if not git_available():
        return RepoInfo()
    probe = _run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"], timeout=timeout)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return RepoInfo()
    branch = _run(["git", "-C", str(root), "branch", "--show-current"], timeout=timeout)
    commit = _run(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=timeout)
    return RepoInfo(
        detected_git=True,
        branch=branch.stdout.strip() if branch.returncode == 0 else "",
        commit=commit.stdout.strip() if commit.returncode == 0 else "",
    )


def clone(
    url: str,
    dest: Path,
    *,
    branch: str = "",
    depth: int = 1,
    credential: str = "",
    timeout: int = 120,
) -> RepoInfo:
    """把远端克隆到受管目录，禁用钩子并返回仓库元信息。

    失败时删除半成品目录：留下一个半截仓库会让下一次「同名创建」误判路径占用。
    """
    if not git_available():
        raise errors.WorkspaceError(errors.GIT_FAILED, "部署环境缺少 git")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "clone", "--no-tags"]
    if branch:
        args += ["--single-branch", "--branch", branch]
    if depth and depth > 0:
        args += ["--depth", str(depth)]
    args += [url, str(dest)]
    askpass = _askpass(credential) if credential else None
    try:
        result = _run(args, timeout=timeout, secret=credential, askpass=askpass)
    finally:
        if askpass is not None:
            askpass.unlink(missing_ok=True)
    if result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise errors.WorkspaceError(errors.GIT_FAILED, f"git clone 失败: {_tail(result.stderr)}")
    # 钩子必须在任何可执行动作前禁用；失败不影响仓库可用，只是少一层保护
    _run(["git", "-C", str(dest), "config", "core.hooksPath", "/dev/null"], timeout=timeout)
    info = detect(dest)
    if not info.detected_git:
        shutil.rmtree(dest, ignore_errors=True)
        raise errors.WorkspaceError(errors.GIT_FAILED, "克隆结果不是可用的 git 工作区")
    return RepoInfo(
        detected_git=True,
        branch=info.branch or branch,
        commit=info.commit,
    )


def validate_branch(name: str) -> str:
    """校验分支名（`git check-ref-format --branch`）；不合法直接拒。"""
    text = str(name or "").strip()
    if not text:
        raise errors.WorkspaceError(errors.SOURCE_INVALID, "branch 不能为空")
    result = _run(["git", "check-ref-format", "--branch", text], timeout=10)
    if result.returncode != 0:
        raise errors.WorkspaceError(errors.SOURCE_INVALID, f"分支名不合法: {text}")
    return text


def main_root(root: Path, *, timeout: int = 10) -> Path | None:
    """从一个工作树反查它的主仓库根；不是 git 目录时返回 None。

    `--git-common-dir` 在主工作区与链接工作树里都指向主仓库的 `.git`，
    所以 `git worktree remove` 能拿到正确的「在哪执行」。这也让删除不必
    额外存一列 origin（表结构不动）。
    """
    if not git_available():
        return None
    result = _run(["git", "-C", str(root), "rev-parse", "--git-common-dir"], timeout=timeout)
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    common = Path(raw)
    if not common.is_absolute():
        common = (root / common).resolve()
    return common.parent if common.name == ".git" else None


def worktree_add(
    repo_root: Path,
    dest: Path,
    *,
    branch: str,
    base: str = "HEAD",
    timeout: int = 120,
) -> RepoInfo:
    """在本地仓库上开一个工作树，返回它的元信息。

    为什么不用「复制目录」：复制会带走未提交改动、把 .git 复制一份导致两边
    各说各话；工作树共用一个对象库，主检出不受影响，删掉工作树也不动用户的
    工作副本。分支默认新建，同名分支已存在时直接复用它（重试一次）。
    """
    if not git_available():
        raise errors.WorkspaceError(errors.GIT_FAILED, "部署环境缺少 git")
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        ["git", "-C", str(repo_root), "worktree", "add", "-b", branch, str(dest), base],
        timeout=timeout,
    )
    if result.returncode != 0 and "already exists" in (result.stderr or ""):
        result = _run(
            ["git", "-C", str(repo_root), "worktree", "add", str(dest), branch],
            timeout=timeout,
        )
    if result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise errors.WorkspaceError(
            errors.GIT_FAILED, f"git worktree add 失败: {_tail(result.stderr)}"
        )
    _run(["git", "-C", str(dest), "config", "core.hooksPath", "/dev/null"], timeout=timeout)
    info = detect(dest)
    return RepoInfo(detected_git=True, branch=info.branch or branch, commit=info.commit)


def worktree_remove(repo_root: Path, dest: Path, *, timeout: int = 60) -> None:
    """注销并删除工作树；主仓库已不可用时退回直接删目录。"""
    if git_available() and repo_root.is_dir():
        _run(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(dest)],
            timeout=timeout,
        )
        _run(["git", "-C", str(repo_root), "worktree", "prune"], timeout=timeout)
    if dest.exists():
        remove_managed_dir(dest)


def status_porcelain(root: Path, *, timeout: int = 20) -> list[str]:
    """取工作区脏文件列表；非 git 目录返回空。"""
    result = _run(["git", "-C", str(root), "status", "--porcelain"], timeout=timeout)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def diff(root: Path, *, timeout: int = 20) -> str:
    """取未提交改动的 patch；非 git 目录返回空串。"""
    result = _run(["git", "-C", str(root), "diff"], timeout=timeout)
    return result.stdout if result.returncode == 0 else ""


def write_delete_patch(root: Path, workspace_id: str, *, timeout: int = 20) -> Path:
    """把脏克隆的改动落成 patch，再允许删除目录（§8.2）。"""
    artifacts = Path(__file__).resolve().parents[3] / "workspace" / "artifacts" / workspace_id
    artifacts.mkdir(parents=True, exist_ok=True)
    target = artifacts / f"delete-{int(time.time())}.patch"
    target.write_text(diff(root, timeout=timeout), encoding="utf-8")
    return target


def _relax_permissions(root: Path) -> None:
    """清掉目录树的只读位：Windows 上 git 对象是只读的，`rmtree` 会被拒绝。

    目录必须保留 `+x`：只给读写会让 `os.scandir` 进不去，在 POSIX 上照样删不掉。
    不用 `onerror` / `onexc` 回调：前者自 3.12 起已弃用、后者 3.12 才有，
    而仓库要同时跑 3.11 与 3.13。
    """
    if not root.exists():
        return
    file_mode = stat.S_IREAD | stat.S_IWRITE
    dir_mode = stat.S_IRWXU
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), file_mode)
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), dir_mode)
    os.chmod(root, dir_mode)


def remove_managed_dir(root: Path) -> None:
    """删除受管克隆目录；只用于 `managed=true`，本地引用目录永不经过这里。"""
    _relax_permissions(root)
    shutil.rmtree(root)


__all__ = [
    "clone",
    "detect",
    "diff",
    "git_available",
    "main_root",
    "remove_managed_dir",
    "status_porcelain",
    "validate_branch",
    "validate_remote_url",
    "worktree_add",
    "worktree_remove",
    "write_delete_patch",
]
