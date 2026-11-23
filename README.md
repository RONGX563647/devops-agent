# devops-agent

基于 AgentScope 2.x 构建的 **DevOps 智能体平台**，规划覆盖三个能力域：可持续交付、项目排期、Bug 测试定位。

> 技术栈：Python 3.11+ / AgentScope 2.x / FastAPI / Docker 沙箱
>
> **当前状态**：已完成框架接线、异步网关（可跑通真实模型）、SSE 流式输出与端到端可观测性（埋点 → SQLite → 观测 API → React 前端）。三个能力域的业务逻辑尚未填充。见文末「Roadmap」。

---

## 核心设计原则

| 原则 | 落地 |
|---|---|
| **编排与执行分离** | 模型不可靠，执行必须可靠；`runtime` 管编排，`executor` 管沙箱 |
| **框架与业务分离** | `agentscope` 只在 6 个装配/工厂模块中 import（见 AGENTS.md §3.1），网关与可观测性依赖协议/工厂，不直接依赖框架 |
| **过程态与源码分离** | 运行态写 `workspace/`（不提交），代码改动走 git PR |
| **过程隔离、结果共享** | 并行 Agent 互不读私有记忆，通过台账共享结果（台账待实现，见 Roadmap） |

---

## 目录结构

```
devops-agent/
├── pyproject.toml           # 唯一配置文件（打包 + 依赖 + 各工具配置）
├── .gitignore
├── config/
│   ├── policy.yaml          # 运行时策略：权限矩阵 / max_iters / 模型重试
│   ├── gates.yaml           # 质量门禁阈值（check_gates.py 读取）
│   └── health.yaml          # 健康分权重与延迟基准
├── scripts/
│   └── check_gates.py       # 从 gates.yaml 读取阈值并执行 CI 门禁
├── src/devops_agent/
│   ├── config.py            # config/*.yaml 的唯一加载入口（强类型）
│   ├── main.py              # FastAPI 入口（/health + /run/stream + /run/{id}/* + /api/*）
│   ├── runtime/             # 编排层
│   │   ├── agent.py         #   build_agent()：Agent/模型装配（框架隔离边界之一）
│   │   ├── events.py        #   事件 → 普通 dict（原生序列化 + duck typing 边界）
│   │   ├── supervisor.py    #   TeamPipeline / GoalPipeline 编排
│   │   └── runtime.py       #   AgentRuntime 协议（框架隔离接口）
│   ├── observability/       # 可观测性（框架无关）
│   │   ├── models.py        #   Run / Span / EvalScore 三模型
│   │   ├── repository.py    #   SQLite 唯一读写入口
│   │   ├── recorder.py      #   事件流 → run + spans 落库
│   │   ├── health.py        #   健康分与聚合统计
│   │   └── api.py           #   观测 API 路由
│   ├── tools/catalog.py     # FunctionTool / Toolkit / ToolGroup
│   ├── executor/boundary.py # LocalWorkspace / DockerWorkspace 沙箱
│   ├── governance/policy.py # PermissionEngine 权限引擎
│   ├── memory/ledger.py     # Mem0Middleware 长期记忆
│   └── eval/scorer.py       # 评测维度常量（EvalDimension）
├── web/                     # React 18 + TS + Vite 可观测性前端
├── tests/                   # unit / integration
├── skills/                  # 预留：可复用技能（Roadmap）
└── workspace/               # 运行态工作区（不提交）
```

---

## 快速开始

```bash
# 1. 建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装（含开发依赖）
pip install -e ".[dev]"

# 3. 启动（未配模型凭据时 /run/stream 走 EchoRuntime，便于自检）
uvicorn devops_agent.main:app --port 8000
# GET http://127.0.0.1:8000/health → {"status":"ok"}
# POST http://127.0.0.1:8000/run/stream  {"session_id":"s1","user_input":"hello"}
#   → SSE 事件流，Echo 模式回显 "[s1] hello"
```

> 未安装包时可临时用 `PYTHONPATH=src uvicorn devops_agent.main:app` 运行。

### 接入真实模型（OpenAI 兼容接口）

设置三个环境变量后，`/run/stream` 自动切换到真实模型运行时；**不要把密钥写进代码或提交到仓库**。

推荐本地用 `.env` 文件：把 `.env.example` 复制为 `.env` 并填入真实值，启动时代码自动加载（`.env` 已被 gitignore，不会进版本库）。已存在的环境变量优先于 `.env`，所以 CI / 容器注入的值不会被本地文件覆盖。

