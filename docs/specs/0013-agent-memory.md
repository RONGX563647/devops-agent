# SPEC-0013：Agent 三层记忆、上下文装配与记忆压缩

- 状态：方案 v5（待评审）
- 日期：2026-11-27
- 基线：`main 76110b9`
- 关系：承接 ADR-0001（可观测性前端）与 SPEC-0008（评测可信度）；压缩事件沿用 `runtime/compression.py`，前端沿用轨迹页签的步骤模型
- 范围：`src/devops_agent/memory/**`、`runtime/{assembly,scope_runtime,compression,runtime}.py`、`observability/{recorder,repository,models,api}.py`、`tools/context_api.py`、`gateway/routes/memory.py`、`config/memory.yaml`、`web/src/{types,api/stream/handlers,workspace}/**`、`tests/**`
- 前置阅读：README「agentscope 真实 API 映射」、AGENTS.md §3.4（能力用内置）、§3.8（前端改动先读 modern-web-guidance）
- 变更说明（v4）：上一版只写了「记忆」没写「上下文」，导致预算、注入位置、快照与恢复都悬空。本版新增第 5 章完整定义上下文；并把记忆预算从「直接取 `context_size × trigger_ratio`」修正为「窗口 − 固定前缀 − 输出预留」后再分账。
- 变更说明（v5）：P1–P4 落地后按代码复核，发现 11 处「文档承诺 / 实现行为」偏差——含 3 个无消费点的配置、1 个名不副实的后端枚举、1 个把轨迹预览长度当注入长度用的实现缺陷。本版新增第 18 章逐条处置，并声明它与第 4/5/6/8 章冲突时以第 18 章为准。
- 证据约定：现状结论带 `路径:行号`；设计建议与既存事实分开写

## 1. 设计结论

1. **上下文与状态是两个对象**：模型每轮能看见的是「系统提示 + 工具 schema + 消息历史 + 请求尾部的临时说明」；`AgentState` 是运行时状态，其中的 `permission_context` / `middle_context` 不会进入模型请求。二者必须分开设计，否则会把「只有运行时知道的字段」误当成「模型看得见的内容」。
2. **上下文分四区**：稳定前缀（系统提示 + 工具 schema）、历史区（`AgentState.context` + `summary`）、易变尾部（档位说明 + 记忆卡 + 长期检索 + 当前输入）、输出预留。只在尾部追加，前缀逐字节稳定，前缀缓存才不会被每轮的临时说明打掉。
3. **记忆分三层**：短期（工作记忆）、中期（会话情景）、长期（语义 + 程序性）。三层各有作用域、存储、生命周期和读写路径，不是同一份历史的三种叫法。
4. **压缩是一等能力**：短期做确定性裁剪，中期用 AgentScope 内置 `compress_context()` 生成情景摘要，长期做去重/合并/衰减。三层共用同一份上下文预算。
5. **层间只传递结论**：短期 → 中期是“压缩成记忆卡”，中期 → 长期是“稳定事实晋升”；原文不向上还原。
6. **优先内置包**：摘要用框架压缩，长期用 `Mem0Middleware` / `AgenticMemoryMiddleware`，状态用 `AgentState`；不手搓向量库和摘要模型。
7. **轨迹必须可见**：`memory.retrieve / trim / compress / write` 四类动作都落成 span，轨迹页签新增「记忆」泳道；上下文装配本身作为「第 0 步」展示。失败、跳过同样可见。

## 2. 目标与非目标

### 2.1 目标

- 一轮对话内自动完成“装配 → 召回 → 注入 → 执行 → 裁剪 → 压缩 → 写入”的闭环。
- 上下文可解释：每次请求都能回答「这一轮装了哪些区、各占多少 token、记忆注入了什么、有没有被裁剪」。
- 压缩可解释：每次都能回答“为什么压、压了哪层、压前压后多少 token、保留了什么”。
- 记忆后端可替换、可降级：缺包、超时、后端故障都不阻断对话。
- 默认会话隔离、写入脱敏、轨迹只留预览、用户可删除。

### 2.2 非目标

- 不自建向量数据库、embedding 服务或摘要模型。
- 不做跨用户共享（等鉴权落地）。
- 不做自动技能生成（程序性记忆本期只预留 `kind=procedure`）。
- 不做完整记忆管理台（只做最小列表、删除、状态）。
- 不改写 AgentScope 的 `compress_context()`，只挂元数据与事件。

## 3. 现状证据

### 3.1 上下文

- **有展示，没有装配设计**：`tools/context_api.py:96-126` 只返回系统提示、工具 schema、档位说明与一个合并指纹；没有历史区、没有记忆区、没有分区预算，前端拿不到「模型这一轮到底装了什么」的完整视图。
- **只有占用圆环，没有分区**：`web/src/workspace/ContextRing.tsx:63-108` 消费 `CONTEXT_BUDGET` 与压缩帧，只能画「已用 / 阈值」，画不出前缀、历史、记忆、输出预留各占多少。
- **注入位置有先例**：`runtime/profile_note.py:111-122` 在 `on_model_call` 的 messages 末尾追加 `UserMsg(name="system")`，并且明确不写回历史——这是记忆注入应当复用的位置（尾部追加，不破坏前缀缓存）。
- **状态里有不发给模型的字段**：`AgentState` 含 `context` / `summary` / `permission_context` / `middle_context`；`runtime/scope_runtime.py:259-263` 整体序列化进 `session_states.state_json`，但其中 `permission_context` / `middle_context` 只用于运行时判定，不进入模型请求。
- **快照无版本、无体积上限**：`sessions.py:121-123`、`sessions.py:255-279` 只做整体 JSON 落库与恢复，`assembly.py:279` 用 `AgentState.model_validate_json` 重建；损坏或跨版本时没有显式回退与体积保护。
- **预算口径只有一个来源**：`config/model.yaml` 的 `context_size` / `trigger_ratio` / `reserve_ratio` / `context_buffer_ratio` 经 `runtime/chat_model.py` 传给框架，`runtime/compression.py:56-75` 复述同一口径；目前没有任何地方扣除系统提示与工具 schema 的固定开销。

### 3.2 记忆

