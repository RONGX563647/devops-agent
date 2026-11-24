# SPEC-0005：内嵌式评测平台（现有 FastAPI + SQLite + React，不新增服务）

- 状态：Superseded by SPEC-0006（指标矩阵、校准与硬性红线被 0006 吸收）
- 日期：2026-11-25
- 取代：SPEC-0004（自托管 Langfuse 与 Postgres/Redis/MinIO 方案被否决）
- 相关：`src/devops_agent/{main.py,eval/*,observability/*,executor/boundary.py}`、
  `web/src/{App.tsx,api,types.ts,pages/EvalBatches.tsx,pages/quality/*}`、
  `.github/workflows/{ci,eval-nightly}.yml`

## 1. 约束与结论

硬约束：

1. **不引入任何需要自部署的第三方服务**（Langfuse / Phoenix / MLflow / Confident AI）。
2. **不引入新的数据库或中间件**（Postgres / Redis / ClickHouse / MinIO / Celery / MQ）。
3. 必须**集成在现有项目内**：同一个 FastAPI 进程、同一个 SQLite 库、
   同一个 React 构建产物、同一个端口。

因此方案是：

| 能力 | 实现 | 说明 |
| --- | --- | --- |
| 任务队列 | SQLite 表 `eval_jobs` + `BEGIN IMMEDIATE` 认领 | 无 Redis；持久化、可恢复 |
| 后台执行 | FastAPI 进程内 `asyncio` 调度 + 每用例子进程 | 不阻塞 API；单机单进程 |
| 数据存储 | 现有 SQLite（WAL）+ `workspace/evals/` 本地文件 | 不引入对象存储 |
| 打分 | DeepEval 作为**库**（可选 extra）+ 现有确定性规则 | 关闭云与遥测；可回退 |
| 可视化 | 现有 React + React Query + Recharts | 新增 `/quality` 子页面 |
| 进度推送 | 现有 SSE 机制，读 SQLite 快照 | 无 Redis pub/sub |
| 鉴权 | SQLite `users` / `api_tokens` + 本地会话 | 无外部 IdP |

**DeepEval 的定位**：可选的指标库，不是平台。若其依赖或遥测无法满足离线要求，
保留现有 `OpenAIJudge` 作为等价实现，平台能力不受影响。

### 1.1 生产级边界（必须写清）

这套方案的“生产级”指**单节点可长期运行**：持久化、崩溃恢复、取消、审计、备份、
限流、数据保留、可观测。它**不提供**：

- 多节点水平扩展与高可用；
- 多 API worker 间的一致性（默认 `uvicorn --workers 1`）；
- 容器级网络隔离（无 Docker 依赖时，恶意仓库的防护弱于容器）。

若将来必须多节点或强隔离，再按 SPEC-0004 演进；本方案不为其预留中间件。

## 2. 架构

```
┌──────────────────────── React（现有构建产物） ────────────────────────┐
│ /quality 概览 / 题库 / 批次 / 批次详情 / 用例详情 / 对比 / 评审 / 运维  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ REST + SSE（同端口）
┌───────────────────────────────▼──────────────────────────────────────┐
│ FastAPI（单进程）                                                     │
│  ├ Eval API：批次、SSE、用例、对比、基线、标注、健康检查、鉴权          │
│  ├ EvalService：asyncio 调度循环（启动即恢复未完成 lease）              │
│  │    ├ SQLite 队列认领 / 心跳 / 取消                                  │
│  │    ├ CaseRunner：子进程执行 AgentScope（超时 kill + rlimit）         │
│  │    ├ graders：确定性判卷（纯函数）                                   │
│  │    └ Metrics：DeepEval GEval（可选）+ RuleMetric                    │
│  ├ SQLite（WAL）：eval_* / users / api_tokens / audit                   │
│  └ workspace/evals：report.json/md、cases.jsonl、trace.jsonl、日志      │
└──────────────────────────────────────────────────────────────────────┘
```

约束：

