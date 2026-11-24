# ADR-0002：权限模型重构 —— 两维拆分、六段规则管线与可沉淀授权

- 状态：Accepted
- 日期：2026-11-24
- 决策者：项目所有者
- 相关：ADR-0001（权限拒绝率是已定义的观测指标之一）、`AGENTS.md` 架构约束、`config/policy.yaml`

## 背景

### 现象

用户反馈：**切换权限档位后，仍然每个动作都要确认**。切到「可执行」档、切到「全放开」档，行为没有可感知变化。

### 三次实测（非推测）

**第一次：切档不重建权限上下文。**

`src/devops_agent/runtime/assembly.py` 的 `_state_for()` 判据是 `state.permission_context.mode != PermissionMode(profile.mode)`。而 `workspace` 与 `execute` 两档的 `mode` 都是 `accept_edits`（`governance/profiles.py`），两者唯一的差别是 `execute` 多一组 `command_allowlist`。实测：

```
workspace 建态    mode=ACCEPT_EDITS  allow_rules={}
切 execute        mode=ACCEPT_EDITS  allow_rules={}   重建=False   ← 白名单从未装配
切 unrestricted   mode=BYPASS        重建=True
```

切到 `execute` 等于什么都没发生，`pytest` 照样 ASK。

反向问题同样存在：同一 `mode` 内切档时，上一档点「本会话允许」攒下的 `allow_rules` 会被带进新档，与 `_state_for` docstring 里「跨档必须丢弃」的承诺相反。

**第二次：判定矩阵。**

直接调用框架 `PermissionEngine.check_permission()`，绕过工具下发：

| 命令 | readonly | workspace | execute | auto |
|---|---|---|---|---|
| `ls` `cat` `git status` `git diff` `git log` | allow | allow | allow | allow |
| `python -m pytest` | deny | **ASK** | allow | deny |
| `npm test` | deny | **ASK** | allow | deny |
| `git add .` / `git commit` | deny | **ASK** | **ASK** | deny |
| `mkdir` | deny | allow | allow | allow |
| `rm -rf build` | deny | **ASK** | ASK | deny |
| `Write` 文件 | deny | allow | allow | deny |

两个发现：

- `workspace` 档的 `Write` 判定是 **allow**，但该档的 `tools=READONLY_TOOLS` 根本不含 Write/Edit。**引擎判定与实际可用工具是两回事**——这正是「档位语义失真」的证据：`accept_edits` 的卖点是工作区内编辑自动放行，而这一档没有编辑工具。
- 可选四档里没有一档是「工作区别问」。`auto` 不问，但非白名单命令是**直接拒绝**而非询问。用户「不想一直问」这个需求，在现有四档里无解。

**第三次：规则沉淀的生命周期。**

点「本会话允许」→ `_rules_for()` 生成 `PermissionRule(source="user")` → 写进 `AgentState.permission_context.allow_rules` → 随 `session_states` 快照生存。换会话、清历史、会话被 LRU 回收后重建，全部归零。

### 与开源实现的结构性差距

|  | 能力边界 | 审批策略 | 规则求值 | 点一次以后 |
|---|---|---|---|---|
| Codex CLI | `sandbox_mode` 三档；可写根内 `.git`/`.codex`/`.aws` 仍只读；网络默认关闭 | `approval_policy`，与沙箱完全独立 | 沙箱之外才升级询问，逐条命令批准 | 不持久化，靠沙箱兜底 |
| Claude Code | 无沙箱，靠工具集 | `defaultMode` + allow/ask/deny 三名单 | deny → ask → allow，第一个匹配胜出；deny 在 bypass 下仍生效 | 写入 `.claude/settings.local.json`，按仓库永久生效 |
| Gemini CLI | `--sandbox` 容器 | approval mode（default/autoEdit/yolo/plan） | decision + priority + tier（admin > user > workspace > default） | 生成规则并记录当前 mode |
| OpenCode | `external_directory` 守卫 | `--auto` 全局开关 | **最后匹配胜出**；`*` 在前、具体在后 | once / always / reject，always 仅会话内 |
| Cline / Roo | 无 | 按动作类型分别 auto-approve | 命令 denylist | 设置面板 |
| Aider | 无 | 单一 `--yes-always` | 无规则，靠 git 提交兜底 | — |

