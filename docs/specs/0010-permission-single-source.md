# SPEC-0010：权限层 —— 单一事实来源、职责拆分与敏感路径守卫

- 状态：Accepted
- 日期：2026-11-26
- 落地：PR #111（P1 结构）+ 本 PR（P2  guard）
- 相关：`docs/wiki/Permissions.md`、`.importlinter`、ADR-0002（已 Superseded）

## 1. 背景：四张实测表

以下数字全部来自本机实测，脚本留在 `workspace/probe_perm.py` / `probe_glob.py`
（`workspace/` 是 gitignore 的过程态目录，不随 PR 提交）。复现方式见 §8。

### 1.1 判定矩阵（改造前，5 档 × 21 用例）

| 用例 | readonly | workspace | execute | auto | unrestricted |
|---|---|---|---|---|---|
| `Read .env`（相对/绝对） | **allow** | **allow** | **allow** | **allow** | **allow** |
| `Grep pattern=AWS` | **allow** | **allow** | **allow** | **allow** | **allow** |
| `Write .git/config` | deny | ask | ask | deny | **allow** |
| `Write .env` | deny | ask | ask | deny | **allow** |
| `Bash cat .env` | deny | deny | deny | deny | deny |
| `Bash cat app.env`（误伤） | deny | deny | deny | deny | deny |
| `Bash cat $(echo LmVudg== \| base64 -d)` | deny | **ask** | **ask** | deny | **allow** |
| `Bash pytest -q` | deny | ask | allow | deny | allow |
| `Bash rm -rf build` | deny | ask | ask | deny | allow |
| `Bash find / -name *.pem` | **allow** | **allow** | **allow** | **allow** | **allow** |

三条结论：

1. **读侧完全无防护**：`Read` / `Grep` 在五档都能读 `.env`；`Bash` 的只读快路径
   不看工作区，`find /` 五档全 allow。
2. **写侧只在 bypass 失守**：`.env` / `.git` 实际由框架内建的 safety-ask
   （`DEFAULT_DANGEROUS_FILES` / `_DIRECTORIES`，basename 大小写不敏感）挡住，
   `BYPASS` 按契约跳过 safety-ask → `unrestricted` 位 allow。policy.yaml 里那 4 条
   deny **在 Write/Edit 上零命中**（否则 workspace 位应是 deny 而不是 ask）。
3. **Bash 侧靠子串**：挡不住构造写法，又会误伤 `cat app.env`。

### 1.2 声明式 glob 覆盖率（与框架 `Read/Write/Edit.match_rule` 同语义）

| 路径 | `.env` | `.git/` | `**/.env` | `**/.env*` | `**/.git/**` |
|---|---|---|---|---|---|
| `.env` | ✅ | ❌ | ❌ | ❌ | ❌ |
| `/repo/.env` | ❌ | ❌ | ✅ | ✅ | ❌ |
| `C:\repo\.env.local` | ❌ | ❌ | ❌ | ✅ | ❌ |
| `.git/config`（相对） | ❌ | ❌ | ❌ | ❌ | **❌** |
| `C:\repo\.git\config` | ❌ | ❌ | ❌ | ❌ | ✅ |
| `.ENV` | ❌ | ❌ | ❌ | ❌ | ❌ |

**没有任何单条 glob 能覆盖全部拼写**（`.git/config` 相对形式全漏、大小写全漏、
fnmatch 的 `*` 还跨分隔符）—— 这是「必须加中间件」的直接依据，也是为什么
`edge` 层的判定不做模式匹配、而做**归一化后按路径段比对**。

### 1.3 配置重复（改造前）

| 事实 | 位置 A | 位置 B |
|---|---|---|
| 可用档集合 | `policy.yaml: available` | `profiles.py: DEFAULT_AVAILABLE`（逐字相同） |
| 默认档 | `policy.yaml: default_profile` | `profiles.py` 里硬回落 |
| 无档位 mode | `policy.yaml: mode` | 五档各自的 `mode` |
| 白名单 | `policy.yaml: command_allowlist` | `profiles.py: execute.allowlist` |
| builtin deny | `policy.yaml: deny_rules` | `policy.py: _profile_deny_specs` |

`allow_rules` / `ask_rules` / `working_directories` 是三个**没有写入方**的死键。

## 2. 缺陷清单

### A 结构与配置

| # | 缺陷 | 位置 |
|---|---|---|
| A1 | 规则数据在 yaml、装配语义在代码，`source: profile` 与位置自相矛盾 | `policy.yaml` ↔ `governance/*` |
| A2 | `profiles.py` 三职责混装：数据表 + env 解析 + 加载 yaml | `profiles.py` |
| A3 | `policy.py` 四职责混装：翻译 + 合并 + 引擎构造 + 离线预演 | `policy.py` |
| A4 | `_profile_deny_specs(_profile, ...)` 首参未使用、两分支等价 | `policy.py:96` |
| A5 | `build_permission_context(mode=...)` 零调用方，且能让只读档跑 bypass | `policy.py:108` |
| A6 | `profiles.py` 声称不 import 框架，实际经 `tools.catalog` 间接拉进 agentscope | `.importlinter` 靠 `allow_indirect_imports=True` 放行 |

