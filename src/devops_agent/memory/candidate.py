"""长期写入候选过滤 —— 判断这一回合值不值得进长期层（SPEC-0013 §18.2 M9）。

长期层按定义只放「稳定事实」，但此前的写入是「用户输入 + 助手回复拼起来」，
等于把长期层当日志：寒暄、纯确认、"继续" 都会变成一条永远命中的记忆，
既污染检索（NB 赛道的 junk_rate），也会把一次性的临时信息固化成事实。

这里只做**能不能写**的判断，不做摘要：摘要式写入留给 P6。
"""

from __future__ import annotations

import re

#: 寒暄与纯确认：去掉标点后只剩这些词，说明这一轮没有信息量。
_COURTESY = frozenset(
    """
    你好 您好 hi hello 嗨 嘿 在吗 在不在 早上好 上午好 下午好 晚上好 晚安
    谢谢 多谢 感谢 好的 好吧 好 行 可以 ok okay 收到 明白 明白了 懂了
    嗯 嗯嗯 哦 继续 继续吧 没问题 辛苦了 拜拜 再见
    """.split()
)

_PUNCT = frozenset(" \t\r\n，,。.！!？?~～、；;：:…—－-\"'“”‘’（）()【】[]<>《》")

#: 「有信号」的回合：带路径、命令行工具或源码后缀。短但值得记。
_SIGNAL = re.compile(
    r"[\\/]|[A-Za-z]:\\"
    r"|\b(git|pytest|npm|pnpm|yarn|curl|docker|kubectl|python|ruff|mypy|make)\b"
    r"|\.(py|ts|tsx|js|json|yaml|yml|toml|md|sh|sql)\b",
    re.IGNORECASE,
)

_MIN_CHARS = 12
_MIN_CHARS_WITH_SIGNAL = 6

#: 以问号结尾 = 提问。配合「回答正文为空」使用：只有问题的回合没有可记的内容。
_QUESTION = re.compile(r"[?？]\s*$")

#: 「我先看一下工作区里实际的东西」这类元话语：描述下一步要做什么，不是结论。
#: 只在与「无技术信号」同时成立时拒写 —— 这是零信息量却占掉 note 预算的典型
#: 形态（SPEC-0019 §7.3），带路径/命令/后缀的回合照常写入。
_META_OPENER = re.compile(
    r"^[\s\-*>#]*(?:好的?[，,、]?\s*)?"
    r"(?:我|我们|让我|现在|接下来|首先|先)"
    r"[^\n]{0,24}"
    r"(?:看一下?|看看|确认|理解|检查|分析|梳理|读|翻|找|开始|需要|打算|准备)"
)
_META_MAX_CHARS = 240


def _bare(text: str) -> str:
    """去掉标点与问候词，留下「实质内容」；全空即寒暄/纯确认。"""
    cleaned = "".join(ch for ch in text.lower() if ch not in _PUNCT)
    residual = cleaned
    for token in sorted(_COURTESY, key=len, reverse=True):
        residual = residual.replace(token, "")
    return residual.strip()


def is_candidate(user_text: str, assistant_text: str) -> tuple[bool, str]:
    """返回 (是否值得写, 跳过原因)。原因是事件里的 `reason`，不要写成句子。"""
    raw = f"{user_text.strip()}\n{assistant_text.strip()}".strip()
    if not raw:
        return False, "empty"
    # 只有问句、没有正文回答：tool-only 回合就是这个形态，记下来只有问题没有信息
    if not assistant_text.strip() and _QUESTION.search(user_text.strip()):
        return False, "question_only"
    # 去掉问候词后只剩一两个字（"谢谢你"→"你"）说明还是寒暄，没有可记的内容
    if len(_bare(user_text)) <= 2:
        return False, "courtesy"
    has_signal = bool(_SIGNAL.search(raw))
    floor = _MIN_CHARS_WITH_SIGNAL if has_signal else _MIN_CHARS
    if len(raw) < floor:
        return False, "too_short"
    # 无技术信号 + 元话语开头 + 短：说的是「接下来要看什么」，写进长期层只是噪音
    if (
        not has_signal
        and len(raw) <= _META_MAX_CHARS
        and _META_OPENER.match(assistant_text.strip())
    ):
        return False, "meta_preamble"
    return True, ""