三家做的事情本项目全没做：**两维拆分**、**规则求值顺序写死且 deny 不可绕过**、**一次点击沉淀为持久规则**。Claude 额外做了 **shell 结构解析**（`Bash(safe-cmd *)` 不放行 `safe-cmd && other-cmd`，且会剥掉 `timeout`/`time`/`nice`/`nohup`/`stdbuf` 再匹配）；Codex 与 Gemini 额外做了 **信任目录**（未信任 = 只读 + 全问）。

### 本项目特有的结构缺陷

| # | 缺陷 | 位置 | 后果 |
|---|---|---|---|
| 1 | 切档只比 `mode` | `runtime/scope_runtime.py` | 同 mode 的不同档位互不生效 |
| 2 | 一个档位捆绑 `mode`+`tools`+`allowlist`+`fallback` | `governance/profiles.py` | 「工作区可写但不问」无法表达 |
| 3 | 沉淀只活在会话快照里 | `runtime/scope_runtime.py:_rules_for` | 换会话即归零，用户反复从零教育 |
| 4 | prefix 只取命令第一个词 | `runtime/confirm.py:rule_content_for` | `git commit -m x` 沉淀成 `git:*` |
| 5 | 不解析 shell 结构 | `runtime/confirm.py:command_of` | `cd /repo && rm -rf build` 沉淀成 `cd:*` |
| 6 | `deny` 绑在 `hard_deny` 档上 | `governance/policy.py:_profile_deny_specs` | workspace 补上写工具后，`.git/`、`.env` 立刻失守 |
| 7 | 无信任目录 | —— | 服务端在任意目录都能写 |

## 决策

把权限拆成**两个正交维度**，把规则从档位里搬出来独立成**六段求值管线**，把「以后都这样」从会话内存搬到**部署级文件**。

### 决策一：`PermissionProfile` 变成 `(scope, approvals)` 二元组

```python
@dataclass(frozen=True, slots=True)
class PermissionProfile:
    scope: str          # readonly | workspace | container
    approvals: str      # always | on-risk | never
    tools: tuple[str, ...]

    @property
    def key(self) -> str:
        """重建判据。任一维度变化都必须重建权限上下文。"""
        return f"{self.scope}+{self.approvals}"
```

- `scope` 决定**工具集**与「工作目录边界」，不决定是否询问。
- `approvals` 只决定框架 `mode`（即兜底动作）与沉淀规则是否降级，**不改动前五层，也不改动工具集**。

**框架约束（实测，决定了哪些组合可实现）**：`PermissionEngine` 的求值顺序是 `deny → ask → allow`，`allow` 无法盖过 `ask`；`mode` 决定无规则命中时的兜底。五种 mode 的实际行为：

| mode | `ls`/`cat`/`git status` | `pytest` | `mkdir` | `Write` |
|---|---|---|---|---|
| `default` | allow | ask | ask | ask |
| `explore` | allow | deny | deny | deny |
| `accept_edits` | allow | ask | allow | allow |
| `dont_ask` | allow | deny | allow | allow |
| `bypass` | allow | allow | allow | allow |

因此**档位默认必须由 `mode` 承担，绝不能写成 `ask` 规则**——一旦写成 ask 规则，它就排到 allow 前面，用户沉淀的规则永远不生效。这个约束是「不变量 2」能成立的前提。

有效组合与映射：

| scope | always | on-risk | never |
|---|---|---|---|
| `readonly` | — | — | `explore` |
| `workspace` | `default` | **`accept_edits`（默认）** | `dont_ask` |
| `container` | `default` | `bypass` | `bypass` |

`readonly` 行的两个空格是有意留白的：框架的只读保证与「什么都问」不可兼得（`default` 下 `mkdir` 是 ask，批准后就能写，破坏只读语义），而 `explore` 的越界是 deny 而非 ask，没有中间态。**「只读」就是「只读」，在这个维度上问不问没有意义**——如实留白，比编一个看起来完整的格子好。

