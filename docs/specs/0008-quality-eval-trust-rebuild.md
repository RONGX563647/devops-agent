# SPEC-0008：质量页与评测可信度重建

- 状态：Draft（待评审；评审通过后按 §7 分期实施）
- 日期：2026-11-26
- 相关：`src/devops_agent/openjudge/{report,run,grade,calibration,store,api}.py`、
  `web/src/pages/Quality.tsx`、`web/src/pages/quality/**`、
  `docs/wiki/Observability-and-Eval.md`
- 参考：《Agent 评测白皮书》系列 01：Agent 评测全览（美团技术团队，2026-09-10）

## 1. 背景

质量页现在是「概览 / 运行与批次 / 用例与模型」三视图，评测侧已有五赛道 90 例、
六维裁判、三道可信度门禁、批次对比与覆盖率棘轮。但实际使用时有两个强烈感受：
**前端读不懂，评测不敢信**。

对着白皮书的四个模块（离线评测 / 在线评测与监控 / Case 挖掘与归因 / 观测基建）
逐项核对，问题不在于「缺模块」，而在于**模块之间没有打通，且结论语义被压扁**：
离线评测的 verdict 同时承担了「模型好不好」「证据齐不齐」「测全了没有」三种含义，
于是任何一项不足都会把整批结论打成同一个词。

## 2. 现状审计（可复现）

> 复现环境：`main@55b16ae`，本地库无批次、无校准记录。

### 2.1 结论层：`pass` 在当前配置下不可达

| # | 现象 | 证据 |
|---|---|---|
| E1 | 无校准记录时，**全部 90 条全过也判 `insufficient`** | `report.py:191-204` 把非 fail 赛道统一降级；`calibration.py:81-103` 无记录即 `valid=False`；`run.py:113-138` 据此令 `judge_gate=False` |
| E2 | 单赛道批次注定 `insufficient` | `report.py:129-132`（缺 stats → insufficient）+ `report.py:207-213`（任一 insufficient 即总判 insufficient） |
| E3 | 默认在线参数（A 赛道 / 10 条 / 不跑裁判）必然 `insufficient` | `RunBar.tsx:152-154` 默认 `track='A' / limit=10 / judge=false`；`report.py:139-142` 无裁判均分即 insufficient |
| E4 | **裁判没跑等于通过** | `grade.py:388-393`：`judge_norm is None` 时 `judge_ok=True`，`passed` 只由规则决定 |
| E5 | 四种不同原因被显示成同一句「样本不足」 | `labels.ts:10-16` 只有一个 `insufficient` 文案；真实原因分散在 `report.py:82-126` 的 `sample_short / judge_missing / judge_below / judge_uncalibrated` |

实测（`report.summarize` 直接调用）：

```text
单赛道 10 条全过            -> verdict: insufficient   （A 赛道自身是 pass，B–E 未测）
全 90 条全过 + 无校准记录    -> verdict: insufficient   （五赛道全部 insufficient）
全 90 条全过 + 有校准记录    -> verdict: pass
```

结论：**在当前仓库状态下，质量页永远不会显示「通过」**。这不是模型差，是判决语义
把「证据不足」和「未测」都算进了「样本不足」，而校准记录的门槛（`n≥100`、
Spearman `≥0.7`，见 `calibration.py:14-15`）实践中没人会去满足。

### 2.2 归因层：裁判低分查不到原因

| # | 现象 | 证据 |
|---|---|---|
| E6 | 只被裁判判负的用例，`failed_checks` 为空、不进 Pareto | `report.py:148-171` 只统计 `result["checks"]`（规则断言） |
| E7 | 失败清单截断 50 条、Pareto 截断 20 条，且不告诉前端「被截断了」 | `report.py:302-303` |
| E8 | 过程指标缺失：只有延迟 P95、token 总量、终态分布 | `report.py:243-260` |
| E9 | 成本恒为 `0.0` | `report.py:255` |
| E10 | 显著性判断用 `1/n`，不是两比例检验 | `format.ts:precisionLimit` + `Overview.tsx:40-43` |

