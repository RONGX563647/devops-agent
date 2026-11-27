# SPEC-0018：沙箱与 Docker 执行面（复用 AgentScope Workspace）

- 状态：草案（待评审）
- 日期：2026-11-27
- 基线：`origin/main 27cc2be`
- 关系：落实 AGENTS §3.2「编排与执行分离」与 §3.4「能力用内置、不手搓」；与 SPEC-0010（权限单源）、ADR-0002（权限模型）共用同一套 `PermissionContext`；不改记忆、评测与确认协议
- 范围：`executor/`（新增执行目标与生命周期管理）、`tools/catalog.py`、`runtime/{assembly,prompt,scope_runtime}.py`、`governance/compile.py`、`config_schema.py`、`config_store/registry.py`、`gateway/{app,bootstrap}.py`、`gateway/routes/health.py`、`web/src/types/health.ts`、新增测试与 ADR
- 触发来源：对 AgentScope 2.x Workspace / DockerWorkspace 实现的一次走查；发现本仓库已有执行面构造函数但生产路径从未接线

## 1. 设计结论

1. **本仓库不实现容器编排，只实现「选择 + 生命周期 + 配置 + 路径映射」**。容器创建、镜像构建、文件读写、MCP gateway 全部由 `agentscope.workspace.DockerWorkspace` 承担；仓库侧代码只决定「用哪个后端、何时初始化/关闭、给模型看什么路径」。
2. **沙箱不是「换一个工具集」，而是换一个执行后端**。现有工具（Bash/Read/Write/Edit/Glob/Grep）已经通过 `ToolBase.backend` 执行；把 `LocalBackend` 换成 `DockerWorkspace.get_backend()` 即可让整套工具在容器内运行，权限规则与中间件不需要重写。
3. **必须引入「宿主路径 / 工具路径」双视图**。`DockerWorkspace` 把宿主 `workdir` 挂到容器 `/workspace`；模型与工具看到的是 `/workspace`，而挂载、审计、文件归属仍是宿主路径。系统提示、工具 `cwd`、权限 `working_directories`、敏感路径守卫必须统一使用工具路径，否则 `accept_edits` 会把容器内写文件判成「工作区外」，每步都要人工确认。
4. **生命周期是异步的，装配路径目前是同步的**。`workspace.initialize()` 是 coroutine，而 `build_agentscope_runtime()` / `SessionRegistry.acquire()` 是同步函数。方案是：FastAPI lifespan 内完成初始化，之后同步装配只消费「已就绪」的执行目标；不把整个会话注册表改成异步（改动面过大）。
5. **降级必须可见，且默认不改变现有行为**。默认 `mode=local`，部署显式切 `docker`；`auto` 仅在 Docker 不可用时回落本机，并在 `/health.sandbox` 与启动日志写明原因。`required=true` 时拒绝回落——静默从容器降到宿主是安全事故，不是可用性优化。
6. **内置 DockerWorkspace 的隔离边界要如实标注**。2.0.x 构造签名不暴露 `network_mode` / CPU / 内存 / user / cap-drop / 只读 rootfs；本方案 P1 获得的是「容器 namespace + 文件系统 + 独立 rootfs」隔离，**网络仍是 bridge 出网、进程仍是 root**。硬网络隔离与资源限额作为 P2 向上游贡献，不在本仓库 fork Docker 创建逻辑。
7. **工作区仍是「进程级共享」而非「会话级隔离」**。当前 `DEVOPS_AGENT_WORKDIR` 本来就是全局目录；P1 沿用这一语义（一个 workdir 一个容器），多租户/每会话隔离不在本方案范围。
8. **评测路径单独处理**。OpenJudge 每个 case 有独立临时 workdir，必须按 case 创建/关闭工作区（`async with`），不能复用聊天用的进程级单例。

## 2. 现状证据（改动前）

