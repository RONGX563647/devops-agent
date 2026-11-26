# SPEC-0013：Agent 三层记忆与记忆压缩（内置中间件 + 轨迹可观测）

- 状态：方案 v2（待评审；v1 只设计长期记忆，短期与压缩缺失，本版重写）
- 日期：2026-11-27
- 基线：`main b90f909`
- 关系：承接 ADR-0001（可观测性前端）与 SPEC-0008（评测可信度）；压缩事件沿用 `runtime/compression.py`，前端沿用轨迹页签的步骤模型
- 范围：`src/devops_agent/memory/**`、`runtime/{assembly,scope_runtime,compression,runtime}.py`、`observability/{recorder,repository,models,api}.py`、`gateway/routes/memory.py`、`config/memory.yaml`、`web/src/{types,api/stream/handlers,workspace}/**`、`tests/**`
- 前置阅读：README「agentscope 真实 API 映射」、AGENTS.md §3.4（能力用内置）、§3.8（前端改动先读 modern-web-guidance）
- 证据约定：现状结论带 `路径:行号`；设计建议与既存事实分开写

## 1. 结论

v1 的错误是把“已有”当成“已设计”。本版把记忆定义为一个**三层 + 压缩**的体系：

1. **短期记忆（工作记忆）**：当前任务内的上下文、最近轮次、工具结果、临时计划。按 token 预算裁剪，不跨轮持久化为原文。
2. **中期记忆（会话情景）**：跨轮但限本会话的结构化记忆卡，由短期压缩而来，保存目标、结论、产物、未决项与错误签名，带版本和 TTL。
3. **长期记忆（语义 + 程序性）**：跨会话的稳定事实、偏好、可复用步骤；用 AgentScope `Mem0Middleware` / `AgenticMemoryMiddleware`，不手搓向量库。

压缩不是长期记忆的附属，而是三层各自的能力：短期做**确定性裁剪**，中期做**内置 `compress_context()` 摘要**，长期做**去重/合并/衰减**。三层共用一个预算调度器和一套 `MEMORY_*` 事件；轨迹页签能看见每一次检索、裁剪、压缩、写入的前后 token 与结果。

## 2. 现状证据

- **短期已有执行态但无策略**：`AgentState.context` + `ContextConfig` 由框架维护；阈值来自 `config/model.yaml` 的 `trigger_ratio`，`runtime/compression.py:16-18` 只做观测，没有定义“保留哪些轮次、工具结果留多少、先丢什么”。
- **中期有摘要但无独立设计**：框架压缩写入 `agent.state.summary`；会话快照经 `sessions.py:121-123`、`sessions.py:270-274` 存进 `session_states.state_json`，重建时 `assembly.py:279` 用 `AgentState.model_validate_json` 恢复。也就是说摘要能跨轮存活，但没有版本、结构、TTL、查询接口，也没有“从摘要回到上下文”的注入策略。
- **长期只有工厂**：`memory/ledger.py:14-31` 的 `build_mem0_middleware()` 无调用点；README 映射标注「仅工厂 + 单测」。
- **观测只覆盖压缩**：`compression.py` 发 `CONTEXT_BUDGET / CONTEXT_COMPRESSION_START / END`，前端 `ContextRing.tsx` 与 `CompressionRecord` 消费；但 `events.py:17-27`、`recorder.py:142-154`、`db.py:58` 的 span 类型都没有记忆；轨迹页签看不到任何记忆动作。
- **前端没有记忆维度**：`types/run.ts:117-125` 的 `ProcessKind`、`run.ts:308-312` 的 `TraceLanes`、`TraceLanes.tsx:15-18` 的 `ROWS` 都只有模型/工具。
- **可复用范式**：`CompressionSink`（`compression.py:79-98`）的“钩子写队列 → 流 drain → SSE”就是记忆事件通道的模板；`profile_note.py:117-122` 的“末尾 system note”是记忆注入的位置先例。

## 3. 目标与非目标

### 3.1 目标