实测（裁判给 0.2 分、规则全过）：

```text
A 赛道: fail
failures: [{"case_id": "a3", "failed_checks": [], "repro": "..."}, ...]
pareto: []
```

即：**页面告诉你「未达标」，但一条失败原因都给不出**；白皮书要求的「端到端报警、
过程定位到层」在这里断在第二句。

### 2.3 前端层

| # | 现象 | 证据 |
|---|---|---|
| F1 | 趋势图门禁线取「第一个赛道」的阈值，画在**聚合通过率**上 | `Overview.tsx:47-53`；`/suites` 顺序固定 A→E，所以永远画 A 的 0.60（B 是 0.80、D 是 1.00） |
| F2 | 批次只取 20 条、无分页；「共 N 批」其实是页大小 | `OpenJudge.tsx:126`；`api.py:64` 的 `total` 是 `len(items)`；`BatchStrip` 显示 `共 {batches.length} 批` |
| F3 | 点任意门禁的「看失败」，永远打开**第一条**失败 | `OpenJudge.tsx:278-282`（`inspect` 忽略传入的 tab） |
| F4 | 定位失败用例时硬编码 `limit: 500`，>500 条的批次找不到 | `OpenJudge.tsx:237` |
| F5 | 视图与 URL 不同步：`?focus=scoreflow` 只在初始化读一次 | `Quality.tsx:18-29`。复现：概览 →「看评分流程」→ 切「概览」→ 刷新，又回到「运行与批次」 |
| F6 | 时间硬编码 UTC 且按字符串切片，GMT+8 用户看到的时间差 8 小时 | `Overview.tsx:96-101`（`replace('T',' ').slice(0,19)`，detail 写 `UTC`） |
| F7 | 赛道表只渲染**跑过的**赛道，未测的不出现 | `OpenJudge.tsx:179`（`Object.keys(tracks)`）；总判却是 insufficient，界面无法自证原因 |
| F8 | `/health` 的 `calibration` 永远 `null`，与 `/calibration` 的 `checked/valid` 自相矛盾 | `store.py:391,403` vs `api.py:195-222` |
| F9 | 单批详情不展示 `meta`（suite_sha / plan_sha / model），看不出「这批能不能和上批比」 | `OpenJudge.tsx` 只用 `summary.kpi/tracks`；`compare` 才有 `not_comparable` |

### 2.4 资产层（白皮书的「一套资产」）

| # | 现象 | 证据 |
|---|---|---|
| A1 | 用例没有「必过集 / 挑战集」分层，90 条同权 | `benchmark.Case` 无 `tier` 字段 |
| A2 | 线上问题无法回流成用例（只有本地覆盖层手工编辑） | `casestore` 只有 `save/delete/restore`，无「从批次转用例」 |
| A3 | 无巡检集，质量页也不接运行指标 | `/api/runs`、`/api/metrics` 在其它页；quality 不引用 |

### 2.5 与白皮书的对应关系

| 白皮书模块 | 现状 | 缺口 | 本 spec |
|---|---|---|---|
| 离线评测（回测门控） | 五赛道 + 阈值 + 覆盖率棘轮 + 批次对比 | 单次试验、不分层门禁、无必过/挑战集、校准不可达 | P0/P1/P4 |
| 在线评测与监控 | `/api/runs`、`/api/metrics`、健康分 | 与质量页割裂，无巡检 | 非本期（P4 留接口） |
| Case 挖掘与归因 | Trace/Spans/InputStages 齐全 | 归因分类未进 summary、无回流入口 | P2/P4 |
| 观测基建 | 强：run/span/input_stage 全链路 | —— | 不动 |

## 3. 根因收敛

1. **一个 verdict 背了三种事实**：「模型好不好」（效果）、「证据齐不齐」（校准/裁判）、
   「测全了没有」（范围）。三者混在一起，只能取最悲观值，于是永远 insufficient。
