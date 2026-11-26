"""网关部署设置 —— 环境变量名与策略对象的唯一入口。

两类值的处置方式不同，这是刻意的：

- **策略**（ask_limit / TTL / 心跳间隔…）来自 config/gateway.yaml，在装配期读成
  不可变的 `GatewayPolicy`，随 `app.state.settings` 注入。理由是它必须能在单测里替换：
  `Settings(policy=GatewayPolicy(stream=...))` 一行就能测「上限改成 1 会怎样」，
  不再靠 monkeypatch 模块常量。
- **环境变量**（domain / model / 凭据）按请求读取。理由是既有语义：`/health` 要反映
  当前进程的环境，测试也会逐个用例 monkeypatch 它。把它们在 import 期冻结会同时
  破坏这两点，收益只是一个更好看的对象图。

所有 env 名都集中在这里：改名时只改这一处，靠 grep 找不到漏网的第二份。
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
        """从 config/gateway.yaml 读策略；缺文件时全回落内置默认。"""
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
