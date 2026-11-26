"""联网模式的唯一解读点（零依赖）。

三个地方要读它：`chat_model` 决定发不发 `extra_body`、`assembly` 决定注册不注册
`web_search` 工具、`profile_note` 决定追不追加联网说明。分散在三处各读一遍环境
变量，拼错一处就会出现「请求里没检索、提示词却说已联网」这种自相矛盾的状态。

放在顶层而不是 `runtime/` 或 `tools/`：两侧都要 import 它，放任何一边都会让
另一边反向依赖，而这里只有两个函数和几个常量。
"""

from __future__ import annotations

import os

SEARCH_MODE_ENV = "DEVOPS_AGENT_SEARCH_MODE"
FORCED_ENV = "DEVOPS_AGENT_SEARCH_FORCED"

SEARCH_MODES = ("tool", "provider", "off")
DEFAULT_SEARCH_MODE = "tool"


def search_mode() -> str:
    """当前联网模式：`tool`（默认）/ `provider`（旧行为）/ `off`。

    未知取值回落到默认而不是 `off`：拼错环境变量不该让联网能力静默消失，
    那是运维最难发现的一类状态。旧值 `forced` / `auto` 映射到 `provider`，
    保留一个版本 —— 现有部署不该因为升级就断了检索。
    """
    raw = (os.environ.get(SEARCH_MODE_ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_SEARCH_MODE
    if raw in ("forced", "auto"):
        return "provider"
    return raw if raw in SEARCH_MODES else DEFAULT_SEARCH_MODE


def is_legacy_mode() -> bool:
    """部署是否还在用 `forced` / `auto` 这两个旧值（用于启动时提示迁移）。"""
    raw = (os.environ.get(SEARCH_MODE_ENV) or "").strip().lower()
    return raw in ("forced", "auto")


def _legacy_forced() -> bool | None:
    """旧 `forced` / `auto` 各自对应的强制语义；新值返回 None（无意见）。"""
    raw = (os.environ.get(SEARCH_MODE_ENV) or "").strip().lower()
    if raw == "forced":
        return True
    if raw == "auto":
        return False
    return None


def forced_on() -> bool:
    """`provider` 模式下是否强制检索。

    显式 `DEVOPS_AGENT_SEARCH_FORCED=0` 才关；未设置时按旧
    `DEVOPS_AGENT_SEARCH_MODE=auto` 推断（迁移期兼容），其余一律强制。
    只发 `enable_search` 时供应商把裁量权交给模型，实测常常一次都不搜 ——
    「点了按钮但什么都没发生」比多花点钱更糟。
    """
    explicit = os.environ.get(FORCED_ENV)
    if explicit is not None:
        return explicit.strip().lower() not in ("0", "false", "off", "no")
    legacy = _legacy_forced()
    return True if legacy is None else legacy
