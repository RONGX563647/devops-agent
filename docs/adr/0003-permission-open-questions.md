# ADR-0003：权限模型遗留三问的处置

- 状态：Proposed
- 日期：2026-11-24
- 决策者：项目所有者
- 相关：ADR-0002（权限模型重构，#41 / #44）、`config/policy.yaml`、`governance/{profiles,rules,policy,store}.py`

ADR-0002 落地时留了三个未决问题。这份文档逐个给出结论：**两个改代码，一个只改呈现**。

## 结论速览

| # | 问题 | 结论 | 改动面 | 优先级 |
|---|---|---|---|---|
| 1 | `container` 没有网络开关 | **加，但只做部署级 `permission.network: allow\|deny`（默认 allow）**；不做第三维；三层落地，并明确标注「策略层，不是隔离」 | `config.py` `rules.py` `policy.py` `agent.py` `main.py` + 前端开关 | P2 |
| 2 | 规则文件全局一份 | **做按 workdir 分桶**（v2 文档 + 读时合并 + 写时归当前 workdir + 路径归一）。前置发现：workdir 目前是部署级而非会话级 | `store.py` `policy.py` `agent.py` `main.py` + 规则列表分组 | P1 |
| 3 | `readonly` 的「会问」恒为空 | **不造新档位，改呈现**：空行给机器可读原因 + 一句人话 + 「切到逐个确认」入口；加不变量测试锁死语义（`unattended` 同为空但原因不同，文案要分开） | `profiles.py` `main.py` `PermissionPicker.tsx` | P0 |

三条互相独立，可以分别发版。

---

## 问题 1：`container` 档要不要网络开关

### 现状（实测，不是推测）

1. **执行路径**：`agentscope` 的 `Bash` 把整条命令包进 shell —— Windows 是 `["cmd", "/c", command]`，POSIX 是 `["/bin/sh", "-c", command]`，然后交给 `LocalBackend.exec_shell()`。`exec_shell` 只用 `asyncio.create_subprocess_exec`，**没有 `env` 参数**，子进程完整继承服务进程的环境变量。
2. **没有沙箱**：`scope=container` 只是「不做路径限制」，`profiles.py` 的模块 docstring 已经写明「这是防误操作的 UX 层，不是安全隔离」。
3. **联网搜索与工具执行无关**：`build_chat_model(enable_search=...)` 走的是模型请求体的 `extra_body={"enable_search": True}`（`runtime/agent.py`），属于供应商侧能力，不经过 Bash。
4. **实测 deny 的前缀语义**（本机 `PermissionEngine`）：

   ```
   deny 'curl'      'curl http://x'   -> deny
   deny 'curl'      'echo curl'       -> deny      ← 裸子串误伤
   deny 'curl:*'    'curl http://x'   -> deny
   deny 'curl:*'    'echo curl'       -> allow     ← 正确粒度
   deny 'pip install:*'  'pip install requests' -> deny
   ```

   即：**deny 规则写成 `X:*` 是按子命令前缀匹配的**，裸子串会误伤。这一点决定了下面 L1 的写法。

### 参照实现

- **Codex CLI**：`sandbox_mode=workspace-write` 带独立的 `network_access`（默认关），并且**由 OS 沙箱真实执行**（Seatbelt / Landlock）——它敢默认关，是因为关得住。
- **Claude Code**：没有网络维度。`settings.json` 的 `permissions.deny` 能拦 `WebFetch`/`Bash(curl:*)` 这类**工具调用**，拦不住脚本内部发起的连接，也不宣称自己能。
- **本项目**：Bash 在宿主机上直接跑，没有任何 OS 级隔离。**照抄「默认禁网」但没有执行力，得到的是安全错觉。**

### 方案对比

