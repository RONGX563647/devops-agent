# SPEC-0004：生产级评测平台（DeepEval 打分 + Langfuse 可视化 + 统一门禁）

- 状态：Superseded by SPEC-0005（自托管可视化与额外中间件被否决）
- 日期：2026-11-25
- 取代：SPEC-0003（其“DeepEval 只替换打分引擎”的结论被本文吸收，范围扩大）
- 相关：`src/devops_agent/eval/*`、`src/devops_agent/observability/*`、
  `src/devops_agent/executor/boundary.py`、`web/src/pages/*`、
  `.github/workflows/{ci,eval-nightly}.yml`、ADR-0001、SPEC-0002

## 1. 决策摘要

生产级评测不是“把 judge.py 换成 DeepEval”，而是把评测拆成四个可独立演进的平面：

| 平面 | 选型 | 职责 | 不做什么 |
| --- | --- | --- | --- |
| 控制面 | 现有 FastAPI + React + PostgreSQL | 题库版本、批次编排、门禁判决、基线审批、审计 | 不存 trace 明细，不自建图表引擎 |
| 执行面 | 独立 worker + 容器沙箱（Docker/K8s Job） | 每用例隔离运行 AgentScope、复制 fixture、限时限网 | 不在 API 进程里跑模型 |
| 打分面 | DeepEval（`GEval` 等）+ 确定性规则 | LLM 指标、reason、阈值；规则断言保持纯函数 | 不把门禁判决交给库 |
| 可视化面 | **Langfuse 自托管**（trace / dataset / experiment / score / annotation） | 单用例瀑布、指标、证据、人工标注、实验对比 | 不承接门禁与 CI 退出码 |

**一句话**：DeepEval 负责“打分”，Langfuse 负责“看见”，我们的控制面负责“能不能上线”。
三者解耦，任何一层可替换，且门禁路径不依赖可视化层可用性。

### 1.1 为什么不是其他方案

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| DeepEval + Confident AI | 否 | 样本出网、数据驻留、账号体系与本地部署冲突，且门禁被 SaaS 绑定 |
| 纯自研 React 可视化 | 否（仅保留薄控制台） | trace 瀑布、dataset/experiment、标注、成本图全自建 = 再次造轮子，16k 行前端已证明维护成本 |
| Arize Phoenix 自托管 | 备选 | OTel 原生、部署轻，但多用户鉴权/RBAC 较弱，适合作为 PoC 或只读旁路 |
| promptfoo | 仅 CI 补充 | 断言与回归友好，Web viewer 不是多用户生产服务 |
| MLflow | 否 | 通用实验跟踪，agent trace 与人工标注体验弱于 Langfuse |
| **Langfuse 自托管** | **采用** | LLM 专用：trace、dataset、experiment、score、annotation、成本、API 齐全；OSS 可自托管 |

> Langfuse 的 SSO / 细粒度 RBAC / 审计可能属于企业版能力。生产落地前必须核对
> 目标版本的 OSS 功能边界；若不满足，降级方案是“反向代理统一认证 + 项目级 API Key”，
> 或退回 Phoenix 只读旁路，控制面鉴权仍由我们自己做。

## 2. 生产定义（先定标尺）

| 维度 | 目标 |
| --- | --- |
| 题库规模 | 40 → 500 例，单批可分片 |
| 并发 | 4 个 worker，10 个并行批次，单 provider 令牌桶限流 |
| 可用性 | 控制面 99.5%；可视化层故障不影响门禁 |
| 性能 | 批次入队到开跑 p95 < 30s；API p95 < 300ms；SSE 进度延迟 < 2s |
| 一致性 | 同一 `suite_sha + engine_version + metric_version` 的结果可复现 |
| 安全 | 沙箱默认无外网；密钥不出 worker；所有写操作可审计 |
| 数据 | 结果保留 1 年，trace/artifact 90 天，可按批次删除 |

部署形态：单机 Docker Compose（团队内网）为默认；同一套镜像可平移 K8s
（Deployment + Job + HPA 按队列深度）。

## 3. 架构

