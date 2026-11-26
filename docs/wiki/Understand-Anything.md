# 代码理解图谱（Understand Anything）

本页说明本仓库怎么用 [Understand Anything](https://github.com/Egonex-AI/Understand-Anything)：
它解决什么问题、产物落在哪、为什么不入库、以及在本仓库的 worktree 流程里要注意什么。

## 1. 为什么需要它

本仓库有三个让「新人 / 新会话」上手很贵的特点：

- 分层严格且由 `.importlinter` 强制，但「谁调谁」散在 9 个装配模块里；
- 源码单文件硬上限 500 行（`scripts/check_file_size.py`），职责被切得很细，
  找一条完整调用链要跨 5–6 个文件；
- 每个任务都在独立 worktree 里做（见 [Development-Workflow](Development-Workflow.md)），
  会话一结束上下文就没了。

Understand Anything 的做法是：把仓库解析成一张**节点（文件 / 函数 / 类）+ 边（imports / calls /
contains 等 38 种）**的图，再用导览和分层视图把它讲一遍。它是**理解辅助**，不参与门禁、不进 CI。

## 2. 装在哪

全局安装，与仓库解耦（不写进 `pyproject.toml`，也不进 `node_modules`）：

| 内容 | 路径 |
|---|---|
| 插件仓库（含 core / dashboard / MCP） | `~/.understand-anything/repo` |
| 9 个 skill 的入口 | `~/.workbuddy/skills/understand*`（指向插件目录的联接） |
| 插件根兜底联接 | `~/.understand-anything-plugin` |

依赖只有 Node ≥ 22 + pnpm；`@understand-anything/core` 需先构建一次：

```bash
cd ~/.understand-anything/repo/understand-anything-plugin
pnpm install && pnpm --filter @understand-anything/core build
```

> Windows 下 `git clone` / `curl` 可能因 schannel 吊销检查失败报
> `CRYPT_E_NO_REVOCATION_CHECK`，加 `-c http.schannelCheckRevoke=false`（curl 用
> `--ssl-no-revoke`）即可，与本仓库无关，属本机环境问题。

## 3. 怎么跑

在会话里直接用技能说话即可（`/understand` 是本仓库外的写法，WorkBuddy 里用自然语言）：

```
用 understand 技能分析 E:/code/devops-agent，只跑 src 和 config
```

建议范围（全仓库跑一次很贵，见 §6）：

| 场景 | 范围 | 说明 |
|---|---|---|
| 日常理解架构 | `src/` + `config/` | 覆盖分层、装配、权限、网关主链路 |
| 追一条完整链路 | 同上，配合 `understand-explain` | 先出图，再针对单文件深挖 |
| 前端问题 | 追加 `web/src` | 会显著增加批次 |
| 不要包含 | `tests/`、`.venv/`、`node_modules/`、`workspace/` | 噪声，且拖长分析 |

产物固定落在 `$UA_DIR`：

```
.ua/
├── config.json              # 输出语言、autoUpdate
├── knowledge-graph.json     # 图本体（看板与 MCP 都读它）
├── meta.json
├── fingerprints.json        # 增量判定的依据
└── intermediate/            # 扫描、批次、各 batch 的中间结果
```

## 4. 为什么不入库

`.ua/` 是**过程态**，按 [AGENTS.md](../../AGENTS.md) §3.3「过程态与源码分离」的同一理由排除：
它由工具生成、随代码漂移、体积不小，且 merge 冲突的语义没人愿意手工处理。

因此：

- `.gitignore` 已忽略 `/.ua/` 与 `/.understand-anything/`；
- 不要把 `knowledge-graph.json` 提交进仓库换取「队友免跑」——本仓库的图每周都在变，
  提交它只是在制造无意义的 diff；
- 想共享，就把本页的命令和范围约定共享出去。

`config.json` 的推荐内容（本机已配好，不入库）：

```json
{ "outputLanguage": "zh", "autoUpdate": false }
```

`autoUpdate` 保持 `false`：自动更新靠提交钩子，而本仓库的提交发生在
`workspace/worktrees/*` 里，钩子覆盖不到，开着只会误导。

## 5. 看板

图要有地方看，本地起 Vite（数据服务写在 `vite.config.ts` 的中间件里，每次请求现读 `.ua/`）：

```bash
cd ~/.understand-anything/repo/understand-anything-plugin/packages/dashboard
GRAPH_DIR=E:/code/devops-agent ./node_modules/.bin/vite --host 127.0.0.1 --port 4317
```

启动后终端会打印带一次性 token 的地址，形如
`http://127.0.0.1:4317/?token=xxxxxxxx`；**缺 token 的数据请求一律 403**，
所以别把网址里的 token 去掉。界面语言取自 `$UA_DIR/config.json` 的 `outputLanguage`，
改完刷新即可，不用重启。

不想装依赖时也能用发布版：`npx --yes <release>/understand-anything-viewer.tgz <项目路径>`。

## 6. worktree 流程里的两个坑

**坑一：输出会被重定向到主仓库根。** 技能检测到 `PROJECT_ROOT` 处于 worktree
（`--git-dir` 与 `--git-common-dir` 不同）时，会把 `.ua/` 写到主仓库根，
因为 worktree 是临时的、图会随会话消失。所以：在 worktree 里跑分析，产出的是
**主仓库 main 的视角**，不是你当前分支的改动。要分析「当前分支」的代码，
显式传主仓库路径之外的目录（例如先把 worktree 路径复制一份，或直接在 worktree 内
调用脚本时把 worktree 伪装成普通 checkout）。

**坑二：成本随文件数线性上升。** 每批文件派一个子代理、最多 5 并发，全仓库意味着
几十个批次。这也是 §3 建议只跑 `src/` + `config/` 的原因——先出主干图，
再用 `understand-diff` / `understand-explain` 按需补细节。

## 7. 九个技能各管什么

| 技能 | 用途 |
|---|---|
| `understand` | 全量/增量分析，产出 `knowledge-graph.json` |
| `understand-dashboard` | 起看板 |
| `understand-explain` | 单个文件 / 函数 / 模块深挖 |
| `understand-diff` | 分析 diff 或 PR 的改动面与风险 |
| `understand-onboard` | 生成上手指南 |
| `understand-chat` | 基于已有图问答 |
| `understand-domain` | 抽业务域流程图 |
| `understand-knowledge` | 分析 wiki / 知识库类语料 |
| `understand-figma` | Figma 设计稿转设计图 |

## 8. 与门禁的关系

明确三条边界，避免误用：

1. **不加进 CI**：图谱生成要调模型、要几十个批次，不适合放进 `quality`；
2. **不替代测试**：图里的「这条边存在」是模型判断 + 静态解析的混合结果，
   不能当断言用；结论仍以源码与测试为准（AGENTS.md §3.6 证据优先）；
3. **不提交产物**：见 §4。