| 方案 | 内容 | 判断 |
|---|---|---|
| **A. 部署级 `permission.network`**（推荐） | `policy.yaml` 里一个枚举，`deny` 时装配网络禁用规则 + 子进程代理黑洞；默认 `allow`，既有部署零变化 | 语义清楚（这台机器该不该出网是环境属性），管理员一处配置，改一次全局生效 |
| B. 第三维 `network` 进档位 | `(scope, approvals, network)`，前端在选择器旁边加开关 | **否决**：管理员已经禁了网，UI 上的开关就意味着「用户可以放开」；要防这个就得再加一层「只能收紧」的判定，等于把 A 的语义做两遍 |
| C. 真沙箱（`unshare -n` / Docker `--network=none`） | Bash 换 backend 到容器，或 Linux 下 `unshare` | **本轮不做**，另立 ADR。跨三平台成本高，需要镜像、挂载、UID 映射；`scope` 的语义已为它预留（workspace 将来下沉成可写根） |

选 **A**。另外补一条**单向**的会话级收紧：`RunRequest.network: bool \| None`，只能从 `allow` 往 `deny` 走，`None` 表示跟随部署。理由：`allow` 部署下用户临时说「这次别联网，只读本地日志」是真实场景；反向则必须挡住。

### 落地三层

**L1 · 规则层（主防线，可解释可审计）**

`governance/rules.py` 新增 `NETWORK_DENY: tuple[Rule, ...]`，**`scope="readonly"`**（`SCOPE_ORDER` 最低 → 每一档都生效，包括 `container`/`bypass`：实测 deny 在 `bypass` 下仍然执行）。按组维护：

| 组 | 规则前缀（`:*` 形式） |
|---|---|
| 下载/传输 | `curl` `wget` `nc` `ncat` `netcat` `socat` `telnet` `ftp` `sftp` `scp` `ssh` `rsync` `aria2c` |
| 探测 | `ping` `traceroute` `dig` `nslookup` `host` `whois` `openssl s_client` |
| 包管理 | `pip install` `pip download` `uv add` `uv pip install` `npm install` `npm publish` `pnpm add` `yarn add` `cargo install` `go get` `go install` |
| 远端/集群 | `git clone` `git fetch` `git pull` `git push` `git submodule` `git ls-remote` `gh` `docker pull` `docker push` `docker login` `kubectl` `helm` |

由 `collect_rules()` 在 `policy.permission.network == "deny"` 时并入，`source="builtin"`、`origin="network:off"`，**与内置 deny 同批放在 `merge_rules` 的最后**（`identity` 相同的本地 allow 会被它盖掉，和 `.env` 一样没有商量余地）。

**L2 · 子进程环境（补规则够不到的库级调用）**

`tools/catalog.py` 里给 Bash 换一个 `LocalBackend` 子类：

```python
class OfflineBackend(LocalBackend):
    """禁网时的 Bash backend：子进程带「指向黑洞」的代理变量。

    只对认这套环境变量的客户端生效（curl / git / pip / requests / httpx …），
    不认代理的（node 内置 fetch、裸 socket）拦不住 —— 所以它是补充层，不是边界。
    """

    def __init__(self, workdir: str | None = None) -> None:
        super().__init__(workdir)
        self._env = {**os.environ, **_OFFLINE_ENV}
```

`_OFFLINE_ENV = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": …, "ALL_PROXY": …, "NO_PROXY": ""}`（端口 9 关闭 → 立刻连接被拒，不是超时挂死）。

注意**不能**给服务进程本身设代理：模型请求走同一个进程，会被一起断掉。必须挂在子进程上。

同一处顺手关掉模型侧搜索：`build_agentscope_runtime` 里 `web_search = web_search and not network_denied()`，并让 `/api/permissions` 回 `network`，前端据此隐藏「联网」按钮 —— 否则用户点了只会拿到一个看不懂的供应商错误。

**L3 · 文案（防止把策略层当隔离）**

- 选择器弹层底部固定一行：「本部署已禁网：靠规则与代理拦截，脚本级直连仍可能绕过。」
- 被拦时报文给出**命中的规则**（`network:off` → `curl:*`），而不是一句「permission denied」。

### 判定矩阵（新增行，并入现有参数化测试）

| 命令 | network=allow（现状） | network=deny |
|---|---|---|
| `curl https://x` | 按档位（container: allow） | **deny** |
| `pip install -r requirements.txt` | 按档位 | **deny** |
| `git push` | 按档位（③ 层 ask 视 force 而定） | **deny** |
| `echo curl` | allow | **allow**（`curl:*` 不误伤） |
| `pytest -q` | allow | allow（本地测试不受影响） |

