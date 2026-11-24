# SPEC-0003：评测打分引擎迁移到 DeepEval

- 状态：Draft（待评审）
- 日期：2026-11-25
- 相关：`src/devops_agent/eval/{judge,dataset,graders,runner,report,cli}.py`、
  `src/devops_agent/observability/{eval_api,repository}.py`、`web/src/pages/EvalBatches.tsx`、
  `.github/workflows/{ci,eval-nightly}.yml`、SPEC-0002、ADR-0001

## 1. 背景与结论

当前评测是自研闭环：规则判卷（`graders.py`）+ 手写 LLM 裁判（`judge.py`）+
自研聚合与门禁（`report.py`）+ 前端批次页。上一轮分析结论是：

- **通用部分重复造轮子**：LLM-as-judge、指标定义、结果聚合、结果浏览；
- **特有部分不能替换**：AgentScope 事件轨迹、权限档位可达性（S8）、
  实验可比性因子、前缀缓存核算。

因此本 spec 的替换面**不是“整个评测”**，而是**打分引擎**：

| 替换 | 保留 |
| --- | --- |
| `judge.py` 的 HTTP 客户端、JSON 解析、六维提示词 | `runner.py`（临时工作区 + AgentScope 事件消费） |
| `report.py` 的 judge 均值/逐维结果结构 | `dataset.py` 的 manifest / sha256 / 配比 / 阈值 |
| 维度与 rubric 的 Python 常量 | `lint.py` S1–S8（尤其 S8 真跑权限引擎） |
| 手写重试/超时/并发 | `report.py` 的门禁、判决、可比性、Markdown |
| — | `eval_api.py` + 前端（Confident AI 云为明确非目标） |

> 若把 `runner`、`lint`、`compatibility` 也交给 DeepEval，会同时失去：
> 逐用例权限档位、静态不可达用例拦截、跨批次归因阻断。这不是迁移，是降级。
> 本 spec 明确拒绝该范围。

## 2. 目标与非目标

### 目标

1. 六维 PEEM 由 DeepEval `GEval` 表达，锚点唯一来源改为
   `eval/rubrics/peem.v1.md`，删除 `judge.JUDGE_RUBRIC` 双份文案。
2. 规则判卷保持确定性，**不交给 LLM**；结果以自定义 `BaseMetric` 进入同一张
   DeepEval 结果表，便于统一展示。
3. DeepEval 的 `reason` 落库并透出前端，补齐 SPEC-0002 的“裁判可解释”缺口。
4. 离线可测：单测用 `DeepEvalBaseLLM` 假模型，不联网；CI 无模型凭据时
   `validate` / `prompt-lint` 行为不变。
5. 灰度双跑、可回滚；跨引擎批次自动判 `not_comparable`。

### 非目标

| 不做 | 理由 |
| --- | --- |
| 接入 Confident AI 云 / `deepeval login` | 当前本地单用户、无鉴权；评测样本不应出网 |
| 用 DeepEval 驱动 AgentScope agent | DeepEval 没有权限档位、临时工作区、fixture 概念 |
| 删除 S1–S8、阈值门禁、可比性因子 | 这些是现有评测的可信度来源 |
| 改 CLI 退出码语义 | `ci.yml` / `eval-nightly.yml` 依赖 `fail`/`warn` 非零、`insufficient` 为零 |
| 用 DeepEval 原生指标整体替换 PEEM | 会切断历史基线，见 §12 |

## 3. 目标架构

```
eval/suites/*.jsonl + manifest        （唯一题库事实源，保留）
        │
        ▼
runner.run_suite（AgentScope / 临时工作区 / 权限档位，保留）
        │  TraceResult
        ▼
eval/engine/deepeval_adapter.py
   ├─ to_llm_test_case(case, trace)     → LLMTestCase
   ├─ build_metrics(case)               → list[BaseMetric]
   └─ from_evaluation_result(result)    → CaseScore + judge_scores + evidence
        │
        ▼
deepeval.evaluate(test_cases, metrics)（并发 / 重试 / 缓存由引擎负责）
        │
        ▼
report.aggregate / gates / compare（保留）
        │
        ├─ report.json / report.md
        └─ SQLite eval_batches / eval_cases → /api/eval → 前端
```

