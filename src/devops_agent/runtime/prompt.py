"""系统提示词与三类指纹 —— 本模块不 import agentscope。

提示词是前缀缓存的字节前提，与模型/工具构造分离，便于评测侧单独引用。
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from devops_agent.model_settings import ModelParams, effective_params
from devops_agent.tools.catalog import shell_name

DEFAULT_SYSTEM_PROMPT = "你是 DevOps 助手，帮助处理持续交付、项目排期与 Bug 定位。"

# 装配用的工作目录：部署环境可用 DEVOPS_AGENT_WORKDIR 指定 Agent 能看的工作区，
# 缺省取进程当前目录（本地开发即仓库根）。工具的相对路径与 shell 的 cwd 都基于它。
WORKDIR_ENV = "DEVOPS_AGENT_WORKDIR"

# 工具约定必须写进系统提示：修复前 Agent 收到的只有一句角色描述，
# 模型不知道自己有工具，于是把「工具调用 functions.exec」写进正文后直接结束。
#
# 注意：这一段**不含任何随会话变化的内容**。前缀缓存按字节比对，这里改一个字，
# 后面的工具 schema 与整段历史一起作废（实测跨档命中 21%）。档位说明因此不在
# 系统提示里，而是由 ProfileNoteMiddleware 追加在每次请求的最后一条消息。
_TOOL_PROMPT = """你可以调用以下工具来获取真实信息，而不是凭记忆回答：
- shell（工具名 {shell_tool}）：执行命令行，语法为 {shell}，工作目录 {workdir}
- {read_tool} / {glob_tool} / {grep_tool}：读文件、按模式找文件、按正则搜内容
硬性要求：
1. 需要文件内容、目录列表、命令输出时，必须真的发起工具调用；
   不要用「工具调用 xxx」这样的文字描述代替调用。
2. 不要编造命令输出或文件内容。工具失败就据实说明失败原因；
   同一个工具连续失败两次就换手段（例如 {glob_tool} 与 shell 互换）；
   换过两种手段仍然失败就停止尝试，说明缺了什么、用已有信息回答，
   不要继续换第三种写法重试。上下文里标注「本轮未能取到的材料」时，
   不要再尝试抓取或读取它们。
