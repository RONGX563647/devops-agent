"""网关部署设置：环境变量名与策略对象的唯一入口。

两类值刻意分开处置 ——
- **策略**（限额 / TTL / 心跳）来自部署配置（代码默认 + config.db 覆盖），装配期读成
  不可变的 `GatewayPolicy`：可整体注入，单测不必 monkeypatch 模块常量；
- **环境变量**（domain / model / 凭据）按请求读取：`/health` 要反映当前进程环境，
  测试也逐个用例 patch 它，import 期冻结会同时破坏这两点。

env 名集中在这里，改名只改一处。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from devops_agent.config import GatewayPolicy, GatewayStreamPolicy, load_gateway

# 模型凭据：配了才走真实模型，否则回退 Echo（自检 / 测试用）
ENV_API_KEY = "DEVOPS_AGENT_API_KEY"
# 模型名：只用于观测标注与 /health 自述，不参与装配
ENV_MODEL = "DEVOPS_AGENT_MODEL"
# 观测数据的域标签（交付 / 排期 / 定位…）：影响 max_iters 取值与健康分批次
ENV_DOMAIN = "DEVOPS_AGENT_DOMAIN"

DEFAULT_DOMAIN = "debug"


@dataclass(frozen=True, slots=True)
class Settings:
    """一份装配期固定的网关策略 + 一组按请求读取的环境值。"""

    policy: GatewayPolicy = field(default_factory=GatewayPolicy)

    @classmethod
    def load(cls) -> Settings:
        """读部署配置里的网关策略；没有覆盖时全回落代码默认。"""
        return cls(policy=load_gateway())

    @property
    def stream(self) -> GatewayStreamPolicy:
        """流式端点的策略（ask_limit / park_ttl / 心跳等）。"""
        return self.policy.stream

    def domain(self) -> str:
        """观测域标签：环境变量优先，空串按缺省处理（空域会让健康分分桶失真）。"""
        return os.environ.get(ENV_DOMAIN) or DEFAULT_DOMAIN

    def model(self) -> str:
        """模型名（空串 = 未配置，界面上显示为「未记录」而不是猜一个）。"""
        return os.environ.get(ENV_MODEL, "")

    def has_credentials(self) -> bool:
        """是否配了模型凭据；决定装配真实运行时还是 Echo。"""
        return bool(os.environ.get(ENV_API_KEY))