```
                    ┌──────────────── React 控制台 ────────────────┐
                    │ 概览 / 题库 / 批次 / 对比 / 基线 / 评审 / 运维 │
                    └───────────────┬──────────────────────────────┘
                                    │ REST + SSE（OpenAPI 生成 TS 类型）
┌───────────────┐          ┌────────▼────────┐          ┌──────────────────┐
│ CI / CLI gate │─────────►│  FastAPI 控制面  │◄────────►│ PostgreSQL       │
└───────────────┘          │ 鉴权/RBAC/审计   │          │ 批次/门禁/基线    │
                           └───┬─────────┬───┘          └──────────────────┘
                               │         │
                     enqueue   │         │ artifacts（S3/MinIO）
                               ▼         ▼
                     ┌──────────────┐  ┌──────────────┐
                     │ Redis Streams│  │ 对象存储      │
                     └──────┬───────┘  └──────▲───────┘
                            │                 │
                     ┌──────▼─────────────────┴──────┐
                     │ Eval Worker（可横向扩）         │
                     │  ├ 沙箱容器：AgentScope runner │
                     │  ├ 确定性规则判卷（graders.py） │
                     │  └ DeepEval 指标（GEval…）      │
                     └──────┬───────────────┬────────┘
                            │ OTel spans    │ scores / dataset run
                            ▼               ▼
                     ┌────────────┐  ┌──────────────┐
                     │ Langfuse   │  │ 结果回写 PG   │
                     │ 自托管 UI   │  │ + SSE 推送    │
                     └────────────┘  └──────────────┘
```

关键约束：

1. **门禁路径只读 PostgreSQL**。Langfuse 宕机不阻止 nightly 出结论。
2. **trace 只存一份**：worker 发出 OTel span 到 Langfuse；本地只存 `trace_uri`
   与用例摘要，避免两套 trace 存储。
3. **控制面不跑模型**：API 只做校验、入队、查询；模型调用全部在 worker 沙箱。
4. **评测引擎可切换**：`engine=deepeval|legacy` 是批次字段，跨引擎对比阻断。

## 4. 数据模型（PostgreSQL，Alembic 管理）

| 表 | 关键列 | 说明 |
| --- | --- | --- |
| `eval_suites` | `id, name, version, sha256, manifest_json, created_by, created_at` | 题库快照；JSONL 仍进 git，入库的是不可变快照 |
| `eval_suite_cases` | `suite_id, case_key, payload_json` | 供 UI 检索/过滤，避免每次解析 JSONL |
| `eval_batches` | `id, suite_id, engine, engine_version, metric_version, prompt_sha, params_sha, model, profile, judge_model, judge_scope, status, verdict, summary_json, budget_usd, created_by, started_at, finished_at` | 批次事实源 |
| `eval_case_results` | `batch_id, case_key, status, passed, hard_failures, judge_scores_json, judge_evidence_json, latency_ms, tokens, cost_usd, trace_uri, error` | 唯一约束 `(batch_id, case_key)`，支持重跑 upsert |
| `eval_metric_results` | `case_result_id, metric_name, score, threshold, success, reason, samples` | 逐指标明细，前端按指标聚合 |
| `eval_baselines` | `id, suite_id, batch_id, active, promoted_by, approved_by, created_at` | 同一 suite 仅一条 `active`；提升需审批 |
| `eval_annotations` | `case_result_id, user_id, label, comment, created_at` | 人工反馈，可回写 Langfuse score |
| `eval_artifacts` | `id, batch_id, kind, uri, sha256, expires_at` | report/cases/原始响应，生命周期清理 |
| `audit_log` | `actor, action, target, before_json, after_json, at` | 基线提升、取消、删除、改阈值必须留痕 |

约束：`eval_batches.suite_id` 存快照而非可变引用；所有状态变更带 `updated_at` 与版本号，
避免并发覆盖。迁移采用 expand/contract：先加列、双写、回填、再删旧列。

## 5. 执行流程

```
POST /api/eval/batches（Idempotency-Key）
  → 校验 suite sha / engine / 预算 / 权限
  → INSERT batch(status=queued) + 审计
  → XADD eval.jobs
Worker:
  → 认领（consumer group，visibility timeout，心跳）
  → status=running；创建 Langfuse dataset run（experiment）
  → 逐例：沙箱容器 → fixture 只读挂载 → AgentScope 运行
          → OTel span（model/tool/permission）→ Langfuse
          → 确定性判卷 → DeepEval 指标（受 max_concurrent 限流）
          → UPSERT case_result + metric_results；SSE 推送
  → 聚合 gates/verdict → batch=completed → 镜像 summary 到 Langfuse
  → 失败：指数退避重试；超过上限进 DLQ，批次标 failed 并保留已完成用例
```

