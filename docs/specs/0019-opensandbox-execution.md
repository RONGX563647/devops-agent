# SPEC-0019：OpenSandbox 执行面与预配置沙箱（消除 Docker 胶水）

- 状态：草案（待评审）
- 日期：2026-11-27
- 基线：`origin/main 7c1a269`（SPEC-0018 执行面已合并）
- 关系：SPEC-0018 把 `ExecutionTarget` / `SandboxManager` 接到了运行路径，但执行后端只有
  `LocalWorkspace` / `DockerWorkspace`。本方案在同一套执行面骨架上新增
  **OpenSandbox** 后端，用「预配置镜像 + 模板/快照 + 暂停复用」把沙箱启动从分钟级压到秒级，
  并移除仓库侧对 Docker 的任何直接管理。
- 范围：`executor/{target,manager,boundary}.py`、`config_schema.py`、`config_store/registry.py`、
  `gateway/{app,bootstrap}.py`、`gateway/routes/health.py`、`pyproject.toml`（可选 extra）、
  `docker/`（预配置镜像构建资产）、新增测试与文档
- 触发来源：SPEC-0018 §10 已明确「网络隔离 / 资源限额 / 非 root」等能力不在本仓库自研；
  OpenSandbox 作为 AgentScope 内置后端，原生提供这些能力，且把容器生命周期交给远端服务管理

---

## 1. 设计结论

1. **不写任何 Docker 胶水**。仓库不 import `docker` / `aiodocker`，不拼 `docker run`、不建容器、
   不做文件同步。OpenSandbox 模式只调用 `agentscope.workspace.OpenSandboxWorkspace`，
   容器/镜像/网络/资源全部由 OpenSandbox 服务与 AgentScope 内置类承担。
2. **执行面骨架不变，只增加一个 provider**。`ExecutionTarget` / `SandboxManager` / 双路径视图 /
   lifespan 接线沿用 SPEC-0018；新增 `mode=opensandbox`，与 `local` / `docker` 并列，
   默认仍是 `local`（不破坏现有开发与 CI）。
3. **`host_workdir` 语义降级为「稳定键 + 审计标签」**。OpenSandbox 没有 bind mount：
   工作区文件只存在于沙箱内，宿主目录既不被挂载也不保存副本。`tool_workdir` 恒为
   `/workspace`，权限、提示词、guard 边界都用它；审计改为记录 `workspace_id` / `sandbox_id`，
   不再声称「文件在宿主某目录」。
4. **`build_target` 不能再用 `isinstance(DockerWorkspace)` 判定模式**。当前实现会把
   `OpenSandboxWorkspace` 误判为 `local`（见证据 E-2）。改为按 `SandboxedWorkspaceBase` /
   `DockerWorkspace` 显式分派，`mode` 增加 `opensandbox`。
5. **启动性能靠「预配置环境」而不是预热容器池**。AgentScope 的 bootstrap 是
   `apt-get + uv 安装 + venv + pip install`，冷启动分钟级；但只要镜像里已存在
   `_gateway_script`，bootstrap 会被整体跳过（框架注释原文见 §7.1）。
   因此默认推荐**预烘焙黄金镜像**，而不是在仓库里维护一个「提前起 N 个空容器」的池子。
6. **模板 / 快照是平台能力，不在本仓库复制**。OpenSandbox 官方支持 `template_id`（黄金镜像）
   与 `snapshot_id`（快照恢复）；但 AgentScope 2.0.10 的 `OpenSandboxWorkspace` 构造签名
   没有暴露这两个参数（证据 E-5）。本方案把「预配置」落在 `image` 上，模板/快照作为上游
   增强项（PR-4）提出，不在仓库里拼私有 API。
7. **暂停即复用，关闭不等于销毁**。`OpenSandboxWorkspace.close()` 调用的是 SDK 的
   `pause()`（保留沙箱文件系统），下次 `initialize()` 会按 metadata 找到同一 `workspace_id`
   并 `resume()` 或 `connect()`（证据 E-6）。这对「同一工作区反复对话」是加速；
   对评测 case 是泄漏风险——需要一个显式销毁语义（§9，开放问题 O-1）。
8. **安全边界如实标注**。OpenSandbox 构造签名原生支持 `network_policy`（出网策略）、
   `resource`（CPU/内存请求）与 Credential Vault；这比 SPEC-0018 的 Docker 基线强。
   但「非 root / cap-drop / seccomp / 只读 rootfs」取决于镜像与平台模板，不在本仓库实现。
