# SPEC-0013：Agent 记忆（内置中间件 + 轨迹可观测）

- 状态：方案（待评审）
- 日期：2026-11-27
- 基线：`main b90f909`
- 关系：承接 ADR-0001（可观测性前端）与 SPEC-0008（评测可信度）；事件通道与 `runtime/compression.py` 同构，前端沿用轨迹页签的步骤模型
- 范围：`src/devops_agent/memory/**`、`runtime/{assembly,scope_runtime,runtime}.py`、`observability/{recorder,repository,models,api}.py`、`gateway/routes/memory.py`、`config/memory.yaml`、`web/src/{types,api/stream/handlers,workspace}/**`、`tests/**`
- 前置阅读：README「agentscope 真实 API 映射」、AGENTS.md §3.4（能力用内置）、§3.8（前端改动必须先读 modern-web-guidance）
- 证据约定：现状结论带 `路径:行号`，基线 `b90f909`；设计建议与既存事实分开写

## 1. 结论

记忆不是“把历史消息再塞回上下文”，而是一层**有选择、有隔离、有生命周期、可解释**的能力。本 spec 的四条主张：

1. **优先用内置包**：长期记忆用 AgentScope 的 `Mem0Middleware` / `AgenticMemoryMiddleware`，任务态用 `AgentState`；不手搓向量库、摘要抽取与去重。
2. **四层记忆模型**：L0 工作记忆（上下文 + 压缩）、L1 会话情景（消息/会话快照）、L2 长期语义（本次落地）、L3 程序性（技能，后续）。L0/L1 已存在，本次只补 L2 的接线。
3. **记忆必须可观测**：读、写、跳过、失败都发 `MEMORY_*` 事件，落成 `spans(type='memory')`，轨迹页签新增「记忆」步骤行与泳道；这条是硬要求，不是可选增强。
4. **默认保守**：当前网关没有鉴权，`scope` 默认 `session`，不假装有跨用户记忆；记忆后端不可用时 fail-open，绝不阻断对话。

## 2. 现状证据

- `memory/ledger.py:14-31` 只有 `build_mem0_middleware()` 工厂，**没有任何调用点**；README 的 API 映射也标注长期记忆「仅工厂 + 单测」。
- `runtime/assembly.py:169-176` 只装配 `ProfileNoteMiddleware`、密钥中间件与 `CompressionSink`，没有记忆中间件。
- `runtime/events.py:17-27` 的 `SPAN_EVENT_TYPES` 只有模型/工具/确认/超限；`observability/recorder/recorder.py:142-154` 的分派表没有记忆事件。
- `observability/repository/db.py:58` 的 `spans.type` CHECK 只允许 `model / tool / permission`；`models.py:89` 的注释同样只有三种。
- 前端 `web/src/types/run.ts:117-125` 的 `ProcessKind` 没有 memory；`run.ts:308-312` 的 `TraceLanes` 只有 model/tool 两条；`web/src/workspace/TraceLanes.tsx:15-18` 的 `ROWS` 硬编码两行。
- 已有可复用的通道范式：`runtime/compression.py:79-98` 的 `CompressionSink`（deque 写、流读取）与 `runtime/scope_runtime.py:227-230` 的 drain；`events.py:154-170` 已示范如何为特殊事件保留结构化数组（`REQUIRE_USER_CONFIRM.tool_calls`）。
- 注入位置已有先例：`runtime/profile_note.py:117-122` 把档位说明追加在 messages 末尾，只影响本次请求、不进历史，且不破坏前缀缓存。

## 3. 目标与非目标

### 3.1 目标

1. 一轮对话内自动完成「检索 → 筛选 → 注入」，回合结束后自动写入。
2. 记忆层与具体后端解耦：Mem0 / Agentic / off 可配置，缺包或超时自动降级。
3. 轨迹页签能回答四个问题：这轮读没读记忆、读了几条、注入了多少、写没写成功；失败要能看到原因。
4. 隔离与隐私：默认按会话隔离；写入前脱敏；轨迹只保留预览；用户可删除。
5. 可评测：命中率、注入成本、延迟、失败率、跨会话污染可量化。

### 3.2 非目标

- 不实现自己的向量数据库、embedding 服务或摘要模型。
- 不做跨用户共享记忆（等鉴权落地后再开 `scope=user`）。
- 不在本期做程序性记忆（技能自动沉淀）与前端完整记忆管理台。
- 不把完整记忆原文塞进轨迹库。

## 4. 一个合格记忆层要做什么

