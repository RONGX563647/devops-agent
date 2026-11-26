# 架构

## 1. 分层

```
                     ┌────────────────────────────────────────────┐
  HTTP / SSE   →     │ gateway/routes/  端点 + 请求体 + 依赖取用   │
                     │ gateway/turn/    编排·租约·收尾·端口·保活   │
                     │ gateway/叶子     contract / errors / settings│
                     └───────────────┬────────────────────────────┘
                                     │ 只依赖协议
                     ┌───────────────▼──────────────────────────┐
   编排层             │ runtime/runtime.py  AgentRuntime 协议     │
                     │   ├─ runtime/scope_runtime.py  AgentScopeRuntime │
                     │   ├─ runtime/sessions.py SessionRegistry  │
                     │   └─ runtime/{confirm,risk,rules,shell}.py 确认协议 │
                     └───────────────┬──────────────────────────┘
                                     │ 装配点（唯一 import agentscope 的地方）
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
 governance/ 权限          tools/ 工具目录              executor/ 执行面
 input/ 输入管线           secrets/ 密钥保险箱            memory/ 记忆（未接线）
                     ┌──────────────────────────────────────────┐
   观测与评测         │ observability/ 事件流 → SQLite → API      │
                     │ openjudge/ 题库 / 判卷 / 裁判 / 报告      │
                     └──────────────────────────────────────────┘
```

三层硬约束：

1. **网关不 import 框架**：`gateway/` 只认 `AgentRuntime` 协议与普通 dict 事件。
2. **编排与执行分离**：`runtime/` 决定「下一步做什么」，`executor/boundary.py`
   决定「在哪执行」（目前只用 `LocalWorkspace`）。
3. **框架隔离边界 9 个文件**：`executor/boundary.py`、`governance/policy.py`、
   `memory/ledger.py`、`runtime/assembly.py`、`runtime/compression.py`、
   `runtime/profile_note.py`、`runtime/supervisor.py`、`secrets/middleware.py`、
   `tools/catalog.py`。

> `runtime/events.py` 刻意不 import 框架：它用 `model_dump(mode="json")` 探测 +
> duck typing 兜底，所以单测可以用假事件对象验证观测层，不绑定框架类型。

## 2. 一次请求的完整链路（`POST /run/stream`）

| 步骤 | 代码位置 | 说明 |
|---|---|---|
| 1. 解析请求 | `gateway/routes/schemas.RunRequest` | `session_id` / `user_input` / `attachments` / `permission` / `web_search` |
| 2. 档位校验 | `gateway/routes/deps.profile_or_403` | 未开放档 → 403 `permission_not_available`，不静默降级 |
| 3. 会话取用 | `gateway/turn/lease.claim_entry` → `SessionRegistry.acquire` | 命中复用；档位或联网开关变了就重建（从 `session_states` 快照） |
| 4. 并发互斥 | `gateway/turn/lease.claim_entry`（同步） | 同会话第二次请求 → 409 `session_busy`，必须在返回响应前判 |
| 5. 首帧心跳 | `gateway/contract.PING_FRAME` | 模型首 token 需 2–3 秒，先让代理确认流已建立 |
| 6. 结清遗留挂起 | `gateway/turn/orchestrate.settle_abandoned` | 上一轮停在 ASKING 时按**拒绝**结清，追跑上限来自 `settle_rounds` |
| 7. 输入管线 | `gateway/turn/input_prep.prepare_input` → `input.prepare()` | 7 阶段；帧推前端并进 recorder；只有 `prepared.text` 进模型 |
| 8. 运行事件流 | `gateway/turn/ports.RuntimePort.stream` → `AgentScopeRuntime.stream` | 框架事件 → `event_to_dict()` → dict |
| 9. 收尾 | `gateway/turn/orchestrate.finish_or_park` | 决策在 `ending.decide_ending`（纯函数）：有挂起 → `event: parked`；否则 `recorder.finish()` → `event: done` |
| 10. finally | `gateway/turn/lease.SessionLease.close` | 兜底终态 → 快照落库 → 放锁 → 补做被延迟的参数热替换 |

输入阶段失败不阻断对话：`input_prep.prepare_input` 捕获一切异常后返回 `None`，
`payload` 保持原始输入。

## 3. 模块职责

### gateway/

按「变更原因」切职责（ADR-0004），再按层分子包（ADR-0005）。路由模块的走样判据：
**出现 `if` 分支或 `try`，就说明有逻辑放错了层**。

