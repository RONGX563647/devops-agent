# SPEC-0001：模型参数三层可配（部署默认 / 会话覆盖 / 只读凭据）

- 状态：Accepted（已按本 spec 一次性实现，实施记录见 §13）
- 日期：2026-11-24
- 相关：`config/model.yaml`（新增）、`config/policy.yaml`、`src/devops_agent/config.py`、
  `src/devops_agent/model_settings.py`（新增）、`src/devops_agent/model_api.py`（新增）、
  `src/devops_agent/runtime/assembly.py`、`src/devops_agent/runtime/sessions.py`、
  `src/devops_agent/observability/repository.py`、`src/devops_agent/main.py`、
  ADR-0002（档位切换与「变了就重建」的判据先例）、ADR-0003（本 spec 的决策记录）、
  SPEC 前置分析（参数普查）

## 1. 背景

### 1.1 现状：模型参数散在四个地方，而且大部分根本没人设

仓库已有 `policy.yaml` / `health.yaml` / `gates.yaml` 三层配置，唯独模型参数没有落进 yaml。
逐层核对结果：

| 层 | 位置 | 内容 |
|---|---|---|
| 环境变量 | `.env` | `DEVOPS_AGENT_MODEL=qwen3.8-flash`、`BASE_URL`、`API_KEY`、`JUDGE_MODEL`（未设）、`ENABLE_SEARCH`（未设） |
| yaml | `config/policy.yaml` | `react.max_iters.default=12`、`delivery=16`、`model.max_retries=3` |
| 代码常量 | `runtime/chat_model.py:126`、`compression.py:37`、`supervisor.py:33`、`eval/judge.py:237` | `stream=True`；`_DEFAULT_TRIGGER_RATIO=0.8`；`_DEFAULT_CONTEXT_SIZE=128000`；`build_goal_pipeline(max_iters=10, max_retries=3)`；judge `temperature=0` |
| 「没人设」 | `build_chat_model()` 未传 `parameters` / `context_size` | `temperature` / `max_tokens` / `top_p` / `reasoning_effort` **全部走供应商默认**；`context_size` **走框架默认 128000** |

### 1.2 三个已确认的缺陷（这是本 spec 的理由，不是"参数不好看"）

**缺陷一：`context_size=128000` 是猜的，且猜错就出事。**

它同时决定了三件事：压缩阈值（`0.8 × 128000 = 102400`）、前端圆环的分母、压缩后保留多少。
如果 `qwen3.8-flash` 的真实窗口是 1M，等于把 87% 的窗口权重浪费掉；如果真实窗口更小，
请求会在压缩触发前就 400。而这个值不是从任何地方读出来的 —— 它只是框架的默认值。

**缺陷二：两级重试叠乘，最坏 9 次请求只显示 1 次调用。**

`OpenAIChatModel(max_retries=3)`（框架默认，项目没传）× `ModelConfig(max_retries=3)`
（`policy.yaml` 传的）= `(1+3) × (1+3) = 16` 次尝试上限（含首次）。成本和延迟都是 16 倍，
而 span 里只有一条 `model` 记录。这个乘法关系目前没有任何地方体现。

**缺陷三：参数不进 `runs` 表，一旦可配就不可归因。**

`runs` 只有 `model` 一列存名字，没有本轮实际使用的 `temperature` / `max_tokens` / `max_iters`。
健康分 v2 已经改成「按当前口径重算」，如果参数可以随时改而运行记录里没有快照，
**参数变化点前后的分数与评测批次会混在一起算**，这正是 `prompt_factors()` 当初要解决的问题，
参数必须同样处理。

### 1.3 需求

部署默认（模型名、上下文窗口、重试次数）与会话级（温度、输出上限、迭代次数、压缩触发比例）
**全部要能在前端改**，不再是只能改配置文件。

## 2. 目标与非目标

### 目标