```bash
cp .env.example .env      # 然后编辑 .env，填入真实 key / 端点 / 模型
uvicorn devops_agent.main:app --port 8000

# 也可直接用环境变量（优先级高于 .env）：
export DEVOPS_AGENT_API_KEY='<your-api-key>'
export DEVOPS_AGENT_BASE_URL='https://<your-endpoint>/compatible-mode/v1'
export DEVOPS_AGENT_MODEL='<model-name>'

curl -N -X POST http://127.0.0.1:8000/run/stream \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","user_input":"用一句话说明什么是 CI"}'
```

运行时的选择在 `main.select_runtime()` 中完成，并挂载到 `app.state.runtime`；测试可替换该对象而不必改环境变量。

### 模型侧联网搜索（可选）

`DEVOPS_AGENT_ENABLE_SEARCH=1` 时，模型请求体会附加 `enable_search=true`，由供应商（如 DashScope 兼容接口）执行联网检索。**默认关闭**。

- 检索发生在供应商侧，事件流里没有工具调用 span，只能看到最终回答，无法审计检索了哪些页面。
- 供应商或模型不支持该参数时可能直接返回 400；关掉开关即可回退。
- 检索通常按次计费，开启前请确认额度与共享环境的影响。

### Agent 的工具与权限

真实运行时装配一组**内置只读工具**：`Bash`（shell）、`Read`、`Glob`、`Grep`。`Edit` / `Write` 不在其中——只读会话不暴露写工具。

- 工作目录：`DEVOPS_AGENT_WORKDIR`（缺省为进程当前目录），同时决定 shell 的 cwd 与文件工具的路径基准。从别的目录启动服务时建议显式指定。
- 权限：读 `config/policy.yaml` 的 `permission.mode`，默认 `dont_ask`。这是**无人值守**服务的正确模式：只读命令放行，其余一律拒绝并给出原因；`default` 模式下敏感调用会返回 ASK，而 HTTP 服务没有人工确认通道，Agent 只会发出一个确认事件就结束这一轮，表现为「没有任何输出也没报错」。
- 想看工具调用细节：`POST /run/stream` 会推送 `TOOL_CALL_START` / `TOOL_RESULT_END`（含 `state` 与 `tool_call_name`），前端把它渲染成一行式步骤。

---

## 架构：agentscope 真实 API 映射

> 重要：AgentScope 2.x **没有 `HarnessAgent`**，核心是 `Agent + ReActConfig`，其余能力由模块组合。

| 本项目的职责 | agentscope 类 |
|---|---|
| Agent 核心 | `Agent(name, system_prompt, model, toolkit, react_config, model_config)` |
| 工具裁剪 / 只读边界 | `FunctionTool(is_read_only=...)` + `Toolkit` / `ToolGroup` |
| 沙箱执行面 | `LocalWorkspace` / `DockerWorkspace` / `K8sWorkspace` |
| 权限治理 | `PermissionEngine` + `PermissionContext` + `PermissionRule` |
| 长期记忆 | `Mem0Middleware` / `AgenticMemoryMiddleware` |
| 模型路由 | `ModelRouterMiddleware` |
| 编排 | `TeamPipeline` / `GoalPipeline` / `TeamMember` |

---

## 三个能力域（规划）

| 域 | Agent | 核心能力 |
|---|---|---|
| 可持续交付 | delivery | 环境生命周期、配方固化、流水线编排、门禁、漂移检测 |
| 项目排期 | schedule | 需求拆解、依赖排期、资源/人力、风险预警、进度同步 |
| Bug 测试定位 | debug | 测试生成、失败定位（证据 `file:line`）、修复建议 |

三个域共用装配入口 `build_agent(name, system_prompt, model, toolkit, max_iters, max_retries, policy)`；`max_iters` / `max_retries` 省略时读 `config/policy.yaml`（按域取 `react.max_iters.<name>`，未命中回退 `default`），并由 `TeamPipeline` 编排。**各域的 system_prompt 与真实工具函数尚未实现。**

---

## 治理

- **边界**：权限矩阵 `PermissionContext(mode, allow_rules, deny_rules, ask_rules)` + Docker 沙箱隔离；
- **git/PR**：每任务一个 worktree + 独立分支 + 一个 PR，主分支受保护、禁止直推，改动只能经 PR 合入（流程细则见 `AGENTS.md`）；
- **记忆**：`Mem0Middleware` 长期记忆，任务状态用 `agentscope.state.AgentState`；
- **评测**：评测维度枚举已定义，具体指标（recall@k / faithfulness / LLM-as-judge）见 Roadmap。

