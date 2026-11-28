"""成本 / 性能看板的数据组装：按模型聚合 token 与耗时，再按单价换算成本。

路由留在 `api.py`，组装逻辑放这里，是为了让「单价、分位、未配置」这些
口径集中在一处——api.py 已经承担了全部路由，再加一百行业务计算必然超行数门禁。
"""

from __future__ import annotations

from devops_agent.observability import health as health_mod
from devops_agent.observability import repository as repo
from devops_agent.observability.pricing import load_prices


def _timing_block(samples: list[int]) -> dict[str, object]:
    """耗时块：均值与 p95；样本不足 2 条时 p95 为 None（不假装有分位）。"""
    if not samples:
        return {"avg": None, "p95": None, "samples": 0}
    return {
        "avg": round(sum(samples) / len(samples)),
        # 一条样本算不出分位：给 None，让前端显示「—」，而不是把均值伪装成 p95
        "p95": health_mod.percentile(samples, 0.95) if len(samples) > 1 else None,
        "samples": len(samples),
    }


def cost_payload(limit: int = 500) -> dict[str, object]:
    """一次返回：每模型用量/耗时/成本 + 全局 TTFT 与工具耗时。

    单价来自 `config/model_prices.yaml`：没配置的模型成本为 None（不计入总额），
    这样「没配价格」不会被当成「免费」，把总成本算低。
    """
    prices = load_prices()
    with repo.connect() as conn:
        repo.init_db(conn)
        models = repo.model_usage(conn)
        model_ms = repo.timing_samples(conn, "model", limit)
        tool_ms = repo.timing_samples(conn, "tool", limit)
        ttft = repo.ttft_samples(conn, limit)
    rows: list[dict[str, object]] = []
    total_cost = 0.0
    priced = 0
    unpriced = 0
    for item in models:
        name = str(item["name"])
        cost = prices.cost_of(
            name,
            int(str(item["input_tokens"])),
            int(str(item["output_tokens"])),
        )
        if cost is None:
            unpriced += 1
        else:
            priced += 1
            total_cost += cost
        rows.append({**item, "cost": cost, "priced": cost is not None})
    return {
        "currency": prices.currency,
        "unit_tokens": prices.unit_tokens,
        "priced_models": priced,
        "unpriced_models": unpriced,
        "total_cost": round(total_cost, 6) if priced else None,
        "models": rows,
        "ttft": _timing_block(ttft),
        "model_ms": _timing_block(model_ms),
        "tool_ms": _timing_block(tool_ms),
    }
