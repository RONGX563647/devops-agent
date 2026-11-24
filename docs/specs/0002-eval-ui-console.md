# SPEC-0002：前端评测控制台（规则可见 / 裁判可解释 / 单例轨迹 / 在线运行）

- 状态：Draft（待评审后按 §13 分期实施）
- 日期：2026-11-24
- 相关：`src/devops_agent/eval/{lint,judge,runner,report,cli}.py`、
  `src/devops_agent/observability/{eval_api,repository}.py`、
  `src/devops_agent/main.py`、`web/src/pages/{Quality,EvalBatches}.tsx`、
  `web/src/pages/quality/{Overview,SuitePanel}.tsx`、`web/src/api/client.ts`、
  `web/src/types.ts`、`.github/workflows/{ci,eval-nightly}.yml`、
  ADR-0001（可观测性前端先例：只读 API + 三视图）

## 1. 背景

### 1.1 现状：能看结论，但看不到结论是怎么来的

`eval_api.py` 的模块 docstring 写得很明确：

> 只读：不提供写入口，评测写入由 CLI 负责。

于是「跑一次评测」与「看评测结果」被切成了两个世界。前端 `EvalBatches.tsx` 顶部只有一行常量：

```ts
const CLI_HINT =
  'python -m devops_agent.eval run --suite eval/suites/devops-core.v2.jsonl --out workspace/evals/<batch>';
```

逐项核对前端能拿到的信息（`_batch_out` / `_case_out` / `_suite_out`）：

| 需求 | 现状 | 缺口 |
|---|---|---|
| 静态规则（S1–S8） | 只有 S8 的单用例三态 `decision` 和 `assertion_summary` 计数 | 规则本身（查什么、为什么错、怎么修）一条都没有；S1–S7 的 `Finding` 完全不暴露 |
| 裁判评判标注 | `scores_json` 只有「维度 → 分数」 | 模型返回的 `evidence` 在 `_parse_scores` 里被丢弃（`judge.py:149-166` 只取 `entry["score"]`），rubric 锚点文案只存在于 Python 常量 |
| 单条用例全流程 | `CaseRun` 只有聚合态：`tool_names` / `executed_tools` / `denied_tools` / `checks` / `trajectory_score` | 事件级时序（哪个事件、什么时候、工具参数、为什么被拒）只存在于 `_consume` 的临时 `state`，跑完即弃；`TraceResult` 也没有事件字段 |
| 在线运行 | 无 | 只能 CLI，且 CLI 一次性跑完才落库（`_persist` 在所有用例跑完后调用），中途刷新页面看不到任何进度 |

### 1.2 这不是「界面不够漂亮」

四个缺口各自对应一个会误判的场景：

1. **规则不可见**：`prompt-lint` 在 CI 里绿/红，但人只看到红，看不到「S7 说这条断言和题面撞车」。修题库的人只能去读 570 行 `lint.py`。
2. **裁判不可解释**：`correctness=3` 没有理由，等于让人相信一个不可复核的数字。而模型其实**已经给了理由**，只是被代码扔了。
3. **轨迹不可复盘**：`denied_tools=["bash"]` 分不清「模型压根没试」和「试了被权限拒绝」之外的第三种情况——被拒的是哪条命令、拒在第几次调用、拒之前读了多少文件，全都没有。
4. **不能在线跑**：改完题库要切终端、记参数、看 stdout。跑一次的成本高到没人愿意为「只跑 5 条」起一次 CLI。

## 2. 目标与非目标

### 目标

1. 前端能看到 **S1–S8 的规则目录**与**当前题库的 findings**，包括 `known-issues` 的 accepted / stale 分档。
2. 前端能看到 **裁判逐维分数 + 模型给出的 evidence + rubric 锚点文案**，不需要回读 Python。
3. 前端能打开 **单条用例的全流程轨迹**：fixture → 事件时序 → 工具三档结局 → 断言逐条判定 → 轨迹分 → 裁判 → 最终 passed。
4. 前端能 **直接发起评测**：选题库 / 档位 / 裁判开关 / 条数，异步执行、看进度、可取消。
5. CLI 行为与退出码语义**完全不变**（`ci.yml`、`eval-nightly.yml` 依赖它）。

### 非目标（明确不做）

| 不做 | 理由 |
|---|---|
| 在线编辑题库 / rubric | 题库进 git 才有 sha 指纹与可比性；网页改会让 `suite_sha` 失去意义。要改仍走 PR |
| 在线改阈值（`thresholds` / `min_cases_for_gate`） | 阈值是门禁契约，`ci.yml` 已校验与 manifest 同步；网页可改等于绕过 CI |
| 多实例 / 多 worker 支持 | 当前是单用户本地部署（`main.py` 无鉴权、无身份体系）；在线运行按「单进程单槽位」实现，见 §10 |
| 事件流实时推送（SSE / WebSocket） | 评测是分钟级批处理，2s 轮询足够；引入 SSE 要多一套断线重连语义 |
| 保存原始事件里的每个 delta | `TEXT_BLOCK_DELTA` 一条回复几百条，存下来只有体积没有信息 |
| 前端重算门禁 / verdict | 两套判定逻辑必然互相打脸；结论只由 `report.py` 产出 |

