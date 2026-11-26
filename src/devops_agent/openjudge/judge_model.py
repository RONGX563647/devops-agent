"""裁判模型构造：凭据只从现有网关读取，缺配置时明确报错。"""

from __future__ import annotations

import os
from typing import Any


def judge_model(name: str = "") -> Any:
    """按现有网关构造裁判模型；不用 Qwen / DashScope 专属 SDK。

    模型名的取值顺序：显式参数 > 界面配置（`oj_settings.judge_model`）> 环境变量。
    界面能换裁判是刻意的：裁判与被测模型同源时会系统性放水。
    """
    from devops_agent.openjudge import settings
    from devops_agent.openjudge.grade import ConfigError

    try:
        from openjudge.models import OpenAIChatModel
    except ImportError as exc:  # pragma: no cover - 取决于可选依赖
        raise ConfigError("未安装 py-openjudge：uv pip install py-openjudge==0.2.5") from exc

    api_key = os.environ.get("DEVOPS_AGENT_API_KEY", "").strip()
    base_url = os.environ.get("DEVOPS_AGENT_BASE_URL", "").strip()
    model = name.strip() or settings.resolved()["judge_model"]
    missing = [
        label
        for label, value in (
            ("DEVOPS_AGENT_API_KEY", api_key),
            ("DEVOPS_AGENT_BASE_URL", base_url),
            ("裁判模型（界面「用例与模型」或 DEVOPS_AGENT_MODEL）", model),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"缺少配置: {', '.join(missing)}")
    # temperature 不是所有 OpenAI 兼容网关都接受，统一走 client_args 透传会 400。
    return OpenAIChatModel(
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_retries=3,
        timeout=90.0,
    )
