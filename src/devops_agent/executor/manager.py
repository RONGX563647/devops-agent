"""执行面生命周期 —— 选择、初始化、复用、关闭（薄层，不复制容器逻辑）。

职责只有四件：

1. `ensure()`：按 `(mode, host_workdir, image)` 缓存已初始化的执行目标，同 key
   并发只创建一次；
2. `close()` / `close_all()`：调用 `workspace.close()`（opensandbox 即 pause），
   异常只记录不抛出；
3. `reap_idle()`：回收空闲的可回收工作区（评测 case），聊天主工作区不回收；
4. `docker_available()` / `opensandbox_available()`：启动探测，失败返回原因
   而不是抛给调用方；不做网络连通性探测，连通性留给 `initialize()`。

为什么不用 `agentscope.app._manager.DockerWorkspaceManager`：它位于私有模块，
隔离键是 `(user_id, agent_id)`，与本仓库「按 workdir 复用 + 评测按 case 销毁」
不一致；直接依赖私有 API 会在小版本升级时断。
"""

from __future__ import annotations

import asyncio
import importlib.util
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
from devops_agent.executor.boundary import (
    build_docker_workspace,
    build_local_workspace,
    build_opensandbox_workspace,
    opensandbox_workspace_id,
)
from devops_agent.executor.target import ExecutionTarget, build_target

logger = logging.getLogger(__name__)

Mode = Literal["local", "docker", "opensandbox"]

# Docker 探测超时：daemon 卡死时不要让启动无限等待
_PROBE_TIMEOUT_S = 5.0

# 失败原因码（与 SPEC-0018 §8 / SPEC-0019 §6 的失败语义矩阵一致）
CODE_UNAVAILABLE = "sandbox_unavailable"
CODE_IMAGE_BUILD_FAILED = "sandbox_image_build_failed"
CODE_START_FAILED = "sandbox_start_failed"
CODE_TIMEOUT = "sandbox_timeout"
CODE_PACKAGE_MISSING = "sandbox_package_missing"
CODE_ENDPOINT_MISSING = "sandbox_endpoint_missing"
CODE_AUTH_FAILED = "sandbox_auth_failed"
CODE_IMAGE_UNAVAILABLE = "sandbox_image_unavailable"
CODE_BOOTSTRAP_FAILED = "sandbox_bootstrap_failed"

# `init_ms` 超过该阈值即判定走了 bootstrap（spec §10 允许的经验判据，不伪造布尔值）
_BOOTSTRAP_SLOW_MS = 30_000


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


def opensandbox_available(policy: SandboxPolicy) -> tuple[bool, str]:
    """探测 OpenSandbox 后端是否可用；返回 `(可用, 不可用原因)`。

    只做两项本地判断，不发网络请求：启动期等待远端会把「服务慢」变成「进程起不来」，
    连通性 / 认证 / 镜像错误一律留给 `initialize()` 走失败矩阵。
    """
    if importlib.util.find_spec("opensandbox") is None:
        return False, (
            f"{CODE_PACKAGE_MISSING}: 未安装 opensandbox（pip install -e '.[opensandbox]'）"
        )
    if not policy.opensandbox.domain:
        # 空 domain 时 SDK 是否存在可用默认值不属于本仓库能保证的契约；
        # 当作「未配置 endpoint」显式失败，胜过在 initialize 时给出含糊错误。
        return False, f"{CODE_ENDPOINT_MISSING}: 未配置 sandbox.opensandbox.domain"
    return True, ""


