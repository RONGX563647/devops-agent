# devops-agent

基于 AgentScope 2.x 构建的 **DevOps 智能体平台**，覆盖三个能力域：可持续交付、项目排期、Bug 测试定位。

> 技术栈：Python 3.11+ / AgentScope 2.x / FastAPI / Docker 沙箱

---

## 核心设计原则

| 原则 | 落地 |
|---|---|
| **编排与执行分离** | 模型不可靠，执行必须可靠；`runtime` 管编排，`executor` 管沙箱 |
| **框架与业务分离** | `runtime/agent.py` 是唯一 import `agentscope` 的地方，业务不依赖框架 |
| **过程态与源码分离** | 运行态写 `workspace/`（不提交），代码改动走 git PR |
| **过程隔离、结果共享** | 并行 Agent 互不读私有记忆，通过 append-only 台账共享结果 |

---

## 目录结构

```
devops-agent/
├── pyproject.toml           # 唯一配置文件（打包 + 依赖 + 各工具配置）
├── .gitignore
├── config/
│   ├── policy.yaml          # 运行时策略：权限矩阵 / max_iters / 模型降级
│   └── gates.yaml           # 质量门禁阈值（CI 读取）
├── src/devops_agent/
│   ├── main.py              # FastAPI 入口（/health）
│   ├── runtime/             # 编排层
│   │   ├── agent.py         #   build_agent()：唯一 import agentscope
│   │   ├── supervisor.py    #   TeamPipeline / GoalPipeline 编排
│   │   └── runtime.py       #   AgentRuntime 协议（框架隔离接口）
│   ├── tools/catalog.py     # FunctionTool / Toolkit / ToolGroup
│   ├── executor/boundary.py # LocalWorkspace / DockerWorkspace 沙箱
│   ├── governance/policy.py # PermissionEngine 权限引擎
│   ├── memory/ledger.py     # Mem0Middleware 长期记忆
│   ├── eval/scorer.py       # 评测维度
│   └── gateway/             # 预留：SSE 网关
├── tests/                   # unit / integration / eval
├── skills/                  # 可复用技能（防重复造轮子）
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

# 3. 跑通健康检查
uvicorn devops_agent.main:app --port 8000
# 访问 http://127.0.0.1:8000/health → {"status":"ok"}
```

> 未安装包时可临时用 `PYTHONPATH=src uvicorn devops_agent.main:app` 运行。

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
| 模型降级 | `ModelConfig(fallback_model=...)` |
| 模型路由 | `ModelRouterMiddleware` |
| 编排 | `TeamPipeline` / `GoalPipeline` / `TeamMember` |

---

## 三个能力域

| 域 | Agent | 核心能力 |
|---|---|---|
| 可持续交付 | delivery | 环境生命周期、配方固化、流水线编排、门禁、漂移检测 |
| 项目排期 | schedule | 需求拆解、依赖排期、资源/人力、风险预警、进度同步 |
| Bug 测试定位 | debug | 测试生成、失败定位（证据 `file:line`）、修复建议 |

每个域通过 `build_agent(name, system_prompt, model, toolkit, max_iters)` 装配，三个域由 `TeamPipeline` 编排。

---

## 治理

- **边界**：权限矩阵 `PermissionContext(mode, allow_rules, deny_rules, ask_rules)` + Docker 沙箱隔离；
- **git/PR**：每任务一个 worktree + 独立分支 + 一个 PR，主分支永不禁直推；
- **记忆**：`Mem0Middleware` 长期记忆，任务状态用 `agentscope.state.AgentState`；
- **评测**：`recall@k`、faithfulness、变异测试（`mutmut`）、LLM-as-judge 回归。

## 质量门禁（CI）

`config/gates.yaml` 定义阈值：覆盖率 ≥ 80%、重复代码 ≤ 5%、圈复杂度 ≤ 10、变异测试 ≥ 70%。

工具链：`ruff`（风格）+ `mypy`（类型）+ `vulture`（死代码）+ `radon`（复杂度）+ `pytest-cov`（覆盖率）+ `mutmut`（测试效果）。

---

