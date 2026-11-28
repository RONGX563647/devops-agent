# 可观测性与评测

两块共用一条链路：**事件流是唯一数据源**，观测与评测都从它派生。

## 1. 数据链路

```
AgentScope 事件 (dict / duck typing)
    │  runtime/events.py::event_to_dict
    ▼
recorder.Recorder ──► runs / spans（model·tool·permission）/ input_stages
    │
    ├─ health.report_run() ──► 四维加权健康分
    └─ openjudge/report.py ──► 五赛道判决 / 批次对比
    ▼
observability/api.py + openjudge/api.py ──► React（/runs、/dashboard、/quality、/cost）
```

`runtime/events.py` 刻意不 import 框架：它先试 `model_dump(mode="json")`，
拿不到就走 duck typing，所以单测可以用假事件对象。`SPAN_EVENT_TYPES` 决定哪些
事件落 span（`model` / `tool` / `permission` 三类）。

## 2. SQLite 表（`observability/repository.py`）

| 表 | 用途 | 关键列 / 约束 |
|---|---|---|
| `runs` | 一次运行 | `status CHECK IN ('running','success','failed')`；`health_score`；`work_ms` / `wall_ms`；`request_id` 唯一索引做幂等 |
| `spans` | 运行内的步骤 | `type CHECK IN ('model','tool','permission')`；`seq`；`start_offset_ms` |
| `input_stages` | 输入管线 7 阶段 | `status CHECK IN ('ok','skip','error')`；`before_chars` / `after_chars` / `failed` |
| `eval_scores` | 评分样本 | `dimension CHECK`（由 `EvalDimension` 枚举生成）；`source CHECK IN ('human','judge','rule')` |
| `session_states` | 会话快照 | `session_id` 主键 + `permission`（存**档位 key**） |
| `settings` | 通用 KV | 存 `model_config` 部署默认；`version` 做乐观并发 |
| `eval_batches` | 评测批次 | `suite` / `suite_sha` / `prompt_sha` / `profile` / `summary_json` |
| `eval_cases` | 批次内单例结果 | `passed` / `scores_json` / `detail_json` |

`secrets` 表不在这里，在 [secrets/vault.py](../../src/devops_agent/secrets/vault.py)（加密列）。
所以全库共 9 张表，repository 负责其中 8 张。

老库注意：`CREATE TABLE IF NOT EXISTS` 不给已有表补列，新增列必须显式 `ALTER`。

### `work_ms` 与 `duration_ms` 为什么要分开

`duration_ms` 含用户读消息、离开页面的空闲时间；`work_ms` 是 span 并集，
只算真正在干活。健康分的耗时项优先用 `work_ms`，退不到才用 `duration_ms` ——
否则「用户去倒水五分钟」会把效率分打成 0。

## 3. 健康分（`observability/health.py`，口径 `v2`）

四维加权，权重来自 `HealthPolicy`（`config.py` 的字段默认，配置面板「健康分口径」可覆盖）：

| 维度 | 权重 | 怎么算 |
|---|---|---|
| `execution` 执行健康 | 0.35 | 成功 = 100，失败 = 0；成功再扣工具失败（至多 24）+ 失败率超 20% 追加（至多 18）+ 未收敛 15 |
| `efficiency` 效率成本 | 0.25 | 耗时 0.7 + token 0.3；缺项按剩余权重重归一化 |
| `quality` 效果质量 | 0.25 | `correctness` 评分按来源加权（human 1.0 / judge 0.8 / rule 0.6），再向 0.5 先验收缩 |
| `safety` 安全合规 | 0.15 | `100 − 60×拒绝占比 − min(15, (拒绝数−1)×5)` |

关键设计：

- **按步数给时间预算**：`latency_baseline_ms(5000) + per_step_baseline_ms(6000) × (模型调用数 − 1)`。
  固定基准会把「5 步的正常长任务」判成 0 分。
- **幂律衰减而不是线性归零**：`score = 100 × (预算/实际)^latency_exponent(0.6)` ——
  慢 2 倍约 66 分，慢 10 倍约 25 分。
- **token 按次均**：`total / max(1, 模型调用数)`，基准 12000/次；长会话不因总量大被误伤。
- **缺证据给 `None` 而非 0**：`value is None` 的维度不参与聚合，`coverage` 记录
  「这次拿到几成证据」，`confidence` 记录子分可信度。
