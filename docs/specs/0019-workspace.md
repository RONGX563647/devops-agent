# SPEC-0019：工作区（Workspace）—— 一仓一环境，贯通记忆、沙箱与 Docker

- 状态：方案（待评审）
- 日期：2026-11-27
- 基线：`main e1b3f6b`（SPEC-0018 已合并）
- 关系：承接 SPEC-0013/0016（三层记忆与 token 口径）、SPEC-0017（配置域与降级）、SPEC-0018（模型能力）；把 `executor/boundary.py` 里已存在但未接线的 `LocalWorkspace` / `DockerWorkspace` 变成运行时的一等公民；不新增外部依赖，容器能力仍由 AgentScope 内置 workspace 提供
- 范围：`src/devops_agent/workspace/**`（新）、`executor/{boundary,workspace}.py`、`runtime/{assembly,sessions,scope_runtime,prompt}.py`、`memory/{runtime,long_term}.py`、`memory_policy.py`、`observability/{models,repository}/*`、`gateway/{routes,contract}.py`、`config_store/{registry,schema}.py`、`config/workspaces.yaml`（新）、`web/src/pages/workspaces/**`（新）、`web/src/workspace/chat/**`、`tests/**`
- 触发来源：当前 Agent 只有一个进程级工作目录（`DEVOPS_AGENT_WORKDIR`），会话只按 `session_id` 隔离；接入第二个仓库就必须改环境变量并重启，记忆也无法按仓库隔离。用户要求「一个工作区对应一个代码仓库和一个工作空间，并接入已有的记忆 / 沙箱 / Docker 体系」
- 证据约定：现状结论一律带 `路径:行号`；设计建议与既存事实分开写；本次沙箱未安装 `agentscope`，涉及框架内部 API 的结论标 `NOT RUN` 并给出实现前的 introspect 命令

## 1. 设计结论

1. **要引入的不是「再包一层目录」，而是把「在哪干活、记忆算谁的、容器归谁」三件事绑成一个可寻址对象**。今天这三件事分别由 `DEVOPS_AGENT_WORKDIR`（进程级）、`session:{id}`（会话级）、一个未接线的 `DockerWorkspace` 工厂（`executor/boundary.py:22-28`）承担，它们之间没有任何外键。工作区就是补上这条主键。

2. **一个工作区 = 1 个仓库根 + 1 个执行目录 + 1 个记忆 scope + 1 个沙箱绑定**。四者不是「都可以配」，而是同一实体的四个投影：不允许出现「工作区 A 的执行目录、工作区 B 的记忆」这种组合。这条不变量由 API 与单测同时钉住（A4/A5）。

3. **会话是工作区的访客，不是所有者**。`session_id` 仍由前端生成，但首次绑定到工作区后不可静默改投；同一工作区可以有多个会话，同一会话只能属于一个工作区。否则「历史里一半是 A 仓的文件、一半是 B 仓的事实」，记忆隔离与权限边界都无从谈起。

4. **记忆的作用域要从 `session|user` 扩成 `session|user|workspace`，且默认值随绑定切换**。现在是 `memory_policy.py:48` 的 `Literal["session", "user"]`，`MemoryRuntime` 只把 `user_id` 或 `session:{id}` 当 `scope_id`（`memory/runtime.py:101-104`）——`long_term.scope` 只影响写库列，没影响 id 的生成。工作区必须显式参与 id：`workspace:{workspace_id}`，且跨工作区检索命中数必须为 0（硬红线）。

5. **Docker 不是新写一套，而是把已有工厂接进生命周期**。`build_docker_workspace()` 已经能产出 `DockerWorkspace`（`executor/boundary.py:22-28`），但它没有容器标签、没有挂载清单、没有 GC、没有健康检查，运行时也从未调用它（`README.md:657`）。本 spec 补的是「绑定 + 生命周期 + 可观测」，不是容器运行时本身。

6. **本地后端先落地，Docker 后落地**。P0 用 `LocalWorkspace` 把对象模型、存储、API、记忆 scope、绑定关系全部跑通；P1 只替换执行面适配器，不改上层契约。这样 Docker 不可用时工作区仍然可用（降级为 local 并如实上报），与 SPEC-0017「读降级、写拒绝」的失败语义一致。

7. **「workspace」这个词在仓库里已有五个含义，必须先消歧**。见 §3.1。本 spec 之后：领域实体一律写「工作区 / Workspace 实体」，执行目录写「执行目录 / workdir」，权限档位 key 保留 `workspace` 但界面文案改为「项目可写」，前端聊天目录不改名（它是 UI 目录，不是领域对象）。

8. **`.git` 是工作区的前置矛盾，必须显式处理**。现状把 `.git` 列为敏感目录，Read/Grep/Write 全拒（`governance/paths.py:58`）。工作区以仓库为中心，但 Agent 不应直接读 `.git`；仓库操作走受控 `GitFacade`（status/diff/log/show 白名单子命令 + 超时 + 输出截断），而不是放开 `.git` 读权限。

9. **默认工作区保证向后兼容**。未带 `workspace_id` 的请求继续走 `resolve_workdir()`（`runtime/prompt.py:41-45`），行为与今天逐字节一致；首次启用时自动登记 `ws_default` 指向该目录，但不强制迁移记忆。开关默认关，回滚只需关开关。

