"""OpenJudge 评测：轨迹采集、确定性门禁、语义判分与看板存储。

本包与 `devops_agent.eval` 完全独立（CI 强制 import 数为 0）：
旧 eval 是「题库 → 打分」的轻量实现，OpenJudge 版把**执行**（AgentScope 真跑）
与**判分**（OpenJudge Grader）解耦，轨迹落盘一次后可以换 plan/rubric 反复判分。

依赖边界：第三方 `openjudge` 与 `agentscope` 只在采集/判分子模块内惰性 import，
纯数据与存储模块（benchmark / trace / rulegate / store / report）保持零重依赖，
这样离线 PR 门禁不需要安装模型 SDK。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
