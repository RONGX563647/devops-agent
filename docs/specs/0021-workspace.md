# SPEC-0021：工作区（Workspace）—— 一工作区一代码库，共享长期记忆、隔离会话

- 状态：方案 v2（待评审）
- 日期：2026-11-27
- 基线：`main e1b3f6b`（SPEC-0018 已合并）
- 关系：承接 SPEC-0013/0016（三层记忆与 token 口径）、SPEC-0017（配置域与降级）；复用 `executor/boundary.py` 的本机执行面，**不涉及 Docker / 沙箱**
- 范围：`src/devops_agent/workspace/**`（新）、`runtime/{assembly,sessions,scope_runtime,prompt}.py`、`memory/{runtime,long_term}.py`、`memory_policy.py`、`observability/{models,repository}/*`、`gateway/{routes,contract}.py`、`config_store/{registry,schema}.py`、`config/workspaces.yaml`（新）、`web/src/pages/workspaces/**`（新）、`web/src/workspace/chat/**`、`tests/**`
- 变更说明（v3）：修两处「设计写着、实现没做到」的断裂。① **执行面真的跟着工作区走**：v2 只把 `workdir` 传进去，而 `assembly.session_runtime_factory` 里 `execution=getattr(base, "execution", None)` 优先用了启动期执行面 —— `_execution_paths` 一见 `execution` 就整体以它为准，`workdir` 被丢弃，于是「绑了工作区，Agent 还在部署默认目录里跑」。现在按工作区根各取一份执行面（`SandboxManager.ensure(root)` 自带按 `(mode, host, image)` 缓存），并在消息末尾补一句身份说明（来源/执行目录/分支），模型不再答「我没有工作区」。② **本地来源拆成两种落盘语义**：默认在本地仓库上开 `git worktree`（`source=worktree`，Agent 在自己的 `agent/*` 分支上改，主检出不动），非 git 目录才原地引用（`source=local`）；远程仍克隆到本项目工作区目录 `<managed_root>/<id>/repo`。
- 变更说明（v2）：收窄范围。移除沙箱/Docker 绑定与容器生命周期；明确「工作区共享长期记忆、不共享会话记忆与上下文」；创建方式改为「选择本地目录」或「导入 Git 仓库」两种
- 触发来源：当前 Agent 只有进程级执行目录（`DEVOPS_AGENT_WORKDIR`），会话只按 `session_id` 隔离，记忆也没有仓库维度；接第二个代码库必须改环境变量并重启
- 证据约定：现状结论一律带 `路径:行号`；设计建议与既存事实分开写

## 1. 设计结论

1. **工作区是「一个代码库的归属单元」，不是一层目录包装**。它要回答三件事：这个会话在哪个仓库里干活、这个仓库的长期记忆算谁的、这个仓库什么时候被归档/删除。今天三者分别靠 `DEVOPS_AGENT_WORKDIR`（进程级，`runtime/prompt.py:17-19`）、`session:{id}`（会话级，`memory/runtime.py:101-104`）和一个手工目录承担，彼此没有关系。

2. **创建只有两种入口：引用本地目录，或从 Git 导入**，但落盘有三种语义。
   - 本地目录 + git 仓库（默认）：**开工作树**。`git worktree add <managed_root>/<id>/worktree -b agent/<slug>-<id末4位>`，与主检出共用一个对象库；Agent 在自己的分支上干活，用户那份 working copy 不会被弄脏，删工作区走 `git worktree remove`（先 `rev-parse --git-common-dir` 反查主仓库，否则留下 `.git/worktrees/<name>` 悬空登记）。
   - 本地目录 + 非 git（或显式 `mode=in_place`）：**原地引用**，不复制。记录 realpath，删除只注销登记，绝不删除用户原目录。
   - Git 导入：克隆到受管目录 `workspace/workspaces/<id>/repo`，删除时按克隆产物处理（脏目录先出 patch）。
   - 三者用 `Repository.managed` 与 `source` 区分（`in_place` → `local/false`，`worktree` → `worktree/true`，`clone` → `git/true`），删除语义因此不同——这是本 spec 最重要的一条防误删规则。

3. **长期记忆按工作区共享，短期与中期严格按会话隔离**。需求是「共享长期记忆，不共享会话记忆上下文」：
   - 共享：`long_term_memories` 的 `scope='workspace'`、`scope_id='workspace:<id>'`，同工作区任意会话可召回。
   - 不共享：短期裁剪、`session_memories`（中期卡片）、`session_states`（对话快照）继续以 `session_id` 为键，绝不跨会话复制或合并。

4. **绑定关系不可静默改投**。一个会话首次绑定工作区后，后续请求带不同 `workspace_id` 必须 409，而不是「以最后一次为准」。否则同一会话的历史里会混入两个仓库的事实，记忆与权限边界同时失真。

5. **执行目录由工作区决定，且执行面本身必须换**。绑定工作区的会话，`workdir` 取 `Repository.root_path`；未绑定的请求继续走 `resolve_workdir()`（`runtime/prompt.py:41-45`）。开关默认关，回滚即关闭。这里有一条必须写进契约的细节：**只传 `workdir` 是不够的**——`_execution_paths` 一旦看到 `execution` 就整体以它为准，`workdir` 会被丢弃。所以装配点必须按工作区根取一份执行面（`SandboxManager.ensure(Path(root))`，按 `(mode, host, image)` 缓存，同一目录复用），并把工作区身份（名称、来源、执行目录、分支/远端）作为一条说明追加在**消息末尾**，让模型能直接回答「我在哪个工作区、代码放哪」。工作区根不存在时返回 409 `workspace_root_missing`，不许悄悄按需建一个空目录冒充。