- **不新增端口、不新增进程依赖**；`python -m devops_agent.main` 启动即包含评测服务。
- API 进程只做校验/查询/入队；模型调用发生在子进程，避免阻塞事件循环。
- 门禁结论只读 SQLite；可视化失败不影响 nightly。
- 所有新增表与列走同一 migration，不破坏现有观测数据。

## 3. 数据模型

### 3.1 现有表扩展（`ALTER TABLE`，不能依赖 `CREATE IF NOT EXISTS`）

| 表 | 新增列 |
| --- | --- |
| `eval_batches` | `engine TEXT`、`engine_version TEXT`、`metric_version TEXT`、`progress_total INT`、`progress_done INT`、`cancel_requested INT DEFAULT 0`、`created_by TEXT`、`budget_usd REAL`、`cost_usd REAL`、`updated_at TEXT` |
| `eval_cases` | `judge_evidence_json TEXT`、`hard_failures_json TEXT`、`trace_path TEXT`、`cost_usd REAL`、`error TEXT` |

### 3.2 新增表

| 表 | 关键列 | 用途 |
| --- | --- | --- |
| `eval_jobs` | `id, batch_id UNIQUE, status, attempts, available_at, lease_until, worker_id, created_at, updated_at` | 持久化队列 |
| `eval_metrics` | `id, case_result_id, metric, score, threshold, success, reason, samples` | 逐指标明细（前端按指标聚合） |
| `eval_case_steps` | `id, case_result_id, seq, type, name, state, start_ms, duration_ms, payload_json` | 紧凑轨迹（不存 delta） |
| `eval_baselines` | `id, suite, suite_sha, batch_id, active, promoted_by, approved_by, created_at` | 每套题仅一条 active |
| `eval_annotations` | `id, case_result_id, user_id, label, comment, created_at` | 人工评审 |
| `eval_audit` | `id, actor, action, target, detail_json, created_at` | 基线提升/取消/删除留痕 |
| `eval_artifacts` | `id, batch_id, kind, path, sha256, bytes, expires_at` | 报告与轨迹文件生命周期 |
| `users` | `id, username UNIQUE, password_hash, role, active, created_at` | 本地鉴权 |
| `api_tokens` | `id, name, token_hash, scopes, expires_at, created_at` | CI 只读/可运行令牌 |

### 3.3 SQLite 运行参数

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

- 所有写事务短小；批量写入按用例提交，避免长事务阻塞 SSE/API。
- 迁移用 `PRAGMA user_version` 递增；每一步可重入。
- `workspace/evals/` 与 DB 同机；路径写入前 `resolve()` 校验，禁止越界。

## 4. 队列与执行

### 4.1 认领（无 Redis）

```sql
BEGIN IMMEDIATE;
UPDATE eval_jobs
SET status='running', worker_id=?, lease_until=?, attempts=attempts+1, updated_at=?
WHERE id = (
  SELECT id FROM eval_jobs
  WHERE status='queued' AND available_at<=?
  ORDER BY id LIMIT 1
)
AND status='queued'
RETURNING id;
COMMIT;
```

- 心跳每 10s 更新 `lease_until`；启动时把 `lease_until < now` 的 `running` 重置为
  `queued`（崩溃恢复），已完成的用例按 `(batch_id, case_id)` upsert 跳过。
- `asyncio.Event` 用于本地唤醒；另设 1s 轮询兜底，避免漏唤醒。
- 取消：`cancel_requested=1`；调度器在用例边界中止，运行中的子进程被 kill，
  批次标 `cancelled`，已完成结果保留。
- 重试：`attempts < max_attempts` 时指数退避重排；超限进 `failed`，不影响其他批次。

### 4.2 用例执行（子进程隔离）

```
parent: 写 job.json（case、workdir、超时、允许的环境变量）
  → subprocess: python -m devops_agent.eval.case_worker --job job.json
      ├ 设置 rlimit（POSIX：CPU/AS/NOFILE）
      ├ 构建 AgentScope runtime（workdir = 临时目录）
      ├ 消费事件 → trace.jsonl（仅 type/name/state/耗时/token，不存 delta）
      ├ 规则判卷 → result.json
      └ 退出
  → parent: 读取 result.json/trace.jsonl，落库；超时 kill 进程组
```

