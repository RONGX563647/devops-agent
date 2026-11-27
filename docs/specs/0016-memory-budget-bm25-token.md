# SPEC-0016：记忆预算执行、BM25 召回与 token 口径

- 状态：方案 v1（待评审）
- 日期：2026-11-27
- 基线：`main 5661ced`
- 关系：修正 SPEC-0013 §5.4（预算只算不用）、§5.6（前缀缓存缺可校验契约）、§4.3（长期写入为回合原文）；在 SPEC-0015 §6（token 校准 fixture 与对账）之上加「分族系数 + 消息开销 + 收紧偏差上界」，不另立第二套口径
- 范围：`memory/{tokens,budget,short_term,long_term,context,runtime,observer}.py`、`memory/bm25.py`（新）、`memory/summarize.py`（新）、`observability/repository/{memory,schema}.py`、`config/{memory,model}.yaml`、`gateway/routes/{health,memory}.py`、`tests/**`
- 触发来源：一次记忆架构走查（3 条结论）+ 走查中补出的 5 条检索侧缺陷
- 证据约定：现状结论一律带 `路径:行号`；未运行的验证标 `NOT RUN`

## 1. 设计结论

1. **预算的问题不是算式，是没有执行点**。`compute_budget` 只有两个消费者：`observer.py:191` 发 `CONTEXT_ASSEMBLED`、`tools/context_api.py:162` 出接口。真正的裁剪只看 `max_turns=12`（`short_term.py:171`），所以「短期 50%」是一张不约束任何东西的仪表盘。工具 schema 越大、窗口越小，这个假象越危险。
2. **不做执行就等于把阈值交给框架单点兜底**。框架在 `R×W=102400` 才压缩，而我们的预算是 `Hmax≈86540`；两者之间 14k token 的灰区里，历史可以涨到「注入加不进去」而没人报错。执行化后两侧口径必须闭合：`S + O + Hmax = T`。
3. **token 口径的错不在系数，在「一套系数打所有模型、所有文本」**。`tokens.py:10-16` 用 `CJK/1.4 + ASCII/3.6` 两个常数覆盖所有模型族；同一段 JSON 在 `o200k` 与 `cl100k` 上的密度差 20% 以上。SPEC-0015 §6 已经补上 fixture 与「估算—实测对账」，但那只是**告诉你漂了**（阈值 8%/12%），没有让估算**不漂**：fixture 是一种文本形状，计数器仍是全模型族通用。本 spec 补的是后者，并把偏差上界收到 5%/2%。
4. **召回比的不是语义，是「最近 50 条里谁字面更像」**。`long_term.py:71` 传 `limit=max(top_k*20, 50)` 给 `list_long_term`，而后者是 `ORDER BY id DESC LIMIT ?`（`repository/memory.py:209`）——第 51 条以后的条目**永远不会被召回**，`max_entries=200` 里有 75% 是死数据。这比「打分不先进」严重得多。
5. **`min_score` 阈值建立在被截断的样本上**。分母是 `sqrt(len(query_tokens))`（`long_term.py:48`），与文档长度无关：一条 20 字条目和一条 900 字条目得分口径不同，阈值 0.35 实际是「短条目优先」的隐式偏好。
6. **「写入原文」的真正代价是让检索失效**。原文拼接（`runtime.py:232`）使每条记忆都含大量寒暄与过程话术，词面重合被稀释；同时 `text=?` 精确去重（`repository/memory.py:180`）几乎永不命中，条目只增不减。
7. **前缀缓存的最大杀手不是注入，是默认开启的去重**。`dedup_reads: true` 每轮都会把历史中间的工具结果改成 `[重复读取已省略]`（`short_term.py:104-136`）。改动点在历史中段 → 从该点起整段缓存失效，而多数轮次其实根本没超预算。**不改历史才是最优策略**，当前实现反了。
8. **顺序：口径 → 执行 → 缓存契约 → 召回 → 摘要**。前两步决定「装不装得下」，第三步决定「装得下还不重复付费」，后两步决定「装进去的东西值不值得」。反序做（先换 BM25）等于优化一个被 `LIMIT 50` 掐死的通道。

## 2. 现状证据

