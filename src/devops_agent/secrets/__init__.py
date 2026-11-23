"""密钥域：输入里认出的凭据在这里加密存放，并在工具边界按需注入。

对外只暴露四件事——写、取、列、删，以及一个给工具中间件用的解析函数；
加密细节与密钥来源都收在 `vault` 里，调用方不需要知道。
"""

from devops_agent.secrets.vault import (
    PLACEHOLDER_RE,
    SecretMeta,
    SecretVault,
    get_vault,
    inject,
    mask,
    placeholder,
    reset_vault,
    resolver,
)

__all__ = [
    "PLACEHOLDER_RE",
    "SecretMeta",
    "SecretVault",
    "inject",
    "mask",
    "placeholder",
    "reset_vault",
    "resolver",
    "get_vault",
]
