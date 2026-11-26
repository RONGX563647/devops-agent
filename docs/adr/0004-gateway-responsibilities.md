# ADR-0004：网关按「变更原因」拆分 —— 责任边界、契约单点与硬编码三分类

- 状态：Accepted
- 日期：2026-11-26
- 决策者：项目所有者
- 相关：ADR-0001（观测数据模型）、ADR-0002（权限模型）、`config/gateway.yaml`、`config/policy.yaml`、`.importlinter`

## 背景

`src/devops_agent/gateway/` 原本 9 个文件、601 行，按**技术分层**切（stream / confirm / sse / deps）。
实测这个切法导致 6 个单元各自承担 3 个以上「变更原因」：

| 单元 | 变更原因数 | 各自的原因 |
|---|---|---|
| `run_stream()`（47 行） | 5 | 前后端契约 / 并发互斥 / 解析编排 / 收尾 UX / 异常映射 |
| `confirm_run()`（47 行） | 5 | 契约 / 归属过期校验 / 幂等实现 / 能力检查 / 记录器接管 |
| `_acquire_lease()` | 4 | 权限解析 / 注册表装配 / 锁 / 观测参数 |
| `_finish_or_park()` | 3 | 结束决策 / 持久化 / 帧渲染 |
| `_park()` / `_prepare_payload()` | 各 3 | 序列化 / 持久化 / 渲染；解析 / 帧 / 落库 |
| `app.py` 模块体 | 4 | env 加载 / 运行时选择 / 启动告警 / 挂载顺序 |

同时存在可度量的重复与漂移源：

- `": ping\n\n"` 三份、`text/event-stream` 两份、`_SSE_HEADERS` 被 `confirm.py` 以下划线名 import；
- 10 处 `raise HTTPException(status_code=..., detail="...")`，8 个裸字符串错误码，
  `session_busy` 与 `session_mismatch` 各重复两次；
- 「Echo 有哪些可选能力」靠 9 处 `getattr(..., None) + callable()` 的隐式约定；
- 主链路有断连兜底收尾，确认续跑没有 —— 客户端在续跑途中断开时，run 会停在
  「已解挂、`ended_at` 为空」的悬空态；
- `runtime/confirm.py` 359 行里挤着三件变更节奏完全不同的事（风险分级 / 规则沉淀 / shell 词法），
  夹带 20 个模块常量与 5 个程序分类 frozenset。

## 决策

### 1. 按「变更原因」重组模块

```text
gateway/
├── app.py          装配根 create_app()：注入 + 挂载 + 启动告警
├── settings.py     部署策略（env 名 + config/gateway.yaml → 强类型）
├── errors.py       ErrorCode 枚举 + STATUS_OF 映射（叶子）
├── contract.py     帧与挂起载荷（叶子）
├── sse.py          保活：队列 + 心跳泵
├── schemas.py      HTTP 请求体
├── deps.py         FastAPI 依赖取用
├── lease.py        会话租约：claim_entry / hold / open_lease / close
├── run.py          一轮编排：run_turn / resume_turn / finish_or_park
├── ending.py       收尾决策纯函数 + RunLimits
├── ports.py        RuntimePort（Null Object）+ PendingStore
├── input_prep.py   输入准备（失败即退化）
├── stream.py       POST /run/stream —— 只做契约映射
├── confirm.py      GET pending / POST confirm —— 只做契约映射
├── sessions.py     档位列表 / 切档
└── health.py       GET /health
```

路由模块的走样判据：**出现 `if` 分支或 `try` 就说明逻辑放错了层**。

### 2. 契约与错误码是叶子，并由 CI 强制

`.importlinter` 新增两条契约：`gateway-contract-is-leaf`（`contract` / `errors` 不得依赖其他
gateway 模块）、`gateway-no-framework`（gateway 不得直接 import agentscope）。
「网关不 import 框架」此前只是 Architecture.md 的一句口号，现在由 CI 可执行地兜住。

### 3. 硬编码按四类分流（**不可配置的也要单点化**）

