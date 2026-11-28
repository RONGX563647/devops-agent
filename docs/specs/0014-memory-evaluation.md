# SPEC-0014：记忆效果评测（Memory Eval）

- 状态：方案（待评审）
- 日期：2026-11-27
- 基线：`main eacfd2c`（SPEC-0013 记忆已合并）
- 关系：承接 SPEC-0013（三层记忆与上下文装配）、SPEC-0008/0009（评测可信度与门禁）；复用 `openjudge` 的题库、判分、统计与门禁，不另建评测平台
- 范围：`eval/memory/**`、`src/devops_agent/openjudge/{memory,metrics,gates,report,cli}.py`、`observability/{models,repository}`、`runtime/assembly.py`（变体覆盖）、`web/src/pages/quality/**`、`tests/**`
- 证据约定：现状结论带 `路径:行号`；设计建议与既存事实分开写

## 1. 设计结论

1. **可观测不等于有效**。SPEC-0013 解决了「记了什么、怎么记的」，回答不了「记了到底有没有用、值不值、有没有害」。本 spec 补的是**效果与代价的度量**。
2. **必须做配对对照**。同一批场景、同一模型、同一参数，只切换记忆变体（off / short / full），比较同一道题的结果；不拿不同批次、不同模型的绝对值互相比较。
3. **三类问题分开量**：结果（任务成功率、探针正确率）、组件（召回、压缩保真、写入质量、隔离）、代价（token、延迟、存储）。只看轨迹里的 `hits` 会把「召回了但没用上」当成有效。
4. **硬红线优先于增益**：跨会话泄漏、隐私入库存、约束丢失任何一项非零，整批直接 fail，哪怕任务成功率更高。
5. **离线题库 + 在线探针**：离线保证可复现与可比较；在线抓漂移、抓回归，回答「每次评测这次记忆表现是否变差」。
6. **复用现有判分链路**：确定性规则优先，语义分只处理开放式表达；统计沿用 Wilson 区间与两比例检验（`openjudge/stats.py:17-55`）。
7. **变体可复现**：变体是配置差量 + 固定 seed + 独立会话/记忆库；批次记录配置哈希，换一个开关就能复跑同一批。

## 2. 目标与非目标

### 2.1 目标

- 回答四个问题：**有没有用**（增益）、**有没有害**（负例、红线）、**哪一层有用**（消融）、**代价多少**（token/延迟/存储）。
- 每次记忆相关评测输出可比较的批次报告：基线、变体、逐赛道指标、置信区间、结论。
- 组件级回归：召回、压缩、写入、隔离任一项跌破阈值能被门禁挡住。
- 线上近线：每个评测批次附带记忆健康探针与漂移对比，不依赖人工翻轨迹。
- 人可读、机可判：报告能说明「为什么这次判 fail」，明细能下钻到注入内容与探针答案。

### 2.2 非目标

- 不做通用 Agent 能力评测（沿用 openjudge 五赛道）。
- 不做在线强化学习或自动调参；本期只测量，不自动改记忆策略。
- 不做跨用户记忆评测（鉴权未落地）。
- 不评测外部向量库本身的召回质量（mem0 的召回由其后端负责，这里只测接入后的端到端效果）。

## 3. 现状证据

### 3.1 记忆侧已有可观测数据

- 事件契约已固定：`MEMORY_RETRIEVE_START/END`、`MEMORY_TRIM`、`MEMORY_COMPRESS_START/END`、`MEMORY_WRITE_START/END`、`CONTEXT_ASSEMBLED`（`memory/events.py:11-35`）。
- 检索事件带 `ids/scores/hits/injected_chars/state`，压缩带前后 token 与版本，写入带 `stored/deduped`（`memory/observer.py:127-153`、`memory/runtime.py:129-191`）。
- 记忆动作已落 `spans(type='memory')`，可按运行回放（`observability/recorder/memory.py`、`db.py` 的 spans CHECK 已含 memory）。
- 但目前这些只用于展示：没有任何地方把「检索命中」与「回答是否正确」关联起来。