**附带收益**：`approvals` 变化只改 `mode` 与规则降级，工具集逐字节不变 → 系统提示与工具 schema 不变 → **调整打扰程度不击穿前缀缓存**。这与 `ProfileNoteMiddleware`（档位说明追加在消息末尾）叠加：同一 `scope` 内换打扰程度，跨档命中率仍维持 95%+。

**弃用别名**：`execute` → `workspace`（同 `workspace+on-risk`），`auto` → `unattended`。两者都记一条 warning 并照常服务，避免老会话的 localStorage 或旧 run 记录请求 403。前端 `PERMISSION_KEYS` 同步替换，`resolvePermission` 的兜底逻辑本来就会回落默认档，不会崩。

### 决策二：规则独立成层，六段管线求值

```python
@dataclass(frozen=True, slots=True)
class Rule:
    tool: str                # Bash | Write | Edit
    pattern: str | None      # None = 整个工具；"git add:*" / ".env"
    behavior: str            # allow | ask | deny
    source: str              # builtin | project | user | session
    scope: str               # readonly | workspace | container：生效范围下限

SCOPE_ORDER = {"readonly": 0, "workspace": 1, "container": 2}
```

求值顺序固定为六段，第一个命中即返回：

| 层 | 内容 | 实现方式 | 能否被 allow 盖过 |
|---|---|---|---|
| ① `deny` · builtin | `.git/`、`.env`、`.ssh/`、`.aws/`、`rm -rf /` | `deny_rules` | 否（实测：bypass 下仍生效） |
| ② `deny` · project / user | `policy.yaml` 与本地规则 | `deny_rules` | 否 |
| ③ `ask` · **显式** | `ask_rules` + 高危正则（手写意图） | `ask_rules` | **否**（实测：框架顺序 ask 先于 allow） |
| ④ `allow` · 精确 | 程序 + 子命令 + 参数 | `allow_rules` | — |
| ⑤ `allow` · 前缀 | 程序 + 子命令（如 `git add:*`） | `allow_rules` | — |
| ⑥ 档位默认 | `always`→`default`；`on-risk`→`accept_edits`/`explore`；`never`→`dont_ask`/`bypass` | **`mode`**，不是规则 | 天然被 ④⑤ 盖过 |

关键区分：**第 ③ 层是「人明确说要问」，第 ⑥ 层只是「没人说过，所以按档位兜底」**。前者由框架顺序保证不可被 allow 推翻，后者因为根本没进 `ask_rules` 桶，allow 命中就直接返回——不需要额外机制。

三条不变量，全部写成参数化测试：

**不变量 1：`deny` 永远先于 `allow`，且在 `never` / `bypass` 档也不失效。**
实测确认框架已经是这个行为（`deny(git:*)` 与 `allow(git status:*)` 同时命中 → deny；`bypass` 下同样 deny）。本项目的问题不是框架，而是 `_profile_deny_specs()` 只在 `hard_deny` 档装配 deny，等于最需要防护的档位最松。改法是让 builtin deny 无条件装配。

**不变量 2：档位默认不写成 `ask` 规则，因此 ④⑤ 的 allow 天然优先于它；但 ③ 的显式 ask 不可被 allow 覆盖。**
这条不靠新机制，靠「把兜底放在 `mode` 里」。如果反过来把「非白名单命令都问」写成 `ask(Bash:*)`，它就排到 allow 前面，用户点过的「以后都这样」全部失效——只能一直点，正是当前的病症。

**不变量 3：`approvals=always` 时，④⑤ 两层的 allow 降级为 ask，而非忽略。**
实现方式是把这些规则从 `allow_rules` 移到 `ask_rules`（`replace(r, behavior="ask")`）。这样审计档是真正的「每个动作都确认」，同时保留「这条命令之前允许过」的提示信息，用户知道自己在重复批准什么。

**规则生效的 range 判断**：`SCOPE_ORDER[current_scope] >= SCOPE_ORDER[rule.scope]`。在 `workspace` 批准的 `pytest:*` 在 `readonly` 下不生效，在 `container` 下生效。这是 Gemini「批准时记录当前 mode」的变体，防止宽松环境攒的规则渗回严格环境。

### 决策三：shell 解析之后才谈 prefix

`pattern` 的生成不再取 `command.split()[0]`：

