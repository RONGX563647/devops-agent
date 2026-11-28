"""工作区领域模型 —— 纯数据，不 import agentscope / fastapi。

一个工作区 = 一个代码库（1:1 仓库来源）+ 一组归属会话（1:N）。
长期记忆是工作区级（`scope='workspace'`），短期/中期仍是会话级，见 SPEC-0021 §7。
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime


def now_iso() -> str:
    """统一秒级 UTC 时间戳；登记、绑定与投影都读它，避免各层格式漂移。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    """生成 `ws_` + 12 位小写 base32；不可变、不复用。"""
    raw = base64.b32encode(secrets.token_bytes(8)).decode("ascii").lower().rstrip("=")
    return f"ws_{raw[:12]}"


@dataclass(frozen=True, slots=True)
class Workspace:
    """工作区登记。`root_path` 是 realpath，也是绑定会话的执行目录。"""

    id: str
    name: str
    owner: str
    root_path: str
    default_profile: str
    status: str
    created_at: str
    updated_at: str
    archived_at: str | None = None


@dataclass(frozen=True, slots=True)
class Repository:
    """工作区的仓库来源；`managed` 决定删除语义（见 I5）。"""

    workspace_id: str
    source: str
    managed: bool
    remote_url: str = ""
    branch: str = ""
    base_commit: str = ""
    credential_ref: str = ""
    detected_git: bool = False
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceSession:
    """`session_id → workspace_id` 的归属；一经建立不可静默改投（I2）。"""

    session_id: str
    workspace_id: str
    bound_at: str
    bound_by: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    """运行期最小投影：执行目录 + 让模型知道自己「在哪个工作区」的身份字段。

    身份字段（name/source/remote_url/branch）不是给执行面用的，而是写进每次请求
    末尾的工作区说明 —— 否则模型只能从一句路径猜自己在哪，回答「我没有 clone 过
    仓库」时用户会以为是功能没生效。
    """

    id: str
    root_path: str
    name: str = ""
    source: str = ""
    remote_url: str = ""
    branch: str = ""


@dataclass(frozen=True, slots=True)
class RepoInfo:
    """一次仓库探测的结果；非 git 目录时 `detected=False`。"""

    detected_git: bool = False
    branch: str = ""
    commit: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceDetail:
    """详情接口的领域投影（会话数与长期条数由服务层补齐）。"""

    workspace: Workspace
    repository: Repository
    session_count: int = 0
    long_term_count: int = 0
    extra: dict[str, object] = field(default_factory=dict)


__all__ = [
    "RepoInfo",
    "Repository",
    "Workspace",
    "WorkspaceDetail",
    "WorkspaceRef",
    "WorkspaceSession",
    "new_id",
    "now_iso",
]
