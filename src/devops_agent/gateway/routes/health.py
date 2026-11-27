"""GET /health —— 存活、运行时模式、工具外部依赖就绪状态。

只有一件事需要解释：**缺依赖不返回 5xx**。探针看到 5xx 会重启容器，而重启治不好
缺少的可执行文件，只会把一次可诊断的降级放大成重启循环。所以状态放 body，
由调用方判断要不要报警。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from devops_agent.config_store import health as config_health
from devops_agent.config_store import mode as config_mode
from devops_agent.gateway.bootstrap import describe_runtime, model_status
from devops_agent.memory.runtime import memory_status
from devops_agent.memory.snapshot import restore_stats
from devops_agent.tools.preflight import health_payload

router = APIRouter()

# 存活标记：只要有响应就是它（进程活着）；真正的问题在 runtime / tools 两段里
HEALTH_STATUS_OK = "ok"


def config_status() -> dict[str, Any]:
    """配置库是否在降级：读不到覆盖时所有域都会静默回落代码默认值。

    只放布尔和一条错误原文：探针关心「现在坏没坏」，逐域明细在 `/api/config`。
    """
    snapshot = config_health.snapshot()
    try:
        mode = config_mode()
    except ValueError:
        # 非法模式（拼错）在 create_app 就会抛；这里兜底只为不让 /health 500
        mode = "invalid"
    return {
        "mode": mode,
        "degraded": bool(snapshot["degraded"]),
        "error": str(snapshot["error"]),
    }


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """存活 + 运行时模式 + 外部依赖。

    `runtime.kind` 说明跑的是真模型还是 Echo 回显：没有它，前端只能把回显当成
    「模型答非所问」；`tools` 段说明哪些外部依赖缺失（缺失时对应工具会持续失败）。
    """
    status = memory_status()
    return {
        "status": HEALTH_STATUS_OK,
        "runtime": describe_runtime(request.app.state.runtime),
        # 模型面：用的是哪个网关的哪个模型、窗口从哪来、哪个网关缺 key。
        # 与 runtime.kind 分开：后者说「真模型还是回显」，这里说「哪张能力卡」。
        "model": model_status(),
        "config": config_status(),
        "memory": {
            "short_term": status["short_term"],
            "episodic": status["episodic"],
            "long_term": status["long_term"],
            "backend": status["backend"],
            "requested": status["requested"],
            "effective": status["effective"],
            "reason": status["reason"],
            # 快照损坏退回新会话的次数：非零说明「历史莫名清空」有据可查
            "restores": restore_stats(),
            # 只放一个布尔：探针关心「记忆现在是不是在降级」，明细在 /api/memory/status
            "degraded": bool(status["health"]["degraded"]),
        },
        "context": status["context"],
        **health_payload(),
    }