| 问题 | 本期答案 |
|---|---|
| 记什么 | 用户偏好、环境事实、已确认结论、任务上下文（由内置中间件自动抽取） |
| 何时读 | 每轮模型推理前，用当前用户输入做 query |
| 何时写 | 回复结束后，按后端能力自动写入 |
| 怎么选 | `top_k` + 最低相关度 + 去重 + 字符预算 |
| 怎么注入 | 追加在 messages 末尾的 system note，带 `<memory>` 边界与“仅作参考”声明 |
| 冲突怎么办 | 交给后端（mem0 的 update/delete）；本期不自研冲突合并 |
| 怎么忘 | TTL + 每作用域条数上限 + 删除 API |
| 怎么隔离 | `session` 默认；`user/global` 只在鉴权或显式配置后启用 |
| 隐私 | 写入前脱敏；轨迹只存预览与 ID；不存密钥 |
| 失败怎么办 | 超时/异常 fail-open，对话照常；失败本身进轨迹 |

分层职责：

| 层 | 内容 | 现有实现 | 本期动作 |
|---|---|---|---|
| L0 工作记忆 | 当前上下文、压缩摘要 | `context_config` + `runtime/compression.py` | 不改 |
| L1 会话情景 | 消息历史、会话快照 | `session_states` + 会话注册表 | 不改 |
| L2 长期语义 | 跨轮/跨会话事实与偏好 | `memory/ledger.py` 仅工厂 | **本期接线** |
| L3 程序性 | 技能/操作手册 | `skills/` 未被运行时加载 | 后续 spec |

## 5. 后端与装配设计

### 5.1 后端选择（优先内置）

`config/memory.yaml` 的 `backend` 三态：

- `mem0`：已有 `Mem0Middleware`，需要 `mem0` client 或 `mem0_config`；
- `agentic`：用 AgentScope 的 `AgenticMemoryMiddleware`（本地存储，无外部服务）；
- `auto`：有 mem0 配置用 mem0，否则 agentic，都不可用则 `off`。

实现前必须先 introspect 真实签名与钩子名（AGENTS.md §3.4），不凭记忆写 API。若内置中间件没有可用的读/写钩子，则用**薄观察者**包一层转发，不复制记忆逻辑。

依赖隔离：`mem0` 只在 `devops_agent/memory/` 内 import，新增 optional extra `memory = ["mem0ai>=..."]`；未安装时 `backend=auto` 退化为 agentic，`/health` 披露原因。

### 5.2 运行时装配

`RuntimeParts` 增加三个字段：`memory_mw`、`memory_sink`、`memory_status`；`assembly._build_middlewares()` 按配置决定是否构造。中间件顺序：

```
ProfileNote / Secret / Compression（现有）
  → MemoryObserver（读）
  → Agent 推理
  → MemoryObserver（写）
```

`scope_runtime.stream()` 在每次框架事件之后 drain 压缩帧的位置（`scope_runtime.py:227-230`），同样 drain 记忆帧。Echo/测试替身不构造记忆中间件，因此不会发假事件。

### 5.3 身份与隔离

- `scope=session`（默认）：`user_id = "session:" + session_id`，跨会话不召回。
- `scope=user`：仅当显式配置 `DEVOPS_AGENT_MEMORY_USER_ID` 时启用；**没有鉴权前不开放给前端**。
- `scope=global`：本期不实现，配置为它时启动告警并降级为 session。

### 5.4 检索

1. query 取本轮经输入管线处理后的用户输入（`PreparedInput.text`），不取模型自己编的检索词。
2. `top_k` 默认 5，低于 `min_score` 丢弃，按 id 去重。
3. 总字符超过 `max_injected_chars` 时按相关度截断。
4. 注入为末尾 system note，结构：边界标记 + 每条 `[id] 内容` + “以下为历史记忆，仅作参考，如与当前任务冲突以当前任务为准”。
5. 无命中不发注入，但仍发 `MEMORY_RETRIEVE_END(state=miss)`。

### 5.5 写入

- 回复结束后触发，写入内容来自本轮对话，交给内置中间件完成抽取/去重/更新。
- 写入前过一遍现有脱敏（`input/pipeline/redact.py`），密钥类内容不进入记忆。
- `max_entries` / `ttl_days` 由配置约束；后端不支持时由适配层定期清理。

### 5.6 失败与降级

- 检索加 `asyncio.wait_for(retrieve_timeout_ms)`；异常、超时、后端缺失都只产生一条失败/跳过事件。
- `fail_open: true` 为默认；设 false 时也只跳过记忆，不让整轮失败。
- 一条对话最多一次检索、一次写入，避免中间件重入导致重复。