幂等与恢复：批次级 `Idempotency-Key`；用例级 `(batch_id, case_key)` upsert；
worker 崩溃后按已完成用例跳过；取消通过 Redis 标记 + 沙箱 kill，状态落 `cancelled`。

## 6. 打分与门禁

### 6.1 指标映射（沿用 SPEC-0003 的边界）

| 现有 | 生产实现 | 是否门禁 |
| --- | --- | --- |
| `correctness` 1–5 | `GEval`，阈值 0.8（=4/5） | 是 |
| `coherence/relevance/objectivity/clarity/conciseness` | 各一个 `GEval`，可裁剪 | 否 |
| 规则断言（contains/regex/cite/file_state） | 自定义 `RuleMetric` 包装 `graders.grade()` | 是（pass_rate） |
| 工具三档 / 预算 / 顺序 | 保留 `graders._tool_score`；`ToolCorrectnessMetric` 仅展示 | 是（trajectory） |
| 硬失败分类 | `kinds.py` 不变 | blocking 一票否决 |

`tools_called` 只放 `executed_tools`；`denied_tools` 进 metadata，防止越权被拒被判命中。

### 6.2 门禁（控制面实现，不使用库的 success 字段）

- `pass` / `fail` / `insufficient` / `not_comparable` 语义与现有 CLI 完全一致。
- 新增阻断因子：`engine`、`engine_version`、`metric_version`；跨引擎/跨指标版本
  对比直接 `not_comparable`，不做 delta 归因。
- 基线提升走独立 API：要求 `suite_sha` 一致、有审批人、写审计；禁止 CI 自动提升。
- nightly 仍走 CLI gate（读 PG 或 report.json），`fail`/`warn` 非零、`insufficient` 为零。

### 6.3 成本控制

- 批次级 `budget_usd`，超预算先停裁判、再停批次并标 `budget_exceeded`。
- 裁判结果按 `(case_key, prompt_sha, model, metric_version)` 缓存。
- 默认 nightly 只跑 `correctness + objectivity + relevance`；全六维需显式开启。
- 逐用例记录 `tokens`、`cache_input_tokens`、`cost_usd`，前端可下钻。

## 7. 前端可视化（重点）

### 7.1 信息架构

| 路由 | 页面 | 核心内容 |
| --- | --- | --- |
| `/quality` | 概览 | 当前 verdict、门禁卡、pass_rate 趋势、硬失败堆叠、成本、活跃基线 |
| `/quality/suites` | 题库 | 版本/sha/配比、用例检索（scenario/subject/tag/档位）、S1–S8 findings |
| `/quality/batches` | 批次 | 状态、引擎/模型/档位、进度（SSE）、触发人、耗时 |
| `/quality/batches/:id` | 批次详情 | 门禁表、指标分布、用例表、硬失败拆解、对比入口 |
| `/quality/cases/:resultId` | 用例详情 | 事件时间线、工具三档、规则逐条、六维分数+reason、原始输出、Langfuse 链接 |
| `/quality/compare` | 对比 | 指标 delta + MDE、可比性横幅、用例翻转矩阵、回归清单 |
| `/quality/reviews` | 人工评审 | 待标注队列、👍/👎+评论、仲裁、导出 |
| `/ops/eval` | 运维（管理员） | 队列深度、worker 心跳、裁判失败率、限流、预算 |

### 7.2 可视化组件与数据

- **用例时间线**：直接嵌入/深链 Langfuse trace；本地只渲染摘要（工具三档、耗时、token）。
  深链携带 `batch_id/case_key`，避免自建瀑布图。
- **门禁卡**：三态颜色 + 原因（`n_below_gate` / `no_samples` / `samples_below_min`），
  禁止把 `insufficient` 画成红色失败。
- **指标分布**：小样本用散点+中位数，不用饼图；judge 维度显示均值、样本数、阈值线。
- **对比页**：表格 + 翻转矩阵（base→head），顶部横幅显示 `blocking/confounded/clean/unknown`
  及原因；回归项可一键生成 issue/PR 评论。
