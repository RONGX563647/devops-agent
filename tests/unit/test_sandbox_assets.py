"""预配置沙箱资产：导出脚本能复刻框架脚本，Dockerfile 路径与常量一致。"""

from __future__ import annotations

import importlib.resources as resources
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / "docker" / "export_gateway_assets.py"
DOCKERFILE = ROOT / "docker" / "sandbox.Dockerfile"
REQUIREMENTS = ROOT / "docker" / "gateway-requirements.txt"


def test_export_script_writes_framework_scripts(tmp_path: Path) -> None:
    """构建期导出必须逐字节复刻已安装 agentscope 携带的脚本。"""
    result = subprocess.run(
        [sys.executable, str(EXPORT), "--dest", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    expected = {
        "_mcp_gateway_app.py": (
            "agentscope.workspace._mcp_gateway",
            "_mcp_gateway_app.py",
        ),
        "_glob_helper.py": ("agentscope.tool._builtin._scripts", "_glob_helper.py"),
    }
    for name, (package, filename) in expected.items():
        source = resources.files(package).joinpath(filename).read_bytes()
        assert (tmp_path / name).read_bytes() == source


def test_dockerfile_matches_framework_paths() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    # GATEWAY_HOME 与 agentscope.workspace._opensandbox._constants 一致。
    assert "/root/.agentscope" in text
    # 两个脚本名与 agentscope.workspace._utils.DEFAULT_GATEWAY_* 一致。
    assert "_mcp_gateway_app.py" in text
    assert "_glob_helper.py" in text or "export_gateway_assets.py" in text
    assert "AGENTSCOPE_VERSION" in text


def test_gateway_requirements_matches_framework_base() -> None:
    pins = {
        line.strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {"mcp<2.0.0", "uvicorn", "fastapi", "httpx"} <= pins