1. 三层记忆各司其职，层间有明确的晋升（短期→中期、中期→长期）与召回（长期→短期）路径。
2. 压缩可解释：每次压缩记录触发原因、所属层、前后 token、保留/丢弃了什么。
3. 优先内置能力：摘要用 AgentScope 压缩，长期用内置记忆中间件；不手搓摘要模型与向量检索。
4. 轨迹可观测：检索、裁剪、压缩、写入四类动作都有事件与 span，前端有独立泳道。
5. 默认安全：会话隔离、写入前脱敏、轨迹只留预览、用户可删除；失败 fail-open。

### 3.2 非目标

- 不自建向量库、embedding 服务或摘要模型。
- 不做跨用户共享记忆（等鉴权落地）。
- 不做自动技能生成（程序性记忆本期只预留写入接口）。
- 不做前端完整记忆管理台（只做最小列表/删除/状态）。

## 4. 三层记忆模型

| 层 | 作用域 | 内容 | 存储 | 生命周期 | 读取方式 | 写入方式 |
|---|---|---|---|---|---|---|
| 短期 | 单轮/当前任务 | 当前输入、最近 N 轮、工具结果、临时计划 | `AgentState.context`（框架内存） | 轮内；快照随会话 | 直接进上下文 | 每轮追加 |
| 中期 | 单会话跨轮 | 目标、结论、产物、未决项、错误签名 | 新增 `session_memories` 表 + `AgentState.summary` | 会话 TTL + 版本上限 | 轮首注入记忆卡 | 压缩触发时生成新版本 |
| 长期 | 跨会话 | 稳定事实、偏好、可复用步骤 | Mem0 / Agentic 后端 | TTL + 条数上限 + 衰减 | `top_k` 语义检索 | 回合/会话结束自动写入 |

程序性记忆不单独算第四层，它是长期记忆的一个 `kind`（`fact / preference / procedure`）。

### 4.1 短期记忆（工作记忆）

**职责**：保证当前任务“想得起来”，同时不把窗口塞爆。

- 内容分层：当前用户输入（永不裁剪）、最近 `max_turns` 轮对话、工具结果（按工具类型限长）、临时计划/待办（scratchpad）、系统注释（档位/联网/记忆）。
- 裁剪顺序：**旧工具结果 → 重复读取结果 → 较早的助手推理 → 较早的轮次**；当前用户输入、未完成的确认请求、安全约束永不被裁。
- 工具结果策略：超过 `tool_output_chars` 时保留头 `head_ratio`、尾 `1-head_ratio`，中间标记“已截断 N 字”；同一文件/同一命令的重复读取只留最后一次（`dedup_reads`）。
- 计划态：scratchpad 作为 `AgentState` 的一个字段或一条 system note，任务结束时清空；不写入长期记忆。
- 持久化：不把裁剪后的原文写入新表；会话快照仍由 `AgentState` 整体序列化到 `session_states`（现状路径），重建时恢复。

### 4.2 中期记忆（会话情景）

**职责**：跨轮保留“这个会话已经决定了什么”，而不是把历史消息再塞回去。

- 记忆卡不是自由文本，而是“内置摘要 + 事件派生字段”：
  - `text`：AgentScope `compress_context()` 产出的摘要，权威内容；
  - `artifacts`：本轮工具调用产生的文件路径/行号（从事件派生，不解析摘要）；
  - `pending`：未完成的确认/待办（从 `REQUIRE_USER_CONFIRM` 派生）；
  - `errors`：失败签名（从工具/模型错误事件派生）；
  - `tokens`：压缩前后与预算；
  - `source_run_ids`：来源运行，便于回溯。
- 版本：每次压缩成功写一个新版本，`is_active` 只保留一条；最多 `max_versions`，超出删最旧。
- 读取：轮首把 active 卡片作为末尾 system note 注入；如果当前上下文里已经有同一版本的摘要（例如刚压缩完），不重复注入。
- 晋升：短期压缩触发即生成中期版本；会话空闲/结束时把“稳定事实”交给长期层。
- TTL：`ttl_days` 到期删除；会话删除时级联删除。