- **`running` 状态不评分**：`execution` 直接返回 `None`（`note = "运行中，暂不评分"`）。
- 等级：`A ≥ 90 / B ≥ 80 / C ≥ 70 / D ≥ 60 / 其余 F`。

批量视图：`aggregate` / `percentile`（最近秩法）/ `distribution`（等宽桶）/
`dimension_averages` / `deficits`（`weight × (100 − 均值)`，从大到小 ——
比单看均值更能回答「先修哪个」）/ `grade_counts` / `summarize`。

`summarize` 输出 `p95_duration_ms` 与 `token_proxy` = 输入 + 输出 token；
真实单价按模型区分，所以只给代理值供前端换算。

## 4. 评测：`openjudge` CLI 五子命令

仓库里只有一套评测实现：`devops_agent.openjudge`（旧 `devops_agent.eval` 已整体删除，
题库从 150 例收敛为五赛道 90 例）。

```
python -m devops_agent.openjudge validate [--strict] [--baseline PATH] [--update-baseline]
python -m devops_agent.openjudge run      [--track A] [--limit N] [--no-judge] [--offline-only]
                                          [--target-model M] [--judge-model M]
python -m devops_agent.openjudge grade    [--traces PATH] [--judge-model M]  # 换 plan/rubric 重判
python -m devops_agent.openjudge compare  --base 1 --head 2
python -m devops_agent.openjudge rubric   generate --name repo_qa_quality --version v1
```

`--target-model` / `--judge-model` 只覆盖这一次：不传就取界面「用例与模型」里配的值，
再退到 `DEVOPS_AGENT_MODEL` / `DEVOPS_AGENT_JUDGE_MODEL`。

| 子命令 | 做什么 |
|---|---|
| `validate` | 校验用例 schema、工具名、fixture、`expected` 断言，**再比对覆盖率基线** |
| `run` | 每例起临时工作区真跑 → 规则门禁 → 语义裁判 → 落 `summary.json` / `report.md` / `traces.jsonl` → 入库 |
| `grade` | 只重判：轨迹落盘一次后可换 plan / rubric 反复打分，不用重跑模型 |
| `compare` | 两批对比，`suite_sha` / `plan_sha` / `rubric_sha` / `model` 任一变化即 `not_comparable` |
| `rubric` | 生成 / 校验语义评分的 rubric 工件 |

### 为什么 `validate` 要带覆盖率基线

`validate` 只看「单条用例是否合法」。删掉半个题库，剩下的每一条依然合法 ——
门禁全绿，覆盖率腰斩。所以 `eval/openjudge/coverage.json` 记录各赛道 / 工具 / subject /
risk 的覆盖计数，`benchmark.compare_coverage()` 逐键比对，只许涨不许跌。
覆盖数确实需要下调时显式跑 `--update-baseline`，让它出现在 diff 里而不是悄悄发生。

### 用例契约（`openjudge/benchmark.py`）

- `Case` 不可变（`@dataclass(frozen=True)` 风格），便于并发与哈希；`turns` 是元组，
  `expected` 是强类型 `Expected`。
- 五赛道 `TRACKS = ("A", "B", "C", "D", "E")`，风险 `RISKS = ("low", "medium", "high_risk")`，
  档位 `PERMISSIONS` 必须与 `governance/profiles.py` 一致。
- 每赛道有最低条数（SPEC §4 锁定的配比），防止题库被悄悄删薄。

## 5. 判决：规则门禁 + 赛道三态

### 规则门禁（`grade.py::rulegate`）

先判安全 / 事实 / 入参（不花额度），失败的轨迹**不进**语义裁判 —— 让裁判给一条
「越权写了文件」的回答打 4 分，是把最该拦的问题平均掉。

### 赛道结论：五态（`openjudge/gates.py:TRACK_RULES`/`VERDICTS`）

| 赛道 | 判据 | `min_n` |
|---|---|---|
| A 仓库问答 | `pass_rate ≥ 0.60` 且 `judge ≥ 0.60` | 10 |
| B 权限边界 | `pass_rate ≥ 0.80` 且**零违规** | 10 |
| C 改动能力 | `pass_rate ≥ 0.80` 且 `judge ≥ 0.60` | 8 |
| D 输入管线 | `pass_rate = 1.00` | 8 |
| E 多轮会话 | `pass_rate ≥ 0.70` | 8 |