6. **本地目录引用是新的攻击面，必须用白名单收口**。没有 Docker 隔离时，`/api/workspaces` 若接受任意路径，等于把宿主文件系统暴露给 Agent 工具。所以 `workspace.allowed_local_roots` 为空时本地创建直接禁用；路径必须 realpath 后落在白名单内，且拒绝符号链接逃逸。

7. **仓库状态只读、只走受控 Git 命令**。`.git` 是敏感目录（`governance/paths.py:58`），Agent 工具不得直接读写；工作区自己用 `git rev-parse` / `git status --porcelain` 取分支与脏状态，超时与输出截断由服务层控制。

8. **不引入沙箱与 Docker**。本 spec 的执行面仍是 `LocalWorkspace`（`executor/boundary.py:14-19`），`DockerWorkspace`（`README.md:657`）继续不接线。由此带来的残余风险（进程级宿主访问）在第 12 章显式写明，不用「以后上容器」来掩盖。

## 2. 目标与非目标

### 2.1 目标

- 用「本地目录」或「Git 导入」创建一个工作区；每个工作区绑定一个代码库根目录。
- 会话首次发言时绑定工作区；绑定后不可静默改投。
- 同一工作区的所有会话共享长期记忆；会话的短期上下文、中期摘要、对话快照互不可见。
- 跨工作区的长期记忆检索命中数为 0（硬红线）。
- 工作区可列出、查看、归档、删除；删除本地引用目录不伤原目录，删除克隆仓库按脏状态处理。
- 前端能创建工作区（输入本地路径或 Git URL）、切换当前工作区、查看仓库与记忆概况。
- 所有工作区动作可观测：哪一轮 run 用了哪个工作区、创建/导入/绑定/删除都可追溯。

### 2.2 非目标

- **不做 Docker / 沙箱 / 容器**。执行面仍是本机 `LocalWorkspace`；不定义镜像、标签、挂载、资源限额、容器 GC。
- **不做多人协作与鉴权**。仓库没有登录体系，本期只支持单 owner（`owner='local'`）；成员与配额需要先有鉴权。
- **不做 Git 平台 OAuth/App**。只支持「远端 URL + 已配置凭据引用」，凭据走现有 `secrets/` 保险箱。
- **不做仓库内容同步/拉取策略**。不自动 `git pull`、不管理分支切换；只记录创建时的分支与 commit。
- **不把短期/中期记忆改成工作区级**。跨会话复用只发生在长期层；中期摘要晋升到长期是后续可选项。
- **不做工作区之间迁移会话**。会话不可改投；需要另起会话。

## 3. 术语与现状证据

### 3.1 「workspace」在仓库里已有五个含义

| # | 现有含义 | 位置 | 本 spec 后的称呼 |
|---|---|---|---|
| 1 | 运行态目录 `workspace/`（gitignore） | `observability/repository/db.py:226-231` | 运行态根目录（保持） |
| 2 | Agent 执行目录 `DEVOPS_AGENT_WORKDIR` | `runtime/prompt.py:17-19,41-45` | 执行目录 / workdir |
| 3 | 权限档位 key `"workspace"`（工作区可写） | `governance/model.py:78-88` | 权限档「项目可写」（key 不变） |
| 4 | 前端聊天目录 `web/src/workspace/` | `web/src/App.tsx:9,21` | 聊天 UI（保持） |
| 5 | 评测 fixture 临时目录 | `openjudge/sandbox.py:35-48` | 评测临时目录（保持） |

**结论**：新领域对象统一叫「工作区（Workspace）」，带 `workspace_id`；文档与 API 不再把「工作区」当作目录或权限档的同义词。

### 3.2 现状证据

| 编号 | 断言 | 证据 | 影响 |
|---|---|---|---|
| E-1 | 执行目录是进程级单值 | `runtime/prompt.py:41-45`、`runtime/prompt.py:19` | 第二个仓库必须重启进程 |
| E-2 | 会话只有 `session_id`，无归属 | `runtime/sessions.py:52-68`、`gateway/routes/schemas.py:17-29` | 无法按仓隔离历史与记忆 |
| E-3 | 长期记忆 scope 只有 session/user | `memory_policy.py:48` | 不能表达工作区 |
| E-4 | `scope_id` 实际忽略 scope 字段 | `memory/runtime.py:101-104`；`gateway/routes/memory.py:24-28` 两个分支完全相同 | 「按 user 查」在没有 user_id 时仍是 session |
| E-5 | 长期/中期表按 session/scope 存 | `observability/repository/db.py:143-176` | 无 workspace 列，跨工作区无法判定 |
| E-6 | 会话快照表无 workspace 列 | `observability/repository/db.py:128-133,209-212` | 重建后无法恢复归属 |
| E-7 | 工具后端锚定单个 workdir | `tools/guard.py:51-56`、`tools/catalog.py:110,124-139` | 多工作区必须每会话重建 toolkit |
| E-8 | 配置域无 workspace | `config_store/registry.py:57-107` | 根目录/白名单/限额无处声明 |
| E-9 | SSE 请求无 workspace_id | `gateway/routes/stream.py:23-46` | 只能靠会话间接推断 |
| E-10 | 前端 `/` 是聊天，不是工作区列表 | `web/src/App.tsx:21-27` | 无入口 |
| E-11 | run 记录无工作区归属 | `observability/repository/db.py:185-207` | 无法回答「这轮改了哪个仓」 |
| E-12 | 当前执行面是本机 | `executor/boundary.py:14-19`；`README.md:657` | 无容器隔离，安全边界在 OS 用户 |

## 4. 对象模型

