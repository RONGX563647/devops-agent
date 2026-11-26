"""把引用与附件取成材料块：远程抓取、HTML 转文本、本地读取。

阶段失败一律降级成该阶段的 status=error / skip，绝不抛异常打断对话：
引用取不到（404、越界、二进制）只损失那一条材料，不损失这一轮。

一条引用的结局有三种，必须分开记（见 `Resolution`）：

| 结局 | 含义 | 计入 failed？ | 进 misses？ |
|---|---|---|---|
| `OK` | 取到了 | 否 | 否 |
| `SKIPPED` | **没尝试**（远程抓取未开启） | 否 | 否（不是内容不可得） |
| `FAILED` | 尝试了但失败 | 是 | 是 |

把 SKIPPED 并进 FAILED 会让三处口径分裂：阶段状态报 error、摘要说「全部未取到」、
而模型上下文又不告诉它缺了什么 —— 用户看到红灯，实际什么都没发生。
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from devops_agent.config import InputPolicy, load_input
from devops_agent.input.attachments import Attachment

from .model import Reference, Stage, _elapsed_ms, _stage

# 下面三个常量是「默认值」，与 InputPolicy 的字段默认值一致；
# 运行期一律读 config/input.yaml（load_input）。保留常量是为了导出面与对账测试。
MAX_FILE_BYTES: Final = 256 * 1024
FETCH_TIMEOUT_SECONDS: Final = 5.0
FETCH_MAX_BYTES: Final = 512 * 1024
# 「服务端没代为抓取」：与真正的抓取失败区分开——这不是内容不可得，
# 模型用自己的工具仍可能取到，不能替它宣布放弃。
REMOTE_FETCH_OFF: Final = "远程抓取未开启"
# 失败原因保留的字符数：够看清是哪一类错误，又不至于把 detail 撑爆
REASON_LIMIT: Final = 120
# 能安全转成文本的 Content-Type；其余（PDF / 图片 / 二进制）如实说「不是文本」
_TEXTUAL_PREFIXES: Final = ("text/", "application/json", "application/xml", "application/xhtml+xml")
_TEXTUAL_HINTS: Final = ("javascript", "x-www-form-urlencoded")


class Outcome(StrEnum):
    """一条引用的结局。"""

    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Resolution:
    """取一条引用的结果：结局 / 内容 / 问题描述。"""

    outcome: Outcome
    content: str = ""
    problem: str = ""


def _remote_fetch_enabled(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    return os.environ.get("DEVOPS_AGENT_ALLOW_REMOTE_FETCH", "").strip() in {"1", "true", "yes"}


def _clip(text: str, limit: int = REASON_LIMIT) -> str:
    """把失败原因压成一行：detail 要能扫，不该贴整段 traceback。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _reason_of(reason: object) -> str:
    """把 `URLError.reason` 归类成人能行动的原因（DNS / TLS / 超时 / 其它）。"""
    if isinstance(reason, socket.gaierror):
        return "DNS 解析失败"
    if isinstance(reason, ssl.SSLError):
        return "TLS 握手失败"
    if isinstance(reason, TimeoutError):
        return "连接超时"
    return _clip(str(reason), limit=80)


def _is_textual(content_type: str) -> bool:
    """Content-Type 是否可能是可读文本；空值按文本处理（不少站点不返回该头）。"""
    if not content_type:
        return True
    head = content_type.split(";")[0].strip().lower()
    if head.startswith(_TEXTUAL_PREFIXES):
        return True
    return any(hint in head for hint in _TEXTUAL_HINTS)


def _guard_url(url: str, policy: InputPolicy) -> None:
    """抓取前的地址守卫：只允许 http(s)，且默认拒绝非公网地址。

    防的是「服务端替人访问」：一旦允许任意 URL，`http://169.254.169.254/…`（云元数据）
    或 `http://127.0.0.1:端口`（本机服务）就会被代取并写进模型上下文。
    这里判断的是**真实解析到的 IP**，不是主机名字符串。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https，收到 {parts.scheme or '空 scheme'}")
    host = parts.hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    if policy.allow_private_addresses:
        return
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"DNS 解析失败：{host}") from exc
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]))
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            raise ValueError(f"拒绝访问非公网地址：{address}")


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向每一跳都重新过地址守卫，并限制总跳数。

    不校验的话，`https://public.example/302 → http://127.0.0.1/` 就绕过了入口检查。
    跳数自己数：基类的 `max_redirections` 是类属性，没法按配置逐实例设。
    """

    def __init__(self, policy: InputPolicy) -> None:
        self._policy = policy
        self._hops = 0

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._hops += 1
        if self._hops > self._policy.max_redirects:
            raise ValueError(f"重定向超过 {self._policy.max_redirects} 跳，已停止")
        _guard_url(newurl, self._policy)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _html_to_text(html: str) -> str:
    """正文抽取：优先 trafilatura，未安装时退化为标签剥离。"""
    try:
        import trafilatura  # type: ignore[import-not-found]
    except ImportError:
        return re.sub(r"<[^>]+>", " ", html)
    extracted = trafilatura.extract(html)
    return extracted if extracted else re.sub(r"<[^>]+>", " ", html)


