"""出网守卫与抓取：scheme / 主机 / 端口 / 域名 / 解析后 IP / 重定向。

与 `fetch.py` 分开的理由：这一层是**安全边界**（任何调用都不许绕过），
`fetch.py` 关心的是「取到之后这算不算有用」。混在一起时，改判定要读一遍安全代码，
审安全又得跳过一段业务逻辑。

所有判断都基于**真实解析到的 IP**，不是主机名字符串 —— 否则
`http://169.254.169.254/`、`http://127.0.0.1:端口` 之类会被服务端代取。
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import urllib.parse
import urllib.request
from typing import Any, Final

from devops_agent.config import InputPolicy, load_input

# 能安全转成文本的 Content-Type；其余（PDF / 图片 / 二进制）如实说「不是文本」
_TEXTUAL_PREFIXES: Final = ("text/", "application/json", "application/xml", "application/xhtml+xml")
_TEXTUAL_HINTS: Final = ("javascript", "x-www-form-urlencoded")


def reason_of(reason: object) -> str:
    """把 `URLError.reason` 归类成人能行动的原因（DNS / TLS / 超时 / 其它）。"""
    if isinstance(reason, socket.gaierror):
        return "DNS 解析失败"
    if isinstance(reason, ssl.SSLError):
        return "TLS 握手失败"
    if isinstance(reason, TimeoutError):
        return "连接超时"
    return " ".join(str(reason).split())[:80]


def is_textual(content_type: str) -> bool:
    """Content-Type 是否可能是可读文本；空值按文本处理（不少站点不返回该头）。"""
    if not content_type:
        return True
    head = content_type.split(";")[0].strip().lower()
    if head.startswith(_TEXTUAL_PREFIXES):
        return True
    return any(hint in head for hint in _TEXTUAL_HINTS)


def is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """非公网地址：非 global / 内网 / 回环 / 链路本地 / 保留 / 组播（任一命中即拒）。"""
    return any(
        (
            not address.is_global,
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_reserved,
            address.is_multicast,
        )
    )


def resolve_addresses(host: str, port: int) -> list[str]:
    """解析主机名；解析失败折成一句可读原因，不把 gaierror 抛给上层。"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"DNS 解析失败：{host}") from exc
    return [str(info[4][0]) for info in infos]


def split_url(url: str) -> tuple[str, str, int]:
    """拆出 (scheme, host, port)；scheme 或主机名不合法直接拒。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https，收到 {parts.scheme or '空 scheme'}")
    host = parts.hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, host, port


def guard_host(host: str, port: int, policy: InputPolicy) -> None:
    """端口与域名白/黑名单：三条判断，任一命中即拒。"""
    if policy.allowed_ports and port not in policy.allowed_ports:
        raise ValueError(f"端口 {port} 不在允许列表 {sorted(policy.allowed_ports)}")
    lowered = host.lower()
    if lowered in {item.lower() for item in policy.denied_hosts}:
        raise ValueError(f"域名被拒绝：{host}")
    allowed = {item.lower() for item in policy.allowed_hosts}
    if allowed and lowered not in allowed:
        raise ValueError(f"域名不在允许列表：{host}")


def guard_address(host: str, port: int, policy: InputPolicy) -> None:
    """按**真实解析到的 IP** 判定是否公网；显式放行内网时直接返回。"""
    if policy.allow_private_addresses:
        return
    for raw in resolve_addresses(host, port):
        address = ipaddress.ip_address(raw)
        if is_blocked(address):
            raise ValueError(f"拒绝访问非公网地址：{address}")


def guard_url(url: str, policy: InputPolicy) -> None:
    """抓取前的完整守卫：scheme/主机 → 端口与域名 → 解析后 IP。"""
    _scheme, host, port = split_url(url)
    guard_host(host, port, policy)
    guard_address(host, port, policy)


class GuardedRedirect(urllib.request.HTTPRedirectHandler):
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
        guard_url(newurl, self._policy)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_text(url: str) -> str:
    """抓取一个网址的**原始响应文本**（stdlib；守卫 + 超时 + 体积 + 类型四道闸）。

    只做 IO，不做正文抽取：净化与裁剪统一在 `sanitize.screen` 里完成 —— L2 的
    检索结果也要过同一条链，两处各抽一遍必然漂移。
    """
    policy = load_input()
    guard_url(url, policy)
    opener = urllib.request.build_opener(GuardedRedirect(policy))
    request = urllib.request.Request(url, headers={"User-Agent": "devops-agent/0.1"})
    with opener.open(request, timeout=policy.fetch_timeout_seconds) as resp:  # noqa: S310
        content_type = (resp.headers.get("Content-Type") or "").strip().lower()
        raw: bytes = resp.read(policy.fetch_max_bytes)
    if not is_textual(content_type):
        raise ValueError(f"非文本内容（{content_type.split(';')[0]}）")
    return raw.decode("utf-8", errors="replace")
