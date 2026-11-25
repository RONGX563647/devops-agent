# AGENTS.md — 本仓库的智能体协作契约

适用对象：所有在本仓库工作的 AI 编码智能体（Claude Code / Codex / WorkBuddy / Copilot 等）及人工开发者。
本文件描述**强制流程**，不是建议。违反这些规则的改动不应被创建、审核或合并。

---

## 1. 铁律：一切改动走 worktree 分支 + PR

`main` 分支受 GitHub 分支保护：强制 PR、`quality` CI 必过、禁止强推与删除、管理员同样受约束。
因此：

- **禁止在 `main` 上直接编辑、提交或 push 任何文件。**
- **每个任务必须在独立的 git worktree + 独立分支上开发**，无论改动多小（哪怕只改一个标点）。
- **唯一进入 `main` 的路径是合并 PR。**
- 禁止 `git push --force` 到任何共享分支；禁止带 `--no-verify` 绕过检查。

---

## 2. 标准开发流程（每个任务从头到尾执行）

### 2.1 建 worktree

分支命名：`<type>/<short-slug>`，type 取 `feat` / `fix` / `docs` / `refactor` / `test` / `chore`。
worktree 统一放在 `workspace/worktrees/<slug>`（该目录已被 `.gitignore` 排除，不会污染主仓库）。

```bash
git fetch origin
git worktree add -b feat/xxx workspace/worktrees/xxx origin/main
cd workspace/worktrees/xxx
```

**开始前先确认当前目录确实在 worktree 里**：

```bash
git rev-parse --show-toplevel   # 必须以 /workspace/worktrees/ 结尾
git branch --show-current       # 必须是 feat/xxx，不能是 main
```

任何一步不在 worktree 或分支是 `main`，立即停下，不要写任何代码。

### 2.2 开发与本地验证

在 worktree 内完成改动后，**提交前**必须跑通全部本地质量门禁：

```bash
.venv/Scripts/python.exe -m ruff check .        # 风格（对应 CI 第 1 步）
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy src            # 类型（CI 第 2 步）
.venv/Scripts/python.exe -m vulture src --min-confidence 80  # 死代码（CI 第 3 步）
.venv/Scripts/python.exe -m pytest --cov=devops_agent --cov-report=term --cov-fail-under=80  # CI 第 5 步
```

> 注 1：worktree 目录没有自己的 `.venv`，请用主仓库的 `E:/code/devops-agent/.venv` 绝对路径。
>
> 注 2（**重要陷阱**）：`pip install -e` 生成的 `.pth` 指向主仓库的 `src/`，所以在 worktree 里直接跑 pytest，
> 覆盖率报告里显示的仍是 `E:/code/devops-agent/src/...`——即**测的是主仓库源码，不是 worktree 里的改动**。
> 要让测试真正加载 worktree 的源码，在 worktree 内显式覆盖：

```bash
PYTHONPATH=src E:/code/devops-agent/.venv/Scripts/python.exe -m pytest --cov=devops_agent --cov-report=term --cov-fail-under=80
```

> 注 3：CI 在 Ubuntu + Python 3.11 上跑，本地 3.13 通过不等价于 CI 必过，因此**以 CI 结果为准**。

覆盖率门槛：任何新增源码分支都要有测试，`--cov-fail-under=80` 不得放宽。

文件行数棘轮（源码单文件 ≤500 行，存量基线只能下降）：

```bash
python scripts/check_file_size.py
```

### 2.3 提交与推送

```bash
git add -A
git commit -m "<type>: <一句话描述>"       # 例：fix: 修正权限规则分桶类型
git push -u origin feat/xxx
gh pr create --fill --base main
```

提交信息用 `<type>: <描述>`，type 与分支前缀一致。每个 PR 只做一件事，保持小而可审。

### 2.4 等待 CI 与合并

- PR 创建后确认 `quality` check 为绿色；红色则在工作树里修复、再 push，PR 会自动更新。
- 本仓库 `required_approving_review_count=0`（单人仓库不强制自审），但**CI 必须通过**才能合并。
- 合并用 squash，保持 main 历史线性：

```bash
gh pr merge <num> --squash --delete-branch
```

### 2.5 清理 worktree

```bash
cd E:/code/devops-agent
git worktree remove workspace/worktrees/xxx
git fetch origin && git checkout main && git pull
```

---

## 3. 架构约束（写代码时遵守）

1. **框架使用**：允许直接使用 AgentScope / OpenJudge 的类型与 API，不再维护 `import agentscope`
   的白名单；第三方 pip 依赖 `openjudge`（`py-openjudge`）仍只在 `devops_agent/openjudge/` 内
   import，保持可选依赖隔离——没装 `[openjudge]` extra 时其余功能不受影响。分层依赖仍由
   `.importlinter` + `lint-imports` 强制（`prompt_lint` / `input` / `observability` 不直接依赖 `agentscope`，
   网关不反向依赖 runtime/observability）。`runtime/events.py` 仍靠 duck typing 不 import 框架，
   以保持事件转译可用假事件测试。现有代码不为此规则重构。
2. **编排与执行分离**：`runtime/` 只做编排，`executor/boundary.py` 管沙箱执行面。
3. **过程态与源码分离**：运行态写 `workspace/`（已 gitignore），源码改动才进 git/PR。
4. **能力用内置、不手搓**：权限、沙箱、记忆、Pipeline 一律用 AgentScope 2.x 内置类，
   新增能力先 introspect 真实签名再写，不要凭记忆猜 API。
5. **未知即占位**：缺参数或缺文件时用占位符并继续交付，不要停下来只提问。
6. **证据优先**：不编造哈希、偏移量、测试结果；未运行的步骤显式标注 `NOT RUN` 和复现命令。
7. **注释规范与单文件行数**：
   - 源码文件（不含测试）单文件硬上限 **500 物理行**，由 `scripts/check_file_size.py` +
     `config/gates.yaml` 执行；存量超标文件记录在 `config/size-baseline.json`（当前为空），
     只能下降。新增超限文件必须先拆职责，禁止用 `eslint-disable`、压缩空行或堆注释规避。
   - 注释分三层：模块 docstring 写不变量与长「为什么」；函数 docstring 一句话职责，
     超过 20 行的编排函数写 3–6 条编号步骤；行内注释只写顺序、不变量、反直觉实现，
     单条不超过 3 行，禁止复述代码。长解释放 docstring / ADR，不在函数中间堆段落。

---

## 4. 自我检查清单（提交前逐条确认）

- [ ] `git rev-parse --show-toplevel` 指向 worktree，不是主仓库根目录
- [ ] 当前分支不是 `main`
- [ ] 本地质量门禁全部通过（含文件行数棘轮）
- [ ] 新增代码有测试覆盖，覆盖率门禁未被放宽
- [ ] 源码文件 ≤500 行（超限已拆分）
- [ ] 编排函数有步骤注释且行内长注释已收敛
- [ ] 提交信息符合 `<type>: <描述>`
- [ ] 没有 force push、没有 `--no-verify`
- [ ] CI `quality` 通过后才请求合并
- [ ] 合并后 worktree 已清理，主仓库 main 已更新

---

## 5. 违规处理

发现改动直接在 main 上发生、或绕过 CI 合并：视为流程事故。回滚该提交
（`git revert`，不要 rewrite 共享历史），在分支上以合规 PR 重做，并在 PR 描述里说明原因。