- **短期有执行态，无策略**：`AgentState.context` + `ContextConfig` 由框架维护；阈值来自 `config/model.yaml` 的 `trigger_ratio`；`runtime/compression.py:16-18` 只做观测，没有定义“保留哪些轮次、工具结果留多少、先丢什么”。
- **中期有摘要，无独立设计**：框架压缩写入 `agent.state.summary`；会话快照经 `sessions.py:121-123`、`sessions.py:270-274` 存进 `session_states.state_json`，重建时 `assembly.py:279` 用 `AgentState.model_validate_json` 恢复。摘要能跨轮存活，但没有版本、结构、TTL、查询接口和注入策略。
- **长期只有工厂**：`memory/ledger.py:14-31` 的 `build_mem0_middleware()` 无调用点；README 映射标注「仅工厂 + 单测」。
- **观测只覆盖压缩**：`compression.py` 发 `CONTEXT_BUDGET / CONTEXT_COMPRESSION_START / END`，前端 `ContextRing.tsx` 与 `CompressionRecord` 消费；但 `events.py:17-27`、`recorder.py:142-154`、`db.py:58` 的 span 类型都没有记忆；轨迹页签看不到记忆动作。
- **前端没有记忆维度**：`types/run.ts:117-125` 的 `ProcessKind`、`run.ts:308-312` 的 `TraceLanes`、`TraceLanes.tsx:15-18` 的 `ROWS` 都只有模型/工具。
- **可复用范式**：`CompressionSink`（`compression.py:79-98`）的“钩子写队列 → 流 drain → SSE”是记忆事件通道的模板；`profile_note.py:117-122` 的“末尾 system note”是记忆注入的位置先例。

## 4. 三层记忆模型

| 层 | 作用域 | 内容 | 存储 | 生命周期 | 读取 | 写入 |
|---|---|---|---|---|---|---|
| 短期 | 单轮/当前任务 | 当前输入、最近 N 轮、工具结果、临时计划 | `AgentState.context` | 轮内；随会话快照 | 直接进上下文 | 每轮追加 |
| 中期 | 单会话跨轮 | 目标、结论、产物、未决项、错误签名 | `session_memories` 表 + `AgentState.summary` | 会话 TTL + 版本上限 | 轮首注入记忆卡 | 压缩触发时生成新版本 |
| 长期 | 跨会话 | 稳定事实、偏好、可复用步骤 | Mem0 / Agentic 后端 | TTL + 条数上限 + 衰减 | `top_k` 语义检索 | 回合/会话结束自动写入 |

程序性记忆不单独算第四层，它是长期记忆的一个 `kind`（`fact / preference / procedure`）。

### 4.1 短期记忆（工作记忆）

职责：保证当前任务“想得起来”，同时不把窗口塞爆。

- 内容：当前用户输入（永不裁剪）、最近 `max_turns` 轮、工具结果（按类型限长）、临时计划/待办、系统注释（档位/联网/记忆）。
- 裁剪顺序：**旧工具结果 → 重复读取结果 → 较早的助手推理 → 较早的轮次**。当前用户输入、未完成确认、安全约束永不被裁。
- 工具结果：超过 `tool_output_chars` 时保留头 `head_ratio`、尾 `1-head_ratio`，中间标“已截断 N 字”；同一文件/命令的重复读取只留最后一次。
- 计划态：scratchpad 任务结束时清空，不写入长期记忆。
- 持久化：裁剪后的原文不单独落库；会话快照仍由 `AgentState` 整体序列化。

### 4.2 中期记忆（会话情景）

职责：跨轮保留“这个会话已经决定了什么”，而不是把历史消息再塞回去。

- 记忆卡 = 内置摘要 + 事件派生字段：
  - `text`：`compress_context()` 产出的摘要，权威内容；
  - `artifacts`：工具调用产生的文件路径/行号；
  - `pending`：未完成的确认/待办；
  - `errors`：失败签名；
  - `tokens`：压缩前后与预算；
  - `source_run_ids`：来源运行，便于回溯。
- 版本：每次压缩成功写新版本，`is_active` 只保留一条；最多 `max_versions`，超出删最旧。
- 读取：轮首把 active 卡片作为末尾 system note 注入；已注入过同一版本则不重复。
- 晋升：压缩触发即生成中期版本；会话结束把稳定事实交给长期层。
- TTL：`ttl_days` 到期删除；会话删除级联删除。

### 4.3 长期记忆（语义 + 程序性）

职责：跨会话记住“这个人/这台机器的稳定事实”。

- 后端：请求侧保留 `mem0` / `agentic` / `auto`，`effective` 目前只有 `local` / `off`；请求未接入的后端时降级为 `local`，状态接口用 `requested` / `effective` / `reason` 三字段说明（§18.2 M1）。
- 写入：只有「回合结束写入」一种实现（`write_on: turn`）；`session_end` 未实现，配置里写它会在加载时报错。
- 去重/冲突：交给后端（mem0 的 update/delete），不自研合并。写入前过 `input/pipeline/redact.py` 脱敏。
- 读取：`top_k` + `min_score` + 去重 + 字符预算；默认 `scope=session`，鉴权后再开 `user`。
- 遗忘：`ttl_days`、`max_entries`、删除 API、低置信度衰减；会话结束时做 consolidation。

### 4.4 层间流动

```
短期 --(确定性裁剪)--> 短期
短期 --(阈值/轮数触发, 内置摘要)--> 中期新版本
中期 --(会话结束, 稳定事实晋升)--> 长期
长期 --(轮首 top_k 检索)--> 上下文尾部注入
```

原则：**原文只向下压缩，不向上还原**；晋升的是结论，不是消息。

## 5. 上下文（Context）设计

本章补上 v3 缺失的部分：模型一轮到底看到什么、这些内容按什么顺序装配、各占多少预算、怎么与记忆层协作、怎么落快照、怎么观测。

### 5.1 两个对象：模型可见上下文 vs AgentState

必须先区分，否则「记忆注入」会被误当成「写历史」：

| 对象 | 位置 | 是否发给模型 | 谁维护 | 说明 |
|---|---|---|---|---|
| 系统提示 | `runtime/prompt.py` 的 `build_system_prompt()` | 是 | 装配期 | 稳定前缀的第一段 |
| 工具 schema | `tools/catalog.py` 按档位装配 | 是 | 装配期 | 稳定前缀的第二段；切档才变 |
| 消息历史 | `AgentState.context` | 是 | 框架 + 短期策略 | 追加、裁剪、压缩都发生在这里 |
| 框架摘要 | `AgentState.summary` | 是（框架拼进请求） | 框架 | 压缩产物，工作副本 |
| 档位说明 | `ProfileNoteMiddleware` 尾部消息 | 是 | 每轮请求 | 不写回历史 |
| 记忆卡 / 长期检索 | 本 spec 新增的尾部消息 | 是 | 每轮请求 | 不写回历史 |
| `permission_context` | `AgentState` | 否 | 运行时 | 只用于工具判定 |
| `middle_context` | `AgentState` | 否 | 运行时 | 装配标记（档位、快照版本、最后注入版本） |
| 中期版本表 / 长期条目 | `session_memories` / 后端 | 否（先检索再注入） | 记忆层 | 原始存储，按需取用 |

不变量：

1. **凡是要发给模型的内容，必须能归到上表「是」的某一行**；不允许有隐式拼装。
2. **注入不写历史**：尾部说明只存在于本次请求的 `messages` 副本里，不进入 `AgentState.context`，也不进快照。
3. **状态不等于上下文**：`permission_context` / `middle_context` 永远不序列化进模型请求。

