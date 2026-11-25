"""OpenJudge 评测：轨迹采集、确定性门禁、语义判分与看板存储。

本包是仓库里**唯一**的评测实现（旧 `devops_agent.eval` 已删除，题库从 150 例
收敛为五赛道 90 例）。它把**执行**（AgentScope 真跑）与**判分**（OpenJudge Grader）
解耦：轨迹落盘一次后可以换 plan/rubric 反复判分。

模块分工：
- `benchmark` / `trace` / `store` / `report`：纯数据、存储与汇总，零重依赖；
- `run`：采集 → 判分 → 落盘 → 入库的唯一流水线，CLI 与在线评测共用；
- `run_store`：`oj_runs` 表（在线任务的进度与单飞锁），从 `store` 分片出来
  只为守住单文件 500 行上限；
- `service` / `api`：在线任务生命周期与 HTTP 契约；
- `harness` / `grade` / `rubric`：真正驱动模型与裁判的部分，惰性 import。

依赖边界：第三方 `openjudge` 与 `agentscope` 只在采集/判分子模块内惰性 import，
这样离线 PR 门禁不需要安装模型 SDK。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