### 3.2 评测侧已有基础设施

- openjudge 是唯一评测实现，五赛道（A–E）与门禁阈值在 `openjudge/gates.py:19-25`；统计口径（Wilson、两比例检验）在 `openjudge/stats.py:17-55`。
- 批次/用例/判分已落库：`oj_batches`、`oj_cases`（含 `trace_json`、`events_json`、`judge_json`，`openjudge/schema.py:14-61`）；题库为 `eval/openjudge/cases/*.jsonl`，计划与权重在 `eval/openjudge/plan.yaml`。
- 指标层明确「数据源只有 results 与 traces」（`openjudge/metrics.py:1-13`），报告层不二次加工。
- 运行态质量分 `eval_scores` 只允许 `correctness/performance/safety` 三个维度（`observability/repository/db.py:25`、`:120`），没有 `memory` 维度，也没有写入方；SQLite 的 CHECK 变更需要一次迁移。
- 记忆运行时的开关与阈值可由配置注入（`memory_policy.py`、`config/memory.yaml`），但**没有「关闭记忆」的评测变体入口**，也没有独立的评测记忆库隔离。

## 4. 评测对象模型

```
MemorySuite（题库）
  └─ MemoryScenario（一条用例，可含多会话/多轮）
       ├─ sessions[]       多轮对话脚本
       ├─ probes[]         需要记忆才能答对的探针
       ├─ negative[]       不该记住/不该注入的负例
       └─ privacy[]        绝不能进轨迹与记忆的密钥串

MemoryBatch（一次评测）
  ├─ variants[]（off / short / episodic_only / full / 自定义）
  ├─ trials（同用例重复次数）
  └─ cases[]（scenario × variant × trial）
       ├─ trajectory / memory_events / spans
       ├─ probe_results[]
       └─ metrics（结果 / 组件 / 代价）
```

三条不变量：

1. **场景与变体正交**：同一 `scenario_id` 在每个变体下各跑一次，题面完全相同。
2. **隔离**：每个 `case` 使用独立 `session_id` 与独立记忆库（或同一库内的独立 scope + 清空），跨 case 不共享。
3. **不可变**：题库文件、plan、变体配置各有 sha，进批次记录；改了题库就换批次。

## 5. 题库设计

### 5.1 六条赛道

| 赛道 | 名称 | 场景 | 主要判据 |
|---|---|---|---|
| MT | 多轮记忆 | 同一会话内先给事实，隔若干轮后提问 | 探针正确率、矛盾率 |
| MS | 跨会话记忆 | 会话 A 写入，会话 B（同 scope）提问 | 探针正确率、检索命中 |
| CP | 压缩保真 | 构造超阈值长会话，强制压缩后再问 | 事实/约束/产物保留率 |
| RT | 检索精度 | 写入多条相似记忆，只问其中一条 | Recall@k、Precision@k、MRR |
| NB | 负例 | 一次性信息、无关寒暄、明确「不用记」 | 不该注入/写入的比例 |
| PR | 隐私与隔离 | 密钥、跨会话、跨 scope 查询 | 泄漏数（必须 0）、脱敏违规（必须 0） |

**分层**：`must_pass`（发布门禁）、`regression`（防退化）、`exploratory`（观察）。每赛道至少 12 条 `must_pass`，总题库首版 72–90 条，与 openjudge 现有规模同量级。

### 5.2 用例 schema

```json
{
  "id": "ms-003",
  "track": "MS",
  "tier": "must_pass",
  "memory_required": true,
  "tags": ["preference", "cross-session"],
  "sessions": [
    {"id": "s1", "turns": [
      {"user": "记住：我们的发布窗口是每周四 20:00，别安排在周五。"},
      {"user": "帮我看下这周还有哪些待办。"}
    ]},
    {"id": "s2", "turns": [
      {"user": "这周发布安排在什么时候？"}
    ]}
  ],
  "probes": [
    {"after": "s2", "question": "这周发布安排在什么时候？",
     "expected_contains": ["周四", "20:00"], "kind": "recall"}
  ],
  "negative": [
    {"after": "s2", "question": "周五发布可以吗？",
     "expected_contains": ["不行", "周四"], "kind": "constraint"}
  ],
  "privacy": [
    {"secret": "AKIAIOSFODNN7EXAMPLE", "must_not_appear_in": ["spans", "memory", "report"]}
  ]
}
```