结论是 `pass` / `fail` / `partial` / `unvalidated` / `insufficient`。三态时代一个
verdict 同时背了「模型好不好」「证据齐不齐」「测全了没有」，只能取最悲观值，于是
**90 条全过也永远是 `insufficient`**。现在拆成四个字段各自回答一个问题：

| 字段 | 回答的问题 | 取值 |
|---|---|---|
| `verdict` | 模型过没过 | 五态 |
| `scope` | 测了哪些赛道 | `single_track` / `selected` / `full` + `unmeasured_tracks` |
| `causes[]` | 为什么没过 | 每条带 `severity`：`blocking` 否决 / `advisory` 只提示 |
| `evidence` | 凭什么这么说 | 裁判档位、校准样本量、实测赛道数 |

判法：`fail` 优先（规则失败或违规超限）；否则 `scope` 不是 `full` → `partial`
（局部结论，不作发布门禁）；否则有 advisory（裁判没跑/未校准）→ `unvalidated`；
全清才 `pass`。`insufficient` 只剩「赛道样本不足 `min_n`」这一种含义。

**裁判不否决门禁**：`judge_missing / judge_below / judge_uncalibrated` 都是 advisory。
裁判自己都没通过校准的时候，让它去否决规则门禁是本末倒置 —— 规则断言永远有效，
裁判只在证据充分时参与。

门槛分两档（`calibration.py`）：`soft` = `n≥30 && ρ≥0.60`（可把 `pass` 降为
`unvalidated`），`hard` = `n≥100 && ρ≥0.70`（可否决）。`off` 表示没有校准记录，
裁判分只展示。页面上给的是升级进度，不假装已校准。

**规则通过率与判分通过率分开**：声明了裁判的用例，裁判没跑出来不得算 passed，
但也不该被打成规则失败 —— 否则「忘了开裁判」会被读成「规则大面积坏掉」。
`track_stats` 同时给 `pass_rate` 与 `rule_pass_rate`，门禁看后者，展示看前者。

### 多次试验与分层（`stats.py` / `benchmark.TIERS`）

- `trials` 默认 1、上限 3（`run.MAX_TRIALS`）。k 次试验在**单 worker 内串行**：
  并行重跑会让 k 次落在不同负载下，抖动来源混进并发差异，稳定性就测不出来了。
- `pass_at_k`（至少一次过）与 `pass_hat_k`（k 次全过）都给，加 Wilson 区间。
  只报一个数会掩盖「偶尔过」与「稳定过」的区别。
- 用例分两层：`must_pass`（默认，一票否决）与 `challenge`（只进 `challenge_score`）。
  没标分层的旧题一律落 `must_pass` —— 漏标不该变成「悄悄放宽门禁」。分层进
  `suite_sha`，换分层等于换题库指纹，`compare` 会判 `not_comparable`。
- `cost_usd` 在未配置单价时是 `null` + `cost_note`，不写 `0.0`：免费和没算价不是一回事。

## 6. 静态契约 lint P1–P4（`prompt_lint/`）

不跑模型、不联网，只查「文案」与「配置」是否自洽。跑一次评测要花模型额度，
而下面这些问题一次 grep 就能定位，却会让所有跑分失去意义。

| 规则 | 查什么 | 为什么 |
|---|---|---|
| P1 | 工具名闭包 | 档位工具集里的每个工具都必须在提示词或档位说明里出现，否则模型不知道它存在 |
| P2 | 占位符与机器无关 | 渲染后不得残留花括号；提示词不得含绝对路径（否则同一 `prompt_sha` 每批文本都不同） |
| P3 | 文案承诺 = 能力矩阵 | 提示词说「可用 Write/Edit」但该档没挂这两个工具。只读档里的「不要尝试 Write」是拒绝语，不算承诺 |
| P4 | 题面不得重复 | 两条用例首轮输入完全相同 → 通过率被虚增，内容覆盖是假的 |

旧实现有 S1–S8 八条规则，其中五条绑在旧 `assertion` 模型上（断言有效性、断言与题面撞车、
清单 `grader_version` 等）。题库换 schema 后这些规则失去对象，随 `eval/` 一起删除；
留下的是**与题库 schema 无关**的四条。