```python
def split_segments(cmd: str) -> list[str]      # 按 && || ; | 切，尊重引号与转义
def strip_wrappers(argv: list[str]) -> list[str]  # timeout/time/nice/nohup/stdbuf/env
def rule_prefix(cmd: str) -> str | None        # → "git add:*" / "pytest:*"
```

| 命令 | 现在沉淀成 | 应沉淀成 |
|---|---|---|
| `git commit -m "x"` | `git:*` | `git commit:*` |
| `git add .` | `git:*` | `git add:*` |
| `pytest -q tests/unit` | `pytest:*` | `pytest:*` |
| `cd /repo && rm -rf build` | `cd:*` | 拒绝沉淀（多段且含破坏性命令） |
| `timeout 30 npm test` | `timeout:*` | `npm test:*` |

判定同样逐段：`&&` / `||` / `;` / `|` 连接的每一段都必须被允许，任一段不通过则整体 ASK。这是 `rm -rf` 藏在 `cd` 后面时的唯一防线。

### 决策四：「以后都这样」落到部署级文件

按项目所有者决策：**部署级共享**（本后端的所有使用者共享同一套沉淀规则），不引入 `user_id`。

| 按钮 | 写入 | 生命周期 |
|---|---|---|
| 这一次 | 不写规则 | 仅本次调用 |
| 本会话 | `session` 层（现有行为） | 当前会话快照 |
| 以后都这样 | `config/permissions.local.yaml`（gitignore） | 跨会话、跨进程、跨重启 |

写入约定：

- 文件由程序追加写，**原子替换**（写临时文件 + `os.replace`），进程内加 `asyncio.Lock` 串行化。
- 文件格式错误、字段缺失一律**跳过该条并记 warning**，不让服务起不来（沿用 `available()` 的宽容原则）。
- 每条规则带 `created_at` 与 `origin`（生成它的命令原文），便于审计与撤销。
- 提供 `GET/POST/DELETE /api/permissions/rules`，前端规则列表显示来源与作用范围，可逐条删。现在点错了规则只能清会话，没有撤销手段。

## 关键事实：为什么 `approvals` 必须与规则分层

如果 `approvals=never` 顺手把 deny 一起关掉（当前 `unrestricted` 的做法正是相反方向：只在这一档才装配 deny），那么「无人值守」就没有任何约束；如果 `approvals=on-risk` 顺手把 allow 规则一起清掉，用户点过的「以后都这样」就白点。

Codex 把这两件事做成两个独立配置项（`sandbox_mode` 与 `approval_policy`），本方案的对应关系是：

| Codex | 本项目 |
|---|---|
| `sandbox_mode=read-only` | `scope=readonly` |
| `sandbox_mode=workspace-write` | `scope=workspace` |
| `sandbox_mode=danger-full-access` | `scope=container` |
| `approval_policy=on-request` | `approvals=on-risk` |
| `approval_policy=never` | `approvals=never` |
| （`untrusted`，已废弃） | `approvals=always` |

差异：Codex 的 `workspace-write` 由 OS 沙箱强制（Seatbelt / Landlock / AppContainer），本项目的 `scope=workspace` 只是路径判定，**没有系统级强制**。这一点在 `governance/profiles.py` 的模块 docstring 里已经写清「这是防误操作的 UX 层，不是安全隔离」，本方案不改这个定位，但要在文档与 UI 上保持一致的口径。

## 详细设计

### `governance/profiles.py`

字典的 key 是**预设名**（前端展示与请求体用），`profile.key` 是**判据**（`scope+approvals` 组合串，存进会话快照）。两者不同，不要混用。

```python
PROFILES: dict[str, PermissionProfile] = {
    "readonly":   PermissionProfile(scope="readonly",  approvals="never",   tools=READONLY_TOOLS),
    "workspace":  PermissionProfile(scope="workspace", approvals="on-risk", tools=READONLY_TOOLS + WRITE_TOOLS),
    "cautious":   PermissionProfile(scope="workspace", approvals="always",  tools=READONLY_TOOLS + WRITE_TOOLS),
    "unattended": PermissionProfile(scope="workspace", approvals="never",   tools=READONLY_TOOLS + WRITE_TOOLS),
    "container":  PermissionProfile(scope="container", approvals="never",   tools=READONLY_TOOLS + WRITE_TOOLS),
}
```