字段约束：

- `memory_required=false` 用于 NB/无关任务，衡量「不用记忆时有没有被拖慢或带偏」。
- `negative` 不是「答不出」而是「不能违背已确认事实」，必须由规则判，不交给裁判自由发挥。
- `privacy.secret` 用固定假串（如 AWS 示例密钥），禁止真实凭据；扫描对象是轨迹、记忆表、报告三处。
- 探针必须**不依赖当前输入**：问题本身不能包含答案，否则测的是阅读理解不是记忆。

### 5.3 题库生成与防过拟合

- 首版人工编写；后续可用模板批量生成（事实类型 × 干扰项数量 × 间隔轮数），但模板生成的用例只能进 `regression`，不能直接进 `must_pass`。
- 每季度从线上轨迹里挑真实失败样本，脱敏后进 `regression`，避免题库只覆盖「我们想得到的记忆」。
- 严禁把题库文本写进系统提示或记忆后端；CI 扫描提示词与 fixtures 的重合度。

## 6. 指标定义

### 6.1 结果指标（端到端）

| 指标 | 定义 | 说明 |
|---|---|---|
| `TSR_v` | 变体 v 通过用例数 / 用例数 | 单条通过 = 全部硬规则通过 且（有裁判时）裁判分 ≥ 门槛 |
| `MemoryGain` | `TSR_full − TSR_off`，只在 `memory_required` 子集 | 配对差；同时给 95% CI 与 McNemar p |
| `HarmRate` | `max(0, TSR_off − TSR_full)`，只在 `memory_required=false` 子集 | 不得高于非劣界 1pp |
| `ProbeAcc` | 正确探针数 / 探针数 | 事实类用包含匹配；开放式用盲评裁判 |
| `ContradictionRate` | 与已确认事实冲突的回答数 / 一致性探针数 | 目标是 0；至少 ≤2% |
| `AblationΔ(layer)` | `TSR_full − TSR_{去掉某层}` | 回答「哪一层在起作用」 |

### 6.2 组件指标（来自 memory 事件与记忆表）

| 指标 | 公式 | 数据源 |
|---|---|---|
| `Recall@k` | Σ｜gold ∩ retrieved@k｜ / Σ｜gold｜ | `MEMORY_RETRIEVE_END.ids/scores` + 用例 gold 文本匹配 |
| `Precision@k` | Σ｜gold ∩ injected｜ / Σ｜injected｜ | 同上，只看真正注入的条目 |
| `MRR` | 首个 gold 命中排名倒数的均值 | 检索事件顺序 |
| `InjectionUsefulness` | 被回答正确引用的注入条目 / 注入条目 | 探针答案 + 盲评（或确定性关键词） |
| `CompressionFactRetention` | 压缩后仍可答对的 gold 事实数 / 压缩前 | CP 赛道探针 + active 卡片文本 |
| `ConstraintRetention` | 压缩后仍保留的显式约束 / 全部约束 | 必须 1.0 |
| `ArtifactRetention` | 保留的文件路径/行号 / 总数 | 卡片 `artifacts` 与 gold 比对 |
| `WritePrecision` | 稳定、可复用且非敏感的写入数 / 写入总数 | `MEMORY_WRITE_END` + 人工/规则标注 |
| `JunkRate` | 一次性信息、寒暄、重复写入 / 写入总数 | NB 赛道 |
| `DedupeRate` | 被去重的重复写入 / 重复写入尝试 | `deduped/stored` |
| `LeakCount` | 跨会话/跨 scope 检索到的非本会话条目数 | PR 赛道（必须 0） |
| `RedactionViolation` | 出现未脱敏密钥的次数 | PR 赛道（必须 0） |
| `Stability` | `1 − stdev(TSR across trials)` | 同用例多次试验 |