### B 功能 bug

| # | 缺陷 | 修法 |
|---|---|---|
| B1 | 默认档不可用时回落 `sorted(allowed)[0]` → 字典序首个是 `auto`（无人值守） | 按 `SAFETY_ORDER` 回落最严可用档 |
| B2 | `.git/`、`.env` 的 Write/Edit deny 规则零命中（沉默失效） | 规则改由敏感名单生成 + 中间件兜底 |
| B3 | `bash_decision` 每次新建引擎、`resolve()` 算两遍 | `lru_cache` 按 (档位, workdir) 复用 |
| B4 | `available()` / `default_profile()` 每次重读 env，同请求内可能不一致 | 按 env 原文缓存结果 |
| B5 | 判重键 `(tool, content)` 分不清 `None` 与 `""` | 改成 `(tool, content, behavior)` |

### C 安全

| # | 等级 | 缺陷 |
|---|---|---|
| C1 | 高 | **凭据可读**：`Read`/`Grep` 无敏感路径概念，五档都能读 `.env` |
| C2 | 中 | **只读档不是只读**：`find / -name *.pem` 五档全 allow（Bash 只读快路径不看工作区） |
| C3 | 高 | **`.git` 可写**：`unrestricted` 写 `.git/config` = allow → 改 hooks = 代码执行 |
| C4 | 中 | 子串匹配可绕：`base64` / `chr()` 构造命令 |
| C5 | 中 | 白名单含 `npx` / `pnpm` / `yarn` / `make`：远端拉包 / 任意 Makefile 目标，无确认执行 |
| C6 | 低 | 反方向误伤：`cat app.env` 被 deny |

## 3. 决策

| # | 决策 | 落地 |
|---|---|---|
| D-1 | 删除 `policy.yaml` 的 `permission:` 段，档位与规则成为代码常量（`governance/model.py`） | PR #111 |
| D-2 | `governance/` 拆成 `model` / `presets` / `compile` / `decide`，依赖单向，`model`+`presets` 由 importlinter 强制零框架 | PR #111 |
| D-3 | 敏感路径三道防线：声明式 deny（生成）+ 工具中间件（归一化判定）+ Bash 分词 | 本 PR |
| D-4 | `readonly` 的文件工具加工作区边界（`scope=readonly`） | 本 PR |
| D-5 | bypass 档把 danger 命令与解释器 `-c/-e` 显式写进 deny（不依赖会被跳过的 safety-ask） | 本 PR |
| D-6 | 默认档配置错误时回落最严可用档 | PR #111 |

### D-3 的三道防线

1. **声明式（早失败）**：由 `paths.SENSITIVE_FILES` / `SENSITIVE_DIRS` 生成
   「名字 × 工具 × 拼写形式」。Bash 用带分隔符的子串（`" .env"`），既匹配
   `cat .env` 又不误伤 `cat app.env`。
2. **中间件（真实边界）**：`tools/guard.py::SensitivePathGuard` 在工具执行**前**
   做 `expanduser → 反斜杠归一 → realpath → casefold` 后按路径段比对；命中返回
   `ToolResultState.DENIED` 且**不调用工具**。挂在 `build_toolkit_for` 里，
   是默认行为而非调用方可选。
3. **Bash 分词**：`shlex` 切 token，逐 token 判定敏感路径；AST 级危险命令分析
   仍交给框架的 `Bash.check_permissions`（它 ASK，`dont_ask` 档转 DENY）。

`Grep` 不整体拒绝，只注入 `--glob !<name>` 排除敏感文件：拒搜索太粗，
「结果里带出 .env 内容」才是要防的事。

### 已达成 vs 未达成（目标矩阵）

| 用例 | 目标 | 现状 |
|---|---|---|
| `Read .env` / `.env` 的任意拼写 | 五档 deny | ✅ |
| `Grep` 搜到 `.env` 内容 | 结果不含 | ✅（`--glob !` 排除） |
| `Write/Edit .git/config` | 五档 deny | ✅ |
| `cat .env` / `cat ./.env` / `cp .env` | 五档 deny | ✅ |
| `cat app.env` | 不应被拦 | ✅（改为按 basename 精确比对） |
| `rm -rf` / `python -c` 在 bypass | deny | ✅（别档保持 ASK） |
| `npx` / `pnpm` / `yarn` / `make` 在 execute | ASK | ✅ |
| `find /` 在 readonly | deny | ❌ **未达成** |