### 新增模块

```
src/devops_agent/eval/engine/__init__.py
src/devops_agent/eval/engine/model.py       # 项目凭据 → DeepEval 模型
src/devops_agent/eval/engine/metrics.py     # PEEM GEval + RuleMetric + ToolCorrectness
src/devops_agent/eval/engine/adapter.py     # EvalCase/TraceResult ↔ DeepEval 对象
```

框架导入边界：DeepEval 只允许出现在 `eval/engine/`，与 AGENTS.md §3.1 的
“装配点集中”一致；`dataset.py`、`graders.py`、`report.py` 继续不 import 引擎。

## 4. 依赖与版本

```toml
[project.optional-dependencies]
eval = ["deepeval>=2,<3"]
```

- 基础安装不变；CI `quality` 装 `.[dev,eval]`，nightly 装 `.[eval]`。
- `deepeval` 会带入 `openai` / `requests` / `rich` 等传递依赖，PR 需附
  `pip install -e ".[eval]"` 的体积与 pydantic 冲突检查结果。
- **P0 必须先 introspect**（AGENTS.md §3.4），不得凭记忆写 API。核对清单：
  1. `EvaluationDataset` / `Golden` / `LLMTestCase` / `ToolCall` 的实际字段；
  2. `GEval` 的 `criteria`、`evaluation_params`、`threshold`、`model` 签名；
  3. `deepeval.evaluate()` 在无登录状态下是否可用、返回结构；
  4. `DeepEvalBaseLLM` 需实现的最小方法（`generate` / `a_generate` / `load_model`）；
  5. 关闭遥测/云上传的环境变量名，并在测试中断言无出网；
  6. `max_concurrent` / 重试 / 缓存的当前配置入口。

## 5. 数据映射

### 5.1 `EvalCase` → `Golden`（运行时生成，不落盘）

```python
Golden(
    input=case.input,
    additional_metadata={
        "case_id": case.id, "tags": case.tags, "subject": case.subject,
        "permission": case.permission, "fixture": case.fixture,
        "expected": case.expected.model_dump(mode="json"),
        "timeout_s": case.timeout_s,
    },
)
```

题库仍以 JSONL + manifest 为准：DeepEval 的 dataset 不提供 `sha256`、场景配比、
`min_cases_for_gate`，这些必须留在 `dataset.py`。

### 5.2 `TraceResult` → `LLMTestCase`

```python
LLMTestCase(
    input=case.input,
    actual_output=trace.final_text,
    tools_called=[ToolCall(name=n) for n in trace.executed_tools],
    expected_tools=[ToolCall(name=n) for n in must_tools],
    completion_time=trace.latency_ms / 1000,
    token_cost=None,  # 无单价，不编造；token 明细进 metadata
    additional_metadata={
        "status": trace.status,
        "tool_names": trace.tool_names,        # 叫过
        "executed_tools": trace.executed_tools,# 跑过
        "denied_tools": trace.denied_tools,    # 被拒
        "tokens": trace.tokens,
        "cache_input_tokens": trace.cache_input_tokens,
        "cache_creation_tokens": trace.cache_creation_tokens,
        "error": trace.error,
    },
)
```

**关键约束**：`tools_called` 只放 `executed_tools`。现有三档语义里“被拒”不是
“调用成功”；若把 `denied_tools` 也塞进 `tools_called`，`ToolCorrectnessMetric`
会把越权被拒判成命中，直接破坏 `require_executed` 的安全断言。

### 5.3 指标映射

| 现有判分 | DeepEval 实现 | 门禁 |
| --- | --- | --- |
| `correctness` 1–5 | `GEval(criteria=rubric_correctness, threshold=0.8)` | 是（4.0/5） |
| `coherence` / `relevance` / `objectivity` / `clarity` / `conciseness` | 各自 `GEval`，仅报告 | 否 |
| 规则断言 `must_contain` 等 | 自定义 `RuleMetric(BaseMetric)` 包装 `graders.grade()`，score 0/1 | 是（pass_rate） |
| 工具 must / forbid / 三档 | 自定义 `RuleMetric` + 可选 `ToolCorrectnessMetric` 仅作展示 | 是（trajectory 门禁保留自定义公式） |
| `max_total` 预算、`ordered` | 保留 `graders._tool_score` | 是 |
| `status` / `safety` / `grounding` | 规则判卷硬失败分类，不变 | 是（blocking） |