| 编号 | 断言 | 证据 | 复现 |
|---|---|---|---|
| E-1 | 长期召回只覆盖最近 50 条 | `memory/long_term.py:71` → `observability/repository/memory.py:203-212`（`ORDER BY id DESC LIMIT ?`） | 写 60 条后检索第 1 条内容，恒为空 |
| E-2 | 去重是全文精确相等，原文几乎不会重复 | `repository/memory.py:179-184` | 同一事实两次不同措辞 → `stored=True` 两次 |
| E-3 | `score` 列恒为 0，检索分数不落库 | `repository/memory.py:189`（`VALUES (?, ?, ?, ?, 0, ?, ?)`），全仓无 UPDATE | grep `UPDATE long_term_memories` → 0 |
| E-4 | 无命中计数与最近使用时间，无法强化或按热度淘汰 | `repository/memory.py:168-200`（淘汰用 `created_at`） | 表结构无 `hits`/`last_hit_at` |
| E-5 | 预算零执行 | `memory/budget.py` 全文无调用方写回裁剪；`short_term.py:171` 只用 `max_turns` | `compute_budget` 调用点：`observer.py:191`、`context_api.py:162` |
| E-6 | 去重默认开启，改的是历史中段 | `memory/short_term.py:104-136`，`memory_policy.py:20`（`dedup_reads: bool = True`） | 同一 tool_result 出现两次 → 第一次被替换，位置在历史中段 |
| E-7 | token 系数单点校准且不分模型族 | `memory/tokens.py:10-16` | 见 §4.6 |
| E-8 | 长期写入是「用户原文 + 助手原文」截 1000 字 | `memory/runtime.py:232-233`（`_MAX_WRITE_CHARS = 1000`） | 读 `long_term` 表即是整段对话 |

E-1 复现（`NOT RUN`，按 §10 A30 实现为用例）：

```text
写入 60 条 → search("第 1 条的关键词") → hits == []
原因：limit=max(5*20,50)=50，ORDER BY id DESC 只取最新 50 条
```

## 3. 目标与非目标

### 3.1 目标

- token 计数误差可控且**可验证**：单段 ≤3%、总额 ≤1.5%。
- 预算成为硬约束：每一轮请求的 `历史 + 注入 + 固定前缀 + 输出预留 ≤ 压缩阈值`。
- 前缀缓存有契约、有指纹、有测试：**未超预算的轮次不改历史一个字节**。
- 召回改用 BM25，覆盖 scope 内全量条目，分数可比较、阈值有依据。
- 长期写入从「回合原文」变为「结论条目」，同义条目合并而非新增。

### 3.2 非目标

- 不引入 embedding / 向量库 / mem0 / agentic（`backend` 语义保持 SPEC-0013 §18.2 M1：请求即降级）。
- 不用 SQLite FTS5。理由：中文需预分词成空格隔开的二元组才能用 `unicode61`，等于把分词器搬到写入侧；`max_entries=200` 的量级下纯 Python 索引重建 <2ms，FTS5 的持久化收益换不回跨平台不确定性。
- 不做跨会话共享索引、不做记忆的语义摘要（模型摘要只作为可选模式，默认抽取式）。

## 4. token 口径（S1）

### 4.1 三层精度

| 级 | 来源 | 用途 | 缺省行为 |
|---|---|---|---|
| L0 | 供应商返回的 `usage.prompt_tokens` | 校准与审计的**真值** | 无（拿不到就跳过校准） |
| L1 | 可选真实编码器（`[tokenize]` extra → `tiktoken`） | 预算执行 | 未安装时降级 L2 |
| L2 | 分族系数估算（本 spec 的最小实现） | 预算执行与 L1 兜底 | 恒可用 |

精度优先级 L1 > L2；L0 不参与执行，只用于「L1/L2 是否漂移」的判定。

### 4.2 分族系数

`config/model.yaml` 增：

```yaml
tokenizer:
  # 模型族：决定系数；未知族按 generic
  family: generic          # cl100k | o200k | qwen | generic
  # 每 token 承载的字符数（按族不同，generic 为保守值）
  cjk_per_token: 1.4
  ascii_per_token: 3.6
  # 文本/JSON 密度差：JSON 标点密度高，同字符数 token 更多
  json_factor: 1.12
  # 每条消息的固定开销（role、分隔符、包装）
  per_message_overhead: 4
```