| 编号 | 断言 | 证据 | 复现 |
|---|---|---|---|
| E-1 | 执行面构造函数存在，但生产路径从未调用 | `src/devops_agent/executor/boundary.py:11` 定义 `build_local_workspace` / `build_docker_workspace`；全仓库唯一调用方是 `tests/unit/test_tools_boundary.py:9` | `rg "build_local_workspace\|build_docker_workspace" --glob '*.py'` 只命中 boundary 与测试 |
| E-2 | 工具实际跑在宿主 | `src/devops_agent/tools/catalog.py:104-118` 每个工具注入 `_WorkdirBackend`（`LocalBackend` 子类）；`catalog.py:138-143` 只接受宿主 `workdir` | 放开档执行 `pwd`，输出宿主目录 |
| E-3 | 运行时同步装配工具 | `src/devops_agent/runtime/assembly.py:211-217` 在 `_build_agent` 内直接 `build_toolkit_for(...)`；`assembly.py:293-309` 会话工厂是同步函数 | 无法在 `SessionRegistry.acquire()` 中 `await workspace.initialize()` |
| E-4 | 系统提示只写宿主路径 | `src/devops_agent/runtime/prompt.py:28` 把 `{workdir}` 写进工具说明；`prompt.py:41-45` 的 `resolve_workdir()` 返回宿主绝对路径 | Docker 模式下模型被告知宿主路径，容器内不存在 |
| E-5 | 权限只登记宿主工作目录 | `src/devops_agent/governance/compile.py:94-111` 把宿主 `workdir` 塞进 `working_directories` | 模型传 `/workspace/app.py` 时被判为工作区外 |
| E-6 | 真实运行时在 `create_app()` 阶段就装配 | `src/devops_agent/gateway/app.py:78` 同步调用 `pick_runtime()`；`gateway/bootstrap.py:25-29` 内同步 `build_agentscope_runtime()` | 进程启动时还没有事件循环可跑 `workspace.initialize()` |
| E-7 | `/health` 没有执行面状态 | `src/devops_agent/gateway/routes/health.py:53-72` 只有 runtime / config / memory / tools | Docker 不可用或回落本机时，界面只能看到「工具失败」 |
| E-8 | 配置中心没有 sandbox 域 | `src/devops_agent/config_store/registry.py:57` 起的 `DOMAINS` 只有 health/input/memory/gateway/policy/model…；`config_schema.py` 无 `SandboxPolicy` | `GET /api/config/sandbox` 不存在 |

## 3. 目标与非目标

### 3.1 目标

- 引入 `ExecutionTarget`：一次装配所需的全部执行面信息（模式、宿主目录、工具目录、backend、shell、工作区句柄）。
- 引入 `SandboxManager`：按 workdir 管理 `LocalWorkspace` / `DockerWorkspace` 的初始化、复用、TTL 与关闭；启动预热、关闭清理。
- 让 `tools/catalog.py` 接受注入的 backend 与工具 cwd，保留现有 guard / secret 中间件与只读档工具集语义。
- 新增 `sandbox` 配置域（代码默认 + config.db 覆盖 + env 覆盖），默认 `local`。
- `/health.sandbox` 暴露模式、实际生效后端、降级原因、容器/镜像标识、容器内依赖探测结果。
- 评测 harness 可按 case 创建/销毁独立工作区。
- 全量单测覆盖模式解析、降级、路径映射、权限目录与 backend 注入；Docker 集成测试在无 daemon 时跳过。

### 3.2 非目标

- 不自研容器运行时、镜像构建器、MCP gateway 或文件同步；不复制 AgentScope 的 Docker 实现。
- 不做 E2B / Daytona / K8s / OpenSandbox 接入（保留 `mode` 枚举扩展位，不在本期实现）。
- 不做多租户、每会话容器、容器内网络隔离与资源配额（P2 / 上游）。
- 不改权限模型、确认协议、记忆层与前端交互；前端只增加健康类型字段（可选）。
- 不接管 OpenJudge 自己的评测沙箱（`openjudge/sandbox.py` 保持现状），只让 Agent 执行面可复用。

## 4. 架构