1. 模型参数集中到 `config/model.yaml`，代码里不再有第二份常量。
2. 三层覆盖：**会话覆盖 > 部署默认（持久化）> yaml > 环境变量 > 框架默认**。
3. 每一项都能查到「当前值 + 来源」，前端显示来源徽标。
4. 修改后多数参数**热生效**（下一次模型调用 / 下一轮），不需要重启服务。
5. 改动写进 run 记录，评测与健康分可以按参数切片。
6. 所有越界与联动约束在服务端校验，前端只是提示。

### 非目标（明确不做，避免误以为漏了）

| 不做 | 理由 |
|---|---|
| `api_key` / `base_url` 前端可改 | 凭据不该经 HTTP 往返；写入后还会落到明文 DB。要改仍走 `.env` + 重启。若将来要做，应另开「凭据管理」并接密钥库 |
| 逐请求（每轮对话单独）覆盖 | 会话级已经够用，逐轮会让人没法比较两轮结果 |
| 多租户隔离 / 按用户配置 | 当前是单用户本地部署，没有身份体系可挂 |
| 参数自动寻优 / A/B | 那属于评测批次侧，不在配置范畴 |
| `judge_model` 与评测预算 | 属于评测侧配置，另开一份 spec；本次只保证它不被误改 |
| `stream=False` | 关掉流式会让前端从逐字渲染退化成一次性出现（实测 SET→DELTA 差 0.07ms），只读 |

## 3. 参数清单与分层

`E` = 编辑层（会话可改、部署默认可改）、`D` = 仅部署默认、`R` = 只读展示。

| # | 参数 | 层 | 类型 / 范围 | 默认来源 | 生效点 |
|---|---|---|---|---|---|
| 1 | `model` | E | `str`，1–128 字符 | env `DEVOPS_AGENT_MODEL` | 重建模型对象 |
| 2 | `context_size` | E | `int`，8192–2 000 000 | `model.yaml` 128000（标「未校准」） | 替换 `context_config` |
| 3 | `model_max_retries` | E | `int`，0–5 | `model.yaml` 3（原框架默认） | 重建模型对象 |
| 4 | `agent_max_retries` | E | `int`，0–5 | `model.yaml` 3（原 `policy.yaml model.max_retries`） | 替换 `model_config` |
| 5 | `max_iters` | E | `int`，1–200 | `model.yaml` 12 / delivery 16（原 `policy.yaml react.max_iters`） | 替换 `react_config` |
| 6 | `temperature` | E | `float \| null`，0–2 | `null`（供应商默认） | 重建模型对象 |
| 7 | `max_tokens` | E | `int \| null`，1–`context_size` | `null`（不限制） | 重建模型对象 |
| 8 | `top_p` | E | `float \| null`，(0, 1] | `null` | 重建模型对象 |
| 9 | `reasoning_effort` | E | `enum \| null`：`none/minimal/low/medium/high/xhigh` | `null` | 重建模型对象 |
| 10 | `thinking_enable` | E | `bool \| null` | `null` | 重建模型对象 |
| 11 | `parallel_tool_calls` | E | `bool` | `true` | 重建模型对象 |
| 12 | `trigger_ratio` | E | `float`，(0, 0.9] | 框架默认 0.8 | 替换 `context_config` |
| 13 | `reserve_ratio` | E | `float`，(0, 0.9)，必须 `< trigger_ratio` | 框架默认 0.1 | 替换 `context_config` |
| 14 | `context_buffer_ratio` | E | `float`，[0, 1] | 框架默认 0.2 | 替换 `context_config` |
| 15 | `api_key` / `base_url` | R | — | env | 重启 |
| 16 | `stream` | R | `true` 固定 | 代码 | 重启 |
| 17 | `judge_model` | R | — | env `DEVOPS_AGENT_JUDGE_MODEL` → 回落主模型 | 重启 |

说明：