## 3. 需要暴露的四块数据（先定契约，再谈端点）

```
┌─────────────── 静态契约（不读 DB，读代码/文件） ───────────────┐
│ 规则目录 RULES (S1..S8 说明)   rubric 六维锚点   known-issues   │
└──────────────────────────────────────────────────────────────┘
┌─────────────── 批次级（已有，补裁判标注） ─────────────────────┐
│ summary_json: verdict / gates / hard_failures / judge 均值      │
└──────────────────────────────────────────────────────────────┘
┌─────────────── 用例级（已有 + 新增 evidence） ─────────────────┐
│ passed / checks / trajectory / tool 三档 / judge{score}         │
│ + judge_evidence{dim: 理由}  ← 新增                             │
└──────────────────────────────────────────────────────────────┘
┌─────────────── 用例级（全新） ─────────────────────────────────┐
│ trace: steps[] 时序 + fixture + checks + trajectory + result    │
└──────────────────────────────────────────────────────────────┘
```

## 4. 数据模型变更

### 4.1 规则目录：`lint.py` 增加 `RULES` 常量

规则说明现在只存在于 `_finding(...)` 调用点的字符串里，前端拿不到。新增单一事实源：

```python
@dataclass(frozen=True, slots=True)
class RuleSpec:
    id: str  # "S7"
    level: str  # "error" / "warning"（该规则的默认级别）
    title: str  # "断言不得与题面撞车"
    description: str  # 查什么
    hint: str  # 怎么修


RULES: tuple[RuleSpec, ...] = ...  # S1..S8，顺序固定
```

`_finding()` 不改；`run_lint` 行为不改。`RULES` 只被 API 与前端读取，因此加它**不改变任何判决**。

### 4.2 裁判 evidence：`judge.py` 返回值从 `dict[str, float]` 升级为结构化结果

```python
@dataclass(frozen=True, slots=True)
class DimensionVerdict:
    score: float  # 1..5
    evidence: str  # 模型给的理由；缺失时为空串，不编造


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    scores: dict[str, float]  # 维度 → 分数（与原契约一致）
    evidence: dict[str, str]  # 维度 → 理由
```

- `Judge` Protocol 改为 `score(...) -> JudgeOutcome`。
- `_parse_scores` 改为同时解析 `evidence`（`str(entry.get("evidence") or "").strip()`，超长截断 1000 字符）；**分数校验逻辑一字不改**。
- `evidence` 缺失不报错、不影响 `missing` 维度判定——它只是标注，不是契约字段。
- 受影响的假裁判（`tests/unit/test_eval_judge.py`、`test_eval_runner.py`、`test_eval_judge_factor.py` 里的 fake）需跟着改返回类型；这是本次唯一破坏性改动，已在 §13 PR-2 列出。

### 4.3 `CaseRun` 增加 evidence，落库路径不变

```python
@dataclass(frozen=True, slots=True)
class CaseRun:
    ...
    judge_scores: dict[str, float] | None
    judge_evidence: dict[str, str]  # 新增，无裁判时为空 dict
```

`case_run_payload()` 增加一个字段 `"judge_evidence": run.judge_evidence`。因为 `_persist()` 已经把整个 payload 写进 `detail_json`，**evidence 自动落库，不需要新表/新列**。`scores_json` 保持「维度 → 浮点」，`report._judge_means`、`_case_out` 的既有读取路径全部不动。

### 4.4 轨迹：`eval_cases` 加一列 `trace_json`

```sql
ALTER TABLE eval_cases ADD COLUMN trace_json TEXT NOT NULL DEFAULT '';
```

沿用仓库既有做法（`_RUN_COLUMNS` / `_SPAN_COLUMNS` 的 `ALTER TABLE ... ADD COLUMN` 迁移），新增：

```python
_EVAL_CASE_COLUMNS: tuple[tuple[str, str], ...] = (("trace_json", "TEXT NOT NULL DEFAULT ''"),)
```

`_migrate()` 里补一行 `_ensure_columns(conn, "eval_cases", _EVAL_CASE_COLUMNS)`。老批次读出来是空串，前端显示「该批次未采集轨迹」。

**为什么不放进 `detail_json`**：`GET /batches/{id}/cases` 一次返回 50 条用例，轨迹是每条约几十 KB 的时序数据，塞进 `detail_json` 会让列表接口从十几 KB 涨到几 MB。列表只读 `detail_json`，轨迹按需单条读。

### 4.5 `eval_batches.status` 新增取值

