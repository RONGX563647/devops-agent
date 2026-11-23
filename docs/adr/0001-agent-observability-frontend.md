# ADR-0001：Agent 可观测性前端与端到端观测体系

- 状态：Accepted
- 日期：2026-11-23
- 决策者：项目所有者
- 相关：`README.md` 的 Roadmap、`AGENTS.md` 架构约束、`config/gates.yaml`

## 背景

项目已有可运行的异步网关（`POST /run`）与真实模型接入，但**没有任何运行过程数据**：`eval/scorer.py` 只有三个枚举值，`/run` 返回最终文本后即丢弃全部中间状态。当前无法回答"这个 agent 跑得好不好"——成功率、延迟、token 成本、工具失败、权限拒绝、答案质量都不可见。

需求是加入一个前端，用于**观测 agent 运行的好坏**。这里的"观测"区别于"监控"：不仅是看是否存活与耗时，还要量化执行健康、效果质量、效率成本、安全合规。

## 决策

采用**端到端可观测性体系**：后端埋点 → SQLite 存储 → 观测 API → React 前端。分阶段实施，后端先行。

### 核心原则：先有骨，再有皮

前端只是可视化层，数据来自后端。若先写前端，只能展示"成功/失败"的空壳或用 mock 自欺。因此阶段顺序固定为：埋点与存储 → 观测 API → 前端页面。

### 关键事实（已实测验证，非推测）

对已安装的 agentscope 2.0.12 做 introspect，确认观测数据由框架原生提供，无需手搓：

- `Agent.reply_stream()` 返回异步事件流，事件类型包括：`ReplyStartEvent` / `ReplyEndEvent` / `ExceedMaxItersEvent` / `ModelCallStartEvent` / `ModelCallEndEvent` / `TextBlock*` / `ThinkingBlock*` / `ToolCallStartEvent` / `ToolCallEndEvent` / `ToolResultStartEvent` / `ToolResultEndEvent` / `RequireUserConfirmEvent` 等 20+ 种。
- `MiddlewareBase` 提供钩子：`on_reply`、`on_reasoning`、`on_acting`、`on_model_call`、`on_check_permission`、`on_system_prompt`、`on_compress_context`。
- `Agent.reply()` 是一次性返回 `Msg` 的同步语义接口（coroutine），拿不到过程事件。**当前 `/run` 用的正是 `reply()`，必须改为消费 `reply_stream()` 才能获得过程数据。**

### 架构约束的遵守方式

`AGENTS.md` 规定 `agent.py` 是唯一允许直接 `import agentscope` 的业务装配点。本方案不破坏该约束：

- 事件消费逻辑留在 `main.py`，**只用属性访问（duck typing）读取事件字段，不 import agentscope 事件类型**；
- 转换为普通 `dict` 后交给 `observability/` 层，该层完全框架无关，不 import agentscope；
- 未来的中间件装配若在 `agent.py` 内进行，仍保持单一装配点。

## 数据模型：运行好坏的四类信号

统一健康分（0-100）由四类加权聚合，权重写入配置（保持"配置单一来源"），Dashboard 顶部展示总分，下钻看明细。

| 类别 | 指标 | 数据来源 |
|---|---|---|
| 1. 执行健康度 | 成功率/失败率、是否 `ExceedMaxIters` 未收敛、工具调用成功/失败次数、重试/降级次数 | `reply_stream` 事件 |
| 2. 效率与成本 | 端到端 P50/P95 延迟、首 token 延迟、输入/输出 token、单次成本 | `ModelCallEndEvent.usage` + 时间戳 |
| 3. 效果质量 | correctness 评分、人工 👍/👎、`LLM-as-judge` 自动评判、黄金测试集通过率 | `eval/scorer.py` + 人工反馈 |
| 4. 安全合规 | 权限拒绝次数、越界路径访问、写操作审计、safety 评分 | `on_check_permission` 钩子 |

前两类在 P0-P2 即可实现（数据现成），第三类需逐步引入评测机制，第四类待权限引擎真正接入工具后完善。

## 技术栈

用户已确认选型：

| 层 | 选型 | 理由 |
|---|---|---|
| 前端框架 | React 18 + TypeScript + Vite | Dashboard 交互复杂（筛选、瀑布图、下钻），React 生态最合适；Vite 构建快 |
| 图表 | Recharts | 覆盖趋势线/柱状/饼图，声明式且轻 |
| 数据请求 | TanStack Query | 自动轮询、缓存、加载态，适配准实时观测面板 |
| 存储 | SQLite（stdlib `sqlite3`） | 零运维、无新依赖；上层加 repository 层，将来迁移 Postgres 只改一处 |
| 样式 | 纯 CSS / CSS Modules | 不引入重型 UI 库 |

注：main 已由 PR #7 升级 CI action 至 Node 24 兼容版本，前端构建接入 CI 的基础已具备。