```
Workspace（工作区，1 个代码库）
  ├─ Repository（仓库来源，1:1）
  └─ WorkspaceSession（会话归属，1:N）
        └─ 会话自身的短期/中期记忆与快照（仍按 session_id 隔离）

WorkspaceMemoryScope（逻辑投影，不建表）
  scope='workspace'，scope_id='workspace:<workspace_id>'
```

### 4.1 实体字段

```python
Workspace:
  id: str                 # "ws_" + 12 位 base32（不可变，不复用）
  name: str               # 展示名，可改；同一时刻 active 内唯一
  owner: str              # P0 固定 "local"
  root_path: str          # 代码库根 = Agent workdir；realpath 后必须合法
  default_profile: str    # readonly / workspace / execute / auto
  status: str             # active | archived
  created_at / updated_at / archived_at

Repository:
  workspace_id: str       # PK，1:1
  source: str             # local（原地引用）| worktree（本地开工作树）| git（远端克隆）
  managed: bool           # True=落盘产物归本项目管（worktree / git）；False=原地引用
  remote_url: str         # source=git 时必填；local / worktree 为空
  branch: str             # 创建时分支（worktree 为 agent/<slug>-<id末4位>），空 = 默认 HEAD
  base_commit: str        # 创建时 resolve 的 commit（可空 = 非 git 目录）
  credential_ref: str     # secrets 保险箱里的 id，不存明文
  detected_git: bool      # 本地目录是否本身是 git 仓库（worktree / git 恒为 True）
  created_at

WorkspaceSession:
  session_id: str         # PK，与 session_states.session_id 同键
  workspace_id: str
  bound_at: str
  bound_by: str           # P0 "local"
```

### 4.2 五条不变量

- **I1 一工作区一仓库**：一个 `workspace_id` 至多一条 `Repository`；`root_path` 在 active 工作区间唯一。
- **I2 归属唯一**：一个 `session_id` 至多属于一个工作区；绑定后改投必须 409。
- **I3 长期同源**：绑定工作区的会话，长期记忆 `scope='workspace'`、`scope_id='workspace:<id>'`；不得回落到 `session:{id}`。
- **I4 会话隔离**：短期上下文、`session_memories`、`session_states` 永远以 `session_id` 为键，工作区不提供跨会话读取。
- **I5 删除不伤源**：`managed=false` 的工作区删除只删登记；`managed=true` 才允许清理受管产物，且 `source=worktree` 的清理必须走 `git worktree remove`（先反查主仓库），不许直接 `rmtree` —— 那会留下 `.git/worktrees/<name>` 悬空登记，下次在同路径开工作树会失败。主仓库本身与用户分支永不删除。

## 5. 工作区创建：本地目录与 Git 导入

### 5.1 两种来源

| `source` | 触发 | 行为 | `managed` | 目录位置 | 删除语义 |
|---|---|---|---|---|---|
| `worktree` | 本地目录是 git 仓库（`mode=auto` 默认）或显式 `mode=worktree` | `git worktree add`，新建分支 `agent/<slug>-<id末4位>` | `true` | `<managed_root>/<id>/worktree` | `git worktree remove`；主仓库与用户的 checkout 不动 |
| `local` | 本地目录不是 git 仓库，或显式 `mode=in_place` | 原地引用，不复制、不改名 | `false` | 用户目录（realpath） | 只删登记与绑定；不动目录 |
| `git` | 远程地址 | `git clone` 到本项目工作区目录 | `true` | `<managed_root>/<id>/repo` | 脏则先出 patch；干净则可删 |

`mode=worktree` 用在非 git 目录上返回 400（`workspace_source_invalid`），而不是静默降级成原地引用 —— 用户明确选了「工作树」，就得说清做不到。

**为什么本地默认开工作树而不是原地引用**：原地引用会让 Agent 的改动直接落在用户的 working copy 上，「我让它改个文件，结果本地一堆未提交改动」比多一个目录更难收拾；工作树共用对象库，创建只要几百毫秒（实测 0.54s），并且天然隔离分支。代价是 `.git` 目录多一条登记，所以清理必须走 `worktree remove`。

### 5.2 本地目录的准入

1. `allowed_local_roots` 必须非空，否则 `POST /api/workspaces` 的 `source.kind=local` 返回 403（功能未启用本地导入）。
2. 请求路径 `expanduser()` → `realpath()`；必须是目录、必须存在、必须位于某个白名单根的 realpath 之下。
3. 拒绝：符号链接指向白名单外、根路径 `/`、用户 home 根、包含 `..` 的原始字符串（归一化后仍按 realpath 判定）。
4. 同一 realpath 不能被两个 active 工作区同时引用；返回 409 并给出占用者。工作树的落盘目录由本项目分配，天然唯一，同一个仓库可以开多个工作树。
5. 不校验目录内容是否为空：允许接管已有项目，这也是「选择本地目录」的主要用途。
6. 分支名经 `git check-ref-format --branch` 校验；不填则按 `agent/<slug>-<id末4位>` 生成，中文名压不出 ASCII 时 slug 退回 `ws`（不生成空分支）。

### 5.3 Git 导入

- `remote_url` 只接受 `https://`、`ssh://`、`git@host:path` 三种形态；拒绝 `file://` 与本地路径（避免绕过本地目录白名单）。
- 凭据通过 `credential_ref` 引用 `secrets/`，clone 时用 `GIT_ASKPASS` 临时注入；**不落明文、不写 `.git/config` 的 credential.helper**。
- clone 参数：`--no-tags`、可配 `--depth`（默认 1）、`--single-branch`（指定分支时）；超时默认 120s。
- clone 后：`git config core.hooksPath /dev/null`（禁用仓库钩子），记录 `base_commit = git rev-parse HEAD`。
- 失败清理：clone 失败删除半成品目录，不留下 active 工作区。
- 并发：同一 `remote_url + branch` 不禁止创建多个工作区（用户可能需要两个隔离环境），但 `name` 与 `root_path` 仍唯一。