9. **API Key 只走环境变量**。`api_key` 是凭据，不写入 `config.db`、不进入 `/health`、
   不通过容器 `env` 透传给被测命令；与模型密钥同属「env-only」层。

---

## 2. 现状证据（改动前）

| 编号 | 断言 | 证据 | 复现 |
|---|---|---|---|
| E-1 | 配置只认 local/docker/auto | `config_schema.py:243` `mode: Literal["local","docker","auto"]`；`SandboxDockerPolicy` 在 `:222` | `rg "Literal\[\"local\"" src/devops_agent/config_schema.py` |
| E-2 | 模式判定按 DockerWorkspace，会将 OpenSandbox 误判为 local | `executor/target.py:41` `mode: Literal["local","docker"]`；`:69` `if isinstance(workspace, DockerWorkspace)` | `rg "isinstance\(workspace, DockerWorkspace\)" src/devops_agent` |
| E-3 | backend 注入只看 `is_docker` | `executor/target.py:50` `def is_docker`；assembly/scop_runtime 据此决定是否注入共享 backend | `rg "is_docker" src/devops_agent` |
| E-4 | 生命周期只有 Docker 探测与构造分支 | `executor/manager.py:67` `docker_available()`；`:105` `_default_factory()` 仅 local/docker | `rg "def docker_available\|def _default_factory" src/devops_agent/executor` |
| E-5 | AgentScope 已内置 OpenSandbox 后端，但本仓库未使用 | `agentscope.workspace` 导出 `OpenSandboxWorkspace`；构造签名 `image/api_key/domain/protocol/request_timeout_seconds/timeout_seconds/gateway_port/env/sandbox_metadata/resource/entrypoint/network_policy/extra_pip/...`（`_opensandbox_workspace.py:49-65`），**无 template_id / snapshot_id** | `python -c "from agentscope.workspace import OpenSandboxWorkspace; import inspect; print(inspect.signature(OpenSandboxWorkspace.__init__))"` |
| E-6 | OpenSandbox 复用/暂停语义已内置 | `_opensandbox_workspace.py:159` `_find_existing_sandbox()`；`:282` `Sandbox.resume(...)`；`:289` `Sandbox.connect(...)`；`_teardown_backend`（`:168`）调用 `pause()` | `sed -n '150,320p' .venv/.../agentscope/workspace/_opensandbox/_opensandbox_workspace.py` |
| E-7 | 冷启动成本来自 bootstrap，且「镜像已含脚本」时有 fast-path | `_sandboxed_base.py:445-456`：bootstrap 仅在 `_gateway_script` 不存在时执行，注释原文「Docker's image already has it → fast-path」 | `sed -n '440,470p' .venv/.../agentscope/workspace/_sandboxed_base.py` |
| E-8 | 黄金镜像需要预置的具体路径 | `GATEWAY_HOME=/root/.agentscope`（`_opensandbox/_constants.py:41`），`DEFAULT_GATEWAY_VENV=".venv"`、`DEFAULT_GATEWAY_SCRIPT="_mcp_gateway_app.py"`、`DEFAULT_GLOB_HELPER_SCRIPT="_glob_helper.py"`（`agentscope/workspace/_utils.py:93-96`） | `rg "DEFAULT_GATEWAY" .venv/.../agentscope/workspace/_utils.py` |
| E-9 | 现有 Docker 镜像模板可直接作为烘焙参考 | `agentscope/workspace/_docker/Dockerfile.template:18-49`：apt(curl/ca-certificates/ripgrep) → uv → venv → pip → COPY 两个脚本 | `sed -n '1,60p' .venv/.../agentscope/workspace/_docker/Dockerfile.template` |

> 说明：E-5～E-9 的证据路径位于已安装的 `agentscope==2.0.10`。实现前必须按 AGENTS §3.4
> 重新 introspect 当前锁定版本，不凭本文记忆写 API。

---

## 3. 目标与非目标

### 3.1 目标