估算公式（`memory/tokens.py` 改写）：

```text
tokens(text) = ceil( cjk / cjk_per_token + (chars − cjk) / ascii_per_token )
tokens(messages) = Σ tokens(text_i) + per_message_overhead × n
tokens(tool_schemas) = ceil( tokens(json) × json_factor )
```

族系数取自公开编码器的实测样本，落成 `tests/fixtures/token_calibration.json`（每族 ≥6 段：纯中文 / 纯英文 / 中英混排 / 代码 / JSON / 路径密集），由 §10 A24 校验。该文件是 SPEC-0015 §6.2 那份 fixture 的**超集**：保留原有 `system_chars/tools_chars/expected_tokens` 三键作基线断言，新增 `shapes[]` 数组，老测试不需要改。

### 4.3 自适应修正

沿用 SPEC-0015 §6.3 的「估算—实测对账」事件（`CONTEXT_ESTIMATE_DELTA`，每 run 首帧），只扩状态、不新增事件名。每次拿到 L0 真值后记一条 `(est, actual)`；滚动窗口 32 条，用中位数比率 `α = median(actual/est)`：

- `|α − 1| ≤ 0.02`：不动作（噪声以内），事件 `state=ok`。
- `0.02 < |α − 1| ≤ 0.10`：修正本进程内存系数，事件 `state=adjusted`（SPEC-0015 的 0.12 手工阈值收敛为 0.10，因为系数分族后剩的是真漂移，不再包含族差异）。
- `|α − 1| > 0.10`：事件 `state=error`，并在 `/health` 的 `memory.tokens.drift` 里暴露——通常意味着换了模型族却没改 `family`。

α 不落配置文件（避免运行期改配置），只落 `workspace/token_calibration.json` 供排障。与 SPEC-0015 `estimate_margin_ratio` 的关系：margin 只作用在**固定前缀**上，`headroom`（§5.1）只作用在**历史**上，两者不重复扣减。

### 4.4 计数点收敛

全仓只允许两处计数：

1. `memory/tokens.py::count()`——唯一的估算入口；
2. 框架侧 `count_context_tokens(agent)`（`runtime/compression.py:138`）——唯一的真值入口。

删除 `runtime/compression.py` 里对 `context_config.trigger_ratio` 的重复解析（`compression.py:129-137` 与 `memory/budget.py` 各算一次），改为共用 `compute_budget`。

### 4.5 额度的字符换算

`note_max_chars` 由 token 额度派生，不再独立配置：

```text
note_max_chars = min(policy.context.note_max_chars_hard, floor((B_episodic + B_long) × cjk_per_token))
```

`note_max_chars_hard` 是硬上限（默认 1200），防止「窗口极大 → 注入极长」的失控。

### 4.6 为什么不是「把系数调准就行」

SPEC-0015 §6.1 已经量化过单点校准的失效：docstring 里 409 字符 + 10139 字符的样本，被现实中的 452 + 10147 超过了，而测试仍绿。但换成新样本仍然只是一种形状：同一对系数用在 200 字中文摘要上偏差约 −12%，用在路径密集的工具输出上约 +9%（`NOT RUN`，A24 落地时实测填入）。单点校准必然在长尾上错——所以本 spec 的验收是**分形状的偏差上界**，不是「调一个更好的常数」。

## 5. 预算执行（S2）

### 5.1 闭合等式

沿用 SPEC-0013 §5.4 的符号（`config/model.yaml`）：`W=128000`、`R=0.8`、`O=12800`、`S≈3060`。

> 文档间有一处口径打架：SPEC-0015 §6.1 写的是 `128000 × 0.8 − 4096`，把输出预留当成固定 4096；而 `compute_budget`（`memory/budget.py:57`）在 `max_tokens` 为空时取 `reserve_ratio × W = 12800`。本 spec 以代码为准（`O=12800`），并把「`O` 的来源」写进 §5.1 的等式，避免再出现第三种说法。

```text
T    = R × W                     = 102400   # 框架压缩阈值
Hmax = T − S − O                 = 86540    # 历史面总额度
history_limit = B_short + B_headroom      = 51924
note_limit    = B_episodic + B_long       = 34616
恒等式：S + O + history_limit + note_limit = T
```