- **第 3、4 项必须拆开**。合成一个 `max_retries` 就是把「哪个重试」这件事藏起来，
  而它们的乘数效应正是缺陷二。UI 必须显示 `(1+model) × (1+agent)` 的**最坏尝试次数**。
- **第 15–17 项出现在面板里但禁用**，并给出「在哪改」的指引。完全隐藏会让人以为系统没有这个参数。
- `max_iters` 与域相关（`model.yaml` 的 `max_iters.delivery=16`）。会话级编辑只覆盖**当前会话所属域**的值；
  面板上注明「本会话（domain=delivery）」。

## 4. 优先级与来源解析

### 4.1 优先级链

```
会话覆盖（session_states.model_config）
  > 部署默认（settings['model_config']，前端改、持久化）
  > config/model.yaml
  > 环境变量（仅 model / judge_model / enable_search）
  > 框架默认（context_size / retries / ContextConfig）
```

### 4.2 解析器

新增 `src/devops_agent/model_settings.py`（单一装配点，与 `config.py` 同级职责）：

```python
class Source(str, Enum):
    SESSION = "session"
    STORED = "stored"
    YAML = "yaml"
    ENV = "env"
    FRAMEWORK = "framework-default"

@dataclass(frozen=True)
class Resolved:
    value: Any
    source: Source

def resolve(session_overrides: dict | None = None) -> dict[str, Resolved]:
    """逐项解析出「值 + 来源」；None 表示该项用供应商/框架默认。"""
```

`resolve()` 是纯函数，输入三份 dict，输出逐项来源。这是全 spec 的核心：
**任何地方需要参数，只能走 `resolve()`，不能自己读 yaml 或 env**。

### 4.3 为什么要 `source`

没有来源，用户看不出「这个 128000 是我设的，还是框架塞的」。
缺陷一之所以隐藏了这么久，就是因为它没有来源标注。前端徽标与 `GET` 响应里的 `source` 字段是同一份数据。

## 5. 数据模型

### 5.1 新增表 `settings`（KV，单行存 JSON）

```sql
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,              -- JSON 文本
    version    INTEGER NOT NULL DEFAULT 1, -- 乐观并发
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
```

用 KV 而不是新建 `model_settings` 表：以后 `health.yaml` 的权重、`gates.yaml` 的阈值
若要下放到前端，复用同一张表，不必每次加表。

`updated_by` 当前没有身份体系，写入固定 `"local"`；保留列是为了将来接认证时不用改 schema。

### 5.2 `session_states` 加列

```python
("model_config", "TEXT NOT NULL DEFAULT ''")   # JSON；空串 = 无覆盖
```

加列位置：`repository._SESSION_COLUMNS`（新增常量），由 `_migrate()` 统一走
`_ensure_columns()`，和 `_RUN_COLUMNS` 同一套幂等迁移。

**必须持久化**：`SessionRegistry` 是 LRU（默认 16），会话被回收后要从 DB 重建，
会话级覆盖如果只在内存里，一次淘汰就丢了。

### 5.3 `runs` 加列

```python
("model_params", "TEXT NOT NULL DEFAULT ''")   # JSON 快照
```

只存**实际下发**的非默认值（`{"temperature": 0.3, "model": "qwen3.8-flash"}`），
不存全部默认值 —— 否则每行都塞一份 12 键的 JSON，且默认值变了会误判为「参数变了」。

同时 `runs.model` 继续写模型名（已有列），`model_params` 里的 `model` 作为冗余校验。

### 5.4 评测可比性

`prompt_factors()` 增加一项 `params_sha`：对 `model_params` 的稳定 JSON 做 sha256。
评测批次对比（`/api/eval/compare`）把它作为**不可比因子**，与 `prompt_sha` 同级处理：
参数变了就该标「不可直接比较」，而不是归因为噪声。

## 6. 生效语义

### 6.1 热替换矩阵