def _fetch_text(url: str) -> str:
    """抓取一个网址的正文（stdlib；地址守卫 + 超时 + 体积 + 类型四道闸）。"""
    policy = load_input()
    _guard_url(url, policy)
    opener = urllib.request.build_opener(_GuardedRedirectHandler(policy))
    request = urllib.request.Request(url, headers={"User-Agent": "devops-agent/0.1"})
    with opener.open(request, timeout=policy.fetch_timeout_seconds) as resp:  # noqa: S310
        content_type = (resp.headers.get("Content-Type") or "").strip().lower()
        raw = resp.read(policy.fetch_max_bytes)
    if not _is_textual(content_type):
        raise ValueError(f"非文本内容（{content_type.split(';')[0]}）")
    return _html_to_text(raw.decode("utf-8", errors="replace"))


def _read_local_file(token: str, root: Path | None) -> tuple[str, str]:
    """读取本地文件，返回 (内容, 问题描述)；问题描述非空表示这条引用没取到。"""
    if root is None:
        return "", "未配置工作区根目录，跳过本地文件"
    base = root.resolve()
    candidate = (base / token).resolve() if not Path(token).is_absolute() else Path(token).resolve()
    if not candidate.is_relative_to(base):
        return "", "路径越界，已拒绝读取"
    if not candidate.is_file():
        return "", "文件不存在"
    data = candidate.read_bytes()[: load_input().max_file_bytes]
    if b"\x00" in data[:1024]:
        return "", "二进制文件，跳过"
    return data.decode("utf-8", errors="replace"), ""


def _resolve_ref(ref: Reference, root: Path | None, remote_fetch: bool) -> Resolution:
    """取一条引用的内容，并给出三态结局（取到 / 没尝试 / 尝试后失败）。"""
    if ref.kind == "path":
        content, problem = _read_local_file(ref.value, root)
        if problem:
            return Resolution(Outcome.FAILED, problem=problem)
        return Resolution(Outcome.OK, content=content)
    if ref.kind != "url":
        return Resolution(Outcome.OK, content=f"（git 引用，未克隆）{ref.value}")
    if not remote_fetch:
        # 没尝试：既不进 failed，也不进 misses（见模块 docstring 的表）
        return Resolution(Outcome.SKIPPED, problem=REMOTE_FETCH_OFF)
    content, problem = _fetch_quietly(ref.value)
    if problem:
        return Resolution(Outcome.FAILED, problem=problem)
    return Resolution(Outcome.OK, content=content)


