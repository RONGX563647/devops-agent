"""密钥保险箱 —— 认出来的凭据加密入库，只有工具真正要用的那一刻才解密。

为什么必须做这一层：
- 输入里的 API Key 一旦原样进上下文，它就会随每一轮对话、每一条日志、
  每一次 span 落库反复出现，泄漏面是「模型提供方 + sqlite + 前端历史」三处；
- 但它又不能直接被抹掉——用户贴 key 的意图通常是「用这个 key 去调一下」，
  抹掉就等于这件事做不成。

于是取一个折中：**模型只看到 `{{secret:name}}` 占位符，工具执行前在边界
替换成真值，工具输出里若带回真值再抹回占位符**。模型全程看不到明文，
而命令能真正带着密钥跑起来。

密钥本身用 Fernet（AES128-CBC + HMAC）加密，密钥来源优先级：
1. `DEVOPS_AGENT_SECRET_KEY`（urlsafe base64，32 字节）；
2. `DEVOPS_AGENT_SECRET_KEY_FILE` 指定的文件；
3. 自动生成到与 sqlite 同目录的 `.secret.key`（权限 0600）。
换密钥后旧密文解不开——此时按「未命中」处理，占位符原样透传，
不会让整轮对话因为一个解不开的密钥而失败。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

from devops_agent.observability.repository import connect, default_db_path

KEY_ENV: Final = "DEVOPS_AGENT_SECRET_KEY"
KEY_FILE_ENV: Final = "DEVOPS_AGENT_SECRET_KEY_FILE"
# 占位符放在正文里的唯一形态：解析管线写它，工具中间件认它
PLACEHOLDER_PATTERN: Final = r"\{\{secret:([A-Za-z0-9_]+)\}\}"
PLACEHOLDER_RE: Final = re.compile(PLACEHOLDER_PATTERN)
# 名字只允许标识符字符：它要同时当 sqlite 主键与占位符里的字段
_NAME_RE: Final = re.compile(r"[^A-Za-z0-9_]+")
# 指纹只用于「同一个值是否已经存过」，不参与解密
_FINGERPRINT_CHARS: Final = 12

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    name         TEXT PRIMARY KEY,
    ciphertext   TEXT NOT NULL,
    hint         TEXT NOT NULL DEFAULT '',
    fingerprint  TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'input',
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    uses         INTEGER NOT NULL DEFAULT 0
);
"""


def placeholder(name: str) -> str:
    """把一个密钥名渲染成正文里的占位符。"""
    return "{{secret:" + name + "}}"


def safe_name(name: str) -> str:
    """把任意名字折成占位符能承载的形式；空名兜底成 secret。"""
    cleaned = _NAME_RE.sub("_", name).strip("_")
    return cleaned or "secret"


def fingerprint(value: str) -> str:
    """值的短指纹：用于判重，不可逆，也不足以暴力还原。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_FINGERPRINT_CHARS]


def hint(value: str) -> str:
    """只暴露前 4 位与长度：用户能认出是哪个凭据，旁人拿不到全部。"""
    return f"{value[:4]}…（{len(value)} 位）"


def key_file() -> Path:
    """密钥文件位置：默认与 sqlite 同目录，随 workspace 一起被 gitignore。"""
    override = os.environ.get(KEY_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return default_db_path().parent / ".secret.key"


def load_key(path: Path | None = None) -> bytes:
    """取 Fernet 密钥：环境变量 > 已有文件 > 生成新文件。"""
    raw = os.environ.get(KEY_ENV, "").strip()
    if raw:
        return raw.encode()
    target = path if path is not None else key_file()
    if target.is_file():
        return target.read_bytes().strip()
    key = Fernet.generate_key()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(key)
    try:
        target.chmod(0o600)
    except OSError:  # Windows 上 chmod 语义有限，只能依赖目录权限
        pass
    return key


@dataclass(frozen=True, slots=True)
class SecretMeta:
    """对外可见的密钥元信息——有意不含密文与明文。"""

    name: str
    hint: str
    source: str
    created_at: str
    last_used_at: str | None
    uses: int


def _now() -> str:
    """UTC ISO 8601 时间戳，与 runs / spans 的时间口径一致。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