```text
                    ┌──────────────────────────────┐
配置域 sandbox ───▶ │ SandboxPolicy（模式/镜像/TTL）│
                    └──────────────┬───────────────┘
                                   ▼
   DEVOPS_AGENT_WORKDIR ──▶ SandboxManager.ensure(host_workdir)
                                   │  (async, 幂等, 按 key 加锁)
                    ┌──────────────┴───────────────┐
                    ▼                              ▼
             LocalWorkspace                 DockerWorkspace
             LocalBackend                   DockerBackend
                    └──────────────┬───────────────┘
                                   ▼
                          ExecutionTarget
              { mode, host_workdir, tool_workdir, backend,
                workspace, shell, degraded_reason }
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
 build_toolkit_for()        build_system_prompt()      compile_context()
 backend + tool cwd         /workspace + /bin/sh       /workspace 登记
```

### 4.1 `ExecutionTarget`（新文件 `executor/target.py`）

```python
@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    mode: Literal["local", "docker"]   # 实际生效模式，不是请求模式
    host_workdir: Path                 # 挂载/审计/文件归属
    tool_workdir: str                  # 模型与工具看到的路径
    backend: BackendBase               # 工具执行原语
    shell: str                         # 系统提示里的 shell 语法
    workspace: WorkspaceBase | None = None
    degraded_reason: str = ""          # auto 回落时非空
```

约束：

- `mode=local`：`tool_workdir == str(host_workdir)`，`shell` 沿用 `catalog.shell_name()`。
- `mode=docker`：`tool_workdir == "/workspace"`，`shell == "/bin/sh"`；宿主目录只用于挂载与审计。
- `backend` 必须来自已初始化的 workspace（`workspace.get_backend()`；实现前用 introspection 确认 2.0.x 真实签名，禁止凭记忆写 API）。

### 4.2 `SandboxManager`（新文件 `executor/manager.py`）

职责只有四件：

1. `async ensure(host_workdir: Path) -> ExecutionTarget`：按 `(mode, resolved_host_workdir, image_tag)` 缓存；同 key 用 `asyncio.Lock` 防并发重复创建；命中直接返回。
2. `async close(host_workdir)` / `async close_all()`：调用 `workspace.close()`；异常只记录不抛出。
3. TTL 回收：`ttl_seconds` 内未使用的非聊天工作区（评测 case）由后台任务关闭；聊天主工作区不回收。
4. 启动探测：`docker_available()` 用 `aiodocker.Docker().version()` 或 `docker info` 探测；失败返回原因，不抛到调用方。

为什么不用 `agentscope.app._manager.DockerWorkspaceManager`：它位于私有模块 `agentscope.app._manager`，隔离键是 `(user_id, agent_id)`，与本仓库「按 workdir 复用 + 评测按 case 销毁」的语义不一致；直接依赖私有 API 会在小版本升级时断。这里只做薄生命周期层，不复制容器逻辑。

### 4.3 工具装配（改 `tools/catalog.py`）

```python
def build_toolkit_for(
    names, workdir, middlewares=None, *, scope="workspace",
    extra=None, backend_factory=None, tool_cwd=None,
) -> Toolkit:
    cwd = tool_cwd or str(Path(workdir).resolve())
    ...
    "bash": lambda: Bash(cwd=cwd, middlewares=mws, backend=backend_factory()),
    "read": lambda: Read(middlewares=mws, backend=backend_factory()),
```

- `backend_factory` 省略时保持现有 `_WorkdirBackend(cwd)`，本机行为逐字节不变。
- Docker 模式传入 `lambda: target.backend`（每工具共享同一个 DockerBackend；它本身无会话状态）。
- guard 中间件用 `tool_cwd` 作为边界：容器模式下「工作区内」= `/workspace`，不是宿主目录。
- 不使用 `workspace.list_tools()`：那会绕过本仓库的 secret 注入与 guard 中间件；`get_backend()` + 现有构造器能同时保留两者。

### 4.4 系统提示与权限（改 `runtime/prompt.py`、`governance/compile.py`）

- `build_system_prompt(tool_workdir, shell)`：Docker 模式渲染 `/workspace` 与 `/bin/sh`，不再渲染宿主路径。
- `compile_context(profile, workdir=tool_workdir)`：Docker 模式登记 `/workspace` 为附加工作目录；宿主路径不再登记（避免 `accept_edits` 对容器路径失效）。
- `AgentScopeRuntime` 持有 `ExecutionTarget`，切档重建工具时复用同一 backend 与 tool cwd（热替换不能把执行面换回宿主）。

