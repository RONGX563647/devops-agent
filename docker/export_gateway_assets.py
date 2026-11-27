#!/usr/bin/env python3
"""构建期导出 gateway 脚本：从已安装的 agentscope 读取两个脚本字节并写盘。

运行时零胶水：镜像里预置好 `_mcp_gateway_app.py` 与 `_glob_helper.py` 后，
`SandboxedWorkspaceBase` 的 bootstrap（apt-get + uv + pip，分钟级）会被整体
跳过（SPEC-0019 §7.1）。本脚本只在 `docker build` 阶段执行一次，不进入运行时
镜像的启动路径。

用法（在镜像构建中，用 gateway venv 的 python 执行）：

    python docker/export_gateway_assets.py --dest /root/.agentscope
"""

from __future__ import annotations

import argparse
import importlib.resources as resources
from pathlib import Path

# 目标文件名 → (资源包, 包内文件名)。名字必须与框架常量一致：
# `agentscope.workspace._utils.DEFAULT_GATEWAY_SCRIPT` / `DEFAULT_GLOB_HELPER_SCRIPT`。
_ASSETS: dict[str, tuple[str, str]] = {
    "_mcp_gateway_app.py": ("agentscope.workspace._mcp_gateway", "_mcp_gateway_app.py"),
    "_glob_helper.py": ("agentscope.tool._builtin._scripts", "_glob_helper.py"),
}


def export(dest: Path) -> list[Path]:
    """把两个脚本写到 `dest`，返回写出的路径列表。"""
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, (package, filename) in _ASSETS.items():
        data = resources.files(package).joinpath(filename).read_bytes()
        target = dest / name
        target.write_bytes(data)
        written.append(target)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", required=True, help="gateway home（如 /root/.agentscope）")
    args = parser.parse_args()
    for path in export(Path(args.dest)):
        print(f"exported {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