- 新增 `mode=opensandbox`：执行面选择、初始化、复用、降级与 SPEC-0018 完全同构。
- 移除仓库对 Docker 的直接管理：OpenSandbox 路径不 import Docker SDK，不操作 docker.sock。
- 预配置沙箱：提供可复现的黄金镜像构建资产，使运行时 bootstrap 被跳过（秒级启动）。
- 复用平台能力：`network_policy`、`resource`、`timeout_seconds`、pause/resume。
- `/health.sandbox` 暴露 provider、`sandbox_id`、`image`、`init_ms`、`reused`、`bootstrap_skipped*`。
- 失败语义与 SPEC-0018 一致：`required=true` 拒绝回落（503），`auto` 回落但可见。

### 3.2 非目标

- 不自研沙箱运行时、镜像仓库、快照服务、网络代理；不 fork OpenSandbox。
- 不实现容器预热池（warm pool）；理由见 §1.5 与 §7。
- 不把 `template_id` / `snapshot_id` 通过私有 API 硬塞进 `OpenSandboxWorkspace`（上游 PR-4）。
- 不改权限模型、确认协议、记忆、前端交互（`/health` 字段为可选扩展）。
- 不替代 OpenJudge 自己的评测沙箱（`openjudge/sandbox.py` 保持现状）。

---

## 4. 架构

```text
            sandbox 配置域（mode=opensandbox + opensandbox.*）
                              │
                              ▼
   DEVOPS_AGENT_WORKDIR ──▶ SandboxManager.ensure(key=host_workdir)
                    （异步、幂等、按 key 加锁、失败不缓存）
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        LocalWorkspace   DockerWorkspace   OpenSandboxWorkspace
        LocalBackend     DockerBackend     OpenSandboxBackend
                              │
                              ▼
                       ExecutionTarget
        { mode, host_workdir(键/审计), tool_workdir=/workspace,
          backend, shell=/bin/sh, workspace, degraded_reason,
          remote=True, sandbox_id, workspace_id, init_ms }
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
 build_toolkit_for()   build_system_prompt()   compile_context()
 共享 remote backend    /workspace + /bin/sh     /workspace 登记
```

### 4.1 `ExecutionTarget` 变更（`executor/target.py`）

```python
@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    mode: Literal["local", "docker", "opensandbox"]
    host_workdir: Path          # opensandbox：仅作稳定键与审计标签，不挂载
    tool_workdir: str
    backend: BackendBase
    shell: str
    workspace: WorkspaceBase | None = None
    degraded_reason: str = ""
    sandbox_id: str = ""        # opensandbox 的实际实例标识（可空）
    init_ms: int = 0            # ensure 创建耗时（用于验证预配置收益）

    @property
    def uses_shared_backend(self) -> bool:
        """docker / opensandbox 共用无状态 backend；local 逐工具新建。"""
        return self.mode != "local"
```

要点：

- 用 `uses_shared_backend` 取代 `is_docker`（保留 `is_docker` 作为 `mode == "docker"` 的只读
  属性以兼容既有调用；新增代码一律用新属性）。
- `build_target` 分派：
  `isinstance(ws, OpenSandboxWorkspace) → opensandbox`；
  `isinstance(ws, DockerWorkspace) → docker`；否则 `local`。
- opensandbox 的 `sandbox_id` 通过 `workspace` 的公开属性获取；若 2.0.x 不暴露，则先留空并在
  PR 描述标注（禁止读私有字段）。

### 4.2 `SandboxManager` 变更（`executor/manager.py`）

- `Mode` 扩为 `Literal["local","docker","opensandbox"]`。
- 新增 `opensandbox_available() -> tuple[bool, str]`：
  1. `importlib.util.find_spec("opensandbox")` 为空 → 不可用（`sandbox_package_missing`）；
  2. 缺少 endpoint 配置（`domain` 为空且 SDK 环境无默认）→ 不可用（`sandbox_endpoint_missing`）；
  3. 不做网络连通性探测（启动期不等待远端），连通性失败留给 `initialize()` 走失败矩阵。
- `_default_factory(mode, host, policy)` 增加 opensandbox 分支，构造参数全部来自
  `SandboxOpensandboxPolicy`；`network_policy`（dict）在**函数内延迟 import**
  `opensandbox.models.sandboxes.NetworkPolicy` 后转换，保证未安装 SDK 时模块仍可 import。
- 缓存键不变 `(mode, str(host), image)`；opensandbox 的 image 参与键，换镜像即换沙箱。
- `close()` 对 opensandbox 调用 `workspace.close()`（即 `pause()`，保留文件系统）。
  真正的销毁留给 §9 的显式接口。

### 4.3 工具与路径