| 参数 | 生效机制 | 用户可见延迟 |
|---|---|---|
| `temperature` `max_tokens` `top_p` `reasoning_effort` `thinking_enable` `parallel_tool_calls` `model` `model_max_retries` | `self._agent.model = build_chat_model(params)` | 下一次模型调用 |
| `context_size` `trigger_ratio` `reserve_ratio` `context_buffer_ratio` | `self._agent.context_config = ContextConfig(...)` | 下一轮（压缩检查在每轮推理前） |
| `max_iters` | `self._agent.react_config.max_iters = n` | 下一次 reply |
| `agent_max_retries` | `self._agent.model_config.max_retries = n` | 下一次 reply |

依据：`apply_profile()` 已经用同一手法热替换过模型对象（`scope_runtime.py:87`），说明
「重建模型对象 + 赋值」这条路是通的，不需要销毁 `Agent`、不需要丢对话历史。

**替换模型时必须同步 `context_size`**：`build_chat_model` 要把 `context_size` 显式传给
`OpenAIChatModel(context_size=...)`，否则换了模型但压缩阈值还是旧值，
表现为「窗口明明变大，却还是 10 万 token 就压缩」。

### 6.2 部署默认的传播：applied / deferred

改部署默认时，活跃会话要不要跟着变？两个极端都不对：

- 只影响新会话 → 用户改完以为生效了，实际还在跑旧参数；
- 立刻广播到所有会话 → 可能打断正在跑的 run。

**规则**：写入 DB 后遍历 registry，逐会话判断：

```
if 会话没有正在跑的 run（lock 未持有）  → 立即热替换，计入 applied
else                                    → 标记 pending，run 结束后应用，计入 deferred
```

`AgentScopeRuntime.stream()` 的 `finally` 里检查 pending 并应用。
响应返回 `{"applied": 3, "deferred": 1}`，前端提示「3 个会话已更新，1 个等当前轮结束」。

### 6.3 不可热替换的情况

Echo / 测试替身（没有 `apply_model_params` 方法）、以及会话尚未创建时：
返回 `applied=false`，`effective_from="下一轮"`。**不能谎称立即生效**——
这是 `switch_session_permission` 已经建立的约定（`applied` 字段）。

## 7. API

所有端点挂在现有 `main.py`，风格对齐 `/api/permissions`。

### 7.1 `GET /api/model-config`

```json
{
  "effective": {
    "model": "qwen3.8-flash",
    "context_size": 128000,
    "temperature": null,
    "max_tokens": null,
    "max_iters": 12,
    "model_max_retries": 3,
    "agent_max_retries": 3,
    "trigger_ratio": 0.8,
    "reserve_ratio": 0.1,
    "context_buffer_ratio": 0.2,
    "top_p": null,
    "reasoning_effort": null,
    "thinking_enable": null,
    "parallel_tool_calls": true
  },
  "sources": {
    "model": "env",
    "context_size": "framework-default",
    "max_iters": "yaml",
    "temperature": "framework-default"
  },
  "bounds": {
    "temperature": {"min": 0, "max": 2, "step": 0.1},
    "context_size": {"min": 8192, "max": 2000000, "step": 1024},
    "max_iters": {"min": 1, "max": 200, "step": 1}
  },
  "editable": ["model", "context_size", "..."],
  "readonly": {
    "api_key": "环境变量 DEVOPS_AGENT_API_KEY（重启生效）",
    "stream": "固定 true：关闭会让前端退化为一次性渲染"
  },
  "derived": {
    "compression_threshold": 102400,
    "worst_case_attempts": 16,
    "context_uncalibrated": true
  },
  "version": 3
}
```

`derived` 是给前端直接用的**推算值**，不要让它自己乘：
`compression_threshold = trigger_ratio × context_size`、
`worst_case_attempts = (1 + model_max_retries) × (1 + agent_max_retries)`。

`context_uncalibrated=true` 表示 `context_size` 来自框架默认、没有经过部署者确认，
前端必须显示警告徽标 —— 这是缺陷一的可见化。

### 7.2 `PATCH /api/model-config`（部署默认）