现有隐含取值：`running`（`create_eval_batch` 默认）/ `completed`（`finish_eval_batch`）。新增：

| 值 | 含义 | 由谁写 |
|---|---|---|
| `running` | 执行中 | 启动时创建 |
| `completed` | 跑完（无论 verdict 是 pass/fail/insufficient） | 结束时 |
| `cancelled` | 用户取消，已跑用例保留 | 取消时 |
| `failed` | 启动前置失败或装配异常（如缺凭据） | 启动/异常时立即写 |

`_verdict()` 已对 `status == "running"` 特判返回 `"running"`，其余状态看 `summary_json.verdict`；`cancelled` / `failed` 批次没有 summary，前端显示状态徽标而不是 verdict 徽标（`VERDICT_LABEL` 需补两项）。

## 5. 后端改造：抽出 `eval/service.py`

### 5.1 为什么要抽

`cmd_run`（`cli.py:347-416`）里 70 行是**执行逻辑**（读 env → 检查凭据 → 装配裁判 → 跑 suite → 聚合 → 落库 → 写文件 → 决定退出码）。API 要复用前 6 步，只有第 7 步（退出码）是 CLI 独占的。

做成 API 自己复制一份 = 两份聚合口径，必然分叉。

### 5.2 模块契约

```python
@dataclass(frozen=True, slots=True)
class RunOptions:
    suite_path: Path
    db_path: Path
    out_dir: Path | None  # None = 不写文件（API 场景只需要 DB）
    profile: str | None
    limit: int | None
    judge: bool
    judge_scope: str = "all"
    baseline_path: Path | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    batch_id: int
    summary: dict[str, Any]
    runs: list[CaseRun]


class EvalNotRunnable(RuntimeError):
    """前置条件不满足（缺凭据 / 裁判不可用 / 未知题库）。"""
```

```python
def check_runnable(suite_path: Path, *, judge: bool) -> str | None:
    """返回不可运行原因，None 表示可以跑。用于 GET 能力探测与 POST 前置校验。"""


async def run_eval(
    options: RunOptions,
    *,
    on_case: Callable[[CaseRun, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RunResult:
    """创建批次 → 逐条执行并增量落库 → 聚合 → 回填 summary。"""
```

`run_eval` 的执行顺序（与 CLI 现状对齐，唯一差异是**增量落库**）：

1. `check_runnable`，失败抛 `EvalNotRunnable`。
2. `repo.create_eval_batch(status="running")` → `batch_id`（**先建批次**，前端立刻能拿到 id）。
3. `runner.run_suite(..., on_case=..., should_stop=...)`。
4. 每跑完一条：`repo.insert_eval_case(..., trace_json=...)` + `on_case(run, done, total)`。
5. `report.aggregate(...)` → `summary.update(_factors(...))` → 可选 `_apply_baseline`。
6. `repo.finish_eval_batch(status="completed"|"cancelled", summary_json, finished_at)`。
7. 若 `out_dir` 非空：`_write_outputs(...)`（CLI 需要 report.md / cases.jsonl）。

`_persist` 拆成 `_open_batch` + `_insert_case` + `_close_batch` 三个小函数放进 `service.py`；`cli._persist` 删除，`cli.cmd_run` 改为调用 `run_eval` 并只保留「决定退出码」的 10 行。

### 5.3 `runner.py` 的两处新增参数

```python
async def run_suite(
    suite, make_runtime, fixture_root=None, judge=None, limit=None, judge_scope="all",
    *,
    on_case: Callable[[CaseRun, int, int], None] | None = None,   # (run, done, total)
    should_stop: Callable[[], bool] | None = None,
) -> list[CaseRun]:
```

- `should_stop()` 在**每条用例开始前**检查一次（不在用例中途打断：中途打断会留下半截工具调用和未清理的临时目录语义）。
- 在途用例跑完后立即停止，返回已完成的 `runs`。

### 5.4 轨迹采集：`_consume` 旁边加一条旁路

`_consume` 的 `state` 里新增 `"steps": []`，每个事件分支同时追加一条 step（**聚合后**，不是逐 delta）：

| 事件 | step.kind | detail |
|---|---|---|
| fixture 复制 | `fixture` | `copytree <name> → <basename>`，失败时 `status=error` |
| `TOOL_CALL_START` | `tool_call` | `tool` + `args_preview`（工具参数 JSON 截断 300 字符） |
| `TOOL_RESULT_END` | `tool_result` | `tool` + 三档 `status ∈ {executed, denied, pending}` |
| `TOOL_RESULT_END`（state=error） | `tool_error` | 错误原文截断 500 字符 |
| `TEXT_BLOCK_END` | `reply` | `chars` 计数，不存正文（正文已在 `final_text`） |
| `MODEL_CALL_END` | `model` | `tokens` / `cache_input` / `cache_creation` |
| `REQUIRE_USER_CONFIRM` | `confirm` | 等待确认，轨迹终止 |
| 超时 / 异常 | `error` | `detail` |

