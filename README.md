# devops-agent

基于 AgentScope 2.x 构建的 **DevOps 智能体平台**，规划覆盖三个能力域：可持续交付、项目排期、Bug 测试定位。

> 技术栈：Python 3.11+ / AgentScope 2.x / FastAPI + SSE / SQLite / React 18 + TypeScript + Vite

**当前实际能力**（以 `src/` 代码为准，不含规划）：

- **单 Agent + 会话隔离**：每个 session 一份 Agent 实例，LRU 上限 16，状态快照落 SQLite，回收后可从库重建。
- **5 档权限**：档位 = 权限模式 + 工具集 + 越界兜底动作，一次切换三者同改。
- **确认挂起与续跑**：越界调用挂起 → `event: parked` → 前端作答 → 从同一 run 续跑。
- **输入管线 7 阶段**：引用探测/展开、附件抽取、密钥脱敏与保险箱、预算裁剪。
- **端到端可观测性**：事件流 → SQLite（runs / spans / input_stages / eval_scores）→ 观测 API → React 前端。
- **评测闭环**：题库 + 规则判卷 + 六维裁判 + 基线与回归对比 + 静态契约 lint + 夜间流水线。

三个能力域的业务提示词与真实工具函数**尚未填充**；`TeamPipeline` / `GoalPipeline` / Mem0 记忆 / Docker 沙箱目前只有装配工厂与单测，生产链路未接线。详见文末 [Roadmap](#roadmap尚未实现)。

---

## 核心设计原则

| 原则 | 落地 |
|---|---|
| **编排与执行分离** | `runtime/` 只做编排，`executor/boundary.py` 管沙箱执行面 |
| **框架与业务分离** | `import agentscope` 只出现在 9 个装配模块（下方清单），网关与可观测性只依赖协议/工厂 |
| **过程态与源码分离** | 运行态写 `workspace/`（已 gitignore），代码改动走 worktree + PR |
| **默认拒绝、显式放开** | 档位白名单来自配置，客户端不能靠请求体自行扩容到 `unrestricted`；未开放档返回 403 而不是静默降级 |
| **诚实降级** | 缺模型凭据回落 Echo 并在 `/health` 与前端横幅标明；缺 `rg` 只告警不拒绝启动 |

### 框架隔离边界（实测 9 个文件）

框架使用已放开（见 AGENTS.md §3.1）：允许直接使用 AgentScope / OpenJudge 的类型与 API，不再维护
`import agentscope` 白名单。第三方 pip 依赖 `openjudge`（`py-openjudge`）仍只在
`src/devops_agent/openjudge/` 内 import，保持可选依赖隔离 —— 没装 `[openjudge]` extra 时其余功能不受影响。
分层依赖仍由 `.importlinter` + `lint-imports` 强制：`prompt_lint/`、`input/`、`observability/`
不直接 import `agentscope`，网关不反向依赖 runtime/observability。`runtime/events.py` 仍靠 duck typing
转译事件，因此可以用假事件测试。

---

## 目录结构

```
devops-agent/
├── pyproject.toml            # 打包 + 依赖 + ruff/mypy/pytest 配置
├── config/
│   ├── policy.yaml           # 权限回落 mode / 档位白名单 / 命令白名单 / builtin deny
│   ├── model.yaml            # 模型参数部署默认（窗口 / 两级重试 / 采样 / 压缩比例）
│   ├── gates.yaml            # 质量门禁阈值（check_gates.py 读取，唯一来源）
│   └── health.yaml           # 健康分权重、延迟/token 基准、评分来源权重
├── eval/
│   ├── fixtures/             # 5 个本地仓库样本（bare-repo / dirty-repo / docs-only / node-service / python-basic）
│   ├── openjudge/            # 5 赛道 90 例题库、plan 权重、覆盖率基线、fixtures、rubric 工件
│   └── lint-baseline.json    # 已接受的存量 lint 违规清单
├── scripts/
│   ├── check_gates.py        # 读 gates.yaml 并实际执行覆盖率/死代码/复杂度
│   └── check_file_size.py    # 单文件 500 行棘轮
├── docs/
│   ├── adr/                  # 0001 可观测前端 / 0002 权限模型 / 0003 模型参数
│   ├── specs/                # 0001 模型参数配置 / 0002 评测 UI 控制台
│   └── wiki/                 # 分层讲解（Home / Architecture / Permissions / …）
├── .github/workflows/        # ci.yml（quality + frontend）、openjudge-nightly.yml
├── docs/
│   ├── adr/                  # 0001 可观测前端 / 0002 权限模型 / 0003 模型参数
│   ├── specs/                # 0001 模型参数配置 / 0002 评测 UI 控制台
│   └── wiki/                 # 分层讲解（Home / Architecture / Permissions / …）
├── .github/workflows/        # ci.yml（quality + frontend）、openjudge-nightly.yml
├── src/devops_agent/
│   ├── config.py             # config/*.yaml 的唯一加载入口（@cache 强类型）
│   ├── env.py                # 进程启动加载 .env（override=False）
│   ├── gateway/              # FastAPI 入口：会话/权限/SSE/挂起确认
│   ├── model_api.py          # 模型参数端点（部署默认 + 会话覆盖）
│   ├── model_settings.py     # 参数五层解析与 derived 推算
│   ├── runtime/
│   │   ├── assembly.py       #   build_agent / build_agentscope_runtime（系统提示、工具、中间件）
│   │   ├── sessions.py       #   SessionRegistry：LRU + DB 快照重建 + 参数热替换
│   │   ├── confirm.py        #   确认协议：风险分级、作用域、前缀沉淀规则
│   │   ├── profile_note.py   #   档位说明 + 联网说明追加在消息末尾（保前缀缓存）
│   │   ├── compression.py    #   压缩中间件 → SSE 帧
│   │   ├── events.py         #   框架事件 → 普通 dict
│   │   ├── supervisor.py     #   TeamPipeline / GoalPipeline 装配（仅单测引用）
│   │   └── runtime.py        #   AgentRuntime 协议 + EchoRuntime
│   ├── governance/
│   │   ├── profiles.py       #   5 档定义：mode + tools + fallback
│   │   └── policy.py         #   PermissionContext / Engine、bash_decision 静态预演
│   ├── tools/
│   │   ├── catalog.py        #   工具名唯一事实源 + build_toolkit_for
│   │   ├── schema.py         #   按档位导出 OpenAI 形状 schema
│   │   ├── preflight.py      #   外部可执行依赖探测（rg）
│   │   ├── api.py            #   /api/tools/schema、/api/tools/preflight
│   │   └── context_api.py    #   /api/context：本轮真正装配了什么
│   ├── input/
│   │   ├── pipeline.py       #   7 阶段解析（normalize → compose）
│   │   ├── attachments.py    #   上传/抽取（txt/md/pdf/docx/xlsx/pptx）
│   │   ├── events.py         #   阶段结果 → SSE 帧
│   │   └── api.py            #   /api/attachments
│   ├── secrets/
│   │   ├── vault.py          #   Fernet 加密保险箱 + 占位符注入/回抹
│   │   ├── capture.py        #   从输入里识别凭据
│   │   ├── middleware.py     #   SecretInjectionMiddleware（工具边界解密）
│   │   └── api.py            #   /api/secrets
│   ├── observability/
│   │   ├── models.py         #   Run / Span / EvalScore
│   │   ├── repository.py     #   SQLite 读写入口（runs/spans/… 8 张表；secrets 表在 vault.py）
│   │   ├── recorder.py       #   事件流 → run + spans + input_stages
│   │   ├── health.py         #   四维加权健康分
│   │   ├── api.py            #   /api/runs、/api/metrics、/api/feedback
│   ├── openjudge/
│   │   ├── benchmark.py      #   用例模型、五赛道枚举、覆盖率基线比对
│   │   ├── casestore.py      #   题库读取：仓库 → 覆盖层 → 去隐藏（在线改用例的落点）
│   │   ├── settings.py       #   oj_settings：被测/裁判模型、默认开关、并发与超时
│   │   ├── run.py            #   采集 → 判分 → 落盘 → 入库的唯一流水线
│   │   ├── store.py          #   批次 / 用例 / rubric / 配置存储
│   │   ├── run_store.py      #   oj_runs（在线任务进度与单飞锁）
│   │   ├── service.py / api.py #  在线任务生命周期与 /api/openjudge/*（只读）
│   │   ├── api_edit.py       #   /api/openjudge/{settings,cases}（可写，与 api.py 分开挂）
│   │   ├── report.py         #   聚合、赛道判决、Markdown 渲染
│   │   ├── grade.py / rubric.py / harness.py # 裁判、权重、真跑
│   │   └── cli.py            #   validate / run / grade / compare / rubric
│   ├── prompt_lint/          # P1–P4 提示词静态契约（不跑模型、不联网）
│   ├── executor/boundary.py  #   LocalWorkspace / DockerWorkspace 工厂
│   └── memory/ledger.py      #   Mem0Middleware 工厂（未接线）
├── web/                      # React 18 + TS + Vite 前端
├── tests/                    # unit / integration
├── skills/, config/agents/   # 占位（仅 .gitkeep）
└── workspace/                # 运行态：observability.db、附件、评测报告、题库覆盖层（不提交）
```

---

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 未配模型凭据时自动走 EchoRuntime，便于自检
uvicorn devops_agent.gateway.app:app --port 8000
# GET  http://127.0.0.1:8000/health → {"status":"ok","runtime":{"kind":"echo",...},"tools":[...]}
# POST http://127.0.0.1:8000/run/stream  {"session_id":"s1","user_input":"hello"}
#   → SSE 事件流，Echo 模式回显 "[s1] hello"
```

未安装包时可临时用 `PYTHONPATH=src uvicorn devops_agent.gateway.app:app`。

### 接入真实模型（OpenAI 兼容接口）

`.env.example` 复制为 `.env` 并填值（`.env` 已 gitignore）。[env.py](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/src/devops_agent/env.py) 用 `override=False` 加载，**已存在的环境变量优先**，CI/容器注入值不会被本地文件覆盖。

```bash
cp .env.example .env

# 也可直接用环境变量：
export DEVOPS_AGENT_API_KEY='<your-api-key>'
export DEVOPS_AGENT_BASE_URL='https://<your-endpoint>/compatible-mode/v1'
export DEVOPS_AGENT_MODEL='<model-name>'

curl -N -X POST http://127.0.0.1:8000/run/stream \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","user_input":"用一句话说明什么是 CI"}'
```

运行时的选择在 `main.select_runtime()`：`DEVOPS_AGENT_API_KEY` 非空即装配真实 Agent，否则回退 Echo。
结果挂在 `app.state.runtime`，测试可直接替换该对象。

| 环境变量 | 作用 |
|---|---|
| `DEVOPS_AGENT_API_KEY` / `_BASE_URL` / `_MODEL` | 模型接入（key 为空即 Echo） |
| `DEVOPS_AGENT_CONFIG_DIR` | 覆盖 `config/` 位置（测试/部署用） |
| `DEVOPS_AGENT_DOMAIN` | 运行域标记，写入 `runs.domain`，缺省 `debug` |
| `DEVOPS_AGENT_WORKDIR` | Agent 工具的工作目录（shell cwd + 文件工具路径基准） |
| `DEVOPS_AGENT_WORKSPACE` | 输入管线解析本地引用的根目录，缺省进程 cwd |
| `DEVOPS_AGENT_ALLOW_REMOTE_FETCH` | 设为 1 才允许输入管线抓取远程 URL（默认关闭） |
| `DEVOPS_AGENT_DB_PATH` | 观测库路径，缺省 `workspace/observability.db` |
| `DEVOPS_AGENT_ATTACHMENT_DIR` | 附件目录，缺省 `workspace/attachments` |
| `DEVOPS_AGENT_SECRET_KEY` | Fernet 主密钥（base64，优先级最高） |
| `DEVOPS_AGENT_SECRET_KEY_FILE` | Fernet 主密钥文件，缺省库同目录 `.secret.key` |
| `DEVOPS_AGENT_ENABLE_SEARCH` | 设为 1 时请求体附 `enable_search=true`（默认关闭） |
| `DEVOPS_AGENT_SEARCH_MODE` | `forced`（默认，连同 `search_options` 下发）/ `auto`（只授权不强制） |
| `DEVOPS_AGENT_PERMISSION_PROFILES` | 覆盖开放档位（逗号分隔） |
| `DEVOPS_AGENT_DEFAULT_PROFILE` | 覆盖默认档位 |
| `DEVOPS_AGENT_JUDGE_MODEL` | 评测裁判模型的兜底值（页面配的 `judge_model` 优先；nightly 用它） |
| `DEVOPS_AGENT_EVAL_DB_PATH` | 评测库路径 |
| `DEVOPS_AGENT_OPENJUDGE_OVERLAY` | 题库覆盖层目录，缺省 `workspace/openjudge/cases`（测试靠它隔离） |

### 模型侧联网搜索

`DEVOPS_AGENT_ENABLE_SEARCH=1` 时，模型请求体会附加 `enable_search=true`，由供应商（如 DashScope 兼容接口）执行联网检索。**默认关闭**。这是环境变量层的部署默认；前端另有逐请求的「联网」开关。

只有 `enable_search` 是不够的：它只是**授权**模型可以搜，搜不搜由模型自由裁量。实测同一份请求体（system prompt + 4 个工具定义 + 只读提示），带与不带 `enable_search` 的 `prompt_tokens` 完全一致（3150 vs 3150）——模型一次都没检索，用户看到的就是一个按下去没反应的按钮。因此默认还会下发 `search_options = {"forced_search": true, "search_strategy": "standard"}`，由供应商无条件检索并把结果注入上下文（同一请求体升到 4436 tokens）。

- `DEVOPS_AGENT_SEARCH_MODE=forced`（默认）：如上，点了就一定搜。
- `DEVOPS_AGENT_SEARCH_MODE=auto`：只发 `enable_search`，把裁量权交回模型（省 token，但多数情况下不会搜）。写成其他值一律按 `forced` 处理，避免拼错一个环境变量就把联网退化成空开关。

开关实现在 [chat_model.py](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/src/devops_agent/runtime/chat_model.py) 的 `_search_extra_body()`。

开启联网的那一轮，末条消息会追加一段说明：检索结果**若存在**，会以带来源的引用块出现在上下文里，看到就用、看不到就直说没有检索到；并明确「不需要再用工具抓网页，同一地址失败两次就停」。这句话与档位说明一样放在消息末尾而不是系统提示里——系统提示在请求最前面，改一个字后面的工具 schema 与整段历史全部缓存失效。

之所以写成「若存在」而不是「已附带」：应用侧拿不到供应商到底有没有检索的证据（框架不透出 `search_info`，事件流里也没有检索 span），写死「已附带」会让模型去核对一段它无法辨认的内容，反而触发「我得自己抓一遍」的验证循环。

前端输入框下方另有「联网」开关：网页端**每次都显式表态**（开 → 带 `web_search: true`，关 → `false`），所以界面上的「关」在开了环境变量的部署里也真的关得掉。只有直接调 API 时省略该字段才表示「按部署默认」。这样不必为了偶尔查资料把整个部署的检索打开。

- 检索发生在供应商侧，事件流里没有工具调用 span，只能看到最终回答，无法审计检索了哪些页面。
- 供应商或模型不支持该参数时可能直接返回 400；关掉开关即可回退。
- 检索通常按次计费，开启前请确认额度与共享环境的影响。

## 权限：5 个档位

档位不是直接暴露 `PermissionMode` —— 只切 mode 而工具集不动，会出现「切了档什么都没变」。
所以一个档位 = **mode + 工具集 + 越界兜底动作**，三者一起换（[profiles.py](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/src/devops_agent/governance/profiles.py#L50-L99)）：

| 档位 | 标签 | mode | 工具集 | 越界兜底 | 说明 |
|---|---|---|---|---|---|
| `readonly` | 只读 | `explore` | bash/read/glob/grep | `deny` | 只读命令放行，越界直接拒绝，不打断 |
| `workspace` | 工作区可写 | `accept_edits` | 只读 + write/edit | `ask` | 工作区内建/删/改，越界弹确认卡片 |
| `execute` | 可执行 | `accept_edits` | 只读 + write/edit | `ask` | 工作区权限 + `policy.command_allowlist` 注入为 allow_rules |
| `auto` | 无人值守 | `dont_ask` | 只读 + write/edit | `deny` | 定时任务用：区内读写，越界拒绝且不弹确认 |
| `unrestricted` | 全放开 | `bypass` | 只读 + write/edit | `hard_deny` | 不逐次询问，只剩 deny_rules；**默认不在可用集** |

- `DEFAULT_AVAILABLE = ("readonly", "workspace", "execute", "auto")`；默认档为 `workspace`（`config/policy.yaml`）。
- 工具名的唯一事实源是 [catalog.py](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/src/devops_agent/tools/catalog.py#L41-L43)：`READONLY_TOOL_NAMES = ("bash","read","glob","grep")`、`WRITE_TOOL_NAMES = ("write","edit")`。
  `build_toolkit_for(names, workdir, middlewares)` 是档位切换唯一入口，未知工具名抛 `ValueError`（静默跳过会把「放开档少了写工具」表现成「权限切了但模型还是不写文件」）。
- 每个工具**独占一个 `LocalBackend(workdir=cwd)`**：不给 backend 时框架自建 `workdir=None` 的实例，相对路径会回落到**服务进程 cwd**，表现为「Grep/Glob 不传 path 就搜错地方」。
- builtin deny 对所有档位无条件生效（`Bash`/`Write`/`Edit` 匹配 `.git/`，`Bash` 匹配 `.env`）——否则 `auto` 档能直接读密钥。
- 请求里的档位未开放或不存在 → **403 `permission_not_available`**，不静默降级。

**定位要说明白**：这套档位是防误操作的 UX 层，不是安全隔离。真正的隔离靠容器、只读挂载、网络策略、非 root 运行；`deny_rules` 只是点防御，`Bash` 走子串/前缀匹配，挡不住 `python -c "shutil.rmtree(...)"`。设计取舍见 [ADR-0002](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/docs/adr/0002-permission-model.md)。

### 档位说明为什么追加在消息末尾

[profile_note.py](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/src/devops_agent/runtime/profile_note.py#L1-L10) 把「本会话权限：…」作为最后一条 user 消息下发，而不是写进系统提示：前缀缓存按字节比对，系统提示改一个字会让后面的工具 schema 与整段历史全部作废。同一段 10 轮历史实测，跨档缓存命中从约 21% 提升到约 96%。这条说明不写入历史快照，因此不会逐轮累积。

### 确认挂起与续跑

1. 权限引擎判定为 ASK → 运行时停在 `ASKING`，`pending_calls()` 返回待确认列表；
2. SSE 收尾发 `event: parked`（**不发 `done`**），挂起态连同命令原文落库，TTL **600 秒**；
3. 前端回 `POST /run/{run_id}/confirm`，只传 `tool_call_id + confirmed + scope`；
4. 服务端用权威副本重建事件并 `resume()`，同一个 run 继续。

三条硬约束（[confirm.py](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/src/devops_agent/runtime/confirm.py#L1-L10)）：

- **客户端永远不回传命令原文**，否则等于允许客户端改写将要执行的命令；
- 一个 run 最多打扰 **3 次**（`ASK_LIMIT`），超限自动全部拒绝，让模型换思路；
- 高危命令（`rm -rf` / `mkfs` / `dd if=` / fork bomb …）只允许 `scope=once`，不允许沉淀成规则。

`scope` 三档与 `prefix` 沉淀规则：`git commit -m x` 沉淀为 `git commit:*` 而不是 `git:*`；复合命令（`&&`/`||`/`;`/`|`/换行）一律不沉淀；`rm`/`bash`/`python -c` 等永不沉淀；包装层（`timeout`/`nice`/`env`/`nohup`…）先剥掉再取程序名。

### 会话隔离

[runtime/sessions.py](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/src/devops_agent/runtime/sessions.py) 的 `SessionRegistry`：每 session 一份 Agent，`max_entries=16`。
三条独立理由足以说明为什么必须隔离：规则串味（A 会话点「本会话允许」会放行 B）、挂起互斥（两个会话同时 ASKING 无法分辨该答哪个）、历史串味。

- 命中复用；**档位或联网开关变了就重建**；未命中从 `session_states` 快照重建 → 「回收不等于丢失」，不需要粘性会话。
- `peek()` 只取不重建，供运行中切档端点使用（`acquire()` 会按请求档位重建，正在跑的流会被换掉）。
- `switch()` 有热替换能力时原地改（`applied=True`），没有时重建并**如实返回 `applied=False`**，前端不能说「立即生效」。
- 部署默认参数变更：正在跑的会话标记 `pending_model`，run 结束由 `flush_pending()` 补上；返回 `{"applied": n, "deferred": m}`。
- 同一会话同时只跑一个 run：SSE 整段持有 `entry.lock`，并发请求返回 **409 `session_busy`**。

---

## 输入管线：7 个阶段

[runtime 之前的 `input/prepare()`](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/src/devops_agent/input/pipeline.py) 把用户输入整理成「真正进模型的那段文本」，过程作为 SSE 帧推给前端画轨迹（也落 `input_stages` 表）：

| 阶段 | 名称 | 做什么 |
|---|---|---|
| `normalize` | 归一化 | 统一换行、空白与空行，不改变语义 |
| `detect` | 引用探测 | 识别 URL / 本地路径 / git 引用 / 附件，最多 `MAX_REFERENCES=5` |
| `expand` | 引用展开 | 读本地文件（`MAX_FILE_BYTES=256KB`）或抓远程（超时 5s、上限 512KB） |
| `attach` | 附件读取 | 按 id 取回附件正文 |
| `redact` | 密钥脱敏 | 识别 aws/openai/github/slack/jwt/私钥等，替换为 `{{secret:NAME}}` 并交给 `on_secret` 落库 |
| `budget` | 预算裁剪 | 总字符上限 `MAX_TOTAL_CHARS=24000`，超出按优先级丢弃 |
| `compose` | 组装 | 正文 + 已取到的材料 + **未取到的材料清单**，拼成最终文本 |

**任何阶段都不抛异常**：解析失败只影响该阶段状态与材料完整性，对话照常进行（`main._prepare_input` 再兜一层，异常即退回原始输入）。

### 为什么「取不到」也要写进上下文

引用抓取失败时，把原因只写进阶段详情（前端能看、模型看不到）会引发非常昂贵的行为：
模型不知道自己手里缺了什么，于是自己去 `curl` 同一个地址，被 shell 拆参数、被网络重置，
再换 PowerShell、换 python 脚本、换代理设置——实测一次会话烧掉 **21 步工具调用**
才承认拿不到。

所以 `_expand` / `_attach` 除了材料块还返回 `misses`，由 `_render_misses()` 渲染成
「本轮未能取到的材料」清单 + 停止规则（同一目标失败两次就停、基于已有材料回答并
说明缺什么）。两个细节：

- **`远程抓取未开启` 不进 `misses`**：这只是服务端没代为抓取，不是内容不可得。
  写进去等于替模型宣布放弃，反而劝退它用 shell 自己取。
- **`misses` 也要过一遍脱敏**：失败说明里会带原始 URL，URL 里可能含 token。

补充：

- 只有 `prepared.text` 进模型上下文，密钥明文与裁剪细节不外泄。
- token 估算用 `len(text) // 4`（`CHARS_PER_TOKEN`），用于与模型回报的 `input_tokens` 对账。
- 附件：`POST /api/attachments` 上传，单文件上限 10MB、最多 8 个、正文截断 40k 字符，保留 7 天；支持 txt/md/csv/json/html/xml、源码、`.docx`/`.xlsx`/`.pptx`（OOXML 抽文本）、`.pdf`（需 `pip install -e ".[parse]"`）。

### 密钥保险箱

- 凭据用 Fernet（AES128-CBC + HMAC）加密存 SQLite 的 `secrets` 表。主密钥优先级（`vault.load_key`）：`DEVOPS_AGENT_SECRET_KEY`（urlsafe base64，32 字节）→ `DEVOPS_AGENT_SECRET_KEY_FILE` 指向的文件 → 已有文件 → 自动生成到库同目录 `.secret.key`（0600）。换密钥后旧密文解不开，此时按「未命中」处理，占位符原样透传，不会让整轮失败。
- 工具边界由 `SecretInjectionMiddleware` 把 `{{secret:NAME}}` 换成真值，工具输出里若带回真值再抹回占位符 —— **模型全程看不到明文**。占位符语法在 `vault.PLACEHOLDER_PATTERN` 与 `pipeline._PLACEHOLDER_RE` 各写一份，由一条对账测试钉住。
- 对外只暴露四件事：写、取、列、删（`/api/secrets`），加密细节收在 `vault.py`。

---

## 模型参数：五层解析

```
会话覆盖（DB: session_states.model_config）
  > 部署默认（DB: settings['model_config']，前端可改、持久化）
  > config/model.yaml
  > 环境变量（model / judge_model / enable_search）
  > 框架默认（context_size / 两级重试 / ContextConfig）
```

`config/model.yaml` 是部署默认的唯一出处，逐项注释了取值理由；改它需重启，改前两层不需要。
`model_settings.resolve()` 是唯一入口，任何地方要参数都必须走它，不要自己读 yaml 或 env。

```yaml
model:
  name: ""                  # 空 = 用 DEVOPS_AGENT_MODEL
  context_size: 128000      # 决定压缩阈值、圆环分母、压缩后保留量
  context_calibrated: false # 未核实真实窗口时前端显示「⚠ 未校准」
  model_max_retries: 3      # 供应商 HTTP 层重试
  agent_max_retries: 3      # 一轮 ReAct 内模型调用重试
  max_iters: {default: 12, delivery: 16}
  trigger_ratio: 0.8        # 占窗口多少开始压缩（框架硬约束 <= 0.9）
  reserve_ratio: 0.1        # 必须 < trigger_ratio
```

**两级重试必须分开看**：最坏请求次数 = `(1 + model_max_retries) × (1 + agent_max_retries)` = `4 × 4 = 16`。前端直接显示这个乘积，不让用户自己乘。

- `derived` 字段（`compression_threshold`、`worst_case_attempts`）由后端算好，前端不重复实现口径。
- `patch` 里的 `null` 表示**删除这一层覆盖、回落下层**，不是「设成 null」。
- `api_key` / `base_url` / `stream` 只展示不可改：凭据不该经 HTTP 往返，关掉流式会让前端退化为一次性渲染。
- 改动写进 `runs.model_params`（只存非默认值），并计入 `prompt_factors()` 的 `params_sha` —— 参数变过的批次在评测对比中标为不可直接比较。

设计见 [SPEC-0001](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/docs/specs/0001-model-parameter-config.md) 与 [ADR-0003](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/docs/adr/0003-model-parameter-config.md)。

---

## HTTP 接口

### 会话与运行（`gateway/`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 存活 + `runtime.kind`（echo/agentscope）+ 工具外部依赖状态（缺依赖不返 5xx） |
| GET | `/api/permissions` | 本部署开放档位（含 label / note / fallback / write） |
| POST | `/api/session/{session_id}/permission` | 运行中切档，返回 `applied` 与 `effective_from` |
| POST | `/run/stream` | SSE 主入口；`X-Request-ID` 作幂等键 |
| GET | `/run/{run_id}/pending` | 刷新后恢复挂起卡片（必须带 `session_id` 校验归属） |
| POST | `/run/{run_id}/confirm` | 带决策续跑；用条件更新保证只执行一次 |

`POST /run/stream` 请求体：`{"session_id", "user_input", "attachments": [], "permission": null, "web_search": null}`。
`web_search` 省略或传 `null` = 按部署默认（`DEVOPS_AGENT_ENABLE_SEARCH`）；显式 `true`/`false` = 这一轮就按它来。默认值不能写 `False` —— 那样「没带字段」会被当成「关」，环境变量永远不生效。

SSE 帧约定：

- 每个事件一帧 `data: {...}`，类型包括 `INPUT_PARSE_START` / `INPUT_PARSE_STAGE` / `INPUT_PARSE_END`（输入管线）、`CONTEXT_COMPRESSION_START|END`（压缩检查），以及 Agent 事件 `REPLY_START` / `TEXT_BLOCK_DELTA` / `MODEL_CALL_START|END` / `TOOL_CALL_START|END` / `TOOL_RESULT_END` / `REQUIRE_USER_CONFIRM` / `EXCEED_MAX_ITERS`；
- 输入管线与压缩检查的帧在前，Agent 事件在后；`RECORDER` 只把 `SPAN_EVENT_TYPES`（模型调用、工具调用、确认、超迭代数）落成 span，delta 类事件仅用于流式渲染；
- 静默超过 **15s** 补一帧注释 `: ping`（用独立 pump/beat 双任务，避免 `wait_for` 取消上游生成器）；
- 结束：`event: done`（带 `run_id`、`status`、token 与缓存命中）或 `event: parked`（带 `expires_at` 与 `tool_calls`）。

### 其它路由

| 模块 | 路径 |
|---|---|
| 附件 | `POST /api/attachments`、`DELETE /api/attachments/{id}` |
| 密钥 | `GET /api/secrets`、`GET /api/secrets/{name}/value`、`DELETE /api/secrets/{name}` |
| 工具 | `GET /api/tools/schema?profile=`、`GET /api/tools/preflight` |
| 上下文 | `GET /api/context?session_id=&permission=` |
| 模型参数 | `GET|PATCH /api/model-config`、`POST /api/model-config/reset`、`GET|PATCH|DELETE /api/session/{id}/model-config` |
| 观测 | `GET /api/runs`、`/api/runs/{id}`、`/spans`、`/input-stages`、`/health`、`/api/metrics/{summary,health,breakdown,timeseries}`、`POST /api/feedback/{run_id}`、`GET /api/eval/trends` |
| 评测批次 | `GET /api/openjudge/{suites,batches,batches/{id},batches/{id}/cases,compare,health,series,runs,runs/{id}}`、`POST /api/openjudge/runs`、`POST /api/openjudge/runs/{id}/cancel` |
| OpenJudge 基准 | `GET /api/openjudge/{health,batches,batches/{id},batches/{id}/cases,cases/{id},compare,coverage,rubrics}` |
| 评测配置与题库（可写） | `GET|PATCH /api/openjudge/settings`、`GET /api/openjudge/cases?track=`、`GET|PUT|DELETE /api/openjudge/cases/{id}`、`POST /api/openjudge/cases/{id}/restore` |

> 单条用例有两个 id 空间：顶层 `/cases/{id}` 是**题库**里的用例（可编辑定义，`id` 是字符串），
> 批次内单条明细是 `/batches/{batch_id}/cases/{row_id}`（`row_id` 是 `oj_cases` 自增主键）。
> 两者同路径会让 FastAPI 只认先注册的那个，所以批次明细必须带 `batch_id`。

`/api/tools/schema` 与 `/api/context` 都**按档位返回**：放开档实际挂了 Write/Edit，写死只读四件套会让界面上的表是错的。`/api/context` 还会给出系统提示、工具 schema 的体积与指纹 —— 工具 schema 的 JSON 有一万字符，是系统提示的二十多倍，只说「工具 4 个」等于把大头藏起来。

---

## agentscope 真实 API 映射

> AgentScope 2.x **没有 `HarnessAgent`**，核心是 `Agent + ReActConfig`，其余能力由模块组合。

| 本项目的职责 | agentscope 类 | 生产接线 |
|---|---|---|
| Agent 核心 | `Agent(name, system_prompt, model, toolkit, ...)` | ✅ `runtime/assembly.py` |
| 工具装配 | `Bash` / `Read` / `Glob` / `Grep` / `Write` / `Edit` + `Toolkit` / `ToolGroup` | ✅ `tools/catalog.py` |
| 沙箱执行面 | `LocalWorkspace` / `DockerWorkspace` | Local ✅；Docker 仅单测 |
| 权限治理 | `PermissionEngine` + `PermissionContext` + `PermissionRule` | ✅ `governance/policy.py` |
| 上下文压缩 | `ContextConfig` + 压缩中间件 | ✅ `runtime/compression.py` |
| 长期记忆 | `Mem0Middleware` / `AgenticMemoryMiddleware` | ❌ 仅工厂 + 单测 |
| 编排 | `TeamPipeline` / `GoalPipeline` / `TeamMember` | ❌ 仅工厂 + 单测 |
| 模型路由 | `ModelRouterMiddleware` | ❌ 未实现 |

shell 一律用内置 `Bash` 而不是按平台切 `PowerShell`：`Bash` 会按命令判定只读（`ls`/`cat`/`git status` 直接放行），而 `PowerShell` **每条命令都返回 ASK**；服务端没有人工确认通道时，ASK 会退化成「发一个确认事件后静默结束」，表现为一轮对话无输出也无报错 —— 这正是修复前的线上现象。`Bash` 在 Windows 上走 `cmd /c`，POSIX 上走 `/bin/sh -c`。

`Grep` 依赖外部可执行 `rg`（`LocalBackend.exec_shell` 直接 spawn，不走 shell）；缺失时表现为 `exit 127` 而不是「命令不存在」。`tools/preflight.py` 用 `shutil.which` 在启动时探测，通过 `/health`、`/api/tools/preflight` 与启动日志暴露安装提示。

---

## 可观测性

后端在事件流上同步落库，前端把「运行好坏」画成量化画像。设计见 [ADR-0001](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/docs/adr/0001-agent-observability-frontend.md)。

SQLite 8 张表：`runs`、`spans`、`input_stages`、`eval_scores`、`session_states`、`settings`（`observability/repository/`）与 `secrets`（`secrets/vault.py`，同库不同模块）。评测批次不进这张库：`oj_batches` / `oj_cases` / `oj_rubrics` / `oj_runs` 在独立的 `workspace/evals/openjudge.db`，因为「删旧批次」在生产库里是高风险操作。

**健康分（0–100）**四维加权，权重在 `config/health.yaml`：

| 维度 | 权重 | 口径 |
|---|---|---|
| execution | 0.35 | 成功/失败、未收敛、工具失败率 |
| efficiency | 0.25 | 耗时（按模型调用次数给预算）与次均 token 强度 |
| quality | 0.25 | correctness 评分，按来源加权（human 1.0 / judge 0.8 / rule 0.6），向 0.5 先验收缩 |
| safety | 0.15 | 权限拒绝与重复越界 |

时间预算 = `latency_baseline_ms + per_step_baseline_ms × (模型调用次数 - 1)`，超预算按 `100 × (预算/实际)^0.6` 衰减 —— 固定基准会把 5 步的正常长任务判成 0 分。等级 A≥90 / B≥80 / C≥70 / D≥60，低于 D 为 F；缺失维度剔除后按剩余权重重归一化。

前端（`web/`，React 18 + TS + Vite + Recharts + TanStack Query）6 个路由：

| 路由 | 页面 |
|---|---|
| `/` | Workspace：对话、权限选择、确认卡片、Trace 面板、密钥页签 |
| `/dashboard` | 总览（含实时 SSE 运行） |
| `/runs`、`/runs/:id` | 运行列表 / Trace 瀑布与健康拆解 |
| `/quality?view=overview\|batches\|scoreflow\|studio` | 质量四视图：概览 / 批次 / 评分流程 / 题库与模型（后两者可改被测与裁判模型、直接改题库、改用例分层） |
| `/cost` | 成本与性能 |

```bash
cd web && npm ci && npm run dev    # 开发期 Vite 把 /api /run 代理到 :8000
npm run lint / typecheck / test:coverage / build
```

---

## 评测体系

**唯一实现是 `devops_agent.openjudge`**（旧 `devops_agent.eval` 已整体删除）：五赛道 90 例，
题库在 `eval/openjudge/cases/`（在线改动落在 `workspace/openjudge/cases/` 覆盖层，不碰仓库文件），
用 `py-openjudge` 的语义 Grader 判分，写入独立 SQLite（默认 `workspace/evals/openjudge.db`），
与生产库完全分离。

分三层：**纯数据与纯函数**（`benchmark` / `trace` / `gates` / `metrics` / `report`，不 import 模型 SDK、不联网）、
**执行流水线**（`run.execute`，在临时工作区真跑）、**两个入口**（CLI 与在线评测，共用同一条
流水线 —— 否则页面结论会与 CI 结论打架）。

### 赛道与阈值

| 赛道 | 内容 | 用例 | 判据（`gates.TRACK_RULES`） |
|---|---|---|---|
| A | 仓库问答 | 28 | `pass_rate ≥ 0.60` 且 `judge ≥ 0.60`，`min_n 10` |
| B | 权限边界 | 24 | `pass_rate ≥ 0.80` 且**零违规**，`min_n 10` |
| C | 改动能力 | 14 | `pass_rate ≥ 0.80` 且 `judge ≥ 0.60`，`min_n 8` |
| D | 输入管线 | 12 | `pass_rate = 1.00`，`min_n 8` |
| E | 多轮会话 | 12 | `pass_rate ≥ 0.70`，`min_n 8` |

`min_n` 是「样本不足不判」的闸门：批次里该赛道不足 `min_n` 条时给 `insufficient`，
而不是拿 3 条用例的 100% 当结论。

### 结论语义（SPEC-0008）

一个 `verdict` 曾同时背「模型好不好」「证据齐不齐」「测全了没有」，只能取最悲观值，
于是 90 条全过也永远是 `insufficient`。现在拆开：

| 字段 | 回答 |
|---|---|
| `verdict` | `pass` / `fail` / `partial` / `unvalidated` / `insufficient` |
| `scope` | 测了哪些赛道（`single_track` / `selected` / `full`） |
| `causes[]` | 为什么没过，每条带 `blocking`（否决）或 `advisory`（只提示） |
| `evidence` | 裁判档位、校准样本量、实测赛道数 |

`fail` 优先；否则没测全 → `partial`（局部结论，不作发布门禁）；否则裁判没跑/未校准
→ `unvalidated`；全清才 `pass`。裁判不否决门禁（`judge_*` 全是 advisory），规则断言永远有效。
校准分三档：`off` / `soft`（`n≥30 && ρ≥0.60`）/ `hard`（`n≥100 && ρ≥0.70`）。

`trials`（默认 1、上限 3）给 `pass@k` / `pass^k` 与 Wilson 区间；用例分
`must_pass`（默认，一票否决）与 `challenge`（只进挑战分）两层。

### CLI 五个子命令

```bash
pip install -e ".[dev,openjudge]"          # 第三方 openjudge 为可选依赖
python -m devops_agent.openjudge validate  --strict           # 结构 + 覆盖率基线
python -m devops_agent.openjudge run       --concurrency 4 --timeout 180 \
    --db workspace/evals/openjudge.db --out-dir workspace/evals/nightly
python -m devops_agent.openjudge run       --target-model deepseek-v4.1-flash \
    --judge-model qwen3-235b-a22b          # 覆盖页面里配的模型（只这一次）
python -m devops_agent.openjudge grade     --traces ... --out-dir ...   # 换 plan/rubric 重判
python -m devops_agent.openjudge compare   --base 1 --head 2
python -m devops_agent.openjudge rubric    generate --name repo_qa_quality --version v1
```

- `validate` 的第二半是**覆盖率棘轮**：`eval/openjudge/coverage.json` 记录各赛道、工具、subject、
  risk 的覆盖数，`benchmark.compare_coverage()` 只许涨不许跌。只查结构的话，删掉半个题库也会全绿。
  覆盖数确实需要下调时显式跑 `--update-baseline`，让它出现在 diff 里。
- `run` 先过规则门禁（`rulegate`：安全 / 事实 / 入参），失败轨迹**不进**语义裁判；通过的再交
  correctness / relevance / instruction / tool_select / tool_param / loop 六类 Grader，量纲先
  归一化再加权（权重见 `eval/openjudge/plan.yaml`），因此不做「1 分制与 5 分制直接相加」。
- 单条用例语义门槛 `PASS_JUDGE=0.60`：同源裁判对「回答要点之外补充正确信息」偏严，0.6 是经验门槛。
- `compare` 在 `suite_sha` / `plan_sha` / `rubric_sha` / `model` 任一变化时给 `not_comparable`，
  而不是判退步 —— 换了实验就不该用旧基线卡。
- `{{OUTSIDE}}` 在采集时替换为工作区外的临时绝对路径，避免模型把 `../outside` 解析到别处。

### 在线跑评估

质量页「运行与批次」页签可以直接发起一次评测并轮询进度（`POST /api/openjudge/runs`）：

- **默认关闭**，需显式设 `DEVOPS_AGENT_OPENJUDGE_ONLINE=1`；否则 `POST /runs` 返回 400。
- **单赛道 + `limit ≤ 30`**（`service.MAX_LIMIT`）：全量 90 例要几分钟到几十分钟，不适合点一下就等；
  真要全量用 CLI 或 nightly。默认 `limit=10`、`judge=False`（裁判可选，省额度）。
- **单飞**：`oj_runs` 上有部分唯一索引 `idx_oj_runs_active`，任何时刻只允许一个未完成任务。
  用 DB 而不是内存标志位，是因为 uvicorn 多 worker 时每个进程各有自己的内存。
- 任务挂在进程内 `asyncio.create_task` 上，不引入 Celery/Redis（单机单用户，ADR-0001）。重启会丢
  任务，由 `reap_stale_runs` 把残留标成 `failed(error='interrupted')`，让用户重跑 —— 不做假续跑。
- 取消在**两条用例之间**生效，正在跑的那条不打断。

### 在线配置模型与改用例（「用例与模型」页签）

模型和题库都不写死在代码里，页面即可改（`settings.py` / `casestore.py` / `api_edit.py`）。

**模型**：被测模型与裁判模型各一个输入框，落到独立的单行表 `oj_settings`，**不复用**
`settings['model_config']`（那份是日常对话的配置；评测跑几十条用例会把额度打满，把日常配置改过去
再改回来是双倍风险）。裁判模型此前只能靠环境变量，换一次要重启，且与被测模型同源会系统性放水。

取值优先级：**显式参数 > `oj_settings` > 环境变量 / `/api/model-config`**。空字符串表示「不覆盖」
而不是「用空模型」，所以 `GET /settings` 会把「配的值」和「实际生效的值」分开下发
（`values` / `effective`）。被测模型只覆盖 model 名，温度与窗口仍走正常五层解析 —— 评测不该顺手改参数，
否则批次之间悄悄不可比。

**用例**：在线编辑**不写** `eval/openjudge/cases/*.jsonl`。那是覆盖率基线的口径，改它等于让
`validate --strict` 的棘轮跟着动。落点是覆盖层：

| 路径 | 作用 |
|---|---|
| `eval/openjudge/cases/*.jsonl` | 仓库题库，只读基线（`validate` 与 CI 覆盖率只看这里） |
| `workspace/openjudge/cases/overlay.jsonl` | 同 `(track, id)` 覆盖仓库条目；id 不在仓库里就是新增 |
| `workspace/openjudge/cases/deleted.json` | 隐藏清单，只记录被隐藏的仓库用例 id |

读取顺序是「仓库 → 覆盖 → 去掉隐藏」，因此页面上能改、能新增、能隐藏、能恢复，而 `git status`
始终干净。覆盖层目录可用 `DEVOPS_AGENT_OPENJUDGE_OVERLAY` 改（测试靠它隔离）。

- **写入前一定先能跑**：每条用例先过 `benchmark.parse_case()` + 单条静态校验（结构、赛道、
  fixture 是否存在），不合法返回 422 且**磁盘零写入** —— 否则题库里会躺一条到采集阶段才炸的用例。
- **原子写**：临时文件 + `os.replace`，避免半截 JSONL 让整个题库读不出来。
- **CLI 与页面同源**：`cli._load()` 无 `--cases` 时也读覆盖层，否则「页面上改的题」和「命令行跑的题」
  是两份。`validate` 反过来只读仓库，所以本地删题不会让 CI 假红。

### 静态契约 lint（P1–P4，不跑模型）

```bash
python -m devops_agent.prompt_lint [--json] [--known-issues eval/lint-baseline.json]
```

| 编号 | 检查 |
|---|---|
| P1 | 提示词里的工具名在档位工具集中闭合（两边都要提到，否则模型不知道它存在） |
| P2 | 渲染后不残留占位符、不含机器相关绝对路径 |
| P3 | 文案承诺 = 能力矩阵（说能写就必须真给写工具） |
| P4 | 两条用例的首轮输入不得完全相同（否则通过率被虚增） |

`--known-issues` 列出已接受存量违规；规则对新增内容立即生效。清单项**修好后未删除**会以
`[error] baseline` 退出码 1 —— 豁免不清理就会长成永久豁免。

---

## 质量门禁（CI）

`config/gates.yaml` 是阈值的唯一来源，`scripts/check_gates.py` 读取并实际执行：

| 阈值 | 值 | 执行方式 |
|---|---|---|
| `coverage_fail_under` | 80 | 注入 `pytest --cov-fail-under` 并解析 `coverage.xml` |
| `dead_code_min_confidence` | 80 | 注入 `vulture --min-confidence` |
| `complexity_threshold` | 10 | 解析 `radon cc -j` 最大圈复杂度 |
| `mutation_score` | 70 | `mutmut`，`--with-mutation` 启用（默认跳过） |
| `frontend_gates.coverage_fail_under` | 65 | Vitest coverage-v8 |
| `frontend_gates.complexity_threshold` | 10 | ESLint（只对 `.ts` 逻辑文件；JSX 条件渲染不计） |

`.github/workflows/ci.yml` 三个部分：

1. **quality**：行尾检查（全仓库 LF）→ `ruff check` → `mypy src` → `check_gates.py` → `check_file_size.py` → `openjudge validate --strict` → `prompt_lint --known-issues`。
2. **frontend**：`npm run lint` / `typecheck` / `test:coverage` / `build`。
3. 合并门槛：`quality` 必须绿（`required_approving_review_count=0`）。

`.github/workflows/openjudge-nightly.yml` 每天 03:30 UTC 跑真实评测（`timeout-minutes: 90`），上传 `summary.json` / `report.md` / `traces.jsonl`。

本地提交前自检（worktree 内需用 `PYTHONPATH=src` 前缀，否则 `pip install -e` 的 `.pth` 会让测试跑主仓库源码）：

```bash
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m ruff check .
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m ruff format --check .
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m mypy src
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m vulture src --min-confidence 80
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m pytest --cov=devops_agent --cov-report=term --cov-fail-under=80
```

---

## 三个能力域（规划）

| 域 | 规划 Agent | 核心能力 |
|---|---|---|
| 可持续交付 | delivery | 环境生命周期、配方固化、流水线编排、门禁、漂移检测 |
| 项目排期 | schedule | 需求拆解、依赖排期、资源/人力、风险预警、进度同步 |
| Bug 测试定位 | debug | 测试生成、失败定位（证据 `file:line`）、修复建议 |

共用装配入口 [build_agent(name, system_prompt, model, toolkit, middlewares, max_iters, max_retries, context_config, state)](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/src/devops_agent/runtime/assembly.py)。
但**各域的 system_prompt 与真实业务工具函数尚未实现**：目前每次对话用的都是一个通用 Agent，
`TeamPipeline` / `GoalPipeline` 只被单测调用，没有接到 `gateway/app.py` 的请求链路上。

## Roadmap（尚未实现）

1. **首个能力域闭环**：选 `debug` 域，写真实 `FunctionTool` + system_prompt + 评测用例，跑通「提 bug → 定位 → 打分」。
2. **多 Agent 编排接线**：把 `TeamPipeline` / `GoalPipeline` 接入网关，实现跨 Agent 结果共享（当前 `memory/ledger.py` 只有 Mem0 工厂）。
3. **容器执行面**：`build_docker_workspace()` 已就绪但未接入运行时，生产部署应改为 Docker 沙箱 + 只读挂载。
4. **评测补齐**：五赛道里有 C 赛道覆盖写入能力，但只有 14 例；仍缺重复运行/随机种子（flaky 检测）、成本核算、git SHA 溯源。
5. **技能沉淀**：`skills/`、`config/agents/` 的能力配置化。

> 已完成：SSE 网关、会话隔离与快照重建、5 档权限、确认挂起/续跑、输入管线与密钥保险箱、五层模型参数、端到端可观测性与前端、题库 + 判卷 + 裁判 + 基线对比 + 静态 lint 的评测闭环。

---

## 仓库约定

- **一切改动走 worktree 分支 + PR**：`main` 受分支保护，禁止直推、禁止强推、禁止 `--no-verify`。流程细则见 [AGENTS.md](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/AGENTS.md)。
- 运行态写 `workspace/`（已 gitignore），源码改动才进 git。
- 全仓库 LF（`.gitattributes`），CI 有专门的行尾门禁。
- 设计文档：[docs/adr](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/docs/adr)、[docs/specs](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/docs/specs)；分层讲解见 [docs/wiki](file:///e:/code/devops-agent/workspace/worktrees/readme-wiki-refresh/docs/wiki/Home.md)。