```json
// 请求
{"patch": {"temperature": 0.3, "max_iters": 20}, "version": 3}

// 响应 200
{"effective": {...}, "sources": {...}, "version": 4,
 "affected": {"applied": 3, "deferred": 1}}
```

- `patch` 里值为 `null` 表示**重置该项为下层默认**（不是"设为 null 值"，语义要写死在文档和 UI 上）；
- `version` 不匹配返回 `409 config_version_conflict`，响应带当前 `version` 与 `effective`，
  前端让用户选择「覆盖」还是「放弃」。

### 7.3 `POST /api/model-config/reset`

```json
{"keys": ["temperature", "max_iters"]}   // 或 {"all": true}
```

清除 `settings` 里的覆盖项，回落到 yaml / env / 框架默认。返回同 7.2。

### 7.4 `GET|PATCH|DELETE /api/session/{session_id}/model-config`

```json
// PATCH 请求
{"overrides": {"temperature": 0.2, "max_iters": 30}}
// PATCH 响应
{"effective": {...}, "sources": {...},
 "applied": true, "effective_from": "下一次模型调用"}

// DELETE 响应
{"effective": {...}, "sources": {...}, "cleared": true}
```

- `PATCH` 的 `overrides` 是**合并**语义，不是替换；要删除单项用 `{"temperature": null}`；
- `DELETE` 清空该会话所有覆盖；
- `session_id` 与路径不一致返回 `400 session_mismatch`（对齐切档端点）；
- 会话尚未创建也能写入：覆盖存 DB，首次 `acquire` 时装配。

### 7.5 `POST /api/model-config/probe`（可选，前端「测试连接」按钮）

用当前或待保存的参数发一次最小请求（`max_tokens=1`，`messages=[{"content":"ok"}]`），
返回 `{"ok": true, "latency_ms": 320}` 或 `{"ok": false, "error": "..."}`。

**保存不探测**：否则外部依赖抖动会让配置存不进去。探测是显式动作，会产生一次计费调用，
按钮上要写明。

### 7.6 错误码

| 码 | detail | 场景 |
|---|---|---|
| 400 | `invalid_model_config` + `{field, reason, bounds}` | 越界 / 联动冲突 |
| 400 | `session_mismatch` | 路径与 body 的 session_id 不一致 |
| 409 | `config_version_conflict` | 乐观并发失败 |
| 404 | `session_not_found` | 会话不存在且无法创建覆盖（当前不触发，保留） |

## 8. 校验与联动

服务端校验写在 `ModelConfigPatch` 的 pydantic validator 里，越界由 `Field(ge=, le=)` 兜住，
联动由 `model_validator` 兜住。前端做同样提示，但**服务端是唯一裁判**。

| 规则 | 理由 |
|---|---|
| `trigger_ratio ≤ 0.9` | 框架 `ContextConfig` 的硬约束（要留空间给压缩本身） |
| `reserve_ratio < trigger_ratio` | 框架 `_validate_context_config_ratios` 会抛异常 |
| `max_tokens ≤ context_size` | 输出上限超过窗口等于必截断 |
| `context_size ≥ 8192` | 系统提示 + 工具 schema 本身就占几千 token |
| `thinking_enable=false` 且 `reasoning_effort≠none` | 警告不报错：框架的行为是 `false` 覆盖成 `none`，不是冲突 |
| `worst_case_attempts > 6` | 只警告：这是成本提示，不是非法值 |
| `max_iters > 50` | 只警告：一次回复可能跑很久且烧大量 token |

`max_iters` 上限给 200 而不是 12：本地试验里就出现过把 `max_iters` 改成 120 的需求，
说明确实有长任务场景。上限卡死会逼人去改代码，那比让他在界面上改更糟。

## 9. 前端

### 9.1 入口与形态

输入栏权限选择器（`PermissionPicker`）旁新增齿轮按钮 → **Popover**（不是独立路由）。