10. **配置进入既有配置域，不新建第二套**。新增 `workspace` 域（`config_store/registry.py:57-107` 的注册表模式），环境变量走 `ENV_OVERLAY`（`config_store/registry.py:115-122`），生效时机标 `restart`（镜像与后端在启动期固定）。

## 2. 目标与非目标

### 2.1 目标

- 一个工作区绑定一个代码仓库、一个执行目录、一个长期记忆 scope、一个沙箱实例；四者在存储层有外键，在 API 层可查询。
- 会话在首次发言时绑定工作区；绑定后不可静默改投，跨工作区访问返回明确错误码。
- 记忆默认按工作区隔离：同一问题在 A 工作区写入的事实，B 工作区检索不到（自动化红线）。
- Docker 后端可按工作区创建/启动/停止/回收容器，带标签、镜像 digest、资源限制与健康检查。
- 工作区的创建、归档、删除、脏目录处理、容器 GC 都有可重放的状态机，不靠人工清目录。
- 前端能列出工作区、切换当前工作区、看到沙箱状态；聊天页显示当前工作区。
- 所有生命周期动作落 span/事件，评测（openjudge）可为每个用例分配临时工作区，验证隔离。

### 2.2 非目标

- **不做多人协作与鉴权**。仓库目前没有登录体系，P0–P1 只支持单 owner；成员表、角色、配额放 P2，且必须先有鉴权，否则「成员」只是装饰。
- **不做远程仓库托管平台的 OAuth/App 安装**。P1 只支持「已有远端 URL + 已配置凭据引用」，凭据走现有 `secrets/` 保险箱，不引入 Git 平台 SDK。
- **不做在线迁移的热切换**。工作区迁移（换目录、换镜像）要求停止会话，P3 只做离线迁移与归档恢复。
- **不把短/中期记忆改成工作区级**。短期是轮内工作集、中期是会话摘要，天然属于会话；只有长期（语义/程序性）跨会话，才归工作区。跨会话再聚合放到 P2 的「会话摘要晋升」。
- **不重写容器运行时**。继续用 `agentscope.workspace.DockerWorkspace`；不自研 cgroup/seccomp/镜像构建。

## 3. 术语与现状证据

### 3.1 五个「workspace」先消歧

| # | 现有含义 | 位置 | 本 spec 后的称呼 |
|---|---|---|---|
| 1 | 运行态目录 `workspace/`（gitignore） | `observability/repository/db.py:226-231` | 运行态根目录（保持） |
| 2 | Agent 执行目录 `DEVOPS_AGENT_WORKDIR` | `runtime/prompt.py:17-19,41-45` | 执行目录 / workdir |
| 3 | 权限档位 key `"workspace"`（工作区可写） | `governance/model.py:78-88` | 权限档「项目可写」（key 不变） |
| 4 | 前端聊天目录 `web/src/workspace/` | `web/src/App.tsx:9,21` | 聊天 UI（保持） |
| 5 | 评测 fixture 临时目录 | `openjudge/sandbox.py:35-48` | 评测临时工作区（复用新实体） |

**结论**：新领域对象统一叫「工作区（Workspace）」，带 `workspace_id`；所有文档与 API 不再把「工作区」当作目录或权限档的同义词。

### 3.2 现状证据

| 编号 | 断言 | 证据 | 影响 |
|---|---|---|---|
| E-1 | 执行目录是进程级单值 | `runtime/prompt.py:41-45`、`runtime/prompt.py:19` | 第二个仓库必须重启进程 |
| E-2 | 会话只有 `session_id`，无归属 | `runtime/sessions.py:52-68`、`gateway/routes/schemas.py:17-29` | 无法按仓隔离历史与记忆 |
| E-3 | 长期记忆 scope 只有 session/user | `memory_policy.py:48` | 不能表达工作区 |
| E-4 | scope_id 实际忽略 scope 字段 | `memory/runtime.py:101-104`；`gateway/routes/memory.py:24-28` 两个分支完全相同 | 「按 user 查」在没有 user_id 时仍是 session |
| E-5 | 长期/中期表按 session/scope 存 | `observability/repository/db.py:143-176` | 无 workspace 列，跨工作区查询只能靠字符串猜 |
| E-6 | DockerWorkspace 有工厂、无接线 | `executor/boundary.py:11-28`、`README.md:431,657` | 生产仍是本机执行面 |
| E-7 | 权限档有 workspace/container 两级 | `governance/model.py:47-56,99-107` | 只有「工作区内」概念，没有「哪个工作区」 |
| E-8 | 工具后端锚定单个 workdir | `tools/guard.py:51-56`、`tools/catalog.py:110,124-139` | 多工作区必须每会话重建 toolkit |
| E-9 | 会话快照表无 workspace 列 | `observability/repository/db.py:128-133,209-212` | 重建后无法恢复归属 |
| E-10 | 配置域无 workspace | `config_store/registry.py:57-107` | 镜像/根目录/限额无处声明 |
| E-11 | SSE 请求无 workspace_id | `gateway/routes/stream.py:23-46` | 只能靠会话间接推断 |
| E-12 | 前端 `/` 是聊天，不是工作区列表 | `web/src/App.tsx:21-27` | 无入口 |
| E-13 | 评测沙箱是临时复制目录 | `openjudge/sandbox.py:35-48` | 不复用容器，也不验证工作区隔离 |
| E-14 | spans 类型只有四类 | `observability/repository/db.py:32-36` | 工作区/沙箱动作无落点 |

