# 开发流程与质量门禁

本页是 [AGENTS.md](../../AGENTS.md) 的可执行版本：列出每一步实际跑什么命令、
在哪一步容易出错、失败时看哪里。

## 1. 铁律

`main` 受 GitHub 分支保护：强制 PR、`quality` CI 必过、禁止强推与删除。

- 禁止在 `main` 上直接编辑、提交或 push。
- 每个任务在**独立 worktree + 独立分支**上开发，改动再小也一样。
- 唯一进入 `main` 的路径是合并 PR。

## 2. 建 worktree

分支名 `<type>/<slug>`，type 取 `feat` / `fix` / `docs` / `refactor` / `test` / `chore`。
worktree 统一放 `workspace/worktrees/<slug>`（已被 `.gitignore` 排除）。

```bash
git fetch origin
git worktree add -b feat/xxx workspace/worktrees/xxx origin/main
cd workspace/worktrees/xxx
```

写代码前先确认两件事：

```bash
git rev-parse --show-toplevel   # 必须以 /workspace/worktrees/ 结尾
git branch --show-current       # 必须是 feat/xxx，不能是 main
```

任何一条不满足就停下，不要继续。

## 3. 本地五道门禁

在 worktree 内跑，用主仓库的 venv 绝对路径（worktree 没有自己的 `.venv`）：

```bash
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m ruff check .
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m ruff format --check .
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m mypy src
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m vulture src --min-confidence 80
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m pytest --cov=devops_agent --cov-report=term --cov-fail-under=80
```

**`PYTHONPATH=src` 不是可选的**：`pip install -e` 生成的 `.pth` 指向主仓库的 `src/`，
不覆盖的话覆盖率报告里显示的是 `E:/code/devops-agent/src/...` ——
即**测的是主仓库源码，不是 worktree 里的改动**。

`--cov-fail-under=80` 不得放宽；新增源码分支必须带测试。

> CI 在 Ubuntu + Python 3.11 上跑，本地 3.13 通过不等价于 CI 必过，**以 CI 为准**。

## 4. CI 三段

### 4.1 `quality`（`.github/workflows/ci.yml`）

顺序即门禁顺序，前面失败后面不跑：

| 步骤 | 命令 | 说明 |
|---|---|---|
| 行尾门禁 | `git ls-files --eol` | `.gitattributes` 规定全仓库 LF；CRLF 或混用直接拦下（二进制显示 `none` 放行） |
| lint | `ruff check .` | |
| type check | `mypy src` | |
| 质量门禁 | `python scripts/check_gates.py` | 读 [config/gates.yaml](../../config/gates.yaml)，实际执行覆盖率 / 死代码 / 复杂度（`--with-mutation` 才加变异测试） |
| 题库校验 | `python -m devops_agent.openjudge validate --strict` | 校验用例结构，并比对 `coverage.json` 覆盖率基线（只许涨） |
| 契约 lint | `python -m devops_agent.prompt_lint --known-issues eval/lint-baseline.json` | P1–P4；存量违规显式记账，修好后不删清单项会以 `[error] baseline` 失败 |

### 4.2 `frontend`

`working-directory: web`，node 24：`npm ci` → `npm run lint` →
`npm run typecheck` → `npm run test:coverage` → `npm run build`。

### 4.3 阈值来源

[config/gates.yaml](../../config/gates.yaml) 是唯一来源，`check_gates.py` 与前端都读它：

```yaml
quality_gates:      # 后端
  coverage_fail_under: 80
  complexity_threshold: 10
  mutation_score: 70
  dead_code_min_confidence: 80
frontend_gates:     # web/
  coverage_fail_under: 65
  complexity_threshold: 10
```

`check_gates.py` 会把本次 checkout 的 `src` 插到 `sys.path` 最前，
确保读到的是本仓库的配置层而非 editable install 指向的副本。

## 5. Nightly 评测（`.github/workflows/openjudge-nightly.yml`）

- 触发：`cron "30 3 * * *"`（UTC 03:30 / 北京时间 11:30）+ `workflow_dispatch`；
  `timeout-minutes: 90`（五赛道 90 例真要跑模型）。
- 步骤：`openjudge validate --strict` → `openjudge run --concurrency 4` →
  上传 `summary.json` / `report.md` / `traces.jsonl`（`if: always()`）。
- 回归检测靠 `compare`：只要库里有上一批，`--base/--head` 就能出 delta；
  指纹（`suite_sha` / `plan_sha` / `rubric_sha` / `model`）不一致时判 `not_comparable`
  而不是退步 —— 换了实验不该用旧基线卡。
- 退出码：`fail` 非零；`insufficient`（该赛道未达 `min_n`）为 0。

裁判可关（`--no-judge`）：门禁里的规则部分不花额度，先在 PR 里跑规则、nightly 再跑
完整口径，是刻意的分层。

## 6. 提交与合并

```bash
git add -A
git commit -m "<type>: <一句话描述>"    # 例：docs: 依据当前代码更新 README 并新增 wiki
git push -u origin docs/xxx
gh pr create --fill --base main
```

- 提交信息 `<type>: <描述>`，type 与分支前缀一致；一个 PR 只做一件事。
- 等 `quality` 为绿；红色就在 worktree 修，push 后 PR 自动更新。
- `required_approving_review_count = 0`（单人仓库），但 CI 必须通过才能合并。
- 合并用 squash，保持 main 历史线性：

```bash
gh pr merge <num> --squash --delete-branch
```

禁止 `git push --force` 到任何共享分支，禁止 `--no-verify` 绕过检查。

## 7. 清理

```bash
cd E:/code/devops-agent
git worktree remove workspace/worktrees/xxx
git fetch origin && git checkout main && git pull
```

## 8. 提交前自检

- [ ] `git rev-parse --show-toplevel` 指向 worktree，不是主仓库根
- [ ] 当前分支不是 `main`
- [ ] 五道本地门禁全部通过（带 `PYTHONPATH=src`）
- [ ] 新增代码有测试，覆盖率门槛没被放宽
- [ ] 提交信息符合 `<type>: <描述>`
- [ ] 没有 force push、没有 `--no-verify`
- [ ] CI `quality` 绿后才合并
- [ ] 合并后 worktree 已清理、主仓库 main 已更新

## 9. 架构约束（写代码时遵守）

1. **框架隔离**：`import agentscope` 只允许出现在 9 个装配模块
   （`executor/boundary.py`、`governance/policy.py`、`memory/ledger.py`、
   `runtime/assembly.py`、`runtime/compression.py`、`runtime/profile_note.py`、
   `runtime/supervisor.py`、`secrets/middleware.py`、`tools/catalog.py`）。
   新增装配点时集中在此边界内并同步更新清单（AGENTS.md §3.1 目前只列了 8 个）。
2. **编排与执行分离**：`runtime/` 只做编排，`executor/boundary.py` 管执行面。
3. **过程态与源码分离**：运行态写 `workspace/`（gitignore），源码改动才进 git/PR。
4. **能力用内置**：权限、沙箱、记忆、Pipeline 一律用 AgentScope 2.x 内置类；
   新增能力先 introspect 真实签名，不要凭记忆猜 API。
5. **未知即占位**：缺参数或缺文件时用占位符并继续交付，不要停下来只提问。
6. **证据优先**：不编造哈希、偏移量、测试结果；未运行的步骤显式标 `NOT RUN` 并给复现命令。

## 10. 违规处理

发现改动直接在 `main` 上发生、或绕过 CI 合并：视为流程事故。
用 `git revert` 回滚（不要 rewrite 共享历史），在分支上以合规 PR 重做，
并在 PR 描述里说明原因。