- **证据卡**：score、threshold、`reason`、rubric 锚点（从 `peem.v1.md` 读取），
  支持“标注为误判”回写 `eval_annotations`。
- **SSE**：`GET /api/eval/batches/{id}/events` 推送 `queued/running/case_done/completed`；
  断线重连带 `Last-Event-ID`，前端用 React Query 缓存做补偿。

### 7.3 前端工程约束

- OpenAPI → TS 类型自动生成，禁止手写重复接口类型（现有 `types.ts` 899 行的教训）。
- 表格虚拟化（500 例 × 多批），筛选状态进 URL，可分享。
- 图表统一用一套库；空态/错误态/加载态必须显式设计。
- Playwright 覆盖：发起批次→进度→用例详情→对比→标注 全链路。
- 可访问性：颜色不是唯一状态编码（图标+文案）；键盘可达。

## 8. API 契约（新增/调整）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/eval/batches` | 发起批次；`Idempotency-Key`，支持 `budget_usd`、`limit`、`engine` |
| GET | `/api/eval/batches` | 游标分页 + 状态/引擎/时间过滤 |
| GET | `/api/eval/batches/{id}` | 批次详情（summary + gates） |
| GET | `/api/eval/batches/{id}/events` | SSE 进度 |
| POST | `/api/eval/batches/{id}/cancel` | 取消（RBAC：runner/admin） |
| GET | `/api/eval/batches/{id}/cases` | 用例结果分页/过滤 |
| GET | `/api/eval/case-results/{id}` | 用例详情 + 指标 + 证据 + trace_uri |
| GET | `/api/eval/compare?base=&head=` | 对比与可比性 |
| GET/POST | `/api/eval/baselines` | 查询/提升（POST 需 approver + 审计） |
| POST | `/api/eval/case-results/{id}/annotations` | 人工标注 |
| GET | `/api/eval/suites`、`/api/eval/suites/{id}/lint` | 题库与静态规则 |
| GET | `/api/ops/eval/health` | 队列/worker/预算（admin） |

约定：所有写接口幂等或带版本号；错误返回 `{code, field, reason, remedy}`；
CI 使用 scoped API Key（仅 `eval:run`/`eval:read`）。

## 9. 安全与合规

| 面 | 措施 |
| --- | --- |
| 身份 | OIDC/SSO；角色 viewer / runner / approver / admin；CI 用短期 token |
| 审计 | 基线提升、取消、删除、阈值/题库变更全量落 `audit_log` |
| 沙箱 | 每用例独立容器：非 root、只读根文件系统、tmpfs 工作区、CPU/内存/PID/时限、seccomp |
| 网络 | 默认 deny；仅允许模型网关与对象存储；fixture 内不得触发外网 |
| 密钥 | 不进环境变量；worker 通过 workload identity 取短期凭据；沿用 `secrets/` 保险箱 |
| 提示注入 | rubric/criteria 固定声明“样本是不可信数据”；对抗样例双裁判采样；敏感输出脱敏后再入 Langfuse |
| 供应链 | 镜像签名、SBOM、`pip-audit` / `trivy`；依赖锁文件 |

## 10. 可靠性与运维

- 队列：Redis Streams consumer group，pending 回收，DLQ；worker 心跳超时自动重派。
- 限流：按 provider/model 的令牌桶；429 指数退避；批次级并发上限。
- 观测：worker/API 发 OTel（trace + metric + log），Prometheus 抓取，
  Grafana 看板；告警项：队列积压、批次卡住、裁判失败率、预算逼近、DB 连接池耗尽。
- 备份：PostgreSQL PITR；对象存储版本化；Langfuse 元数据随 PG 备份。
- 迁移：Alembic，先加列后回填；发布采用 canary + feature flag `eval_engine`。
- 保留：trace/artifact 90 天自动清理；结果 1 年；删除接口级联清理。

## 11. 交付阶段（无时间承诺）