## 4. 对象模型

```
Workspace（工作区）
  ├─ Repository（代码仓库，1:1）
  ├─ SandboxBinding（沙箱绑定，1:0..1 活跃）
  ├─ WorkspaceSession（会话归属，1:N）
  └─ MemoryScope（长期记忆，1:1，scope='workspace'）
```

### 4.1 实体字段

```python
Workspace:
  id: str                 # "ws_" + 12 位 base32（不可变，不复用）
  name: str               # 展示名，可改
  owner: str              # P0 固定 "local"，P2 接鉴权后为真实用户
  root_path: str          # 绝对路径，realpath 后必须落在 workspace_root 内
  default_profile: str    # readonly / workspace / execute / auto
  status: str             # active | archived | deleted
  created_at / updated_at / archived_at

Repository:
  workspace_id: str       # PK，1:1
  kind: str               # git | plain
  remote_url: str         # 空 = 本地 init
  branch: str             # 默认 HEAD
  base_commit: str        # 创建时 resolve 的 commit（不可变基线）
  credential_ref: str     # secrets 保险箱里的 id，不存明文
  created_at

SandboxBinding:
  workspace_id: str       # PK
  backend: str            # local | docker
  image: str              # 仅 docker
  image_digest: str       # sha256:...，创建时固定
  container_id: str       # 仅 docker，停止后保留用于清理
  host_workdir: str       # 挂载到容器的工作目录
  cpu_limit / mem_limit_mb / pids_limit
  network: str            # none | egress
  status: str             # stopped | starting | running | degraded | error
  last_heartbeat: str

WorkspaceSession:
  workspace_id: str
  session_id: str         # 与 session_states.session_id 同键
  bound_at: str
  bound_by: str

MemoryScope（逻辑投影，不单独建表）:
  scope = "workspace"
  scope_id = "workspace:" + workspace_id
```

### 4.2 六条不变量

- **I1 唯一绑定**：一个 `workspace_id` 至多一条 `Repository`、至多一条活跃 `SandboxBinding`。
- **I2 归属唯一**：一个 `session_id` 至多属于一个工作区；绑定后改投必须显式 `fork`（P2），P0 直接拒绝。
- **I3 记忆同源**：工作区绑定的会话，其长期记忆 `scope='workspace'` 且 `scope_id='workspace:<id>'`；不得回落到 `user:{id}`。
- **I4 路径封闭**：`root_path` 与 `host_workdir` 的 realpath 必须位于 `workspace_root` 下，且不含符号链接逃逸。
- **I5 单一租约**：同一工作区同一时刻只允许一个活跃沙箱；会话级并发仍由 `SessionRegistry` 的锁负责（`runtime/sessions.py:61-63`）。
- **I6 标签可回收**：Docker 容器必须带 `devops.managed=1`、`devops.workspace=<id>`；无标签容器一律不碰。

## 5. 身份、隔离与目录布局

### 5.1 目录布局

```
workspace/                          # 已 gitignore 的运行态根
└─ workspaces/
   └─ ws_7f3k2m9q1x4p/              # workspace_id
      ├─ repo/                      # 仓库根 = Agent workdir
      ├─ data/                      # 会话产物、附件、临时文件
      ├─ artifacts/                 # 导出物（报告、patch、日志）
      └─ meta.json                  # 冗余快照（DB 为唯一真相，此文件供排障）
```

`workspace_root` 默认 `workspace/workspaces`，可由 `DEVOPS_AGENT_WORKSPACE_ROOT` 覆盖。**DB 是唯一真相**，`meta.json` 只写不读；两者不一致时以 DB 为准并打 warning。

### 5.2 id 生成与校验

- 生成：`uuid4().hex` 的 Crockford base32 前 12 位，前缀 `ws_`；碰撞时重试 3 次。
- 校验：`^ws_[a-z0-9]{12}$`。任何来自 URL/body 的 id 先校验再落路径，禁止把原始字符串拼进 `Path`。
- 路径解析：`(root / ws_id).resolve()` 后再次断言 `is_relative_to(root.resolve())`；符号链接逃逸与 `..` 一律 400。

### 5.3 会话绑定解析顺序

`RunRequest` 增加可选 `workspace_id`（`gateway/routes/schemas.py:17-29`）。服务端按以下顺序解析，**只取第一个命中**：

1. 请求显式 `workspace_id`（必须存在且 active，否则 404/409）；
2. `workspace_sessions` 里该 `session_id` 的绑定（存在则忽略请求里的不同值并 409，防止静默改投）；
3. 配置的默认工作区 `ws_default`（首次启用时懒创建，指向 `DEVOPS_AGENT_WORKDIR`）；
4. 功能开关关闭时：不解析，完全走旧路径。

## 6. 生命周期

```
create ──> provision(repo) ──> bind(session) ──> start(sandbox)
   │                              │                  │
   │                              └── run/stream ────┘
   ├──> archive（只读，禁止 start）
   └──> delete（需无活跃租约；脏目录按策略归档）
```

