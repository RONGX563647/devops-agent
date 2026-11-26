"""GET /health —— 存活、运行时模式、工具外部依赖就绪状态。

只有一件事需要解释：**缺依赖不返回 5xx**。探针看到 5xx 会重启容器，而重启治不好
缺少的可执行文件，只会把一次可诊断的降级放大成重启循环。所以状态放 body，
由调用方判断要不要报警。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from devops_agent.gateway.bootstrap import describe_runtime
from devops_agent.memory.runtime import memory_status
from devops_agent.tools.preflight import health_payload

router = APIRouter()

# 存活标记：只要有响应就是它（进程活着）；真正的问题在 runtime / tools 两段里
HEALTH_STATUS_OK = "ok"


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
        "memory": {
            "short_term": status["short_term"],
            "episodic": status["episodic"],
            "long_term": status["long_term"],
            "backend": status["backend"],
            "reason": status["reason"],
        },
        "context": status["context"],
        **health_payload(),
    }