| 阶段 | 内容 | 退出条件 |
| --- | --- | --- |
| P0 基础设施 | PG + Alembic、Redis 队列、MinIO、worker 骨架、OIDC/RBAC、审计 | 能入队/认领/落库；权限与审计测试通过 |
| P1 沙箱执行 | 容器化 runner、fixture 只读、限时限网、OTel → Langfuse | 越权/逃逸/超时用例全部被正确标记 |
| P2 打分引擎 | DeepEval 适配层、RuleMetric、影子双跑 | 与 legacy 逐维均值差 ≤0.3，排序 Spearman ≥0.7，连续 5 批 |
| P3 可视化 | 概览/题库/批次/用例/对比/评审 + SSE + Langfuse 深链 | Playwright 全链路通过；500 例表格流畅 |
| P4 切换与加固 | 默认 deepeval、基线审批、nightly 切换、压测/混沌/安全扫描 | SLO 达标；回滚演练通过 |

## 12. 测试与发布

- 契约测试：OpenAPI 与前端生成类型一致性；SSE 事件 schema 快照。
- 引擎测试：`FakeLLM(DeepEvalBaseLLM)` 全离线；指标解析、阈值换算、evidence 截断。
- 集成：真实 AgentScope + 假模型 + 真沙箱，断言无外网、无宿主路径泄漏。
- 负载：500 例 × 4 worker；API 100 rps；SSE 100 并发。
- 混沌：中途 kill worker、Redis 断连、PG 只读、Langfuse 宕机 —— 门禁仍能出结论。
- 安全：`pip-audit`、`trivy`、沙箱逃逸用例、提示注入样本回归。
- 发布：expand/contract 迁移；`eval_engine` 灰度；回滚只切 flag，不删数据。

## 13. 成本

| 项 | 说明 |
| --- | --- |
| 基础设施 | PG/Redis/MinIO/Langfuse（含 ClickHouse）单机约 4–8 GB 内存；K8s 按需 |
| 模型 | 13 条裁判用例 × 3 维 ≈ 39 次/批；全六维 ≈ 78 次/批；缓存可显著降低 |
| 可视化 | Langfuse 自托管无按量费用；企业版 SSO/RBAC 需单独评估 |

预算控制是批次字段，不是口头约定：超预算先降级裁判维度，再中止并标注原因。

## 14. 风险与回滚

| 风险 | 缓解 | 回滚 |
| --- | --- | --- |
| 评测与可视化双写不一致 | 门禁只读 PG；Langfuse 异步镜像，允许最终一致 | 关闭镜像开关 |
| Langfuse 运维复杂（ClickHouse） | Compose 固化版本与备份；单机资源上限 | 可视化降级为只读链接，控制面不受影响 |
| 裁判漂移 | 影子双跑 + `engine/metric_version` 阻断因子 | 切 `engine=legacy`，基线回退 |
| 沙箱逃逸/数据外泄 | 非 root、只读、无网、seccomp、审计 | 立即停 worker，批次标 quarantined |
| 依赖/鉴权不满足 | P0 核对 Langfuse OSS 功能边界 | 反向代理认证或 Phoenix 旁路 |

## 15. 验收标准

1. 500 例批次在 4 worker 下完成，无跨用例污染；kill worker 后可恢复且结果不重复。
2. 门禁判决与现有 CLI 对确定性用例完全一致；`insufficient` / `not_comparable` 语义不变。
3. `/quality` 五个页面 + 用例详情 + 对比 + 标注全链路可用；SSE 进度 <2s。
4. 跨引擎批次自动 `not_comparable`，原因含 `engine` / `metric_version`。
5. Langfuse 宕机时 nightly 仍能产出 `report.json` 与退出码。
6. 所有写操作有审计；CI key 无法提升基线；沙箱无外网。
7. 单测覆盖率 ≥80%，复杂度门禁通过；压测、混沌、安全扫描达标。

## 16. 未决问题

1. Langfuse 目标版本的 OSS 鉴权/RBAC/审计能力边界（决定是否需要企业版或代理认证）。
2. 单机 Compose 是否足够，还是首期就上 K8s（取决于团队现有平台）。
3. trace 采样率：全量还是错误/慢例全量 + 成功例采样。
4. 是否保留现有 `/runs`、`/dashboard`、`/cost` 页面，还是逐步收敛到 Langfuse。
5. 六维全量 vs 三维默认的成本/信息量取舍（由 P2 数据决定）。