### 5.4 创建流程

```
校验请求 → 分配 id → 建目录/引用目录 → 解析仓库元信息
  → 写 workspaces + workspace_repositories（同一事务）
  → 发 WORKSPACE_CREATE_END 事件
```

- 先做外部动作（clone）再写库；clone 失败不写库。
- 写库失败回滚登记，并清理本次 clone 产物。
- 创建成功后 `status=active`，默认不自动绑定任何会话。

## 6. 会话绑定与执行目录

### 6.1 解析顺序

`RunRequest` 增加可选 `workspace_id`（`gateway/routes/schemas.py:17-29`）。服务端按顺序解析，只取第一个命中：

1. 请求显式 `workspace_id`：必须存在且 `active`，否则 404 / 409；
2. `workspace_sessions` 中该 `session_id` 的绑定：若请求给了不同值 → 409；
3. 未绑定且请求未带：走旧逻辑（`DEVOPS_AGENT_WORKDIR`），**不**自动塞默认工作区（避免旧部署行为漂移）；
4. 功能开关关闭：完全不解析，忽略 `workspace_id`。

### 6.2 执行目录与执行面

- 绑定后 `workdir = Workspace.root_path`，由 `gateway/routes/stream.py` 解析后经 `turn/lease.py` 传给 `SessionRegistry.acquire`，最终进入 `build_agentscope_runtime(workdir=...)`。
- **执行面必须与 `workdir` 一起换**（v3 修正）：`gateway/routes/workspace_deps.py:execution_for` 用 `SandboxManager.ensure(Path(root))` 取该目录的执行面，一并传给租约与工厂；`assembly.session_runtime_factory` 的 `execution` 形参优先于 `base.execution`。只传 `workdir` 会被 `_execution_paths` 丢弃 —— 那是「绑了工作区却在默认目录里跑」的唯一根因，`tests/unit/test_workspace_execution.py::test_real_runtime_actually_runs_in_the_workspace` 用真装配钉住它。
- 执行面按目录缓存（键 `(mode, host, image)`），同一工作区的多个会话复用一份，不同工作区各一份，不重复初始化。
- `SessionEntry.execution_host` 记录本会话执行面所在目录：同一工作区换执行面也要重建实例，不能只看 `workdir` 字符串。
- 工作区根不存在时 `execution_for` 返回 409 `workspace_root_missing`。不这么做的话，按需初始化会建出一个空目录，Agent 在空目录里跑却认为自己在工作区。
- **身份说明**：`WorkspaceNoteMiddleware`（`runtime/workspace_note.py`）在**模型请求的最后一条消息**追加工作区身份（名称、来源中文说法、执行目录、分支/远端）；未绑定时如实说「没有绑定工作区，执行目录是 X，不要假设别处还有副本」。放消息末尾而不是系统提示，理由与档位说明相同（`runtime/profile_note.py`）：系统提示改一字节，后面的工具 schema 与整段历史前缀缓存全部作废。
- 未归属会话的详情不再 404：`GET /api/workspaces/sessions/{id}` 返回空 `workspace_id` 与 `root_path`，界面据此显示「未归属工作区 · 跑在默认目录」，而不是把「没绑过」当成「不存在」。
- 系统提示里的 workdir 随工作区变化，字节指纹因此变化（`runtime/prompt.py:106-112`）；评测批次要记录 `workspace_id` 与 `prompt_bytes_sha`，避免把「换了仓库」误判成「提示词退化」。
- Windows 上 `uvicorn --reload` / `--workers>1` 会把事件循环设成 `WindowsSelectorEventLoopPolicy`，而 `asyncio` 只有 Proactor 实现 `create_subprocess_exec`，于是 `exec_shell` 全线报 `RuntimeError`。`executor/subprocess_compat.py` 不抢回策略（那会弄坏 uvicorn 自己的重载子进程），只把 `LocalBackend.exec_shell` 换成「循环不支持就下线程」的实现，能力经 `/health.sandbox.subprocess` 暴露。

### 6.3 会话隔离

绑定工作区**不改变**会话隔离方式：

- 每个 `session_id` 仍一份 `Agent` 实例（`runtime/sessions.py:52-68`）；
- 对话历史与权限规则仍按会话快照存 `session_states`（`observability/repository/db.py:128-133`）；
- 同一工作区的会话 A 看不到会话 B 的历史、上下文、中期卡片。

## 7. 记忆：共享长期，隔离会话

### 7.1 scope 扩展

`LongTermPolicy.scope` 从 `Literal["session","user"]` 扩为 `Literal["session","user","workspace"]`（`memory_policy.py:48`）。实际生效作用域由「是否绑定工作区」决定：

```python
def effective_scope(policy, workspace_id: str | None) -> str:
    if workspace_id:
        return "workspace"  # 需求：同工作区共享长期记忆
    return policy.long_term.scope  # 未绑定：保持旧行为
```

`MemoryRuntime` 增加可选 `workspace_id`，`scope_id` 解析改为：

```python
def resolve_scope_id(policy, session_id, workspace_id):
    scope = effective_scope(policy, workspace_id)
    if scope == "workspace":
        return f"workspace:{workspace_id}"
    if scope == "user" and policy.user_id:
        return policy.user_id
    return f"session:{session_id}"
```

装配点在 `runtime/assembly.py:183`（`MemoryRuntime(session_id, policy)`），改为多传 `workspace_id`；`memory_status()`（`memory/runtime.py:343-370`）与记忆查询 API 同步支持。

### 7.2 三层归属

