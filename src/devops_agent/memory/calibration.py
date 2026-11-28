"""估算漂移的自适应修正（SPEC-0016 §4.3）。

`CONTEXT_ESTIMATE_DELTA` 已经把「估算 vs 实测」报出来了，但报数不等于修正：
每轮都报 +12%，历史额度就永远按低估算。这里把最近 32 条的
`actual / estimated` 取中位数得到 α，只修正**本进程**的估算，不改配置文件。

三个状态直接对应事件里的 `state`：

- `|α − 1| ≤ 0.02`：噪声以内，`ok`；
- `≤ 0.10`：`adjusted`，估算按 α 放大/缩小；
- `> 0.10`：`error`，通常意味着换了模型族却没改 `family`。

α 落 `workspace/token_calibration.json` 只为排障，读不回来（重启重新积累）——
读回来会把「上一次部署的模型」的漂移带进「这一次部署」。
"""

from __future__ import annotations

import json
import statistics
import threading
from collections import deque
from pathlib import Path
from typing import Any

WINDOW = 32
#: 少于这么多样本时只观察、不修正：单条样本的比值噪声太大，修正会来回震荡
MIN_SAMPLES = 3
NOISE = 0.02
DRIFT = 0.10
_LOCK = threading.Lock()
_SAMPLES: deque[float] = deque(maxlen=WINDOW)
_ALPHA = 1.0
_STATE = "ok"


def reset() -> None:
    """测试用：清空样本与 α。"""
    global _ALPHA, _STATE
    with _LOCK:
        _SAMPLES.clear()
        _ALPHA = 1.0
        _STATE = "ok"


def alpha() -> float:
    """当前修正系数；默认 1.0（不修正）。"""
    with _LOCK:
        return _ALPHA


def observe(estimated: int, actual: int) -> tuple[str, float]:
    """记一条对账样本并更新 α；返回 (状态, α)。

    `estimated <= 0` 或 `actual <= 0` 视为没有样本：算不出比值，记进去只会
    把中位数往 0 拉。
    """
    global _ALPHA, _STATE
    if estimated <= 0 or actual <= 0:
        return _STATE, _ALPHA
    with _LOCK:
        _SAMPLES.append(actual / estimated)
        if len(_SAMPLES) < MIN_SAMPLES:
            return _STATE, _ALPHA
        ratio = statistics.median(_SAMPLES)
        _ALPHA = min(2.0, max(0.5, ratio))
        deviation = abs(ratio - 1.0)
        _STATE = "ok" if deviation <= NOISE else "adjusted" if deviation <= DRIFT else "error"
        snapshot = (_STATE, _ALPHA)
    _persist(*snapshot)
    return snapshot


def drift() -> dict[str, Any]:
    """`/health` 的 `memory.tokens.drift`：够不够修正、修成了多少。"""
    with _LOCK:
        return {
            "state": _STATE,
            "alpha": round(_ALPHA, 4),
            "samples": len(_SAMPLES),
            "window": WINDOW,
        }


def _persist(state: str, value: float) -> None:
    """落一份排障快照；写失败不影响计数（观测不能反过来打断主流程）。"""
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"state": state, "alpha": round(value, 4)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        return


def _path() -> Path:
    return Path(__file__).resolve().parents[3] / "workspace" / "token_calibration.json"
