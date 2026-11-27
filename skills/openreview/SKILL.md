---
name: openreview
description: |
  Run line-level AI code review on local git changes with Alibaba's Open Code Review CLI (`ocr`).
  MANDATORY trigger: review my changes / 评审这次改动 / 提 PR 前自审 / 看看这个 diff 有没有问题 /
  review --from main / 安全与并发排查。Produces findings anchored to real file:line, graded P0–P3,
  aligned with this repo's AGENTS.md gates.
  DO NOT trigger for: 解释代码在干什么、写测试、修 bug、重构实施（那些是 develop 任务，评审只是其中一步）；
  也不要用它替代 CI 门禁（ruff / mypy / pytest / check_file_size）。
argument-hint: ["[--from <ref>] [--to <ref> | --commit <sha>] [--effort low|medium|high] [--path <dir>]"]
---

# Open Review（阿里 Open Code Review）

`ocr` 是阿里开源的 AI 代码评审 CLI：确定性管线负责选文件、分组、规则匹配、行号定位，模型只负责判断。
它评的是 **git diff**（`review`）或 **整份文件**（`scan`），不做仓库改造。

适用：提 PR 前自审、评审别人的分支、关键模块上线前排查、CI 之外的本地第一道筛。
不适用：让它修代码、跑测试、替代 `quality` 门禁——它是**筛子，不是防线**（官方基准明确 Recall 偏低）。

## 0. 前置检查（每次评审先跑，失败就别继续）

```bash
command -v ocr && ocr version        # 缺则 npm i -g @alibaba-group/open-code-review
ocr llm test                         # 连通性；返回失败先修 provider，不要在没连通时假装评审过
```

本机基线：`ocr` v1.14.3，provider = dashscope / qwen3.8-flash（配置在 `~/.opencodereview/config.json`）。
没配 key 或走不通时，直接转 §4 委派模式，不要卡住。

## 1. 定评审范围（先 `--preview`，零成本确认审哪些文件）

| 场景 | 命令 |
|---|---|
| 工作区已暂存 / 未暂存 / 未跟踪改动 | `ocr review` |
| 当前分支 vs 主干（merge-base 模式） | `ocr review --from origin/main` |
| 两个分支之间 | `ocr review --from origin/main --to feat/x` |
| 单个 commit（对比其父提交） | `ocr review --commit abc123` |
| 没有 diff，扫整份文件 | `ocr scan --path src/devops_agent/runtime` |
| 只想看会审哪些文件 | 任意命令加 `--preview` |

`--path` **不要传空值**：空参数会回落到扫全仓（已知坑，`--path` 不传与传空是两回事）。

## 2. 执行（agent 场景的固定姿势）

```bash
ocr review --from origin/main \
  --audience agent \
  --format json --output /tmp/ocr-review.json \
  --effort medium --timeout 15
```

每个参数的理由：

- `--audience agent`：只留摘要，进度行不灌进上下文；给人看时用默认 `human`。
- `--format json --output`：结果落盘，避免整段文本吃上下文，再按需抽取（见 §3）。
- `--effort low|medium|high`：low 快速自检，medium 常规，**high 只给碰钱、碰权限、碰并发的改动**。
  深度越高越会追着已发现问题继续挖，收敛即停，不会无限烧 token。
- `--background "..."` 或 `-B <file.md>`：把业务背景喂进去，官方把这条列为提升质量的首选手段。
  有 PR 描述或 spec 时优先用 `-B`。
- `--concurrency 8` 是默认；撞限流就调低。`--timeout` 是并发任务总预算（分钟）。
- `--max-tokens-budget`：给整次评审封顶；只截断不报错的场景退出码仍为 0。

## 3. 读结果

- **text**：`─── file:line-line ───` + 意见正文；末尾 `Summary: N file(s), M comment(s), ~T tokens, Ss`。
- **json**（v1.14.3 实测结构，顶层）：

  ```
  status      // complete | partial —— partial 表示覆盖不全（如预算打满），不是「没问题」
  llm         // { provider, model }
  message     // 一句话结论，含 N finding(s) / 覆盖了几分之几
  summary     // { files_reviewed, comments, total_tokens, input_tokens,
              //   output_tokens, cache_read_tokens, elapsed, budget_exceeded }
  comments[]  // { path, start_line, end_line, severity, category,
              //   content, existing_code, thinking }
  groups / tool_calls / warnings / session_id / manifest
  ```

  `severity` 实测取值 `high|medium|low`，`category` 实测 `bug|maintainability|performance`。

- **`thinking` 字段极长**（实测 13 条意见：`content` 合计 10 KB，`thinking` 合计 170 KB）。
  整条读进来会把上下文吃光，**只取需要的字段**：

  ```bash
  jq -r '.comments[] | "\(.severity)\t\(.path):\(.start_line)-\(.end_line)\t\(.category)"' /tmp/ocr-review.json
  jq -r '.comments[0].content' /tmp/ocr-review.json          # 要看正文再单独取
  jq -r '.summary, .status' /tmp/ocr-review.json
  ```