### 4.5 启动与关闭（改 `gateway/app.py`、`bootstrap.py`）

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = SandboxManager(load_sandbox())
    target = await manager.ensure(resolve_workdir())
    app.state.execution = target
    app.state.runtime = pick_runtime(execution=target)   # 初始化后再同步装配
    try:
        yield
    finally:
        await manager.close_all()
```

- `create_app(runtime=...)` 的显式注入路径不变（测试/部署替换 runtime 仍可用）。
- `create_app(runtime=None)` 在 lifespan 之前不装配真实 Agent：`app.state.runtime` 先放 `EchoRuntime()` 占位；lifespan 内替换。生产 ASGI 一定先跑 lifespan，不影响请求。
- 若 `required=true` 且初始化失败：记录 `app.state.sandbox_error`，`/health` 返回 `sandbox.degraded=true`，对话端点返回 503 `sandbox_unavailable`；不回落宿主。
- 若 `auto` 且 Docker 不可用：`ensure()` 返回 local target + `degraded_reason`，启动日志 warning，`/health` 可见。

## 5. 配置契约

### 5.1 `config/sandbox.yaml`（新增；代码默认值与文件逐项对账）

```yaml
sandbox:
  # local | docker | auto
  # local：现状行为；docker：强隔离，不可用即失败；auto：优先 docker，失败回落 local
  mode: local
  # true：初始化失败不回落，对话端点 503；生产 docker 部署建议 true
  required: false
  # 非聊天工作区（评测 case）空闲回收；聊天主工作区不回收
  ttl_seconds: 900
  docker:
    base_image: python:3.11-slim
    # 额外 Python 包装进 gateway venv（不是宿主环境）
    extra_pip: []
    # 显式透传进容器的环境变量；禁止放密钥（密钥仍走 SecretInjectionMiddleware）
    env: {}
    # 容器内 gateway 端口；不映射到宿主机
    gateway_port: 8765
    # 关闭时删除容器；宿主 workdir 永久保留
    remove_container_on_close: true