`B_headroom` 的作用从「保险」变成「显式额度」：它属于历史，用来吸收本轮新增工具结果的波动；`headroom` 用完就触发 §5.2 的 S4，而不是等框架压缩。

### 5.2 裁剪算法（`trim_messages` 重写）

```text
输入：messages, policy, history_limit(tokens)
S0  若 tokens(messages) ≤ history_limit → 原样返回（report.changed=False）
S1  截断「最后一条 user 之后的」超长工具输出（尾段优先，改动点最靠后）
S2  去重重复读取（必须看原文，故早于 S3）
S3  截断历史中段的超长工具输出
S4  从最旧整轮开始淘汰，直到 ≤ history_limit 或只剩一轮
S5  仍超（单轮本身过大）→ 截断当前轮工具输出，report.over_budget=True
```

三条硬约束：

- **S0 必须真的零改动**：返回同一个 list 对象（不 `model_copy`），否则等于每轮重建历史（§6 V3）。
- **当前用户输入永不截断**，只截它的工具结果。
- **S4 只切整轮**（`head + messages[keep_from:]`），不在轮内挖洞——轮内挖洞会让「同一个 tool_call 的结果」与「它的调用」错位。

`TrimReport` 增字段：`tokens_before`、`tokens_after`、`limit`、`over_budget`、`stage`（`S1|S2|S3|S4|S5`），全部进 `MEMORY_TRIM` 事件。

### 5.3 `dedup_reads` 改为三态

`dedup_reads: never | on_pressure | always`，默认 `on_pressure`。

理由见 §1.7：默认 `true` 会在**未超预算**的轮次里改写历史中段。`on_pressure` = 只在 S0 判定超预算后才启用；`always` 保留给评测对比。这是本 spec 对缓存命中最直接的一项改动。

### 5.4 与框架阈值的关系

- 启动期校验：`S + O + history_limit + note_limit ≤ T`，超出则 `logger.warning` 并在 `/health` 的 `memory.budget.ok=false` + `overshoot` 字段说明差额；不拒绝启动（降级为「按比例压缩四项」）。
- 运行期断言（仅测试环境）：裁剪后 `tokens(messages) + note_tokens + S + O ≤ T × 1.02`（2% 给估算误差）。
- 不再新增「记忆自己的压缩阈值」：压缩仍由框架在 `T` 触发，记忆只负责**保证触发前装得下**。

## 6. 前缀缓存契约（S3）

### 6.1 五条不变量

| 编号 | 不变量 | 违反后果 |
|---|---|---|
| V1 | 系统提示与工具 schema 逐字节稳定；工具按固定名序注册，不随 dict/权限顺序变化 | 每次调用全量 miss |
| V2 | 记忆注入只追加在消息序列末尾（`UserMsg(name="system")`），不插入历史中段 | 从中段起全 miss |
| V3 | 未超预算的轮次，历史消息逐字节不变（S0 早退） | 每轮全 miss |
| V4 | 版本号、命中哈希、`turn_key`、时间戳一律不进 prompt；它们只存 `middle_context` | 前缀每轮变 |
| V5 | 同轮内多次模型调用复用同一份 note（`observer.py:103-107` 已有），且 note 内容不依赖「第几次调用」 | 同轮内重复付费 |

### 6.2 观测：三段指纹

`CONTEXT_ASSEMBLED` 增 `cache` 段：

```json
{
  "cache": {
    "prefix_fingerprint": "9f2c1ab0d3e4",
    "prefix_reused": true,
    "history_changed_at": 37,
    "history_changed_by": "dedup",
    "cached_prefix_tokens": 41200,
    "estimated": true
  }
}
```

- `prefix_fingerprint = sha256(system_text + tool_names + tool_schema_json)[:12]`；
- `history_changed_at` = 与上一轮逐条比较的第一个差异下标（-1 表示未变）；
- `history_changed_by` ∈ `none | trim_tail | dedup | trim_mid | drop_turns`，直接对应 §5.2 的 S1–S4，让「缓存为什么失效」在轨迹里可归因；
- `cached_prefix_tokens = S + tokens(messages[0:history_changed_at])`，前端据此画命中率。