- 子进程崩溃只影响一条用例；父进程记录 `status=error` 与 stderr 摘要。
- 默认并发：全局 `EVAL_MAX_CONCURRENCY=2`，按 provider 再限流；
  40 例规模下单机足够。
- 工作区：`workspace/evals/runs/<batch_id>/<case_id>/`，用例结束清理；
  fixture 复制沿用现有 `_fixture_path_error` 校验。
- 环境变量白名单：只透传模型凭据相关项；子进程不继承服务端密钥。

### 4.3 轨迹持久化（可视化的前提）

- `eval_case_steps` 只存结构化步骤：`MODEL_CALL_START/END`、`TOOL_CALL_START`、
  `TOOL_RESULT_END`、`REQUIRE_USER_CONFIRM`、`REPLY_END`。
- `TEXT_BLOCK_DELTA` 不入库；最终文本存 `eval_cases.detail_json`。
- 单用例步骤上限 2000，单字段 payload 上限 4KB，超出截断并标记；
  防止长会话把 SQLite 撑爆。

## 5. 打分与门禁

### 5.1 指标

| 现有 | 实现 | 门禁 |
| --- | --- | --- |
| `correctness` 1–5 | DeepEval `GEval`，阈值 0.8（=4/5） | 是 |
| `coherence/relevance/objectivity/clarity/conciseness` | 各自 `GEval`，默认 nightly 只跑后三维中的 `objectivity/relevance` | 否 |
| 规则断言 | 现有 `graders.grade()`，包装为 `RuleMetric` 仅用于统一展示 | 是（pass_rate） |
| 工具三档/预算/顺序 | 沿用 `graders._tool_score`；`ToolCorrectnessMetric` 只展示 | 是（trajectory） |
| 硬失败 | `kinds.py` 不变 | blocking 一票否决 |

- `tools_called` 只放 `executed_tools`；被拒进 metadata，防止越权判成命中。
- DeepEval 结果换算回 1–5 分；`judge_evidence` 落 `eval_cases.judge_evidence_json`。
- 引擎异常降级：该用例 `judge=None` + `check(judge, ok=False)`，规则结论不变。

### 5.2 离线与合规

- DeepEval 只作为 `[eval]` extra；`DEEPEVAL_TELEMETRY_OPT_OUT` 等变量在 P0 实测确认。
- 禁止配置 `CONFIDENT_API_KEY`；单测 monkeypatch 网络，断言零外呼。
- 若依赖冲突或无法彻底关闭遥测：`engine=legacy` 使用现有 `OpenAIJudge`，
  UI/队列/存储/门禁全部不变。

### 5.3 门禁

- `pass/fail/insufficient/not_comparable` 与现有 CLI 完全一致；
  `engine`、`engine_version`、`metric_version` 为新增阻断因子。
- 基线提升需 `approver` 角色 + 审计；CI 令牌无提升权限。
- nightly 继续用 CLI 读 SQLite 或 `report.json`，`fail/warn` 非零、`insufficient` 为零。
- 成本：批次 `budget_usd`；超限先停裁判，再停批次并标 `budget_exceeded`；
  裁判结果按 `(case_id, prompt_sha, model, metric_version)` 缓存在 SQLite。