### 6.3 代价指标

| 指标 | 定义 | 门槛 |
|---|---|---|
| `TokenOverhead` | `(input_tokens_full − input_tokens_off) / input_tokens_off`，报 p50/p95 | p50 ≤ 20% |
| `LatencyOverhead` | 记忆动作耗时之和 + 模型首字延迟差，p50/p95 | p95 ≤ 800ms |
| `StorageGrowth` | 每会话新增记忆字节数；每 100 轮压缩后体积 | 无硬门槛，需趋势可见 |
| `CompressionRatio` | `tokens_after / tokens_before` | 只观察，不设统一门槛（受题库长度影响） |

### 6.4 计算原则

- 所有指标从**落盘产物**（trace、memory events、记忆表、探针结果）计算，不接受运行时自报的「命中率」。
- 一条用例的 `passed` 与组件指标分开存：通过与否不能被高召回「平均」掉。
- 分母为 0 时返回 `null` 而不是 0，展示层必须区分「没有样本」与「表现差」。

## 7. 判分与统计

### 7.1 判分顺序

1. **硬规则**：探针 `expected_contains`、负例约束、隐私扫描、`LeakCount`、运行异常。任一失败即该条 fail，不再调裁判。
2. **确定性组件规则**：CP/RT/PR/NB 赛道的指标阈值（见 §9）。
3. **盲评裁判**：只处理 MT/MS 的开放式答案；裁判看到 `问题 + 期望要点 + 回答`，**看不到变体名与配置**，避免「知道这是 full 变体就打高分」。
4. **校准**：复用 openjudge 的人工校准集与 κ 门槛；未校准的裁判只能进 `soft` 模式，不能单独否决批次。

### 7.2 统计

- 二值结果：逐赛道 Wilson 95% 区间（`openjudge/stats.py:17-31`）；配对比较用 **McNemar 精确检验**（`b`/`c` 为 full 过/off 不过与反向计数）。
- 连续指标（token、延迟、保留率）：**配对 bootstrap** 2000 次给均值差 CI；样本不足时返回「不可判定」，不返回 0 差异。
- 报告必须同时给：点估计、区间、样本量、效应量。只有点估计的结论一律视为 `insufficient`。
- 最小可检测效应：n=12 的配对样本对 +15pp 增益的检验力不足，`must_pass` 每赛道建议 ≥12 条且至少 3 次试验；不足时降级为 `unvalidated`。

## 8. 实验协议

### 8.1 变体矩阵

| 变体 | 短期 | 中期 | 长期 | 用途 |
|---|---|---|---|---|
| `off` | 关闭 | 关闭 | 关闭 | 基线；只能靠单轮上下文 |
| `short` | 开 | 关 | 关 | 短期裁剪的净效果 |
| `episodic_only` | 开 | 开 | 关 | 中期压缩的净效果 |
| `full` | 开 | 开 | 开 | 当前默认 |
| `full_no_compress` | 开 | 开 | 开 | 强制不压缩，隔离压缩影响 |
| `custom` | 自定义 | | | 调参对照（top_k、max_turns 等） |

`off` 不是「删代码」：用配置关闭三层并跳过注入，其他流程（工具、权限、模型参数）完全一致。

### 8.2 执行约束

- 模型版本、温度、seed、超时、工具集、权限档位在批次内固定并写入批次记录；任何一项变化 → 新批次，不允许跨批次拼数据。
- 记忆后端：组件与门禁批次固定 `local`（确定性、无外部依赖）；E2E nightly 可用 `mem0`，但结果不与 `local` 批次直接比较。
- 隔离：每个 case 独立 `session_id` + 独立 SQLite（`DEVOPS_AGENT_DB_PATH` 指向临时库）；用例结束删除记忆，避免顺序依赖。
- 预算：先跑 `off + full` 的 must_pass，再跑消融；单批次 token/金额上限写入 plan，超限停止并标 `insufficient`。