### 6.1 状态机与动作

| 动作 | 前置 | 结果 | 失败语义 |
|---|---|---|---|
| `create` | 名称合法、未超 `max_workspaces` | 建 DB 行 + 空目录；`status=active` | 409 重名 / 429 超限 |
| `attach_repo` | 工作区 active 且未绑仓库 | clone 或本地 init；记录 `base_commit` | 401 凭据无效 / 409 已绑定 / 422 URL 非法 |
| `bind_session` | 工作区 active、会话未绑定 | 写 `workspace_sessions` + `session_states.workspace_id` | 409 已绑定到其他工作区 |
| `start_sandbox` | 仓库已就绪、无活跃租约 | local 直接置 running；docker 拉/建/启容器 | 503 镜像不可用；降级 local 需显式策略 |
| `stop_sandbox` | 有活跃绑定 | docker stop + 保留容器（便于查日志）；local 置 stopped | 幂等 |
| `archive` | 无活跃租约 | `status=archived`，禁止 start，仍可读记忆 | 409 有活跃会话 |
| `delete` | 无活跃租约 | 删 DB 行 + `docker rm` + 目录移入 `workspace/trash/`（保留 7 天） | 409 脏目录且 `require_clean_on_delete=true` |

### 6.2 脏目录处理

- 删除前跑 `git status --porcelain`（受控 `GitFacade`，不经 Agent 工具）。
- `clean`：直接进入 trash。
- `dirty` 且 `require_clean_on_delete=false`：先 `git diff > artifacts/<ws>/delete-<ts>.patch`，再归档；归档失败则中止删除（宁可留下，不可丢改动）。
- `dirty` 且 `require_clean_on_delete=true`：409，错误体给出 `changed_files` 与建议动作。

### 6.3 租约与 GC

- `SandboxBinding` 带 `last_heartbeat`；进程每 30s 更新一次运行中租约。
- 启动时与每 10 分钟对账：标签匹配但 DB 无记录 → 记 `SANDBOX_ORPHAN`，超过 TTL 才删；DB 记录 running 但容器不存在 → 置 `degraded` 并告警，不自动重建（避免掩盖故障）。
- 容器回收只按标签，绝不 `docker prune -a`。

## 7. 与记忆体系的集成

### 7.1 scope 扩展

`LongTermPolicy.scope` 从 `Literal["session","user"]` 扩为 `Literal["session","user","workspace"]`（`memory_policy.py:48`）。`MemoryRuntime` 增加可选 `workspace_id`，`scope_id` 解析改为：

```python
def _scope_id(policy, session_id, workspace_id):
    if policy.long_term.scope == "workspace":
        if not workspace_id:
            raise ScopeUnavailable("workspace 作用域缺少 workspace_id")
        return f"workspace:{workspace_id}"
    if policy.long_term.scope == "user" and policy.user_id:
        return policy.user_id
    return f"session:{session_id}"
```

装配点在 `runtime/assembly.py:183`（`MemoryRuntime(session_id, policy)`），改为多传一个 `workspace_id`；该值由会话绑定的工作区提供，来源是 `workspace_sessions`。**不改变 `memory_policy` 的默认值**（仍 `session`），只有工作区绑定后才切到 `workspace`，避免未绑定会话的行为漂移。

### 7.2 三层归属

| 层 | 归属 | 跨会话 | 工作区隔离方式 |
|---|---|---|---|
| 短期 | 会话 | 否 | 会话独立，天然隔离 |
| 中期 `session_memories` | 会话 | 否 | 加 `workspace_id` 列，仅用于审计与删除 |
| 长期 `long_term_memories` | **工作区** | 是 | `scope='workspace'` + `scope_id='workspace:<id>'` |

中期记忆不改成工作区级：它是当前会话的压缩摘要，跨会话复用会引入「上一会话的假设」。需要保留的结论应在会话结束时以 `kind='session_summary'` 晋升到长期（P2，可开关）。

### 7.3 迁移与兼容

- 已有 `scope='session'` 的长期条目**不自动复制**。绑定工作区后，会话读到的是新 scope，旧条目留在原 scope。
- 提供 `workspace migrate-memory --session <id> --to <workspace_id>`：把该会话旧条目复制到工作区 scope，保留原条目（幂等，按 text 去重）。
- 若用户希望「历史会话继续可用」，P3 提供反向视图：工作区查询时合并 `workspace:{id}` 与显式迁移过的 `session:{id}`，**不做隐式合并**。
- `gateway/routes/memory.py` 的 `_scope_id`（`gateway/routes/memory.py:24-28`）必须改为真正按 scope 分派，并接受 `workspace_id` 查询参数。

### 7.4 红线

- 跨工作区检索命中数必须为 0；由单测与评测双重验证（A4）。
- 工作区删除时，长期记忆默认一并删除（隐私优先）；`--keep-memory` 仅在 P2 提供，且要求审计记录。
- 记忆 API 只能按工作区前缀查询，禁止把原始 SQL 暴露给前端；参数仍走 pydantic 校验。

## 8. 与沙箱 / Docker 体系的集成

### 8.1 新增执行面端口