### 5.7 配置

```yaml
memory:
  enabled: true
  backend: auto            # auto | mem0 | agentic | off
  mode: both               # read | write | both | off
  scope: session           # session | user
  top_k: 5
  min_score: 0.35
  retrieve_timeout_ms: 1500
  max_injected_chars: 2000
  max_entries: 200
  ttl_days: 90
  fail_open: true
  trace:
    query_preview_chars: 120
    hit_preview_chars: 80
```

环境变量覆盖：`DEVOPS_AGENT_MEMORY_ENABLED / _BACKEND / _SCOPE / _USER_ID`。与其它配置一样，env 优先、yaml 次之、内置默认兜底。

## 6. 轨迹可观测（硬要求）

### 6.1 事件契约

| 事件 | 时机 | 关键字段 |
|---|---|---|
| `MEMORY_RETRIEVE_START` | 检索前 | `scope`、`backend`、`top_k`、`query_preview` |
| `MEMORY_RETRIEVE_END` | 检索后 | `state(hit/miss/error/skipped)`、`hits`、`ids`、`scores`、`injected_chars`、`latency_ms`、`reason` |
| `MEMORY_WRITE_START` | 写入前 | `backend`、`scope`、`candidates` |
| `MEMORY_WRITE_END` | 写入后 | `state(ok/error/skipped)`、`stored`、`deduped`、`latency_ms`、`reason` |

事件负载用结构化 `memory` 对象承载，沿用 `events.py` 对 `REQUIRE_USER_CONFIRM.tool_calls` 的保留方式（`events.py:154-170`）；只放预览，不放全文。

### 6.2 事件通道

复用压缩层的成熟做法，不改 AgentScope 源码：

```
MemoryObserver 钩子 → MemorySink(deque)
    → AgentScopeRuntime.stream() drain → SSE 帧 → recorder + 前端
```

`MemorySink` 与 `CompressionSink` 同构（`compression.py:79-98`），`drain` 在 `scope_runtime.py:227-230` 同一位置补发。

### 6.3 落库

- `spans` 新增 `type='memory'`，`name='memory.retrieve' | 'memory.write'`。
- `input` 存 query 预览；`output` 存命中摘要 JSON（id/score/kind/preview）；`duration_ms` 真实耗时；`status='ok'|'error'`；`error` 存原因。
- 现有统计按 type 过滤（`api.py:331-355` 只取 tool/permission/model），memory 不会污染工具失败率；`/cost` 与健康分也不需要新口径。
- **迁移**：`spans.type` 的 CHECK 在 SQLite 改不了，需一次性重建表（新表允许 `memory`，复制旧行、重建索引、改回原名），放进 `observability/repository/db.py` 的迁移段，幂等且带老库测试。备选方案是复用 `type='tool'` 加 `name='memory.*'`，但会污染工具泳道与失败率，不采用。

### 6.4 前端

- `types/run.ts`：`ProcessKind` 加 `memory`；`TraceStep.kind` 加 `memory`；`TraceStep.layer` 与 `TraceSpan.lane` 加 `memory`；`TraceLanes` 加 `memory: TraceSpan[]`。
- 新增 `web/src/api/stream/handlers/memory.ts`，在 `handlers/index.ts` 注册；步骤行文案：`记忆检索 · 3 条` / `记忆写入 · 2 条`；失败显示红色并保留原因。
- `TraceLanes.tsx` 增加第三条泳道「记忆」，与模型/工具同色系但可区分；图例必须同时有文字，不能只靠颜色。
- `StepRow` 与 `TraceInspector` 复用现有 `args/result` 槽位：`args` 展示 query，`result` 展示命中/写入明细；不新增一套渲染管线。
- 历史回放走 `/api/runs/{id}/spans`，`SpanOut.type: str` 已透传，前端按 `type` 映射即可。
- 按 AGENTS.md §3.8，改 `web/**` 前必须跑 modern-web-guidance 的 search → retrieve → 校验，并在交付说明里披露。

轨迹示意：

```
记忆   ██ 12ms    检索 3 条 / 注入 480 字（top_k=5, hit）
模型   ████████    第 1 次推理
工具   ██           Bash · 802ms
记忆   █ 3ms       写入 2 条（新增 1 / 去重 1）
```

## 7. 隐私与隔离

