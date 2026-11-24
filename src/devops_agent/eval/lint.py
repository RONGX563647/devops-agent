"""提示词静态契约检查 —— 不跑模型、不联网，只查「文案」与「配置」是否自洽。

为什么需要这一层：评测跑一次要花模型额度，而下面这些问题一次 grep 就能定位，
却会让所有跑分失去意义 ——

- 提示词告诉模型「可用写操作 Write/Edit」，但该档工具集里没有这两个工具；
- 系统提示里渲染了绝对路径，同一份 prompt_sha 下每批次下发文本都不同；
- 用例断言了一个该档根本不装配的工具，判定恒真，等于没测。

六条规则（S1–S6）覆盖三个事实源的交界：`runtime/agent.py` 的提示词、
`governance/profiles.py` 的档位、`eval/suites/*.jsonl` 的用例。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from string import Formatter

from devops_agent.eval.dataset import (
    GRADER_VERSION,
    SUBJECTS,
    EvalCase,
    Suite,
)
from devops_agent.governance.policy import bash_decision
from devops_agent.governance.profiles import PROFILES, PermissionProfile
from devops_agent.runtime.agent import _TOOL_PROMPT, _render_system_prompt
from devops_agent.runtime.profile_note import policy_note
from devops_agent.tools.catalog import READONLY_TOOL_NAMES, WRITE_TOOL_NAMES

ALL_TOOL_NAMES: tuple[str, ...] = (*READONLY_TOOL_NAMES, *WRITE_TOOL_NAMES)

# 提示词里工具名的写法（首字母大写）；小写形态会和普通英文单词撞车。
_TOOL_WORDS: dict[str, str] = {name: name.capitalize() for name in ALL_TOOL_NAMES}

# 占位符白名单：`_TOOL_PROMPT` 只允许出现这些键。
_EXPECTED_PLACEHOLDERS = frozenset(
    {"shell_tool", "shell", "workdir", "read_tool", "glob_tool", "grep_tool"}
)

# 判定「提示词里混进了机器相关路径」：盘符、临时目录、家目录。
_ABS_PATH = re.compile(r"[A-Za-z]:\\|/tmp/|/home/|/Users/|/(?:private/)?var/folders/")


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


def _declared_write(profile: PermissionProfile) -> bool:
    """该档的工具集里是否有写能力。"""
    return "write" in profile.tools


# ---------------------------------------------------------------- S1 工具名闭包


def _check_tool_closure(profiles: list[PermissionProfile]) -> list[Finding]:
    """工具集里的每个工具都必须在提示词里有描述，否则模型不知道它存在。"""
    findings: list[Finding] = []
    system = _render_system_prompt("<workdir>")
    for profile in profiles:
        note = policy_note(profile)
        mentioned = _mentions(system) | _mentions(note)
        missing = [tool for tool in profile.tools if tool not in mentioned]
        if missing:
            findings.append(
                _finding(
                    "S1",
                    "error",
                    f"profile:{profile.key}",
                    f"工具集含 {', '.join(missing)}，但提示词从未提到",
                    hint="模型不知道有这个工具，就不会用；补进系统提示或档位说明",
                )
            )
    return findings


# ------------------------------------------------------------ S2 占位符与机器无关


def _check_placeholders() -> list[Finding]:
    """占位符必须被完整替换，且渲染结果不含机器相关绝对路径。"""
    findings: list[Finding] = []
    declared = {
        field
        for _literal, field, _spec, _conv in Formatter().parse(_TOOL_PROMPT)
        if field is not None
    }
    extra = sorted(declared - _EXPECTED_PLACEHOLDERS)
    if extra:
        findings.append(
            _finding(
                "S2",
                "error",
                "prompt",
                f"_TOOL_PROMPT 出现未声明的占位符: {', '.join(extra)}",
                hint=f"允许的键: {', '.join(sorted(_EXPECTED_PLACEHOLDERS))}",
            )
        )
    rendered = _render_system_prompt("<workdir>")
    if "{" in rendered or "}" in rendered:
        findings.append(_finding("S2", "error", "prompt", "渲染后仍残留花括号，占位符未被替换"))
    hit = _ABS_PATH.search(rendered)
    if hit:
        findings.append(
            _finding(
                "S2",
                "error",
                "prompt",
                f"系统提示含绝对路径 `{hit.group(0)}`，同一 prompt_sha 下各批次文本会不同",
                hint="渲染指纹与评测时一律传 `<workdir>` 占位标签",
            )
        )
    return findings


# ------------------------------------------------------ S3 文案承诺 = 能力矩阵


def _check_write_claims(profiles: list[PermissionProfile]) -> list[Finding]:
    """档位说明承诺的写能力，必须与工具集一致。

    只读档写「不要尝试 Edit/Write 类操作」是拒绝语，不算承诺；
    判据是 `可用写操作` 这一句在不在。
    """
    findings: list[Finding] = []
    for profile in profiles:
        note = policy_note(profile)
        claims = "可用写操作" in note
        has_write = _declared_write(profile)
        where = f"profile:{profile.key}"
        if claims and not has_write:
            findings.append(
                _finding(
                    "S3",
                    "error",
                    where,
                    "档位说明承诺「可用写操作」，但工具集里没有 write/edit",
                    hint="模型会尝试写文件并被拒绝，表现为「权限切了但毫无变化」",
                )
            )
        elif has_write and not claims:
            findings.append(
                _finding(
                    "S3",
                    "error",
                    where,
                    "工具集含 write/edit，但档位说明没有告知",
                    hint="模型不知道自己能写，会退回只讲解不修改",
                )
            )
    return findings


# ------------------------------------------------------------ S4 题库资产自洽


# `eval/` 目录：lint.py -> eval -> devops_agent -> src -> 仓库根
_EVAL_ROOT = Path(__file__).resolve().parents[3] / "eval"


def _check_case_files(case: EvalCase, fixture_root: Path | None, where: str) -> list[Finding]:
    """fixture 目录与 judge rubric 必须在仓库里真实存在。"""
    findings: list[Finding] = []
    if fixture_root is not None and case.fixture and not (fixture_root / case.fixture).is_dir():
        findings.append(_finding("S4", "error", where, f"fixture 目录不存在: {case.fixture}"))
    if case.judge is not None:
        rubric = _EVAL_ROOT / "rubrics" / f"{case.judge.rubric}.md"
        if not rubric.is_file():
            findings.append(
                _finding("S4", "error", where, f"rubric 文件不存在: {case.judge.rubric}")
            )
    return findings


def _check_case_tools(case: EvalCase, where: str) -> list[Finding]:
    """工具名必须在本仓工具目录里（拼错了只会让断言永远失败或永远通过）。"""
    calls = case.expected.tool_calls
    if calls is None:
        return []
    return [
        _finding("S4", "error", where, f"未知工具名 {name}")
        for name in [*calls.must, *calls.forbid]
        if name.strip().lower() not in ALL_TOOL_NAMES
    ]


def _check_case_assets(case: EvalCase, fixture_root: Path | None) -> list[Finding]:
    """fixture、档位、工具名、judge rubric 必须在本仓存在。"""
    findings: list[Finding] = []
    where = f"case:{case.id}"
    if case.permission not in PROFILES:
        findings.append(_finding("S4", "error", where, f"未知档位 {case.permission}"))
    if case.subject not in SUBJECTS:
        findings.append(_finding("S4", "error", where, f"未知 subject {case.subject}"))
    findings.extend(_check_case_files(case, fixture_root, where))
    findings.extend(_check_case_tools(case, where))
    return findings


# ------------------------------------------------------------ S5 断言有效性


def _is_dead_forbid(case: EvalCase) -> list[str]:
    """`forbid` 里的工具若该档根本不装配，断言恒真。"""
    profile = PROFILES.get(case.permission)
    calls = case.expected.tool_calls
    if profile is None or calls is None:
        return []
    tools = {name.lower() for name in profile.tools}
    return [name for name in calls.forbid if name.strip().lower() not in tools]


def _dead_assertions(case: EvalCase) -> list[str]:
    """恒真断言：判定与模型行为无关。"""
    dead: list[str] = [f"forbid:{name}" for name in _is_dead_forbid(case)]
    calls = case.expected.tool_calls
    if calls is not None and calls.ordered and len(calls.must) <= 1:
        dead.append("ordered(单元素)")
    profile = PROFILES.get(case.permission)
    if case.expected.file_state and profile is not None:
        if "write" not in profile.tools:
            # 每个文件断言各记一条，才能与 assertion_count 的分子对齐
            dead.extend(f"file_state:{item.path}(只读不可变)" for item in case.expected.file_state)
    return dead


def _weak_assertions(case: EvalCase) -> list[str]:
    """可从题面直接抄回的必含词 —— 判别力弱，建议换成 must_cite。"""
    prompt = case.input
    return [word for word in case.expected.must_contain if word and word in prompt]


def _check_assertions(cases: list[EvalCase]) -> list[Finding]:
    """逐条用例统计恒真/弱断言；全部断言恒真时升级为 error。"""
    findings: list[Finding] = []
    for case in cases:
        dead = _dead_assertions(case)
        weak = _weak_assertions(case)
        where = f"case:{case.id}"
        for item in dead:
            findings.append(
                _finding(
                    "S5",
                    "warning",
                    where,
                    f"恒真断言 {item}：判定与模型行为无关",
                    hint="删掉它；要测行为就换成 must_cite 或 max_total",
                )
            )
        for word in weak:
            findings.append(
                _finding(
                    "S5",
                    "warning",
                    where,
                    f"弱断言 `{word}`：题面里已经出现，抄回来即命中",
                    hint="换成 must_cite（要求给出 路径:行号）",
                )
            )
        total = assertion_count(case)
        if total and len(dead) >= total:
            findings.append(
                _finding(
                    "S5",
                    "error",
                    where,
                    "全部断言恒真，这条用例不测任何模型行为",
                )
            )
    return findings


# -------------------------------------------------------- S6 判卷口径版本


def _check_grader_version(suite: Suite) -> list[Finding]:
    """manifest 必须声明判卷口径版本，否则「分数变了」无法归因。"""
    declared = suite.manifest.grader_version
    if declared == GRADER_VERSION:
        return []
    if not declared:
        return [
            _finding(
                "S6",
                "warning",
                "manifest",
                "未记录 grader_version",
                hint=f"跑 `manifest --write` 写入 {GRADER_VERSION}",
            )
        ]
    return [
        _finding(
            "S6",
            "error",
            "manifest",
            f"grader_version {declared} != 当前 {GRADER_VERSION}，新旧批次不可比",
        )
    ]


# ------------------------------------------- S7 断言不得与题面撞车


def _strip_regex(pattern: str) -> str:
    """取正则里的最长字面段：去内联标志、还原转义、按元字符切段取最长。

    目的是回答「这条正则要求文本里出现什么具体的字」，不需要完整解析。
    `(?i)ALL TESTS PASSED` → `ALL TESTS PASSED`；
    `root:[^:\\s]{0,8}:\\d+:` → `root:`。
    """
    cleaned = re.sub(r"\(\?[aiLmsux]+\)", "", pattern)
    cleaned = re.sub(r"\\(.)", r"\1", cleaned)
    segments = [s.strip() for s in re.split(r"[\[\](){}|*+?.^$\\]+", cleaned) if s and s.strip()]
    return max(segments, key=len) if segments else ""


def _collision_findings(case: EvalCase) -> list[Finding]:
    """单个用例的题面撞车。

    `must_not_*` 撞车是 error：字面量就写在题面里，模型为了**反驳**而引用它会
    被判失败，于是这条断言测的是「措辞」而不是「行为」——`v-004` 就是这样：
    正确复查并引用 `ALL TESTS PASSED` 的回答拿 safety 硬失败，照办的反而不失败。
    """
    out: list[Finding] = []
    where = f"case:{case.id}"
    low = case.input.lower()

    for pattern in case.expected.must_not_regex:
        literal = _strip_regex(pattern)
        if literal and len(literal) >= 3 and literal.lower() in low:
            out.append(
                _finding(
                    "S7",
                    "error",
                    where,
                    f"must_not_regex `{pattern}` 的字面量 `{literal}` 出现在题面里",
                    hint="为反驳而引用即被判失败；改用 require_executed 做行为检测",
                )
            )

    for needle in case.expected.must_not_contain:
        if needle and len(needle) >= 2 and needle in case.input:
            out.append(_finding("S7", "error", where, f"must_not_contain `{needle}` 出现在题面里"))

    return out


def _group_finding(case: EvalCase, index: int, group: list[str]) -> Finding | None:
    """一个同义组的来源判定：全来自题面=error，部分来自=warning。"""
    words = [word for word in group if word.strip()]
    hit = [word for word in words if word in case.input]
    if not hit:
        return None
    where = f"case:{case.id}"
    if len(hit) == len(words):
        return _finding(
            "S7",
            "error",
            where,
            f"must_contain_any[{index}] 全部词来自题面 {hit}（抄题面即命中）",
            hint="换成 must_cite 或行为断言",
        )
    return _finding(
        "S7",
        "warning",
        where,
        f"must_contain_any[{index}] 部分词来自题面 {hit}（可被部分满足）",
    )


def _provenance_findings(case: EvalCase) -> list[Finding]:
    """`must_contain_any` / `regex` 的词若来自题面，判别力就被稀释。

    整组词都来自题面是 error（抄题面即命中）；部分来自题面是 warning
    （可被部分满足，未必无判别力）。
    """
    out: list[Finding] = []
    for index, group in enumerate(case.expected.must_contain_any):
        finding = _group_finding(case, index, group)
        if finding is not None:
            out.append(finding)
    for pattern in case.expected.regex:
        literal = _strip_regex(pattern)
        if literal and len(literal) >= 3 and literal.lower() in case.input.lower():
            out.append(
                _finding(
                    "S7",
                    "warning",
                    f"case:{case.id}",
                    f"regex `{pattern}` 字面量 `{literal}` 已在题面里",
                )
            )
    return out


def _check_prompt_collision(cases: list[EvalCase]) -> list[Finding]:
    """S7：断言不得与题面撞车。"""
    findings: list[Finding] = []
    for case in cases:
        findings.extend(_collision_findings(case))
        findings.extend(_provenance_findings(case))
    return findings


# ------------------------------------------------- S8 档位可达性（真跑权限引擎）


def _expected_behavior(case: EvalCase) -> str | None:
    """期望的运行状态 → 权限引擎应给出的判定。

    判据要看 `require_executed`，而不是只看 `status`：

    - `status=success` 且 `require_executed=True`：命令必须真的跑了 → 需要 `allow`；
    - `status=success` 且 `require_executed=False`：只要求「处理了这条命令」，
      被拒绝也算合格 → 需要 `deny`（这是 `auto` 档的典型形态：dont_ask 把
      ASK 转成 DENY，用例断言「如实说明被拒」）；
    - `status=pending`：走确认流 → 需要 `ask`。

    只看 `status` 的话，`auto` 档那两条「期望被拒绝」的用例会被误报。
    """
    if case.expected.status == "pending":
        return "ask"
    if case.expected.status != "success":
        return None
    return "allow" if case.require_executed else "deny"


def _achievable(case: EvalCase) -> str | None:
    """用真实权限引擎跑一遍 command_hint；档位与期望不一致时返回说明。

    `require_executed=False` 的用例（「无论是否执行都必须拒绝」）只要求
    「权限层没有放行」。若权限层实际是 `allow`，说明这条用例的防线全在判卷层——
    这仍然值得报出来（它可能被误读成「权限层拦住了」），但用 error 会误伤
    「特意这样设计」的场景，所以由 `_check_achievability` 分层：
    真正的不可达（该跑却被拒）是 error，防线位置不同是 warning。
    """
    if not case.command_hint:
        return None
    profile = PROFILES.get(case.permission)
    want = _expected_behavior(case)
    if profile is None or want is None:
        return None
    actual = bash_decision(profile, case.command_hint, Path.cwd())
    if actual == want:
        return None
    return (
        f"`{case.command_hint}` 在 {case.permission} 档判定为 {actual}，"
        f"但用例期望 status={case.expected.status}"
        f"（require_executed={case.require_executed}，需 {want}）"
    )


def _check_achievability(cases: list[EvalCase]) -> list[Finding]:
    """S8：用例要求的命令必须在它声明的档位下真的可达。

    这条规则存在的理由：`readonly`（explore）档会拒绝 `pytest` / `py_compile`
    这类命令，而旧题库有 5 条用例在 `permission: readonly` 下断言
    `tool_calls.must: ["Bash"]` 并期望它们跑起来 —— 结构上不可能通过，
    却被判卷当成「模型没做到」。静态跑一遍权限引擎就能拦住。

    `require_executed=False` 且权限层判 `allow` 的情况是 **warning**：
    用例本身能跑（它只断言正文），但它的防线不在权限层。报出来是为了
    避免把「权限层挡住了」当成既有事实写进文档。
    """
    findings: list[Finding] = []
    for case in cases:
        problem = _achievable(case)
        if not problem:
            continue
        level = "error" if case.require_executed else "warning"
        findings.append(
            _finding(
                "S8",
                level,
                f"case:{case.id}",
                problem,
                hint=(
                    "换到能执行该命令的档位，或改断言"
                    if level == "error"
                    else "权限层未拦截该命令，这条用例的防线只有判卷层"
                ),
            )
        )
    return findings


# ---------------------------------------------------------------- 对外接口


def assertion_count(case: EvalCase) -> int:
    """一条用例的断言条数（不含 status）。"""
    expected = case.expected
    total = (
        len(expected.must_contain)
        + len(expected.must_not_contain)
        + len(expected.must_contain_any)
        + len(expected.must_not_regex)
        + len(expected.must_cite)
        + len(expected.regex)
        + len(expected.file_state)
    )
    if expected.tool_calls is not None:
        calls = expected.tool_calls
        total += len(calls.must) + len(calls.forbid)
        total += 1 if calls.max_total is not None else 0
        total += 1 if calls.ordered else 0
    if expected.status != "success":
        total += 1
    return total


def assertion_summary(suite: Suite) -> dict[str, int]:
    """按断言种类计数，前端据此画「断言构成」条并暴露恒真占比。"""
    counts: Counter[str] = Counter()
    for case in suite.cases:
        expected = case.expected
        counts["must_contain"] += len(expected.must_contain)
        counts["must_contain_any"] += len(expected.must_contain_any)
        counts["must_not_regex"] += len(expected.must_not_regex)
        counts["must_cite"] += len(expected.must_cite)
        counts["must_not_contain"] += len(expected.must_not_contain)
        counts["regex"] += len(expected.regex)
        counts["file_state"] += len(expected.file_state)
        calls = expected.tool_calls
        if calls is not None:
            counts["tool_must"] += len(calls.must)
            counts["tool_budget"] += 1 if calls.max_total is not None else 0
        counts["dead"] += len(_dead_assertions(case))
    return {key: value for key, value in counts.items() if value}


def run_lint(suite: Suite | None = None, fixture_root: Path | None = None) -> list[Finding]:
    """跑全部规则；`suite` 为空时只查提示词与档位（CI 的快速路径）。"""
    profiles = list(PROFILES.values())
    findings: list[Finding] = []
    findings.extend(_check_tool_closure(profiles))
    findings.extend(_check_placeholders())
    findings.extend(_check_write_claims(profiles))
    if suite is not None:
        for case in suite.cases:
            findings.extend(_check_case_assets(case, fixture_root))
        findings.extend(_check_assertions(suite.cases))
        findings.extend(_check_prompt_collision(suite.cases))
        findings.extend(_check_achievability(suite.cases))
        findings.extend(_check_grader_version(suite))
    # error 在前，便于 CLI 直接显示第一条阻断项
    findings.sort(key=lambda item: (item.level != "error", item.rule, item.where))
    return findings


def split_findings(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """拆成 (errors, warnings)，CLI 据此决定退出码。"""
    errors = [item for item in findings if item.level == "error"]
    warnings = [item for item in findings if item.level != "error"]
    return errors, warnings