### 4.3 长期记忆（语义 + 程序性）

**职责**：跨会话记住“这个人/这台机器的稳定事实”。

- 后端：`mem0` / `agentic` / `auto`；`mem0` 只在 `memory/` 内 import，缺包时退化。
- 写入时机：回合结束（`write_on: turn`）或会话结束（`write_on: session_end`，默认）；只写稳定、可复用、非敏感内容。
- 去重/冲突：交给后端（mem0 的 update/delete）；不自研合并。写入前过 `input/pipeline/redact.py` 的脱敏。
- 读取：`top_k` + `min_score` + 去重 + 字符预算；默认 `scope=session`（当前无鉴权），鉴权后再开 `user`。
- 遗忘：`ttl_days`、`max_entries`、删除 API、低置信度衰减； consolidation 在会话结束时触发。

### 4.4 层间流动

```
短期 --(确定性裁剪)--> 短期
短期 --(阈值/轮数触发, 内置摘要)--> 中期新版本
中期 --(会话结束, 稳定事实抽取)--> 长期
长期 --(轮首 top_k 检索)--> 短期注入
```

原则：**原文只向下压缩，不向上还原**；晋升的是结论，不是消息。

## 5. 记忆压缩设计

### 5.1 统一预算调度

记忆预算不是整个窗口，而是上下文预算的一部分：

```
memory_budget = context_size × trigger_ratio
short_term_ratio = 0.50
episodic_ratio   = 0.30
long_term_ratio  = 0.10
headroom_ratio   = 0.10   # 留给系统提示、工具 schema、输出
```

比例可配置；某层未启用时，其份额自动让给短期。超预算时按固定顺序收缩：

1. 丢弃长期检索结果（最便宜，重新检索即可）；
2. 截断中期摘要（保留目标/未决项，删细节）；
3. 裁剪旧工具结果；
4. 裁剪最早轮次；
5. 仍超则触发一次中期压缩；压缩失败也只记录事件，不阻断对话。

### 5.2 触发矩阵

| 层 | 触发条件 | 方法 | 产出 |
|---|---|---|---|
| 短期 | 工具结果 > `tool_output_chars`；重复读取；轮数 > `max_turns`；token > 短期份额 | 确定性裁剪/去重，无 LLM | 回收 token，保留头尾 |
| 中期 | `compressed=True`（框架压缩真正发生）；或每 `trigger_turns` 轮；或短期压缩后仍超预算 | AgentScope `compress_context()` | 新版本记忆卡 |
| 长期 | 会话结束；每 `consolidate_every_runs` 次运行 | 后端去重/合并/衰减 | 条目数收敛 |

### 5.3 三层压缩管线

- **短期 = 确定性压缩**：不调模型，保证低延迟。工具结果头尾保留、重复结果去重、旧轮次淘汰。每次裁剪发 `MEMORY_TRIM`，记录丢弃类别与回收字符。
- **中期 = 内置摘要**：复用框架 `compress_context()`（已有钩子与阈值，见 `compression.py:100-150`），不自己写摘要提示词。压缩后把摘要落成新版本；`CONTEXT_COMPRESSION_*` 继续供 `ContextRing` 使用，同时补发 `MEMORY_COMPRESS_*` 供轨迹页签使用，避免重复压缩。
- **长期 = 合并而非摘要**：由后端做去重/更新/删除；可观测记录合并前后的条目数与耗时，不生成新的自然语言摘要。

### 5.4 保留与丢弃规则

| 保留（跨压缩） | 丢弃（可安全消失） |
|---|---|
| 当前用户目标与显式约束 | 问候、寒暄、重复确认 |
| 已确认的决策与原因 | 中间推理过程 |
| 文件路径、行号、命令 | 超长工具原始输出（保留头尾） |
| 错误签名与修复结果 | 重复读取的同一文件 |
| 未完成的确认/待办 | 过期的时间性信息（可进长期层） |

