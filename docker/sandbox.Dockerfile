# syntax=docker/dockerfile:1.6
# 预配置黄金镜像（SPEC-0019）：把 gateway venv 与两个脚本烘焙进镜像，使运行时
# bootstrap（apt-get + uv + pip，分钟级）被整体跳过，OpenSandbox 启动只剩
# 「拉起 gateway + 轮询 /health」（秒级）。
#
# 路径必须与框架常量一致：
#   GATEWAY_HOME=/root/.agentscope           （agentscope.workspace._opensandbox._constants）
#   .venv / _mcp_gateway_app.py / _glob_helper.py（agentscope.workspace._utils）
# 改这些名字会让 fast-path 失效、退回分钟级 bootstrap。
#
# 构建上下文是仓库根目录：
#   docker build -f docker/sandbox.Dockerfile -t devops-agent-sandbox:<ver> .
FROM python:3.11-slim

# 与运行时锁定的 agentscope 版本保持一致；升级时两处一起改。
ARG AGENTSCOPE_VERSION=2.0.10

ENV GATEWAY_HOME=/root/.agentscope \
    UV_LINK_MODE=copy \
    PATH=/root/.agentscope/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# 1) 系统依赖：uv 安装器需要 curl/ca-certificates，Grep 工具需要 ripgrep。
#    与框架 bootstrap 命令同构，只是提前到构建期完成。
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

# 2) gateway venv：先装基础依赖，再 --no-deps 装 agentscope 本体。
WORKDIR $GATEWAY_HOME
RUN uv venv $GATEWAY_HOME/.venv
COPY docker/gateway-requirements.txt $GATEWAY_HOME/requirements.txt
RUN uv pip install -r $GATEWAY_HOME/requirements.txt \
 && uv pip install --no-deps "agentscope==${AGENTSCOPE_VERSION}"

# 3) 两个脚本从已安装的 agentscope 中导出（构建期一次性，不进运行时启动路径）。
COPY docker/export_gateway_assets.py /tmp/export_gateway_assets.py
RUN $GATEWAY_HOME/.venv/bin/python /tmp/export_gateway_assets.py --dest $GATEWAY_HOME \
 && rm -f /tmp/export_gateway_assets.py

# 4) 沙箱内工作目录（agentscope.workspace._opensandbox._constants.SANDBOX_WORKDIR）。
WORKDIR /workspace