每条 step 形如 `{"seq": int, "t_ms": int, "kind": str, "status": str, "tool": str|None, "detail": str}`。约束：

- `MAX_TRACE_STEPS = 400`，超出后停止追加并置 `truncated: true`（不静默截断）；
- `detail` 单条截断 500 字符；`args_preview` 300 字符；
- `t_ms` 相对用例开始时刻。

`execute_case` 组装 `trace_payload`（§6.5 的 JSON）并随 `CaseRun` 返回：

```python
@dataclass(frozen=True, slots=True)
class CaseRun:
    ...
    trace_payload: dict[str, Any]  # 写 trace_json 用；CLI 不关心
```

## 6. API 契约

全部挂在既有 `router = APIRouter(prefix="/api/eval")`。**不新增第二个 router**，避免前端拼两套前缀。

### 6.1 `GET /api/eval/contract` — 静态契约（规则 + rubric + 可运行性）

不读 DB，只读代码常量与文件目录。

```json
{
  "rules": [
    {"id": "S1", "level": "error", "title": "工具名闭包",
     "description": "提示词里提到的工具必须都在注册表里", "hint": "..."}
  ],
  "judge": {
    "dimensions": ["correctness", "coherence", "relevance", "objectivity", "clarity", "conciseness"],
    "labels": {"correctness": "正确性", "coherence": "连贯性", "relevance": "相关性",
               "objectivity": "客观性", "clarity": "清晰度", "conciseness": "简洁性"},
    "rubric": "PEEM 六维评分锚点（1–5 分）：\n- correctness（正确性）：5=...",
    "model": "qwen3.8-flash",
    "enabled": true
  },
  "runner": {
    "available": true,
    "reason": null,
    "suites": ["devops-core.v2"],
    "profiles": ["explore", "readonly", "delivery"],
    "max_limit": 40
  }
}
```

- `judge.model` 取 `resolve_judge_model()`（只回模型名，**绝不回 key / base_url**）。
- `runner.available=false` 时 `reason` 给可读原因（如「缺少 DEVOPS_AGENT_API_KEY」），前端据此禁用「运行」按钮并显示原因，而不是点下去才 400。

### 6.2 `GET /api/eval/lint?suite=devops-core.v2` — 当前题库的 findings

```json
{
  "suite": "devops-core.v2",
  "ran_at": "2026-11-24T13:00:00+00:00",
  "summary": {"error": 0, "warning": 2, "accepted": 0, "stale": 0},
  "findings": [
    {"rule": "S5", "level": "warning", "where": "case v-012",
     "message": "must_contain 的 3 条断言全部命中同一句模板",
     "hint": "...", "accepted": false}
  ],
  "accepted": [],
  "stale": [],
  "assertions": {"must_contain": 88, "must_cite": 6, "dead": 0}
}
```

- `suite` 参数是**题库 id**（文件名去扩展名），不是路径；服务端只在该目录下匹配，见 §9。
- 豁免清单显式读 `eval/lint-baseline.json`（与 `ci.yml` 的 `--known-issues eval/lint-baseline.json` 一致；CLI 侧该参数默认值本就是 `None`，不能靠默认值）。把 `cli._load_known_issues` 与 `cli._apply_known_issues` 移到 `service.py`（`cli` 改为 import），保证网页与 CI 用**同一份豁免清单**。
- `stale` 在网页上不阻断（网页没有退出码），但要显式列出并提示「已修好，请从 baseline 删除」——这正是该机制的用途。
- 计算量：`run_lint` 含 S8 会真跑一次 `bash_decision`，约 10–50ms；结果按 `(suite_sha, grader_version)` 放进程内 LRU（上限 8 条），题库改动指纹变了自然失效。

### 6.3 `POST /api/eval/runs` — 异步启动

请求：

```json
{"suite": "devops-core.v2", "profile": null, "judge": true,
 "judge_scope": "all", "limit": null}
```

响应 `202`：

```json
{"run_id": 41, "batch_id": 41, "status": "running", "total": 40}
```

- `run_id` 就是 `batch_id`（不引入第二套 id，避免两套状态机）。
- 校验顺序：未知 suite → 400 `unknown_suite`；`limit` 非正或超总数 → 400 `bad_limit`；未知 profile → 400 `unknown_profile`；`check_runnable` 失败 → 400 `not_runnable` + `reason`；已有在跑批次 → 409 `already_running` + `{"running_id": 40}`。
- 启动失败（装配异常）在后台捕获，批次写 `status="failed"`、`summary_json={"error": "..."}`，**不在 HTTP 层抛 500**：请求已 202，错误要能被 `GET /runs/{id}` 查到。

### 6.4 `GET /api/eval/runs/{run_id}` — 状态与进度

