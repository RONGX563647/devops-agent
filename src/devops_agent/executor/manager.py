"""执行面生命周期 —— 选择、初始化、复用、关闭（薄层，不复制容器逻辑）。

职责只有四件：

1. `ensure()`：按 `(mode, host_workdir, image)` 缓存已初始化的执行目标，同 key
   并发只创建一次；
2. `close()` / `close_all()`：调用 `workspace.close()`，异常只记录不抛出；
3. `reap_idle()`：回收空闲的可回收工作区（评测 case），聊天主工作区不回收；
4. `docker_available()`：启动探测 Docker daemon，失败返回原因而不是抛给调用方。

为什么不用 `agentscope.app._manager.DockerWorkspaceManager`：它位于私有模块，
隔离键是 `(user_id, agent_id)`，与本仓库「按 workdir 复用 + 评测按 case 销毁」
不一致；直接依赖私有 API 会在小版本升级时断。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentscope.workspace import WorkspaceBase

from devops_agent.config import SandboxPolicy
from devops_agent.executor.boundary import build_docker_workspace, build_local_workspace
from devops_agent.executor.target import ExecutionTarget, build_target

logger = logging.getLogger(__name__)

Mode = Literal["local", "docker"]

# Docker 探测超时：daemon 卡死时不要让启动无限等待
_PROBE_TIMEOUT_S = 5.0

# 失败原因码（与 SPEC-0018 §8 的失败语义矩阵一致）
CODE_UNAVAILABLE = "sandbox_unavailable"
CODE_IMAGE_BUILD_FAILED = "sandbox_image_build_failed"
CODE_START_FAILED = "sandbox_start_failed"
CODE_TIMEOUT = "sandbox_timeout"


class SandboxUnavailable(RuntimeError):
    """`required=true` 时初始化失败：调用方必须拒绝服务，不得回落宿主。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(slots=True)
class _Entry:
    """缓存项：执行目标 + 最近使用时间 + 是否可被 TTL 回收。"""

    target: ExecutionTarget
    last_used: float
    reapable: bool