- 复用 SPEC-0018 的 `backend_factory` / `tool_cwd` 注入，只需把注入条件从 `is_docker` 改为
  `uses_shared_backend`，opensandbox 自动获得共享 `OpenSandboxBackend`。
- 提示词、`Bash.cwd`、权限 `working_directories`、guard 边界继续用 `/workspace`，
  **不需要为新后端写任何分支**。

### 4.4 网关

- `gateway/app.py` 的 lifespan 不变：`await manager.ensure(resolve_workdir())` 后装配 runtime。
- `describe_sandbox` 增加字段（见 §11），`mode` 取值扩展。
- `required=true` 失败时 503 的行为与错误码沿用 SPEC-0018。

---

## 5. 配置契约

### 5.1 类型（`config_schema.py`）

```python
class SandboxOpensandboxPolicy(BaseModel):
    # 预配置黄金镜像；生产建议用 digest 固定。空串非法。
    image: str = Field(default="python:3.11-slim", min_length=1)
    # 连接参数；api_key 不在此处（env-only，见 §5.3）
    domain: str = ""
    protocol: Literal["http", "https"] = "https"
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    # 空闲保活；与 sandbox.ttl_seconds 独立（平台侧 vs 本地缓存侧）
    timeout_seconds: int = Field(default=1800, gt=0)
    gateway_port: int = Field(default=5600, ge=1, le=65_535)
    env: dict[str, str] = Field(default_factory=dict)
    extra_pip: list[str] = Field(default_factory=list)
    # 平台原生能力：原样透传，含义由 OpenSandbox 决定
    resource: dict[str, str] = Field(default_factory=dict)
    network_policy: dict[str, object] = Field(default_factory=dict)
    entrypoint: list[str] = Field(default_factory=list)


class SandboxPolicy(BaseModel):
    mode: Literal["local", "docker", "opensandbox", "auto"] = "local"
    required: bool = False
    ttl_seconds: int = Field(default=900, gt=0)
    docker: SandboxDockerPolicy = Field(default_factory=SandboxDockerPolicy)
    opensandbox: SandboxOpensandboxPolicy = Field(default_factory=SandboxOpensandboxPolicy)
```

`auto` 的优先级固定为 **opensandbox → docker → local**，每级失败原因写入
`ExecutionTarget.degraded_reason` 与 `/health.sandbox.reason`，不允许静默。

### 5.2 域与热更新

- `config_store/registry.py` 的 `DOMAINS` 中 `sandbox` 仍是 `hot="restart"`；
  `opensandbox` 作为 `sandbox` 的子结构，不新增独立域。
- `ENV_OVERLAY["sandbox"]` 追加：
  `("opensandbox.domain", "DEVOPS_AGENT_SANDBOX_OSB_DOMAIN", "str", ())`、
  `("opensandbox.image", "DEVOPS_AGENT_SANDBOX_OSB_IMAGE", "str", ())`、
  `("mode", "DEVOPS_AGENT_SANDBOX_MODE", "str", ("local","docker","opensandbox","auto"))`。

### 5.3 凭据（env-only）

| 变量 | 用途 | 是否进入 config.db / `/health` |
|---|---|---|
| `DEVOPS_AGENT_SANDBOX_OSB_API_KEY` | `OpenSandboxWorkspace(api_key=...)` | 否 / 否 |
| `DEVOPS_AGENT_SANDBOX_OSB_DOMAIN` | endpoint 主机 | 可覆盖配置 / 可展示 |
| `DEVOPS_AGENT_SANDBOX_OSB_PROTOCOL` | http/https | 可覆盖配置 / 可展示 |

API Key 既不放 `config.db`，也不放 `sandbox.opensandbox.env`（那是容器内可见的环境变量）。

### 5.4 可选依赖

```toml
[project.optional-dependencies]
opensandbox = ["agentscope[workspace-opensandbox]>=2.0"]
```

- 未安装时 `import devops_agent` 与 `mode=local/docker` 完全不受影响；
- `mode=opensandbox` 且未安装：`required=true` → 503 `sandbox_package_missing`；
  `auto` → 回落并在 `/health` 显示原因。

---

## 6. 失败语义矩阵（沿用 SPEC-0018，新增 provider 码）