现状 `executor/boundary.py` 只有两个工厂，没有任何生命周期。新增 `executor/workspace.py`：

```python
class WorkspaceBackend(Protocol):
    name: str                                  # "local" | "docker"
    def provision(self, spec: SandboxSpec) -> SandboxHandle: ...
    def start(self, handle: SandboxHandle) -> SandboxHandle: ...
    def stop(self, handle: SandboxHandle, *, remove: bool) -> None: ...
    def health(self, handle: SandboxHandle) -> SandboxHealth: ...
    def exec(self, handle: SandboxHandle, argv: list[str], *, timeout: int) -> ExecResult: ...
```

两个适配器：

- `LocalBackend`：直接返回 `build_local_workspace(root_path, skill_paths)`（`executor/boundary.py:14-19`），`exec` 走 `subprocess` + `timeout` + 输出截断；不提供额外隔离，仅用于开发与 P0。
- `DockerBackend`：包装 `build_docker_workspace(...)`（`executor/boundary.py:22-28`），补标签、挂载、资源限制、健康检查；`exec` 走 `docker exec`。

> **NOT RUN**：本次环境未安装 `agentscope`，`DockerWorkspace` 的真实构造参数、`exec` 能力、挂载语义无法核实。实现前必须 introspect：`python -c "import inspect,agentscope.workspace as w; print(inspect.signature(w.DockerWorkspace.__init__)); print(inspect.getsource(w.DockerWorkspace))"`。在核清之前不得按本 spec 的字段名臆造调用。

### 8.2 容器规格

| 项 | 值 | 理由 |
|---|---|---|
| 标签 | `devops.managed=1`、`devops.workspace=<id>`、`devops.image_digest=<digest>` | GC 与审计的唯一依据 |
| 用户 | 非 root（镜像内 UID/GID 固定） | 降低逃逸收益 |
| 根文件系统 | 只读 | 只有 `repo/`、`data/`、`artifacts/` 可写 |
| 挂载 | `repo` rw、`data` rw、`artifacts` rw、`skills` ro | 与本地后端同一目录语义 |
| 网络 | 默认 `none`；需要装包时走显式 `egress` 开关 | 与 `PermissionProfile.net` 分离，不偷偷出网 |
| 资源 | `cpu_limit`、`mem_limit_mb`、`pids_limit` 必填 | 防止 fork bomb / 内存打爆宿主 |
| 能力 | `--cap-drop ALL`、`no-new-privileges`、默认 seccomp | 不自研 seccomp |
| 密钥 | **绝不进容器 env / 镜像层** | 继续由 `SecretInjectionMiddleware` 在工具边界替换 |
| 镜像 | 记录 `image_digest`，变更即需重建绑定 | 避免「同一个 tag 悄悄换内容」 |

### 8.3 容器与工具执行的关系

今天工具直接用 `LocalBackend(workdir)` 在宿主进程里跑（`tools/catalog.py:110,124-139`）。Docker 后端有两种落法，本 spec 选 **A**：

- **A（采用）**：文件工具（Read/Glob/Grep/Write/Edit）继续作用于宿主上的挂载目录；只有 `Bash` 改由 `DockerBackend.exec` 在容器内执行。理由：AgentScope 文件工具已经锚定 workdir 且安全判定在宿主 realpath 上完成（`tools/guard.py:51-56`），改一套远程文件协议收益小、风险大。
- **B（不采用）**：所有工具 RPC 进容器。需要重写工具后端协议，超出本期范围。

因此 `build_toolkit_for` 需要接受一个可选的 `shell_exec` 适配器；未提供时行为不变（P0 与 local 后端）。

### 8.4 健康检查与降级

- `health`：本地检查目录可读写；Docker 检查 `docker inspect` 的 `State.Running` + 一次 `true` 探针。
- 启动失败：P0 直接 503；P1 若配置 `allow_local_fallback=true`，降级 local 并把 `SandboxBinding.status=degraded`、`backend=local`，**必须**在状态接口与前端横幅显示，不允许静默降级（沿用 `bootstrap.warn_runtime` 的形状，`gateway/bootstrap.py:127-134`）。

## 9. 存储与迁移

沿用观测库（stdlib sqlite3，零新依赖，`observability/repository/db.py:3-6`）。新增表追加在 `_SCHEMA` 末尾，老库通过 `init_db` 幂等建表：

```sql
CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT 'local',
    root_path       TEXT NOT NULL,
    default_profile TEXT NOT NULL DEFAULT 'workspace',
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','archived','deleted')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_name ON workspaces(name)
    WHERE status != 'deleted';

CREATE TABLE IF NOT EXISTS workspace_repositories (
    workspace_id   TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('git','plain')),
    remote_url     TEXT NOT NULL DEFAULT '',
    branch         TEXT NOT NULL DEFAULT '',
    base_commit    TEXT NOT NULL DEFAULT '',
    credential_ref TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_sandboxes (
    workspace_id   TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    backend        TEXT NOT NULL CHECK (backend IN ('local','docker')),
    image          TEXT NOT NULL DEFAULT '',
    image_digest   TEXT NOT NULL DEFAULT '',
    container_id   TEXT NOT NULL DEFAULT '',
    host_workdir   TEXT NOT NULL DEFAULT '',
    cpu_limit      REAL NOT NULL DEFAULT 1.0,
    mem_limit_mb   INTEGER NOT NULL DEFAULT 1024,
    pids_limit     INTEGER NOT NULL DEFAULT 256,
    network        TEXT NOT NULL DEFAULT 'none' CHECK (network IN ('none','egress')),
    status         TEXT NOT NULL DEFAULT 'stopped'
                   CHECK (status IN ('stopped','starting','running','degraded','error')),
    last_heartbeat TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS workspace_sessions (
    session_id   TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    bound_at     TEXT NOT NULL,
    bound_by     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_workspace_sessions_ws ON workspace_sessions(workspace_id);
```