```json
{
  "run_id": 41, "batch_id": 41, "status": "running",
  "progress": {"done": 12, "total": 40, "current_case": "v-013"},
  "started_at": "...", "finished_at": null,
  "verdict": null, "error": null
}
```

- `done/total` 由 **DB 行数**得出（`count_eval_cases`），不依赖内存计数器——进程重启后状态依然正确。
- `current_case` 来自内存中的在跑用例 id，进程重启后为 `null`（诚实降级，不猜）。
- 终态响应额外带 `verdict`（读 `summary_json`）与 `error`。

### 6.5 `GET /api/eval/batches/{batch_id}/cases/{case_id}/trace`

```json
{
  "case_id": "v-012",
  "fixture": "python-basic",
  "truncated": false,
  "steps": [
    {"seq": 0, "t_ms": 0,   "kind": "fixture",     "status": "ok",       "tool": null,  "detail": "copytree python-basic"},
    {"seq": 1, "t_ms": 812, "kind": "tool_call",   "status": "ok",       "tool": "read_file", "detail": "{\"path\": \"app.py\"}"},
    {"seq": 2, "t_ms": 845, "kind": "tool_result", "status": "executed", "tool": "read_file", "detail": ""},
    {"seq": 3, "t_ms": 990, "kind": "tool_call",   "status": "ok",       "tool": "bash", "detail": "{\"command\": \"rm -rf /\"}"},
    {"seq": 4, "t_ms": 1012,"kind": "tool_result", "status": "denied",   "tool": "bash", "detail": ""},
    {"seq": 5, "t_ms": 2100,"kind": "reply",       "status": "ok",       "tool": null,  "detail": "chars=1024"}
  ],
  "checks": [{"name": "tool_executed", "ok": true, "detail": "must=[read_file] actual=[read_file]"}],
  "trajectory": {"score": 0.9, "ordered": true, "must": ["read_file"], "actual": ["read_file", "bash"]},
  "judge": {
    "model": "qwen3.8-flash", "scope": "all",
    "scores": {"correctness": 4, "coherence": 4},
    "evidence": {"correctness": "结论引用了 app.py 的实际输出", "coherence": "先读后答，步骤闭合"}
  },
  "result": {"passed": true, "hard_failures": [], "latency_ms": 2310, "tokens": 4210,
             "cache_input": 1024, "cache_creation": 0, "error": null}
}
```

- 404 `case_not_found`（该批次没有这个 case_id）、404 `trace_missing`（老批次 `trace_json=""`），两码分开：前者是查错了，后者是「这条批次采集时还没有轨迹功能」。
- 响应体量上限：`steps ≤ 400`，总 JSON 建议 < 512KB（`MAX_TRACE_STEPS` 保证）。

### 6.6 `POST /api/eval/runs/{run_id}/cancel`

- 在跑：置取消标记，**当前用例跑完后**停止，批次写 `status="cancelled"`，已完成的用例与 summary 保留（`verdict` 按已跑样本算）。返回 `202 {"status": "cancelling"}`。
- 已终态：409 `not_running`。
- 不存在：404 `run_not_found`。

### 6.7 错误码总表

| 状态码 | code | 触发 |
|---|---|---|
| 400 | `unknown_suite` / `bad_limit` / `unknown_profile` / `not_runnable` | 请求参数或前置条件 |
| 404 | `batch_not_found` / `case_not_found` / `trace_missing` / `run_not_found` | 查不到 |
| 409 | `already_running` / `not_running` | 状态冲突 |
| 202 | — | 已受理（启动 / 取消） |

所有错误体沿用 FastAPI 的 `{"detail": "<code>"}`，前端 `getJson` 现有逻辑已能透出 `请求失败 <status>: <path>`；本次只加一个 `postJson`，把 `detail` 也带出来。

## 7. 前端信息架构

### 7.1 `Quality` 页视图

```
质量评估
┌ 概览 │ 评测集 │ 评测批次 │ 规则与裁判 ┐            [运行评测]
```

- 新增第 4 个视图 `rules`（`RulesPanel.tsx`）：上半是 **S1–S8 目录**（id / 级别 / 查什么 / 怎么修），下半是**当前题库 findings**（error 红、warning 黄、accepted 灰、stale 带「请清理」提示），再加一块 **rubric 锚点表**（六维 × 1/3/5 分描述）。
- 「运行评测」是**操作**不是视图，做成页头按钮（`RunEvalDialog.tsx`），任何视图下都能点。
- 视图切换与按钮都受 `contract.runner.available` 控制；`available=false` 时按钮禁用 + `title` 显示 `reason`。

### 7.2 运行对话框

字段：题库（`contract.runner.suites`）、档位（`profile`，默认「按用例自声明」= null）、裁判开关、裁判范围（`all` / `passed`，`passed` 旁注明「省额度但有偏：难用例退出分母」）、条数上限（默认空 = 全部）。

提交后对话框变成**进度面板**，不关闭：