### 5.5 中期记忆卡 schema

```json
{
  "version": 3,
  "session_id": "s1",
  "created_at": "2026-11-27T10:00:00+08:00",
  "trigger": "context_threshold",
  "source_run_ids": [118, 119],
  "tokens": {"before": 103200, "after": 41200, "budget": 102400},
  "text": "<AgentScope compress_context 产出的摘要>",
  "artifacts": ["src/devops_agent/runtime/assembly.py:169"],
  "pending": [],
  "errors": ["pytest: exit 1 (test_xxx)"]
}
```

`text` 是权威摘要；其余字段只从事件派生，不解析摘要文本，避免脆弱的字符串处理。

### 5.6 原子性与失败

- 新版本先写库（`is_active=0`），成功后再把旧版本置 `0`、新版本置 `1`，单事务完成；失败保留旧版本。
- 压缩超时/异常：发 `MEMORY_COMPRESS_END(state=error)`，沿用旧摘要，本轮继续。
- 短期裁剪永远不删当前用户输入与未决确认；压缩失败也不丢当前轮。

### 5.7 与现有 `compression.py` 的关系

不重写压缩逻辑，只加两层元数据：

1. `CompressionMiddleware` 仍是唯一压缩入口；
2. 在其 `START/END` 处补发 `MEMORY_COMPRESS_*`（带 `layer=episodic`、`trigger`、`reclaimed_tokens`）；
3. 压缩成功后由记忆层落新版本；`CONTEXT_*` 事件保持不变，前端 ContextRing 不受影响。

## 6. 每轮读写时序

1. **轮首**：加载 active 中期卡 → 长期 `top_k` 检索 → 预算调度 → 作为末尾 system note 注入（先中期、后长期）。
2. **执行中**：短期追加消息与工具结果；超过单项上限立即裁剪；框架到达阈值时触发中期压缩。
3. **轮末**：按 `write_on` 写入长期候选；若触发中期版本更新则落库；会话快照照旧保存。
4. **会话结束/空闲**：最终压缩一次中期、长期 consolidation、清理过期版本。

## 7. 轨迹可观测（硬要求）

### 7.1 统一事件契约

| 事件 | 层 | 关键字段 |
|---|---|---|
| `MEMORY_RETRIEVE_START/END` | 中/长 | `layer`、`scope`、`backend`、`top_k`、`hits`、`ids`、`scores`、`injected_chars`、`latency_ms`、`state(hit/miss/error/skipped)`、`reason` |
| `MEMORY_TRIM` | 短 | `dropped_tool_results`、`dropped_turns`、`deduped_reads`、`reclaimed_chars`、`reason` |
| `MEMORY_COMPRESS_START/END` | 短→中 | `trigger`、`tokens_before`、`tokens_after`、`budget`、`version`、`latency_ms`、`state`、`reason` |
| `MEMORY_WRITE_START/END` | 中/长 | `layer`、`stored`、`deduped`、`consolidated`、`latency_ms`、`state`、`reason` |

所有事件用结构化 `memory` 对象承载；`events.py` 对特殊数组的保留方式见 `REQUIRE_USER_CONFIRM.tool_calls`（`events.py:154-170`）。只放预览，不放全文。

### 7.2 事件通道

```
MemoryObserver 钩子 → MemorySink(deque)
    → AgentScopeRuntime.stream() drain → SSE 帧 → recorder + 前端
```

`MemorySink` 与 `CompressionSink` 同构（`compression.py:79-98`），在 `scope_runtime.py:227-230` 同一位置补发。Echo/测试替身不构造记忆中间件，不产生记忆事件。

### 7.3 落库