理由：13 个参数放进弹窗已经偏多，但如果做成全屏设置页，用户在调完参数后要跳回来才能
继续对话，改一次参数的成本太高。Popover 贴着输入框，改完直接发消息验证。

### 9.2 结构

```
┌ 模型设置 ────────────────────────── [本会话 ⚙ 2 项] ┐
│ 模型        qwen3.8-flash        ▾   env            │
│ 上下文窗口  128,000  ⚠ 未校准        framework-default│
│                                                     │
│ ── 采样 ─────────────────────────────────────────── │
│ 温度        ●──────── 0.3            yaml           │
│ top_p       1.0                      framework      │
│ 最大输出    未限制                    framework      │
│ 推理强度    默认                      framework      │
│                                                     │
│ ── 迭代与重试 ───────────────────────────────────── │
│ 迭代上限    12                       yaml           │
│ 模型重试    3  ×  智能体重试 3  = 最坏 16 次  ⚠      │
│                                                     │
│ ── 上下文压缩 ───────────────────────────────────── │
│ 触发比例    80%  →  102,400 tokens                  │
│ 保留比例    10%                                     │
│ 预警缓冲    20%                                     │
│                                                     │
│ ── 只读 ─────────────────────────────────────────── │
│ api_key     ••••  环境变量（重启生效）               │
│ 流式输出    固定开启                                 │
│                                                     │
│ [恢复默认]              [保存到本会话]  [设为部署默认]│
└─────────────────────────────────────────────────────┘
```

### 9.3 三个必须说清的点

**一、两个保存按钮的区别。** 「保存到本会话」只改自己；「设为部署默认」影响所有会话，
要二次确认，确认框里写清「将影响 N 个活跃会话，其中 M 个等当前轮结束」。

**二、来源徽标。** 每行右侧显示 `session` / `stored` / `yaml` / `env` / `framework-default`，
配色区分「可改」与「只读」。`framework-default` 加一个「未配置」的淡色，暗示应该确认一下。

**三、推算值不让前端算。** `derived` 里的 `compression_threshold` / `worst_case_attempts`
直接用后端给的。前端重复实现口径，迟早和后端不一致 —— 健康分 v1 就是这么埋下坑的。

### 9.4 危险项确认

| 操作 | 确认文案 |
|---|---|
| 改 `model` | 「下一次模型调用即生效。若模型名无效，该次调用会失败且不会自动回滚到旧模型。」 |
| 改 `context_size` | 「压缩阈值会按当前触发比例重算为 X。当前占用 Y，[会 / 不会] 立即触发一次压缩。」 |
| 改 `max_iters` > 50 | 「一次回复最多可能进行 N 轮工具调用，耗时与 token 消耗相应放大。」 |
| 改重试 | 「最坏情况下一次提问会发出 N 次模型请求（当前 M 次）。」 |

`context_size` 那条要真的算：后端在响应里给 `compression_threshold`，前端按当前
`contextTokens`（圆环已有数据）比较，能准确说出「会 / 不会立即压缩」。

### 9.5 与既有 UI 的衔接

- `chat-meta` 里 `view.modelName` 后面加标记：有会话覆盖时显示 `· 参数已改`，点击打开面板；
- `ContextRing` 的 tooltip 补一行「阈值 X（来自 触发比例 × 上下文窗口）」，
  窗口未校准时加 `⚠`；
- 健康分 `HealthPanel` 不需要改：`token_baseline_per_call` 是评分口径，
  与模型参数解耦（改上下文窗口不应该改变「多省」的判定标准）。

## 10. 并发与多实例

**当前是单进程单 worker**（`uvicorn` 未指定 `--workers`），进程内缓存够用。
但配置写进 DB 就意味着可能有第二个进程读到旧值，所以：

1. `settings` 读取带 **5 秒 TTL** 的进程内缓存；
2. 每次 run 开始时比对 `version`，不一致就重载；
3. 部署默认的 `PATCH` 只对自己进程的 registry 做热替换，其他进程靠 TTL 在下一轮收敛。