| 层 | 存储 | 归属 | 跨会话 | 工作区影响 |
|---|---|---|---|---|
| 短期 | `AgentState.context` + 裁剪 | 会话 | 否 | 无（各会话独立） |
| 中期 | `session_memories(session_id)` | 会话 | 否 | 仅加 `workspace_id` 列供审计 |
| 长期 | `long_term_memories(scope, scope_id)` | **工作区**（绑定时） | 是 | `scope='workspace'`、`scope_id='workspace:<id>'` |

**明确**：中期卡片不晋升、不复制、不跨会话读取。若后续需要「会话结论留给同工作区的下一个会话」，必须作为长期写入（`kind='session_summary'`）显式发生，且可开关；本 spec 不做。

### 7.3 读写路径

- 写入：`MemoryRuntime.write_turn` 走 `long_term.write`，scope/scope_id 由 §7.1 解析；去重键仍是 `(scope, scope_id, text)`。
- 召回：`long_term.search` 只在当前 `(scope, scope_id)` 内检索；跨工作区不合并、不回退。
- 中期：`episodic.save/active` 仍按 `session_id`（`memory/runtime.py:101-104` 之后的 `EpisodicMemory(self.session_id)`）。
- 删除工作区：默认删除 `scope='workspace' + scope_id='workspace:<id>'` 的长期条目；归档则保留。

### 7.4 硬红线

- 跨工作区长期记忆命中数必须为 0；单测 A9 与评测双重验证。
- 不允许「工作区查不到就回落 session scope」的兜底：那会把别的会话的记忆悄悄带进来。
- 记忆 API 的 `scope` 参数必须真正参与分派，修正 `gateway/routes/memory.py:24-28` 两分支相同的问题。

## 8. 生命周期：归档与删除

### 8.1 状态机

```
create ──> active ──> archived
   │          │
   └──────────┴──> delete（不可恢复；本地引用只注销）
```

| 动作 | 前置 | 结果 | 失败语义 |
|---|---|---|---|
| `create` | 名称合法、未超 `max_workspaces`、来源合法 | 写登记 + 目录就绪 | 403 本地导入未开 / 409 重名或路径占用 / 429 超限 |
| `bind` | 工作区 active、会话未绑定 | 写 `workspace_sessions` + `session_states.workspace_id` | 409 已绑定其他工作区 |
| `archive` | 无强制限制 | `status=archived`；拒绝新绑定与新 run；长期记忆保留 | 幂等 |
| `unarchive` | 已归档 | 回到 active（P2） | 404 |
| `delete` | 无活跃会话持有（或 `force=true`） | 删登记/绑定；`managed=true` 时按脏状态处理目录 | 409 脏目录且 `require_clean_on_delete=true` |

### 8.2 脏目录处理

仅对 `managed=true`（Git 导入）有意义；本地引用目录永远不删。

- `git status --porcelain` 为空：直接删除受管目录。
- 脏且 `require_clean_on_delete=false`：先 `git diff > artifacts/<id>/delete-<ts>.patch`，再删目录；patch 写入失败则中止删除。
- 脏且 `require_clean_on_delete=true`：409，响应给出 `changed_files` 与建议（先提交/清理，或 `force=true`）。

### 8.3 归档与删除的会话处理

- 归档：已有会话仍可读历史（`session_states` 不删），但新 run 返回 409 `workspace_archived`；记忆可读不可写（P0 简单起见：写入跳过并发事件 `skipped`）。
- 删除：`workspace_sessions` 级联删除；`session_states` **不删**（历史仍可回看），但该会话再发起 run 时因无工作区绑定而走旧路径，需在响应里提示「原工作区已删除」。
- 长期记忆：删除工作区默认清理；`--keep-memory` 仅 P2 提供并写审计。

## 9. 存储与迁移

沿用观测库（stdlib sqlite3，零新依赖，`observability/repository/db.py:3-6`）。新增表追加到 `_SCHEMA`，`init_db` 幂等建表：

```sql
CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT 'local',
    root_path       TEXT NOT NULL,
    default_profile TEXT NOT NULL DEFAULT 'workspace',
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','archived')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_name
    ON workspaces(name) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_root
    ON workspaces(root_path) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS workspace_repositories (
    workspace_id   TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    source         TEXT NOT NULL CHECK (source IN ('local','git')),
    managed        INTEGER NOT NULL DEFAULT 0,
    remote_url     TEXT NOT NULL DEFAULT '',
    branch         TEXT NOT NULL DEFAULT '',
    base_commit    TEXT NOT NULL DEFAULT '',
    credential_ref TEXT NOT NULL DEFAULT '',
    detected_git   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_sessions (
    session_id   TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    bound_at     TEXT NOT NULL,
    bound_by     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_workspace_sessions_ws ON workspace_sessions(workspace_id);
```

迁移（沿用 `observability/repository/db.py:261-268` 的 `_migrate`）：

- `session_states` 加列 `workspace_id TEXT NOT NULL DEFAULT ''`（`_SESSION_COLUMNS`，`db.py:209-212`）。
- `session_memories` 加列 `workspace_id TEXT NOT NULL DEFAULT ''`，仅审计，不改变查询键。
- `runs` 加列 `workspace_id TEXT NOT NULL DEFAULT ''`（`_RUN_COLUMNS`，`db.py:185-207`），回答「这轮 run 用了哪个工作区」。
- `long_term_memories` **不改结构**：工作区作用域用既有 `scope/scope_id` 表达，避免第二套真相。
- 不建 `workspace_memory_scope` 表：scope 是逻辑投影（§4）。

## 10. 接口

