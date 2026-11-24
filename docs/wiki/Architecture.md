# 架构

## 1. 分层

```
                     ┌──────────────────────────────────────────┐
  HTTP / SSE   →     │ main.py  网关：会话、权限、SSE、挂起续跑   │
                     └───────────────┬──────────────────────────┘
                                     │ 只依赖协议
                     ┌───────────────▼──────────────────────────┐
   编排层             │ runtime/runtime.py  AgentRuntime 协议     │
                     │   ├─ runtime/agent.py   AgentScopeRuntime │
                     │   ├─ runtime/sessions.py SessionRegistry  │
                     │   └─ runtime/confirm.py  确认协议数据结构  │
                     └───────────────┬──────────────────────────┘
                                     │ 装配点（唯一 import agentscope 的地方）
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
 governance/ 权限          tools/ 工具目录              executor/ 执行面
 input/ 输入管线           secrets/ 密钥保险箱            memory/ 记忆（未接线）
                     ┌──────────────────────────────────────────┐
   观测与评测         │ observability/ 事件流 → SQLite → API      │
                     │ eval/ 题库 / 判卷 / 裁判 / 报告 / lint     │
                     └──────────────────────────────────────────┘
```

三层硬约束：

1. **网关不 import 框架**：`main.py` 只认 `AgentRuntime` 协议与普通 dict 事件。
2. **编排与执行分离**：`runtime/` 决定「下一步做什么」，`executor/boundary.py`
   决定「在哪执行」（目前只用 `LocalWorkspace`）。
3. **框架隔离边界 9 个文件**：`executor/boundary.py`、`governance/policy.py`、
   `memory/ledger.py`、`runtime/agent.py`、`runtime/compression.py`、
   `runtime/profile_note.py`、`runtime/supervisor.py`、`secrets/middleware.py`、
   `tools/catalog.py`。

> `runtime/events.py` 刻意不 import 框架：它用 `model_dump(mode="json")` 探测 +
> duck typing 兜底，所以单测可以用假事件对象验证观测层，不绑定框架类型。

## 2. 一次请求的完整链路（`POST /run/stream`）

| 步骤 | 代码位置 | 说明 |
|---|---|---|
| 1. 解析请求 | `main.RunRequest` | `session_id` / `user_input` / `attachments` / `permission` / `web_search` |
| 2. 档位校验 | `main._profile_or_403` | 未开放档 → 403 `permission_not_available`，不静默降级 |
| 3. 会话取用 | `SessionRegistry.acquire` | 命中复用；档位或联网开关变了就重建（从 `session_states` 快照） |
| 4. 并发互斥 | `entry.lock` | 同会话第二次请求 → 409 `session_busy` |
| 5. 首帧心跳 | `yield ": ping\n\n"` | 模型首 token 需 2–3 秒，先让代理确认流已建立 |
| 6. 结清遗留挂起 | `_settle_abandoned` | 上一轮停在 ASKING 时按**拒绝**结清，最多追跑 `SETTLE_ROUNDS=2` 轮 |
| 7. 输入管线 | `input.prepare()` | 7 阶段；帧推前端并进 recorder；只有 `prepared.text` 进模型 |
| 8. 运行事件流 | `AgentScopeRuntime.stream` | 框架事件 → `event_to_dict()` → dict |
| 9. 收尾 | `_stream_entry` | 有挂起 → `event: parked`；否则 `recorder.finish()` → `event: done` |
| 10. finally | `registry.save` / `flush_pending` | 快照落库、释放锁、补做被延迟的参数热替换 |

输入阶段失败不阻断对话：`_prepare_input` 捕获一切异常后返回 `None`，
`payload` 保持原始输入。

## 3. 模块职责

### runtime/

| 文件 | 职责 | 关键事实 |
|---|---|---|
| `agent.py` | 唯一主装配点 | `build_agent` / `build_chat_model` / `build_agentscope_runtime`；`PROFILE_KEY` 写进 `middle_context` |
| `sessions.py` | 会话隔离 | `max_entries=16`；`acquire/peek/switch/save/drop/flush_pending/broadcast_model_defaults` |
| `confirm.py` | 确认协议数据结构 | 风险分级、`RULE_SCOPES`、`prefix` 沉淀规则；**不 import 框架** |
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

### 模型侧联网搜索

`agent.build_chat_model(enable_search=...)` 组装 `extra_body`，逻辑在 `_search_extra_body()`：

- `enable_search` 入参显式给出时优先于环境变量 —— 前端逐请求的「联网」开关走这条路；
  传 `None` 才回落到 `DEVOPS_AGENT_ENABLE_SEARCH`。
- `DEVOPS_AGENT_SEARCH_MODE=forced`（默认）额外下发
  `search_options = {"forced_search": true, "search_strategy": "standard"}`；
  `auto` 只发 `enable_search`，由模型裁量（实测多数情况不检索）。

只发 `enable_search` 时它只是「授权」，实测同一请求体的 `prompt_tokens` 与不带时一致
（3150 vs 3150），补上 `forced_search` 后变成 4436 —— 说明默认不开强制检索时，
前端那个开关按下去没有实际效果。

联网那一轮的说明文字由 `ProfileNoteMiddleware` 追加在消息末尾（`set_web_search()`），
写的是「检索结果**若存在**」：应用侧拿不到供应商是否真的检索了（框架不透出
`search_info`，事件流里没有检索 span），写死「已附带」会诱发模型自己去抓一遍。

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
| 巨型模块 | `observability/repository.py`（1079 行）、`input/pipeline.py`（807）、`eval/report.py`（780） | 单文件承担多职责，改动半径大 |
| 只装配未接线 | `supervisor.py`、`memory/ledger.py`、`DockerWorkspace` | 架构图上有、运行链路上没有，容易误判能力 |
| AGENTS.md 清单少一项 | §3.1 列 8 个，实测 9 个 | 新增装配点容易漏更新 |