### 已知缺口（写进文档，不假装堵住）

- `bash -c "curl x"`、`python -c "import urllib..."`、node `fetch` 绕过 L1；L2 只覆盖认代理的客户端。
- 前缀只认命令开头：`pip install -e .` 同样被拒，离线档下装本地包只能临时把 `network` 调回 `allow`。粗糙，但比「看起来禁了其实没禁」好。
- 没有 DNS 层拦截；`getent hosts` 仍然能解析。

### 验收

- `pytest`：矩阵新增 5 行参数化；`network=deny` 下 `collect_rules` 包含全部网络规则且 `source=builtin`；本地 allow 规则无法覆盖其中任一条。
- 单测 `OfflineBackend`：构造后 `_env["HTTPS_PROXY"]` 非空且**不污染** `os.environ`。
- 手工：`network: deny` + `container` 档，`curl https://example.com` 被拒且报文含规则名；`pytest -q` 正常。

---

## 问题 2：规则要不要按 workdir 分组

### 前置发现：workdir 现在是**部署级**的

`runtime/agent.py:resolve_workdir()`：`显式参数 > DEVOPS_AGENT_WORKDIR > 进程 cwd`。而：

- `AgentScopeRuntime.__init__(agent, *, profile)` **不持有 workdir**；
- `session_runtime_factory` 调 `build_agentscope_runtime(profile=…, state=…)` 时不传 workdir；
- `RunRequest` 没有 workdir 字段，`session_states` 也不记。

也就是说：**一个部署只有一个 workdir**，换项目靠改环境变量重启。ADR-0002 设想的「不同项目的常用命令不同」，在当前形态下表现为「A 项目沉淀的 `pytest:*` 在 B 项目里静默生效」——这正是要修的东西，但入口不在规则文件，而在「workdir 得先是个可观测值」。

### 结论

**做，但只做到「读时按 workdir 合并、写时归到当前 workdir」**，不引入会话级 workdir（那是更大的改动，见 P3）。

- 规则桶：`global`（所有项目）+ `workspaces[<path>]`（本项目）。
- 求值：`global + 当前 workdir 桶`，仍按 `scope` 过滤 —— 桶只减少串味，不改判定顺序。
- 写入：确认卡片的「以后都这样」写**当前 workdir 桶**；`workdir` 为空（脚本 / 单测直接建引擎）时退回 `global`，与今天一致；`policy.yaml` 的 `command_allowlist` 仍归 `global`（部署级配置本就该全局）。
- 迁移：**v1 文件永远可读**，`rules:` 视为 `global`；写回时升到 v2。降级回旧版本不会炸（旧代码只读 `rules`，v2 里仍有该键）。

### 数据格式 v2

```yaml
version: 2
rules:                     # 保留：v1 兼容层，等价于 global
  - {tool: Bash, pattern: "pytest:*", behavior: allow, source: user, scope: workspace, origin: "pytest -q"}
workspaces:
  "E:/Code/Proj-A":        # 键 = realpath（保留大小写，便于人读；比对时归一）
    - {tool: Bash, pattern: "npm test:*", behavior: allow, source: user, scope: workspace, origin: "npm test"}
trust:
  mode: permissive
  workdirs: [{path: "E:/Code/Proj-A"}]
```

键保留原始大小写，**比对时归一**：`os.path.normcase(os.path.realpath(p))`。Windows 下 `E:\Code\Proj-A` 与 `e:/code/proj-a` 是同一个目录，不做归一就会出现「规则在但没生效」。

顺带修一个同源缺陷：`store.trust_config().is_trusted()` 现在用 `_realpath` 精确比对，Windows 上大小写不同即判「未信任」——同一把归一化函数即可。

### 接线清单