def _classify(exc: BaseException, mode: Mode) -> str:
    """把初始化异常归类成失败原因码（用于 `/health` 与排障）。

    docker 与 opensandbox 的错误文案不同，分开归类；顺序是先超时（最确定），
    再按各后端的关键词匹配，都不中就给通用的启动失败。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return CODE_TIMEOUT
    if mode == "opensandbox":
        return _classify_opensandbox(text)
    if "image" in text or "build" in text:
        return CODE_IMAGE_BUILD_FAILED
    return CODE_START_FAILED


def _classify_opensandbox(text: str) -> str:
    """OpenSandbox 特有错误分类（text 已转小写）。"""
    if "bootstrap" in text:
        return CODE_BOOTSTRAP_FAILED
    if any(word in text for word in ("401", "403", "auth", "unauthorized", "permission")):
        return CODE_AUTH_FAILED
    if "image" in text or "manifest" in text:
        return CODE_IMAGE_UNAVAILABLE
    return CODE_START_FAILED


WorkspaceFactory = Callable[[Mode, Path, SandboxPolicy], WorkspaceBase]


def _default_factory(mode: Mode, host_workdir: Path, policy: SandboxPolicy) -> WorkspaceBase:
    """按模式构造 workspace（构造不启动容器 / 沙箱）。"""
    if mode == "docker":
        docker = policy.docker
        return build_docker_workspace(
            base_image=docker.base_image,
            host_workdir=str(host_workdir),
            env=dict(docker.env),
            extra_pip=docker.extra_pip,
            gateway_port=docker.gateway_port,
        )
    if mode == "opensandbox":
        osb = policy.opensandbox
        return build_opensandbox_workspace(
            workspace_id=opensandbox_workspace_id(str(host_workdir)),
            image=osb.image,
            domain=osb.domain,
            protocol=osb.protocol,
            request_timeout_seconds=osb.request_timeout_seconds,
            timeout_seconds=osb.timeout_seconds,
            gateway_port=osb.gateway_port,
            env=dict(osb.env),
            extra_pip=osb.extra_pip,
            resource=dict(osb.resource),
            network_policy=dict(osb.network_policy),
            entrypoint=osb.entrypoint,
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
        opensandbox_probe: Callable[[], tuple[bool, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self._factory = workspace_factory or _default_factory
        self._probe = probe or docker_available
        self._probe_osb = opensandbox_probe or (lambda: opensandbox_available(policy))
        self._clock = clock
        self._entries: dict[tuple[str, str, str], _Entry] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def ensure(self, host_workdir: str | Path, *, reapable: bool = False) -> ExecutionTarget:
        """取得（必要时创建）该工作目录的执行目标；同 key 并发只初始化一次。

        1) 解析请求模式，必要时探测后端（失败按 required/auto 决定回落或拒绝）；
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
        """解析实际生效模式：远程后端先探测，失败按 required/auto 回落。

        `auto` 的优先级固定为 opensandbox → docker → local，每级失败原因拼进
        `degraded_reason`，不允许静默降级。
        """
        requested = self.policy.mode
        if requested == "local":
            return "local", ""
        if requested == "auto":
            return self._resolve_auto()
        ok, why = self._probe_for(requested)
        if ok:
            return requested, ""
        if self.policy.required:
            raise SandboxUnavailable(self._required_code(requested, why), why)
        return "local", why

    def _resolve_auto(self) -> tuple[Mode, str]:
        """`auto` 的固定优先级：opensandbox → docker → local。"""
        ok, why_osb = self._probe_osb()
        if ok:
            return "opensandbox", ""
        ok, why_docker = self._probe()
        if ok:
            return "docker", ""
        return "local", "; ".join(part for part in (why_osb, why_docker) if part)

    def _probe_for(self, mode: Mode) -> tuple[bool, str]:
        """按模式选择探测函数。"""
        return self._probe_osb() if mode == "opensandbox" else self._probe()

    @staticmethod
    def _required_code(mode: Mode, reason: str) -> str:
        """required=true 拒绝服务时的错误码：opensandbox 保留具体原因码。"""
        return _code_of(reason) if mode == "opensandbox" else CODE_UNAVAILABLE

    async def _create(self, mode: Mode, host: Path, reason: str) -> ExecutionTarget:
        """构造并初始化 workspace；远程后端失败时按配置回落或拒绝。"""
        workspace = self._factory(mode, host, self.policy)
        started = time.perf_counter()
        try:
            await workspace.initialize()
        except Exception as exc:  # noqa: BLE001 - 失败语义由配置决定
            await self._safe_close(workspace)
            if mode == "local":
                raise
            code = _classify(exc, mode)
            message = f"{code}: {exc}"
            if self.policy.required:
                raise SandboxUnavailable(code, str(exc)) from exc
            logger.warning("%s 执行面初始化失败，回落本机（%s）", mode, message)
            return await self._create("local", host, message)
        init_ms = int((time.perf_counter() - started) * 1000)
        return build_target(workspace, host, degraded_reason=reason, init_ms=init_ms)

    def _key(self, mode: Mode, host: Path) -> tuple[str, str, str]:
        """缓存键：模式 + 宿主目录 + 镜像；换镜像必须产生新沙箱。"""
        if mode == "docker":
            image = self.policy.docker.base_image
        elif mode == "opensandbox":
            image = self.policy.opensandbox.image
        else:
            image = ""
        return mode, str(host), image

    @staticmethod
    async def _safe_close(workspace: WorkspaceBase | None) -> None:
        """关闭 workspace；容器 / 沙箱残留只记录，不向调用方抛。"""
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
        """`/health.sandbox` 的数据源：模式、降级原因与沙箱标识。

        `reused` 框架未暴露（无法区分 resume / 新建），如实置 null；
        `bootstrap_skipped` 无法直接观测，用 `init_ms` 阈值给出经验判断，
        不伪造布尔值（SPEC-0019 §10）。
        """
        entry = next(iter(self._entries.values()), None)
        effective = entry.target.mode if entry is not None else "local"
        reason = entry.target.degraded_reason if entry is not None else ""
        workspace = entry.target.workspace if entry is not None else None
        init_ms = entry.target.init_ms if entry is not None else 0
        return {
            "mode": self.policy.mode,
            "effective": effective,
            "provider": effective,
            "degraded": bool(reason),
            "reason": reason,
            "workspace_id": getattr(workspace, "workspace_id", ""),
            "sandbox_id": entry.target.sandbox_id if entry is not None else "",
            "image": self._image_for(effective),
            "host_workdir": str(entry.target.host_workdir) if entry is not None else "",
            "tool_workdir": entry.target.tool_workdir if entry is not None else "",
            "containers": len(self._entries),
            "init_ms": init_ms,
            "reused": None,
            "bootstrap_skipped": self._bootstrap_skipped(effective, init_ms),
        }

    def _image_for(self, effective: str) -> str:
        """实际生效后端使用的镜像；local 无镜像。"""
        if effective == "docker":
            return self.policy.docker.base_image
        if effective == "opensandbox":
            return self.policy.opensandbox.image
        return ""

    @staticmethod
    def _bootstrap_skipped(effective: str, init_ms: int) -> bool | None:
        """预配置是否生效的经验判断：远程后端且启动未超过慢路径阈值。"""
        if effective != "opensandbox" or init_ms <= 0:
            return None
        return init_ms < _BOOTSTRAP_SLOW_MS


def _code_of(reason: str) -> str:
    """从探测原因里取失败码（`code: detail` 的 code 部分）。"""
    code = reason.split(":", 1)[0].strip()
    return code or CODE_UNAVAILABLE


__all__ = [
    "CODE_AUTH_FAILED",
    "CODE_BOOTSTRAP_FAILED",
    "CODE_ENDPOINT_MISSING",
    "CODE_IMAGE_BUILD_FAILED",
    "CODE_IMAGE_UNAVAILABLE",
    "CODE_PACKAGE_MISSING",
    "CODE_START_FAILED",
    "CODE_TIMEOUT",
    "CODE_UNAVAILABLE",
    "SandboxManager",
    "SandboxUnavailable",
    "docker_available",
    "opensandbox_available",
]
