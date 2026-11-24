"""档位约束与联网说明的下发位置 —— 追加在模型请求的最后一条消息，而不是写进系统提示。

为什么值得单独一个模块：前缀缓存按字节比对，系统提示在请求最前面，
那里改一个字，后面的工具 schema 与整段历史全部作废。实测（同一段 10 轮历史）：

- 档位说明写在系统提示里，跨档命中 2048 / 9645 ≈ 21%；
- 同样的说明放在消息末尾，跨档命中 9216 / 9628 ≈ 96%。

约束本身仍然每次下发，只是换了位置；不写进历史快照，所以下一轮不会重复累积。
联网说明同理：它随开关逐轮变化，写进系统提示等于每切一次开关就丢一次缓存。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentscope.agent import Agent
from agentscope.message import Msg, UserMsg
from agentscope.middleware import MiddlewareBase

from devops_agent.governance.profiles import PermissionProfile

# 按档位改写的约束句：只读档说「没有写权限」，其余档说边界在哪。
# 这条不同步改的话，切到放开档模型仍然不会写文件，表现为「权限切了但毫无变化」。
_WRITE_RULES: dict[str, str] = {
    "readonly": "本会话是只读档：没有写权限，不要尝试 Edit/Write 类操作，被拒绝时如实说明。",
    "workspace": (
        "本会话可在工作区内建/删/改文件；工作区外的操作会被拦下并要求确认，"
        "遇到拒绝就说明原因、换方案。"
    ),
    "execute": (
        "本会话可在工作区内建/删/改文件，并可执行白名单内的构建/测试命令；"
        "越界操作会要求确认，遇到拒绝就说明原因。"
    ),
    "auto": (
        "本会话是无人值守档：工作区内可以读写文件与执行命令，越界操作一律被拒绝且不会有人确认，"
        "不要反复重试同一条被拒的命令。"
    ),
    "unrestricted": (
        "本会话可直接改文件；但 .git/ 与 .env 类路径被硬性拒绝，遇到拒绝就换方案，不要绕过。"
    ),
}

_WRITE_TOOL_LINE = (
    "可用写操作：{write_tool} / {edit_tool}（在工作区内新建文件、按片段替换已有内容）。"
)

# 一行说清「这次能干什么」；环境信息（工作目录等）不在这里，否则每轮都要重读历史。
_POLICY_TAIL = """本会话权限：{profile_label}。{write_tool_line}{write_rule}"""


def policy_note(profile: PermissionProfile) -> str:
    """档位约束的一句话说明，由中间件与上下文接口共用同一份文案。"""
    writes = "write" in profile.tools
    return _POLICY_TAIL.format(
        profile_label=profile.label,
        write_tool_line=(
            _WRITE_TOOL_LINE.format(write_tool="Write", edit_tool="Edit") if writes else ""
        ),
        write_rule=_WRITE_RULES.get(profile.key, _WRITE_RULES["workspace"]),
    )


# 联网说明：检索结果由供应商塞进同一次请求的上下文，但模型看不出那些段落是检索来的，
# 于是按手里的文件工具理解世界，回答「我没有联网能力」，用户以为按钮坏了。
#
# 文案必须**可验证**：应用侧拿不到供应商有没有真检索的证据（框架不透出 search_info，
# 事件流里也没有检索 span），所以这里只能说「如果看到就怎么用」，不能说「已经有结果」。
# 实测写死「已附带搜索结果」会让模型去核对一段它无法辨认的内容，反而触发「我得自己抓
# 一遍」的验证循环 —— 一次真实会话因此跑了 21 步工具调用才收手。
_SEARCH_ON_NOTE = (
    "本轮已开启联网检索（这是本会话本就有的能力，不需要你再用工具去抓网页）。"
    "若检索结果存在，它会以带来源的引用块出现在本次上下文里："
    "看到就优先依据它回答并写明来源；没看到就直说没有检索到，不要声称检索过、"
    "也不要假设结果存在。抓取网页在本环境可能因网络策略失败，同一地址失败两次就停止尝试，"
    "改用已有信息回答并说明缺了什么。"
)


def search_note(web_search: bool) -> str:
    """联网开关的一句话说明；关闭时返回空串（不占末尾位置，不动请求形状）。"""
    return _SEARCH_ON_NOTE if web_search else ""


class ProfileNoteMiddleware(MiddlewareBase):
    """在 messages 末尾补档位说明与联网说明；只改本次请求，不进入会话历史。"""

    def __init__(self, profile: PermissionProfile, web_search: bool = False) -> None:
        self._profile = profile
        self._web_search = web_search

    def set_profile(self, profile: PermissionProfile) -> None:
        """运行中切档：下一条模型请求带的档位说明就用新档位。

        只改这一个字段，不重建中间件列表 —— 系统提示保持逐字节稳定。
        """
        self._profile = profile

    def set_web_search(self, web_search: bool) -> None:
        """运行中开/关联网：下一轮请求末尾才带（或不带）检索说明。"""
        self._web_search = web_search

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        messages: list[Msg] = list(input_kwargs.get("messages") or [])
        messages.append(UserMsg(name="system", content=policy_note(self._profile)))
        note = search_note(self._web_search)
        if note:
            messages.append(UserMsg(name="system", content=note))
        return await next_handler(messages=messages)
