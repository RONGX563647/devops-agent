# 权限、确认与续跑

## 1. 为什么是「档位」而不是直接暴露 `PermissionMode`

`PermissionMode` 描述判定策略（`explore` / `accept_edits` / `dont_ask` / `bypass`），
名字是给实现者看的；用户要选的是「能让它干到哪一步」。而且**只切 mode 而工具集不动**，
会出现「切了档什么都没变」——`accept_edits` 的卖点是工作区内编辑自动放行，
但工具集里压根没有 `Write`/`Edit`。

所以一个档位 = **mode + 工具集 + 越界兜底动作**，三者一起换
（[governance/model.py](../../src/devops_agent/governance/model.py)）。

`governance/` 按职责分四层，依赖单向：`model`（纯数据）→ `presets`（env 解析）→
`compile`（唯一碰框架的模块）→ `decide`（离线判定）。`model` / `presets` 由
`.importlinter` 强制零框架依赖 —— 旧实现里 `profiles.py` 经 `tools.catalog` 间接
拉进 agentscope，那条契约形同虚设。

**产品定位（写进模块 docstring）**：这是防误操作的 UX 层，**不是安全隔离**。
它防的是「模型手滑删错目录」，防不住恶意用户。真正的隔离靠容器、只读挂载、
网络策略、非 root 运行。

## 2. 五档定义

| key | label | mode | tools | fallback | scope | 写工具 |
|---|---|---|---|---|---|---|
| `readonly` | 只读 | `explore` | bash/read/glob/grep | `deny` | readonly | ✗ |
| `workspace` | 工作区可写 | `accept_edits` | 只读 + write/edit | `ask` | workspace | ✓ |
| `execute` | 可执行 | `accept_edits` | 只读 + write/edit | `ask` | workspace | ✓ |
| `auto` | 无人值守 | `dont_ask` | 只读 + write/edit | `deny` | workspace | ✓ |
| `unrestricted` | 全放开 | `bypass` | 只读 + write/edit | `hard_deny` | container | ✓ |

- `DEFAULT_AVAILABLE = ("readonly", "workspace", "execute", "auto")` ——
  **默认不含 `unrestricted`**，要显式配置才开放。
- 默认档 `workspace`（`governance/model.py` 的 `DEFAULT_PROFILE_KEY`）。
- 覆盖顺序：环境变量 `DEVOPS_AGENT_PERMISSION_PROFILES` /
  `DEVOPS_AGENT_DEFAULT_PROFILE` > 内置默认。
  配置里写了不认识的名字 → 跳过该名字，不让服务起不来。
- 默认档被配成未开放档 → 按 `SAFETY_ORDER` 回落**最严可用档**。旧实现回落
  `sorted(allowed)[0]`，字典序首个恰好是 `auto`（无人值守），等于配置写错反而把
  用户推向最不想有人盯的档位。
- `write` 由 `PermissionProfile.can_write` 求工具集交集得出，不靠调用方猜工具名。
- 请求里的档位未开放或不存在 → `UnknownProfile` → **403 `permission_not_available`**。
  **不做静默降级**：客户端以为在放开档、实际被降成只读，比直接报错更难排查。

### 为什么 `workspace` 一定要带写工具

`workspace` / `execute` / `auto` 的 `note` 都承诺「可在工作区内建/删/改文件」，
那就必须真挂 `Write`/`Edit`。只挂只读四件套时，`workspace` 与 `readonly` 的
唯一差别只剩「越界问不问」。

`execute` 同理：白名单里的构建命令如果改不了产物，等于「只能跑不能干」。

## 3. 权限不读 yaml：`governance/model.py` 是唯一事实来源

```python
PROFILES  # 五档：mode + tools + fallback + note + scope
DEFAULT_AVAILABLE  # 默认可用集（不含 unrestricted）
ALLOW_PREFIXES  # execute 档的命令白名单（"pytest:*" 形式）
BUILTIN_DENY  # builtin deny 规则（Bash / Write / Edit）
```