2. **「没证据」被当成「坏消息」**：缺裁判均分、缺校准记录都直接降级，而不是「该项不参与判定」。
   结果是规则门禁这条真正能拦住问题的线，被证据缺失一起拖死。
3. **前端把批次快照当体系状态**：批次是「某次、某些赛道、某个模型」的记录，
   页面却用一张聚合曲线、一条阈值线、一个「共 N 批」去表达它。

## 4. 目标与非目标

### 目标

1. 任何一次评测的结论都能回答三件事：**判了什么、没判什么、凭什么**。
2. 规则门禁永远有效：缺裁判/缺校准只让「质量结论」降级为 advisory，不让整批作废。
3. 前端不再输出无法自证的数字：每个聚合数都能下钻到用例、每条失败都能落到原因。
4. 统计口径能区分「真变化」与「抖动」：多次试验 + 区间/检验，替换 `1/n` 经验值。

### 非目标

1. 不做 AB / 影子流量（需要线上分流基建，本期只留数据接口）。
2. 不重做观测链路（trace/span 已足够）。
3. 不引入新评测框架（仍走 `openjudge` 一条流水线）。
4. 不追求「所有维度都到 L3」，只补齐最短的三块板：结论语义、归因、资产分层。

## 5. 方案

### 5.1 判决语义：拆成 `scope` + `causes` + `evidence`

新增三个正交维度，替代现在的一个 `verdict`：

```jsonc
{
  "verdict": "pass | fail | partial | unvalidated | insufficient",
  "scope": {
    "mode": "single_track | selected | full",
    "measured_tracks": ["A"],
    "unmeasured_tracks": ["B", "C", "D", "E"]
  },
  "causes": [
    {"code": "judge_uncalibrated", "message": "裁判未校准，质量分仅供参考", "severity": "advisory"},
    {"code": "judge_missing", "message": "本批未跑语义裁判", "severity": "advisory"},
    {"code": "sample_short", "message": "A 赛道 3 条，不足 10 条", "severity": "blocking"}
  ]
}
```

判定规则（替换 `report._verdict` / `_overall_verdict`）：

1. **只在 `measured_tracks` 内判**。未测赛道不产生 insufficient，只进 `unmeasured_tracks`。
2. `sample_short`（低于 `min_n`）是 blocking：该赛道 `insufficient`。
3. `judge_missing` / `judge_uncalibrated` 是 advisory：裁判不参与否决，赛道按规则判定；
   若该赛道**规则全过**，verdict 记 `unvalidated`（不是 pass，也不是 insufficient）。
4. 单赛道/选例批次：没有 blocking 时总判 `partial`，明确表示「局部结论，不能当发布门禁」。
5. 只有 `mode=full` 且无 blocking、无 advisory 时才能 `pass`。

配套改动：

- `report.TRACK_RULES` 增加 `judge_gate: "blocking | advisory | off"`，由校准状态在运行时决定
  （不再用 `judge_gate=False` 把裁判彻底摘掉，见 `run.py:138`）。
- `grade._finish` 修正 E4：`judge_norm is None` 且该用例**声明了** judge 时，不得计为 passed，
  记 `judge_missing`（`grade.py:388-393`）。
- `store.health` 改为复用 `/calibration` 的计算，删掉写死的 `calibration: None`（修 F8）。

### 5.2 多次试验与统计口径

- `RunOptions.trials: int = 1`，上限 3；`oj_runs.trials` 落库；`oj_cases` 增 `trial` 列。
- 指标口径写进 `summary.kpi`：
  - `pass_at_k`：k 次里至少一次通过（稳定性上界）
  - `pass_hat_k`：k 次全过（稳定性下界）
  - 默认页面展示 `pass_at_k`，并同时给 `pass_hat_k`；`trials=1` 时两者相等，不额外显示。
- 置信：`wilson_interval(passed, n, 0.95)` 进 `summary.kpi.ci`；
  `compare` 用两比例 z 检验（`two_proportion_p`），删掉 `precisionLimit`。