| 类 | 判据 | 归宿 | 例 |
|---|---|---|---|
| A 部署策略 | 随环境变，运维/产品想调 | `config/gateway.yaml` + pydantic，装配期注入 | `ask_limit` / `park_ttl_seconds` / `settle_rounds` / `heartbeat_seconds` / `max_entries` |
| B 协议契约 | 一变就要同步改前端/DB | `contract.py` / `errors.py` 的枚举与常量 | 帧名、`RUN_DONE` 字段、媒体类型、错误码 |
| C 域知识 | 安全语义，可调但调错危险 | `policy.yaml` 的 `confirm:` 段 + 加载期校验正则 | 高危/写入正则、作用域、程序分类、`display_limit` |
| D 状态枚举 | 同时出现在代码与 DDL | `observability.models.RunStatus`，DDL 由它生成 | `running/success/failed` |
| E 展示文案 | 人读的中文句子 | 后端出 `code`，前端出文案（过渡期 `code + message` 双轨） | 运行时自述、切档生效范围 |

**永远不进配置**：HTTP 状态码、帧类型与字段、DB 状态值。它们写进 yaml 只会让
「代码 / 前端 / 数据库」三处各存一份默认值然后漂移。

### 4. 收尾决策与执行分离

`ending.decide_ending` 是纯函数（不读时钟、不写库、不拼帧），`run.finish_or_park` 只按
`Ending` 分派副作用。顺带把一处隐藏策略显式化：旧代码在「确认计数不可得」时返回
`ASK_LIMIT + 1`，把「库不可用」硬编码成「按超限处理」；现在是配置项
`on_counter_unavailable: reject|park`，默认 `reject` 保持历史行为。

### 5. 可选能力用 Null Object 收敛

`RuntimePort` 在构造时一次性探测 `resume / pending_calls / snapshot / model_params_record /
web_search`，缺能力给 Null 行为（空列表 / `None` / 空 dict），`resume` 抛
`RuntimeCapabilityError` 而不是静默空流。唯一保留的布尔是 `resumable`，它决定错误话术。

## 影响

**收益（可度量）**

| 指标 | 改造前 | 改造后 |
|---|---|---|
| 裸错误码字符串 | 10 处 | 0（`ErrorCode` 枚举 + 表驱动测试） |
| 能力探测点 | 9 处 | 1 处（`RuntimePort`） |
| 同字面量重复（ping / 媒体类型 / 错误码） | 3 / 2 / 2 | 1 / 1 / 1 |
| 承担 ≥3 变更原因的函数 | 6 个 | 0 |
| 续跑断连的终态 | 悬空 | `failed` + `ended_at`（新增回归用例） |
| `runtime/confirm.py` | 359 行 / 3 责任 | 门面 62 行，实现在 `risk`/`rules`/`shell` |

**代价**

- 网关行数 601 → 约 1100（含配置模型、错误码表、端口 Null 实现、逐段注释）。变多的是显式化，
  不是重复；单文件最大 133 行。
- 两处对外契约有语义调整：
  1. 切档端点「路径与 body 不一致」的 detail 从 `session_mismatch` 改为 `request_mismatch`
     （400）。前端不按该字段分支（已核对 `web/src`），语义上「请求自相矛盾」与「越权」应当分开。
  2. `/health.runtime` 与切档响应新增 `code` 字段；原文案字段保留，前端可逐步迁移。
- 测试需要 `PYTHONPATH=src` 才能加载 worktree 源码（既有陷阱，见 AGENTS.md 注 2）。

## 不做什么

- 不引入 DI 容器：`deps.py` + `Depends` 已是依赖层，且注入点只有 3 个。
- 不做完整状态机：3 个状态 + 独立 `parked` 列，`Ending` 枚举 + `match` 够用。
- 不把 gateway 改成 ports&adapters 全量重写：`.importlinter` 已锁死方向，全量重写只会把 601 行推到 2000+。
- 不做 TS 类型代码生成：`contract.py` 的字段名 + 帧快照测试足够，生成器是负债。

## 验证

```bash
PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q --cov=devops_agent --cov-fail-under=80
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m vulture src --min-confidence 80
python scripts/check_file_size.py
PYTHONPATH=src .venv/Scripts/lint-imports.exe    # 8 contracts kept
```

新增回归用例：`tests/unit/test_gateway_contract.py`（错误码全覆盖、帧字段快照、载荷闭环）、
`test_gateway_ending.py`（决策表 + 计数不可得策略）、`test_gateway_ports.py`（Null 能力、
挂起态幂等）、`test_gateway_settings.py`（yaml 与代码默认值对账）。