## 详细设计

### 存储层（`observability/repository.py`）

三张表：

- `runs`：`id`、`session_id`、`domain`、`status`、`input`、`output`、`started_at`、`ended_at`、`duration_ms`、`model`、`total_tokens`、`health_score`
- `spans`：`id`、`run_id`、`type`（model / tool / permission）、`name`、`start_offset_ms`、`duration_ms`、`input`、`output`、`tokens`、`status`、`error`
- `eval_scores`：`id`、`run_id`、`dimension`（correctness / performance / safety）、`score`、`source`（human / judge / rule）、`rater`、`created_at`、`comment`

全部通过 repository 函数访问，业务代码不直接写 SQL。

### 观测 API（FastAPI）

| 端点 | 用途 |
|---|---|
| `GET /api/runs` | 运行列表，支持 domain / status / 评分 / 时间窗筛选与分页 |
| `GET /api/runs/{id}` | 单次运行详情 |
| `GET /api/runs/{id}/spans` | 该次运行的时间线 span 列表 |
| `GET /api/metrics/summary` | 汇总指标（健康分、成功率、P95、成本） |
| `GET /api/metrics/timeseries` | 趋势序列 |
| `POST /api/feedback/{id}` | 人工反馈 👍/👎 + 评论，写入 `eval_scores` |
| `GET /api/eval/trends` | 质量维度趋势 |

### `/run` 改造

从 `await agent.reply(msg)` 改为消费 `agent.reply_stream(msg)`：

- HTTP 层仍可返回非流式 JSON（聚合所有事件后 `text_from_msg` 提取最终文本），保持现有测试契约不变；
- 同一套事件流未来可直接用于 SSE 流式输出（对应 README Roadmap 第 4 项），无需二次改造；
- 事件消费过程中同步写入 `runs` 与 `spans`，异常路径也要落库（记录 status=failed 与错误堆栈）。

### 前端页面（五个）

1. **总览 Dashboard**：健康分 / 成功率 / P95 / Token 成本四个指标卡、健康度趋势线、最近运行列表。
2. **运行列表**：按域、状态、评分、时间筛选，点击进入详情。
3. **Trace 详情**：瀑布时间线（模型调用 / 工具调用 / 权限决策的起止与耗时），展开查看每步输入输出。
4. **质量评估**：correctness / performance / safety 三维评分、人工反馈、`LLM-as-judge` 结果、黄金集回归通过率。
5. **成本 / 性能**：token 与成本趋势、延迟分布。

### CI 接入

在 `.github/workflows/ci.yml` 增加 Node setup + `npm run build`（独立 job 或并入 quality）。当前 CI 已有 Python 五道门禁（ruff / format / mypy / vulture / check_gates），前端构建作为新增检查项。

## 分阶段实施计划

每个阶段一个独立 worktree + 分支 + PR，CI 通过后才 squash 合并，遵守 `AGENTS.md`。

| 阶段 | PR 内容 | 可验证产出 |
|---|---|---|
| P0 地基 | 数据模型 + SQLite repository + 单测 | 能写入/读出一条 run |
| P1 采集 | `reply_stream` 消费 + recorder 接入 | 跑一次 `/run`，库里出现 run + spans |
| P2 API | 观测端点 + 健康分计算 | curl 拿到 summary 与 trace |
| P3 前端骨架 | React 工程 + 总览页 | 页面显示真实数据（非 mock） |
| P4 下钻 | 运行列表 + Trace 详情 | 点开一次运行看到完整时间线 |
| P5 闭环 | 质量评估 + 反馈 + CI 接前端 | 👎 反馈进入 `eval_scores` |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| `reply_stream` 改造影响现有 `/run` 测试契约 | 保持 HTTP 返回格式不变，聚合后再返回；新增事件流单测用假事件对象 |
| SQLite 并发写入 | 早期单进程足够；repository 层隔离，将来换 Postgres 不改业务 |
| 效果质量（第三类）依赖评测机制，短期做不实 | 先做人工反馈 👍/👎（最真实的质量信号），`LLM-as-judge` 后置 |
| 敏感数据（用户输入、prompt）落库 | 数据库文件放 `workspace/`（已 gitignore），不提交；后续加脱敏开关 |
| 前端构建拖慢 CI | 独立 job 并行；构建产物不提交 |

## 备选方案（已否决）

- **只做前端 + mock 数据**：无法验证真实性，否决。
- **Streamlit 纯 Python 快速原型**：交互定制性差，不适合精细可观测面板，否决。
- **直接上 OpenTelemetry + Jaeger**：对当前阶段过重，违背"用内置不手搓"与零运维原则，否决；将来规模上来可在 repository 层之上替换。
- **在 `observability/` 里 import agentscope 事件类型**：破坏单一装配点约束，否决；改用 duck typing 转换普通 dict。
