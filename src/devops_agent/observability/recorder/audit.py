"""网络获取决策的收集与落库（SPEC-0012 §7）。

为什么独立成模块：`recorder.py` 已经贴着 500 行棘轮，而这块逻辑只需要「攒字段 +
结束时写一条 UPDATE」。放在这里，recorder 只留两行调用，也避免它与 repo 的
列名细节绑在一起。
"""

from __future__ import annotations

import json
from pathlib import Path

from devops_agent.observability import repository as repo

#: 只允许这几个键：写脏字段会让前端与报表各按自己的猜测解释
_ALLOWED = ("net", "prefetch", "fallback")


class WebAccessLog:
    """一次 run 的网络获取决策（net / prefetch / fallback）。"""

    def __init__(self) -> None:
        self._fields: dict[str, object] = {}

    def note(self, **fields: object) -> None:
        """记录/覆盖一项决策；未知键直接丢弃（宁可少记，也不要记出噪声字段）。"""
        for key, value in fields.items():
            if key in _ALLOWED:
                self._fields[key] = value

    def flush(self, db_path: Path, run_id: int) -> None:
        """把累积的决策写成一行 JSON；没有决策就不写库（老记录保持空串 = 未记录）。"""
        if not self._fields:
            return
        payload = json.dumps(self._fields, ensure_ascii=False)
        with repo.connect(db_path) as conn:
            repo.set_run_web_access(conn, run_id, payload)