迁移（`_migrate`，`observability/repository/db.py:261-268` 同形状）：

- `session_states` 加列 `workspace_id TEXT NOT NULL DEFAULT ''`（`_SESSION_COLUMNS`，`db.py:209-212`）。
- `session_memories` 加列 `workspace_id TEXT NOT NULL DEFAULT ''`，仅审计；长期表**不改结构**，用 `scope/scope_id` 表达。
- `spans.type` 的 CHECK 加 `workspace`、`sandbox`：沿用现有「重建表 + 原样复制」迁移（`db.py:270-290`），DDL 与 `_SPANS_DDL`（`db.py:32-36`）逐字一致。
- 不建 `memory_scope` 表：scope 是逻辑投影，建表会产生第二份真相。

## 10. 接口

### 10.1 HTTP（`gateway/routes/workspaces.py`，新）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workspaces` | 列表（含沙箱状态摘要） |
| POST | `/api/workspaces` | `{name, default_profile}` → 201 |
| GET | `/api/workspaces/{id}` | 详情 + 仓库 + 沙箱 + 会话数 |
| PATCH | `/api/workspaces/{id}` | 改名 / 默认档位 |
| DELETE | `/api/workspaces/{id}` | `?force=false`；409 见 §6.1 |
| POST | `/api/workspaces/{id}/repo` | `{kind, remote_url?, branch?, credential_ref?}` |
| POST | `/api/workspaces/{id}/sandbox/start` | 幂等 |
| POST | `/api/workspaces/{id}/sandbox/stop` | `{remove: bool}` |
| GET | `/api/workspaces/{id}/status` | 心跳、容器、磁盘、记忆条数 |
| POST | `/api/session/{session_id}/workspace` | `{workspace_id}` 绑定；409 已绑其他 |
| GET | `/api/workspaces/{id}/memory` | 复用记忆查询，固定 `scope=workspace` |

错误码沿用现有语义：404 不存在、409 状态冲突、422 参数非法、503 后端不可用。所有路由不得 import `gateway.app`（`.importlinter` 契约 `gateway-routes-no-app`）。

### 10.2 请求链路

`RunRequest` 加 `workspace_id: str | None = None`（`gateway/routes/schemas.py:17-29`）；`gateway/turn/lease.py` 解析后把 `workspace_id` 交给 `SessionRegistry.acquire`，后者在 `_build` 时把它传给 `build_agentscope_runtime(..., workspace_id=...)`（`runtime/sessions.py:120-134`、`runtime/assembly.py:249-280`）。未解析到工作区时保持旧签名，`workspace_id=None`。

### 10.3 CLI（`python -m devops_agent.workspace`）

```
workspace list
workspace create --name <n> [--profile workspace]
workspace attach <id> --kind git --remote <url> [--credential <ref>]
workspace bind <id> --session <sid>
workspace start|stop <id> [--remove]
workspace status <id>
workspace archive <id>
workspace delete <id> [--force]
workspace gc [--dry-run]
workspace migrate-memory --session <sid> --to <id>
```

CLI 只调用同一组服务函数，不复制业务逻辑；`--dry-run` 默认用于 `gc`。

### 10.4 前端

- 新增路由 `/workspaces`、`/workspaces/:id`（`web/src/App.tsx:21-27` 追加），侧边栏加入口。
- 聊天页顶部显示当前工作区切换器；未绑定时显示「绑定工作区」引导，而不是静默落默认。
- 工作区详情页展示：仓库（branch / base commit / dirty）、沙箱（backend / status / image digest / 心跳）、记忆条数、最近会话。
- 改动 `web/**` 必须先按 AGENTS §3.8 读取 `skills/modern-web-guidance/SKILL.md` 并检索；本 spec 不含前端实现细节。

## 11. 配置

新增 `workspace` 配置域（`config_store/registry.py:57-107`），模型 `WorkspacePolicy` 放 `config_schema.py`：

```python
class WorkspacePolicy(BaseModel):
    enabled: bool = False
    root: str = ""                     # 空 = workspace/workspaces
    backend: Literal["local", "docker"] = "local"
    image: str = "python:3.11-slim"
    allow_local_fallback: bool = True
    max_workspaces: int = Field(default=10, ge=1, le=200)
    idle_stop_minutes: int = Field(default=30, ge=1, le=10080)
    require_clean_on_delete: bool = False
    default_profile: str = "workspace"
    container: ContainerLimits = Field(default_factory=ContainerLimits)
```

环境变量（`ENV_OVERLAY`，`config_store/registry.py:115-122`）：

