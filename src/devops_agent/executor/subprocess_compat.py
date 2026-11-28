"""子进程能力兼容 —— Windows 上 SelectorEventLoop 也能跑 shell / glob / grep。

问题现场（真实会话，一次跑了 56 步）：Bash、Glob、Grep 全部报

    The current event loop doesn't support subprocesses
    (e.g. SelectorEventLoop on Windows), use a ProactorEventLoop instead.

`asyncio` 的结论是对的：Windows 上只有 ProactorEventLoop 实现
`create_subprocess_exec`，Selector 循环里它直接抛 `NotImplementedError`。
而 Selector 循环不是我们选的 —— `uvicorn --reload` / `--workers > 1` 在 Windows
上会主动 `set_event_loop_policy(WindowsSelectorEventLoopPolicy())`（它自己要起
重载子进程）。于是「开发时用 --reload 启动」= 「Agent 的所有文件与命令工具全废」，
表现成模型在那儿反复换写法重试，用户只看到它一直失败。

为什么不直接改回 Proactor 策略：uvicorn 换策略是为了它自己的 reloader，抢回来会
把热重载弄坏；而运行中的循环也没法原地换类型。这里走窄路：只替换 agentscope
`LocalBackend.exec_shell` 这一个入口，循环不支持子进程时改用线程 + `subprocess`。
影响面只有本地执行面，不碰全局 asyncio，也不改变 POSIX / Docker 行为。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)

_SUBPROCESS_HINT = "doesn't support subprocesses"
_installed = False


def supports_subprocess() -> bool:
    """当前事件循环能否 `create_subprocess_exec`；非 Windows 恒为 True。"""
    if sys.platform != "win32":
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 没有运行中的循环：不构成问题
        return True
    return not isinstance(loop, asyncio.SelectorEventLoop)


def loop_kind() -> str:
    """当前循环的类名（`/health` 与启动日志用，便于一眼看出是哪种循环）。"""
    try:
        return type(asyncio.get_running_loop()).__name__
    except RuntimeError:
        return ""


async def _run_via_thread(command: list[str], cwd: str | None, timeout: float | None) -> Any:
    """线程里跑 `subprocess.run`，返回与 `ExecResult` 同形的对象。

    只在没有子进程支持的循环上用。`asyncio.to_thread` 依赖线程池，任何循环都有。
    """
    from agentscope.tool._builtin._backend import ExecResult  # noqa: PLC0415 - 依赖可选

    extra: dict[str, Any] = {}
    if sys.platform == "win32":
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _call() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - 命令由工具层给定，与原生实现同源
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            **extra,
        )

    try:
        done = await asyncio.to_thread(_call)
    except subprocess.TimeoutExpired:
        return ExecResult(exit_code=-1, stdout=b"", stderr=b"timed out")
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        # 与原生实现一致：可执行文件找不到等同于 shell 的 127
        return ExecResult(exit_code=127, stdout=b"", stderr=str(exc).encode("utf-8"))
    return ExecResult(
        exit_code=done.returncode or 0,
        stdout=done.stdout or b"",
        stderr=done.stderr or b"",
    )


def install() -> bool:
    """替换 `LocalBackend.exec_shell`；已装过或依赖不可用时是空操作。"""
    global _installed
    if _installed:
        return True
    try:
        from agentscope.tool._builtin import _backend  # noqa: PLC0415 - 依赖可选
    except Exception:  # noqa: BLE001 - 没有 agentscope 时无需兼容
        logger.debug("未找到 agentscope 本地执行面，跳过子进程兼容", exc_info=True)
        return False

    original = _backend.LocalBackend.exec_shell

    async def exec_shell(
        self: Any,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        if supports_subprocess():
            return await original(self, command, cwd=cwd, timeout=timeout)
        return await _run_via_thread(command, cwd, timeout)

    _backend.LocalBackend.exec_shell = exec_shell  # type: ignore[method-assign]
    _installed = True
    return True


def report() -> dict[str, Any]:
    """能力自述：`/health` 用它说明「工具能不能真的执行命令」。"""
    capable = supports_subprocess()
    return {
        "loop": loop_kind(),
        "supports_subprocess": capable,
        # 装过兼容层时，结论以线程回落为准：哪怕循环不支持，工具也还能用
        "exec_available": capable or _installed,
        "patched": _installed,
    }


__all__ = ["install", "loop_kind", "report", "supports_subprocess"]