最后一行是有意留下的：Bash 的绝对路径参数（`/bin/sh`、`/usr/bin/env`）是正常执行
的一部分，按工作区边界判会把只读档直接打废。**真正的边界靠容器/只读挂载**，
这与 `governance/model.py` 的产品定位（防误操作 UX 层，不是安全隔离）一致。

## 4. 不变量（每条一个测试）

| # | 不变量 | 测试 |
|---|---|---|
| I1 | builtin deny 在五档全部生效 | `test_engine_denies_sensitive_*_in_every_profile` |
| I2 | 敏感路径任意拼写不可读/写/搜 | `test_any_spelling_of_a_secret_is_denied` |
| I3 | 普通路径不被误拦（含 `app.env`） | `test_ordinary_paths_are_not_denied` |
| I4 | 我们的敏感名单是框架 danger 清单的超集 | `test_sensitive_lists_cover_framework_defaults` |
| I5 | 默认档配置错误 → 最严可用档 | `test_default_profile_falls_back_to_strictest_available` |
| I6 | 白名单只在 execute 档生效 | `test_whitelist_belongs_to_execute_only` |
| I7 | container 规则不泄漏到 workspace 档 | `test_container_rules_do_not_leak_into_workspace` |
| I8 | 守卫默认挂载、命中即短路 | `test_toolkit_mounts_guard_by_default` / `test_guard_short_circuits_denied_call` |
| I9 | 切档丢弃上一档 allow_rules | `test_runtime.py::test_switch_profile_*`（既有） |

## 5. 风险

| 风险 | 缓解 |
|---|---|
| 守卫拒绝让模型陷入重试循环 | 拒绝文案固定「策略性拦截，不要重试，换路径或方案」 |
| 拒绝率上升污染健康分 | `permission_denied` 指标需拆 `guard_denied` 子项（后续） |
| 敏感名单变长导致规则数量膨胀 | 由生成器保证（约 280 条/档），构造在装配期一次；单测按名字断言而非条数 |
| 升级 AgentScope 后框架名单新增 | I4 的对账测试会红，提醒同步 |

## 6. 非目标

- 不引入 OS 级沙箱（Seatbelt / Landlock / AppContainer）。
- 不做多用户隔离（无 `user_id`）。
- 不恢复 ADR-0002 的 `scope+approvals` 两维拆分与 `permissions.local.yaml`。

## 7. 未决

1. 部署级额外 deny 是否要 env 注入（`DEVOPS_AGENT_DENY_EXTRA`）？
2. 8.3 短名与 UNC 路径是否纳入（当前覆盖 UNC 语义，不覆盖 8.3）。
3. `guard_denied` 观测字段的落点（span 还是 run 级计数）。

## 8. 复现

```
# 判定矩阵（改造前基线，需要切到 PR #111 之前的 commit 跑）
E:/code/devops-agent/.venv/Scripts/python.exe workspace/probe_perm.py
# glob 覆盖率
E:/code/devops-agent/.venv/Scripts/python.exe workspace/probe_glob.py
# 判定矩阵的回归版（不需要旧 commit）
E:/code/devops-agent/.venv/Scripts/python.exe -m pytest tests/unit/test_permission_guard.py -v
```

## 9. 改造后实测矩阵（本机，2026-11-26）

复现：PYTHONPATH=src python workspace/probe_after.py（脚本在 gitignore 的过程态目录）。

| 用例 | readonly | workspace | execute | auto | unrestricted |
|---|---|---|---|---|---|
| Read .env / Read .ENV | deny | deny | deny | deny | deny |
| Write/Edit .git/config | deny | deny | deny | deny | deny |
| Grep/Glob .git | deny | deny | deny | deny | deny |
| Read app.env（不该拦） | allow | allow | allow | allow | allow |
| Bash cat .env / cat ./.env / cp .env | deny | deny | deny | deny | deny |
| Bash cat app.env（误伤已修） | allow | allow | allow | allow | allow |
| Bash pytest -q | deny | ask | allow | deny | allow |
| Bash rm -rf build | deny | ask | ask | deny | **deny** |
| Bash python -c pass | deny | ask | ask | deny | **deny** |
| Bash npx some-pkg | deny | ask | **ask** | deny | allow |
| Bash find / -name *.pem | allow | allow | allow | allow | allow |

守卫层（判定不受引擎影响）：cat .env → denied（受保护文件 .env）；cat app.env →
allowed；grep -r x .ssh/ → denied（受保护目录）；
eadonly 档 Read 工作区外 → denied，
workspace 档同一路径 → allowed。

与 §3 目标矩阵一致；最后一行 ind / 是有意保留的未达成项（理由见 §3 末）。
