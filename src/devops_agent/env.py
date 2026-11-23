"""进程启动时加载本地 .env —— 凭据只放本地文件，不进源码与 git。

语义：
- .env 只填充「尚未设置」的变量（override=False），CI / 容器注入的真实环境变量优先；
- 文件缺失时静默返回 False，这是 CI / wheel 安装场景的正常路径；
- 路径锚定项目根（src/devops_agent/env.py 的 parents[2]），与 config.py 的配置目录同构。
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# src/devops_agent/env.py -> parents[0]=devops_agent, [1]=src, [2]=项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


def load_env(path: Path | None = None) -> bool:
    """把 .env 注入进程环境；返回是否真的加载了文件。

    override=False：已存在的环境变量优先，避免本地文件覆盖 CI 注入值。
    """
    return load_dotenv(dotenv_path=path or ENV_FILE, override=False)
