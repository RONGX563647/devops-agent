# devops-agent

基于 AgentScope 2.x 构建的 **DevOps 智能体平台**，规划覆盖三个能力域：可持续交付、项目排期、Bug 测试定位。

> 技术栈：Python 3.11+ / AgentScope 2.x / FastAPI / Docker 沙箱
>
> **当前状态**：已完成框架接线与异步网关（可跑通真实模型），三个能力域的业务逻辑尚未填充。见文末「Roadmap」。

---

## 核心设计原则

| 原则 | 落地 |
|---|---|
| **编排与执行分离** | 模型不可靠，执行必须可靠；`runtime` 管编排，`executor` 管沙箱 |
| **框架与业务分离** | `runtime/agent.py` 是唯一 import `agentscope` 的地方，业务不依赖框架 |
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
│   └── gates.yaml           # 质量门禁阈值（check_gates.py 读取）
├── scripts/
│   └── check_gates.py       # 从 gates.yaml 读取阈值并执行 CI 门禁
├── src/devops_agent/
│   ├── config.py            # config/*.yaml 的唯一加载入口（强类型）
│   ├── main.py              # FastAPI 入口（/health + /run）
│   ├── runtime/             # 编排层
│   │   ├── agent.py         #   build_agent()：唯一 import agentscope
│   │   ├── supervisor.py    #   TeamPipeline / GoalPipeline 编排
│   │   └── runtime.py       #   AgentRuntime 协议（框架隔离接口）
│   ├── tools/catalog.py     # FunctionTool / Toolkit / ToolGroup
│   ├── executor/boundary.py # LocalWorkspace / DockerWorkspace 沙箱
│   ├── governance/policy.py # PermissionEngine 权限引擎
│   ├── memory/ledger.py     # Mem0Middleware 长期记忆
│   ├── eval/scorer.py       # 评测维度枚举（打分逻辑待实现）
│   └── gateway/             # 预留：SSE 网关（Roadmap）
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

# 3. 启动（未配模型凭据时 /run 走 EchoRuntime，便于自检）
uvicorn devops_agent.main:app --port 8000
# GET http://127.0.0.1:8000/health → {"status":"ok"}
# POST http://127.0.0.1:8000/run  {"session_id":"s1","user_input":"hello"}
#   → {"result":"[s1] hello"}      （Echo 模式回显）
```

> 未安装包时可临时用 `PYTHONPATH=src uvicorn devops_agent.main:app` 运行。

### 接入真实模型（OpenAI 兼容接口）

设置三个环境变量后，`/run` 自动切换到真实模型运行时；**不要把密钥写进代码或提交到仓库**，只通过环境变量注入。

```bash
export DEVOPS_AGENT_API_KEY='<your-api-key>'
export DEVOPS_AGENT_BASE_URL='https://<your-endpoint>/compatible-mode/v1'
export DEVOPS_AGENT_MODEL='<model-name>'
uvicorn devops_agent.main:app --port 8000

curl -X POST http://127.0.0.1:8000/run \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","user_input":"用一句话说明什么是 CI"}'
```

运行时的选择在 `main.select_runtime()` 中完成，并挂载到 `app.state.runtime`；测试可替换该对象而不必改环境变量。

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

---

## Roadmap（尚未实现）

以下是骨架已铺好、但代码尚未填充的部分，按建议顺序：

1. **首个能力域闭环**：选 `debug` 域，写真实 `FunctionTool` + system_prompt + 评测逻辑，跑通「提 bug → 定位 → 打分」；
2. **评测打分**：在 `eval/scorer.py` 实现 correctness 等维度，接入 recall@k / faithfulness / LLM-as-judge；
3. **结果台账**：实现跨 Agent 的 append-only 结果共享；
4. **网关流化**：`gateway/` 的 SSE 流式输出（当前 `/run` 为非流式）；
5. **技能沉淀**：`skills/`、`config/agents/` 的能力配置化。