五个预设正好对应框架的五个 `mode`，每个都有明确用途，且没有一个处于「选了完全没变化」的状态：

| 预设 | scope + approvals | mode | 用途 |
|---|---|---|---|
| `readonly` | readonly + never | `explore` | 只读浏览，越界静默拒 |
| `workspace` | workspace + on-risk | `accept_edits` | **日常默认** |
| `cautious` | workspace + always | `default` | 有写能力，但每个动作都确认 |
| `unattended` | workspace + never | `dont_ask` | 定时任务 |
| `container` | container + never | `bypass` | 全放开，默认不开放 |

`available` 配置项列出开放的预设名；`available()` / `default_profile()` / `resolve()` 的宽容策略不变（未知名字跳过、越权配置回落可用集第一项）。`PermissionProfile.__post_init__` 校验 `(scope, approvals)` 在映射表里存在，避免配置出一个不可实现的组合。

- 白名单 `command_allowlist` **不再属于某个档位**，而是作为 `source=project` 的 `ask`-例外规则注入到 `scope >= workspace` 的所有档位。即「白名单命令不问」是规则层的事，不是档位的属性。
- `git add` / `git commit` 按所有者决策**不进白名单**，保持逐次询问。

### `governance/rules.py`（新增，框架无关）

承载 `Rule`、`split_segments`、`strip_wrappers`、`rule_prefix`、`evaluate(rules, call, profile)`。

**必须不 import agentscope**。`Rule → PermissionRule` 的转换留在 `governance/policy.py`（已在 `AGENTS.md` 的框架边界清单内）。这样 `governance/rules.py` 可以用纯数据结构测试，不需要构造框架对象——现在 `tests/unit/test_confirm_protocol.py` 与 `test_permission_profiles.py` 那种「先 new 一个 `Bash()` 再问引擎」的写法可以退役。

### `governance/policy.py`

`compile_context(profile, rules, policy)` 负责把六段管线编译成框架的 `PermissionContext`。**三名单 + `mode` 是唯一出口**，不写自定义判定：

```python
MODES: dict[tuple[str, str], str] = {
    ("readonly",  "never"):   "explore",
    ("workspace", "always"):  "default",
    ("workspace", "on-risk"): "accept_edits",
    ("workspace", "never"):   "dont_ask",
    ("container", "always"):  "default",
    ("container", "on-risk"): "bypass",
    ("container", "never"):   "bypass",
}

def compile_context(profile, rules, policy) -> PermissionContext:
    active = [r for r in rules if SCOPE_ORDER[profile.scope] >= SCOPE_ORDER[r.scope]]
    deny = [r for r in active if r.behavior == "deny"]
    ask = [r for r in active if r.behavior == "ask"]
    allow = [r for r in active if r.behavior == "allow"]
    if profile.approvals == "always":
        # 不变量 3：审计档把沉淀过的 allow 降级为 ask，但规则本身不丢
        ask += [replace(r, behavior="ask") for r in allow]
        allow = []
    return PermissionContext(
        mode=PermissionMode(MODES[(profile.scope, profile.approvals)]),
        working_directories=_dirs(policy, profile.scope),
        allow_rules=_to_rule_map(allow),
        deny_rules=_to_rule_map(deny),      # builtin 规则无条件并入，不按档位开关
        ask_rules=_to_rule_map(ask),
    )
```

builtin deny 的注入点从 `_profile_deny_specs()`（按 `hard_deny` 过滤）改成无条件并入——这是缺陷 6 的修法，也是不变量 1 的落地点。

### `runtime/assembly.py:_state_for`

```python
_PROFILE_KEY = "perm_key"

def _state_for(profile: PermissionProfile, rules: Sequence[Rule], state: AgentState | None) -> AgentState:
    if state is None:
        state = AgentState(permission_context=compile_context(profile, rules))
        state.middle_context[_PROFILE_KEY] = profile.key
        return state
    if state.middle_context.get(_PROFILE_KEY) != profile.key:
        # 档位变了就整体换掉权限上下文：旧档攒的「本会话允许」不能跨档生效
        state.permission_context = compile_context(profile, rules)
        state.middle_context[_PROFILE_KEY] = profile.key
    return state
```

