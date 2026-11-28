"""发现结果的两级缓存：内存 TTL + 落盘首屏。

为什么落盘：进程重启后设置面板第一条请求不该等一次上游往返。但**只落
`id / label / created_at`** —— 窗口与模态不落，它们走四源合并；落下去就会
出现「JSON 里 128k、卡片里 1M」的第二份真相。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from devops_agent.model_catalog.schema import DiscoveryItem, DiscoveryResult

logger = logging.getLogger(__name__)

#: 成功 300s：面板反复开关不该重复打上游。
TTL_OK = 300.0
#: 失败 30s：401 连点刷新不该连打 30 次，但用户改完 key 不该等太久。
TTL_FAIL = 30.0
#: 该 kind 没有 `/models` 是稳定事实，1 小时。
TTL_UNSUPPORTED = 3600.0

#: 落盘只写这三项：它们不参与合并，不会产生第二份真相。
DISK_KEYS = ("id", "label", "created_at")

_CACHE_ROOT = Path(__file__).resolve().parents[3] / "workspace" / "model_catalog"


def ttl_for(code: str) -> float:
    """按结果码定 TTL。未知码按失败处理（宁可多问一次，不要抱着错结论）。"""
    if code == "ok":
        return TTL_OK
    if code == "unsupported":
        return TTL_UNSUPPORTED
    return TTL_FAIL


@dataclass(frozen=True, slots=True)
class _Entry:
    result: DiscoveryResult
    expires_at: float


def _disk_path(provider_id: str, root: Path) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in provider_id)
    return root / f"{safe}.json"


def _to_disk(result: DiscoveryResult) -> dict[str, Any]:
    return {
        "fetched_at": result.fetched_at,
        "code": result.code,
        "items": [{key: item.as_dict()[key] for key in DISK_KEYS} for item in result.items],
    }


def _from_disk(provider_id: str, raw: Any) -> DiscoveryResult | None:
    """盘上数据坏掉就当没有：首屏少一次加速，比带着坏数据启动好。"""
    if not isinstance(raw, dict):
        return None
    items = raw.get("items")
    if not isinstance(items, list):
        return None
    parsed: list[DiscoveryItem] = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            parsed.append(
                DiscoveryItem(
                    id=str(item["id"]),
                    label=str(item.get("label") or ""),
                    created_at=str(item.get("created_at") or ""),
                    origin=provider_id,
                )
            )
    return DiscoveryResult(
        items=tuple(parsed),
        code=str(raw.get("code") or "ok"),
        fetched_at=str(raw.get("fetched_at") or ""),
        stale=True,
    )


class CatalogCache:
    """进程内 TTL + 落盘。键是 `(provider_id, base_url, key_fingerprint)`。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else _CACHE_ROOT
        self._entries: dict[tuple[str, str, str], _Entry] = {}
        #: 最近一次「成功」的列表，与 `_entries` 分开存。
        #: 合在一起的话，一次 500 会把旧列表覆盖成空，面板就从「有模型」变成
        #: 「网关没有模型」—— 后者是错误结论，不是降级。
        self._last_good: dict[tuple[str, str, str], DiscoveryResult] = {}
        #: ETag 只存在内存：304 是「省一次传输」的优化，重启后重新拉一遍更简单
        self._etags: dict[tuple[str, str, str], str] = {}
        self._disk_loaded: set[str] = set()

    # --- ETag -----------------------------------------------------------

    def etag(self, key: tuple[str, str, str]) -> str:
        return self._etags.get(key, "")

    def set_etag(self, key: tuple[str, str, str], value: str) -> None:
        if value:
            self._etags[key] = value
        else:
            self._etags.pop(key, None)

    # --- 内存 -----------------------------------------------------------

    def fresh(
        self, key: tuple[str, str, str], *, now: float | None = None
    ) -> DiscoveryResult | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if (now if now is not None else time.monotonic()) >= entry.expires_at:
            return None
        return entry.result

    def older(self, provider_id: str, key: tuple[str, str, str]) -> DiscoveryResult | None:
        """任意旧值（含已过期），标 `stale=true`。失败时用它顶住首屏。"""
        good = self._last_good.get(key)
        if good is not None:
            return replace(good, stale=True)
        entry = self._entries.get(key)
        if entry is not None and entry.result.code in {"ok", "empty"}:
            return replace(entry.result, stale=True)
        return self.from_disk(provider_id)

    def put(self, key: tuple[str, str, str], result: DiscoveryResult) -> None:
        ttl = ttl_for(result.code)
        self._entries[key] = _Entry(result=result, expires_at=time.monotonic() + ttl)
        # 失败结果不覆盖盘上的成功列表：一次 500 不该把首屏清空
        if result.code in {"ok", "empty"}:
            self._last_good[key] = result
            self.save_disk(key[0], result)

    def clear(self) -> None:
        self._entries.clear()
        self._last_good.clear()
        self._etags.clear()
        self._disk_loaded.clear()

    # --- 落盘 -----------------------------------------------------------

    def from_disk(self, provider_id: str) -> DiscoveryResult | None:
        if provider_id in self._disk_loaded and provider_id not in {k[0] for k in self._entries}:
            # 已经确认盘上没有，不必每次 stat
            pass
        path = _disk_path(provider_id, self._root)
        if not path.is_file():
            self._disk_loaded.add(provider_id)
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("模型列表缓存损坏，忽略：%s", path, exc_info=True)
            return None
        return _from_disk(provider_id, raw)

    def save_disk(self, provider_id: str, result: DiscoveryResult) -> None:
        path = _disk_path(provider_id, self._root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(_to_disk(result), ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            # 落盘是加速不是功能：写不进去只影响下次首屏，不该让发现失败
            logger.warning("模型列表缓存写入失败：%s", path, exc_info=True)


_shared: CatalogCache | None = None


def shared() -> CatalogCache:
    """进程级单例：面板与 `/health` 看到同一份缓存。"""
    global _shared
    if _shared is None:
        _shared = CatalogCache()
    return _shared