- 成本：`cost_usd` 改为「配置了单价才算」，否则 `null` + `cost_note: "未配置单价"`；
  绝不写 0.0 冒充「免费」（修 E9）。

### 5.3 裁判校准分档

现在的二值门禁（`n≥100 && ρ≥0.7`）让 pass 在现实中不可达。改为三档：

| 档位 | 条件 | 裁判作用 | 页面文案 |
|---|---|---|---|
| `off` | 无记录 | 只展示分数，不参与 verdict（规则仍需判） | 「裁判未参与门禁」 |
| `soft` | `n ≥ 30 && ρ ≥ 0.60` | 可把赛道从 pass 降为 `unvalidated`，不能判 fail | 「裁判软门禁（n=.., ρ=..）」 |
| `hard` | `n ≥ 100 && ρ ≥ 0.70` | 可判 fail（现行行为） | 「裁判已校准」 |

- `calibration_gate` 返回 `level: off|soft|hard`，保留 `valid` 兼容旧调用。
- `store.health` 与 `/calibration` 同时下发 `level / n / rho / progress`。
- UI 在「评分流程」里显示校准进度条（`n/30`、`n/100`），把「怎么才能让门禁生效」讲清楚。

### 5.4 归因：失败必须可落到原因

- `failures[]` 改为所有 `passed=false` 的用例，字段：
  `case_id / track / status / failed_checks[]（规则）/ judge_dims[]（低于 PASS_JUDGE 的裁判维度）/
  failure_type / repro / trace_ref`。
- `pareto` 拆两组：`pareto: {rule: [...], judge: [...]}`；前端用 tabs 切换。
- 截断显式化：`truncated: {failures: bool, pareto: bool, total_failures: int}`。
- 过程指标 `stages`（白皮书的「过程评测集」，按赛道下钻）：
  `tool_call_success_rate / tool_param_ok_rate / avg_steps / retry_rate / timeout_rate /
  confirm_rate`。数据源已有（`trace.steps`），只是没汇总。

### 5.5 前端信息架构

1. **视图进 URL**：`?view=overview|batches|scoreflow|studio`，删除 `focus` 隐藏入口；
   `Quality.tsx` 由 URL 驱动（修 F5，同时修掉「刷新跳视图」）。
2. **批次分页**：`/batches` 返回真实 `COUNT(*)`；批次条支持「加载更早」（修 F2）。
3. **批次身份卡**：批次头固定展示 `#id · 时间（本地 + title 显示 UTC）· 模型 · suite_sha 前 8 位`，
   与上一批任一指纹不同则打 `not_comparable` 徽标（修 F9、F6）。
4. **赛道表固定五行**：未测显示「未测」并计入 `scope.unmeasured_tracks`，不再凭空消失（修 F7）。
5. **概览去掉单条阈值线**：改为「每赛道门禁矩阵」（规则 / 裁判 / 校准三列 + 证据状态），
   聚合通过率只给趋势，不做阈值断言（修 F1）。
6. **失败下钻带上来源**：`inspect(scope)` 按点击的门禁过滤失败用例，
   而不是永远第一条；`locateFailure` 用分页参数取数，不用 `limit: 500`（修 F3、F4）。

### 5.6 资产：必过集 / 挑战集与回流

- `Case` 增 `tier: must_pass | challenge`（默认 `must_pass`，仓库题库批量标注一次）。
- `report` 按 tier 分别给 verdict：`must_pass` 一票否决，`challenge` 只进趋势与挑战分。
- 「从批次转用例」：`POST /api/openjudge/cases/from-batch/{batch_id}/{case_row_id}`，
  读取 trace 的输入与期望，生成覆盖层条目并强制人工确认（写 `reviewer`）。
- 巡检：只留接口约定（`/api/openjudge/patrol` 返回「最近 N 批关键赛道通过率」），
  本期不做调度。

## 6. 数据契约变更（向后兼容）