| 场景 | required=true | required=false | auto |
|---|---|---|---|
| 未安装 `opensandbox` | 503 `sandbox_package_missing` | local + reason | local + reason |
| 缺 endpoint 配置 | 503 `sandbox_endpoint_missing` | local + reason | local + reason |
| `initialize()` 认证失败 | 503 `sandbox_auth_failed` | local + reason | local + reason |
| 镜像 / 模板不可用 | 503 `sandbox_image_unavailable` | local + reason | local + reason |
| bootstrap 失败 | 503 `sandbox_bootstrap_failed` | local + reason | local + reason |
| 启动超时 | 503 `sandbox_timeout` | local + reason | local + reason |

- 失败**不写缓存**，下次请求重试（与 SPEC-0018 一致）；
- `required=true` 时绝不回落宿主：`host_workdir` 在容器外执行属于安全事故。

---

## 7. 预配置沙箱：把启动从分钟级降到秒级

### 7.1 为什么有效（框架事实）

`SandboxedWorkspaceBase` 的启动流程是：若 `_gateway_script` 不存在，则执行
`_bootstrap_commands()`（apt-get 安装 curl/ripgrep、安装 uv、`uv venv`、`uv pip install`
基础依赖与额外包、写两个脚本），再启动 gateway。注释原文：