### 5.2 上下文分区与稳定性

```
┌──────────────────────── 模型请求 ────────────────────────┐
│ 前缀 P  系统提示（稳定）                                   │
│ 前缀 T  工具 schema（档位内稳定）                           │
│ 历史 H  AgentState.summary + AgentState.context（追加/压缩）│
│ 尾部 N  档位说明 → 中期记忆卡 → 长期检索结果 → 当前输入      │
│ 预留 O  输出 token（max_tokens / reserve_ratio）            │
└──────────────────────────────────────────────────────────┘
```

| 区 | 内容 | 稳定性 | 变化时机 | 失效代价 |
|---|---|---|---|---|
| P | 系统提示 | 逐字节稳定 | 改提示词 / 换工作目录 | 前缀缓存全冷 |
| T | 工具 schema | 档位内稳定 | 切档、开/关联网工具 | 前缀缓存全冷 |
| H | 摘要 + 历史消息 | 追加；压缩时收缩 | 每轮追加；到阈值压缩 | 只影响新增段 |
| N | 档位/记忆/输入 | 每轮可变 | 每轮 | 位于尾部，不打掉前缀 |
| O | 输出预留 | 按参数 | 改 `max_tokens` | 无 |

设计要求：**只有 H 可以追加和收缩，P/T 不能逐轮改写，N 只能放末尾**。这是前缀缓存能命中的前提，也是记忆卡必须放尾部而不是写进系统提示的原因。

### 5.3 每轮上下文装配流水线

固定七步，顺序不可交换（交换会改变缓存或预算口径）：

1. **解析装配输入**：档位、工作目录、模型参数、联网开关 → 得到 P 与 T（`assembly.py` 现有流程）。
2. **恢复历史**：从 `session_states.state_json` 还原 `AgentState`（无快照则新建）；只取 `context` / `summary` 作为 H。
3. **轮首召回**：读 active 中期卡 → 长期 `top_k` 检索 → 去重、截断、按字符预算装配 N。
4. **短期确定性裁剪**：在 `on_model_call` 拿到 messages 后，对旧工具结果做头尾保留、重复读取去重、超 `max_turns` 的旧轮次淘汰；当前输入与未决确认不动。
5. **尾部注入**：按「档位说明 → 中期卡 → 长期检索」顺序追加 `UserMsg(name="system")`；同一版本/同一批命中只注入一次（见 5.5）。
6. **框架压缩**：若 token 超过 `trigger_ratio × context_size`，由 `compress_context()` 收缩 H；成功则写中期新版本（第 6 章）。
7. **落快照**：轮末保存 `AgentState`（不含任何 N 区内容），并记录本版中期卡 id 与最后注入版本。

对应模块：

| 步骤 | 落点 |
|---|---|
| 1 | `runtime/assembly.py`（已有） |
| 2 | `runtime/sessions.py` + `runtime/assembly.py::session_runtime_factory`（已有，补版本与体积保护） |
| 3 | 新增 `memory/context.py::assemble_notes()` |
| 4 | 新增 `memory/short_term.py::trim_messages()`，由 `MemoryMiddleware.on_model_call` 调用 |
| 5 | 新增 `memory/context.py::inject_notes()`，与 `ProfileNoteMiddleware` 同位置 |
| 6 | `runtime/compression.py`（已有，补 `MEMORY_COMPRESS_*`） |
| 7 | `runtime/scope_runtime.py::snapshot()`（已有，补体积保护与 `middle_context` 标记） |

### 5.4 预算算式（单一来源）

窗口与比例不在本 spec 另立常量，全部来自 `config/model.yaml`（经 `load_model()`）：

```
W  = context_size                       # 模型窗口
R  = trigger_ratio                       # 框架压缩阈值比例（<=0.9）
O  = max_tokens or reserve_ratio × W     # 输出预留；max_tokens 为空时用 reserve_ratio
S  = tokens(system prompt) + tokens(tool schemas)   # 固定前缀
Hmax = max(0, R × W − S − O)             # 可分配给「历史 + 记忆」的总额度

B_short    = Hmax × 0.50
B_episodic = Hmax × 0.30
B_long     = Hmax × 0.10
B_headroom = Hmax × 0.10                 # 为追加消息与工具结果波动留的余量
```

**这是对 v3 的修正**：v3 直接令 `memory_budget = context_size × trigger_ratio`，没有扣掉系统提示、工具 schema 与输出预留。实测工具 schema 单项可达上万字符（`tools/context_api.py:1-13` 的注释给出 10139 字符 ≈ 3000 token），不扣除会让「短期 50%」大于真实可用的历史空间，压缩触发时才发现装不下。

示例（`W=128000`、`R=0.8`、`max_tokens` 为空、`reserve_ratio=0.1`、`S≈8000`）：

```
Hmax = 0.8 × 128000 − 8000 − 12800 = 81600
短期 40800 / 中期 24480 / 长期 8160 / 余量 8160
```

约束与降级：

- `S + O >= R × W` 时 `Hmax=0`：视为配置错误，启动期告警并在 `/health` 的 `memory.reason` 里说明；运行时仍可用，但不启用记忆注入。
- 某层未启用（如长期 `off`）时，其份额**全部并入短期**；`headroom` 不参与再分配（§18.4）。
- token 计数取不到时用 `tools/context_api.py::estimate_tokens()` 估算，并在事件里标记 `estimated=true`；估算只影响预算分配，不影响是否压缩（压缩仍由框架判定）。

### 5.5 注入格式与去重

尾部注入统一由 `memory/context.py` 生成，格式固定、可测试：

```
## 会话记忆（第 {version} 版）
目标：...
已决策：...
产物：path:line, ...
未决：...

## 长期记忆（检索 top_k={k}，命中 {n} 条）
[1] (score=0.82) ...
[2] (score=0.61) ...
```

规则：

- **只注入变化**：`middle_context["memory"]` 记录 `episodic_version` 与 `long_term_hash`；版本未变且命中集合未变则不重复注入。会话重建后按快照里的标记继续去重。
- **分段预算**：中期卡最多占 `note_max_chars × episodic_note_ratio`，长期最多占 `note_max_chars × long_note_ratio`；超了先丢低分长期条目，再截中期卡（保留目标/未决）。
- **不注入空内容**：没有中期卡、没有命中时不追加空标题，避免每轮多一段无效文本。
- **安全**：长期条目来自外部内容，按不可信文本处理——转义边界标记、不覆盖系统约束、不包含密钥占位符真值；写入前已脱敏（第 9 章）。
- **顺序固定**：档位说明 → 中期卡 → 长期检索。档位说明在最前，保证权限约束先于记忆内容被模型读到。

### 5.6 前缀缓存与失效条件