## 6. API（同一 FastAPI，同端口）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/eval/batches` | 发起批次；`Idempotency-Key`、`limit`、`engine`、`budget_usd` |
| GET | `/api/eval/batches` | 分页 + 状态/引擎过滤 |
| GET | `/api/eval/batches/{id}` | 详情 + gates |
| GET | `/api/eval/batches/{id}/events` | SSE：读 DB 快照，1s 节流 + 15s ping |
| POST | `/api/eval/batches/{id}/cancel` | 取消（runner/admin） |
| GET | `/api/eval/batches/{id}/cases` | 用例分页/过滤 |
| GET | `/api/eval/case-results/{id}` | 用例详情 + 指标 + 证据 |
| GET | `/api/eval/case-results/{id}/trace` | 轨迹步骤 |
| GET | `/api/eval/compare?base=&head=` | 对比 + 可比性 |
| GET/POST | `/api/eval/baselines` | 查询/提升（POST 需 approver） |
| POST | `/api/eval/case-results/{id}/annotations` | 人工标注 |
| GET | `/api/ops/eval/health` | 队列/lease/磁盘/裁判失败率（admin） |
| POST | `/api/auth/login`、`/api/auth/logout` | 本地会话 |

约定：错误统一 `{code, field, reason, remedy}`；写接口带 CSRF 校验；
CI 使用 scoped `api_tokens`。

## 7. 前端可视化（集成在现有 React）

现有技术栈已够用：React Router、TanStack Query、Recharts、原生 fetch/SSE。
**不新增运行时依赖**；不新增独立页面服务。

| 路由 | 内容 |
| --- | --- |
| `/quality` | verdict 横幅、门禁卡（三态）、pass_rate 趋势、硬失败堆叠、成本、活跃基线 |
| `/quality/suites` | 题库版本/sha/配比、用例检索、S1–S8 findings |
| `/quality/batches` | 批次列表、状态、引擎/模型/档位、进度、触发人、耗时 |
| `/quality/batches/:id` | 门禁表、指标分布、用例表、硬失败拆解、取消/对比入口 |
| `/quality/cases/:resultId` | 轨迹时间线、工具三档、规则逐条、六维分数+reason、原始输出 |
| `/quality/compare` | delta + MDE、可比性横幅、用例翻转矩阵、回归清单 |
| `/quality/reviews` | 待标注队列、👍/👎+评论、导出 |
| `/ops/eval` | 队列深度、lease 超时、磁盘、裁判失败率、预算（admin） |

实现要点：

- 复用 `web/src/api/client.ts` 与 `useRunStream` 的 SSE 模式，新增
  `useEvalStream(batchId)`；断线重连后用 React Query 重新拉快照。
- 图表统一 Recharts；小样本用散点+中位数，不用饼图；`insufficient` 用灰色，
  不能画成红色失败。
- 用例详情的时间线来自 `eval_case_steps`，本地渲染即可，无需第三方 UI。
- 表格先分页（现有 API 已支持 offset/limit），500 例不引入虚拟化依赖。
- `types.ts` 新增类型与后端 Pydantic 契约测试对齐；避免继续手抄分叉。
- `EvalBatches.tsx` 拆分组件，不重写；保留现有测试。

## 8. 鉴权与安全（内嵌，无外部 IdP）

- `users` + `hashlib.scrypt`（标准库）存密码；会话 Cookie `HttpOnly + SameSite=Lax`；
  HTTPS 下加 `Secure`。
- CSRF：双提交 Token；所有写接口校验。
- 角色：`viewer` / `runner` / `approver` / `admin`；基线提升仅 `approver/admin`。
- 启动引导：`python -m devops_agent.auth create-admin` 或一次性环境变量。
- `DEVOPS_AGENT_AUTH=off` 保留单用户开发模式，UI 顶部提示“未启用鉴权”。
- 子进程：环境变量白名单、密钥脱敏、trace 中不写 Authorization；
  复用 `secrets/` 保险箱。
- 路径：fixture/artifact 全部 `resolve()` + 根目录校验；禁止绝对路径与 `..`。
- 限流：进程内令牌桶（按用户/IP），无 Redis。
- 审计：登录、发起/取消批次、基线提升、删除、阈值变更。

## 9. 运维

