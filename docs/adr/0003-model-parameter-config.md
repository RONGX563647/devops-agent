# ADR-0003：模型参数三层可配（会话覆盖 / 部署默认 / 只读凭据）

- 状态：Accepted
- 日期：2026-11-24
- 决策者：项目所有者
- 相关：SPEC-0001（`docs/specs/0001-model-parameter-config.md`，本 ADR 的实现依据）、
  ADR-0002（「变了就重建」的判据先例与档位热替换手法）、ADR-0001（观测与回归对比口径）、
  `config/model.yaml`、`config/policy.yaml`

## 背景

### 现象：13 个模型参数散在四个地方，其中一半根本没人设

`policy.yaml` / `health.yaml` / `gates.yaml` 早就有了，唯独模型参数没落进 yaml：`max_iters` 与
`agent_max_retries` 在 `policy.yaml`，`stream=True` 与 `_DEFAULT_TRIGGER_RATIO=0.8` /
`_DEFAULT_CONTEXT_SIZE=128000` 在代码常量里，而 `temperature` / `max_tokens` / `top_p` /
`reasoning_effort` —— `build_chat_model()` 压根没传 `parameters`，全部走供应商默认。

配置散落本身只是难看。真正的理由是实现过程中核实出的三个缺陷。

### 缺陷一：`context_size=128000` 是猜的，猜错就出事

它同时决定三件事：压缩阈值（`0.8 × 128000 = 102400`）、前端上下文圆环的分母、压缩后保留多少。
如果 `qwen3.8-flash` 的真实窗口是 1M，等于把 87% 的窗口权重浪费掉；如果真实窗口更小，
请求会在压缩触发前就 400。而这个值不是从任何地方读出来的 —— 它只是框架默认。

这个缺陷能藏这么久，是因为它**没有来源标注**：界面上写着 128,000，看不出是"部署者设的"
还是"框架塞的"。

### 缺陷二：两级重试叠乘，最坏 16 次请求只显示 1 次调用

`OpenAIChatModel(max_retries=3)`（框架默认，项目没传）× `ModelConfig(max_retries=3)`
（`policy.yaml` 传的）= `(1+3) × (1+3) = 16` 次尝试上限（含首次）。成本和延迟都是 16 倍，
而 span 里只有一条 `model` 记录。项目此前没有任何地方体现这个乘法关系。

### 缺陷三：参数不进 `runs` 表，一旦可配就不可归因

`runs` 只存模型名。健康分 v2 已改成「按当前口径重算」，如果参数可以随时改而运行记录里没有快照，
**参数变化点前后的分数与评测批次会混在一起算** —— 这正是 `prompt_factors()` 当初要解决的问题
（当时它只有 `prompt_sha` / `tools_sha` / `system_sha` 三个因子）。

## 决策

### 一、五层优先级，逐项解析并标注来源

```
会话覆盖（session_states.model_config）
  > 部署默认（settings['model_config']，前端改、持久化）
  > config/model.yaml
  > 环境变量（仅 model / judge_model / enable_search）
  > 框架默认（context_size / 两级重试 / ContextConfig）
```

新增 `src/devops_agent/model_settings.py`，`resolve()` 是**纯函数**：输入三份 dict，输出
`dict[str, Resolved(value, source)]`。约束是硬的 —— **任何地方需要参数只能走 `resolve()`，
不许自己读 yaml 或 env**。这样"这个值哪来的"永远只有一个答案，缺陷一不可能再隐性复发。

`source` 的五个取值（`session` / `stored` / `yaml` / `env` / `framework-default`）同时是
前端徽标的取值，前后端共用同一份数据，不各自翻译。

### 二、为什么不开放 `api_key` / `base_url`

`api_key`、`base_url`、`stream`、`judge_model` 出现在面板里但**禁用**，并写明在哪里改。

1. **凭据不该经 HTTP 往返。** 一旦开了 PATCH 口子，密钥就会出现在请求体、反向代理日志、
   浏览器 devtools、以及可能的错误上报里。当前的部署形态没有 TLS 终止、没有请求日志脱敏、
   没有认证 —— 这个口子开了就是明文密钥在链路上滚。
2. **写入后只能落明文 DB。** `settings` 表是 SQLite 明文，密钥进了这里就等于多一份散落副本，
   且备份/快照会带走它。要正经做应另开「凭据管理」并接密钥库，不属于配置面板的范畴。
3. **`stream=False` 是有意的只读。** 实测关掉流式后前端从逐字渲染退化成一次性出现
   （SET→DELTA 差 0.07ms），是体验倒退而非可选项。
4. **`judge_model` 属于评测侧配置。** 它影响的是评测批次的评分口径，与"这一轮对话怎么跑"
   是两件事，混进同一个面板会让评测结果的可比性依赖一个对话参数。

完全隐藏这四项也不对：用户会以为系统没有这个参数。所以采用「展示 + 说明 + 不可编辑」。

### 三、为什么把 `max_retries` 拆成两个

合成一个 `max_retries` 就是把「哪个重试」这件事藏起来，而它们的乘数效应正是缺陷二。
拆成 `model_max_retries`（供应商 HTTP 层）与 `agent_max_retries`（一轮 ReAct 内的模型调用重试）
之后，`derived.worst_case_attempts = (1+model) × (1+agent)` 才能被算出来并显示。

顺带一个必须同时做的事：`build_chat_model` 要把 `context_size` **显式**传给
`OpenAIChatModel(context_size=...)`。否则换模型时窗口不同步，表现为「窗口明明变大了，
却还是 10 万 token 就压缩」。

### 四、为什么用 KV 表而不是建 `model_settings` 表