### 6.3 明确禁止

- 把档位说明、记忆卡、时间戳写进系统提示（SPEC-0013 §5.6 已有，本 spec 加测试钉住）。
- 为了「格式整齐」重排历史、合并相邻同角色消息。
- 在 system note 里写「第 N 轮」「本轮时间」这类每轮必变的内容。
- 任何在 S0 之前发生的 `model_copy` / `model_dump` 往返（会改变序列化字节）。

## 7. BM25 召回（S4）

### 7.1 分词器（`memory/bm25.py::tokenize`）

```text
1) ASCII：小写；snake_case / camelCase 整体保留 + 按 _ 与大小写切出的子词各记一次
2) 路径与命令：按 / \ . : - 切开，再按 1) 处理
3) 数字串：4 位以上保留原样（版本号、端口、issue 号）
4) 中文：连续汉字段的二元组（单字段落用单字）
```

保留现有 `_tokens` 的「ASCII 词 + 中文二元组」骨架，新增 1) 的子词、2) 的路径切分、3) 的数字串——这三类恰是 DevOps 语料的召回主力（`SKILL.md`、`app.py:120`、`v1.5.2`、`#71`）。

### 7.2 打分

```text
idf(t)  = ln(1 + (N − df + 0.5) / (df + 0.5))
score(d) = Σ_t idf(t) × tf(t,d)(k1+1) / (tf(t,d) + k1(1 − b + b·dl(d)/avgdl))
k1 = 1.2, b = 0.75
norm(d) = min(1.0, score(d) / ideal)
ideal   = 同一 query 对「理想文档」的打分（每词出现 1 次、dl = avgdl）
```

- `norm` 使 `min_score` 保持 0~1 且**与文档长度无关**，修掉 §1.5 说的隐式偏好（分母 `sqrt(len(query_tokens))` 让短条目天然占优）；
- `min_score` 默认值由 0.35 重标为 0.30（`fixture` 上取 precision ≥0.8 的最小阈值，见 A31）；
- `Hit.score` 语义改为 `norm`，`Hit` 增 `raw`（原始 BM25）供调试。

### 7.3 索引与失效

- 索引粒度：`(scope, scope_id)`；结构 `{term: [(doc_id, tf)]}` + `{doc_id: dl}` + `avgdl`。
- 失效键：`(count, max_id, max(updated_at))`，三者任一变化即重建；写入路径（`LocalLongTerm.write`）主动 `invalidate()`，读路径懒重建。
- 规模：`max_entries=200` × 平均 60 词 → 重建 <2ms，不落盘、不跨进程共享。
- **取数改为全量**：`list_long_term` 增 `all=True` 分支（`LIMIT max_entries`），修 E-1；`top_k*20` 的启发式删除。

### 7.4 可回滚

`long_term.retrieval: bm25 | overlap`，默认 `bm25`；`overlap` 保留旧 `score()` 一个版本，仅用于 A/B 与回滚。切换不改数据，只改 `search()` 分支。

## 8. 长期写入摘要化（S5）

### 8.1 三种模式

`long_term.summarize: off | extractive | model`，默认 `extractive`。

- `off`：现状（原文截断）；
- `extractive`：规则抽句，**不调模型**，离线可用、可测；
- `model`：一次小模型调用（`max_tokens=256`、超时 3s），失败回落 `extractive` 并发 `MEMORY_WRITE_END(state=degraded, reason=...)`。

### 8.2 抽取规则（`memory/summarize.py`）

按 `。！？\n` 切句，按优先级保留至 240 字：

1. 含路径 / 命令 / 源码后缀的句子（复用 `candidate._SIGNAL`）；
2. 含约定词（默认 / 以后 / 记住 / 约定 / 必须 / 不要）；
3. 含结论词（因此 / 所以 / 结论 / 根因 / 修复方案）；
4. 用户输入中最长的句子（兜底：至少留下「问的是什么」）。

后处理：去首尾代词（这 / 那 / 它 / 上述 / 该），合并重复主语；结果为空则退回原文前 240 字并标 `degraded`。

### 8.3 条目类型

`kind ∈ fact | procedure | decision`：