- P/T 只在以下情况变化：改系统提示、换工作目录、切档、开/关联网工具、换模型。此时前缀缓存必然失效，属于预期成本。
- N 区每轮可变，但位于请求尾部，不影响 P/T/H 已缓存的前缀。
- 明确禁止：把档位说明、记忆卡、时间戳写进系统提示；把每轮变化的内容插到历史中间；为了「格式整齐」重排历史消息。
- `/api/context` 的指纹拆成 `system_fingerprint`、`tools_fingerprint`、`prefix_fingerprint` 三段，前端能指出是哪一段变了导致的未命中。

### 5.7 上下文快照与恢复

快照存储沿用 `session_states.state_json`，但明确内容与约束：

- **存**：`context`、`summary`、`permission_context`、`middle_context`（含 `permission_profile`、`snapshot_version`、`memory.episodic_version`、`memory.long_term_hash`）。
- **不存**：任何尾部注入内容、长期原始条目、中期卡片全文（卡片在 `session_memories`，快照只存 active 版本号）。
- **版本**：`middle_context["snapshot_version"] = 2`；恢复时缺省视为 1（老快照），只补默认值，不改写 `context`。
- **体积上限**：`snapshot_max_chars`（默认 1.5M 字符）。序列化前先做一次短期裁剪；仍超限则丢弃最早的整轮消息并记 `MEMORY_TRIM(reason=snapshot_cap)`，绝不截断当前输入。
- **失败回退**：`model_validate_json` 失败 → 视为新会话（沿用现有行为），发 `CONTEXT_RESTORE` 事件说明原因；写快照失败 → 只记日志与事件，不影响本轮回答。
- **恢复后注入**：快照里的 `memory.episodic_version` 用于避免重建后重复注入同一版本；长期命中不缓存，重建后重新检索。

### 5.8 上下文裁剪、压缩与记忆更新的协同

一轮内只允许发生一次框架压缩，顺序固定：

1. 先做短期确定性裁剪（不调模型、低延迟）；
2. 裁剪后仍超过 `R × W` 才触发框架压缩；
3. 压缩成功 → 写中期新版本 → 更新 `middle_context["memory"]`；
4. 压缩失败/超时 → 保留旧摘要与旧卡片，本轮继续，事件 `state=error`；
5. 压缩触发后仍超预算 → 按「丢长期 → 截中期 → 裁旧工具结果 → 裁旧轮次」继续收缩；不再二次调模型。

超预算收缩顺序与第 6 章一致；本节的职责是保证**上下文装配与记忆压缩不各自为政**：预算只有一份（5.4），触发只有一次（本节），事件只有一类（第 8 章）。

### 5.9 上下文可观测

**第 0 步（装配视图）**：扩展 `GET /api/context`，在现有字段上增加分区与预算：

```json
{
  "profile": "workspace",
  "zones": {
    "prefix": {"system_tokens": 1200, "tools_tokens": 3000, "system_fingerprint": "...", "tools_fingerprint": "..."},
    "history": {"messages": 18, "summary_chars": 2400},
    "tail": {"policy_note_tokens": 90, "memory_note_chars": 480, "injected": true},
    "output_reserve": 12800
  },
  "budget": {"window": 128000, "threshold": 102400, "history_max": 81600,
             "short_term": 40800, "episodic": 24480, "long_term": 8160, "headroom": 8160},
  "memory": {"episodic_version": 3, "long_term_hits": 2, "backend": "auto"},
  "snapshot": {"version": 2, "chars": 41200, "trimmed": false}
}
```

**流内事件**：每轮在第一次模型调用前发一次 `CONTEXT_ASSEMBLED`（只在真实运行时发，Echo/测试替身不发），字段：

| 字段 | 含义 |
|---|---|
| `zones` | 各分区的 token/字符数（与 `/api/context` 同结构） |
| `injected_chars` | 本轮记忆注入了多少字符 |
| `dropped` | 本轮被丢弃的类别与数量（工具结果/轮次/长期条目） |
| `estimated` | token 是否为估算值 |
| `reason` | 跳过原因（未启用/无记忆/超预算降级） |

`CONTEXT_ASSEMBLED` 是 UI 帧，不落 span；历史回放时前端改用 `/api/context` + `spans` 重建第 0 步，避免为每次运行多存一条冗余记录。

**轨迹**：第 0 步显示分区条形图与「注入了什么（预览）」；记忆动作仍按第 8 章进入「记忆」泳道。

### 5.10 失败模式与降级

| 失败 | 影响 | 处理 |
|---|---|---|
| token 计数不可用 | 无法精确判断阈值 | 用 `estimate_tokens()` 估算，标记 `estimated`；压缩仍由框架决定 |
| 固定前缀过大（`Hmax=0`） | 没有历史预算 | 启动告警；不启用记忆注入；对话仍可进行 |
| 记忆卡超长 | 尾部挤压历史 | 先丢长期低分条目，再截断卡片细节，保留目标/未决 |
| 快照损坏 | 无法恢复历史 | 按新会话重建，发 `CONTEXT_RESTORE(state=error)`，不阻断对话 |
| 快照超限 | 落库失败 | 序列化前裁剪；仍超则丢最早轮次并记录 |
| 压缩失败/超时 | 摘要未更新 | 保留旧摘要与旧卡片，发 `MEMORY_COMPRESS_END(state=error)`，本轮继续 |
| 记忆后端不可用 | 无长期检索/写入 | 降级为「仅短期 + 中期」，`/health` 与 `/api/memory/status` 报原因 |

## 6. 记忆压缩设计

### 6.1 统一预算

预算算式见 5.4，本章不再重复定义。补充三条硬约束：

1. 达到 `R × W` 才触发框架压缩；短期裁剪不算压缩，不消耗 LLM。
2. 各层份额只在未启用时重新分配，不允许在运行中为「省事」临时越界。
3. 输出预留 O 不可被任何记忆层占用。

超预算按固定顺序收缩：

1. 丢弃长期检索结果（最便宜，可重检索）；
2. 截断中期摘要（保留目标/未决项，删细节）；
3. 裁剪旧工具结果；
4. 裁剪最早轮次；
5. 仍超则触发一次中期压缩；压缩失败只记录事件，不阻断对话。

### 6.2 触发矩阵

| 层 | 触发条件 | 方法 | 产出 |
|---|---|---|---|
| 短期 | 工具结果超 `tool_output_chars`；重复读取；轮数超 `max_turns`；token 超短期份额 | 确定性裁剪/去重，无 LLM | 回收 token，保留头尾 |
| 中期 | 框架压缩真正发生（`compressed=True`）；短期压缩后仍超预算 | `compress_context()` | 新版本记忆卡 |
| 长期 | 会话结束；每 `consolidate_every_runs` 次运行 | 后端去重/合并/衰减 | 条目数收敛 |

### 6.3 三层压缩管线