`version` 列同时承担「乐观并发」与「跨进程失效信号」两个作用，不加额外的通知机制
（不引入 Redis / pub-sub，这个规模不需要）。

多 worker 时要补一条：`deferred` 的会话可能落在另一个进程，热替换不会发生，
表现为「等这一轮结束」但其实一直不结束。那时需要改成「每轮开始重新解析」，
本 spec 不做 —— 写在未决问题里。

## 11. 迁移与回滚

### 迁移

全部走已有的幂等机制，不引入迁移框架：

| 变更 | 方式 |
|---|---|
| `settings` 表 | `_SCHEMA` 加 `CREATE TABLE IF NOT EXISTS` |
| `session_states.model_config` | `_SESSION_COLUMNS` + `_ensure_columns` |
| `runs.model_params` | `_RUN_COLUMNS` + `_ensure_columns` |
| `config/model.yaml` | 新文件；缺失时 `load_model()` 回落到内置默认（与 `config.py` 现有行为一致） |

**老库升级不需要脚本**：`_ensure_columns` 已按 `PRAGMA table_info` 补列，重复执行安全。
现有 100+ 条 run 的 `model_params` 为空串，读出来按「未记录」处理，
健康分与评测对比里显示为「参数未知」，**不假装是默认值**。

### 回滚

- 代码回滚：新增表/列对旧代码无影响（旧代码不读它们）；
- 配置回滚：删掉 `settings` 里的 `model_config` 行 → 立即回到 yaml / env 值；
- `config/model.yaml` 写错导致启动失败时，`_load_yaml` 返回 `{}` → 全部回落默认，服务仍能起。

## 12. 测试清单

### 后端

| 用例 | 断言 |
|---|---|
| 优先级解析（5 层各一） | session > stored > yaml > env > framework 的覆盖顺序 |
| `resolve()` 来源标注 | 每项 `source` 与实际来源一致 |
| `context_size` 未配置 | `context_uncalibrated=true` |
| 联动校验 | `reserve_ratio ≥ trigger_ratio` → 400 `invalid_model_config` |
| `max_tokens > context_size` | 400 |
| 越界 | temperature=3 → 422（pydantic） |
| 乐观并发 | 旧 version → 409，带回当前 version |
| 热替换 | `PATCH` 后 `agent.model.parameters.temperature` 已变 |
| 替换模型同步窗口 | 换 `context_size` 后 `agent.model.context_size` 与 `context_config` 一致 |
| deferred | 持有 lock 的会话不在 `applied` 里，在 `deferred` 里 |
| run 记录参数 | `runs.model_params` 只含非默认值 |
| 会话覆盖持久化 | 写入后清空 registry，重新 acquire 覆盖仍在 |
| DELETE 清空 | `sources` 全部回到 stored/yaml |
| `params_sha` | 同参数同 sha，改一项变 sha |

### 前端

| 用例 | 断言 |
|---|---|
| 来源徽标 | 三种 source 渲染对应文案 |
| 两个保存按钮调用不同端点 | mock client 断言路径 |
| 部署默认二次确认 | 点保存先出确认框 |
| 推算值直接用后端 | `worst_case_attempts` 不参与前端计算 |
| 越界禁用保存 | temperature 输入 3 时保存按钮 disabled |
| 只读项不可编辑 | `api_key` 行无 input |
| 非默认标记 | 有覆盖时 `chat-meta` 出现「参数已改」 |

## 13. 实施记录

原计划分四个 PR 独立合并，实际**合并为一个 PR 交付**：四段之间存在硬依赖
（解析器没有持久化就只能读 yaml；API 没有热替换就只能返回"下一轮"；面板没有 API 就是死 UI），
拆开会产生三个"半成品中间态"，每个都要单独解释"现在为什么还不生效"。

### 实际落点

