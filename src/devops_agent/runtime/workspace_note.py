"""工作区说明 —— 追加在模型请求最后一条消息，而不是写进系统提示。

为什么值得单独一个模块：模型此前只从系统提示里拿到一句「工作目录 X」，不知道自己
在哪个工作区、代码是从哪来的。真实会话里出现过两连败的情况 —— 用户问「这个工作区
从远程 clone 的仓库你放在哪了」，模型答「我没有 clone 过任何仓库，我的工作区是
E:\\code\\devops-agent」，而它其实就绑在一个克隆出来的工作区上。

位置与档位说明一致（`runtime/profile_note.py`）：系统提示在请求最前面，那里改一个
字节，后面的工具 schema 与整段历史全部作废；说明放在消息末尾，不写进历史快照，
下一轮不会重复累积。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentscope.agent import Agent
from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase

from devops_agent.memory.turns import injected_note

# 来源的中文说法：模型据此回答「这仓库是哪来的」，不用猜
_SOURCE_LABEL = {
    "git": "远端克隆",
    "worktree": "本地仓库的工作树",
    "local": "本地目录（原地引用）",
}

_UNBOUND = (
    "本轮没有绑定工作区，执行目录是部署默认目录 {workdir}。"
    "用户问「你在哪个工作区」时按这个事实回答：没有工作区绑定，"
    "目录里的内容就是这个目录的全部内容，不要假设别处还有一份副本。"
)

_BOUND = """本轮工作区：{name}（{source_label}）。
执行目录：{root}
{branch_line}{remote_line}shell 的工作目录与文件工具的路径基准都是这个目录；相对路径都基于它。
回答「你在哪 / 代码放在哪」时直接用上面的路径，不要凭印象换成别的目录，也不要说自己没有工作区。"""


def _text(workspace: Any, name: str) -> str:
    """取字段的字符串形态：缺失、None、空串都归一成空串。"""
    return str(getattr(workspace, name, "") or "")


def _line(label: str, value: str) -> str:
    """`分支：x` 这样的一行；值为空就整行不出现。

    写 `分支：None` 比不写更糟——模型会把它当成一个真实的分支名念给用户。
    """
    if not value:
        return ""
    return f"{label}：{value}\n"


def workspace_brief(workspace: Any | None, workdir: str = "") -> str:
    """把工作区摊成一句说明；未绑定时也给出「没有工作区」这个事实。"""
    if workspace is None:
        return _UNBOUND.format(workdir=workdir or "未知") if workdir else ""
    source = _text(workspace, "source")
    return _BOUND.format(
        name=_text(workspace, "name") or _text(workspace, "id"),
        source_label=_SOURCE_LABEL.get(source) or source or "未登记来源",
        root=_text(workspace, "root_path") or workdir or "未知",
        branch_line=_line("分支", _text(workspace, "branch")),
        remote_line=_line("远端", _text(workspace, "remote_url")),
    )


class WorkspaceNoteMiddleware(MiddlewareBase):
    """在消息末尾补一句「你在哪个工作区」；只改本次请求，不进入会话历史。"""

    def __init__(self, brief: str = "") -> None:
        self._brief = brief

    def set_brief(self, brief: str) -> None:
        """工作区换了（同会话改绑不该发生，但重建时会走到这里）。"""
        self._brief = brief

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        if not self._brief:
            return await next_handler(**input_kwargs)
        messages: list[Msg] = list(input_kwargs.get("messages") or [])
        messages.append(injected_note(self._brief, kind="workspace"))
        return await next_handler(messages=messages)


__all__ = ["WorkspaceNoteMiddleware", "workspace_brief"]