- `spans` 新增 `type='memory'`；`name` 取 `memory.retrieve / memory.trim / memory.compress / memory.write`。
- `input` 存查询或触发原因；`output` 存结果摘要 JSON（层、命中 id/score、前后 token、丢弃类别）；`duration_ms`；`status`；`error`。
- 现有统计只按 `tool/permission/model` 取数（`api.py:331-355`），memory span 不会污染工具失败率。
- **迁移**：`spans.type` 的 CHECK 在 SQLite 改不了，需一次性重建表（新表允许 `memory`，复制旧行、重建索引、改回原名），幂等并带老库测试。不采用伪装成 `tool` 的方案，会污染泳道与失败率。

### 7.4 前端

- `types/run.ts`：`ProcessKind` 加 `memory`；`TraceStep.kind/layer` 加 `memory`；`TraceLanes` 加 `memory: TraceSpan[]`。
- 新增 `api/stream/handlers/memory.ts` 并在 `handlers/index.ts` 注册；步骤行文案带层徽标：
  - `记忆检索 · 长期 · 3 条`
  - `记忆裁剪 · 短期 · 回收 12k 字`
  - `记忆压缩 · 短期→中期 · 103k→41k`
  - `记忆写入 · 长期 · 新增 2 / 去重 1`
- `TraceLanes.tsx` 增加第三条泳道「记忆」；图例同时有文字与图标，不能只靠颜色。
- `StepRow` / `TraceInspector` 复用现有 `args/result` 槽位：压缩行展示前后 token 与保留项，检索行展示命中明细，写入行展示新增/去重。
- 历史回放通过 `/api/runs/{id}/spans`，`SpanOut.type` 已透传，按 `type='memory'` 映射即可。
- 按 AGENTS.md §3.8，改 `web/**` 前必须执行 modern-web-guidance 的 search → retrieve → 校验，并披露。

轨迹示意：

```
记忆   ██ 8ms     压缩 短期→中期 103k→41k（保留 3 决策 / 2 产物）
模型   ████████   第 1 次推理
工具   ██          Bash · 802ms
记忆   █ 3ms      检索 长期 命中 2 条 → 注入 480 字
记忆   █ 1ms      裁剪 短期 回收 12k 字（旧工具结果 3 条）
```

## 8. 隔离与隐私

- 默认 `scope=session`，`user_id = "session:" + session_id`；没有鉴权前不开放跨用户。
- 写入前脱敏；密钥占位符不进入任何层。
- 轨迹只存预览（query 120 字、命中 80 字、摘要 160 字），不存全文。
- 删除 API 同时删中期版本与长期条目；轨迹保留“已删除”动作而非原文。
- 跨会话污染是红线：会话 A 写入后，会话 B 检索必须为空。

## 9. 配置

```yaml
memory:
  enabled: true
  short_term:
    max_turns: 12
    tool_output_chars: 1600
    tool_output_head_ratio: 0.7
    dedup_reads: true
  episodic:
    enabled: true
    trigger_turns: 6
    max_chars: 3000
    max_versions: 20
    ttl_days: 30
  long_term:
    enabled: true
    backend: auto          # auto | mem0 | agentic | off
    scope: session         # session | user
    top_k: 5
    min_score: 0.35
    max_entries: 200
    ttl_days: 90
    write_on: session_end  # turn | session_end
  budget:
    short_term_ratio: 0.50
    episodic_ratio: 0.30
    long_term_ratio: 0.10
    headroom_ratio: 0.10
  compress:
    fail_open: true
  trace:
    query_preview_chars: 120
    hit_preview_chars: 80
    summary_preview_chars: 160
```

环境变量：`DEVOPS_AGENT_MEMORY_ENABLED / _BACKEND / _SCOPE / _USER_ID`；env 优先、yaml 次之、内置默认兜底。

## 10. 接口（最小面）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/memory/session/{session_id}` | 当前中期卡片 + 版本列表（预览） |
| GET | `/api/memory/long-term?scope=session&session_id=...` | 长期记忆列表（分页、预览） |
| DELETE | `/api/memory/{layer}/{memory_id}` | 删除中期版本或长期条目 |
| GET | `/api/memory/status` | 三层开关、后端、条数、降级原因 |

`/health` 增加 `memory: {short_term, episodic, long_term, backend, reason}`。