`config/policy.yaml` 现在只剩 `confirm:`（确认协议的域知识）。权限为什么搬走：

- **同一事实被写过两遍**：`available` 在 yaml 与代码里逐字相同，`default_profile`
  在 yaml 与 `default_profile()` 的回落里各有一份，`command_allowlist` 与档位的
  `allowlist` 布尔分散两处。改一处忘另一处，前端列出的档位就不是真正开放的档位。
- **yaml 里的规则会静默失效**：`.git/`、`.env` 两条 deny 在 Write/Edit 上从未命中
  （框架用 fnmatch 匹配 `file_path`，`.git/` 这种 pattern 匹配不到任何真实路径），
  可它们看起来一直在生效 —— 真正的保护来自框架内建的 safety-ask。
- 规则现在是代码契约：有判定矩阵、有跨平台语义，由 mypy 与单测覆盖，而不是只在
  加载期拼错才报错。

附加工作目录改由 `DEVOPS_AGENT_WORKDIRS`（`os.pathsep` 分隔）声明。

### builtin deny 为什么要对所有档位生效

旧实现只在 `hard_deny` 档（`unrestricted`）装配 deny，理由是「其余档位越界会 ASK」。
但 `auto` 档的 `dont_ask` 对 `.env` 这种**工作区内**路径直接 allow —— ASK 根本不会触发，
等于无人值守档能读密钥文件。框架层 deny 优先于 allow/ask，所以无条件并入才是安全默认。

### `workdir` 必须登记进 `working_directories`

框架判定「这个路径在不在工作区内」用的是 `os.getcwd() + context.working_directories`。
部署时真正的工作区常常不是进程 cwd（`DEVOPS_AGENT_WORKDIR` 指到别处）。
不登记的话，`accept_edits` 档写文件会被判成「工作区外」→ 每次都要确认，
用户看到的是「明明选了工作区可写，写个文件还要问」。

### 敏感路径的三道防线

只靠声明式规则不够，这是实测结论（[SPEC-0010](../specs/0010-permission-single-source.md) §1.2）：
没有任何单条 glob 能覆盖全部拼写 —— `.git/config` 的相对形式所有候选都漏、`.ENV`
因为大小写全漏、`fnmatch` 的 `*` 还跨分隔符。

| 层 | 位置 | 职责 | 命中后 |
|---|---|---|---|
| ① 声明式 | `governance/model.py` 生成 | 常见写法在引擎层就拒，省一次工具调用 | `deny` |
| ② 中间件 | `tools/guard.py` | `realpath` + 大小写 + 分隔符归一后按路径段判 | `ToolResultState.DENIED`，**不执行工具** |
| ③ Bash 分词 | 同上，`shlex` | 逐个 token 判敏感路径 | 同上 |

- `SENSITIVE_FILES` / `SENSITIVE_DIRS` 在 [governance/paths.py](../../src/devops_agent/governance/paths.py)，
  是框架 `DEFAULT_DANGEROUS_FILES` 的**超集**（`test_sensitive_lists_cover_framework_defaults`
  负责对账：框架加了新名字，那条测试会红）。
- 守卫挂在 `build_toolkit_for` 里，**默认生效**：靠调用方记得传等于迟早漏挂一条路。
- `Grep` 不整体拒绝，只注入 `--glob !<name>` 排除敏感文件 —— 拒搜索太粗，
  「结果里带出 `.env` 内容」才是要防的事。
- `readonly` 档额外把「工作区内」当硬边界（`scope=readonly`）。**Bash 侧不做这个边界**：
  `/bin/sh`、`/usr/bin/env` 这类绝对路径是正常执行的一部分，按根边界判会把只读档打废；
  真正的边界靠容器/只读挂载，与「这是防误操作 UX 层」的定位一致。

### bypass 档为什么要把 danger 命令写进 deny