阈值换算：DeepEval 阈值是 `0..1`，现有 `judge_correctness.min = 4.0` 在
适配层除以 5；manifest 仍存 1–5 口径，避免历史基线失真。

### 5.4 结果回写

- `judge_scores`：维度 → 1–5 分，换算回原量纲，`report.py` 的 `_judge_means`
  与 DB `scores_json` 不改结构。
- `judge_evidence`：维度 → DeepEval `reason`（截断 1000 字符），新增字段，
  前端 SPEC-0002 直接消费。
- `detail_json.engine = {"name": "deepeval", "version": ..., "metric_version": ...}`。
- 规则判卷仍先执行；DeepEval 指标异常时降级为 `judge=None` + 一条 `judge` check，
  不否定规则结论（沿用现有 `_score_with_judge` 语义）。

## 6. 文件与接口变更

| 文件 | 变更 |
| --- | --- |
| `eval/judge.py` | 保留 `Judge` Protocol；新增 `DeepEvalJudge`（P1）；`OpenAIJudge` 标记 deprecated，P3 后删除 |
| `eval/engine/*.py` | 新增：模型、指标、适配器 |
| `eval/runner.py` | 不改；`judge.score()` 契约不变 |
| `eval/report.py` | `_judge_means` 读入证据字段；summary 增加 `judge_evidence`；门禁/对比逻辑不动 |
| `eval/cli.py` | 新增 `--engine legacy|deepeval`（默认 legacy 至 P3）；退出码不变 |
| `eval/dataset.py` | `GRADER_VERSION` 升级为 `v3`，`manifest.grader_version` 同步 |
| `observability/repository.py` | `ALTER` 增加 `eval_batches.engine` / `engine_version` / `metric_version`；旧库必须显式迁移 |
| `observability/eval_api.py` | 批次与用例响应增加 `engine`、`judge_evidence` |
| `pyproject.toml` | 新增 `[eval]` extra |
| `README.md` / `docs/wiki/Observability-and-Eval.md` | 更新裁判与门禁说明 |

## 7. 分阶段实施

### P0 — 可行性 spike（只读，不改行为）

- 装 `.[eval]`，跑通 1 条用例的 `GEval`；记录真实签名与返回结构到本 spec 附录。
- 验证无登录可用、无云上传、可注入假模型。
- 产出：`docs/specs/0003-*.md` 附录 + 依赖体积报告。
- 退出条件：上述 6 项 introspection 全部有实际输出。

### P1 — 适配层与并行开关

- 新增 `engine/` 四个模块；`DeepEvalJudge.score()` 复用现有 `TraceResult`。
- 单测全离线；`--engine legacy` 仍为默认，nightly 行为不变。
- 验收：`pytest` 覆盖率 ≥80%，`ruff` / `mypy` / `vulture` 通过。

### P2 — 影子双跑

- nightly 对 13 条配了裁判的用例同时跑 legacy 与 deepeval，写两套结果；
  门禁仍用 legacy。
- 对比：逐维均值差 `|Δ| ≤ 0.3`，用例级排序 Spearman `≥ 0.7`，连续 5 个 nightly 达标。
- 不达标则回到 P1 调整 criteria / 模型 / 阈值，不进入 P3。

### P3 — 切换默认

- `--engine` 默认改 `deepeval`；manifest `grader_version=v3`，刷新基线。
- 比较层新增阻断因子：`eval_engine`、`metric_version`；跨引擎批次判 `not_comparable`。
- legacy 保留一个发布周期，供回滚。

### P4 — 清理

- 删除 `judge.py` 的 `urllib` 客户端、`_parse_scores`、`JUDGE_RUBRIC` 常量。
- rubric 改为运行时读取 `eval/rubrics/peem.v1.md`；S4 从“只查存在”升级为“可解析”。
- 删除 `--engine legacy` 分支与相关测试。

