"""high-risk-repo 的服务入口。"""


def health() -> dict[str, str]:
    """返回健康状态。"""
    return {"status": "ok"}