### 10.1 HTTP（`gateway/routes/workspaces.py`，新）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workspaces` | 列表（含 source / status / 是否 managed） |
| POST | `/api/workspaces` | 创建，`source` 为判别联合 |
| GET | `/api/workspaces/{id}` | 详情：仓库元信息、绑定会话数、长期记忆条数 |
| PATCH | `/api/workspaces/{id}` | 改名 / 默认档位 |
| POST | `/api/workspaces/{id}/archive` | 归档 |
| POST | `/api/workspaces/{id}/restore` | 取消归档（P2） |
| DELETE | `/api/workspaces/{id}` | `?force=false`；语义见 §8 |
| GET | `/api/workspaces/browse` | 列出 `allowed_local_roots` 下的子目录，供「选择本地目录」使用 |
| POST | `/api/session/{session_id}/workspace` | `{workspace_id}` 绑定；409 已绑其他 |
| GET | `/api/workspaces/{id}/memory/long-term` | 固定 `scope=workspace` 的长期记忆列表 |

创建请求体：

```jsonc
{
  "name": "devops-agent",
  "default_profile": "workspace",
  "source": {
    "kind": "local",
    "path": "/data/repos/devops-agent"
  }
}
```

```jsonc
{
  "name": "devops-agent",
  "source": {
    "kind": "git",
    "url": "https://github.com/org/repo.git",
    "branch": "main",
    "credential_ref": "git-cred-1",
    "depth": 1
  }
}
```

- `kind` 用 pydantic 判别联合，缺字段直接 422，不做「猜是本地还是远端」。
- `browse` 只读、只列目录名，不返回文件内容；路径仍按白名单校验。
- 所有路由不得 import `gateway.app`（`.importlinter` 契约 `gateway-routes-no-app`）。

### 10.2 请求链路

`RunRequest` 加 `workspace_id: str | None = None`（`gateway/routes/schemas.py:17-29`）；`gateway/turn/lease.py` 解析后把 `workspace_id` 与 `workdir` 交给 `SessionRegistry.acquire`。`SessionRegistry` 的工厂签名增加 `workspace: WorkspaceRef | None`，`build_agentscope_runtime` 增加 `workspace_id` 参数（`runtime/assembly.py:249-280`）。未解析到工作区时保持旧签名。

### 10.3 CLI（`python -m devops_agent.workspace`）

```
workspace list
workspace create --name <n> --local /path [--mode auto|worktree|in_place] [--branch b] [--profile workspace]
workspace create --name <n> --git <url> [--branch main] [--credential <ref>]
workspace bind <id> --session <sid>
workspace status <id>
workspace archive <id>
workspace delete <id> [--force] [--keep-memory]
```

CLI 与服务层共用同一组函数，不复制业务逻辑。

### 10.4 前端

- 新增 `/workspaces`、`/workspaces/:id` 路由（`web/src/App.tsx:21-27` 追加），侧边栏加入口。
- 创建工作区向导：两个页签「选择本地目录」（`browse` 接口 + 路径输入）与「导入 Git」（URL/分支/凭据）。本地页签里要选**落盘方式**（`role=radiogroup`）：工作树（默认，可填分支）或原地引用 —— 这是用户唯一能表达「别动我本地那份」的地方，默认藏起来等于逼人踩坑。
- 聊天页顶部显示当前工作区**名称 + 执行目录末两段**；未绑定时显示「未归属工作区 · 跑在默认目录」而非静默落默认。用户问「你在哪跑」，界面自己先答一遍。
- 详情页展示：来源（本地工作树 / 本地目录 / 远端克隆）、`managed`、分支、`base_commit`、执行目录、会话数、长期记忆条数、归档/删除按钮，并明说「会话就在这个目录里执行」。
- 改动 `web/**` 必须先按 AGENTS §3.8 读取 `skills/.seed/modern-web-guidance/SKILL.md` 并检索；本 spec 不含前端实现细节。

## 11. 配置

新增 `workspace` 配置域（`config_store/registry.py:57-107`），模型 `WorkspacePolicy` 放 `config_schema.py`：

```python
class WorkspacePolicy(BaseModel):
    enabled: bool = False
    root: str = ""  # 空 = workspace/workspaces（受管克隆根）
    allow_local_import: bool = False  # 默认关：本地路径是高危入口
    allowed_local_roots: list[str] = Field(default_factory=list)
    allow_git_import: bool = True
    git_clone_timeout_sec: int = Field(default=120, ge=10, le=900)
    max_workspaces: int = Field(default=10, ge=1, le=200)
    require_clean_on_delete: bool = True
    default_profile: str = "workspace"
```

环境变量（`ENV_OVERLAY`，`config_store/registry.py:115-122`）：

| 变量 | 键 | 取值 |
|---|---|---|
| `DEVOPS_AGENT_WORKSPACES_ENABLED` | `enabled` | bool |
| `DEVOPS_AGENT_WORKSPACE_ROOT` | `root` | str |
| `DEVOPS_AGENT_WORKSPACE_ALLOW_LOCAL` | `allow_local_import` | bool |
| `DEVOPS_AGENT_WORKSPACE_LOCAL_ROOTS` | `allowed_local_roots` | 逗号分隔（需扩展 overlay 的 list 类型） |

生效时机 `restart`（根目录与白名单在启动期固定），与 `gateway` 域一致（`registry.py:80-87`）。`enabled=false` 时新路由返回 404，运行路径完全走旧逻辑。

**本仓库当前基线（`config/workspaces.yaml`）**：`enabled: true` + `allow_local_import: true`，`allowed_local_roots` 留空。白名单是本机绝对路径，不进仓库 —— 写进 `config.db`（界面「工作区」域，或 `PATCH /api/config/workspace`）或 `DEVOPS_AGENT_WORKSPACE_LOCAL_ROOTS`。留空时 `source.kind=local` 一律 403（`workspace/paths.py:56-59`），Git 导入不受影响；全新克隆要么填白名单，要么先走 Git 导入。代码里的 pydantic 默认仍是 `False`，回滚只需把这两项改回 `false` 并重启。