## 8. 成本与并发

现状：13 条裁判用例 × 1 次请求。迁移后六维各一次 `GEval`：**约 78 次/批**。

- 缓解 1：`deepeval.evaluate(..., max_concurrent=N)` 并发，N 默认 4，nightly 可调。
- 缓解 2：按 `(case_id, prompt_sha, model, metric_version)` 缓存裁判结果，
  命中则复用上一批 `judge_evidence`（缓存键必须含 metric_version）。
- 缓解 3：coherence/clarity 等非门禁维度支持 `--judge-dimensions` 裁剪，
  仅手动排查时全开；nightly 默认 correctness + objectivity + relevance。
- P2 若 `GEval` 多次采样导致成本超预算，评估改用一次调用返回多维的
  `DAGMetric`（以 P0 的 introspection 结果为准），但不得退回手写解析器。

## 9. 测试策略

| 层 | 方式 |
| --- | --- |
| 映射 | `EvalCase`/`TraceResult` → `LLMTestCase` 的字段快照；被拒工具不得进 `tools_called` |
| 指标 | `FakeLLM(DeepEvalBaseLLM)` 返回固定 JSON；断言六维解析、阈值换算、evidence 截断 |
| 降级 | 引擎抛异常 → `judge=None` + `check(judge, ok=False)`，规则结论不变 |
| 报告 | rule-only 路径的 `summary` schema 与迁移前逐字段一致（golden snapshot） |
| 离线 | 单测禁用网络（monkeypatch `urlopen` / `requests`），断言无云上传 |
| 迁移 | `--engine legacy` 与 `deepeval` 对同一 fake 输出产生相同 `CaseScore.passed` |

CI：`quality` 安装 `.[dev,eval]` 并跑上述单测；`eval-nightly` 在 P3 前
只加 shadow job，不改门禁。

## 10. 风险与回滚

| 风险 | 措施 |
| --- | --- |
| 裁判漂移导致分数不可比 | P2 影子双跑；`eval_engine` 列为阻断因子；基线刷新走 PR |
| 依赖体积 / pydantic 冲突 | 独立 extra；P0 记录 lock 差异；冲突则锁定 `deepeval` 上限 |
| 遥测/云上传 | 显式关闭；单测断言无出网；不配置 `CONFIDENT_API_KEY` |
| API 不稳定 | 固定 minor 版本；P0 introspection 成文；适配层隔离 |
| 成本上升 | 并发 + 缓存 + 维度裁剪；成本超限回滚 P2 |
| 全量替换冲动 | 本 spec 明确 `runner` / `lint` / `compatibility` 不在替换面 |

回滚路径：`--engine legacy` 一键切回；DB 只增列不删列；`grader_version` 回退 +
基线回退，比较层会判 `not_comparable` 而不是误报退化。

## 11. 验收标准

1. `--engine deepeval` 跑完 40 例，`report.json` 字段只增不改；`judge_evidence` 存在。
2. 规则类失败（`safety` / `forbidden_tool` / `status`）的退出码与迁移前完全一致。
3. 跨引擎 compare 返回 `not_comparable`，原因包含 `eval_engine`。
4. 单测全程离线；覆盖率 ≥80%；`ruff` / `mypy` / `vulture` / 复杂度门禁通过。
5. README / wiki / SPEC-0002 的评测章节同步；旧 `JUDGE_RUBRIC` 删除后
   `peem.v1.md` 成为唯一锚点。

## 12. 未决问题

1. **六维 vs 原生指标**：是否用 `AnswerRelevancy` / `Faithfulness` 等替换
   `relevance` / `objectivity`？建议先保持 PEEM，避免历史基线断裂；P4 再评估。
2. **单次调用 vs 六次 GEval**：成本与一致性取舍，由 P2 数据决定（§8）。
3. **Confident AI**：仅当用户明确接受云端样本时才评估；当前按非目标处理。
4. **`scorer.py` 三口径**：迁移后 `EvalDimension` 仍是“反馈/健康分维度”，
   与 PEEM 六维不是同一概念；本 spec 只要求改名与文档澄清，不合并枚举。
