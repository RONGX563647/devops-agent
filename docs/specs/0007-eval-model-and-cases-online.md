# SPEC-0007：在线配置评测模型与在线修改评估用例

- 状态：已实施（随 PR `feat/eval-model-and-cases` 落地）
- 相关：`src/devops_agent/openjudge/{settings,casestore,api_edit}.py`、
  `src/devops_agent/openjudge/{grade,harness,run,service,api,cli,store}.py`、
  `web/src/pages/quality/studio/*`、`web/src/pages/quality/OpenJudge.tsx`、
  `tests/unit/test_openjudge_edit.py`

## 1. 背景

评测跑之前的两个动作仍然必须离开页面：

1. **换模型**要改环境变量 + 重启进程。被测模型走 `effective_params()`（日常对话那份配置），
   裁判模型更是只在 `DEVOPS_AGENT_JUDGE_MODEL` / `DEVOPS_AGENT_MODEL` 里找
   （`grade.judge_model()`）。于是「换一个裁判试试」的成本高于「忍受一个同源裁判」。
2. **改用例**要编辑 `eval/openjudge/cases/*.jsonl` 再提交。题库是 git 里的一份数据，
   想临时加一条用例看效果，得先决定「这条要不要进仓库」。

两件事叠加的结果是：页面只能看结论，任何实验都得回到终端。

### 1.1 为什么「能换」本身就是评测质量的一部分

裁判与被测模型同源时会系统性放水 —— 同源裁判对自己风格的补充信息更宽容。让裁判可换，
才谈得上做「同源 vs 跨源」的对照。这不是便利性问题。

## 2. 目标与非目标

### 目标

1. 页面能改被测模型与裁判模型，**不重启进程**，且不改动日常对话的模型配置。
2. 页面能改 / 增 / 隐藏 / 恢复一条用例，**不弄脏 `git status`**。
3. 非法输入在写入前被拒（422），磁盘零写入。
4. CLI 与页面读到同一份题（否则改完立刻对不上）；`validate` 的覆盖率口径不变。

### 非目标

1. 不做多用户 / 权限分级：单机单用户（沿用 ADR-0001）。
2. 不做表单化用例编辑器：先给 JSON 文本框 + 服务端校验，结构化编辑器等有人真的天天改用例再说。
3. 不覆盖采样参数：在线只换 model 名，温度 / 窗口仍走五层解析（见 §6.2）。
4. 不做题库版本管理 / 冲突合并：覆盖层是「本机运行态」，多人协作仍走 git。

## 3. 模型配置：为什么单开一张表

`settings['model_config']` 是日常对话的配置（`/api/model-config` 改的就是它）。
如果评测直接复用：

- 跑一次几十条用例会把额度打满，而「把日常配置改到评测模型再改回来」是双倍风险：
  中途崩溃 / 忘记改回，用户会以为自己的对话模型莫名其妙变了；
- 「只为评测指定模型」这个意图无法表达 —— 它会变成「改了全局配置」。

所以单开单行表 `oj_settings`（`openjudge/store.py` 的 `SCHEMA`），只放评测自己的开关：
`target_model` / `judge_model` / `judge` / `concurrency` / `timeout`。

### 3.1 空字符串是「不覆盖」，不是「用空模型」

这让「跟随默认」变得可表达，代价是读取方必须知道「最终会用什么」。因此
`GET /settings` 同时下发两份：

| 字段 | 含义 |
|---|---|
| `values` | 库里配的值（`""` 表示没配） |
| `effective` | 这一次真会用的模型名，按优先级算出来的 |

优先级：**显式参数 > `oj_settings` > 环境变量 / `/api/model-config`**。
前端把 `effective` 当 `placeholder` 与「实际生效：…」显示，用户不用猜空框意味着什么。

### 3.2 校验：未知字段必须报错

`validate()` 按键查 `_CHECKERS`（一字段一函数），未知字段直接 `SettingsError` →
422 且不落库。静默丢弃前端笔误（`judge_model_name`）的后果是「界面显示保存成功，
跑出来还是老模型」。`bool` 是 `int` 的子类，`concurrency` / `timeout` 显式排除 `bool`。

## 4. 用例：为什么写覆盖层而不是改 git 文件

`eval/openjudge/cases/*.jsonl` 是**覆盖率基线的口径**（`benchmark.compare_coverage()`
逐键比对，只许涨不许跌）。在线编辑如果直接改它：

- 页面删一条题 → 覆盖率掉 → `validate --strict` 红 → CI 红。而「我只是想临时看一条用例」
  完全不构成让 CI 变红的理由；
- 反过来说，谁改动过仓库题库也无法从 diff 看出意图。

落点改为 `workspace/openjudge/cases/`（运行态，`.gitignore` 已覆盖 `workspace/`）：

| 文件 | 作用 |
|---|---|
| `overlay.jsonl` | 按 `(track, id)` 覆盖仓库条目；仓库里没有同 id 就是新增 |
| `deleted.json` | 隐藏清单，只存 id |