- `middle_context` 已验证能随 `model_dump(mode="json")` 持久化并回读，存在会话快照里。
- 老快照没有 `perm_key` → 视为档位不同 → 重建一次。只替换 `permission_context`，`context`（对话历史）不动，用户无感。
- 判据从 `mode` 换成 `key`，**决策一直接修掉缺陷 1**，同时解决「同 mode 切档规则串档」。

### 接口

```
GET  /api/permissions
  → { presets: [{key, label, scope, approvals, tools, auto_allow, will_ask}],
      rules:   [{tool, pattern, behavior, source, scope, created_at}],
      rules_file: "config/permissions.local.yaml",
      trusted_workdirs: [...] }

POST /api/permissions/rules      # 沉淀一条（确认卡片的一键加规则也走这里）
DELETE /api/permissions/rules    # body: {tool, pattern}，按三元组精确删
```

`presets` 里 `auto_allow` / `will_ask` 是**由规则编译结果反推出来的两个字符串列表**，不是手写文案——档位说明必须与实际判定一致，否则又回到「note 说越界才问、实际每条都问」的老问题。

前端改动集中在三处：

- `PermissionPicker`：从「四选一」改成两行（能力范围 / 打扰程度），下面直接列「不问：读、写工作区、白名单命令」「会问：其余执行」。用户不必试一次才知道。
- 确认卡片：`这一次 / 本会话 / 以后都这样` 三态，默认选中间；高危命令禁用后两项（现有 `effective_scope()` 的判断保留并前移到 UI，让按钮直接置灰而不是点完被降级）。
- 新增规则列表页签：显示每条规则的来源与作用范围，可逐条删。

### 信任目录

`config/permissions.local.yaml` 增 `trusted_workdirs`（realpath + 首次信任时间）。未信任的 workdir **强制落到 `readonly+always`**，界面顶部一条「信任此目录以启用写入」。这是 Codex 与 Gemini 都有、本项目完全没有的一层；对一个能跑在别人机器上的服务端，这层比任何规则都管用。

### 审批疲劳观测

已有 span 数据与 pending 流程，增量很小：

- 状态栏：`等待确认 3 次 · 累计 24s`
- 同一前缀被问 ≥2 次 → 面板顶部提示「`pytest:*` 已被问 3 次，以后都允许？」，一键生成规则
- 轨迹页每轮标出「这次为什么被问」命中的是哪一层（①~⑥），让 ask 的原因可解释

开源实现都是被动地「你点一次我记一次」，不会主动告诉用户「你正在被反复打扰，该加规则了」。这一条是唯一无法从开源直接抄来的部分。

## 分阶段实施计划

| 阶段 | 内容 | 风险 | 独立可上线 |
|---|---|---|---|
| **P0** | `perm_key` 判据 + workspace 补写工具 + builtin deny 与档位解耦 + 白名单改规则层 + `execute` 降为别名 | 低。纯修 bug，不引入新概念 | 是 |
| **P1** | `Rule` 模型 + 六段管线 + shell 解析 + 判定矩阵参数化测试 | 中。行为会变（prefix 变窄），但变窄是安全方向 | 是 |
| **P2** | `permissions.local.yaml` + 三按钮 + 规则 CRUD + 信任目录 | 中。新增持久文件，需处理并发写与迁移 | 是 |
| **P3** | 疲劳统计 + 一键加规则 | 低。纯增量 | 是 |

P0 单独发一版：它不依赖任何新结构，能立刻止住「切了白切」。

## 测试计划

**判定矩阵参数化**（P1 的核心产物，这张表就是需求说明书）：

```python
@pytest.mark.parametrize("cmd,readonly,workspace,cautious,unattended,container", [
    ("ls",                   "allow", "allow", "allow", "allow", "allow"),
    ("pytest -q",            "deny",  "allow", "ask",   "allow", "allow"),  # 白名单 → 规则
    ("git commit -m x",      "deny",  "ask",   "ask",   "deny",  "allow"),  # 不进白名单
    ("cd /repo && rm -rf b", "deny",  "ask",   "ask",   "deny",  "ask"),    # 高危 → ③ 层
    ("cat .env",             "deny",  "deny",  "deny",  "deny",  "deny"),   # ① 层，任何档位
])
def test_matrix(...): ...
```