| 位置 | 唯一职责 | 什么时候会改 |
|---|---|---|
| `app.py` / `bootstrap.py` | 装配根 `create_app()`：注入、挂载、启动告警 | 新增路由 / 新增进程级单例 |
| `contract.py` | SSE 帧、传输常量、挂起载荷（**叶子**） | 帧字段变（前端同步） |
| `errors.py` | `ErrorCode` 枚举 + `STATUS_OF` 映射（**叶子**） | 新增错误码（前后端同步） |
| `settings.py` | env 名 + `config/gateway.yaml` → 强类型策略（**叶子**） | 新增部署旋钮 |
| `turn/orchestrate.py` | 一轮编排：`run_turn` / `resume_turn` / `finish_or_park` | agent 流程变 |
| `turn/lease.py` | 会话租约：`claim_entry` / `hold` / `open_lease` / `close` | 并发模型变 |
| `turn/ending.py` | 收尾决策纯函数 + `RunLimits` | 收尾 UX 变 |
| `turn/ports.py` | `RuntimePort`（Null Object）+ `PendingStore` | 运行时能力面变 / 挂起态存储变 |
| `turn/input_prep.py` | 输入准备（失败即退化） | 解析管线接口变 |
| `turn/sse.py` | 保活：队列 + 心跳泵（唯一消费者是编排） | 保活机制变 |
| `routes/stream.py` · `confirm.py` · `sessions.py` · `health.py` | HTTP 契约映射 | 端点契约变 |
| `routes/deps.py` · `schemas.py` | 依赖取用 / 请求体 | 注入点或请求字段变 |

`.importlinter` 强制四条：叶子不得依赖 `app`/`bootstrap`/`routes`/`turn`；`turn` 不得依赖
`routes`/`app`；`routes` 不得依赖 `app`；`gateway` 不得直接 import `agentscope`。

### runtime/

| 文件 | 职责 | 关键事实 |
|---|---|---|
| `assembly.py` | Agent 装配 | `build_agent` / `build_agentscope_runtime` / `session_runtime_factory` |
| `chat_model.py` | 模型与上下文配置 | `build_chat_model` / `build_context_config` / 搜索参数 |
| `scope_runtime.py` | 框架运行时适配 | `AgentScopeRuntime`；`PROFILE_KEY` 写进 `middle_context` |
| `sessions.py` | 会话隔离 | `max_entries=16`；`acquire/peek/switch/save/drop/flush_pending/broadcast_model_defaults` |
| `confirm.py` | 确认协议**门面** | 只做导出，实现拆在下面三个模块；**不 import 框架** |
| `risk.py` | 风险分级 | `read/exec/write/danger`；正则来自 `policy.yaml` 的 `confirm.risk` |
| `rules.py` | 规则沉淀 | 作用域、`prefix` 深度、复合命令不沉淀；原料来自 `confirm.rules` |
| `shell.py` | shell 词法 | 纯函数（拆段 / 剥包装层），不读配置，可零配置单测 |
| `profile_note.py` | 档位说明 + 联网说明下发位置 | 追加在消息末尾，保前缀缓存（21% → 96%）；`set_profile()` / `set_web_search()` 支持运行中切换 |
| `compression.py` | 压缩中间件 | 钩子无事件通道，由运行时 `drain()` 补帧 |
| `events.py` | 事件转译 | 输出稳定 key 契约；`SPAN_EVENT_TYPES` 决定哪些落 span |
| `supervisor.py` | `TeamPipeline` / `GoalPipeline` 装配 | **仅单测引用**，未接网 |
| `runtime.py` | `AgentRuntime` 协议 + `EchoRuntime` | 无凭据时网关走 Echo |

### 为什么 `_state_for` 用 `profile.key` 而不是 `mode`

`workspace` 与 `execute` 的 `mode` 都是 `accept_edits`。只比 `mode` 会得出
「没换档」的结论 → `execute` 档的 `command_allowlist` 从不装配，旧档攒下的
`allow_rules` 还留着。所以快照里记的是 `permission_profile` 这个档位 key。

`Agent` 构造时 `PermissionEngine(self.state.permission_context)` 持有的是**对象引用**，
因此会话中切档必须逐字段写回（`apply_profile`），不能整体替换对象 —— 换对象后引擎
仍按旧上下文判定，界面却显示新档。

### tools/

`catalog.py` 是工具名的唯一事实源：

```python
READONLY_TOOL_NAMES = ("bash", "read", "glob", "grep")
WRITE_TOOL_NAMES = ("write", "edit")
```

`build_toolkit_for(names, workdir, middlewares)` 是档位切换唯一入口，未知工具名抛
`ValueError`。每个工具独占一个 `LocalBackend(workdir=cwd)`：不传 backend 时框架自建
`workdir=None` 实例，相对路径回落到**服务进程 cwd**。

shell 统一用 `Bash`：`Bash` 会按命令判定只读（`ls`/`cat`/`git status` 直接放行），
`PowerShell` 每条命令都返回 ASK；服务端没有确认通道时 ASK 会退化成静默结束。
`Bash` 在 Windows 走 `cmd /c`，POSIX 走 `/bin/sh -c`。

`Grep` 依赖外部 `rg`（`LocalBackend.exec_shell` 直接 spawn），缺失时表现为 `exit 127`；
`tools/preflight.py` 用 `shutil.which` 在启动时探测，经 `/health` 与 `/api/tools/preflight` 暴露。

### input/

`pipeline.py` 的 `prepare()` 串起 7 阶段：
`normalize → detect → expand → attach → redact → budget → compose`。