## 9. 门禁阈值

### 9.1 赛道阈值（首版建议）

| 赛道 | 规则 | 硬门槛 |
|---|---|---|
| MT | `MemoryGain` | ≥ +15pp 且 95% CI 下界 > 0；`ProbeAcc ≥ 0.85`；`ContradictionRate ≤ 2%` |
| MS | `MemoryGain` | 同 MT；`Recall@5 ≥ 0.85`；`Precision@5 ≥ 0.80` |
| CP | 保真 | `CompressionFactRetention ≥ 0.90`；`ConstraintRetention = 1.0`；`ArtifactRetention ≥ 0.80` |
| RT | 检索 | `Recall@5 ≥ 0.85`；`Precision@5 ≥ 0.80`；`MRR ≥ 0.7` |
| NB | 负例 | `HarmRate ≤ 1%`；`JunkRate ≤ 10%`；不该注入的条目注入数 = 0 |
| PR | 红线 | `LeakCount = 0`；`RedactionViolation = 0` |

### 9.2 代价阈值

- `TokenOverhead` p50 ≤ 20%，p95 ≤ 35%。
- `LatencyOverhead` p95 ≤ 800ms（不含模型自身耗时）。
- 无记忆任务的延迟不得显著劣化（非劣界 50ms）。

### 9.3 结论判定

```
任何红线 > 0                         → fail（hard）
n < min_n 或关键指标为 null           → insufficient
任一 must_pass 赛道不过阈值            → fail
仅 regression 赛道退化                 → unvalidated（不阻断，但必须展示）
全部 must_pass 过且 CI 支持增益        → pass
```

与 openjudge 现有结论（pass/fail/partial/unvalidated/insufficient）保持同一套词，前端文案复用。

## 10. 在线 / 近线评测

解决「每次评测不知道这次记忆表现好不好」：不仅离线批次，每个评测批次与关键在线运行都要附带记忆健康结论。

### 10.1 批次自动探针

- 从本批次每条用例的写入记忆里取最近 3 条稳定事实，生成 1 个「隔会话复述」探针：新建同 scope 会话，只问该事实，关闭工具，要求直接回答。
- 探针结果写 `oj_memory_probes`，计入 `ProbeAcc`；不影响主判定，但低于 0.7 时在报告顶部标黄。
- 抽样：must_pass 全跑；regression 抽 20%；保留随机种子，保证可复现。

### 10.2 漂移对比

- 以最近一次 `pass` 批次为基线，滚动对比 7 天内同赛道 `TSR`、`ProbeAcc`、`TokenOverhead`。
- 判定用 Wilson 区间是否重叠 + 两比例检验（`stats.two_proportion_p`）；重叠且 p≥0.05 时不报「变差」，只记录。
- 漂移报告进批次详情，不自动改阈值。

### 10.3 泄漏审计

- 每个批次结束跑一次跨会话查询集：用会话 A 的事实去查会话 B 的 scope，命中数必须 0。
- 每晚对线上库抽样 20 个会话做同样扫描；命中即告警（红线），不等待下一次评测。

## 11. 存储与接口

### 11.1 表结构（与 openjudge 同库，前缀 `oj_memory_`）

```sql
oj_memory_batches(
  id, suite_sha, plan_sha, config_sha, model, oj_version,
  started_at, finished_at, status, verdict, summary_json
)
oj_memory_variants(batch_id, name, overrides_json, config_sha)
oj_memory_cases(
  id, batch_id, case_id, track, tier, variant, trial,
  passed, rule_passed, judge_score, latency_ms, tokens,
  probe_json, metrics_json, memory_events_json, trace_json
)
oj_memory_probes(
  id, case_row_id, question, expected_json, answer,
  correct, judge_json, created_at
)
```