读时合并「仓库 → 覆盖 → 去隐藏」。两个口径分工明确：

- `casestore.repo_cases()`：**只有仓库**。`validate` 与 CI 覆盖率用它，本地删题不会假红；
- `casestore.load_cases()`：合并后。在线跑、CLI 无 `--cases` 时用它。

### 4.1 写入前必须能执行

`casestore.check()` 复用 `benchmark.parse_case()` + 单条静态校验（结构 / 赛道 / fixture 存在）。
不合法返回 422 且**磁盘零写入**（测试有断言）。允许一条跑不起来的用例进题库，等于把它
推迟成采集阶段的一次崩溃 —— 那时候报错信息指向的是工作区，不是那条用例。

### 4.2 原子写

临时文件 + `os.replace`。半截 JSONL 会让整个题库读不出来，而题库读不出来时
`/suites`、`/coverage`、在线跑全部 500 —— 一次写坏的代价远大于一次写失败。

## 5. HTTP 契约（`api_edit.py`，prefix `/api/openjudge`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings` | `{values, effective, limits}` |
| PATCH | `/settings` | 部分更新，未知字段 / 越界 → 422 |
| GET | `/cases?track=&source=` | `{items, total, suite, thresholds, limits}` |
| GET | `/cases/{id}` | `{case, limits}`；不存在 404 |
| PUT | `/cases/{id}` | body.id 与路径不一致 → 422（不支持改名） |
| DELETE | `/cases/{id}` | 记为隐藏；`{id, deleted, suite}` |
| POST | `/cases/{id}/restore` | 撤销覆盖 / 取消隐藏 |

### 5.1 为什么单独一个模块

读接口失败是 500（库坏了），写接口失败是 422（输入不对）。混在一个 `api.py` 里会让
异常处理退化成「一次判两种错」，而且写接口需要 `_read` 之外的错误映射。

### 5.2 两个 id 空间不能同路径

- `/cases/{id}`：**题库**里的用例，`id` 是字符串（`[A-Za-z0-9._-]{1,64}`）；
- 批次内单条明细：`oj_cases` 的自增主键，挂在 `/batches/{batch_id}/cases/{case_row_id}`。

同路径不同方法会被 FastAPI 合并进同一 path item，只认先注册的那个 —— 这是实施过程中
真实踩到的 bug（`GET /cases/{case_row_id}` 与 `GET /cases/{case_id}` 互相遮蔽）。
`case_row_id` 必须校验 `batch_id` 归属，否则能用别的批次 id 读到别处的用例。

## 6. 流水线接线

### 6.1 只解析一次

`run.execute` 里 `models = settings.resolved(opts.target_model, opts.judge_model)` 解析一次，
trace 的 `model` 字段与批次的 `meta.judge_model` 都读它。分批解析会在一次跑里出现两个
模型名，`compare` 的 `not_comparable` 判断就会不可靠。

### 6.2 只覆盖 model 名

`harness._default_runtime()` 在给了 `target_model` 时走
`effective_params(session={"model": target_model})` —— 温度 / 窗口 / 工具集仍走正常五层解析。
理由：评测顺手改参数会让批次之间悄悄不可比。`compare` 靠 `meta.model` 判 `not_comparable`，
同模型换温度目前识别不出来（见 wiki 缺口表）。

## 7. 测试

`tests/unit/test_openjudge_edit.py`（33 例）重点不在「能存能读」，而在几件不该发生的事：

- 非法配置被静默接受（并发 99、模型名带空格、未知字段）；
- 非法用例落盘后才在采集阶段炸掉；
- 在线改用例把 git 里的题库文件也改了（断言 `repo_raw()` 零变化）；
- 删掉仓库用例后覆盖率基线假红（断言 `repo_cases()` 条数不变）；
- `_CHECKERS` 缺字段（新增配置项忘了写校验 → `KeyError` → 500）。

`tests/conftest.py` 的 autouse fixture 同时隔离 `DEVOPS_AGENT_OPENJUDGE_DB_PATH` 与
`DEVOPS_AGENT_OPENJUDGE_OVERLAY`：测试碰开发者真实题库是最不该发生的事。

## 8. 未决问题

| # | 问题 | 当前默认 | 备注 |
|---|---|---|---|
| 1 | 覆盖层是否要能「导出成仓库格式」 | 不做 | 现在只能手动复制 JSON；真要长期生效的题应走 git |
| 2 | 用例编辑是否要表单化 | 不做 | JSON 文本框 + 422 回显；结构化编辑器工作量远大于收益 |
| 3 | 是否允许在线改名（改 `id`） | 不允许，422 | 改名等于「删一条 + 加一条」，会让批次对比失去锚点 |
| 4 | 覆盖层是否需要并发写保护 | 不加锁 | 单机单用户；原子写已避免写坏，后写覆盖先写可接受 |