- 轨迹与日志只存 query 预览和命中预览（默认 120/80 字），不存完整记忆正文。
- 写入前必须脱敏；`secrets/` 的占位符不进入记忆。
- 删除：`DELETE /api/memory/{id}` 同时删除后端记录；轨迹保留“已删除”事件而不是原文。
- 跨会话污染是红线：测试必须覆盖“会话 A 写入、会话 B 检索不到”。
- 轨迹库是本地 SQLite，保留策略跟随 `workspace/`；文档明确“删库即清空记忆轨迹”。

## 8. 接口（最小面）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/memory?scope=session&session_id=...` | 列出当前作用域记忆（分页、只返回预览） |
| DELETE | `/api/memory/{memory_id}` | 删除单条 |
| GET | `/api/memory/status` | 后端、作用域、条数与降级原因 |

`/health` 增加 `memory: {enabled, backend, scope, reason}`，缺包或后端不可用时与工具 preflight 一样明确披露。

## 9. 兼容与迁移

1. 老观测库：启动时执行 spans 表重建迁移；迁移失败只关闭记忆，不影响已有运行。
2. 老前端：未知 `type` 的 span 目前按通用步骤渲染，新增 `memory` 不需要同步升级后端才能看历史。
3. Echo/测试：不构造记忆中间件，不产生记忆事件；现有 633 个测试不应因默认配置改变。
4. 首版默认 `backend=auto`、`scope=session`；若部署方未配置 mem0，退化为 agentic；两者都不可用则 `off`，轨迹里不出现记忆行。

## 10. 验收

- A1 检索命中：预置一条记忆 + 相关输入，事件 `state=hit`，注入内容出现在上下文，轨迹有记忆行。
- A2 无命中：`state=miss`，不注入，轨迹仍有一行。
- A3 失败降级：后端抛错/超时，对话正常完成，轨迹行 `state=error` 且有原因。
- A4 写入：回复结束后 `MEMORY_WRITE_END(state=ok)`，重复内容被去重。
- A5 隔离：会话 A 写入后，会话 B 检索不到。
- A6 隐私：轨迹 output 不含完整正文与密钥；删除后列表不可见。
- A7 迁移：用旧 schema 的库启动，迁移后新 span 可写入，旧 run 的 spans 仍可读。
- A8 前端：`MEMORY_*` 事件折叠为步骤行，泳道显示记忆占用；单测覆盖成功/失败/空态。
- A9 门禁：后端覆盖率 ≥80%、单文件 ≤500 行、ruff/mypy/vulture 通过；前端 65% 覆盖率与 lint/typecheck 通过。

## 11. 实施顺序（4 个 PR）

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P1 | 配置 + 后端工厂（auto/off）+ 检索注入 + MemorySink/事件 + spans 迁移与 recorder | 无 |
| P2 | 前端类型、事件 handler、记忆步骤行与第三条泳道、历史回放 | P1 |
| P3 | 写入链路 + 去重/上限/TTL + 列表与删除 API + `/health` 披露 | P1 |
| P4 | 评测指标（命中率/注入成本/延迟/污染）+ 文档与 README 更新 | P2/P3 |

每个 PR 只做一层，P1 合入后即使前端未跟进，事件与落库也已经可用。

## 12. 风险与决策点

1. **内置中间件 API 不确定**：`Mem0Middleware` 的读/写钩子名与返回值未在本仓库验证过，实施前必须 introspect；这是 P1 的第一个任务。
2. **无鉴权**：默认 `scope=session` 是对现状的诚实映射；开放 `user` 需要与鉴权 spec 联动。
3. **记忆污染**：低质量写入会长期影响回答；首版用 `min_score` + 去重 + 可删除兜底，不做自动学习。
4. **延迟成本**：检索增加一次外部调用；`retrieve_timeout_ms` 默认 1.5s，失败即跳过。
5. **轨迹隐私**：预览仍可能含业务信息；默认截断 + 可删除，必要时提供 `trace.include_content=false` 的硬开关（默认已关闭）。
6. **迁移风险**：spans 重建期间若进程崩溃，用事务包裹并在启动时校验表结构；失败则禁用记忆而非阻塞服务。

## 13. 度量

- `memory_retrieve_hit_rate`、平均命中条数、`injected_chars`；
- `memory_retrieve_ms` p50/p95、失败率、超时率；
- 写入条数、去重率、删除率；
- 跨会话污染 = 0（红线）；
- 记忆带来的 token 增量与首字延迟增量。

## 14. 未决

- AgentScope `AgenticMemoryMiddleware` 的存储位置与清理接口；
- mem0 是走云端还是本地 OSS（影响部署与隐私）；
- 是否提供“记住这条”的前端显式操作（本期只做自动写入）；
- 程序性记忆（L3）是否复用 `skills/` 目录。