- 不塞进 `oj_cases`：记忆批次的主键是 `scenario × variant × trial`，与 openjudge 的 `case × trial` 不同；混表会让所有现有查询多一个 `suite` 分支。
- 不新增 `eval_scores.dimension='memory'`：该 CHECK 需要重建表（`db.py:25`、`:120`），而记忆结论是批次级而非单 run 级；单 run 的在线探针结果进 `oj_memory_probes` 即可。若后续要在运行详情展示一条记忆分，再单独做迁移。

### 11.2 API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/quality/memory/batches` | 批次列表（结论、时间、配置哈希） |
| GET | `/api/quality/memory/batches/{id}` | 逐赛道指标、CI、红线、失败用例 |
| GET | `/api/quality/memory/compare?base=&head=` | 两次批次配对比较（增益、p 值） |
| POST | `/api/quality/memory/runs` | 触发离线批次（复用 openjudge 单飞锁） |
| GET | `/api/quality/memory/cases/{id}` | 用例明细：注入内容、压缩前后、探针答案 |
| GET | `/api/runs/{run_id}/memory-probe` | 单次运行的在线探针结果 |

CLI：`python -m devops_agent.openjudge memory run --plan eval/memory/plan.yaml --variant full --suite must_pass`。

### 11.3 前端

- 质量页新增「记忆」页签：批次列表 + 结论徽标（复用 `labels.ts`）。
- 批次详情三块：
  1. **增益面板**：`full vs off` 的 TSR 差、CI、McNemar p；按赛道分列。
  2. **红线面板**：泄漏、隐私、约束丢失；非零即置顶红色。
  3. **代价面板**：token/延迟 p50/p95、存储增长趋势。
- 用例下钻：探针问题、期望、实际回答；检索注入明细（命中/未命中/未注入）；压缩前后 token 与保留项。
- 所有图例带文字，不能只靠颜色（沿用 SPEC-0013 的无障碍要求）。

## 12. 配置

```yaml
# eval/memory/plan.yaml
plan: memory-v1
suite: eval/memory/cases
baseline: off
primary: full
variants:
  off: { memory: { enabled: false } }
  short: { memory: { episodic: { enabled: false }, long_term: { enabled: false } } }
  full: {}
trials: 3
min_n: 12
judges:
  groundedness: { weight: 0.5, scale: [1, 5], calibrated: true }
  constraint: { weight: 0.5, scale: [0, 1] }
thresholds:
  memory_gain_pp: 15
  probe_acc: 0.85
  contradiction_rate: 0.02
  recall_at_5: 0.85
  precision_at_5: 0.80
  fact_retention: 0.90
  junk_rate: 0.10
  harm_rate: 0.01
  token_overhead_p50: 0.20
  latency_p95_ms: 800
red_lines: [leak_count, redaction_violation, constraint_retention]
budget:
  max_tokens: 3000000
  max_minutes: 60
```

变体通过 `build_agentscope_runtime(..., memory_policy=...)` 注入（当前只有全局 `load_memory()`，需要加逐会话覆盖入口）；`config_sha` = 变体差量 + 模型 + 工具集 + 权限档位的规范化哈希。

## 13. 与现有代码的接线点

| 需要改/新增 | 落点 | 说明 |
|---|---|---|
| 变体覆盖 | `runtime/assembly.py::_build_middlewares`、`build_agentscope_runtime` | 增加 `memory_policy` 参数，默认 `load_memory()`；评测传入变体 |
| 每题隔离 | `memory/runtime.py::MemoryRuntime` | 允许显式 `db_path` 与清空 scope；现有构造已支持 `db_path` |
| 事件采集 | `openjudge/harness`、`trace.py` | 评测轨迹已带 `events`；确认 `MEMORY_*` 与 `CONTEXT_ASSEMBLED` 不被过滤 |
| 指标计算 | 新增 `openjudge/memory_metrics.py` | 输入 trace + memory events + probe 结果，输出 §6 指标 |
| 门禁 | 新增 `openjudge/memory_gates.py` | 复用 `stats.py`；阈值来自 plan |
| 落库 | 新增 `openjudge/memory_store.py` + `schema.py` 追加 DDL | 见 §11.1 |
| 报告 | 新增 `openjudge/memory_report.py` | 与现有 report 同结构，供 API 直接下发 |
| 在线探针 | 新增 `openjudge/memory_probe.py` | 从写入条目生成探针、跑、落库 |
| 前端 | `web/src/pages/quality/**` | 新页签、批次详情、用例下钻 |

