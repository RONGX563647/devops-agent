# 预配置沙箱镜像（SPEC-0019）

`mode=opensandbox` 的启动耗时几乎全部来自 in-sandbox bootstrap：`apt-get` 安装
`curl`/`ripgrep`、安装 `uv`、建 venv、`pip install` 基础依赖与 `agentscope`。
`SandboxedWorkspaceBase` 只在 `_mcp_gateway_app.py` **不存在**时执行这段
（框架注释：*"Docker's image already has it → fast-path"*）。因此把 venv 与脚本
烘焙进镜像，就能把分钟级冷启动压到秒级。

## 构建

构建上下文是仓库根目录（Dockerfile 里引用 `docker/...`）：

```bash
docker build -f docker/sandbox.Dockerfile \
  --build-arg AGENTSCOPE_VERSION=2.0.10 \
  -t devops-agent-sandbox:2.0.10 .
```

`AGENTSCOPE_VERSION` 必须与 `pyproject.toml` 锁定的版本一致，否则镜像里的
gateway 脚本与宿主驱动进程可能说不同的协议版本。

## 验证 fast-path 前提

```bash
docker run --rm devops-agent-sandbox:2.0.10 sh -c '
  test -f /root/.agentscope/_mcp_gateway_app.py &&
  test -f /root/.agentscope/_glob_helper.py &&
  test -x /root/.agentscope/.venv/bin/python &&
  rg --version &&
  pwd'
```

最后一行应输出 `/workspace`。

## 推送与固定

生产环境建议用 digest 固定，避免 `latest` 漂移：

```bash
docker tag devops-agent-sandbox:2.0.10 registry.example.com/devops-agent-sandbox:2.0.10
docker push registry.example.com/devops-agent-sandbox:2.0.10
# docker/sandbox.Dockerfile 构建后可用 `docker inspect --format '{{index .RepoDigests 0}}'` 取 digest
```

把 digest 写进配置（`sandbox.opensandbox.image`）或用环境变量覆盖：

```bash
export DEVOPS_AGENT_SANDBOX_MODE=opensandbox
export DEVOPS_AGENT_SANDBOX_REQUIRED=true
export DEVOPS_AGENT_SANDBOX_OSB_DOMAIN=opensandbox.example.com
export DEVOPS_AGENT_SANDBOX_OSB_IMAGE=registry.example.com/devops-agent-sandbox@sha256:...
# 凭据只走环境变量：不落 config.db、不进 /health
export DEVOPS_AGENT_SANDBOX_OSB_API_KEY=...
```

`DEVOPS_AGENT_SANDBOX_OSB_API_KEY` 由 `executor/boundary.py` 读取并传给
`OpenSandboxWorkspace(api_key=...)`；它不会出现在 `sandbox.opensandbox.env`
（那是沙箱内被测命令可见的环境变量）。

## 安全边界（如实说明）

- `network_policy` / `resource` 由 OpenSandbox 构造参数透传，实际生效取决于平台。
- **非 root、cap-drop、seccomp、只读 rootfs 不在本仓库实现**，取决于平台模板与
  镜像策略；若生产需要，请在平台侧模板中配置，或在此 Dockerfile 追加
  `USER` / 只读挂载等约束后再验证工具链可用。
- 仓库不接触 `docker.sock`：opensandbox 路径零 Docker 依赖，镜像构建是唯一的
  Docker 使用点，且只发生在部署机，不在进程运行时。