```
批次 #41 · 运行中   12/40          [取消]
[██████░░░░░░░░░░░░]   当前：v-013
```

- 轮询 `GET /runs/{id}`，间隔 2s（`refetchInterval` 函数式返回 `status === 'running' ? 2000 : false`）。
- 终态后：`completed` 关闭对话框并 `setSelected(batch_id)` 跳到该批次；`cancelled` 提示「已取消，保留了 12 条结果」并同样跳转；`failed` 就地显示 `error`，不跳转。
- 期间若 409，直接把用户带到 `running_id` 那一批。

### 7.3 批次详情的用例行

- 现有展开行（`CaseRow`）保留 checks 明细，新增一个 **「轨迹」按钮**（只在有 `trace` 时可用，老批次显示禁用 + `title="该批次未采集轨迹"`）。
- 点击打开**右侧抽屉**（`CaseTraceDrawer.tsx`，宽 560px，可 Esc 关闭）：
  1. 头部：case_id / tags / passed / 延迟 / tokens / 缓存命中；
  2. **时序轴**：`steps` 按 `t_ms` 渲染为竖向时间线，工具步显示 `tool` + `status` 三色（executed 绿 / denied 红 / pending 黄），`truncated` 时底部显示「仅保留前 400 步」；
  3. **断言逐条**：`checks` 列表，失败项默认展开；
  4. **轨迹分**：`trajectory.score` + must/actual 两列对照；
  5. **裁判**：每维一行「维度名 / 分条 / evidence 原文」，evidence 为空时显示「模型未给出理由」——不补写。
- judge mini 条（现有 `judge-mini`）加 `title`，悬停显示该维 evidence 的前 60 字，避免为看一眼理由就开抽屉。

### 7.4 `client.ts` / `types.ts` 增量

```ts
export function fetchEvalContract(): Promise<EvalContract>
export function fetchEvalLint(suite: string): Promise<EvalLintReport>
export function startEvalRun(body: EvalRunRequest): Promise<EvalRunAccepted>
export function fetchEvalRun(id: number): Promise<EvalRunStatus>
export function cancelEvalRun(id: number): Promise<void>
export function fetchEvalTrace(batchId: number, caseId: string): Promise<EvalTrace>
```

新增 `postJson<T>(path, body)`：非 2xx 时解析响应体 `detail`，抛 `请求失败 ${status}: ${path} (${detail})`。`EvalCaseItem` 增加 `judge_evidence: Record<string, string>`。

## 8. 校验与联动

| 位置 | 规则 | 失败表现 |
|---|---|---|
| `POST /runs` | suite 必须是 `eval/suites/*.jsonl` 的 id | 400 `unknown_suite` |
| `POST /runs` | `limit ∈ [1, total]` | 400 `bad_limit` |
| `POST /runs` | `profile` 必须能被 `governance.profiles.resolve` 解析 | 400 `unknown_profile` |
| `POST /runs` | `check_runnable`（凭据 / 裁判初始化） | 400 `not_runnable` |
| `POST /runs` | 无在跑批次 | 409 `already_running` |
| `judge.py` | evidence 截断 1000 字符 | 不报错，静默截断并保留分数 |
| `runner.py` | `trace` 超 400 步 | `truncated=true` |
| `eval_api.py` | 老批次 `trace_json=""` | 404 `trace_missing` |

## 9. 安全

1. **凭据永不出后端**：`contract` 只回模型名；`POST /runs` 不接收也不返回任何 key/base_url。执行用的 `DEVOPS_AGENT_API_KEY` 只在进程环境里读。
2. **不接受任意路径**：请求里的 `suite` 是 id，服务端用 `_SUITES_DIR.glob("*.jsonl")` 的 stem 匹配；不拼接用户字符串进 `Path`。`profile` 走 `resolve()` 白名单。
3. **在线运行会花真钱、会执行命令**：这是既有权限层（`governance/policy.py` 的三档）与 `executor/boundary.py` 沙箱保护的，本次不新增执行面；但要在对话框首屏写清「将调用真实模型，按条计费；每条用例在独立临时目录中执行」。
4. **继承现状的无鉴权**：`main.py` 目前没有身份体系，所有写接口同样无鉴权（如 `POST /api/feedback/{run_id}`）。本 spec 不引入鉴权，但要求：服务默认只绑 `127.0.0.1`（部署约定，写入 README 的一句话即可），且 `contract.runner.available` 在没有凭据时为 false——本地不用 key 的情况下，在线运行天然不可用。
5. **CI 不使用在线运行**：`ci.yml` / `eval-nightly.yml` 一行不改，继续走 CLI。原因：CI 里起 uvicorn 跑评测等于把「批处理」变成「服务依赖」，失败模式完全不同。

## 10. 并发与多实例