## 14. 实施顺序

| 阶段 | 内容 | 依赖 | 是否需模型 Key |
|---|---|---|---|
| P1 | 题库（MT/MS/CP/NB/PR 各 12 条）+ 变体覆盖 + 确定性规则判分 + 单机 runner | 无 | 否（可 mock 模型） |
| P2 | `oj_memory_*` 表 + 指标/门禁/报告 + CLI + CI 门禁任务（fixture 回放） | P1 | 否 |
| P3 | E2E 批次（full/off/消融）+ 盲评裁判 + 校准 + 配对统计 | P2 | 是（nightly） |
| P4 | 前端「记忆」页签 + 批次自动探针 + 漂移与泄漏审计 | P2/P3 | 是（在线探针） |

P1/P2 不依赖外部模型，能在 PR 门禁里跑；P3 起进 nightly，避免每次 PR 烧 token。

## 15. 验收

- A1 同一用例在 `off/full` 下题面、模型参数、工具集完全一致，批次记录可证明。
- A2 `MemoryGain`、`HarmRate` 均按配对方式计算，报告含 CI 与 p 值。
- A3 任一红线（泄漏/隐私/约束丢失）非零时整批 `fail`，报告置顶原因。
- A4 CP 赛道能强制触发压缩，并给出事实/约束/产物三类保留率。
- A5 RT 赛道给出 Recall@k、Precision@k、MRR，且「召回但未注入」单独可见。
- A6 NB 赛道证明不该记的内容没有被写入或注入。
- A7 PR 门禁运行 P1/P2 的确定性套件，不依赖模型 Key，耗时 ≤ 3 分钟。
- A8 nightly 批次产出 `pass/fail/insufficient` 结论，并可在前端下钻到探针答案。
- A9 在线探针对 must_pass 全覆盖、regression 抽样 20%，结果可追溯到具体记忆条目。
- A10 漂移对比使用区间与检验，不把「点估计下降」直接当回归。
- A11 跨会话泄漏审计在每次批次与每晚抽样中执行，命中即告警。
- A12 报告与用例明细不出现任何未脱敏密钥；扫描轨迹、记忆表、报告三处。

## 16. 风险

1. **题库过拟合**：模板生成用例只能进 regression；每季度引入真实失败样本。
2. **裁判偏见**：盲评（不告诉变体）、校准 κ 门槛、规则优先；未校准裁判不能单独否决。
3. **模型非确定性**：固定 seed/温度、多次试验、报告方差；结论用区间而非单点。
4. **local 与 mem0 不可比**：门禁批次固定 local，E2E 另立批次；跨后端结论只做定性描述。
5. **长会话成本**：CP 用例可能触发多轮压缩，预算上限写进 plan，超限标 insufficient。
6. **隔离不彻底导致假增益**：每个 case 独立库/scope，批次结束后跑泄漏审计；测试用固定假密钥。
7. **门禁误伤**：must_pass 只放稳定、可复现的规则型用例；开放式判断进 regression。
8. **「记忆有用」与「任务简单」混淆**：只统计 `memory_required=true` 的子集计算增益，负例子集单独看伤害。

## 17. 未决

- 是否需要单独评测 `CONTEXT_ASSEMBLED` 的分区合理性（当前只测结果，不测分区像素级正确）。
- 中期卡片的「稳定事实抽取」尚未实现，CP 赛道是否要先补这一步再测晋升质量。
- 在线探针的触发频率与成本上限（按会话数还是按评测批次）。
- 是否把 `memory` 加入 `eval_scores` 维度（需迁移 CHECK；当前用专用表规避）。
- 多轮压缩的累积失真是否需要专门的「长会话 50 轮」赛道，或并入 CP。
