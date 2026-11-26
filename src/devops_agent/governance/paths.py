"""敏感路径与工作区边界的判定 —— 纯函数，零框架、零配置，可单测。

为什么不能只靠声明式 glob（实测数据见 docs/specs/0010）：没有任何单条 glob 能覆盖
全部拼写 —— `.git/config` 的相对形式所有候选 pattern 都漏，`.ENV` 因为大小写全漏，
`fnmatch` 的 `*` 还会跨分隔符，所以「`**/` 等于递归目录」本身就不成立。

这里的判定方式是：**先归一化（expanduser + realpath + 分隔符统一 + 大小写折叠），
再按路径段比对**。glob 只用来生成框架需要的那串 `rule_content`（早失败），
真正的边界在 `tools/guard.py` 里用本模块的判定做。
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# 敏感文件名（basename）：与 agentscope 的 DEFAULT_DANGEROUS_FILES 取并集，再多几个
# 本项目关心的密钥后端产物。覆盖关系由 tests/unit/test_permission_guard.py 的对账
# 用例钉住 —— 框架加了新名字，那条测试会红，提醒我们同步而不是静默漏掉。
SENSITIVE_FILES: frozenset[str] = frozenset(
    {
        ".env",
        ".envrc",
        ".env.local",
        ".env.development",
        ".env.development.local",
        ".env.test",
        ".env.test.local",
        ".env.staging",
        ".env.production",
        ".env.production.local",
        ".gitconfig",
        ".gitmodules",
        ".bashrc",
        ".bash_profile",
        ".zshrc",
        ".zprofile",
        ".profile",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
    }
)

# 敏感目录（任一路径段命中即拒）：这些目录里放的是凭据或可被用来执行代码的东西。
SENSITIVE_DIRS: frozenset[str] = frozenset({".git", ".ssh", ".aws", ".kube", ".gnupg", ".docker"})

# 只保护写入的目录：编辑器配置不该被模型改，但读它没什么风险。
WRITE_ONLY_DIRS: frozenset[str] = frozenset({".vscode", ".idea"})

# 给 Grep 注入的排除 glob（rg 语法），避免「搜到结果里带出 .env 内容」
GREP_EXCLUSIONS: tuple[str, ...] = tuple(
    pattern for name in sorted(SENSITIVE_FILES) for pattern in (f"!{name}", f"!**/{name}")
)


@dataclass(frozen=True, slots=True)
class Policy:
    """一次判定需要的边界信息。`roots` 只在 `within_root=True` 时作为白名单生效。"""

    roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class Verdict:
    """判定结果：denied + 一句人话原因（会原样进拒绝消息，模型据此换方案）。"""

    denied: bool
    reason: str = ""


ALLOW = Verdict(denied=False)


def normalize(raw: str, base: Path) -> Path:
    """把用户/模型给的路径字符串归一化成绝对真实路径。

    realpath 会吃掉 `..`、符号链接与 Windows 的 junction；文件不存在时 Python 仍
    返回归一化后的路径（3.6+），所以「写一个还没创建的文件」同样能判定。
    """
    text = os.path.expanduser(raw.strip().strip("'\" "))
    if not text:
        return base
    # 反斜杠统一当分隔符：Windows 路径在 POSIX 上被当成一个巨型文件名时，
    # `.git\\config` 会漏判成「未知文件」。安全判定宁可跨平台一致。
    text = text.replace("\\", "/")
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(os.path.realpath(candidate))


def _parts(path: Path) -> tuple[str, ...]:
    """路径段（小写）：跨平台比较统一走这里，不做字符串前后缀匹配。"""
    return tuple(part.casefold() for part in path.parts)


def _basename_hit(basename: str) -> str | None:
    """命中则返回那条敏感名（含通配的按 fnmatch 比）。"""
    for name in SENSITIVE_FILES:
        if name.startswith("*."):
            if fnmatch.fnmatch(basename, name):
                return name
            continue
        if basename == name:
            return name
    return None


def _dir_hit(parts: tuple[str, ...], *, write: bool) -> str | None:
    candidates = SENSITIVE_DIRS | WRITE_ONLY_DIRS if write else SENSITIVE_DIRS
    for part in parts:
        if part in candidates:
            return part
    return None


def build_policy(roots: Iterable[str | Path] = ()) -> Policy:
    """构造判定策略；roots 会先归一化，避免 `C:\\x` 与 `c:/x` 判成两个根。"""
    resolved = tuple(Path(os.path.realpath(str(root))) for root in roots)
    return Policy(roots=resolved)


def check(
    raw: str,
    *,
    base: Path,
    policy: Policy,
    write: bool = False,
    within_root: bool = False,
) -> Verdict:
    """判定一个路径是否可被访问（`write` 区分读写，`within_root` 用于 readonly 档）。"""
    path = normalize(raw, base)
    parts = _parts(path)
    if not parts:  # 极端输入（空串 / 只有分隔符）：交给工具自己报错，不在这里拦
        return ALLOW

    name = _basename_hit(parts[-1])
    if name is not None:
        return Verdict(True, f"受保护文件 {name}")

    directory = _dir_hit(parts, write=write)
    if directory is not None:
        return Verdict(True, f"受保护目录 {directory}")

    if within_root:
        roots = [_parts(root) for root in policy.roots]
        if roots and not any(parts[: len(root)] == root for root in roots):
            return Verdict(True, "超出工作区范围")
    return ALLOW


def file_patterns(name: str) -> tuple[str, ...]:
    """一个敏感文件名在框架 fnmatch 语义下需要的拼写形式（含裸名与带目录形式）。"""
    return (name, f"**/{name}", f"*/{name}")


def dir_glob_patterns(name: str) -> tuple[str, ...]:
    """敏感目录的 glob 形式（用于 Read/Write/Edit 的 rule_content）。"""
    return (f"{name}/**", f"**/{name}/**", f"{name}", f"**/{name}")


def bash_patterns(name: str, *, kind: str) -> tuple[str, ...]:
    """敏感名的 Bash 子串形式。

    Bash 的 `match_rule` 只有「子串 / 通配 / `:*` 前缀」三种语义，没有分词，
    所以这里刻意带上分隔符：`" .env"` 能匹配 `cat .env` 而不会误伤 `cat app.env`
    （后者历史上被 `.env` 裸子串误杀过）。
    """
    if kind == "dir":
        return (f"/{name}", f" {name}/", f" {name}")
    return (f" {name}", f"/{name}", f"'{name}", f'"{name}')