| 位置 | 改动 |
|---|---|
| `store.py` | `local_rules(workdir=None)` → `global + bucket`；`add_rule(rule, workdir=None)`；`remove_rule(tool, pattern, workdir=None)` 返回 `"workspace" \| "global" \| None`；新增 `workspace_rules(workdir)`、`workspaces()` 概览；`normalize_workdir()` |
| `policy.py` | `collect_rules(policy, workdir=None)`、`compile_context(profile, rules=None, *, workdir=None)` 透传 |
| `agent.py` | `AgentScopeRuntime(..., workdir=base)`；`_rules_for` 落盘带 `workdir=self._workdir`；`build_permission_context(..., workdir=…)` |
| `main.py` | `/api/permissions` 增加 `workdir`、`rules` 每行带 `layer`、`workspaces` 概览；`POST/DELETE /api/permissions/rules` 接受 `layer`（默认 `workspace`） |
| 前端 | 规则列表分两段「本项目 / 所有项目」；删除请求带 `layer`；确认卡片文案「以后都这样」下加小字「仅本项目」 |

`/api/permissions` 直接调 `resolve_workdir()`，不新增查询参数：它必须和 runtime 用的是同一个值，两个来源迟早不一致。

### 删除语义

`DELETE` 先找 workdir 桶，未命中再找 `global`；返回 `{"layer": "workspace" \| "global"}`。前端据此提示「已从本项目移除」/「这是全局规则，会影响其他项目」。

### 风险

| 风险 | 缓解 |
|---|---|
| 换了 workdir 重启后，旧桶成孤儿 | `rules` 列表对不存在的目录标「目录已不存在」，支持整桶删；不自动清理（路径可能只是暂时卸载） |
| 用户以为「以后都这样」是全局 | 确认卡片按钮下加一行小字「仅本项目」；规则列表分段展示 |
| v2 写入把注释丢了 | 现状即如此（机器写文件），文档写明 |

### 验收

- `store`：v1 文件读出来等于 `global`；写一次后文件有 `version: 2` 且 `rules` 保留；v1/v2 都能读。
- 归一化：`E:\Code\Proj-A` 与 `e:/code/proj-a` 命中同一个桶（Windows 用例，POSIX 上 skip）。
- 求值：A 桶里的 `npm test:*` 在 B workdir 下**不生效**；`global` 规则在两个 workdir 下都生效。
- 删除：先删桶、再删 global，重复删除返回 404。

---

## 问题 3：`readonly` 的「会问」恒为空

### 为什么这是对的

1. `MODES` 里 `("readonly", "never") → explore`，且 `compile_context` 在 explore 下把 `ask` 全部并入 `deny`（代码注释写了原因：explore 没有「先问后放行」的通道，问下去只会把会话悬在没人能批的状态）。
2. 框架实测：`explore` **忽略 allow 规则**、把 ask 降级为 deny。
3. 反证「补一个 `readonly+always`、用 `default` 模式」这条路：只读档的工具集里仍有 `Bash`，`default` 模式下 `mkdir`/`rm` 是 ask —— 用户批准一次就能写，**只读语义当场破防**。换成「非白名单一律 deny」，ask 照样恒空。所以「只读 = 越界即拒」是定义，不是欠实现。

结论：**不新增档位**。空行是正确信息，错的是把它渲染成一个孤零零的 `—`。

### 关键澄清：空「会问」有两种，原因不同

拿真实引擎跑 `describe()` 得到现状（本次实测）：

| 档位 | mode | 会问 |
|---|---|---|
| readonly | `explore` | `[]` |
| workspace | `accept_edits` | `git commit -m x`、`rm -rf build` |
| cautious | `default` | 6 条（含 `mkdir build`、`写文件`） |
| unattended | `dont_ask` | `[]` |
| container | `bypass` | `rm -rf build` |

`unattended` 也是空的 —— 但原因不是「越界即拒」，而是 **`dont_ask` 把 ask 自动降级为 deny**（没人在，问了只会把会话挂到一个没人批的状态）。两者都必须解释，但不能用同一句话：

| mode | 空的原因 | 文案 |
|---|---|---|
| `explore` | 只读语义：越界即拒 | 「越界一律拒绝，不会问你 —— 想让它先问再批准，切到「逐个确认」」 |
| `dont_ask` | 无人值守：问也没人批 | 「需要确认的动作直接拒绝 —— 无人值守档没人在，问了只会卡住」 |