## 11. 兼容与迁移

1. 老观测库：启动时执行 spans 表重建；失败只关闭记忆观测，不影响运行。
2. 中期版本表新增，不影响 `session_states`；旧会话没有卡片时按“无中期记忆”处理。
3. Echo/测试：不构造记忆中间件，现有 633 个测试的默认行为不变。
4. `CONTEXT_*` 事件与 ContextRing 保持不变，新事件只增量添加。
5. 首版默认开启短期裁剪与中期压缩，长期默认 `auto`（无后端则 off）。

## 12. 验收

- A1 短期裁剪：工具结果超限后只留头尾，当前输入与未决确认不丢；`MEMORY_TRIM` 有丢弃明细。
- A2 中期压缩：达到阈值后生成新版本，`text` 来自内置摘要，`tokens_before/after` 正确；旧版本保留。
- A3 中期召回：下一轮注入 active 卡片，不重复注入；重建会话后仍能读到。
- A4 长期读写：命中时注入并记录；未命中不注入；写入去重；跨会话隔离。
- A5 压缩失败：异常/超时保留旧卡片，对话继续，轨迹 `state=error`。
- A6 预算顺序：超预算先丢长期、再截中期、再裁旧工具结果，最后才动旧轮次。
- A7 轨迹：四类动作（检索/裁剪/压缩/写入）都能在轨迹页签看到；历史回放同样可见。
- A8 隐私：轨迹不含全文与密钥；删除后列表与检索均不可见。
- A9 迁移：旧 schema 库可升级，旧 run 的 spans 仍可读。
- A10 门禁：覆盖率 ≥80%、单文件 ≤500 行、ruff/mypy/vulture/前端 lint/typecheck 通过。

## 13. 实施顺序（4 个 PR）

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P1 | 短期策略（窗口/工具结果/去重）+ `MEMORY_TRIM` + `MemorySink` + spans 迁移 + recorder | 无 |
| P2 | 中期记忆卡与版本表 + 接 `compress_context()` + `MEMORY_COMPRESS_*` + 注入 | P1 |
| P3 | 长期后端工厂（mem0/agentic/auto）+ 检索/写入/consolidation + 隔离 | P2 |
| P4 | 前端轨迹泳道与步骤行 + 管理 API/最小面板 + 指标与文档 | P1–P3 |

P1 合入后轨迹已能看到短期裁剪；P2 能看到压缩前后；P3 才引入外部后端。

## 14. 风险与决策点

1. **内置压缩摘要格式未知**：`compress_context()` 的摘要结构需先 introspect；本期只把 `text` 当权威，不做文本解析。
2. **中期卡片与框架 summary 双份**：必须明确“框架 summary 是工作副本，DB 卡片是版本快照”，压缩后以新版本覆盖 active，避免双写漂移。
3. **无鉴权**：默认 session 隔离；开放 user 需与鉴权 spec 联动。
4. **压缩延迟**：中期压缩是 LLM 调用；超时保留旧卡，事件可见。
5. **记忆污染**：长期只写稳定内容，`min_score` + 去重 + 可删除兜底。
6. **迁移风险**：spans 重建用事务包裹，启动校验结构；失败禁用记忆观测而非阻塞服务。

## 15. 度量

- 短期：回收字符/轮次、裁剪触发率、当前输入丢失数（必须为 0）。
- 中期：压缩次数、压缩比、压缩延迟、版本数、重建后可用率。
- 长期：检索命中率、注入字符、写入/去重/删除数、跨会话污染（必须为 0）。
- 全局：记忆带来的 token 增量、首字延迟增量、失败率。

## 16. 未决

- `AgenticMemoryMiddleware` 的存储位置、清理接口与并发行为。
- mem0 走云端还是本地 OSS（影响隐私与部署）。
- 中期卡是否需要“稳定事实抽取”再进长期，还是直接交给 mem0 自动抽取。
- 程序性记忆（长期层 `kind=procedure`）何时接入 `skills/`。