| 变量 | 键 | 取值 |
|---|---|---|
| `DEVOPS_AGENT_WORKSPACES_ENABLED` | `enabled` | bool |
| `DEVOPS_AGENT_WORKSPACE_ROOT` | `root` | str |
| `DEVOPS_AGENT_WORKSPACE_BACKEND` | `backend` | local / docker |
| `DEVOPS_AGENT_WORKSPACE_IMAGE` | `image` | str |

生效时机 `restart`（后端与镜像在启动期固定），与 `gateway` 域一致（`registry.py:80-87`）。`enabled=false` 时所有新路由返回 404，运行路径完全走旧逻辑。

## 12. 安全

| 风险 | 控制 |
|---|---|
| 跨工作区数据泄漏 | I3 + `scope_id` 强校验；A4 用两个工作区互查断言 0 命中 |
| 路径穿越 / 符号链接逃逸 | id 正则 + `realpath` + `is_relative_to`；拒绝任何 `..` 段 |
| 容器逃逸 | 非 root、只读根、cap-drop、no-new-privileges、pids/mem/cpu、默认 seccomp；绝不挂 docker socket |
| 密钥泄漏 | 凭据只存 `credential_ref`，clone 时经 askpass 临时注入；容器 env 不含密钥；日志脱敏沿用 `redaction/` |
| `.git` 读写与权限矛盾 | Agent 不直接读 `.git`（`governance/paths.py:58` 保持）；仓库状态走 `GitFacade` 白名单子命令 |
| 恶意仓库钩子 | clone 后禁用 hooks（`core.hooksPath=/dev/null`），checkout 前校验目标路径 |
| 资源耗尽 | 每工作区限额 + 全局 `max_workspaces` + 容器 pids/mem 限制 + 磁盘配额检查 |
| 误删 | 删除进 trash 保留 7 天；脏目录先导 patch；`--force` 需显式 |

## 13. 可观测

- 事件：`WORKSPACE_CREATE_START/END`、`WORKSPACE_REPO_ATTACH_*`、`SANDBOX_PROVISION_*`、`SANDBOX_START/STOP_*`、`SANDBOX_EXEC_*`、`SANDBOX_ORPHAN`、`SANDBOX_HEARTBEAT_LOST`、`MEMORY_SCOPE_BOUND`。
- spans：`type` 增加 `workspace`、`sandbox`（§9 的 CHECK 迁移）；`name` 取动作名，`input/output` 存脱敏后的参数与摘要。
- 指标（`/api/workspaces/{id}/status` 与 `/health`）：
  - `provision_ms` p50/p95；
  - `exec_ms` p50/p95（容器内命令）；
  - `container_restarts`、`orphan_containers`；
  - `workspace_disk_bytes`；
  - `memory_entries`（按工作区）；
  - `leak_hits`（必须恒为 0）。
- 失败必须可见：降级 backend、心跳丢失、GC 失败都写事件并进入 `/health` 摘要，不只在日志里。

## 14. 门禁、测试与验收

### 14.1 验收项

| 编号 | 验收 | 方式 |
|---|---|---|
| A1 | 创建本地工作区，`repo/` 存在且 workdir 解析正确 | 单测 + CLI |
| A2 | 绑定会话后 `session_states.workspace_id` 落库，刷新重建仍归属同一工作区 | 单测 |
| A3 | 同一会话改投其他工作区返回 409 | 单测 |
| A4 | 两个工作区写入不同事实，互查命中数为 0 | 单测（硬红线） |
| A5 | 长期记忆 `scope='workspace'`、`scope_id='workspace:<id>'`；中期/长期表带 `workspace_id` | 单测 + SQL 断言 |
| A6 | 未带 `workspace_id` 的请求行为与改动前一致（提示词字节、workdir 不变） | 回归测试 |
| A7 | 脏目录删除：非强制时先出 patch；强制时 409 且不删除 | 单测 |
| A8 | 路径穿越 `../`、绝对路径、符号链接均 400/denied | 参数化单测 |
| A9 | Docker 容器带标签、非 root、只读根、限额、无密钥 env | 集成测试（有 docker 时）；无 docker 标 SKIP 并打印原因 |
| A10 | 停止/删除后容器被清理，孤儿容器按 TTL 回收且不误删他人容器 | 集成测试 |
| A11 | `workspace` 配置域可读、越界值 400、env 覆盖生效、`enabled=false` 路由 404 | 单测 |
| A12 | 工作区动作写 span/事件，`/status` 指标非空 | 单测 |
| A13 | 前端工作区列表/切换/状态展示可用 | 前端测试（需先读 modern-web-guidance） |
| A14 | 评测用例可分配临时工作区与独立记忆 scope，用例结束清理 | openjudge 单测 |
| A15 | `workspace gc --dry-run` 只报告不删除；无标签容器永不处理 | 单测 |
| A16 | 后端降级 local 时 `/health` 与前端都显示 degraded | 单测 |

### 14.2 质量门禁

沿用仓库现有门禁：`ruff`、`mypy src`、`vulture`、`pytest --cov-fail-under=80`、`check_file_size.py`（新文件 ≤500 行）、`lint-imports`。新增契约：