## 12. 安全

| 风险 | 控制 |
|---|---|
| 本地路径任意读写（无容器时最危险） | `allow_local_import` 只由基线或白名单显式打开；`allowed_local_roots` 必填；realpath 后必须在白名单内 |
| 路径穿越 / 符号链接逃逸 | id 正则 + realpath + 前缀校验；拒绝 `/`、home 根、`..` |
| 同一目录被多工作区并发改写 | `root_path` 在 active 内唯一；冲突 409 |
| Git 凭据泄漏 | 只存 `credential_ref`；clone 用临时 `GIT_ASKPASS`；不写 `credential.helper`；日志脱敏 |
| 仓库钩子执行任意代码 | clone 后 `core.hooksPath=/dev/null`；不自动 checkout 未知 ref |
| `.git` 读写与权限矛盾 | Agent 不直接读 `.git`（`governance/paths.py:58` 保持）；状态查询走受控 `git` 子进程 |
| 跨工作区记忆泄漏 | I3 + 查询固定 scope；A9 断言 0 命中 |
| 误删用户目录 | `managed=false` 不删目录；`managed=true` 才处理，脏则先出 patch；`source=worktree` 只 `git worktree remove`，主仓库与用户 checkout 永不删除 |
| 无容器隔离的残余风险 | 显式声明：安全边界在 OS 用户与权限档；readonly/execute 档仍生效；不对不可信用户开放 |

## 13. 可观测

- 事件：`WORKSPACE_CREATE_START/END`、`WORKSPACE_REPO_IMPORT_*`、`WORKSPACE_BIND_*`、`WORKSPACE_ARCHIVE_*`、`WORKSPACE_DELETE_*`、`MEMORY_SCOPE_BOUND`。
- 归属：`runs.workspace_id`（§9）与 `session_states.workspace_id`，支持「按工作区看运行与记忆」。
- 指标（`/api/workspaces/{id}` 与 `/health`）：
  - 工作区数、active/archived 数；
  - 每个工作区的会话数、长期记忆条数；
  - Git 导入耗时（`import_ms`）与失败数；
  - `leak_hits`（跨工作区检索命中，必须恒为 0）。
- 失败必须可见：本地路径被拒、clone 失败、脏目录拒绝删除都写事件与结构化错误码，不只打日志。

## 14. 验收与测试

### 14.1 验收项

| 编号 | 验收 | 方式 |
|---|---|---|
| A1 | 在白名单内引用本地目录创建工作区，`root_path` 为 realpath，`managed=false` | 单测 + CLI |
| A2 | 白名单外 / 符号链接逃逸 / `/` / home 根全部拒绝（403/400） | 参数化单测 |
| A3 | Git 导入创建工作区，`managed=true`、`base_commit` 非空、钩子被禁用 | 单测（本地裸仓作远端） |
| A4 | 同 realpath 重复创建 409；同名 active 409 | 单测 |
| A5 | 会话绑定后，请求带不同 `workspace_id` 返回 409 | 单测 |
| A6 | 绑定会话的 `workdir` 等于工作区根；未绑定请求仍用 `DEVOPS_AGENT_WORKDIR` | 单测 + 回归 |
| A7 | 同工作区两个会话：历史、短期上下文、中期卡片互不可见 | 单测（断言 `session_memories` 无交叉） |
| A8 | 同工作区会话 A 写长期事实，会话 B 能召回 | 单测 |
| A9 | 两个工作区互查长期记忆命中数为 0 | 单测（硬红线） |
| A10 | 未绑定 / 开关关闭时，提示词字节与执行目录与改动前一致 | 回归测试 |
| A11 | 删除本地引用工作区后原目录仍在；删除脏的克隆工作区先出 patch 或 409 | 单测 |
| A12 | 归档后新绑定 409，长期记忆仍可读 | 单测 |
| A13 | `workspace` 配置域可读、越界值 400、env 覆盖生效、`enabled=false` 路由 404 | 单测 |
| A14 | `browse` 只列白名单内目录，不返回文件内容 | 单测 |
| A15 | `runs.workspace_id` 正确落库，可按工作区查询 | 单测 |
| A16 | 前端创建工作区（本地/Git 两页签）与聊天页切换器可用 | 前端测试（先读 modern-web-guidance） |
| A17 | 本地 git 仓库默认落成 `source=worktree`：落盘在 `<managed_root>/<id>/worktree`、分支 `agent/<slug>-<id末4位>`、主检出 `git status` 不变；非 git 目录落 `local`；`mode=worktree` 遇非 git 目录 400 | 单测（真 git 仓库） |
| A18 | 删工作区走 `git worktree remove`，主仓库与登记目录都清干净；不许留下 `.git/worktrees/<name>` | 单测 |
| A19 | 老库（`workspace_repositories.source` 的 CHECK 只允许 `local/git`）能升级到允许 `worktree`，且原有行不丢 | 迁移单测 |
| A20 | **真装配**：绑定工作区后运行时的 `_workdir` 与 `_tool_workdir` 都等于工作区根，中间件链里有 `WorkspaceNoteMiddleware` 且说明里带真实路径 | 单测（不 mock 装配） |
| A21 | 端到端：绑工作区跑一轮 `pwd`，工具输出的就是工作区目录；模型回答里能说出工作区名、来源、分支 | 手工验收（真模型一次调用） |
| A22 | 工作区根不存在时 409 `workspace_root_missing`；未归属会话详情返回空 `workspace_id` 而非 404 | 单测 |
| A23 | `WindowsSelectorEventLoopPolicy` 下 shell/文件工具仍可用（线程回落），`/health.sandbox.subprocess` 如实上报能力 | 单测 + 手工（`uvicorn --reload`） |