## 质量门禁（CI）

`config/gates.yaml` 定义阈值：覆盖率 ≥ 80%、圈复杂度 ≤ 10、变异测试 ≥ 70%、死代码置信度 80。`config/gates.yaml` 是阈值的**唯一来源**，`scripts/check_gates.py` 读取它并实际执行（CI 调用该脚本）。

| 阈值 | 执行方式 |
|---|---|
| `coverage_fail_under` | 注入 `pytest --cov-fail-under` 并解析 `coverage.xml` |
| `dead_code_min_confidence` | 注入 `vulture --min-confidence` |
| `complexity_threshold` | 解析 `radon cc -j` 的最大圈复杂度并判定 |
| `mutation_score` | `mutmut`，用 `--with-mutation` 启用（默认跳过以免拖慢 CI） |

工具链：`ruff`（风格）+ `mypy`（类型）+ `vulture`（死代码）+ `radon`（复杂度）+ `pytest-cov`（覆盖率）+ `mutmut`（测试效果）。

**前端门禁**（CI 的 `frontend` job）阈值同样来自 `config/gates.yaml`，读其中的 `frontend_gates` 段：`eslint.config.js` 取 `complexity_threshold`（只对 `.ts` 逻辑文件生效，JSX 条件渲染不计入圈复杂度），`vitest.config.ts` 取 `coverage_fail_under`（coverage-v8）。

| 命令 | 门禁 |
|---|---|
| `npm run lint` | ESLint + typescript-eslint + react-hooks |
| `npm run typecheck` | `tsc -b --noEmit` |
| `npm run test:coverage` | Vitest + 覆盖率阈值判定 |
| `npm run build` | 生产构建 |

---

## 可观测性与前端

后端在 `reply_stream()` 事件流上同步落库（runs / spans / eval_scores 三表），前端展示运行好坏的量化画像。设计见 `docs/adr/0001-agent-observability-frontend.md`。

**流式端点** `POST /run/stream`：逐帧返回 `data: {...}\n\n`（SSE），结束帧 `event: done` 带 run_id 与 token 汇总；`X-Request-ID` 幂等防重试重复落库。

**观测 API**（FastAPI，前缀 `/api`）：

| 端点 | 用途 |
|---|---|
| `GET /api/runs` | 运行列表（domain/status/时间筛选 + 分页） |
| `GET /api/runs/{id}` / `/spans` / `/health` | 详情 / 步骤时间线 / 健康分拆解 |
| `GET /api/metrics/summary` | 成功率 / 平均健康分 / P95 / token / 工具失败率 |
| `GET /api/metrics/timeseries` | 按天趋势 |
| `POST /api/feedback/{id}` | 人工 👍/👎 写入 eval_scores |
| `GET /api/eval/trends` | 各维度评分均值与来源分布 |

健康分（0-100）由执行健康 / 效率成本 / 效果质量 / 安全合规四类加权，权重在 `config/health.yaml`；缺失维度剔除后重归一化。

**前端**（`web/`，React 18 + TypeScript + Vite + Recharts + TanStack Query）：总览 Dashboard（含实时 SSE 运行）、运行列表、Trace 瀑布详情、质量评估/反馈、成本性能五个页面，全部读真实 API。开发期 Vite 把 `/api` `/run` 代理到 `:8000`。

```bash
cd web && npm ci && npm run dev        # 前端开发服务器（配合后端 :8000）
npm run lint                           # ESLint（CI 执行）
npm run typecheck                      # 类型检查（CI 执行）
npm run test:coverage                  # Vitest + 覆盖率（CI 执行）
npm run build                          # 生产构建（CI 执行）
```

数据库路径默认 `workspace/observability.db`（已 gitignore），可用 `DEVOPS_AGENT_DB_PATH` 覆盖。

## Roadmap（尚未实现）

以下是骨架已铺好、但代码尚未填充的部分，按建议顺序：

1. **首个能力域闭环**：选 `debug` 域，写真实 `FunctionTool` + system_prompt + 评测逻辑，跑通「提 bug → 定位 → 打分」；
2. **评测打分**：在 `eval/scorer.py` 实现 correctness 等维度，接入 recall@k / faithfulness / LLM-as-judge；
3. **结果台账**：实现跨 Agent 的 append-only 结果共享；
4. **技能沉淀**：`skills/`、`config/agents/` 的能力配置化。

> 已完成：网关流化（SSE）、端到端可观测性与前端（见上一节）。