3. 最终回答用中文，可用 Markdown（标题、列表、表格、代码块）；引用工具输出时保持原样。"""


def resolve_workdir(workdir: str | os.PathLike[str] | None = None) -> Path:
    """确定 Agent 的工作目录：显式参数 > 环境变量 > 进程当前目录。"""
    if workdir is not None:
        return Path(workdir).expanduser().resolve()
    return Path(os.environ.get(WORKDIR_ENV) or os.getcwd()).expanduser().resolve()


# 提示词版本：改动系统提示或档位说明时手动递增，评测批次会记录它，
# 便于把「分数变化」和「哪一版提示词」对上。hash 由 prompt_fingerprint() 现算。
# v2：重试条款改成「同一工具连续失败两次就换手段」，不再指定具体工具名 ——
#     依赖缺失时（如缺 rg）原来的「改用 Grep 重试」会把模型推回同一堵墙。
# v3：指纹拆成「语义 / 字节 / 档位说明」三个（见 prompt_fingerprint 的说明）。
# v4：补「换过两种手段仍失败就停止」——只写「换手段」时模型会一直换下去：
#     实测一次会话里它换了 curl / PowerShell / python 脚本 / 代理设置六种写法，
#     失败都发生在同一层（网络），白烧 21 步工具调用。
PROMPT_ID = "agent-system"
PROMPT_VERSION = "v4"


def _shell_name() -> str:
    """解析 shell 名，优先取兼容门面 `runtime.agent` 上的实现。

    单测通过 `monkeypatch.setattr(devops_agent.runtime.agent, "shell_name", ...)`
    模拟不同平台，这条替换路径必须继续生效，因此按调用时动态解析。
    """
    facade = sys.modules.get("devops_agent.runtime.agent")
    resolver = getattr(facade, "shell_name", None)
    if callable(resolver):
        return str(resolver())
    return shell_name()


def _render_system_prompt(workdir_label: str, shell_label: str) -> str:
    """按给定的工作目录与 shell 标签渲染系统提示（指纹用占位符）。"""
    tool_prompt = _TOOL_PROMPT.format(
        shell_tool="Bash",
        shell=shell_label,
        workdir=workdir_label,
        read_tool="Read",
        glob_tool="Glob",
        grep_tool="Grep",
    )
    return "\n\n".join([DEFAULT_SYSTEM_PROMPT, tool_prompt])


def build_system_prompt(workdir: str | os.PathLike[str] | None = None) -> str:
    """拼装系统提示：角色描述 + 可用工具契约。

    **整段与会话无关、逐字节稳定**——这是前缀缓存真正能复用的部分。
    随档位变化的内容（能不能写、越界怎么办）不放在这里：系统提示在请求最前面，
    那里改一个字，后面的工具 schema 与整段历史全部作废（实测跨档命中 21%）。
    档位约束改由 `runtime/profile_note.py` 追加在消息末尾（实测 96%）。
    """
    return _render_system_prompt(str(resolve_workdir(workdir)), _shell_name())


def prompt_fingerprint() -> str:
    """提示词语义指纹：跨平台稳定，用于评测批次对比与 delta 归因。

    workdir 与 shell 都用占位符渲染，所以同一份代码在 Windows 与 Linux 上得到
    同一个值。这是 `compare` 唯一认作「提示词变了」的因子。

    历史上它同时承担「真实下发字节」的角色，导致两个方向都错：
    - 用 `<workdir>` 渲染 → 同一 prompt_sha 下真实字节因机器而异，前缀缓存
      不能跨机复用，但指纹看不出来；
    - 反过来，档位说明（每次请求都下发）不在指纹里，改了它 prompt_sha 不变，
      「同 sha 分数变了」会被误判成噪声。
    现在拆成三个函数，各管一件事。
    """
    payload = f"{PROMPT_ID}:{PROMPT_VERSION}\n{_render_system_prompt('<workdir>', '<shell>')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prompt_bytes_sha(workdir: str | os.PathLike[str] | None = None) -> str:
    """真实下发字节的指纹：含真 workdir 与真 shell 名。

    同一个 `prompt_fingerprint` 在不同机器/平台上这个值不同——那正是重点：
    它回答「前缀缓存能不能复用」，而不是「提示词有没有改」。
    """
    return hashlib.sha256(build_system_prompt(workdir).encode("utf-8")).hexdigest()


def profile_note_sha(profile_key: str) -> str:
    """档位说明的指纹：它每次请求都下发，改了必须能归因。"""
    from devops_agent.governance.profiles import PROFILES  # noqa: PLC0415 - 避免循环 import
    from devops_agent.runtime.profile_note import policy_note  # noqa: PLC0415

    profile = PROFILES.get(profile_key)
    note = policy_note(profile) if profile is not None else ""
    return hashlib.sha256(note.encode("utf-8")).hexdigest()


def prompt_factors(
    workdir: str | os.PathLike[str] | None = None,
    params: ModelParams | None = None,
) -> dict[str, str]:
    """评测报告要记录的提示词相关因子。

    - `prompt_sha`：语义指纹，**阻断性因子**（变了就不可归因）；
    - `prompt_bytes_sha`：真实字节，用于判断前缀缓存能否跨批复用；
    - `shell_platform`：shell 名，解释字节差异的来源；
    - `params_sha`：模型参数指纹。参数可以随时改，没有它「同一 prompt_sha
      分数变了」会被误判成噪声。
    """
    active = params if params is not None else effective_params(stored={})
    return {
        "prompt_sha": prompt_fingerprint(),
        "prompt_bytes_sha": prompt_bytes_sha(workdir),
        "shell_platform": _shell_name(),
        "params_sha": active.fingerprint(),
    }