- **短期 = 确定性压缩**：不调模型，低延迟。工具结果头尾保留、重复结果去重、旧轮次淘汰。每次发 `MEMORY_TRIM`，记录丢弃类别与回收字符。
- **中期 = 内置摘要**：复用框架 `compress_context()`（已有钩子与阈值，见 `compression.py:100-150`），不自写摘要提示词。压缩后落新版本；`CONTEXT_COMPRESSION_*` 继续供 `ContextRing`，同时补发 `MEMORY_COMPRESS_*` 供轨迹页签，避免重复压缩。
- **长期 = 合并而非摘要**：后端做去重/更新/删除；记录合并前后条目数与耗时，不生成新的自然语言摘要。

### 6.4 保留与丢弃

| 保留（跨压缩） | 丢弃（可安全消失） |
|---|---|
| 当前用户目标与显式约束 | 问候、寒暄、重复确认 |
| 已确认的决策与原因 | 中间推理过程 |
| 文件路径、行号、命令 | 超长工具原始输出（保留头尾） |
| 错误签名与修复结果 | 重复读取的同一文件 |
| 未完成的确认/待办 | 过期的时间性信息（可进长期层） |

### 6.5 中期记忆卡 schema

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

`text` 是权威摘要；其余字段只从事件派生，不解析摘要文本。

### 6.6 原子性与失败

- 新版本先写库（`is_active=0`），成功后单事务把旧版本置 `0`、新版本置 `1`；失败保留旧版本。
- 压缩超时/异常：发 `MEMORY_COMPRESS_END(state=error)`，沿用旧摘要，本轮继续。
- 短期裁剪永不删当前输入与未决确认；压缩失败不丢当前轮。

### 6.7 与现有 `compression.py` 的关系

不重写压缩逻辑，只加元数据：

1. `CompressionMiddleware` 仍是唯一压缩入口；
2. 在 `START/END` 处补发 `MEMORY_COMPRESS_*`（带 `layer=episodic`、`trigger`、`reclaimed_tokens`）；
3. 压缩成功后由记忆层落新版本；`CONTEXT_*` 事件保持不变，ContextRing 不受影响。

## 7. 每轮读写时序

与 5.3 的装配流水线是同一件事的时序表达，便于实现对照：

1. **轮首**：加载 active 中期卡 → 长期 `top_k` 检索 → 预算调度（5.4）→ 作为末尾 system note 注入（先中期、后长期）。
2. **执行中**：短期追加消息与工具结果；超过单项上限立即裁剪；框架到阈值时触发中期压缩。
3. **轮末**：按 `write_on` 写长期候选；若触发中期更新则落库；会话快照照旧保存。
4. **会话结束/空闲**：最终压缩一次中期、长期 consolidation、清理过期版本。

## 8. 轨迹可观测（硬要求）

### 8.1 统一事件契约

| 事件 | 层 | 关键字段 |
|---|---|---|
| `CONTEXT_ASSEMBLED` | 上下文 | `zones`、`injected_chars`、`dropped`、`estimated`、`reason` |
| `CONTEXT_RESTORE` | 上下文 | `state(ok/error/empty)`、`snapshot_version`、`chars`、`reason` |
| `MEMORY_RETRIEVE_START/END` | 中/长 | `layer`、`scope`、`backend`、`top_k`、`hits`、`ids`、`scores`、`injected_chars`、`latency_ms`、`state(hit/miss/error/skipped)`、`reason` |
| `MEMORY_TRIM` | 短 | `dropped_tool_results`、`dropped_turns`、`deduped_reads`、`reclaimed_chars`、`reason` |
| `MEMORY_COMPRESS_START/END` | 短→中 | `trigger`、`tokens_before`、`tokens_after`、`budget`、`version`、`latency_ms`、`state`、`reason` |
| `MEMORY_WRITE_START/END` | 中/长 | `layer`、`stored`、`deduped`、`consolidated`、`latency_ms`、`state`、`reason` |

事件负载用结构化 `memory` 对象承载；`events.py` 对特殊数组的保留方式见 `REQUIRE_USER_CONFIRM.tool_calls`（`events.py:154-170`）。只放预览，不放全文。

### 8.2 事件通道

```
MemoryObserver 钩子 → MemorySink(deque)
    → AgentScopeRuntime.stream() drain → SSE 帧 → recorder + 前端
```

`MemorySink` 与 `CompressionSink` 同构（`compression.py:79-98`），在 `scope_runtime.py:227-230` 同一位置补发。Echo/测试替身不构造记忆中间件，不产生记忆事件。

### 8.3 落库与迁移

- `spans` 新增 `type='memory'`；`name` 取 `memory.retrieve / trim / compress / write`。
- `input` 存查询或触发原因；`output` 存结果摘要 JSON（层、命中 id/score、前后 token、丢弃类别）；`duration_ms`；`status`；`error`。
- 现有统计只按 `tool/permission/model` 取数（`api.py:331-355`），memory span 不会污染工具失败率。
- `spans.type` 的 CHECK 在 SQLite 改不了，需一次性重建表（新表允许 `memory`，复制旧行、重建索引、改回原名），幂等并带老库测试。不采用伪装成 `tool` 的方案，会污染泳道与失败率。
- `CONTEXT_ASSEMBLED` / `CONTEXT_RESTORE` 不落 span：前者每轮必发、体积小且可由 `/api/context` 重建；后者属于会话级诊断，进日志与 `/health`。

### 8.4 前端

- `types/run.ts`：`ProcessKind` 加 `memory`；`TraceStep.kind/layer` 加 `memory`；`TraceLanes` 加 `memory: TraceSpan[]`；新增 `ContextZone` / `ContextAssembly` 类型供第 0 步。
- 新增 `api/stream/handlers/memory.ts` 并在 `handlers/index.ts` 注册；步骤行带层徽标：
  - `记忆检索 · 长期 · 3 条`
  - `记忆裁剪 · 短期 · 回收 12k 字`
  - `记忆压缩 · 短期→中期 · 103k→41k`
  - `记忆写入 · 长期 · 新增 2 / 去重 1`
- `TraceLanes.tsx` 增加第三条泳道「记忆」；图例同时有文字与图标，不能只靠颜色。
- 轨迹第 0 步「上下文装配」：横向分区条（前缀/历史/尾部/预留）+ 注入预览（中期卡版本、长期命中条数、字符数），数据来自 `/api/context`。
- `StepRow` / `TraceInspector` 复用现有 `args/result` 槽位：压缩行展示前后 token 与保留项，检索行展示命中明细，写入行展示新增/去重。
- 历史回放通过 `/api/runs/{id}/spans`，`SpanOut.type` 已透传，按 `type='memory'` 映射。
- 按 AGENTS.md §3.8，改 `web/**` 前必须执行 modern-web-guidance 的 search → retrieve → 校验，并披露。

### 8.5 轨迹示意