def _fetch_quietly(url: str) -> tuple[str, str]:
    """抓网页并降级：各类网络错误折成一句**可行动**的原因，不向上抛。"""
    # 运行期从包门面取 _fetch_text：测试会 monkeypatch pipeline._fetch_text，
    # 这里必须读到被替换后的实现，不能绑死在本模块的全局名上。
    from devops_agent.input import pipeline as _pipeline

    policy = load_input()
    start = time.perf_counter()
    try:
        content = _pipeline._fetch_text(url)
    except urllib.error.HTTPError as exc:
        return "", _clip(f"抓取失败：HTTP {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        return "", _clip(f"抓取失败：URLError（{_reason_of(exc.reason)}）")
    except TimeoutError:
        return "", f"抓取失败：TimeoutError（超过 {policy.fetch_timeout_seconds}s）"
    except ValueError as exc:
        # 地址守卫与 Content-Type 闸都走这条：原因本身就是结论，不必再包一层类名
        return "", _clip(f"抓取失败：{exc}")
    except Exception as exc:  # 兜底：拿不到具体分类时至少留下类名与首句
        return "", _clip(f"抓取失败：{type(exc).__name__}: {exc}")
    took_ms = _elapsed_ms(start)
    if not content.strip():
        return "", f"抓取成功但正文为空（{took_ms}ms）"
    return content, ""


def _expand_summary(total: int, ok: int, failed: int, skipped: int) -> tuple[str, str]:
    """引用展开的阶段状态。

    口径：全失败才算 error；全都「没尝试」（远程抓取关闭）算 skip；
    部分失败仍算 ok 但要记 failed —— 部分跳过只是附注，不该染红整轮。
    """
    if total == 0:
        return "skip", "无引用可展开"
    if failed == total:
        return "error", f"{failed} 条引用全部未取到"
    if skipped == total:
        return "skip", f"未开启远程抓取，跳过 {skipped} 条 URL"
    parts: list[str] = []
    if ok:
        parts.append(f"{ok} 条成功")
    if failed:
        parts.append(f"{failed} 条降级")
    if skipped:
        parts.append(f"{skipped} 条跳过")
    if not parts:  # 理论上不可达：留一句可读兜底，别让摘要空着
        return "skip", "无可展开的引用"
    return "ok", " · ".join(parts)


def _attach_summary(total: int, ok: int, failed: int) -> tuple[str, str]:
    """附件读取的阶段状态，口径与引用展开一致（全失败才 error）。"""
    if total == 0:
        return "skip", "未附加文件"
    if failed == total:
        return "error", f"{failed} 份附件全部未取到"
    if failed:
        return "ok", f"{ok} 份附件已读取 · {failed} 份降级"
    return "ok", f"{ok} 份附件已读取"


def _expand(
    refs: list[Reference], root: Path | None, remote_fetch: bool
) -> tuple[list[tuple[Reference, str]], list[str], Stage]:
    """把每条引用换成可直接塞进上下文的材料块。

    取不到的引用同时以 `misses` 返回：它必须进模型上下文，只写进阶段详情
    等于模型不知道自己手里缺了什么，于是自己去抓同一个地址反复失败。
    """
    start = time.perf_counter()
    blocks: list[tuple[Reference, str]] = []
    notes: list[str] = []
    misses: list[str] = []
    failures = 0
    skipped = 0

    for ref in refs:
        resolution = _resolve_ref(ref, root, remote_fetch)
        if resolution.outcome is Outcome.SKIPPED:
            skipped += 1
            notes.append(f"{ref.value} → {resolution.problem}")
            continue
        if resolution.outcome is Outcome.FAILED:
            failures += 1
            notes.append(f"{ref.value} → {resolution.problem}")
            misses.append(f"{ref.value} → {resolution.problem}")
            continue
        blocks.append((ref, resolution.content))
        notes.append(f"{ref.value} → {len(resolution.content)} 字")

    status, summary = _expand_summary(len(refs), len(blocks), failures, skipped)
    added = sum(len(content) for _, content in blocks)
    return (
        blocks,
        misses,
        _stage(
            "expand",
            status=status,
            summary=summary,
            # before 记「被展开的引用原文总长」：与 after（材料字数）同轴才有意义
            before=sum(len(ref.value) for ref in refs),
            after=added,
            items=len(refs),
            ms=_elapsed_ms(start),
            detail="\n".join(notes),
            changed=bool(blocks),
            failed=failures,
        ),
    )


def _attach(
    attachments: Sequence[Attachment],
) -> tuple[list[tuple[Reference, str]], list[str], Stage]:
    """把上传的附件变成材料块——正文在入库时已抽好，这里不再碰磁盘。"""
    start = time.perf_counter()
    blocks: list[tuple[Reference, str]] = []
    notes: list[str] = []
    misses: list[str] = []
    failures = 0

    for attachment in attachments:
        if not attachment.ok:
            failures += 1
            notes.append(f"{attachment.name} → {attachment.problem}")
            misses.append(f"{attachment.name} → {attachment.problem}")
            continue
        blocks.append((Reference(kind="file", value=attachment.name), attachment.text))
        suffix = "（已截断）" if attachment.truncated else ""
        notes.append(f"{attachment.name} → {attachment.chars} 字{suffix}")

    status, summary = _attach_summary(len(attachments), len(blocks), failures)
    added = sum(len(content) for _, content in blocks)
    return (
        blocks,
        misses,
        _stage(
            "attach",
            status=status,
            summary=summary,
            before=0,
            after=added,
            items=len(attachments),
            ms=_elapsed_ms(start),
            detail="\n".join(notes),
            changed=bool(blocks),
            failed=failures,
        ),
    )