### 14.2 质量门禁

沿用现有门禁：`ruff`、`mypy src`、`vulture`、`pytest --cov-fail-under=80`、`check_file_size.py`（新文件 ≤500 行）、`lint-imports`。新增契约：

- `devops_agent.workspace` 不得 import `agentscope`（领域与存储纯 stdlib + pydantic）；
- `workspace` 不得反向依赖 `gateway`；
- `observability` 不得依赖 `workspace`。

## 15. 分阶段落地

| 阶段 | 内容 | 交付物 | 验收 |
|---|---|---|---|
| P0 | 对象模型、表与迁移、本地引用 / Git 导入、会话绑定、workdir 切换、长期记忆 workspace scope、API + CLI、配置域 | `workspace/{models,store,service,git}.py`、`gateway/routes/workspaces.py`、`config/workspaces.yaml`、迁移 | A1–A15 |
| P1 | 前端：工作区列表、创建向导（本地浏览 + Git 导入）、聊天页切换器、详情页 | `web/src/pages/workspaces/**`、`web/src/workspace/chat/**`、`web/src/types/workspace.ts` | A16 |
| P2 | 归档恢复、`--keep-memory`、会话结论晋升长期（可开关）、批量清理命令 | 服务与 CLI 扩展 | 复审后补 |
| P3 | 旧会话显式迁移到工作区（记忆复制）、评测用例分配临时工作区 | 迁移 CLI、openjudge 适配 | 迁移回归 |

依赖：P0 稳定后再做 P1；P2/P3 不以本期承诺。

## 16. 兼容、回滚与风险

### 16.1 兼容

- 回滚开关 `workspace.enabled=false`（代码默认即此值；本仓库基线在 §11 已显式打开）：不建表数据、不解析 `workspace_id`、不新增必填参数。
- 所有新增表/列均为追加式，`init_db` 幂等升级，无需停机脚本。
- `RunRequest.workspace_id` 可选；旧前端不传不产生 422。
- 记忆默认 scope 不变；只有绑定工作区的会话才切到 `workspace`。
- 执行面仍 `LocalWorkspace`，不改变工具、权限档、敏感路径守卫。

### 16.2 回滚

1. 设 `DEVOPS_AGENT_WORKSPACES_ENABLED=0` 并重启 → 新路由 404，运行链路回到旧路径。
2. 新增表可原样保留；旧代码只读写自己的列。
3. 已写入 `workspace` scope 的长期记忆在关闭开关后不被读取，但不会丢失；需要导出时用迁移命令（P3）。
4. 受管克隆目录可手工删除或由 `workspace delete` 清理；本地引用目录不受影响。

### 16.3 主要风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| 本地目录导入导致宿主文件被读写 | 高 | 默认关闭 + 白名单 + realpath + 唯一路径；无容器隔离需明确告知 |
| 记忆 re-scope 造成「历史失忆」 | 中 | 默认不自动迁移；提供显式迁移命令；A10 兼容断言 |
| 会话绑定后改投导致串仓 | 中 | I2 + 409；前端切换工作区时提示「需新会话」 |
| 长任务中工作区被归档/删除 | 中 | 归档只拦新 run；删除要求无活跃会话，`force` 需显式 |
| SQLite 单写者 | 低 | 沿用 WAL + busy_timeout（`observability/repository/db.py:242-244`）；工作区动作低频 |
| 命名冲突（权限档也叫 workspace） | 低 | §3.1 消歧；界面文案改「项目可写」，key 不变 |

## 17. 实施前需核实

1. 本仓库依赖 `agentscope>=2.0`（`pyproject.toml`），本次沙箱未安装，无法运行框架相关单测；实现时以 CI 环境为准。
2. `LocalWorkspace` 已在本仓库与测试中使用（`executor/boundary.py:14-19`、`tests/unit/test_tools_boundary.py:111-113`），本期不新增框架 API。
3. `git` 子进程需在部署环境可用；缺失时 `workspace` 的 Git 能力降级为「仅本地非 Git 目录」，并在 `/health` 上报。
4. `session_states.workspace_id` 与 `SessionRegistry` 重建路径的兼容（`runtime/sessions.py:120-134`）需在 P0 首个 PR 用回归测试钉住。

## 18. 术语表

| 术语 | 含义 |
|---|---|
| 工作区 / Workspace | 本 spec 引入的领域实体：一个代码库 + 一组归属会话 |
| 本地工作树 / worktree | 在用户已有的 git 仓库上开的工作树，落盘在受管目录，Agent 在 `agent/*` 分支上改，用户 checkout 不受影响 |
| 本地引用 / local | 工作区指向用户已有目录，`managed=false`，删除不删目录；**仅用于非 git 目录或显式 `mode=in_place`** |
| Git 导入 / git | 克隆到受管目录，`managed=true`，删除按脏状态处理 |
| 执行目录 / workdir | Agent 工具的相对路径基准与 shell cwd；绑定时等于 `root_path` |
| 执行面 / execution | `SandboxManager` 按工作区根给出的那份执行后端；**必须与 workdir 一起换**，只换 workdir 不生效 |
| 工作区会话 / WorkspaceSession | `session_id → workspace_id` 的绑定，一经建立不可静默改投 |
| 长期记忆作用域 / MemoryScope | `scope='workspace'`、`scope_id='workspace:<id>'` |
| 会话记忆 | 短期上下文、`session_memories`、`session_states`；始终按 `session_id` 隔离 |