```
上下文 █           第 0 步 前缀 4.2k · 历史 18 条 · 记忆 480 字 · 预留 12.8k
记忆   ██ 8ms     压缩 短期→中期 103k→41k（保留 3 决策 / 2 产物）
模型   ████████   第 1 次推理
工具   ██          Bash · 802ms
记忆   █ 3ms      检索 长期 命中 2 条 → 注入 480 字
记忆   █ 1ms      裁剪 短期 回收 12k 字（旧工具结果 3 条）
```

## 9. 隔离与隐私

- 默认 `scope=session`，`user_id = "session:" + session_id`；没有鉴权前不开放跨用户。
- 写入前脱敏；密钥占位符不进入任何层。
- 轨迹只存预览（query 120 字、命中 80 字、摘要 160 字），不存全文。
- 删除 API 同时删中期版本与长期条目；轨迹保留“已删除”动作而非原文。
- 跨会话污染是红线：会话 A 写入后，会话 B 检索必须为空。
- 上下文快照只存运行时状态，不存注入内容与长期条目原文。

## 10. 配置

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
    max_chars: 3000
    max_versions: 20
    ttl_days: 30
  long_term:
    enabled: true
    backend: auto          # auto | mem0 | agentic | local | off（effective 只有 local / off）
    scope: session         # session | user
    top_k: 5
    min_score: 0.35
    max_entries: 200
    ttl_days: 90
    write_on: turn         # 只有 turn；session_end 未实现，写它会在加载时报错
  budget:
    short_term_ratio: 0.50
    episodic_ratio: 0.30
    long_term_ratio: 0.10
    headroom_ratio: 0.10
  context:
    enabled: true
    note_max_chars: 1200          # 尾部记忆注入总上限
    episodic_note_ratio: 0.6      # 其中中期卡占比，其余给长期检索
    snapshot_max_chars: 1500000   # 会话快照体积上限
    snapshot_version: 2           # 写入新快照的版本号
    assemble_event: true          # 是否发 CONTEXT_ASSEMBLED
  trace:
    query_preview_chars: 120
    hit_preview_chars: 80
    summary_preview_chars: 160
```

环境变量：`DEVOPS_AGENT_MEMORY_ENABLED / _BACKEND / _SCOPE / _USER_ID`；env 优先、yaml 次之、内置默认兜底。

**单一来源**：`context_size` / `trigger_ratio` / `reserve_ratio` / `context_buffer_ratio` / `max_tokens` 继续只写在 `config/model.yaml`，本文件不复制；`memory.context` 只放「记忆注入与快照」特有的旋钮。

## 11. 接口（最小面）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/context?session=&profile=` | 第 0 步：分区、预算、指纹分段、记忆注入预览（在现有响应上扩展） |
| GET | `/api/memory/session/{session_id}` | 当前中期卡片 + 版本列表（预览） |
| GET | `/api/memory/long-term?scope=session&session_id=...` | 长期记忆列表（分页、预览） |
| DELETE | `/api/memory/{layer}/{memory_id}` | 删除中期版本或长期条目 |
| GET | `/api/memory/status` | 三层开关、后端、条数、降级原因 |

`/health` 增加 `memory: {short_term, episodic, long_term, backend, reason}` 与 `context: {window, threshold, history_max, snapshot_version}`。

## 12. 兼容与迁移

1. 老观测库：启动时执行 spans 表重建；失败只关闭记忆观测，不影响运行。
2. 中期版本表新增，不影响 `session_states`；旧会话没有卡片时按“无中期记忆”处理。
3. 老快照没有 `snapshot_version`：按 1 处理，只补默认值，不改写 `context`；注入去重标记缺失时允许重建后多注入一次，不视为错误。
4. Echo/测试：不构造记忆中间件，现有测试的默认行为不变；`CONTEXT_ASSEMBLED` 只在真实运行时发。
5. `CONTEXT_*` 事件与 ContextRing 保持不变，新事件只增量添加。
6. 首版默认开启短期裁剪与中期压缩，长期默认 `auto`（无后端则 off）。
7. `/api/context` 只在响应上新增字段，不改既有键，老前端按原字段仍可用。

## 13. 验收

- A1 短期裁剪：工具结果超限后只留头尾，当前输入与未决确认不丢；`MEMORY_TRIM` 有丢弃明细。
- A2 中期压缩：达到阈值后生成新版本，`text` 来自内置摘要，`tokens_before/after` 正确；旧版本保留。
- A3 中期召回：下一轮注入 active 卡片，不重复注入；重建会话后仍能读到。
- A4 长期读写：命中时注入并记录；未命中不注入；写入去重；跨会话隔离。
- A5 压缩失败：异常/超时保留旧卡片，对话继续，轨迹 `state=error`。
- A6 预算顺序：超预算先丢长期、再截中期、再裁旧工具结果，最后才动旧轮次。
- A7 轨迹：四类记忆动作都能在轨迹页签看到；历史回放同样可见；第 0 步能看到上下文分区。
- A8 隐私：轨迹不含全文与密钥；删除后列表与检索均不可见。
- A9 迁移：旧 schema 库可升级，旧 run 的 spans 仍可读；旧快照可恢复。
- A10 门禁：覆盖率 ≥80%、单文件 ≤500 行、ruff/mypy/vulture/前端 lint/typecheck 通过。
- A11 预算正确：`Hmax = R×W − S − O`；`Hmax=0` 时启动告警且不注入记忆，对话仍可用。
- A12 前缀稳定：同一档位、同一模型下连续两轮，P/T 指纹不变；记忆注入了不同内容也不改变指纹。
- A13 装配顺序：请求里 N 区顺序恒为「档位 → 中期 → 长期」，且当前输入始终在最后。
- A14 快照：`state_json` 不含任何注入内容；恢复后 `episodic_version` 一致；损坏快照回退到新会话并留事件。
- A15 只注入变化：同一中期版本连续两轮只注入一次；长期命中集合不变时不重复注入。

## 14. 实施顺序（4 个 PR）

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P1 | 上下文装配（分区/预算/尾部注入）+ 短期策略（窗口/工具结果/去重）+ `CONTEXT_ASSEMBLED` + `MEMORY_TRIM` + `MemorySink` + spans 迁移 + recorder | 无 |
| P2 | 中期记忆卡与版本表 + 接 `compress_context()` + `MEMORY_COMPRESS_*` + 注入去重 + 快照版本与体积保护 | P1 |
| P3 | 长期后端工厂（mem0/agentic/auto）+ 检索/写入/consolidation + 隔离 | P2 |
| P4 | 前端轨迹泳道与步骤行 + 第 0 步分区视图 + 管理 API/最小面板 + 指标与文档 | P1–P3 |

P1 合入后轨迹已能看到短期裁剪与上下文装配；P2 能看到压缩前后；P3 才引入外部后端。

## 15. 风险与决策点