- **退出码**：`0` = 拿到可用结果（含预算截断的部分覆盖）；非 `0` = 运维失败（超时、provider 错误）或整体失败。
  非 0 时**不准**把「评审通过」写进结论。
- **`status: partial` 必须如实转述**：实测 300k 预算下跑出 532k token、16 个文件只审完 8 个，
  退出码仍是 0。这种情况只能写「部分覆盖，未审到的文件列出来」，不能写「评审通过」。
- **`--analysis`**：只对 commit / range 生效，额外产出 Behavior / Impact / 维护者决策三段；
  Analysis 不完整只告警，不改变退出码。

## 4. 委派模式（没 key，或想让宿主模型自己判）

```bash
ocr delegate preview --from origin/main --format json   # 该审哪些文件 + mode/ref 元数据
ocr delegate rule src/devops_agent/runtime/agent.py     # 这些文件命中的评审规则（按内容分组）
```

`delegate` 只出**评审规格**，不调 LLM：文件选择、规则匹配仍由确定性管线保证，
分析判断交给宿主 agent。注意委派不等于免费——宿主自己的用量照算。

## 5. 本仓库的判据（评审意见必须对齐这些，冲突时以 AGENTS.md 为准）

1. **500 行硬上限**：`src/` 单文件超限要拆，不能靠压空行或堆注释规避（`scripts/check_file_size.py`）。
2. **分层依赖**：`.importlinter` 约束；`prompt_lint` / `input` / `observability` 不直接依赖 `agentscope`；
   网关不反向依赖 runtime/observability；`runtime/events.py` 保持 duck typing。
3. **可选依赖隔离**：第三方 `openjudge` 只在 `devops_agent/openjudge/` 内 import。
4. **注释三层**：模块 docstring 写不变量与「为什么」；函数 docstring 一句话职责；
   行内注释只写顺序 / 不变量 / 反直觉实现，单条 ≤3 行，禁止复述代码。
5. **流程铁律**：改动必须走 worktree + PR，禁止直推 `main`、禁止 `--no-verify`。
6. **前端改动**：`web/**` 必须走 modern-web-guidance skill。
7. **测试**：新增源码分支要有测试，覆盖率门槛 `--cov-fail-under=80` 不得放宽。

违反 1/2/5 的按 **P1** 报，违反 3/4 的按 **P2** 报。

## 6. 输出纪律

- 每条意见固定三段：`path:line` → 问题是什么 → 为什么是问题（触发条件 / 后果），再给改法。
- **行号只认来源**：`ocr` 输出或 diff hunk 的行号为准；对不上就重新定位，宁可写「大致位置」也不编造。
- 分级：**P0** 阻断（会出错、会泄密、会丢数据）/ **P1** 应修（违反仓库铁律、明显缺陷）/
  **P2** 建议（可读性、重复、边界收紧）/ **P3** 可选。
- 不堆泛泛表扬，不复述代码，不把「看着没问题」写成结论——没查到就说查了哪些面、哪些没覆盖。
- 没跑的验证标 `NOT RUN` 并给复现命令。
- 已知 bug：`scan` 模式下部分发现 `line=0`（无法锚定行号）。要精确行号就用 `review`，别在 `scan` 结果上做自动化消费。

## 7. 故障对照

| 现象 | 处理 |
|---|---|
| 401 / 403 | 查 key 与 `ocr config set providers.<p>.api_key`；先 `ocr llm test` |
| 超时 | 降 `--effort`、减 `--concurrency`、加 `--timeout` |
| 预算打满 | 加 `--max-tokens-budget`，或缩范围（`--path` / `--exclude`） |
| 结果为空 | 先 `--preview` 看是否真选到文件；`--preview` 会标 `excluded: unsupported_ext`（**`.md` 等非代码文件不参与评审**，纯文档改动本来就没有可审内容）；再看 `--exclude` 是否把目标排掉了 |
| 参数疑似被吞 | `--batch` 拼错值会静默按默认跑；跑完先核对自己拼的参数 |
| 想查某文件命中哪条规则 | `ocr rules check <path>` |

## 8. 成本与边界

官方 AACR-Bench：同模型下 Precision / F1 高于通用 Agent，token 约为 1/9，但 **Recall 更低且是刻意的**
（宁漏报不误报）。所以：日常随手审、PR 前第一道筛可以放心用；**涉及钱、权限、数据一致性的改动必须人工复核**。

## 参考

`references/cli-reference.md` —— 各子命令完整参数表（取自 `ocr --help` 实测输出）。