`BYPASS` 按契约跳过所有 tool safety-ask（`rm -rf /`、写 `~/.bashrc`、命令注入模式），
只剩 deny/ask 规则。所以 `unrestricted` 档必须自己 deny：`DANGER_COMMANDS` 与
`INTERPRETER_FLAGS`（`python -c` / `bash -c` / `node -e` …）都带 `scope="container"`，
只在放开档装配 —— 别档保持框架的 ASK，否则日常 `python -c` 会直接变死。

## 4. 工具集与执行面

工具名唯一事实源是 [tools/names.py](../../src/devops_agent/tools/names.py)（零依赖，
`tools/catalog.py` 原样 re-export）：

```python
READONLY_TOOL_NAMES = ("bash", "read", "glob", "grep")
WRITE_TOOL_NAMES = ("write", "edit")
```

- `build_toolkit_for(names, workdir, middlewares, *, scope)` 是档位切换唯一入口。
  未知工具名抛 `ValueError`：静默跳过会把「放开档少了写工具」表现成
  「权限切了但模型还是不写文件」。`scope` 只影响敏感路径守卫是否把工作区当硬边界。
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
      ├─ asks ≤ ask_limit(3) → event: parked（落库，TTL 600s）
      │                       前端 POST /run/{run_id}/confirm
      │                       → 服务端从挂起状态取权威 ToolCallBlock → resume()
      └─ asks > ask_limit    → 自动全部拒绝（让模型换思路，别再弹卡片）
```

关键实现点：

- **不发 `done`**：前端据此把消息标成「等待确认」而不是「已完成」。
- **限额与 TTL 是配置**（`config/gateway.yaml` 的 `gateway.stream`）：`ask_limit` /
  `park_ttl_seconds` / `settle_rounds`。本文里的 3 / 600 / 2 就是当前默认值。
  决策是纯函数 `gateway/ending.decide_ending`，副作用在 `gateway/run.finish_or_park`。
- **TTL 过期**后 confirm 返回 409 `expired`。
- `GET /run/{run_id}/pending` 必须带 `session_id`，不匹配返回 403 `session_mismatch`
  —— 否则知道 run_id 就能读到别人的命令。
- confirm 用条件更新（`parked` 从真改假）保证同一次挂起**只执行一次**；
  已解决的返回 409 `already_resolved`。
- **确认计数读不到**（观测库不可用）时怎么处理是显式策略：
  `on_counter_unavailable: reject | park`，默认 `reject` 与历史行为一致。

### `settle_abandoned`：上一轮没作答怎么办

用户弹卡片后直接关页面，前端只 abort 了请求，**服务端仍停在 ASKING**。
这时再发新消息，框架会抛
`Agent is waiting for N tool calls ... but received no event` ——
用户看到「对话运行失败」。

处理：新 run 开跑前，把遗留挂起**按拒绝结清**（`settle_rounds` 轮上限，默认 2）。
结清方式固定是拒绝：没被批准的调用一条都不执行，发条消息不会顺手放行；
模型被拒后换方案的追跑输出直接丢弃（它回答的是上一个问题）。

### 三条硬约束（`runtime/confirm.py` 门面 docstring）

1. **客户端永远不回传命令原文**。`UserConfirmResultEvent` 需要完整 `ToolCallBlock`，
   让前端带回来就等于允许客户端改写将要执行的命令。所以服务端保留权威副本，
   客户端只回 `tool_call_id + confirmed + scope`。
2. 一个 run 最多打扰 **3 次**（`ask_limit`，配置项而非常量）。
3. 高危命令（`config/policy.yaml` 的 `confirm.risk.danger_patterns`：`rm -rf` / `rm -fr` /
   fork bomb `:(){` / `mkfs` / `dd if=` / `shutdown` / `reboot` / `chmod -R 0?777` / `> /dev/sd`）
   强制降级为 `scope=once`。一秒钟的勾选不该变成永久放行 `rm -rf`。

风险分级 `classify()`（`runtime/risk.py`）返回四档：`read` / `exec` / `write` / `danger`；
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