class SecretVault:
    """凭据的加密存取。同一名字重复写入只在值变化时才覆盖。"""

    def __init__(self, db_path: Path | None = None, key: bytes | None = None) -> None:
        self._db = db_path if db_path is not None else default_db_path()
        self._fernet = Fernet(key if key is not None else load_key())
        with connect(self._db) as conn:
            conn.executescript(_SCHEMA)

    def put(self, name: str, value: str, source: str = "input") -> SecretMeta:
        """写入一条凭据；同值重复写入不动 created_at，值变了才换密文。"""
        clean = safe_name(name)
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        with connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO secrets (name, ciphertext, hint, fingerprint, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    hint = excluded.hint,
                    fingerprint = excluded.fingerprint,
                    source = excluded.source
                WHERE secrets.fingerprint <> excluded.fingerprint
                """,
                (clean, token, hint(value), fingerprint(value), source, _now()),
            )
        stored = self._meta(clean)
        assert stored is not None  # 刚写入，必然存在
        return stored

    def name_for_value(self, value: str) -> str | None:
        """按指纹反查已入库的同一明文，用于输出捕获时复用名字而不重复登记。

        指纹是 sha256 前 12 位，碰撞概率可忽略；即便真碰撞，也只是让两个不同值
        共用一个占位符（解密时取到先存的那个），不会泄漏明文。
        """
        fp = fingerprint(value)
        with connect(self._db) as conn:
            row = conn.execute(
                "SELECT name FROM secrets WHERE fingerprint = ? LIMIT 1", (fp,)
            ).fetchone()
        return str(row["name"]) if row is not None else None

    def get(self, name: str) -> str | None:
        """取明文并记一次使用；解不开或不存在都返回 None。"""
        clean = safe_name(name)
        with connect(self._db) as conn:
            row = conn.execute("SELECT ciphertext FROM secrets WHERE name = ?", (clean,)).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE secrets SET uses = uses + 1, last_used_at = ? WHERE name = ?",
                (_now(), clean),
            )
        try:
            return self._fernet.decrypt(str(row["ciphertext"]).encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return None

    def list(self) -> list[SecretMeta]:
        """列出全部凭据的元信息（不含密文），按名字排序。"""
        with connect(self._db) as conn:
            rows = conn.execute(
                """
                SELECT name, hint, source, created_at, last_used_at, uses
                FROM secrets ORDER BY name
                """
            ).fetchall()
        return [_meta_from_row(row) for row in rows]

    def delete(self, name: str) -> bool:
        """删除一条凭据；不存在返回 False。"""
        clean = safe_name(name)
        with connect(self._db) as conn:
            cursor = conn.execute("DELETE FROM secrets WHERE name = ?", (clean,))
            return cursor.rowcount > 0

    def _meta(self, name: str) -> SecretMeta | None:
        with connect(self._db) as conn:
            row = conn.execute(
                """
                SELECT name, hint, source, created_at, last_used_at, uses
                FROM secrets WHERE name = ?
                """,
                (name,),
            ).fetchone()
        return _meta_from_row(row) if row is not None else None


def _meta_from_row(row: sqlite3.Row) -> SecretMeta:
    return SecretMeta(
        name=str(row["name"]),
        hint=str(row["hint"]),
        source=str(row["source"]),
        created_at=str(row["created_at"]),
        last_used_at=str(row["last_used_at"]) if row["last_used_at"] else None,
        uses=int(row["uses"]),
    )


def inject(text: str, resolve: Callable[[str], str | None]) -> tuple[str, dict[str, str]]:
    """把文本里的占位符换成真值，返回 (替换后文本, {名字: 真值})。"""
    resolved: dict[str, str] = {}

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        secret = resolve(name)
        if secret is None:
            return match.group(0)
        resolved[name] = secret
        return secret

    return PLACEHOLDER_RE.sub(_replace, text), resolved


def mask(text: str, resolved: dict[str, str]) -> str:
    """把真值抹回占位符：工具输出常把入参原样回显，不能让它重新进上下文。"""
    for name, secret in resolved.items():
        if secret and secret in text:
            text = text.replace(secret, placeholder(name))
    return text


_VAULT: SecretVault | None = None


def get_vault() -> SecretVault:
    """进程级单例：密钥文件的读写与建表只做一次。

    刻意不叫 `vault`：那会和子模块 `secrets.vault` 同名，
    `from devops_agent.secrets import vault` 拿到的东西取决于导入顺序。
    """
    global _VAULT
    if _VAULT is None:
        _VAULT = SecretVault()
    return _VAULT


def reset_vault() -> None:
    """测试用：丢弃单例，让下次访问重新读密钥与数据库路径。"""
    global _VAULT
    _VAULT = None


def resolver() -> Callable[[str], str | None]:
    """给工具中间件的解析函数；保箱不可用时退化为「解不出」，不阻断工具执行。"""

    def _resolve(name: str) -> str | None:
        try:
            return get_vault().get(name)
        except Exception:  # 密钥文件损坏 / 表被删：一律当未命中
            return None

    return _resolve
