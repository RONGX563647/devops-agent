"""权限层：单一事实来源 + 唯一的框架边界。

分层与依赖方向（单向，禁止反向）：

- `model`   纯数据：档位与 builtin 规则的声明（零框架、零环境变量、无 IO）
- `presets` 环境变量：本部署开放哪些档位、默认哪档（依赖 model）
- `compile` 本包唯一 import agentscope 的模块：编译成 `PermissionContext`
- `decide`  离线判定：不执行命令，只问引擎（给题库 lint 用）

故意不在本文件 re-export：`from devops_agent.governance import X` 会先执行本模块，
而任何 re-export 都会把 `compile` 的 agentscope 依赖带进 `prompt_lint` 那类
「不得依赖框架」的层。消费者请显式 import 子模块。
"""