- `workspace` 领域层（`devops_agent/workspace/**`）不得 import `agentscope`（业务模型与存储纯 stdlib + pydantic）；
- `executor` 仍是唯一允许接触 `agentscope.workspace` 的层；
- `observability` 不得反向依赖 `workspace`（沿用 `observability-no-gateway` 的形状）。

## 15. 分阶段落地

| 阶段 | 内容 | 交付物 | 验收 |
|---|---|---|---|
| P0 | 对象模型、表结构与迁移、Local 后端、创建/绑定/归档/删除、记忆 scope、API + CLI、默认工作区兼容 | `workspace/{models,store,service}.py`、`executor/{boundary,workspace}.py`、`gateway/routes/workspaces.py`、迁移 | A1–A8、A11、A12、A15、A16 |
| P1 | Docker 后端：标签、挂载、限额、健康检查、GC、spans/事件、Shell 适配 | `executor/workspace.py` 的 `DockerBackend`、`SANDBOX_*` 事件、集成测试 | A9、A10、A12 |
| P2 | 多成员与配额（需先有鉴权）、会话摘要晋升、凭据引用增强、删除保留策略 | `workspace/members.py`、权限中间件、审计 | 成员隔离、配额与晋升用例 |
| P3 | 旧会话迁移、在线再绑定的离线流程、归档恢复、备份/恢复演练、评测临时工作区 | 迁移 CLI、`openjudge` 适配 | A14 + 迁移回归 |

依赖顺序：P0 必须先合并并稳定，P1 才能替换执行面；P2 依赖鉴权，未落地前不排期。

## 16. 兼容、回滚与风险

### 16.1 兼容

- 默认 `workspace.enabled=false`：不建立默认工作区、不解析 `workspace_id`、不新增必填参数。
- 所有新增表/列都是追加式；老库 `init_db` 幂等升级，不需要停机脚本。
- `RunRequest.workspace_id` 可选；旧前端不传时不产生 422。
- 记忆默认 scope 不变；只有绑定工作区的会话才用 `workspace`。

### 16.2 回滚

1. 设 `DEVOPS_AGENT_WORKSPACES_ENABLED=0` 并重启 → 新路由 404、运行链路回到旧路径。
2. 数据保留：新增表可原样留着，不影响旧代码；`spans.type` 迁移是向前兼容的（旧代码只写四类）。
3. 记忆：已写入 `workspace` scope 的条目在关闭开关后不会被读取，但不会丢失；需要恢复时用 `migrate-memory --to session` 导出（P3 提供）。
4. 容器：先 `workspace stop --all`，再关开关；`workspace gc` 可清孤儿。

### 16.3 主要风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| `DockerWorkspace` 真实 API 与假设不符（本次 NOT RUN） | 高 | P1 开工前先 introspect；本 spec 的字段名以实测为准，契约层不提前固化 |
| 文件工具在宿主、Bash 在容器，路径语义不一致 | 中 | 两边都基于同一挂载路径；A9 覆盖 `pwd`、相对路径、写文件后 `cat` 一致性 |
| 记忆 re-scope 导致「历史失忆」 | 中 | 默认不自动迁移；提供显式迁移命令；A6 兼容断言 |
| 单机 SQLite 写竞争 | 中 | 沿用 WAL + busy_timeout（`db.py:242-244`）；工作区动作低频，不加新锁 |
| 命名冲突（权限档也叫 workspace） | 低 | §3.1 消歧；界面文案改「项目可写」，key 不变 |
| 容器逃逸/资源耗尽 | 高 | §12 的默认值；不在容器里挂 socket；限额必填 |

## 17. 实施前必须核实（NOT RUN）

1. `agentscope.workspace.DockerWorkspace` 的构造参数、`exec` 能力、挂载与网络控制项、容器命名/标签支持 —— 本次环境未安装 `agentscope`，无法读取源码。
2. `agentscope.workspace.LocalWorkspace` 是否自带 `exec`；若没有，local 后端的 `exec` 必须由本仓库实现（`subprocess`）。
3. 现有 `build_toolkit_for` 是否能在不破坏安全守卫的前提下接受外部 shell 适配器（`tools/catalog.py:110`）。
4. `session_states.workspace_id` 与 `SessionRegistry` 重建路径的字段兼容（`runtime/sessions.py:120-134`）。

以上四项完成后再进入 P0 编码，避免按记忆猜 API 造出第二套真相（AGENTS §3.4）。

## 18. 术语表

| 术语 | 含义 |
|---|---|
| 工作区 / Workspace | 本 spec 引入的领域实体，1 仓库 + 1 执行目录 + 1 记忆 scope + 1 沙箱 |
| 执行目录 / workdir | Agent 工具的相对路径基准与 shell cwd |
| 仓库 / Repository | 工作区内的代码根；可 git 或 plain |
| 沙箱绑定 / SandboxBinding | 工作区与执行后端（local/docker）的绑定记录 |
| 记忆作用域 / MemoryScope | 长期记忆的 `(scope, scope_id)`；工作区为 `scope='workspace'`、`scope_id='workspace:<id>'` |
| 默认工作区 / ws_default | 兼容旧部署的懒创建工作区，指向 `DEVOPS_AGENT_WORKDIR` |
| 容器租约 / Sandbox lease | 运行中容器的所有权记录，带心跳与 TTL |