如果只按 `scope == "readonly"` 判断，`unattended` 会继续显示一个没有解释的 `—`，等于只修了一半。

### 改法

**后端**（`profiles.py` + `main.py`）：`PermissionProfile` 增加 `ask_note: str = ""`，在 `will_ask` 为空**且** mode 属于 `{explore, dont_ask}` 时下发对应文案。

不写成前端硬编码：文案与 `note` 一样属于后端（现状 `note` 就在后端），前端只负责排版。也不按档位名硬编码 —— 档位名会变（`unattended` 曾经叫 `auto`），`mode` 才是真正决定行为的那个值。

**前端**（`PermissionPicker.tsx` 的 `Preview`）：`会问` 行空且有 `ask_note` 时，渲染灰字说明 + 一个「切到逐个确认」文字按钮（该档在 `available` 里才显示；点了走现有 `onChange`，`cautious` 不在开放集时不显示按钮，只留说明）。

**测试**（锁语义，防以后有人「顺手补齐」）：

```python
def test_empty_ask_iff_mode_downgrades_it():
    """ask 恒空 ⟺ 该 mode 本身会把 ask 降级（explore / dont_ask）。

    若有人给 readonly 补了 ask 通道，或把 dont_ask 换成 default，这条会红。
    """
    for name, profile in PROFILES.items():
        preview = asyncio.run(describe(profile))
        expected_empty = profile.mode in {"explore", "dont_ask"}
        assert (preview["ask"] == []) is expected_empty, name


def test_readonly_has_no_ask_channel():
    # 补 readonly+always 就是破坏只读语义：default 模式下 Bash 的 rm 可被批准执行
    assert ("readonly", "always") not in MODES
    assert ("readonly", "on-risk") not in MODES
```

前端补两条：`will_ask=[]` + `ask_note` → 出现说明文字（`explore` 时还有切档按钮）；没有 `ask_note`（老后端）→ 仍然渲染 `—`，不白屏。

---

## 实施顺序与改动面

| 阶段 | 内容 | 依赖 | 独立上线 |
|---|---|---|---|
| **P0** | 问题 3：`ask_note` 字段 + 预览空行呈现 + 两条不变量测试 | 无 | 是（纯增量，老后端兼容） |
| **P1** | 问题 2：`store` v2 + 路径归一 + `workdir` 透传到 runtime + 规则列表分段 | 无 | 是（v1 兼容读，行为默认不变） |
| **P2** | 问题 1：`permission.network` + L1 规则 + L3 文案 | 无 | 是 |
| **P2.5** | 问题 1 的 L2（`OfflineBackend` + 关搜索） | P2 | 是（可单独回滚：不换 backend 即退回只有 L1） |
| **P3** | 会话级 workdir（`RunRequest.workdir` 固化到会话与 run 记录） | P1 | 是 |

P3 是问题 2 真正发挥作用的时刻（一个部署并行服务多个项目）。在它之前，分桶解决的是「改 env 换项目后旧规则串味」。

## 未采纳

- **给 `container` 档单独加网络开关**：见问题 1 方案 B。任何「用户可放开管理员已禁」的开关都要额外一层判定，收益只有 UI 对称。
- **把 `container` 改名 `unrestricted`**：`container` 名不副实（没有容器），但改名会影响 `localStorage`、旧 run 记录、文档与测试里的 key。等真沙箱（P3 之后）落地时再一次性重命名，届时 `unrestricted` 反而是别名。记在遗留里。
- **`readonly` 加「会问」通道**：见问题 3 反证。

## 遗留（本 ADR 之后仍未解的）

1. `container` 无 OS 级隔离（网络 L1/L2 只是策略层）—— 待「Bash backend 容器化」ADR。
2. `container` 命名与语义不符。
3. 规则无过期清理：`created_at` 超过 90 天且未命中的规则只在 UI 提示，不自动删。
4. 每条规则的命中次数没有落库，疲劳统计仅进程内 `Counter`，重启归零。