1. **内置压缩摘要格式未知**：`compress_context()` 的摘要结构需先 introspect；本期只把 `text` 当权威，不做文本解析。
2. **中期卡片与框架 summary 双份**：框架 summary 是工作副本，DB 卡片是版本快照；压缩后以新版本覆盖 active，避免双写漂移。
3. **无鉴权**：默认 session 隔离；开放 user 需与鉴权 spec 联动。
4. **压缩延迟**：中期压缩是 LLM 调用；超时保留旧卡，事件可见。
5. **记忆污染**：长期只写稳定内容，`min_score` + 去重 + 可删除兜底。
6. **迁移风险**：spans 重建用事务包裹，启动校验结构；失败禁用记忆观测而非阻塞服务。
7. **预算扣除依赖估算**：`S` 在装配期用 `estimate_tokens()` 估算，与真实 tokenizer 有偏差；偏差只影响份额分配，阈值仍由框架真实计数决定。
8. **尾部注入改变请求形状**：每轮记忆内容变化会让「尾部之后」没有缓存可命中；这是把记忆放尾部的固有成本，换来的是前缀与历史仍然命中。若实测收益为负，可改为「版本变化才注入」。

## 16. 度量

- 上下文：固定前缀 token 占比、历史区 token 分布、尾部注入字符数、第 0 步打开率、前缀指纹变化次数。
- 短期：回收字符/轮次、裁剪触发率、当前输入丢失数（必须为 0）。
- 中期：压缩次数、压缩比、压缩延迟、版本数、重建后可用率、重复注入次数（目标为 0）。
- 长期：检索命中率、注入字符、写入/去重/删除数、跨会话污染（必须为 0）。
- 全局：记忆带来的 token 增量、首字延迟增量、失败率。

## 17. 未决

- `AgenticMemoryMiddleware` 的存储位置、清理接口与并发行为。
- mem0 走云端还是本地 OSS（影响隐私与部署）。
- 中期卡是否需要“稳定事实抽取”再进长期，还是直接交给 mem0 自动抽取。
- 程序性记忆（长期层 `kind=procedure`）何时接入 `skills/`。
- 尾部注入的缓存成本需要实测：记忆每轮都变时，是否值得改为「只在版本/命中变化时注入」（已在 15.8 记录为可回退项）。
- 第 0 步是否需要在历史回放时也返回当时的快照分区，而不只是当前 `/api/context` 的实时视图。
- 中期「每 N 轮」周期压缩是否值得（需要显式压缩入口，不是框架阈值能触发的）；在给出稳定入口前不再保留 `trigger_turns` 字段。
- `write_on: session_end` 依赖会话结束钩子，何时接入由 P6 定。

## 18. v5 修订：实现差距与处置

v4 是设计先行；P1–P4 落地后按代码复核，暴露出 11 处偏差：有的旋钮没有消费点，有的承诺没有后端，有的动作完全不可见。本章逐条给出处置。**凡与第 4、5、6、8 章的表述冲突，以本章为准**（被替代的条款在 18.3 集中列出）。

### 18.1 处置原则

1. **配了必须生效**：没有消费点的配置项删除，不保留「预留」。预留字段比缺字段更糟——它让调参的人以为改了有效果。
2. **承诺必须可运行**：没接入的后端只能出现在 `requested`，不能出现在 `effective`。
3. **静默即缺陷**：裁剪、降级、失败都要有事件或日志，否则等于没实现。
4. **修正优先于新增**：先让已声明的能力真实生效，再谈 `session_end` 与向量后端。

### 18.2 差距清单与处置

| # | 差距 | 证据 | 处置 | 阶段 |
|---|---|---|---|---|
| M1 | 后端枚举名不副实：`mem0` / `agentic` 未接入，恒降级 `off` | `memory/long_term.py:128-132`；`memory_policy.py:37` | 状态接口拆 `requested` / `effective`；`auto` 只解析到当期可用后端（目前仅 `local`）；请求未接入后端时启动 WARN，`/api/memory/status` 的 `reason` 写明「X 未接入，已降级」 | P5-1 |
| M2 | `episodic.trigger_turns` 无消费点，「每 N 轮压缩」不存在 | `memory_policy.py:27`；唯一赋值 `openjudge/memory/variants.py:33` | 删除字段、yaml 键与 variants 传参；6.2 触发矩阵删掉该行；周期压缩进未决 | P5-1 |
| M3 | 注入的摘要正文被**轨迹预览长度**截断（默认 160 字），`episodic.max_chars` 从未被读 | `memory/context.py:41`；`memory/observer.py` 传入 `policy.trace.summary_preview_chars` | 正文上限改为 `min(max_chars, note 预算 × episodic_note_ratio)`；`summary_preview_chars` 只用于事件预览；`max_chars` 改为卡片写库前的正文上限 | P5-2 |
| M4 | `write_on: session_end` 静默不写 | `memory/runtime.py:205` 仅 `turn` 通过 | 枚举收窄为 `turn`；`session_end` 依赖会话结束钩子，进未决 | P5-1 |
| M5 | 快照版本从未写入，恢复事件从未发出 | `tools/context_api.py:138` 读 `middle_context["snapshot_version"]` 但全仓无写入点；`memory/events.py:19` 定义 `CONTEXT_RESTORE` 无 emit | `snapshot()` 写入 `policy.context.snapshot_version`；恢复失败/空快照写结构化日志并计入 `/health`；删除 `CONTEXT_RESTORE` 常量（8.3 已规定它不进 SSE） | P5-3 |
| M6 | 快照裁剪不可见：`TrimReport` 被丢弃 | `memory/runtime.py:238-245` | 返回 `TrimReport`；`changed` 且 `dropped_turns>0` 时发 `MEMORY_TRIM(reason=snapshot_cap)` | P5-3 |
| M7 | 关层份额口径不一致：实现全并入短期，文档写 6:1:1 | `memory/budget.py:65-66` 对 §5.4 | 以实现为准改文档：闲层份额全部并入短期，`headroom` 不参与再分配。6:1:1 无实测依据，且关层时中期通常也不需要更多空间 | P5-1（文档） |
| M8 | 检索失败伪装成未命中 | `memory/runtime.py:94-103` 吞异常返回空；`memory/observer.py:118-119` 据此判 `state=miss` | `recall` 返回 `(card, hits, error)`；`error` 非空时 `MEMORY_RETRIEVE_END` 的 `state=error`、`reason` 带异常类名；`miss` 只表示「真的没有」 | P5-2 |
| M9 | 长期写入是回合原文，不是稳定事实 | `memory/runtime.py:203-210` 把 user + assistant 拼成 ≤1000 字直接写 | 本期标注为已知偏差；写入前加候选过滤（去招呼语/纯确认、长度下限、优先含路径与命令的回合），摘要式写入进 P6。理由：4.3 要求「只写稳定事实」，当前实现等于把长期层当日志 | P5-4 |
| M10 | `reply_id` 为空时重复装配、重复注入 | `memory/observer.py:87` 缓存键依赖 `reply` 非空；`observer.py:139-148` 去重也依赖 `previous.reply_id != reply` | 确立不变量：同一 reply 只装配一次；跨 reply 只在版本或命中集合变化时注入；缓存键不得以 `reply_id` 非空为前提（空值回落到请求首条消息的摘要键） | P5-2 |
| M11 | 注释与实现相反 | `memory/short_term.py:158-170` docstring 写「截断→去重→淘汰」，实际是「去重→截断→淘汰」 | 改 docstring；顺序不动——先截断会把同一份输出压成同一段头尾，去重反而失效 | P5-1 |