```

### 5.2 类型与域

- `config_schema.py` 新增 `SandboxDockerPolicy` / `SandboxPolicy`（pydantic，叶子，无 IO）。
- `config_store/registry.py` 的 `DOMAINS` 追加 `DomainSpec(key="sandbox", hot="restart")`：执行面在进程启动期固定，改完需重启；热更新到一半会出现「半进程用容器、半进程用宿主」。
- 环境变量覆盖（测试与 CI 用）：`DEVOPS_AGENT_SANDBOX_MODE=local|docker|auto`、`DEVOPS_AGENT_SANDBOX_REQUIRED=1`。
- 校验：`mode` 非法在启动期 `ValueError`（与配置模式拼错同策略）；`ttl_seconds <= 0` 拒绝；`base_image` 空串拒绝。

### 5.3 默认值取舍

- 默认 `local`：本仓库当前是单机开发工具，默认切容器会让所有开发/测试环境突然依赖 Docker daemon，属于破坏性变更。
- 部署文档建议：服务器/对外服务设 `mode=docker` + `required=true`；本地开发保持 `local`；CI 用 `local`（Docker 集成测试单独 job）。
- `auto` 只作为过渡：它的价值是「装了 Docker 就自动隔离」，代价是同一份配置在不同机器行为不同；生产不要用。

## 6. 生命周期与并发

| 场景 | 工作区粒度 | 创建时机 | 关闭时机 |
|---|---|---|---|
| 聊天 | 进程级（按 `DEVOPS_AGENT_WORKDIR`） | lifespan 启动 | 进程退出 |
| 切档/热替换 | 复用同一工作区 | 不重建 | 不关闭 |
| 评测 case | 每 case 一个临时 workdir | `async with manager.workspace(tmpdir)` | case 结束立即关闭 |
| 初始化失败 | 不缓存失败结果 | 下次请求重试（带退避） | — |

- `ensure()` 幂等；同 key 并发请求只创建一个容器，其余等锁后复用。
- 容器意外退出：`backend.exec_shell` 返回非零/连接错误时，manager 标记该 target 失效；下一次 `ensure()` 重建容器（宿主 workdir 保留，文件不丢）。不在工具调用中途自动重试，避免重复执行写操作。
- 会话状态仍在 SQLite 快照里，与容器生命周期解耦；容器重启不需要恢复会话。
- 容器命名由 AgentScope 负责（`as_ws_<workspace_id>`）；本仓库记录 `workspace_id` 到日志与 `/health`，不自行管理容器名。

## 7. 路径双视图（本方案最容易出错的地方）

| 使用点 | local | docker | 原因 |
|---|---|---|---|
| 系统提示 `{workdir}` | 宿主绝对路径 | `/workspace` | 模型只能看见容器内路径 |
| `Bash.cwd` | 宿主绝对路径 | `/workspace` | 容器内没有宿主路径 |
| `Read/Write/Edit/Glob/Grep` | `backend.getcwd()` = 宿主 | `DockerBackend.getcwd()` = `/workspace` | 相对路径解析 |
| 权限 `working_directories` | 宿主 workdir | `/workspace` | 权限引擎在宿主侧读取工具入参 |
| guard 敏感路径边界 | 宿主 workdir | `/workspace` | 同上 |
| 挂载 / 审计 / 文件归属 | 宿主 workdir | 宿主 workdir | bind mount 的真实来源 |
| 观测事件 `workdir` | 宿主 | 同时记录 `host_workdir` 与 `tool_workdir` | 排障时要能对上两边 |

禁止：把 `/workspace` 当作宿主路径做 `Path.resolve()` 后去读写；也禁止把宿主路径直接交给容器内工具。两者都表现为「路径看起来对、文件就是找不到」。

## 8. 失败语义矩阵

| 场景 | `mode=auto` | `mode=docker` + `required=false` | `mode=docker` + `required=true` |
|---|---|---|---|
| Docker daemon 不可用 | 回落 local，`degraded_reason=docker_unavailable`，启动 warning | 回落 local + 同样告警 | 启动失败 / 对话 503 `sandbox_unavailable` |
| 镜像构建失败 | 回落 local + 告警（含构建日志尾部） | 回落 local + 告警 | 503 `sandbox_image_build_failed` |
| 容器启动失败 | 回落 local + 告警 | 回落 local + 告警 | 503 `sandbox_start_failed` |
| 初始化超时（默认 120s） | 回落 local + 告警 | 回落 local + 告警 | 503 `sandbox_timeout` |
| 运行中容器退出 | 下次请求重建容器 | 同左 | 同左 |
| 关闭失败 | warning；容器可能残留 | 同左 | 同左 |
| 权限 DENY | 不变 | 不变 | 不变 |

原则与配置层一致：**读降级、写拒绝**——能降级的是「可用性」，不能降级的是「隔离承诺」。`required=true` 时任何回落都是违约，必须让请求失败而不是悄悄在宿主执行。

## 9. 可观测性

`GET /health` 追加（全部是新增字段，旧前端不受影响）：

```jsonc
{
  "sandbox": {
    "mode": "docker",              // 请求模式
    "effective": "docker",         // 实际生效
    "degraded": false,
    "reason": "",                  // 回落原因码 + 原文
    "workspace_id": "b1c2...",
    "image": "agentscope-ws:sha256-...",
    "host_workdir": "E:/code/devops-agent/workspace",
    "tool_workdir": "/workspace",
    "containers": 1,
    "tools": { "rg": true, "git": true }   // 容器内 preflight 缓存结果
  }
}
```

- 启动日志一次性输出：模式、实际后端、镜像 tag、workspace_id、回落原因。
- 容器内依赖探测在 `ensure()` 后跑一次（`sh -c 'command -v rg'` 等），结果缓存；不放进每轮请求。
- `/api/tools/preflight` 增加 `sandbox` 段（容器内缺 `rg` 时，`Grep` 会持续失败，和宿主缺依赖是同一类问题）。
- 不新增 SSE 事件：执行面变化是部署期事实，不需要逐轮告诉前端。

## 10. 安全边界与已知缺口

**P1 获得**：

- 容器 namespace、独立 rootfs、只挂载 `host_workdir -> /workspace`；不挂 `docker.sock`，不 privileged。
- 权限引擎仍在宿主侧拦截每次工具调用：`readonly` 档只装配只读工具；`accept_edits` 只在 `/workspace` 内自动放行；`DENY` 规则（`.git/`、`.env`、`.ssh/`…）照旧生效。
- 密钥仍由 `SecretInjectionMiddleware` 在工具边界替换；容器 `env` 只接受显式白名单，不放密钥。

**P1 明确没有**：

- 网络隔离：容器默认 bridge，可访问宿主可达的一切（含云元数据端点）。需要在不可信代码场景使用时，必须先做 P2。
- 资源限额：无 CPU / 内存 / pids 限制；一个 `fork` 炸弹可以拖垮宿主。
- 非 root 运行、cap-drop、seccomp、只读 rootfs：内置构造签名未暴露。
- 镜像供应链：`base_image` 可配置，建议生产用 digest 固定；本方案不引入镜像签名/扫描。
- 会话间隔离：同一 workdir 的会话共享容器与文件；与当前共享 `DEVOPS_AGENT_WORKDIR` 的语义一致，不是新增风险，但也不是多租户方案。
- Linux 原生 Docker 下 bind mount 文件属主是 root：AgentScope 在 `close()` 时 `chown` 回宿主 uid；**运行期间**宿主用户可能无法直接编辑容器写出的文件。Windows/macOS Docker Desktop 由 VM 重映射，通常无感。
- Windows 路径：`DockerWorkspace` 需要 Docker Desktop 的路径转换；CI 只在 Ubuntu 跑 Docker 集成测试，Windows 本地失败按 `auto` 回落处理并告警。

## 11. 接口与兼容性

- `build_local_workspace` / `build_docker_workspace` 保留签名（现有测试不改）；新增 `build_target(policy, host_workdir, backend)` 作为唯一组装点。
- `build_toolkit_for` 新增关键字参数（`backend_factory`、`tool_cwd`），默认行为不变；所有现有调用点不必改。
- `build_agentscope_runtime` 新增可选 `execution: ExecutionTarget | None`；省略时走现有本机路径（评测/单测不受影响）。
- `create_app` 增加 lifespan；`runtime` 显式注入路径不变。
- `/health` 只追加 `sandbox`；前端 `HealthResponse` 增可选 `sandbox?`，旧服务端不带该字段也不会炸。
- `config` API 自动获得 `sandbox` 域（`GET/PATCH /api/config/sandbox`），与现有域同形状。

## 12. 实施拆分（每个 PR 可独立评审）

| PR | 内容 | 验收 |
|---|---|---|
| PR-1 | `ExecutionTarget` + `SandboxPolicy` 配置域 + `SandboxManager`（仅 local）+ `/health.sandbox` | 全量测试绿；local 行为与改动前逐字节一致；覆盖率 ≥80% |
| PR-2 | `catalog` 注入 backend/tool cwd；`prompt`/`compile`/`guard` 路径双视图；lifespan 接线；Docker 模式与降级矩阵 | 新增单测覆盖 E-1…E-8；Docker 集成测试通过（无 daemon 跳过） |
| PR-3 | 评测 harness 按 case 建/销工作区；TTL 回收；启动日志与 preflight 容器探测；部署文档/ADR-0006 | 评测批跑不泄漏容器；case 间文件不串 |
| PR-4 | 上游贡献 `network_mode` / `user` / `mem_limit` / `pids_limit` 到 `DockerWorkspace`；本仓库升级后开 `required=true` + 断网 | 上游合并后单独 PR；本仓库只改配置 |

PR-1 必须先合：它把「配置与执行目标」稳定下来，PR-2 才不用同时改配置、装配与 Docker 三件事。

## 13. 测试计划

**单测（无 Docker）**

- `test_sandbox_policy.py`：默认值、非法 mode、`ttl<=0`、env 覆盖、`required` 解析。
- `test_execution_target.py`：local/docker 的 `tool_workdir` / `shell` 映射；`auto` 回落带 reason；`required` 不回落。
- `test_sandbox_manager.py`：同 key 并发只初始化一次；不同 key 隔离；`close_all` 幂等；初始化失败不缓存；容器退出后重建（用假 workspace）。
- `test_catalog_backend.py`：注入 backend 后六个工具都用同一 backend；`tool_cwd` 透传；不注入时仍是 `_WorkdirBackend`。
- `test_permission_paths.py`：docker 模式 `working_directories` 含 `/workspace`、不含宿主路径；`accept_edits` 对 `/workspace/app.py` 放行、对 `/etc/passwd` 拒绝。
- `test_health_sandbox.py`：`/health.sandbox` 字段完整；降级时 `degraded=true` 且 reason 非空；`required` 失败时对话端点 503。

**Docker 集成（`@pytest.mark.docker`，无 daemon 自动 skip）**

- `Bash("pwd")` 输出 `/workspace`；`Write` 写文件后宿主 workdir 能看到；宿主机已有文件 `Read` 能读到。
- 容器内 `touch /tmp/x` 不影响宿主；`rm -rf /workspace/*` 只影响挂载目录。
- `readonly` 档写文件被权限引擎拒绝（不是容器拒绝）。
- `accept_edits` 档写 `/workspace/a.txt` 不触发 ASK；写 `/etc/a.txt` 触发 ASK/DENY。
- 杀掉容器后下一次 `ensure()` 重建，宿主 workdir 文件仍在。
- 断网/资源限额属于 PR-4 的测试，不在本 PR 断言。

**门禁**：`pytest --cov-fail-under=80` 不放宽；Docker 集成测试若在 CI 的 `quality` job 里运行，必须设超时并允许 skip，避免 daemon 冷启动拖垮门禁；`lint-imports` 保持：`executor` 可依赖 agentscope，`gateway` 不反向依赖。

## 14. 验收标准

1. `mode=local`：现有全量测试与行为不变；`/health.sandbox.effective == "local"`。
2. `mode=docker`：`pwd`、读写文件、`Grep` 都在容器内；宿主 workdir 只通过挂载可见；容器关闭后文件保留。
3. 权限：`readonly` 写被拒；`accept_edits` 在 `/workspace` 内自动放行；`.env` / `.git/` 的 DENY 规则仍生效。
4. 降级：停掉 Docker → `auto` 回落 local 且 `/health` 可见；`required=true` → 对话 503，不悄悄在宿主执行。
5. 资源：进程退出后无遗留容器（`docker ps -a --filter label=agentscope.workspace=true` 为空）；评测 case 结束后无残留。
6. 质量：覆盖率 ≥80%、源码单文件 ≤500 行、`ruff` / `mypy` / `vulture` / `lint-imports` 全绿。

## 15. 未做与开放问题

- **网络隔离是硬需求还是 nice-to-have？** 如果本项目的定位是「让模型在用户仓库里执行命令」，bridge 出网可接受；如果要跑不可信代码，P1 不达标，必须先做 PR-4。需要在评审时定。
- **镜像由谁维护？** `python:3.11-slim` 不自带 `rg`；要么部署时用预装 `ripgrep` 的自定义镜像（`base_image` 配置），要么接受 `Grep` 在容器内不可用。建议提供一个 `docker/sandbox.Dockerfile` 作为部署资产，但不纳入本仓库构建。
- **默认模式是否该是 docker？** 本方案取 local 以保护现有开发/CI；如果你希望「默认即隔离」，需要同时接受所有开发者机器必须装 Docker，且 CI 增加 Docker job。
- **每会话工作区**：当前共享 workdir；若未来要按会话隔离，`SandboxManager` 的 key 改为 `(session_id, workdir)` 即可，但会带来容器数量与镜像构建成本，需要单独 spec。
- **`get_backend()` 的真实签名**：2.0.x 文档存在该方法，但实现前必须 introspect（AGENTS §3.4），若版本不匹配，退回 `workspace.list_tools()` 并补挂中间件，需要重新评估。