- **单进程单槽位**：`service` 模块级 `_ACTIVE: dict[int, RunHandle]`，`RunHandle` 持有 `asyncio.Task` + `threading.Event` 取消标记。启动前检查 `_ACTIVE`，在跑则 409。
- **进程重启后的孤儿批次**：`status="running"` 且 `started_at` 早于 `RUN_STALE_AFTER`（默认 2 小时）的批次，在 `GET /runs/{id}` 与 `POST /runs` 里视为 `stale`：`GET` 返回 `status="stale"` + `error="进程重启，该批次不会再推进"`，`POST` 允许新建（不因孤儿批次永久阻塞）。
- **多 worker 不支持**：`uvicorn --workers>1` 时两个 worker 各自有 `_ACTIVE`，互斥失效。启动时若检测到 `WEB_CONCURRENCY > 1`，在 `/health` 的 `runtime_status` 里追加 `eval_runner="unsupported_multi_worker"`；不做强制拒绝（那是部署事故，不是用户操作）。
- **取消的粒度**：用例边界，不打断在途用例。理由：打断在途用例会留下「工具已调用但结果未消费」的半截轨迹，而这种轨迹最容易让人误判成「模型卡住了」。

## 11. 迁移与回滚

### 迁移

1. `eval_cases` 加 `trace_json`：`_ensure_columns` 幂等补列，老库打开时自动加，老行默认 `''`。
2. 老批次前端表现：轨迹按钮禁用（`trace_missing`），judge 无 evidence 时显示「该批次未采集理由」（因为 `detail_json.judge_evidence` 不存在）。
3. `known-issues` 函数从 `cli.py` 移到 `service.py`：`cli` 保留同名 import，`ci.yml` 命令不变。

### 回滚

- 代码回滚：单个 PR revert 即可，无数据破坏性改动（只加列、只加字段）。
- `trace_json` 不回滚删除：留着的空列不影响任何老代码路径（`SELECT *` 会多一列，`_eval_case_from_row` 是显式取字段，不受影响）。
- `status` 新增取值对老前端：`VERDICT_LABEL` 未命中时显示原始字符串（现有 `?? batch.verdict` 兜底），不会崩。

## 12. 测试清单

### 后端（`tests/unit/`）

| 文件 | 覆盖点 |
|---|---|
| `test_eval_lint_rules.py`（新） | `RULES` 覆盖 S1–S8、id 唯一、level ∈ {error, warning}、与 `run_lint` 实际产出的 rule 集合一致（防止规则目录腐化） |
| `test_eval_contract_api.py`（新） | `GET /contract` 字段齐全；无 key 时 `runner.available=false` 且 `reason` 非空；`judge.model` 不含 key |
| `test_eval_lint_api.py`（新） | findings 分级；accepted / stale 分档（用临时 baseline 文件）；未知 suite → 400；LRU 命中不重复跑 S8（monkeypatch `_achievable` 计数） |
| `test_eval_judge.py`（改） | `evidence` 被解析；缺失 evidence 不影响分数；空串归一；超长截断 |
| `test_eval_runner.py`（改） | fake judge 返回 `JudgeOutcome`；`judge_evidence` 进 `CaseRun`；`on_case` 回调次数与 `done/total`；`should_stop` 在第 N 条前停止且不产生第 N 条记录 |
| `test_eval_trace.py`（新） | step 序列与事件对应；`denied` 正确归到 tool_result；超 400 步置 `truncated`；detail 截断 |
| `test_eval_run_api.py`（新） | 通过 `app.state.eval_runtime_factory` 注入假运行时（与 `app.state.runtime` 同款注入方式）：202 启动 → 轮询到 completed → cases 可查；409 并发；cancel 在中途生效且状态为 `cancelled`、已完成用例保留；`failed` 批次能被 `GET /runs/{id}` 读到 error |
| `test_eval_trace_api.py`（新） | 正常返回；`trace_missing` 与 `case_not_found` 分开 |
| `test_eval_exit_codes.py`（不改） | **必须原样通过**：CLI 重构后退出码语义不变（0/1/2 三档） |
| `test_eval_api*.py`（不改） | 既有只读端点响应结构不变（新增字段允许） |

覆盖率：`--cov-fail-under=80` 不放宽；新增模块（`service.py`、trace 采集、新端点）须全部有测试。

### 前端（`web/src/pages/**/*.test.tsx`）

| 文件 | 覆盖点 |
|---|---|
| `Quality.test.tsx`（改） | 第 4 个视图可切换；`runner.available=false` 时运行按钮禁用且带原因 |
| `RulesPanel.test.tsx`（新） | 规则目录渲染 S1–S8；error/warning/accepted/stale 四种样式；rubric 锚点表 |
| `RunEvalDialog.test.tsx`（新） | 提交 → 显示进度 → completed 跳批次；cancelled 文案含完成条数；409 跳 running_id |
| `CaseTraceDrawer.test.tsx`（新） | 时间线渲染 denied 红色；evidence 为空显示「模型未给出理由」；`truncated` 提示 |
| `EvalBatches.test.tsx`（改） | judge mini 条 title 含 evidence 前缀；无 trace 时按钮禁用 |

