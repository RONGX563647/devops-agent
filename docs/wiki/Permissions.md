# 权限、确认与续跑

## 1. 为什么是「档位」而不是直接暴露 `PermissionMode`

`PermissionMode` 描述判定策略（`explore` / `accept_edits` / `dont_ask` / `bypass`），
名字是给实现者看的；用户要选的是「能让它干到哪一步」。而且**只切 mode 而工具集不动**，
会出现「切了档什么都没变」——`accept_edits` 的卖点是工作区内编辑自动放行，
但工具集里压根没有 `Write`/`Edit`。

所以一个档位 = **mode + 工具集 + 越界兜底动作**，三者一起换
（[governance/profiles.py](../../src/devops_agent/governance/profiles.py#L50-L99)）。

**产品定位（写进模块 docstring）**：这是防误操作的 UX 层，**不是安全隔离**。
它防的是「模型手滑删错目录」，防不住恶意用户。真正的隔离靠容器、只读挂载、
网络策略、非 root 运行。

## 2. 五档定义

| key | label | mode | tools | fallback | allowlist |
|---|---|---|---|---|---|
| `readonly` | 只读 | `explore` | bash/read/glob/grep | `deny` | ✗ |
| `workspace` | 工作区可写 | `accept_edits` | 只读 + write/edit | `ask` | ✗ |
| `execute` | 可执行 | `accept_edits` | 只读 + write/edit | `ask` | ✓ |
| `auto` | 无人值守 | `dont_ask` | 只读 + write/edit | `deny` | ✗ |
| `unrestricted` | 全放开 | `bypass` | 只读 + write/edit | `hard_deny` | ✗ |

- `DEFAULT_AVAILABLE = ("readonly", "workspace", "execute", "auto")` ——
  **默认不含 `unrestricted`**，要显式配置才开放。
- 默认档 `workspace`（`config/policy.yaml` 的 `permission.default_profile`）。
- 覆盖顺序：环境变量（`DEVOPS_AGENT_PERMISSION_PROFILES` /
  `DEVOPS_AGENT_DEFAULT_PROFILE`）> `policy.yaml` > 内置默认。
  配置里写了不认识的名字 → 跳过该名字，不让服务起不来。
- 请求里的档位未开放或不存在 → `UnknownProfile` → **403 `permission_not_available`**。
  **不做静默降级**：客户端以为在放开档、实际被降成只读，比直接报错更难排查。

### 为什么 `workspace` 一定要带写工具

`workspace` / `execute` / `auto` 的 `note` 都承诺「可在工作区内建/删/改文件」，
那就必须真挂 `Write`/`Edit`。只挂只读四件套时，`workspace` 与 `readonly` 的
唯一差别只剩「越界问不问」。

`execute` 同理：白名单里的构建命令如果改不了产物，等于「只能跑不能干」。

## 3. `config/policy.yaml` 里实际有什么

```yaml
permission:
  mode: dont_ask                 # 仅无档位上下文时的回落
  default_profile: workspace
  available: [readonly, workspace, execute, auto]
  command_allowlist: [pytest, python -m pytest, python -m ruff, python -m mypy,
                      npm run, npm test, npm ci, npm install, npx, pnpm, yarn,
                      make, cargo, go test, go build]
  working_directories: []
  allow_rules: []
  ask_rules: []
  deny_rules:                    # builtin deny，对所有档位无条件生效
    - {tool_name: Bash,  rule_content: .git/, behavior: deny}
    - {tool_name: Bash,  rule_content: .env,  behavior: deny}
    - {tool_name: Write, rule_content: .git/, behavior: deny}
    - {tool_name: Edit,  rule_content: .git/, behavior: deny}
```

### builtin deny 为什么要对所有档位生效

旧实现只在 `hard_deny` 档（`unrestricted`）装配 deny，理由是「其余档位越界会 ASK」。
但 `auto` 档的 `dont_ask` 对 `.env` 这种**工作区内**路径直接 allow —— ASK 根本不会触发，
等于无人值守档能读密钥文件。框架层 deny 优先于 allow/ask，所以无条件并入才是安全默认。

### `workdir` 必须登记进 `working_directories`

框架判定「这个路径在不在工作区内」用的是 `os.getcwd() + context.working_directories`。
部署时真正的工作区常常不是进程 cwd（`DEVOPS_AGENT_WORKDIR` 指到别处）。
不登记的话，`accept_edits` 档写文件会被判成「工作区外」→ 每次都要确认，
用户看到的是「明明选了工作区可写，写个文件还要问」。

## 4. 工具集与执行面

工具名唯一事实源是 [tools/catalog.py](../../src/devops_agent/tools/catalog.py)：

```python
READONLY_TOOL_NAMES = ("bash", "read", "glob", "grep")
WRITE_TOOL_NAMES = ("write", "edit")
```

- `build_toolkit_for(names, workdir, middlewares)` 是档位切换唯一入口。
  未知工具名抛 `ValueError`：静默跳过会把「放开档少了写工具」表现成
  「权限切了但模型还是不写文件」。
- 每个工具**独占一个 `LocalBackend(workdir=cwd)`**。不给 backend 时框架自建
  `workdir=None` 的实例，相对路径回落到服务进程 cwd，表现为「Grep/Glob 不传 path 就搜错地方」。
- shell 统一用内置 `Bash`：它按命令判定只读（`ls`/`cat`/`git status` 直接放行）；
  `PowerShell` **每条命令都返回 ASK**，服务端没有确认通道时 ASK 会退化成
  「发个确认事件后静默结束」。`Bash` 在 Windows 走 `cmd /c`，POSIX 走 `/bin/sh -c`。
- `Grep` 依赖外部 `rg`，缺失时 `exit 127`；`tools/preflight.py` 用 `shutil.which` 探测。

## 5. 确认挂起与续跑

### 为什么需要「挂起」而不是同步阻塞

AgentScope 的 `Bash` 在 ASK 时不会自己等人 —— 它把 tool_call 置为 `ASKING` 停在
`AgentState` 里，然后需要一个 `UserConfirmResultEvent` 才能继续。HTTP 侧不能一直挂着
连接等用户点卡片，所以 SSE 流收尾发 `event: parked`，把挂起态落库。

### 状态机

```
run → 权限引擎判 ASK → pending_calls() 非空
      ├─ asks ≤ ASK_LIMIT(3) → event: parked（落库，TTL 600s）
      │                       前端 POST /run/{run_id}/confirm
      │                       → 服务端从挂起状态取权威 ToolCallBlock → resume()
      └─ asks > ASK_LIMIT    → 自动全部拒绝（让模型换思路，别再弹卡片）
```

关键实现点：

- **不发 `done`**：前端据此把消息标成「等待确认」而不是「已完成」。
- **TTL 600 秒**（`PARK_TTL_SECONDS`），过期后 confirm 返回 409 `expired`。
- `GET /run/{run_id}/pending` 必须带 `session_id`，不匹配返回 403 `session_mismatch`
  —— 否则知道 run_id 就能读到别人的命令。
- confirm 用条件更新（`parked` 从真改假）保证同一次挂起**只执行一次**；
  已解决的返回 409 `already_resolved`。

### `_settle_abandoned`：上一轮没作答怎么办

用户弹卡片后直接关页面，前端只 abort 了请求，**服务端仍停在 ASKING**。
这时再发新消息，框架会抛
`Agent is waiting for N tool calls ... but received no event` ——
用户看到「对话运行失败」。

处理：新 run 开跑前，把遗留挂起**按拒绝结清**（`SETTLE_ROUNDS=2` 轮上限）。
结清方式固定是拒绝：没被批准的调用一条都不执行，发条消息不会顺手放行；
模型被拒后换方案的追跑输出直接丢弃（它回答的是上一个问题）。

### 三条硬约束（`runtime/confirm.py` 模块 docstring）

1. **客户端永远不回传命令原文**。`UserConfirmResultEvent` 需要完整 `ToolCallBlock`，
   让前端带回来就等于允许客户端改写将要执行的命令。所以服务端保留权威副本，
   客户端只回 `tool_call_id + confirmed + scope`。
2. 一个 run 最多打扰 **3 次**（`ASK_LIMIT`）。
3. 高危命令（`_DANGER` 正则：`rm -rf` / `rm -fr` / fork bomb `:(){` / `mkfs` /
   `dd if=` / `shutdown` / `reboot` / `chmod -R 0?777` / `> /dev/sd`）
   强制降级为 `scope=once`。一秒钟的勾选不该变成永久放行 `rm -rf`。

风险分级 `classify()` 返回四档：`read` / `exec` / `write` / `danger`；
`Write`/`Edit` 恒为 `write`；非 `Bash` 工具恒为 `read`。

## 6. `prefix` 沉淀规则

`scope` 三档：`once`（不沉淀）/ `exact`（完整命令，最窄）/ `prefix`（前缀）。
非法 scope 退回 `once` —— 宁可多问一次，也不要因为一个错拼把整类命令永久放行。

`prefix` 必须落到**子命令**这一层，规则在 `_prefix_for()`：

| 情况 | 结果 | 原因 |
|---|---|---|
| `git commit -m x` | `git commit:*` | 只取首词会得到 `git:*`，等于放行整个 git |
| `cd /repo && rm -rf build` | 不沉淀 | 复合命令应逐段判定 |
| `timeout 30 npm test` | `npm test:*` | 先剥包装层，否则得到 `timeout:*`（放行所有被 timeout 包住的命令） |
| `nohup python -m pytest -q` | `python -m pytest:*` | 只剥包装器自己的开关；`-m` 属于被包装命令 |
| `rm` / `bash` / `sh` / `cmd` / `pwsh` | 不沉淀（`_NEVER_PERSIST` / `_NEVER_PERSIST_INTERPRETERS`） | `rm:*` 会连 `rm -rf /` 一起放过 |
| `python -c "..."` | 不沉淀 | `-c` 是任意代码 |

其它细节：

- 复合分隔符 `_SEPARATORS = ("&&", "||", ";", "|", "\n")`；单引号内是字面量不算，
  双引号内的 `$(...)` 与反引号算（`"$(rm -rf /)"` 会被执行）。
- 包装层 `_WRAPPERS = {"timeout","time","nice","nohup","stdbuf","env","command","exec"}`；
  `timeout` / `nice` / `env` 各有专门剥离函数（跳过 `-s SIG`、`-n 5`、`VAR=1`）。
- 子命令程序集 `_SUBCOMMAND_PROGRAMS`（git/gh/npm/pnpm/yarn/npx/pip/uv/poetry/cargo/go/
  docker/kubectl/helm/terraform/make/dotnet/mvn/gradle/apt/apt-get/brew/systemctl/
  journalctl/az/aws/gcloud/conda/alembic）。
- 引号不闭合（`shlex.split` 抛 `ValueError`）→ 不沉淀，也不生成乱匹配的规则。

## 7. 会话隔离（`runtime/sessions.py`）

每条独立理由都足以说明为什么必须隔离：

1. **规则串味**：点「本会话允许」会往 `AgentState.permission_context.allow_rules` 加规则；
   单例 Agent 下 A 会话点一下，B 也放行了。
2. **挂起互斥**：两个会话同时 ASKING，`get_awaiting_tool_calls()` 返回一锅粥，
   确认时分不清该答哪个。
3. **历史串味**：共享 `Agent.state.context`。

`SessionRegistry(factory, db_path=None, max_entries=16)` 的对外方法：

| 方法 | 语义 |
|---|---|
| `acquire(session_id, profile, web_search)` | 命中复用；档位或联网开关变了先 `save()` 再重建；未命中从 `session_states` 快照重建 |
| `peek(session_id)` | **只取不重建**，供运行中切档端点用（`acquire` 会按请求档位重建，正在跑的流会被换掉） |
| `switch(...)` | 有热替换能力 → 原地改并返回 `applied=True`；否则重建并如实返回 `False` |
| `apply_model_params(...)` | 会话级参数覆盖；不支持热替换返回 `False`（调用方要说「下一轮生效」） |
| `broadcast_model_defaults()` | 部署默认变更；返回 `{"applied": n, "deferred": m}` |
| `flush_pending(session_id)` | run 结束后补做被延迟的热替换 |
| `save` / `drop` | 快照落库 / 只丢内存态（不删库） |

**回收不等于丢失**：LRU 只淘汰内存实例，DB 快照不动；confirm 缓存未命中就从库重建，
因此不需要「挂起中的会话不许回收」这类豁免，也不依赖粘性会话。
`_evict()` 只淘汰**未被锁住**的会话；全都在跑时宁可超限也不打断。

同会话并发：SSE 整段持有 `entry.lock`，第二个请求返回 **409 `session_busy`**。

### 切档为什么必须逐字段写回

`Agent` 构造时执行 `self._engine = PermissionEngine(self.state.permission_context)`，
引擎持有的是**对象引用**。换对象后引擎仍按旧上下文判定，界面却显示新档。
所以 `apply_profile()` 逐字段写 `mode` / `working_directories` / `allow_rules` /
`deny_rules` / `ask_rules`，并重建 toolkit（重挂密钥中间件）。

旧档攒下的 `allow_rules` 随这次赋值丢弃，与「切档即换信任级别」一致。

### 为什么快照里存档位 key 而不是 mode

`workspace` 与 `execute` 的 mode 都是 `accept_edits`。只比 mode 会得出「没换档」，
导致 `execute` 档的 `command_allowlist` 从不装配，旧档 `allow_rules` 也留了下来。
所以 `AgentState.middle_context["permission_profile"] = profile.key`，老快照没有这个键
视为档位不同、重建一次（只换 `permission_context`，对话历史不动）。