`container` 列里 `git commit` 是 allow（`bypass`）而 `cd /repo && rm -rf b` 是 ask——一个用例同时验证了「`bypass` 放行普通命令」与「显式高危规则在 `bypass` 下仍生效」。

**不变量测试**：

1. `bypass`/`never` 档下 builtin deny 仍生效
2. 精确 allow 盖过宽泛 ask；宽泛 allow 不盖过精确 ask
3. `approvals=always` 时 allow 降级为 ask，但规则不丢
4. `workspace` 批准的规则在 `readonly` 下不生效、在 `container` 下生效
5. 切档（`workspace+on-risk` → `container+never`）必须重建；同档反复调用不得重建（对象 id 不变）
6. 老快照（无 `perm_key`）能正常加载并触发一次重建
7. `cd x && rm -rf y` 不产生任何 allow 规则；`git commit -m x` 产生 `git commit:*` 而非 `git:*`
8. 规则文件损坏时不抛异常、跳过并记 warning

覆盖率门禁维持 `--cov-fail-under=80` 不放宽。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 行为变化让既有用户困惑（`pytest` 在 workspace 从 ASK 变 allow） | P0/P1 在 Release note 与档位 `note` 里逐条写清「不问 / 会问」清单，用户不必试一次才知道 |
| `permissions.local.yaml` 被并发写坏 | 原子替换 + 进程内锁；损坏时跳过并记 warning，不阻断启动 |
| 沉淀规则无限增长 | 规则列表可删；同一 pattern 重复写入时覆盖而非追加；`created_at` 超过 90 天且未命中的规则在启动时提示清理（不自动删） |
| prefix 变窄导致某些工作流变吵 | 变窄是安全方向；被问频次由 P3 的疲劳统计暴露，用户可以据此决定是否加规则 |
| 误以为这是安全隔离 | 保持 `governance/profiles.py` docstring 的现口径：这是防误操作的 UX 层；真正的隔离靠容器、只读挂载、非 root |

## 备选方案（已否决）

**A. 只修 Bug A（`_state_for` 比 mode），不动结构。**
最小改动，但治不了「一直 ask」：沉淀仍然只在会话内（缺陷 3），prefix 仍然只取首词（缺陷 4）。用户过两天会再报同一个问题。

**B. 取消档位，直接暴露 5 个 `PermissionMode`。**
`PermissionMode` 描述的是判定策略，名字是给实现者看的（`accept_edits` / `dont_ask`）；用户需要选的是「能让它干到哪一步」。且只切 mode 不动工具集时，有些档位选了完全没有可观测变化——这正是 `workspace` 现在的样子。

**C. 规则只存会话内，靠「本会话允许」撑住体验。**
即现状。会话被 LRU 回收、用户换设备、服务重启都会归零，用户永远在从零教育一个失忆的助手。这也是 OpenCode 的做法（`always` 仅会话内），但 OpenCode 有 per-agent 配置文件兜底，本项目没有。

**D. 每个终端用户一份规则（`users/<id>/permissions.yaml`）。**
多用户隔离更彻底，但要给会话模型引入 `user_id`，改动面翻倍。当前是「一个人用一台机」的形态，先做部署级共享；将来要隔离时，`Rule.source=user` 这一层直接换成按用户目录读取即可，不需要改管线。

**E. 引入 OS 级沙箱（Seatbelt / Landlock / AppContainer）做真正的隔离。**
方向正确但超出本次范围：跨三平台实现量大，与「先解决用户体感问题」的目标不匹配。记录在此作为后续方向，`scope` 的语义已经为它预留（`scope=workspace` 将来可以下沉为沙箱的可写根）。

## 未决问题

1. `container` 档是否需要 `network_access` 开关（Codex 的 `workspace-write` 默认禁网）。当前项目的 Bash 没有任何网络管控，联网搜索走的是模型侧 `enable_search`，与工具执行无关。
2. 规则文件是否需要支持「按 workdir 分组」（不同项目的常用命令不同）。当前设计是全局一份，若将来出现互相干扰再引入。