def docker_available() -> tuple[bool, str]:
    """探测 Docker daemon 是否可用；返回 `(可用, 不可用原因)`。

    先看 CLI 是否在 PATH，再跑 `docker info`：只检查 CLI 会把「装了客户端但
    daemon 没起」误判为可用，随后在容器创建时才失败，错误出现在更难解释的位置。
    """
    if shutil.which("docker") is None:
        return False, "docker_unavailable: 未找到 docker 命令"
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker_unavailable: {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit {result.returncode}"
        return False, f"docker_unavailable: {detail}"
    return True, ""


def _classify(exc: BaseException) -> str:
    """把初始化异常归类成失败原因码（用于 `/health` 与 PR 描述的排障）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return CODE_TIMEOUT
    if "image" in text or "build" in text:
        return CODE_IMAGE_BUILD_FAILED
    return CODE_START_FAILED


WorkspaceFactory = Callable[[Mode, Path, SandboxPolicy], WorkspaceBase]


def _default_factory(mode: Mode, host_workdir: Path, policy: SandboxPolicy) -> WorkspaceBase:
    """按模式构造 workspace（构造不启动容器）。"""
    if mode == "docker":
        docker = policy.docker
        return build_docker_workspace(
            base_image=docker.base_image,
            host_workdir=str(host_workdir),
            env=dict(docker.env),
            extra_pip=docker.extra_pip,
            gateway_port=docker.gateway_port,
        )
    return build_local_workspace(str(host_workdir))


class SandboxManager:
    """按 workdir 管理执行面的初始化、复用与关闭。"""

    def __init__(
        self,
        policy: SandboxPolicy,
        *,
        workspace_factory: WorkspaceFactory | None = None,
        probe: Callable[[], tuple[bool, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self._factory = workspace_factory or _default_factory
        self._probe = probe or docker_available
        self._clock = clock
        self._entries: dict[tuple[str, str, str], _Entry] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def ensure(self, host_workdir: str | Path, *, reapable: bool = False) -> ExecutionTarget:
        """取得（必要时创建）该工作目录的执行目标；同 key 并发只初始化一次。

        1) 解析请求模式，必要时探测 Docker（失败按 required/auto 决定回落或拒绝）；
        2) 命中缓存直接返回并刷新最近使用时间；
        3) 未命中则在 per-key 锁内创建并初始化，失败不写缓存。
        """
        host = Path(host_workdir).expanduser().resolve()
        mode, reason = await self._resolve_mode()
        key = self._key(mode, host)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_used = self._clock()
                entry.reapable = entry.reapable and reapable
                return entry.target
            target = await self._create(mode, host, reason)
            self._entries[key] = _Entry(target, self._clock(), reapable)
            return target

    async def _resolve_mode(self) -> tuple[Mode, str]:
        """解析实际生效模式：`docker/auto` 需要先确认 daemon 可用。"""
        requested = self.policy.mode
        if requested == "local":
            return "local", ""
        ok, why = self._probe()
        if ok:
            return "docker", ""
        if requested == "docker" and self.policy.required:
            raise SandboxUnavailable(CODE_UNAVAILABLE, why)
        return "local", why

    async def _create(self, mode: Mode, host: Path, reason: str) -> ExecutionTarget:
        """构造并初始化 workspace；docker 失败时按配置回落或拒绝。"""
        workspace = self._factory(mode, host, self.policy)
        try:
            await workspace.initialize()
        except Exception as exc:  # noqa: BLE001 - 失败语义由配置决定
            await self._safe_close(workspace)
            if mode != "docker":
                raise
            code = _classify(exc)
            message = f"{code}: {exc}"
            if self.policy.required:
                raise SandboxUnavailable(code, str(exc)) from exc
            logger.warning("Docker 执行面初始化失败，回落本机（%s）", message)
            return await self._create("local", host, message)
        return build_target(workspace, host, degraded_reason=reason)

    def _key(self, mode: Mode, host: Path) -> tuple[str, str, str]:
        image = self.policy.docker.base_image if mode == "docker" else ""
        return mode, str(host), image

    @staticmethod
    async def _safe_close(workspace: WorkspaceBase | None) -> None:
        """关闭 workspace；容器残留只记录，不向调用方抛。"""
        if workspace is None:
            return
        try:
            await workspace.close()
        except Exception:  # noqa: BLE001 - 关闭失败不应掩盖真正的错误
            logger.warning("关闭执行面失败，容器可能残留", exc_info=True)

    async def close(self, host_workdir: str | Path) -> None:
        """关闭该宿主目录对应的所有工作区（评测 case 结束时调用）。"""
        host = str(Path(host_workdir).expanduser().resolve())
        keys = [
            key for key, entry in self._entries.items() if str(entry.target.host_workdir) == host
        ]
        for key in keys:
            entry = self._entries.pop(key)
            await self._safe_close(entry.target.workspace)

    async def close_all(self) -> None:
        """关闭全部工作区；幂等。"""
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            await self._safe_close(entry.target.workspace)

    async def reap_idle(self) -> int:
        """回收空闲超过 TTL 的可回收工作区；返回回收数量。"""
        deadline = self._clock() - self.policy.ttl_seconds
        stale = [
            key
            for key, entry in self._entries.items()
            if entry.reapable and entry.last_used < deadline
        ]
        for key in stale:
            entry = self._entries.pop(key)
            await self._safe_close(entry.target.workspace)
        return len(stale)

    @asynccontextmanager
    async def workspace(self, host_workdir: str | Path) -> AsyncIterator[ExecutionTarget]:
        """按 case 创建/销毁独立工作区（评测路径），退出时立即关闭。"""
        target = await self.ensure(host_workdir, reapable=True)
        try:
            yield target
        finally:
            await self.close(host_workdir)

    def status(self) -> dict[str, object]:
        """`/health.sandbox` 的数据源：模式、降级原因与容器标识。"""
        target = next(iter(self._entries.values()), None)
        effective = target.target.mode if target is not None else "local"
        reason = target.target.degraded_reason if target is not None else ""
        workspace = target.target.workspace if target is not None else None
        return {
            "mode": self.policy.mode,
            "effective": effective,
            "degraded": bool(reason),
            "reason": reason,
            "workspace_id": getattr(workspace, "workspace_id", ""),
            "image": self.policy.docker.base_image if effective == "docker" else "",
            "host_workdir": str(target.target.host_workdir) if target is not None else "",
            "tool_workdir": target.target.tool_workdir if target is not None else "",
            "containers": len(self._entries),
        }


__all__ = [
    "CODE_IMAGE_BUILD_FAILED",
    "CODE_START_FAILED",
    "CODE_TIMEOUT",
    "CODE_UNAVAILABLE",
    "SandboxManager",
    "SandboxUnavailable",
    "docker_available",
]