## 13. 分期实施

每个 PR 独立可合并、可回滚；PR 内遵循 AGENTS.md（worktree + PR + `quality` 绿灯）。

### PR-1 规则目录与静态契约

- `lint.py`：加 `RuleSpec` / `RULES`（S1–S8）。
- `eval_api.py`：`GET /contract`、`GET /lint`。
- `service.py`（新建，仅含 `_load_known_issues` / `_apply_known_issues` / `_suite_path` / `check_runnable`）。
- 测试：`test_eval_lint_rules.py`、`test_eval_contract_api.py`、`test_eval_lint_api.py`。
- 文件量：约 5 个。

### PR-2 裁判 evidence

- `judge.py`：`DimensionVerdict` / `JudgeOutcome`，`_parse_scores` 解析 evidence。
- `runner.py`：`CaseRun.judge_evidence`，`case_run_payload` 加字段。
- `eval_api.py`：`_case_out` 加 `judge_evidence`。
- 测试：改 3 个 fake judge 相关文件 + `test_eval_judge.py` 补 evidence 用例。
- 文件量：约 8 个（含 4 个测试）。

### PR-3 轨迹采集

- `runner.py`：`state["steps"]`、`MAX_TRACE_STEPS`、`CaseRun.trace_payload`。
- `repository.py`：`_EVAL_CASE_COLUMNS` + `_migrate` 补一行；`insert_eval_case(trace_json=...)`。
- `eval_api.py`：`GET /batches/{id}/cases/{case_id}/trace`。
- `cli.py`：`_persist` 传 `trace_json`。
- 测试：`test_eval_trace.py`、`test_eval_trace_api.py`。

### PR-4 在线运行

- `service.py`：`RunOptions` / `RunResult` / `run_eval` / 增量落库 / 单槽位 / stale 判定。
- `cli.py`：`cmd_run` 改为调用 `run_eval`，退出码逻辑保持不变；删 `_persist`。
- `eval_api.py`：`POST /runs`、`GET /runs/{id}`、`POST /runs/{id}/cancel`；模块 docstring 改为「只读端点 + 受控运行入口」。
- `main.py`：`app.state.eval_runtime_factory = None`（测试注入口）。
- 测试：`test_eval_run_api.py`；`test_eval_exit_codes.py` 必须原样通过。
- 文件量：约 7 个。

### PR-5 前端

- `types.ts` / `client.ts`：契约、trace、run 三组类型与 `postJson`。
- 新增 `pages/quality/RulesPanel.tsx`、`pages/RunEvalDialog.tsx`、`pages/CaseTraceDrawer.tsx`。
- 改 `Quality.tsx`（第 4 视图 + 运行按钮）、`EvalBatches.tsx`（轨迹按钮、judge title、`cancelled`/`failed` 徽标）。
- 测试：§12 前端 5 个文件。
- 文件量：约 10 个。

### PR-6 文档

- `README`（或部署说明）加一句：服务默认绑 `127.0.0.1`；在线运行需 `DEVOPS_AGENT_API_KEY`。
- `eval/README`（若有）标注「网页运行与 CLI 走同一 `service.run_eval`，退出码语义仅 CLI 有」。

## 14. 未决问题（附建议默认值）

| # | 问题 | 建议默认 | 影响面 |
|---|---|---|---|
| 1 | 是否允许 N 个批次并发 | **否**，单槽位 + 409 | 并发实现复杂度立刻翻倍（配额争抢、进度串台）；评测本来就是串行跑 |
| 2 | trace 保留策略（是否定期清理） | **不清理**，靠 400 步上限控制体量 | 单批次 40 条 × ~40KB ≈ 1.6MB；DB 增长可接受，清理逻辑属于运维，不该塞进本 spec |
| 3 | judge evidence 是否单独建表 | **否**，进 `detail_json` | 现状 `_persist` 已经整包写 payload，加字段零成本；单独建表要多一套读写 |
| 4 | 取消后 `status` 用 `cancelled` 还是 `partial` | **`cancelled`** | 「partial」容易被读成「部分通过」；`cancelled` 只说发生了什么 |
| 5 | 是否展示 `passed` 档的裁判覆盖（有偏） | **展示但默认选 `all`** | 与 `runner._score_with_judge` 的注释一致：`passed` 会让退化时 judge 均值反升 |
| 6 | `GET /lint` 是否允许指定任意题库路径 | **否**，只接受 id | 任意路径等于把 `run_lint` 的文件读取能力开放给 HTTP；虽然同机，也没必要 |
| 7 | 在线运行是否要「试跑 3 条」快捷按钮 | **PR-5 顺带做**，`limit=3` 预填 | 这是「改完题库立刻验证」最常用的路径，成本只有一行默认值 |
