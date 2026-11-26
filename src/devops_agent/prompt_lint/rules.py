"""提示词静态契约检查 —— 不跑模型、不联网，只查「文案」与「配置」是否自洽。

为什么保留这一层：下面这些问题一次 grep 就能定位，却会让所有跑分失去意义 ——

- 提示词告诉模型「可用写操作 Write/Edit」，但该档工具集里没有这两个工具；
- 系统提示模板里的占位符拼错，渲染时永远填不上（或被静默替换成机器路径）；
- 挂载的工具从未在任何文案里出现，模型不知道它存在；
- 题库里两条用例题面完全相同，通过率被虚增。

它查的是**提示词与档位本身**，与题库的断言模型无关 —— 断言质量由
`openjudge.benchmark.validate` 负责，两者不重叠。

四条规则（P1–P4）覆盖三个事实源的交界：`runtime/prompt.py` 的提示词、
`governance/profiles.py` 的档位、`eval/openjudge/cases/*.jsonl` 的题面。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from string import Formatter

from devops_agent.governance.model import PROFILES, PermissionProfile
from devops_agent.openjudge.benchmark import Case
from devops_agent.runtime.profile_note import policy_note
from devops_agent.runtime.prompt import _TOOL_PROMPT, DEFAULT_SYSTEM_PROMPT
from devops_agent.tools.catalog import READONLY_TOOL_NAMES, WRITE_TOOL_NAMES

ALL_TOOL_NAMES: tuple[str, ...] = (*READONLY_TOOL_NAMES, *WRITE_TOOL_NAMES)

# 提示词里工具名的写法（首字母大写）；小写形态会和普通英文单词撞车。
_TOOL_WORDS: dict[str, str] = {name: name.capitalize() for name in ALL_TOOL_NAMES}

# 占位符白名单：`_TOOL_PROMPT` 只允许出现这些键。
_EXPECTED_PLACEHOLDERS = frozenset(
    {"shell_tool", "shell", "workdir", "read_tool", "glob_tool", "grep_tool"}
)

# 渲染用样本值：故意用占位标签而不是真实路径，保证检查本身不引入机器差异。
_PLACEHOLDER_VALUES: dict[str, str] = {
    "shell_tool": "Bash",
    "shell": "<shell>",
    "workdir": "<workdir>",
    "read_tool": "Read",
    "glob_tool": "Glob",
    "grep_tool": "Grep",
}

# 判定「提示词里混进了机器相关路径」：盘符、临时目录、家目录。
_ABS_PATH = re.compile(r"[A-Za-z]:\\|/tmp/|/home/|/Users/|/(?:private/)?var/folders/")

# 档位说明里出现这些短语，就是在承诺「能写文件」。
# 判据不能只看 `可用写操作`：`policy_note` 的写工具行由同一个谓词生成，
# 只查它会变成同义反复；真正会漂移的是按档位写死的 `_WRITE_RULES[key]` 文案。
_WRITE_CLAIMS: tuple[str, ...] = (
    "可用写操作",
    "建/删/改文件",
    "可以读写",
    "可直接改文件",
)

_RULE_HELP = "见 docs/wiki/Observability-and-Eval.md 的 prompt-lint 一节"


@dataclass(frozen=True, slots=True)
class Finding:
    """一条检查结论。`level=error` 会让 CLI 退出码为 1。"""

    rule: str
    level: str
    where: str
    message: str
    hint: str = ""


def _finding(rule: str, level: str, where: str, message: str, hint: str = "") -> Finding:
    return Finding(rule=rule, level=level, where=where, message=message, hint=hint)


def _mentions(text: str) -> set[str]:
    """提示词里出现的工具名（小写集合）。"""
    return {
        name for name, word in _TOOL_WORDS.items() if re.search(rf"\b{re.escape(word)}\b", text)
    }


def _has_write(profile: PermissionProfile) -> bool:
    """该档的工具集里是否有写能力。"""
    return "write" in profile.tools


def _render() -> str:
    """按样本值渲染系统提示；模板写错时由调用方捕获 KeyError/ValueError。"""
    tool_prompt = _TOOL_PROMPT.format(**_PLACEHOLDER_VALUES)
    return f"{DEFAULT_SYSTEM_PROMPT}\n\n{tool_prompt}"


# ---------------------------------------------------------------- P1 工具名闭包


def check_tool_closure(profiles: Sequence[PermissionProfile]) -> list[Finding]:
    """工具集里的每个工具都必须在提示词或档位说明里出现，否则模型不知道它存在。"""
    system = _render()
    findings: list[Finding] = []
    for profile in profiles:
        note = policy_note(profile)
        mentioned = _mentions(system) | _mentions(note)
        missing = [tool for tool in profile.tools if tool not in mentioned]
        if missing:
            findings.append(
                _finding(
                    "P1",
                    "error",
                    f"profile:{profile.key}",
                    f"工具集含 {', '.join(missing)}，但提示词从未提到",
                    hint="模型不知道有这个工具，就不会用；补进系统提示或档位说明",
                )
            )
    return findings


# ------------------------------------------------------------ P2 占位符与机器无关


def _declared_fields(template: str) -> set[str]:
    """模板里声明的占位符键；`Formatter` 解析失败时抛 ValueError。"""
    return {
        field for _literal, field, _spec, _conv in Formatter().parse(template) if field is not None
    }


def check_placeholders() -> list[Finding]:
    """占位符必须能被填充，且渲染结果不含机器相关绝对路径与残留花括号。"""
    try:
        declared = _declared_fields(_TOOL_PROMPT)
    except ValueError as exc:
        return [
            _finding(
                "P2",
                "error",
                "prompt",
                f"_TOOL_PROMPT 的花括号不成对: {exc}",
                hint=_RULE_HELP,
            )
        ]
    findings: list[Finding] = []
    extra = sorted(declared - _EXPECTED_PLACEHOLDERS)
    if extra:
        findings.append(
            _finding(
                "P2",
                "error",
                "prompt",
                f"_TOOL_PROMPT 出现未声明的占位符: {', '.join(extra)}",
                hint=f"允许的键: {', '.join(sorted(_EXPECTED_PLACEHOLDERS))}",
            )
        )
    try:
        rendered = _render()
    except (KeyError, IndexError, ValueError) as exc:
        findings.append(
            _finding(
                "P2",
                "error",
                "prompt",
                f"_TOOL_PROMPT 渲染失败，占位符填不上: {exc}",
                hint="占位符拼错（例如把 {workdir} 写成 {tool_prompt}）会让这段文案永远渲染不出来",
            )
        )
        return findings
    if "{" in rendered or "}" in rendered:
        findings.append(_finding("P2", "error", "prompt", "渲染后仍残留花括号，占位符未被替换"))
    hit = _ABS_PATH.search(rendered)
    if hit:
        findings.append(
            _finding(
                "P2",
                "error",
                "prompt",
                f"系统提示含绝对路径 `{hit.group(0)}`，同一 prompt_sha 下各批次文本会不同",
                hint="渲染指纹与评测时一律传 `<workdir>` 占位标签",
            )
        )
    return findings


# ------------------------------------------------------ P3 文案承诺 = 能力矩阵


def _claims_write(note: str) -> bool:
    """档位说明是否在承诺「能写文件」。只读档的「不要尝试 Write」是拒绝语，不算。"""
    return any(claim in note for claim in _WRITE_CLAIMS)


def check_write_claims(profiles: Sequence[PermissionProfile]) -> list[Finding]:
    """档位说明承诺的写能力，必须与工具集一致。"""
    findings: list[Finding] = []
    for profile in profiles:
        note = policy_note(profile)
        claims = _claims_write(note)
        has_write = _has_write(profile)
        where = f"profile:{profile.key}"
        if claims and not has_write:
            findings.append(
                _finding(
                    "P3",
                    "error",
                    where,
                    "档位说明承诺可写文件，但工具集里没有 write/edit",
                    hint="模型会尝试写文件并被拒绝，表现为「权限切了但毫无变化」",
                )
            )
        elif has_write and "可用写操作" not in note:
            findings.append(
                _finding(
                    "P3",
                    "error",
                    where,
                    "工具集含 write/edit，但档位说明没有告知",
                    hint="模型不知道自己能写，会退回只讲解不修改",
                )
            )
    return findings


# -------------------------------------------------------- P4 题面不得重复


def normalize_prompt(text: str) -> str:
    """题面归一化：只压空白与换行差异，不做大小写折叠（中文为主，折叠只会带来误报）。"""
    return " ".join(text.split())


def check_duplicate_prompts(cases: Sequence[Case]) -> list[Finding]:
    """两条用例的首轮输入完全相同 → 通过率被虚增，且内容覆盖是假的。"""
    groups: dict[str, list[Case]] = {}
    for case in cases:
        key = normalize_prompt(case.prompt)
        if key:
            groups.setdefault(key, []).append(case)
    findings: list[Finding] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ids = sorted(member.id for member in members)
        for case in members:
            others = ", ".join(item for item in ids if item != case.id)
            findings.append(
                _finding(
                    "P4",
                    "error",
                    f"case:{case.id}",
                    f"题面与 {others} 完全相同",
                    hint="重复题面会虚增通过率；改成不同场景或删掉一条",
                )
            )
    return findings


# ---------------------------------------------------------------- 对外接口


def run_lint(
    *,
    profiles: Sequence[PermissionProfile] | None = None,
    cases: Sequence[Case] | None = None,
) -> list[Finding]:
    """跑全部规则；`cases` 为空时只查提示词与档位。"""
    targets = list(profiles) if profiles is not None else list(PROFILES.values())
    findings: list[Finding] = []
    findings.extend(check_tool_closure(targets))
    findings.extend(check_placeholders())
    findings.extend(check_write_claims(targets))
    if cases is not None:
        findings.extend(check_duplicate_prompts(cases))
    # error 在前，便于 CLI 直接显示第一条阻断项
    findings.sort(key=lambda item: (item.level != "error", item.rule, item.where))
    return findings


def split_findings(findings: Sequence[Finding]) -> tuple[list[Finding], list[Finding]]:
    """拆成 (errors, advice)，CLI 据此决定退出码。"""
    errors = [item for item in findings if item.level == "error"]
    advice = [item for item in findings if item.level != "error"]
    return errors, advice