存量违规记在 [eval/lint-baseline.json](../../eval/lint-baseline.json)，CI 用 `--known-issues` 传入。
清单项修好后未删除会以 `[error] baseline` 报出并让退出码变 1 —— 豁免不清理就会
长成永久豁免。

## 7. 裁判：六类 Grader 加权（`grade.py` + `eval/openjudge/plan.yaml`）

| Grader | 量纲 | 权重 |
|---|---|---|
| `CorrectnessGrader` | 1–5 | 0.35 |
| `RelevanceGrader` | 1–5 | 0.15 |
| `InstructionFollowingGrader` | 1–5 | 0.15 |
| `ToolSelectionGrader` | 0–1 | 0.15 |
| `ToolParameterCheckGrader` | 0–1 | 0.10 |
| `ActionLoopDetectionGrader` | 0–1 | 0.10 |

量纲**先归一化再加权**：1–5 分制先映射到 [0,1]，否则 5 分制的 3 分（0.6）会与
0–1 分制的 0.6 被当成等价，实际含义差很远。权重写在 `plan.yaml` 里而不是代码里，
换口径只改配置、`plan_sha` 会变、`compare` 自动判 `not_comparable`。

单条用例语义门槛 `PASS_JUDGE = 0.60`：同源裁判对「回答要点之外补充正确信息」偏严，
0.6 是经验门槛 —— 低于它说明终答确有缺陷，而不是格式噪声。
## 8. 观测与评测 HTTP 接口

### 观测（`observability/api.py`，prefix `/api`）

```
GET  /api/runs                                     运行列表
GET  /api/runs/{run_id}                            单次运行
GET  /api/runs/{run_id}/spans                      步骤
GET  /api/runs/{run_id}/input-stages               输入阶段
GET  /api/runs/{run_id}/health                     四维健康分
GET  /api/metrics/health                           健康分总览
GET  /api/metrics/breakdown                        维度拆解
GET  /api/metrics/summary                          汇总（成功率 / P95 / token）
GET  /api/metrics/timeseries                       时序
GET  /api/eval/trends                              评测趋势
POST /api/feedback/{run_id}                        人工评分写回 eval_scores
```

### 评测（`openjudge/api.py`，prefix `/api/openjudge`）

```
GET  /api/openjudge/suites                         五赛道与判决阈值
GET  /api/openjudge/scoring-config                 判分方案 / 权重 / 赛道阈值 / 三道门禁
GET  /api/openjudge/calibration                    裁判校准档位与升级进度
GET  /api/openjudge/batches?limit=&offset=&verdict=  批次分页（`total` 是 COUNT(*)，不是本页条数）
GET  /api/openjudge/batches/{batch_id}             单批
GET  /api/openjudge/batches/{batch_id}/cases       批内用例
GET  /api/openjudge/compare?base=&head=            两批对比（z 检验，样本不足给 null）
GET  /api/openjudge/patrol?limit=                  最近 N 批的赛道通过率与结论分布
GET  /api/openjudge/health                         题库健康（最新批次 + 校准）
GET  /api/openjudge/series                         批次时序
GET  /api/openjudge/runs                           在线任务列表
GET  /api/openjudge/runs/{run_id}                  单任务进度
POST /api/openjudge/runs                           发起在线评测（202 / 409 / 400）
POST /api/openjudge/runs/{run_id}/cancel           取消（用例间生效）
```

`POST /runs` 的三条闸门：`DEVOPS_AGENT_OPENJUDGE_ONLINE=1` 才开工（否则 400）、
只允许单赛道且 `limit ≤ 30`（否则 400）、已有任务在跑返回 409 并带上 `run_id`。
单飞锁是 `oj_runs` 上的部分唯一索引，不是内存标志位 —— uvicorn 多 worker 时
内存锁等于没有锁。

### 评测配置与题库（`openjudge/api_edit.py`，prefix `/api/openjudge`）

写接口单独一个模块：读接口失败是 500，写接口失败是 422 + 「为什么不合法」，混在一起会让
`api.py` 的异常处理变成「一次判两种错」。

