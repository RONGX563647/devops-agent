# Understand Anything（提示词文本快照）

本目录保存 **Understand Anything 的提示词与技能说明文本**（Markdown），用途是阅读、review、追溯“分析规则长什么样”。
可执行的工具本体（源码 + 依赖 + 构建产物）**不在本仓库内**，本目录也不参与任何构建与 CI。

| 项 | 内容 |
|---|---|
| 文本来源 | 上游仓库 `understand-anything-plugin/` 下的 `skills/`、`agents/`、`hooks/` |
| 上游版本 | 2.9.8（commit `b232f35`） |
| 体积 | 约 500 KB / 65 个 Markdown 文件 |

## 目录内容

```
skills/    9 个技能的 SKILL.md，含 frameworks/ languages/ 等参考文本
agents/    12 个子代理提示词（file-analyzer、architecture-analyzer、tour-builder …）
hooks/     提交钩子的说明文本（脚本与 hooks.json 配置未纳入）
```

刻意排除的内容：`*.mjs`、`*.py`（流水线脚本）、`node_modules`、`packages/`（TypeScript 实现）。

原因：脚本脱离 `@understand-anything/core` 的构建产物无法运行，留在仓库里只会造成“看起来能跑、
其实跑不起来”的误解。**判断分析质量看的是这些 Markdown，不是 TypeScript** —— 想让摘要更准、
分层更合理，改的是 `agents/*.md` 里的提示词。

## 怎么用

工具本体装在仓库之外（体积约 560 MB，主要是 `node_modules`），装好后由 WorkBuddy 的技能目录联接
指向它。本机示例：

```
~/.workbuddy/skills/understand-*  ->  <工具本体>\understand-anything-plugin\skills\*
```

在会话里直接说“用 understand 分析这个项目”即可。看板（Vite 应用，读 `.ua/knowledge-graph.json`）：

```powershell
cd <工具本体>\understand-anything-plugin\packages\dashboard
$env:GRAPH_DIR='<项目根>'
.\node_modules\.bin\vite --host 127.0.0.1 --port 4317
```

生成物默认落在项目根 `.ua/` 下，该目录已忽略，不提交。

## 为什么只放文本

- 工具本体 560 MB，主要是 `node_modules` 与构建产物，入库会让仓库体积失控
- 文本约 500 KB，是决定分析效果的部分，放进仓库才能随 PR 一起评审改动
- 上游更新频繁；快照更新时需同步替换本目录，并在 commit message 里写明上游 commit

## 更新流程

```powershell
cd <工具本体>
git -c http.schannelCheckRevoke=false pull
cd understand-anything-plugin
$env:CI='1'; pnpm install --config.confirmModulesPurge=false
pnpm --filter @understand-anything/core build
```

随后把 `skills/`、`agents/`、`hooks/` 下的 `*.md` 重新覆盖到本目录（排除 `*.mjs` / `*.py`），
确认 `git status` 只有 Markdown 变动后再提交。