| 常量 | 值 |
|---|---|
| `MAX_TOTAL_CHARS` | 24 000 |
| `MIN_MATERIAL_CHARS` | 200 |
| `MAX_FILE_BYTES` | 256 KiB |
| `MAX_REFERENCES` | 5 |
| `FETCH_TIMEOUT_SECONDS` | 5.0 |
| `FETCH_MAX_BYTES` | 512 KiB |
| `CHARS_PER_TOKEN` | 4 |

`compose` 不只拼材料，还拼「**本轮未能取到的材料**」清单（`_render_misses()`）。
引用抓取失败若只写进阶段详情（前端可见、模型不可见），模型不知道自己缺了什么，
会自己 `curl` 同一地址、换 shell、换 python 脚本重试 —— 实测一次会话烧掉 21 步工具调用。
渲染出的清单带停止规则：同一目标失败两次就停、基于已有材料回答并说明缺什么。
两个细节：`远程抓取未开启` 不进清单（那只是服务端没代抓，不是内容不可得，写进去等于劝退
模型自己取）；清单本身也要过脱敏（失败说明带原始 URL，URL 里可能含 token）。

`attachments.py`：`MAX_UPLOAD_BYTES = 10MB`、`MAX_ATTACH_CHARS = 40_000`、
`MAX_ATTACHMENTS = 8`、`RETENTION_DAYS = 7`。

### 联网检索（工具化）

`DEVOPS_AGENT_SEARCH_MODE` 的三种取值由 `search_mode.py` 统一解读（零依赖：
模型请求体、工具注册、提示词三处都要读它，分散读环境变量会出现「请求里没检索、
提示词却说已联网」）。

| 模式 | 请求体 | 工具 |
|---|---|---|
| `tool`（默认） | 不带任何检索字段 | 注册 `web_search` |
| `provider` | `enable_search`（+ `forced_search`） | 不注册 |
| `off` | 不带 | 不注册 |

装配链路：

```
web_search: bool|None（请求）
  → assembly 解析：请求 → profile.net → DEVOPS_AGENT_ENABLE_SEARCH
  → _build_search_tool：tool 模式 + 后端可用才建 WebSearchTool
  → build_toolkit_for(profile.tools + ("web_search",), extra={...})
  → 模型调用 → SearchBackend（HttpBackend / ProviderBackend）→ ToolChunk
```

`WebSearchTool` 的实例由运行时持有（`_search_tool`），因为预算要按 run `reset()`；
每轮新建会让「第 6 次调用」永远不触发。

`build_toolkit_for(..., extra=)` 是给「已构造好的实例」留的口子：`web_search`
不能每次装配都 new，否则预算对象与 toolkit 里的不是同一个。

### 轨迹里的检索

`web_search` 走正常 Toolkit 调用，所以自动进 Agent 的 `state.context` →
`harness._absorb_block` → `trace.tool_calls`（`name` / `args.query` / `state` /
`result_excerpt`）。工具输出首行是 `backend=… hits=… elapsed=…`，因此 200 字符的
摘要也保留了「这次检索发生了什么」。

`openjudge/metrics.search_stats` 从这些记录投影出 `summary.searches`
（`calls` / `empty` / `failed` / `budget_exhausted` / `cases` / `queries`），
判分与成本核算不必再翻每条 tool_call。它按首行契约做字符串判定，
两侧改格式必须一起改 —— `test_web_search_tool.py` 覆盖。

`provider` 模式的联网说明仍由 `ProfileNoteMiddleware` 追加在消息末尾（`tool`
模式不追加：工具描述已经说明何时调用，多一条尾部消息只会让前缀缓存每轮失效）。
说明写的是「检索结果**若存在**」：应用侧拿不到供应商是否真的检索了，写死「已附带」
会诱发模型自己去抓一遍。

### secrets/

`vault.py`（Fernet）、`capture.py`（识别凭据）、`middleware.py`（工具边界解密）、
`api.py`（写/取/列/删）。占位符 `{{secret:NAME}}` 的语法在
`vault.PLACEHOLDER_PATTERN` 与 `pipeline._PLACEHOLDER_RE` 各写一份，
由一条对账测试钉住，防止两侧漂移。

### executor/ 与 memory/

`boundary.py`（29 行）只有两个工厂：`build_local_workspace(workdir, skill_paths=None)`
与 `build_docker_workspace(base_image="python:3.11-slim", ...)`；后者未被运行时使用。
`memory/ledger.py` 只有 Mem0 工厂，同样未接线。

## 4. 已知的结构性问题

| 现象 | 位置 | 影响 |
|---|---|---|
| 巨型模块 | `observability/repository/`（已按表拆包，`db.py` 208 行仍偏大）、`openjudge/`（单文件普遍 100–160 行，`api*.py` 偏大） | 单文件承担多职责，改动半径大；`input/pipeline/` 与 `runtime/confirm` 已按 ADR-0004 拆完 |
| 只装配未接线 | `supervisor.py`、`memory/ledger.py`、`DockerWorkspace` | 架构图上有、运行链路上没有，容易误判能力 |
| AGENTS.md 清单少一项 | §3.1 列 8 个，实测 9 个 | 新增装配点容易漏更新 |
