"""子进程兼容层：SelectorEventLoop 下 shell / glob / grep 仍然可用。

现场是「Bash 连续两次同一个运行时错误，Glob 也一样」，模型只好用 Read 猜目录。
根因不是命令写错，而是 Windows 上 `uvicorn --reload` 会把事件循环换成
SelectorEventLoop，而它没有 `create_subprocess_exec`。兼容层只在循环不支持时
把命令放到线程里跑（`subprocess.run`），循环支持时完全走原路。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator

import pytest

from devops_agent.executor import subprocess_compat as compat


@pytest.fixture
def restored_backend() -> Iterator[None]:
    """把 patches 与全局标记复原：兼容层是进程级 patch，不能泄漏给别的用例。"""
    from agentscope.tool._builtin import _backend

    original = _backend.LocalBackend.exec_shell
    was_installed = compat._installed
    try:
        yield
    finally:
        _backend.LocalBackend.exec_shell = original  # type: ignore[method-assign]
        compat._installed = was_installed


def test_report_exposes_loop_and_capability() -> None:
    """`/health.sandbox.subprocess` 的三件事：哪种循环、能不能起子进程、装没装兼容层。"""
    payload = compat.report()
    assert set(payload) == {"loop", "supports_subprocess", "exec_available", "patched"}
    assert isinstance(payload["supports_subprocess"], bool)
    # 跑得起来就等于可用：这条断言在两种循环上都成立
    assert payload["exec_available"] is True or not payload["supports_subprocess"]


def test_install_is_idempotent(restored_backend: None) -> None:
    """重复安装不叠 patch（否则每次重载都多包一层）。"""
    from agentscope.tool._builtin import _backend

    compat._installed = False
    assert compat.install() is True
    patched = _backend.LocalBackend.exec_shell
    assert compat.install() is True
    assert _backend.LocalBackend.exec_shell is patched


def test_exec_shell_falls_back_to_threads(restored_backend: None) -> None:
    """循环不支持子进程时，命令仍要真的跑起来并拿到输出。"""
    from agentscope.tool._builtin import _backend

    compat._installed = False
    compat.install()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(compat, "supports_subprocess", lambda: False)
    try:
        backend = _backend.LocalBackend()
        result = asyncio.run(
            backend.exec_shell(
                [sys.executable, "-c", "print('compat-ok')"],
                cwd=None,
                timeout=30,
            )
        )
    finally:
        monkey.undo()
    assert result.exit_code == 0
    assert b"compat-ok" in result.stdout


def test_exec_shell_reports_missing_binary_as_127(restored_backend: None) -> None:
    """可执行文件不存在按 shell 的 127 处理，不抛异常（与原生实现对齐）。"""
    from agentscope.tool._builtin import _backend

    compat._installed = False
    compat.install()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(compat, "supports_subprocess", lambda: False)
    try:
        result = asyncio.run(
            _backend.LocalBackend().exec_shell(["definitely-not-a-real-binary-xyz"])
        )
    finally:
        monkey.undo()
    assert result.exit_code == 127


def test_exec_shell_uses_native_path_when_supported(restored_backend: None) -> None:
    """循环支持子进程时不走线程回落：POSIX 上行为与原来逐字一致。"""
    from agentscope.tool._builtin import _backend

    if not compat.supports_subprocess():  # Windows + Selector 的机器才需要这条
        pytest.skip("当前循环本就起不了子进程")
    compat._installed = False
    compat.install()
    result = asyncio.run(
        _backend.LocalBackend().exec_shell([sys.executable, "-c", "print('native')"], timeout=30)
    )
    assert result.exit_code == 0
    assert b"native" in result.stdout
