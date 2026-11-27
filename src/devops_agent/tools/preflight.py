"""外部可执行依赖预检 —— 把「工具用不了」提前到启动时可见。

`Grep` 的唯一执行路径是 `LocalBackend.exec_shell(["rg", ...])`：它直接
spawn 进程、**不经过 shell**。因此 `rg` 不在 PATH 时，工具拿到的不是一句
「命令不存在」，而是 `exit 127` 加操作系统错误文本（Windows 上是
`[WinError 2] 系统找不到指定的文件`），最终被包成
`RuntimeError("ripgrep error (code 127)")` 返回给模型。

模型读不懂这层语义。系统提示又建议「工具失败就改用 Grep 重试」，于是它每次
换一个 pattern 再撞同一堵墙 —— 这就是「grep 一直失败」的真实成因：不是检索
逻辑有问题，是进程根本没启动。

探测放在进程内用 :func:`shutil.which`：它与 ``create_subprocess_exec`` 查找
可执行文件走的是同一套 PATH/PATHEXT 规则，所以结论一致。不做缓存 —— 一次
PATH 遍历是微秒级，而缓存会让「装完 rg 不重启」这种正当场景失效。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any

# 依赖外部可执行文件的工具：工具名 → 可执行文件名。
# Grep 之外的五个工具（Bash / Read / Glob / Write / Edit）只用 shell 内建、
# Python 标准库或自身解释器，不在这里登记，免得预检表变成噪音。
_EXTERNAL_BINARIES: dict[str, str] = {
    "grep": "rg",
}

# 安装提示按平台给一条能直接粘进终端的命令：只说「请安装 ripgrep」等于没说。
_INSTALL_HINTS: dict[str, dict[str, str]] = {
    "rg": {
        "nt": "winget install BurntSushi.ripgrep.MSVC",
        "posix": "apt-get install ripgrep（macOS 用 brew install ripgrep）",
    },
}


@dataclass(frozen=True, slots=True)
class ToolReadiness:
    """一个工具的外部依赖就绪情况。

    Attributes:
        tool: 工具名，与 `tools/catalog.py` 注册名一致（小写）。
        binary: 该工具依赖的可执行文件名。
        available: 是否能在当前 PATH 中解析到。
        path: 解析到的绝对路径；不可用时为 None。
        install_hint: 缺失时的安装提示（可直接执行的命令）。
    """

    tool: str
    binary: str
    available: bool
    path: str | None
    install_hint: str

    def as_dict(self) -> dict[str, Any]:
        """转成 JSON 友好的形状（HTTP 层直接用，不需要再包一层）。"""
        return {
            "tool": self.tool,
            "binary": self.binary,
            "available": self.available,
            "path": self.path,
            "install_hint": self.install_hint,
        }


def _hint_for(binary: str) -> str:
    """取该可执行文件的安装提示；未登记时给一句通用说明。"""
    by_platform = _INSTALL_HINTS.get(binary)
    if not by_platform:
        return f"安装 {binary} 并确保它在 PATH 中"
    return by_platform.get(os.name, f"安装 {binary} 并确保它在 PATH 中")


def check_binary(binary: str) -> str | None:
    """解析可执行文件路径；找不到返回 None。

    单独抽出来是为了让测试能替换 :func:`shutil.which` 而不用碰真实 PATH。
    """
    return shutil.which(binary)


def tool_readiness() -> list[ToolReadiness]:
    """全部依赖外部可执行文件的工具的当前就绪状态（按工具名排序，输出稳定）。"""
    readiness: list[ToolReadiness] = []
    for tool, binary in sorted(_EXTERNAL_BINARIES.items()):
        path = check_binary(binary)
        readiness.append(
            ToolReadiness(
                tool=tool,
                binary=binary,
                available=path is not None,
                path=path,
                install_hint=_hint_for(binary),
            ),
        )
    return readiness


def missing_tools() -> list[str]:
    """当前不可用的工具名。空列表表示所有外部依赖都就位。"""
    return [item.tool for item in tool_readiness() if not item.available]


def readiness_payload() -> dict[str, Any]:
    """完整预检结果：`ok` 是汇总位，`missing` 便于前端直接判断。"""
    items = tool_readiness()
    missing = [item.tool for item in items if not item.available]
    return {
        "ok": not missing,
        "tools": [item.as_dict() for item in items],
        "missing": missing,
    }


def health_payload() -> dict[str, Any]:
    """给 `/health` 的精简视图：只回答「哪个工具现在不能用、缺什么」。

    探针看到 200 就认为进程健康，所以缺依赖**不能**变成 5xx —— 那只会让容器
    反复重启，而重启治不好缺少的可执行文件。状态放在 body 里，由调用方判断。
    """
    from devops_agent.input.pipeline.sanitize import capabilities

    return {
        "tools": {
            item.tool: {"available": item.available, "binary": item.binary}
            for item in tool_readiness()
        },
        # 内容安全链的实际实现：缺 [sanitize] extra 时这里会说清楚，而不是静默降级
        "sanitize": capabilities(),
    }