### 18.3 被替代的条款

| 原条款 | 替代为 |
|---|---|
| 1.6「长期用 `Mem0Middleware` / `AgenticMemoryMiddleware`」 | M1：本期不承诺，仅 `local` 可用 |
| 4.3「后端：`mem0` / `agentic` / `auto`」 | M1：枚举保留给请求侧，`effective` 只有 `local` / `off` |
| 4.3「写入：回合结束或会话结束」 | M4：本期只有 `turn` |
| 5.4「未启用层按 6:1:1 并入短期/中期/余量」 | M7：全部并入短期，`headroom` 不参与 |
| 6.2 触发矩阵「每 `trigger_turns` 轮」 | M2：删除，只保留阈值触发 |
| 8.1 `CONTEXT_RESTORE` 事件行 | M5：改为结构化日志 + `/health` 计数，不进 SSE |

### 18.4 修正后的预算分账

5.4 的算式保留，只改分账三行；符号含义不变：

```
Hmax       = max(0, R × W − S − O)
B_short    = Hmax × (0.50 + Σ 未启用层的份额)
B_episodic = Hmax × (episodic 启用 ? 0.30 : 0)
B_long     = Hmax × (long 启用 ? 0.10 : 0)
B_headroom = Hmax × 0.10
```

三条约束：全开时四项和恰为 1.0；任一层关闭时短期吸收其份额，其余比例不动；`headroom` 永不参与再分配，`Hmax=0` 时不注入记忆。

### 18.5 新增验收

- **A16** `requested` / `effective` 分离：配 `mem0` 时 `/api/memory/status` 返回 `requested=mem0`、`effective=local`、`reason` 非空。
- **A17** 死配置清零：`config/memory.yaml` 每个键在 `src/` 内有消费点；`trigger_turns` 与 `write_on=session_end` 各有一条反回归单测（构造该配置应校验失败）。
- **A18** 注入长度：note 中卡正文可达 `min(max_chars, episodic 预算)`，不再被 160 字截断。
- **A19** 快照版本：`snapshot()` 之后 `/api/context` 的 `snapshot.version = 2`；损坏快照恢复有日志且 `/health` 计数 +1。
- **A20** 快照裁剪可见：触发 `MEMORY_TRIM(reason=snapshot_cap)`，且 `dropped_turns > 0`。
- **A21** 空 `reply_id`：同一 reply 内两次模型调用只装配一次、只注入一次。
- **A22** 检索失败：后端抛异常时 `state=error` 而非 `miss`，`reason` 含异常类名。
- **A23** 长期过滤：寒暄与纯确认不写入；NB 赛道 `junk_rate` 不高于 0.10。

### 18.6 实施顺序

| 阶段 | 内容 | 依赖 | 风险 |
|---|---|---|---|
| P5-1 | 清理与语义（M1/M2/M4/M7/M11） | 无 | 纯配置、常量、注释；必须与 P5-2 解耦，否则 diff 混在一起看不出行为变化 |
| P5-2 | 注入质量（M3/M8/M10） | P5-1 | 影响全部记忆指标，改完必须重跑基线 |
| P5-3 | 快照可观测（M5/M6） | 无 | 只加观测，不改注入，可独立合并 |
| P5-4 | 长期写入过滤（M9） | P5-2 | 需要 NB / PR 赛道配合验证 |

P5-1 删除 `trigger_turns` 会同时影响 `openjudge/memory/variants.py`，**评测变体与生产模型必须同 PR 修改**，否则评测启动即 `TypeError`。

### 18.7 对评测的影响

- `variants.py:33` 传 `trigger_turns=6`，P5-1 必须同步删除；这是当前唯一会让评测直接崩掉的耦合点。
- CP（压缩保真）的现役用例摘要都短于 160 字，所以当前通过**不能证明注入长度够用**：真实会话的 `compress_context` 摘要通常远超 160 字，M3 修好前这部分内容会在注入侧被静默截断。CP 目前只覆盖了短摘要场景。
- M3 修完需要重跑 `off / short / full` 三档基线。历史批次的 gain 偏低时，注入截断是可疑原因之一，但只有重跑后才能把它与检索质量区分开。

### 18.8 P5 实施补充

落地时照 §18.5 的验收逐条写测试，暴露出三处需要在这里说清的口径差异。**本节优先于 18.2 中与之冲突的表述。**

**M12（新增差距）`compress.fail_open` 同样是死配置。** 它只在 `MemoryPolicy` 里声明，全仓没有任何读取点；A17 要求「yaml 每个键都有消费点」时被扫出来。处置：删除 `MemoryPolicy.compress` 字段、`memory.yaml` 的 `compress` 段与 4.4 的示例片段。压缩失败时的行为固定在 `CompressionMiddleware`，不做可配开关 —— 一个没人读的开关比没有开关更容易误导。

**A17 的校准：`trigger_turns` 用结构断言，不是「构造即报错」。** 18.5 原文要求「构造该配置应校验失败」，但 pydantic 默认忽略未知键（配置中心 `_build` 也依赖这一点丢弃旧键）。要让它报错就得给 `EpisodicPolicy` 加 `extra="forbid"`，代价是存量 `config.db` 里残留的 `trigger_turns` 覆盖会把整份配置打成无效、服务起不来。因此改为两条断言：字段不在 `EpisodicPolicy.model_fields` 里，且 `memory.yaml` 不含该键。`write_on=session_end` 仍然用校验失败来钉住 —— 它是枚举收窄，本来就该在加载期报错。

**M5 / M6 的落点与时机。**

- 版本戳写在 `AgentScopeRuntime.snapshot()` 里（`memory/snapshot.py::stamp_snapshot`），不依赖记忆是否启用：`/api/context` 读的是状态里的键，与记忆开关无关。
- 恢复失败计数在 `memory.snapshot` 的进程级计数里，`/health` 的 `memory.restores` 暴露 `{restore_failures, last_error}`；空快照是新会话，不计数。
- `MEMORY_TRIM(reason=snapshot_cap)` 在保存时发出，而保存发生在流结束之后，所以这一帧会在**下一次** `drain()` 时才被读走。这是已知且可接受的归属偏移：它描述的是「本轮开始时看到的历史被裁过」，不是「本轮请求裁剪」。