```sql
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL,      -- JSON 文本
    version INTEGER NOT NULL DEFAULT 1,             -- 乐观并发
    updated_at TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT ''
);
```

- 以后 `health.yaml` 的权重、`gates.yaml` 的阈值若要下放到前端，复用同一张表，
  不必为每个配置域加一张表;
- `version` 一列同时承担两个作用：**乐观并发**（`PATCH` 带 version，不匹配返回 409）
  与**跨进程失效信号**（读缓存带 5 秒 TTL）。不需要为此引入 Redis 或 pub-sub;
- `updated_by` 当前固定写 `"local"`（没有身份体系），保留列是为了将来接认证时不用改 schema。

单进程单 worker 的现状下，进程内 5 秒缓存够用；多 worker 的 `deferred` 问题写进了 spec 的未决问题。

### 五、`null` 是「删除覆盖」，不是「设为 null 值」

`patch` 里的 `null` 表示删除该层的这一项、回落下层。理由是 `temperature` 等参数本身合法取值
就包含「不设置（用供应商默认）」：如果 `null` 表示"设成 null 值"，那"把温度改回默认"和
"温度设为 None"就无法区分，用户改不回去。`DELETE /api/session/{id}/model-config` 是同一语义的
批量版本。

### 六、推算值由后端算，前端不重复实现口径

`GET /api/model-config` 返回 `derived: {compression_threshold, worst_case_attempts,
context_uncalibrated}`。`compression_threshold = trigger_ratio × context_size`、
`worst_case_attempts = (1+model) × (1+agent)` 都由后端算好。

健康分 v1 的教训就在这里：同一口径在两处实现，迟早上线和后端不一致。前端只负责显示，
以及用 `compression_threshold` 与当前 `contextTokens`（圆环已有数据）比较，说出
「保存后会 / 不会立即触发一次压缩」。

### 七、部署默认的传播：applied / deferred，不谎称立即生效

改部署默认时两个极端都不对：只影响新会话 → 改完以为生效了其实还在跑旧参数；立刻广播到所有
会话 → 可能打断正在跑的 run。所以写 DB 后遍历 registry 逐会话判断：

```
lock 未持有（没有正在跑的 run）→ 立即热替换，计入 applied
lock 已持有                    → 标记 pending_model，run 的 finally 里 flush，计入 deferred
```

Echo / 测试替身没有 `apply_model_params` 时返回 `applied=false` + `effective_from`。
这是 `switch_session_permission` 已经建立的约定（`applied` 字段语义），沿用不另立说法。

### 八、run 记录只存非默认值，并进 `params_sha`

`runs.model_params` 存**实际下发的非默认项**（`{"temperature": 0.3, "model": "qwen3.8-flash"}`），
不存全部 12 项 —— 否则每行塞一份 JSON，且默认值调整后所有历史行都会被误判为"参数变了"。

`prompt_factors()` 增加 `params_sha`（对 `model_params` 做稳定 JSON sha256）。评测对比里它被归为
**不可比因子**，与 `prompt_sha` 同级：参数变了就该标「不可归因到提示词 / 可归因到模型参数」，
而不是当噪声。老记录的 `model_params` 是空串，读出来按「未记录」处理，**不假装是默认值**。

## 后果

### 正面

- 缺陷一、二在配置层落地时就修掉了（`context_size` 显式传递 + `context_uncalibrated` 可见化、
  两级重试拆开并显示乘积），不依赖 UI;
- 参数改动不再需要改代码或重启，且每次改动都能查到来源与生效范围;
- 参数进入 run 记录与评测因子，健康分可按参数切片，评测批次不会把参数变化混算成噪声;
- 新增配置域（`health` 权重、`gates` 阈值）有现成的 KV 表 + 版本并发 + 来源解析模式可复用。

### 负面 / 代价

- 多了一层解析（`model_settings.py` 约 470 行）与一张表、两个列；
- `settings` 的 5 秒 TTL 意味着跨进程收敛有延迟（单进程下无感）;
- 面板 13 行参数已接近 Popover 的容量上限，再加参数要重新设计形态;
- `context_calibrated` 需要部署者手工确认，默认值是"显式的猜测"而非"正确的值"。

### 未决（不在本 ADR 范围）

1. `qwen3.8-flash` 的真实上下文窗口未知，`model.yaml` 保持 128000 + `context_calibrated: false`;
2. `model` 名要不要白名单（需要 provider 的模型列表接口）;
3. 多 worker 下 deferred 会落空（假设单进程）;
4. `max_iters` 是否要按域分别暴露。

## 替代方案与放弃理由

| 方案 | 放弃理由 |
|---|---|
| 参数全部只读 yaml，重启生效 | 需求就是"前端能改"，重启才能改等于没解决；且每次调参都污染 git 历史 |
| 全部放环境变量 | 环境变量不能热更新，也无法表达「部署默认可由多会话共享并被会话覆盖」的三层语义 |
| 建 `model_settings` 专用表、每参数一列 | 加参数要改 schema + 迁移；且无法复用到 `health` / `gates` 的下放 |
| 用一个 `max_retries` 合成两级 | 藏起乘数效应，缺陷二无法被看见，调参时会把"便宜的重试"和"贵的重试"混为一谈 |
| `null` 当普通值（可以设成 null） | 无法区分「改回默认」与「设为不设置」，用户改不回去 |
| 前端自己算 `worst_case_attempts` / 阈值 | 口径两处实现必然漂移，健康分 v1 已验证这条路不通 |
| 保存时探测模型连通性 | 外部依赖抖动会让配置存不进去；探测应是显式动作（且会产生计费调用） |
| 用 Redis 广播配置变更 | 单进程单 worker，为 5 秒收敛延迟引入一个外部依赖不划算 |