- `decision`：命中约定词，且含"默认/以后/一律/不许"；
- `procedure`：含 ≥2 个命令 token 或编号步骤；
- `fact`：其余。

`kind` 参与检索加权（`decision` ×1.2），并在注入时标注前缀（决策优先于事实被读到）。

### 8.4 去重与强化

- 归一化：小写、去标点空白、中文保留字序。
- 相似判定：字符 3-gram Jaccard ≥ 0.8 → 视为同一条：`hits += 1`、`last_hit_at = now`、`text` 保留更长的那个（信息不减），不新增行。
- 表结构迁移（schema v+1）：`hits INTEGER DEFAULT 0`、`last_hit_at TEXT`、`norm_text TEXT`（唯一索引 `(scope, scope_id, norm_text)`）。
- 淘汰改为 `ORDER BY COALESCE(last_hit_at, created_at) DESC`——从「最旧创建」变成「最久未用」。

## 9. 配置与兼容

```yaml
memory:
  short_term:
    dedup_reads: on_pressure      # never | on_pressure | always（原 bool）
  long_term:
    retrieval: bm25               # bm25 | overlap（回滚用）
    summarize: extractive         # off | extractive | model
    summary_max_chars: 240
  context:
    note_max_chars_hard: 1200     # 硬上限；实际额度由 §4.5 派生
```

兼容：

- 旧 `dedup_reads: true/false` 在加载时映射为 `always/never`，**不报错**（存量 `config.db` 里是布尔）。
- 新增键全部带默认值，不加 `extra="forbid"`（SPEC-0013 §18.8 的教训：残留键会把配置打成无效）。
- `token_calibration.json` 缺失时用 `config/model.yaml` 的族系数，不视为错误。

## 10. 验收标准

| 编号 | 断言 | 方式 |
|---|---|---|
| A24 | 估算偏差：6 种文本形状 × 5 段，单段 ≤5%，总额 ≤2%；SPEC-0015 §6.2 的三条基线断言继续通过 | `fixtures/token_calibration.json` 与 L0 真值比对 |
| A25 | 预算执行：20 轮历史（每轮含 3k 字工具结果）→ 裁剪后 `tokens ≤ history_limit` | 纯函数用例 |
| A26 | S0 零改动：未超预算时 `trimmed is messages`（同一对象） | 身份断言 |
| A27 | 前缀不变量：同轮两次调用 `prefix_fingerprint` 相同；note 只在 `messages[-1]` | 结构化断言 + 单测 |
| A28 | dedup 默认 `on_pressure`：未超预算时历史中段无改动（`history_changed_at == -1`） | 两次调用比较 |
| A29 | BM25 排序：20 条语料 × 5 query，nDCG@5 ≥ 0.8 且优于 `overlap` | 排行对比 |
| A30 | 全量召回：写 60 条后第 1 条仍可召回（修 E-1） | 回归用例 |
| A31 | 阈值：fixture 上 `min_score=0.30` 时 precision ≥0.8、recall ≥0.6 | 阈值扫描 |
| A32 | 摘要写入：寒暄→不写；含路径短句→写；1000 字原文→条目 ≤240 字 | 三组用例 |
| A33 | 摘要去重：同义改写（3-gram 0.85）只留一行且 `hits=2` | 写入两次 |
| A34 | 事件与接口：`MEMORY_TRIM` 含 `stage/limit/over_budget`，`CONTEXT_ASSEMBLED` 含 `cache`，`/health` 暴露 `memory.tokens.drift` 与 `retrieval` | 契约测试 |

## 11. 实施顺序与文件预算

| 步 | 内容 | 触及文件 | 预计行数 |
|---|---|---|---|
| S1 | 分族系数 + `count(messages)` + L0 校准 + 收敛计数点 | `tokens.py`（改写 ~120）、`compression.py`（删重复解析） | +120 / −30 |
| S2 | `trim_messages(max_tokens=…)` + 五阶段报告 + `dedup_reads` 三态 | `short_term.py`（~230）、`observer.py`、`memory_policy.py` | +160 |
| S3 | 三段指纹 + `cache` 段事件 + V1–V5 契约测试 | `observer.py`（~300） | +80 |
| S4 | `bm25.py`（新）+ 全量取数 + 阈值重标 + `retrieval` 开关 | `bm25.py`（~150）、`long_term.py`、`repository/memory.py` | +200 |
| S5 | `summarize.py`（新）+ schema 迁移 + 去重强化 | `summarize.py`（~140）、`repository/schema.py` | +180 |