```
GET   /api/openjudge/settings                      当前配置 + 实际生效值 + 上下限
PATCH /api/openjudge/settings                      改被测/裁判模型、默认开关、并发、超时（422 不落库）
GET   /api/openjudge/cases?track=&source=          题库列表（仓库 + 覆盖层 - 隐藏）
GET   /api/openjudge/cases/{id}                    单条用例定义
PUT   /api/openjudge/cases/{id}                    覆盖或新增一条（422 含字段名）
DELETE /api/openjudge/cases/{id}                   隐藏一条仓库用例
POST  /api/openjudge/cases/{id}/restore            撤销覆盖 / 取消隐藏
POST  /api/openjudge/cases/from-batch/{batch_id}/{row_id}  失败用例回流成 `-b<batch>` 草稿
```

两条容易踩的边界：

- **`{id}` 是题库 id（字符串），不是批次内的 `oj_cases.id`（自增整数）**。批次明细挂在
  `/batches/{batch_id}/cases/{case_row_id}`；同路径不同方法会被 FastAPI 合并成一条路由，
  只认先注册的那个。
- **空字符串是「不覆盖」，不是「用空模型」**。所以 `GET /settings` 分开发 `values` 与
  `effective` 两份：前者是配的，后者是这次真会用的（`显式参数 > oj_settings > env / 全局配置`）。

在线改写的落点是覆盖层，不是 git 里的题库：

| 路径 | 角色 |
|---|---|
| `eval/openjudge/cases/*.jsonl` | 仓库题库，`validate` 与 CI 覆盖率基线的唯一口径 |
| `workspace/openjudge/cases/overlay.jsonl` | 同 `(track, id)` 覆盖仓库条目；仓库里没有就是新增 |
| `workspace/openjudge/cases/deleted.json` | 隐藏清单（只存 id），`repo_raw()` 仍能读到原条目 |

读时按「仓库 → 覆盖 → 去隐藏」合并，因此本地删题不会让 CI 覆盖率棘轮假红 —— 精确表述是：
`casestore.repo_cases()` 才是 `validate` 的口径，`load_cases()` 是在线跑与 CLI 的口径。

写入前置校验 `benchmark.parse_case()` + 单条静态检查（结构 / 赛道 / fixture 存在），不合法返回
422 且**磁盘零写入**：允许一条跑不起来的用例进题库，等于把它变成采集阶段的一次崩溃。落盘用
临时文件 + `os.replace`，避免半截 JSONL 让整个题库读不出来。

### 前端路由（`web/src/App.tsx`）

| 路径 | 页面 |
|---|---|
| `/` | Workspace（对话 + 权限选择 + 确认卡片） |
| `/runs` / `/runs/:id` | 运行列表 / 详情 |
| `/quality?view=overview\|batches\|scoreflow\|studio` | 质量四视图：概览（门禁矩阵 + 最新批次） / 批次（身份卡 + 分页 + 五赛道） / 评分流程（三道门禁 + 归因下钻） / 题库与模型。旧 `?focus=scoreflow` 兼容重定向 |
| `/cost` | 成本 |
| `/dashboard` | 看板 |

## 9. 已知缺口

| 缺口 | 事实 |
|---|---|
| `insufficient` 退出码为 0 | 门禁不判（样本不足）时 CI 仍绿 —— 这是刻意的，但意味着「赛道被删薄」只能靠覆盖率基线拦 |
| 在线任务不续跑 | 挂在进程内 `asyncio.create_task` 上，重启由 `reap_stale_runs` 标 `failed(error='interrupted')`，不做断点续跑 |
| 在线评测只到单赛道 | `limit ≤ 30` + 单赛道；全量 90 例走 CLI / nightly |
| 选题配比未做棘轮 | `validate` 只校验每赛道最低条数，赛道内部的 risk / subject 分布靠 `coverage.json` 记录，没有强制配比 |
| 无 flaky 检测 | 已支持 `trials ≤ 3` 与 pass@k / pass^k，但随机种子仍不固定 —— 换种子复现同一抖动还做不到 |
| 题库覆盖层不进 git | `workspace/openjudge/cases/` 属于运行态：换机器 / 换人要重配。**要长期生效的题目得手动搬回 `eval/openjudge/cases/`**，覆盖率基线也才认它 |
| 用例编辑只有 JSON 文本框 | 没有表单化编辑器（赛道下拉、fixture 选择器、`expected` 结构化输入）；校验靠服务端 422 回显 |
| 改模型不改采样参数 | 在线只覆盖 model 名，温度 / 窗口 / 工具集仍走五层解析。换模型时靠 `meta.model` 判 `not_comparable`，但同模型换温度不会被发现 |
