# devops-agent Wiki

本 Wiki 是 [README](../../README.md) 的分层展开版：README 回答「是什么、怎么跑」，
这里回答「为什么这样切分、每一层实际接了哪些代码」。所有结论以当前 `src/` 代码与
`config/*.yaml` 为准，规划中的能力会显式标注。

## 阅读顺序

| 页面 | 讲什么 |
|---|---|
| [Architecture.md](Architecture.md) | 分层与框架隔离边界、请求从进入网关到落库的完整链路、模块职责表 |
| [Permissions.md](Permissions.md) | 5 档权限的构成、确认挂起与续跑协议、`prefix` 沉淀规则、会话隔离 |
| [Observability-and-Eval.md](Observability-and-Eval.md) | SQLite 表结构、健康分口径、评测五子命令、7 类硬失败、S1–S8 lint |
| [Development-Workflow.md](Development-Workflow.md) | worktree + PR 流程、五道本地门禁、CI 三段、nightly 评测 |
| [Understand-Anything.md](Understand-Anything.md) | 代码理解图谱的安装、跑法、产物为何不入库、worktree 下的两个坑 |

设计决策另见 [ADR](../adr) 与 [SPEC](../specs)：
- [ADR-0001 可观测性前端](../adr/0001-agent-observability-frontend.md)
- [ADR-0002 权限模型](../adr/0002-permission-model.md)
- [ADR-0003 模型参数配置](../adr/0003-model-parameter-config.md)
- [SPEC-0001 模型参数配置](../specs/0001-model-parameter-config.md)
- [SPEC-0002 评测 UI 控制台](../specs/0002-eval-ui-console.md)

## 一句话定位

当前代码的**实际形态**是：单 Agent（AgentScope 2.x `Agent` + `ReActConfig`）
+ 5 档权限 + 确认挂起/续跑 + 7 阶段输入管线 + 五层模型参数
+ 端到端可观测性（事件流 → SQLite → HTTP → React）
+ 完整评测闭环（题库 / 判卷 / 裁判 / 基线 / 静态 lint / nightly）。

尚未接线的部分（只在单测里被调用）：`runtime/supervisor.py` 的
`TeamPipeline` / `GoalPipeline`、`memory/ledger.py` 的 Mem0 中间件、
`executor/boundary.py` 的 `DockerWorkspace`、`eval/scorer.py` 的维度常量。

## 关键约束速查

| 约束 | 事实来源 |
|---|---|
| `import agentscope` 只出现在 9 个装配模块 | `grep -rn 'import agentscope' src/` |
| 工具名只有 `tools/catalog.py` 一处定义 | `READONLY_TOOL_NAMES` / `WRITE_TOOL_NAMES` |
| 参数取值只有 `model_settings.resolve()` 一处解析 | 五层优先级 |
| 每个工具独占一个 `LocalBackend(workdir=cwd)` | `tools/catalog.py` |
| 覆盖率门槛 80%（后端）/ 65%（前端），不得放宽 | [config/gates.yaml](../../config/gates.yaml) |