| 项 | 方式 |
| --- | --- |
| 健康检查 | `/api/ops/eval/health`：队列深度、最老 queued、lease 超时数、磁盘余量 |
| 日志 | 结构化 JSON 到 stdout；`batch_id/case_id` 贯穿 |
| 指标 | `eval_metrics` 表 + health JSON；不引入 Prometheus |
| 备份 | `python -m devops_agent.eval backup --out <path>`（`VACUUM INTO`） |
| 恢复 | 停进程 → 替换 DB/`workspace/evals` → 启动；启动自动重排未完成 job |
| 保留 | 报告/轨迹 90 天，结果 1 年；定时清理 + `eval_artifacts.expires_at` |
| 磁盘 | 批次前检查余量；低于阈值拒绝入队并给出明确错误 |

## 10. 交付阶段

| 阶段 | 内容 | 退出条件 |
| --- | --- | --- |
| P0 | migration、`eval_jobs`、EvalService、本地鉴权、审计、health | 重启可恢复；权限/审计测试通过 |
| P1 | 子进程 CaseRunner、轨迹落库、SSE 进度、取消 | kill 子进程/父进程后状态正确；取消及时 |
| P2 | DeepEval 适配层、RuleMetric、影子双跑 | 与 legacy 均值差 ≤0.3、排序 Spearman ≥0.7，连续 5 批 |
| P3 | `/quality` 全部页面 + 对比 + 标注 + 运维页 | Playwright 全链路；500 例分页可用 |
| P4 | 默认切换、基线审批、nightly、压测/故障演练、文档 | 验收标准全绿；回滚演练通过 |

## 11. 测试

- 单元：队列认领/恢复/取消、migration 幂等、哈希与 CSRF、指标映射、路径校验。
- 集成：真实 SQLite + 假模型 + 子进程；断言无网络、无密钥泄漏。
- 故障：用例中途 kill、进程崩溃、DB busy、磁盘不足、SSE 断线重连。
- 负载：40 例 × 并发 2；API 在此期间 p95 < 300ms。
- 安全：`pip-audit`、路径逃逸、提示注入样本、越权访问。
- 覆盖率 ≥80%，复杂度门禁 ≤10；`ruff` / `mypy` / `vulture` 全绿。

## 12. 风险与回滚

| 风险 | 缓解 | 回滚 |
| --- | --- | --- |
| SQLite 写竞争 | WAL + 短事务 + 单 worker + busy_timeout | 降并发；必要时暂停评测 |
| 单进程故障 | 启动恢复 lease；子进程隔离；备份 | 恢复 DB/目录后重启 |
| 磁盘增长 | 步骤/字段截断、90 天清理、入队前余量检查 | 手动清理 artifact |
| DeepEval 遥测/依赖 | P0 实测并锁版本；失败即用 legacy judge | `engine=legacy` |
| 无容器隔离 | 子进程 + rlimit + 环境白名单；文档声明残余风险 | 仅对可信仓库开启 |
| 鉴权锁死 | CLI 重置管理员；`AUTH=off` 应急 | 关闭鉴权需审计记录 |

## 13. 验收标准

1. 不新增端口、不新增数据库/中间件/第三方服务；`pip install -e ".[eval]"` 后单进程可跑。
2. 重启进程后未完成批次自动续跑；已完成用例不重复计算。
3. 取消在 1 个用例内生效；子进程无残留。
4. `/quality` 六个页面 + 用例详情 + 对比 + 标注全链路可用；SSE 进度 <2s。
5. 确定性用例的门禁 verdict 与迁移前完全一致；跨引擎批次 `not_comparable`。
6. 鉴权、CSRF、RBAC、审计、限流均有测试；CI 令牌不能提升基线。
7. 单测覆盖率 ≥80%；`ruff` / `mypy` / `vulture` / 复杂度门禁通过。

## 14. 未决问题

1. 部署是否固定 `uvicorn --workers 1`（本方案默认）？多 worker 会牺牲 SSE 一致性。
2. 鉴权是否默认开启，还是内网单用户继续 `AUTH=off`？
3. DeepEval 若无法彻底关闭遥测，是否接受 legacy judge 作为默认引擎？
4. 轨迹保留 90 天是否满足复盘需求；是否需要按批次延长。
5. 是否允许在 Linux 上额外用 `unshare -n` 做网络隔离（非必需，不引入服务）。