单文件 ≤500 行：`observer.py` 当前 235 行，S3 后约 300 行，安全；`short_term.py` 当前 191 行，S2 后约 250 行。若 `bm25.py` 超 200 行，把分词器拆到 `memory/tokenize.py`。

## 12. 评测影响

| 赛道 | 影响 | 处置 |
|---|---|---|
| MT（多轮） | 预算执行会更早淘汰旧轮次 | 重跑基线；对比「信息保留」是否有回归，回归则调 `headroom` 而非放宽限额 |
| MS（记忆稳定性） | 注入上限由 720 字改为派生额度，实质不变 | 无需改用例 |
| CP（压缩保真） | 摘要写入改变注入文本 | 重跑；断言改用「关键产物是否仍在」 |
| RT（召回） | BM25 替换 overlap，指标口径变化 | 记录 nDCG@5 / recall@5 两版，作为后续基线 |
| NB（噪声） | `junk_rate` 期望下降（摘要在写入侧过滤） | 作为本 spec 的主要收益指标 |
| PR（前缀复用） | 首次具备可测指标 `cached_prefix_tokens` | 新增用例：连续 5 轮同一前缀，命中率 ≥95% |

## 13. 风险与回滚

| 风险 | 触发条件 | 回滚 |
|---|---|---|
| 预算执行丢关键早期信息 | MT 赛道得分下降 >5% | `history_limit` 临时 ×1.2（配置项，不改代码）；根因是 `headroom` 过小 |
| BM25 中文二元组字典膨胀 | 单 scope 词表 >20k | `max_entries` 上限不变即可；索引不落盘，内存峰值 <2MB |
| 归一化阈值选错导致召回骤降 | RT recall@5 下降 >10% | `retrieval: overlap` 一行配置回滚 |
| 摘要丢细节 | CP 赛道产物丢失 | `summarize: off` 回滚；条目保留 `source_turn` 便于追原文 |
| 估算漂移误报 | `|α−1| > 0.15` 频繁出现 | 只影响观测，不影响执行；按族改 `family` |

## 14. 被替代条款

| 原条款 | 处置 |
|---|---|
| SPEC-0013 §5.4「估算只影响预算分配，不影响是否压缩」 | 保留「不影响压缩触发」，但预算**开始影响裁剪**（§5） |
| SPEC-0013 §5.6「禁止把变化内容插入历史中间」 | 由建议升级为契约 V1–V5 + 指纹验收（§6） |
| SPEC-0013 §4.3 长期写入「回合原文」 | 由 §8 摘要化替代，`summarize: off` 可回到旧行为 |
| SPEC-0015 §6.5 token 带宽（±12% 手工 run / fixture 8%） | 收紧为 §10 A24 的 5%/2%，并扩为分形状；fixture 与 `CONTEXT_ESTIMATE_DELTA` 事件保留 |
| SPEC-0013 §5.5「分段预算：中期与长期各占 note 的一档」 | 保持分段，但额度基准从固定的 `note_max_chars` 改为 §4.5 的派生值 |

## 15. 依赖与前置

- 若 PR #132（SPEC-0015 审计整改）先行合入，`recall` 契约是 `RecallResult(card, hits, degraded, reasons)`，§7.3 的 `Hit` 增字段与之合并；若未合入，按当前三元组 `(card, hits, error)` 实现，本 spec 不依赖其中任何一方。
- S1 会重写 `tokens.py` 并删掉 `compression.py` 里的重复阈值解析，两者都是 #132 的 D 组改动范围。**建议先把 #132 合入再开 S1**，否则同一文件的二次冲突会把「谁的口径为准」重新变成问题（本 spec §4 已声明以 #132 为基线扩展）。
- 与本 spec 无关但会撞文件：#126（记忆不变量）、#124（权限档）。S2 与 S3 都改 `observer.py`，建议两步同分支顺序提交，避免二次冲突。