| 段 | 文件 | 与原计划的差异 |
|---|---|---|
| 配置层 | `config/model.yaml`、`config.py`（`ModelSettings` + `load_model()`）、`config/policy.yaml`（删 `react:` / `model:` 两段） | `config.py` 里**没有**扩展 `ModelPolicy`，而是新建 `ModelSettings` 并删掉旧的 `ModelPolicy` / `ReactPolicy` —— 两个同名默认值会打架 |
| 解析器 | `model_settings.py`（`resolve()` / `derived()` / `ModelParams` / `params_sha`） | 新增 `stored_defaults()` / `invalidate()`，带 5 秒 TTL 缓存（spec §10 要求，实现时集中在这一处） |
| 持久化 | `repository.py`（`settings` 表 + `session_states.model_config` + `runs.model_params`）、`recorder.py`、`models.py`、`api.py` | `summary_rows()` 的显式 SELECT 也必须补新列，否则 `_run_from_row` 抛 `IndexError` |
| 运行时 | `chat_model.py`（`build_chat_model` / `build_context_config`）、`scope_runtime.py`（`apply_model_params`）、`sessions.py`（`apply_model_params` / `broadcast_model_defaults` / `flush_pending`）、`compression.py`、`supervisor.py` | `apply_profile()` 与 `apply_model_params()` 共用"变了就重建"的判据思路；`registry_for()` 从 `main.py` 移到 `sessions.py` 以断开 `model_api → main` 的循环 import |
| API | `model_api.py`（6 端点 + 400/409 形状） | 未实现 §7.5 的可选 probe 端点（"保存不探测"已满足，探测按钮收益不抵一次计费调用） |
| 评测 | `eval/report.py`（`_FACTOR_FIELDS` 加 `params_sha`，`_classify` 拆两支） | `{prompt_sha, params_sha}` 同时变化时归因为"多个因子"，不合并成一句 |
| 前端 | `ModelConfigPopover.tsx`、`ChatTab.tsx`、`api/client.ts`、`types.ts`、`styles.css` | `ContextRing` 的 tooltip 补阈值来源未做（面板里已有阈值与「未校准」徽标，圆环改动留待下一轮） |

### 门禁结果

后端 `ruff` / `mypy`（56 files）/ `pytest`（632 passed）/ `check_gates.py`：
覆盖率 **91.8%** ≥ 80%、圈复杂度 max **10** ≤ 10、死代码置信度 80 PASS。
前端 `lint` / `typecheck` / `test:coverage`（29 files / 307 tests）/ `build` 全绿。

### 与原计划的偏离说明

- **未做 probe 端点**：spec 里已标为"可选"，且"保存不探测"的原则已由保存路径满足。
- **`ContextRing` tooltip 未改**：避免本轮把圆环（刚合入的 PR）再动一次；面板内已有等价信息。
- **`max_iters` 上限仍是 200**：本地试验里有把 120 用起来的长任务需求，
  上限卡死不解决需求只会逼人改代码。

## 14. 未决问题

1. **`qwen3.8-flash` 的真实上下文窗口是多少？** 这个数决定第一版 `model.yaml` 里
   `context_size` 写什么。在拿到之前，PR-1 保持框架默认 128000，但把
   `context_uncalibrated` 打开，前端显示警告 —— 让它是"显式的猜测"而不是"隐形的错误"。
2. **`model` 名要不要白名单？** 当前没有 provider 的模型列表接口，自由输入的代价是
   写错要等下一次调用才失败。方案是给一个可选 `allowed_models` 配置（默认空 = 不限），
   有值时前端渲染成下拉。要不要做取决于是否希望限制。
3. **多 worker 下的 deferred**（见 §10）本 spec 不解决；单进程假设一旦不成立需要重新设计。
4. **`max_iters` 是否要按域分别暴露**？现在会话只能改自己域的值。若要一次改三域，
   面板需要分组 —— 等有实际需求再说。