| 字段 | 变更 | 兼容策略 |
|---|---|---|
| `summary.verdict` | 新增 `partial` / `unvalidated` | 前端 `labels.ts` 补文案；CI 退出码按「非 pass 即 1」不变 |
| `summary.scope` / `causes` | 新增 | 旧批次缺失时前端回退到当前行为（显示 insufficient） |
| `summary.kpi.ci` / `pass_hat_k` / `cost_note` | 新增 | `trials=1` 时前端隐藏 |
| `summary.pareto` | 结构从数组改为 `{rule, judge}` | 旧批次数组时前端做一次形状判断 |
| `health.calibration` | 从 `null` 改为对象 | 现有前端已判空，安全 |
| `Case.tier` | 新增 | 旧 JSONL 无该字段时按 `must_pass` |

## 7. 分期与验收

每期一个 PR，按 2.4 的流程走；每期都必须有可复现断言（不是「看起来对了」）。

### P0（必须先做）：判决语义

- 改 `report._verdict/_overall_verdict/_apply_uncalibrated`、`grade._finish`、`store.health`。
- 验收（写成测试）：
  1. 单赛道 10 条全过 → `partial`，`scope.unmeasured_tracks` 含 B–E；
  2. 全 90 条全过 + 无校准 → `unvalidated`（不是 insufficient、不是 pass）；
  3. 全 90 条全过 + hard 校准 → `pass`；
  4. 裁判未跑 + 规则全过 → `unvalidated`，且该用例不进 `kpi.passed`；
  5. 规则失败 → 仍 `fail`。

### P1：多次试验与统计

- `RunOptions.trials`、`oj_cases.trial`、`pass_at_k/pass_hat_k`、Wilson 区间、compare z 检验。
- 验收：k=3 的抖动样例给出 `pass_at_k > pass_hat_k`；`compare` 在 n 小且 delta 小时判「不显著」。

### P2：归因与过程指标

- `failures` 合并 rule/judge；`pareto` 分组；`stages`；`truncated`。
- 验收：裁判 0.2 分的用例出现在 `failures` 且 `judge_dims=["correctness"]`；Pareto 有对应条目。

### P3：前端信息架构

- URL 视图、批次分页、批次身份卡、五赛道固定行、门禁矩阵、失败下钻、本地时间。
- 验收（组件测试 + 手测清单）：F1–F9 每条一个断言；`?view=` 刷新后视图一致。

### P4：资产与回流

- `tier`、分层 verdict、`from-batch`、`patrol` 契约。
- 验收：`must_pass` 一条失败即批次 fail；`challenge` 失败只降挑战分。

## 8. 风险

| 风险 | 处理 |
|---|---|
| verdict 枚举扩展影响 CI/夜间流水线 | 退出码规则保持「非 pass 即 1」；新增态先只改展示，再改退出码 |
| `trials` 使成本 ×k | 页面默认 1，上限 3；CI 显式 k=3 并在报告标 k |
| 旧批次 summary 结构不一致 | 前端做字段存在性判断；不做数据迁移（批次是历史快照） |
| 校准标注没人做 | soft 档（n≥30）先让裁判有话语权；UI 显示进度，不假装已校准 |
| 「partial 也能显示绿色」被误读 | `partial` 配色用中性蓝，文案固定「局部结论，不作发布门禁」 |

## 9. 开放问题（需要拍板）

| # | 问题 | 建议默认 | 影响 |
|---|---|---|---|
| 1 | `partial` 批次能否作为发布门禁 | **不能**，只作局部验证 | 决定 CI 是否接受单赛道回测 |
| 2 | `trials` 默认值 | 页面 1，CI 3 | 成本与稳定性权衡 |
| 3 | 校准 soft 档 `n≥30 / ρ≥0.6` 是否可接受 | 接受 | 否则裁判仍无法参与 |
| 4 | 是否本期就做「必过集/挑战集」 | 放 P4 | 题库要重新标一遍，工作量大 |
| 5 | `/health` 是否直接删掉 `calibration` 字段 | 改为复用 `/calibration` | 避免两处口径 |
