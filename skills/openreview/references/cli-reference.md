# ocr CLI 参数表（v1.14.3 实测）

以下内容取自本机 `ocr <cmd> --help`，参数名与默认值均为原样。新增／变更版本时重新跑 `ocr <cmd> --help` 校准，
不要凭记忆改。

## 顶层

```
ocr [flags]
ocr [command]

auth        管理 Sign in with ChatGPT 认证
completion  生成 shell 补全脚本
config      配置管理
delegate    输出评审规格给宿主代理（不需要 LLM）
llm         LLM 工具命令
review      基于 diff 的评审
rules       查看与调试评审规则
scan        整份文件扫描（不需要 diff）
session     查看已保存的评审会话
version     版本信息
viewer      启动 WebUI 会话查看器
```

全局：`--color auto|always|never`（默认 `auto`）。

## review

```
ocr review                      # 工作区：已暂存 + 未暂存 + 未跟踪
ocr review --from main          # 当前分支 vs main（merge-base）
ocr review --from main --to dev # 两分支
ocr review --commit abc123      # 单 commit（对比父提交）
ocr review -p                   # 只预览审哪些文件，不调 LLM
```

| 参数 | 说明 |
|---|---|
| `--analysis` | 追加 Behavior / Impact / 维护者决策；仅 commit 或 range 生效；不改变退出码 |
| `--analysis-max-tokens-budget` | 仅限制 Analysis 的输入+输出 token，0 = 不限 |
| `--audience human\|agent` | `agent` 只出摘要；json/sarif 下进度写 stderr（默认 `human`） |
| `-b, --background` | 业务背景字符串 |
| `-B, --background-file` | Markdown 背景文件，优先于 `--background` |
| `-c, --commit` | 单个 commit hash 或 tag |
| `--concurrency` | 并发子任务数，默认 8 |
| `--effort low\|medium\|high` | 评审深度，空 = 用配置或默认 medium |
| `--exclude` | 逗号分隔的 gitignore 风格排除，与 rule.json 合并 |
| `-f, --format text\|json\|sarif` | 输出格式，默认 `text` |
| `--from` | diff 起点 ref |
| `--to` | diff 终点 ref；给了 `--from` 时默认 HEAD |
| `--max-git-procs` | 并发 git 子进程，默认 16 |
| `--max-tokens` | 单组 prompt token 上限 |
| `--max-tokens-budget` | 整次评审 token 总上限，每轮 LLM 前检查；超预算的组会补最后一轮并标记 `failed(budget)` |
| `--max-tools` | 单子任务工具调用轮数上限，最小 50 |
| `--model` | 本次运行覆盖模型 |
| `--no-filter` | 关闭 LLM 后置过滤，保留全部原始意见 |
| `-o, --output` | 写入 UTF-8 文件；`-` 表示 stdout |
| `-p, --preview` | 预览选中的文件，不调 LLM |
| `--provider` | 本次运行覆盖 provider |
| `--repo` | 仓库根目录，默认当前目录 |
| `--resume` | 从会话 id 续跑 |
| `--rule` | 自定义系统规则 JSON 路径 |
| `--timeout` | 并发任务总预算（分钟），默认 15；单请求超时另由 `OCR_LLM_TIMEOUT` 或 provider `timeout_sec`（秒，默认 300）控制 |

## scan

```
ocr scan                                  # 全仓
ocr scan --path internal/agent            # 单目录
ocr scan --path a.go,b.go                 # 多文件
ocr scan --exclude '**/generated/*,**/testdata/*'
```

与 review 相同的参数：`--audience --background(-b) --concurrency --exclude --format(-f)
--max-git-procs --max-tokens --max-tokens-budget --max-tools --model --output(-o) --preview(-p)
--provider --repo --resume --rule --timeout`。

scan 独有：

| 参数 | 说明 |
|---|---|
| `--batch none\|by-language\|by-directory` | 覆盖 BATCH_STRATEGY；值拼错会静默按默认跑 |
| `--path` | 逗号分隔的仓库相对路径；**默认全仓，路径不存在会报错，但传空值会回落全仓** |
| `--no-plan` | 跳过逐文件 PLAN_TASK 预检 |
| `--no-dedup` | 跳过批次内 DEDUP_TASK |
| `--no-summary` | 跳过收尾 PROJECT_SUMMARY_TASK |

## delegate（委派模式，不调 LLM）

```
ocr delegate preview --from main --to feature   # 可审文件 + mode/ref 元数据
ocr delegate preview                           # 工作区改动
ocr delegate rule <path...>                    # 按内容分组输出命中规则
ocr delegate rule --from main internal/a.go
```

`preview` / `rule` 共有参数：`-b/--background`、`-B/--background-file`、`-c/--commit`、`--exclude`、
`-f/--format text|json`（**不支持 sarif**）、`--from`、`--repo`、`--rule`、`--max-git-procs`、`--to`。

## config

```bash
ocr config provider                  # 交互式选 provider、填 key、测连通
ocr config model                     # 交互式选模型
ocr config set provider anthropic
ocr config set model claude-opus-4-6
ocr config set providers.anthropic.api_key "$ANTHROPIC_API_KEY"
# 自定义网关
ocr config set provider my-gateway
ocr config set custom_providers.my-gateway.url https://gateway.internal.com/v1
ocr config set custom_providers.my-gateway.protocol openai
```

配置文件：`~/.opencodereview/config.json`。三种协议：Anthropic Messages、OpenAI Chat Completions、
OpenAI Responses。

## llm / rules / session

```bash
ocr llm test          # 连通性自检
ocr llm providers     # 内置 provider 列表
ocr llm models        # 当前账号可用的 ChatGPT 模型

ocr rules check <path> [--rule custom.json] [--repo <dir>]   # 该文件命中哪条规则、来源层、匹配模式

ocr session list                 # 当前仓库最近会话
ocr session show <id>
ocr session comments <id>
ocr session compare <id1> <id2>
ocr session export <id>          # 导出单文件 HTML
ocr session rm <id>
```

## 其他可选配置

- `.opencodereview/rule.json`：项目级自定义规则，按 glob 匹配文件，例如

  ```json
  { "rules": [ { "path": "**/*.py", "rule": "所有公开函数必须有类型注解", "merge_system_rule": true } ] }
  ```

- 环境变量：`OCR_NO_UPDATE=1`（关闭后台自动更新检查）、`OCR_UPDATE_INTERVAL`（检查间隔分钟）、
  `OCR_LLM_TIMEOUT`（单请求超时秒）。
- 已知问题：`scan` 模式下部分发现 `line=0`，无法锚定行号；要精确行号用 `review`。