> Bootstrap the venv + script if `_gateway_script` is missing (Docker's image already has it → fast-path).
> — `agentscope/workspace/_sandboxed_base.py:445-446`

因此**镜像里预置好 `/root/.agentscope/{.venv,_mcp_gateway_app.py,_glob_helper.py}`**，
OpenSandbox 启动就只剩「拉起 gateway + 轮询 /health」，分钟级的网络安装被整体跳过。

### 7.2 分层策略

| 层级 | 做法 | 预期启动 | 是否本期 |
|---|---|---|---|
| L0 裸镜像 | 默认 `python:3.11-slim`，每次 bootstrap | 分钟级 | 否（仅兜底） |
| **L1 黄金镜像（默认）** | 仓库提供 `docker/sandbox.Dockerfile`，预装 gateway venv 与两个脚本 | 秒级 | **是** |
| L2 模板 / 快照 | 平台 `template_id` / `snapshot_id`（OpenSandbox 原生） | 亚秒～秒级 | 否（上游 PR-4） |
| L3 暂停复用 | 同一 `workspace_id` 的 `pause()` → `resume()/connect()`（框架内置） | 秒级 | 是（已验证框架行为，接线即可） |

### 7.3 黄金镜像构建资产（L1）

新增 `docker/sandbox.Dockerfile` + `docker/README.md`（构建与推送说明），内容与
AgentScope 的 `Dockerfile.template` 同构，但由本仓库维护并固定版本：

```dockerfile
FROM python:3.11-slim
# 1) 系统依赖：bash 工具链需要的 rg，以及 uv 安装器需要的 curl/ca-certificates
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates ripgrep \
 && rm -rf /var/lib/apt/lists/*
# 2) uv
RUN curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
# 3) gateway venv 与脚本（路径必须与框架常量一致，见证据 E-8）
ENV GATEWAY_HOME=/root/.agentscope
RUN uv venv $GATEWAY_HOME/.venv
COPY docker/gateway-requirements.txt $GATEWAY_HOME/requirements.txt
RUN uv pip install -r $GATEWAY_HOME/requirements.txt
# 4) 两个脚本由构建脚本从已安装的 agentscope 中导出（构建期一次性，不进运行时）
COPY docker/export_gateway_assets.py /tmp/
RUN python /tmp/export_gateway_assets.py --dest $GATEWAY_HOME
WORKDIR /workspace
```

`export_gateway_assets.py` 在构建期读取当前 `agentscope` 版本携带的脚本字节并写盘，
**运行时零胶水**；`gateway-requirements.txt` 固定 `agentscope==<锁定版本>` 与 gateway 基础依赖。

构建与验证（纳入 CI 的可选 job）：

```bash
docker build -f docker/sandbox.Dockerfile -t devops-agent-sandbox:<agentscope-version> .
# 断言 fast-path 前提成立，且无 apt/uv 残留动作
docker run --rm devops-agent-sandbox:<ver> sh -c \
  'test -f /root/.agentscope/_mcp_gateway_app.py && test -x /root/.agentscope/.venv/bin/python && rg --version'
```

### 7.4 不做 warm pool 的理由

- 预热池需要「预创建容器 → 分配 → 回收」的调度与隔离键，超出「薄生命周期」定位；
- 平台层已有模板/快照；L1+L3 已把冷启动压到秒级，warm pool 的边际收益低、复杂度高；
- 预热容器若空闲不销毁，反而放大资源占用与安全暴露面。

---

## 8. 路径与数据语义（与 Docker 模式的关键差异）

| 维度 | docker（现状） | opensandbox（本方案） |
|---|---|---|
| 文件位置 | 宿主 bind mount ↔ `/workspace` | 仅在沙箱内 `/workspace` |
| `host_workdir` | 真实挂载源 | 仅稳定键 + 审计标签 |
| 关闭行为 | 删容器（宿主文件保留） | `pause()`（沙箱文件保留，可恢复） |
| 进程退出后 | 宿主文件仍在 | 沙箱暂停，平台按 `timeout_seconds` 回收 |
| 审计字段 | `host_workdir` | `workspace_id` / `sandbox_id` |
| 文件导出 | 直接读宿主 | 需显式 upload/download（本期不做） |

约束：

- `/workspace` 内的产物**不会**出现在宿主 `DEVOPS_AGENT_WORKDIR`；任何依赖「主机上能直接看到
  产物」的功能（若存在）在 opensandbox 模式下必须显式导出，否则标注不支持；
- 评测 case 的临时工作区在 `close()` 后仍是「暂停」状态，必须显式销毁（§9）。

---

## 9. 生命周期与开放问题

| 场景 | 工作区来源 | 初始化 | 关闭 | 销毁 |
|---|---|---|---|---|
| 聊天主工作区 | `DEVOPS_AGENT_WORKDIR` | 启动 warm-up | 进程退出 `pause` | 平台超时回收 |
| 评测 case | 每 case 独立 key | `async with manager.workspace()` | 退出 `pause` | **需显式 kill（O-1）** |
| 换镜像 | image 参与缓存键 | 新沙箱 | 旧沙箱 `pause` | 需显式 kill（O-1） |

- **O-1（阻塞 PR-3）**：AgentScope 2.0.10 的 workspace 只暴露 `close()`（=pause），
  没有 `destroy()`/`kill()`。评测 case 若只 pause，会在平台上累积暂停沙箱。
  方案：向上游提交「`destroy()` 或 `close(delete=True)`」能力；在落地前，评测路径暂不切换
  opensandbox（保持 local，或由平台 TTL 兜底）。禁止读私有字段直接调 SDK 绕过。
- **O-2**：`template_id` / `snapshot_id` 未在构造签名中暴露（E-5）。上游 PR-4 增加后，
  本仓库只加两个配置项并透传，不改生命周期。
- **O-3**：`sandbox_id` 是否有公开访问器需实现时 introspect；没有就先空置，不读私有字段。
- **O-4**：是否废弃 `docker` 模式。建议保留至 L1 镜像稳定运行一个版本后再评估，
  避免同一次变更同时换后端与删旧路径。

---

## 10. 可观测性（`/health.sandbox`）

在 SPEC-0018 字段基础上追加（旧字段保持不变）：

```json
{
  "mode": "auto",
  "effective": "opensandbox",
  "provider": "opensandbox",
  "degraded": false,
  "reason": "",
  "workspace_id": "ws-...",
  "sandbox_id": "sbx-...",
  "image": "registry/devops-agent-sandbox@sha256:...",
  "host_workdir": "/srv/agent-workspace",
  "tool_workdir": "/workspace",
  "containers": 1,
  "init_ms": 1800,
  "reused": true,
  "bootstrap_skipped": true
}
```

- `reused`：是否命中暂停沙箱并 resume（manager 记录 `_find_existing` 等价结果需框架暴露；
  若无法获取则置 `null` 并在文档标注）。
- `bootstrap_skipped`：若框架不暴露，改为记录 `init_ms` 告警阈值（>30s 即判定 bootstrap 发生），
  **不伪造布尔值**。

---

## 11. 安全的「有」与「无」

| 能力 | 来源 | 本期 |
|---|---|---|
| 出网策略 `network_policy` | OpenSandbox 构造参数 | 是（透传） |
| CPU/内存 `resource` | OpenSandbox 构造参数 | 是（透传） |
| Credential Vault / 凭据代理 | OpenSandbox 平台 | 是（平台配置，仓库不存密钥） |
| 非 root / cap-drop / seccomp / 只读 rootfs | 镜像与平台模板 | 依赖 L1 镜像策略，需在 `docker/README.md` 明确 |
| 宿主 Docker socket | — | **完全不接触**（opensandbox 路径零 Docker 依赖） |

---

## 12. 实施拆分

- **PR-1（选择与骨架）**
  1. `SandboxOpensandboxPolicy` + `mode` 枚举 + env overlay + 可选 extra；
  2. `ExecutionTarget` 增加 `opensandbox` / `uses_shared_backend` / `sandbox_id` / `init_ms`；
  3. `build_target` 按 `SandboxedWorkspaceBase` 分派；manager 增加 factory 分支与
     `opensandbox_available()`；assembly/scop_runtime 注入条件改 `uses_shared_backend`；
  4. 单测：模式映射、required/auto 矩阵、未安装 SDK 的降级与 503、local/docker 行为不变。
- **PR-2（预配置环境与可观测性）**
  1. `docker/sandbox.Dockerfile` + `export_gateway_assets.py` + `docker/README.md`；
  2. `init_ms` 计时、`/health.sandbox` 新字段、启动日志；
  3. 镜像 smoke 测试 job（无 daemon 时 skip）；文档：生产 `mode=opensandbox + required=true`。
- **PR-3（case 生命周期，依赖 O-1）**
  1. 评测 harness 按 case 创建/销毁；有上游 `destroy` 后再切 opensandbox；
  2. `reap_idle()` 语义对齐平台 `timeout_seconds`。
- **PR-4（上游贡献，不在本仓库）**
  1. `OpenSandboxWorkspace` 暴露 `template_id` / `snapshot_id`；
  2. 暴露 `destroy()` / `close(delete=True)`；
  3. 暴露 `sandbox_id` / `reused` / `bootstrap_skipped` 状态。

---

## 13. 测试计划

单测（无需真实 OpenSandbox）：

- `SandboxPolicy` 默认值、非法 mode、`request_timeout_seconds<=0`、空 image；
- `build_target`：`OpenSandboxWorkspace` → `opensandbox` + `/workspace` + `/bin/sh`；
  `DockerWorkspace` → docker；`LocalWorkspace` → local（回归）；
- manager：未安装 SDK 的 `required` 503 / `auto` 回落；初始化异常分类；失败不缓存；
  并发只初始化一次；image 变化产生新缓存键；
- 注入：opensandbox 模式六工具共享同一 backend；提示词/权限只出现 `/workspace`；
- `/health.sandbox` 字段与降级原因。

集成（`@pytest.mark.opensandbox`，无包/无服务自动 skip）：

- 黄金镜像下 `initialize()` 不执行 apt/uv（断言 `init_ms` 低于阈值 + gateway `/health` 可达）；
- `pwd` = `/workspace`；`rg --version` 可用；`Read/Write/Glob/Grep` 走 `OpenSandboxBackend`；
- `close()` 后同 `workspace_id` 再次 `initialize()` 命中 resume/connect；
- `network_policy` 拒绝出网、`resource` 生效（平台支持时）。

---

## 14. 验收标准

1. `mode=local` / `mode=docker` 行为与 SPEC-0018 逐字节一致（回归测试全绿）。
2. `mode=opensandbox` + 黄金镜像：首次启动 `init_ms` 显著低于裸镜像（目标 < 10s），
   且未执行 bootstrap 命令。
3. 容器内 `pwd` 为 `/workspace`，六个工具全部经 `OpenSandboxBackend` 执行；
   提示词与权限目录不含宿主路径。
4. `required=true` 在包缺失 / endpoint 缺失 / 认证失败 / 镜像不可用时返回 503，
   错误码可区分；`auto` 回落本机且 `/health.sandbox` 可见原因。
5. opensandbox 路径不 import `docker` / `aiodocker`（`rg` 断言 + import 钩子测试）。
6. 仓库质量门禁全绿：ruff / format / mypy / vulture / 文件行数 / lint-imports /
   `pytest --cov-fail-under=80`。

---

## 15. 开放问题汇总

- **O-1**：case 沙箱销毁语义（阻塞 PR-3，需上游 `destroy`）。
- **O-2**：`template_id` / `snapshot_id` 透传（上游 PR-4）。
- **O-3**：`sandbox_id` 公开访问器（实现时 introspect）。
- **O-4**：`docker` 模式是否废弃（下个版本评估）。
- **O-5**：宿主可见产物（如导出报告）在 opensandbox 下的下载路径，本期不做，需产品确认。
