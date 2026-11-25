"""评测题集：JSONL → Case，以及覆盖率/自洽性校验。

题库是**数据**而不是代码：每条用例一行 JSON，字段稳定、可 diff、可审计。
`validate` 同时被 CLI 与 CI 调用，任何「断言写错但测试永远绿」的用例都在这里
被拦下（未知字段、赛道缺失、权限档位不合法、引用不到 fixture）。

设计约束：
- 只依赖标准库，不 import 模型 / 框架，保证离线门禁可用；
- `suite_sha` 必须覆盖用例内容而不是文件字节，避免 CRLF / 缩进差异造成假不可比。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRACKS = ("A", "B", "C", "D", "E")
RISKS = ("low", "medium", "high_risk")
PERMISSIONS = ("readonly", "workspace", "execute", "auto", "unrestricted")

# 各赛道最低条数（SPEC §4 锁定配比），validate 用它防止题库被悄悄删薄
TRACK_QUOTA = {"A": 28, "B": 24, "C": 14, "D": 12, "E": 12}

_TOP_KEYS = {
    "id",
    "track",
    "subject",
    "risk",
    "permission",
    "fixture",
    "config_id",
    "owner",
    "provenance",
    "turns",
    "expected",
    "judge",
    "offline",
}
_EXPECTED_KEYS = {
    "events",
    "events_forbidden",
    "tools_required",
    "tools_forbidden",
    "tool_args",
    "must_contain",
    "must_contain_any",
    "must_not_contain",
    "must_cite",
    "file_state",
    "input_stages",
    "max_tool_calls",
    "status_any",
}
_FILE_STATE_KEYS = {"path", "contains", "absent", "sha256", "exists"}


@dataclass(frozen=True, slots=True)
class Turn:
    """一轮驱动：要么是用户输入，要么是一条确认决策。"""

    role: str = "user"
    text: str = ""
    action: str = ""
    decision: str = ""

    @property
    def is_confirm(self) -> bool:
        return self.action == "confirm"


@dataclass(frozen=True, slots=True)
class FileExpect:
    """工作区文件的终态断言（相对用例工作区）。"""

    path: str
    contains: str = ""
    absent: str = ""
    sha256: str = ""
    exists: bool | None = None


@dataclass(frozen=True, slots=True)
class Expected:
    """确定性断言集合；空字段表示不检查，而不是「检查为空」。"""

    events: tuple[str, ...] = ()
    events_forbidden: tuple[str, ...] = ()
    tools_required: tuple[str, ...] = ()
    tools_forbidden: tuple[str, ...] = ()
    tool_args: dict[str, dict[str, str]] = field(default_factory=dict)
    must_contain: tuple[str, ...] = ()
    must_contain_any: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    must_cite: tuple[str, ...] = ()
    file_state: tuple[FileExpect, ...] = ()
    input_stages: tuple[str, ...] = ()
    max_tool_calls: int | None = None
    # 终态白名单：部分边界用例「拒绝执行」与「挂起等确认」都算合格，
    # 区别只在 UX 而不是安全性，用 status_any 显式放宽。
    status_any: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Case:
    """一条评测用例（不可变，便于并发与哈希）。"""

    id: str
    track: str
    subject: str
    risk: str
    permission: str
    fixture: str
    config_id: str
    owner: str
    provenance: str
    turns: tuple[Turn, ...]
    expected: Expected
    judge: dict[str, Any] = field(default_factory=dict)
    offline: bool = False

    @property
    def prompt(self) -> str:
        """首轮用户输入，供判分器当作 query。"""
        for turn in self.turns:
            if not turn.is_confirm:
                return turn.text
        return ""


def _parse_turn(raw: Any, case_id: str) -> Turn:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError(f"{case_id}: 用户轮不能为空")
        return Turn(text=text)
    if not isinstance(raw, dict):
        raise ValueError(f"{case_id}: turn 必须是字符串或对象")
    action = str(raw.get("action", ""))
    if action == "confirm":
        decision = str(raw.get("decision", ""))
        if decision not in ("allow_once", "allow_always", "deny"):
            raise ValueError(f"{case_id}: 未知确认决策 {decision!r}")
        return Turn(action="confirm", decision=decision)
    text = str(raw.get("text", "")).strip()
    if not text:
        raise ValueError(f"{case_id}: 用户轮不能为空")
    return Turn(role=str(raw.get("role", "user")), text=text)


def _parse_file_state(raw: Any, case_id: str) -> FileExpect:
    if not isinstance(raw, dict) or not raw.get("path"):
        raise ValueError(f"{case_id}: file_state 项缺少 path")
    unknown = set(raw) - _FILE_STATE_KEYS
    if unknown:
        raise ValueError(f"{case_id}: file_state 未知字段 {sorted(unknown)}")
    return FileExpect(
        path=str(raw["path"]),
        contains=str(raw.get("contains", "")),
        absent=str(raw.get("absent", "")),
        sha256=str(raw.get("sha256", "")),
        exists=bool(raw["exists"]) if "exists" in raw else None,
    )


def _str_tuple(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(item) for item in raw.get(key, ()))


def _parse_tool_args(raw: dict[str, Any], case_id: str) -> dict[str, dict[str, str]]:
    tool_args: dict[str, dict[str, str]] = {}
    for name, args in (raw.get("tool_args") or {}).items():
        if not isinstance(args, dict):
            raise ValueError(f"{case_id}: tool_args.{name} 必须是对象")
        tool_args[str(name)] = {str(k): str(v) for k, v in args.items()}
    return tool_args


def _parse_expected(raw: Any, case_id: str) -> Expected:
    if raw is None:
        return Expected()
    if not isinstance(raw, dict):
        raise ValueError(f"{case_id}: expected 必须是对象")
    unknown = set(raw) - _EXPECTED_KEYS
    if unknown:
        raise ValueError(f"{case_id}: expected 未知字段 {sorted(unknown)}")
    status_any = _str_tuple(raw, "status_any")
    unknown_status = set(status_any) - {"success", "parked"}
    if unknown_status:
        raise ValueError(f"{case_id}: 未知 status_any {sorted(unknown_status)}")
    max_calls = raw.get("max_tool_calls")
    return Expected(
        events=_str_tuple(raw, "events"),
        events_forbidden=_str_tuple(raw, "events_forbidden"),
        tools_required=_str_tuple(raw, "tools_required"),
        tools_forbidden=_str_tuple(raw, "tools_forbidden"),
        tool_args=_parse_tool_args(raw, case_id),
        must_contain=_str_tuple(raw, "must_contain"),
        must_contain_any=_str_tuple(raw, "must_contain_any"),
        must_not_contain=_str_tuple(raw, "must_not_contain"),
        must_cite=_str_tuple(raw, "must_cite"),
        file_state=tuple(_parse_file_state(x, case_id) for x in raw.get("file_state", ())),
        input_stages=_str_tuple(raw, "input_stages"),
        max_tool_calls=int(max_calls) if max_calls is not None else None,
        status_any=status_any,
    )


def _require(value: str, allowed: tuple[str, ...], case_id: str, label: str) -> str:
    if value not in allowed:
        raise ValueError(f"{case_id}: 未知{label} {value!r}")
    return value


def _parse_turns(raw: dict[str, Any], case_id: str) -> tuple[Turn, ...]:
    turns_raw = raw.get("turns")
    if not isinstance(turns_raw, list) or not turns_raw:
        raise ValueError(f"{case_id}: turns 不能为空")
    turns = tuple(_parse_turn(item, case_id) for item in turns_raw)
    if not any(not turn.is_confirm for turn in turns):
        raise ValueError(f"{case_id}: 至少一轮用户输入")
    return turns


def parse_case(raw: dict[str, Any]) -> Case:
    """把一行 JSON 解析成 Case；任何结构错误都带 case id 抛出。

    1. 先查未知顶层字段：拼错的键必须报错，不能被静默忽略成默认值；
    2. id 必填，后续所有报错都带上它，才能定位到具体那一行；
    3. 枚举字段走 `_require` 白名单，turns / expected 各自递归解析。
    """
    unknown = set(raw) - _TOP_KEYS
    case_id = str(raw.get("id", "")).strip()
    if not case_id:
        raise ValueError("用例缺少 id")
    if unknown:
        raise ValueError(f"{case_id}: 未知顶层字段 {sorted(unknown)}")
    track = _require(str(raw.get("track", "")), TRACKS, case_id, "赛道")
    risk = _require(str(raw.get("risk", "")), RISKS, case_id, "风险等级")
    permission = _require(str(raw.get("permission", "")), PERMISSIONS, case_id, "权限档位")
    return Case(
        id=case_id,
        track=track,
        subject=str(raw.get("subject", "")),
        risk=risk,
        permission=permission,
        fixture=str(raw.get("fixture", "")),
        config_id=str(raw.get("config_id", "default")),
        owner=str(raw.get("owner", "")),
        provenance=str(raw.get("provenance", "")),
        turns=_parse_turns(raw, case_id),
        expected=_parse_expected(raw.get("expected"), case_id),
        judge=dict(raw.get("judge") or {}),
        offline=bool(raw.get("offline", False)),
    )


def load_cases(paths: list[Path] | tuple[Path, ...]) -> list[Case]:
    """按路径顺序读 JSONL；空行忽略，重复 id 直接报错。"""
    cases: list[Case] = []
    seen: set[str] = set()
    for path in paths:
        for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: JSON 解析失败: {exc}") from exc
            case = parse_case(raw)
            if case.id in seen:
                raise ValueError(f"{case.id}: 用例 id 重复")
            seen.add(case.id)
            cases.append(case)
    return cases


def suite_sha(cases: list[Case]) -> str:
    """题库指纹：按 id 排序后规范化 JSON 的 sha256（忽略文件排版差异）。"""
    digest = hashlib.sha256()
    for case in sorted(cases, key=lambda item: item.id):
        digest.update(case.id.encode())
        digest.update(
            json.dumps(
                {
                    "turns": [(t.role, t.text, t.action, t.decision) for t in case.turns],
                    "expected": {
                        "events": case.expected.events,
                        "events_forbidden": case.expected.events_forbidden,
                        "tools_required": case.expected.tools_required,
                        "tools_forbidden": case.expected.tools_forbidden,
                        "tool_args": case.expected.tool_args,
                        "must_contain": case.expected.must_contain,
                        "must_contain_any": case.expected.must_contain_any,
                        "must_not_contain": case.expected.must_not_contain,
                        "must_cite": case.expected.must_cite,
                        "file_state": [
                            (f.path, f.contains, f.absent, f.sha256, f.exists)
                            for f in case.expected.file_state
                        ],
                        "input_stages": case.expected.input_stages,
                        "max_tool_calls": case.expected.max_tool_calls,
                        "status_any": case.expected.status_any,
                    },
                    "judge": case.judge,
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        )
    return digest.hexdigest()


def _has_security_assertion(case: Case) -> bool:
    expected = case.expected
    return bool(
        expected.events
        or expected.events_forbidden
        or expected.tools_forbidden
        or expected.tools_required
        or expected.tool_args
        or expected.file_state
    )


def _confirm_problem(case: Case) -> list[str]:
    if not any(turn.is_confirm for turn in case.turns):
        return []
    expected = case.expected
    if expected.events or expected.status_any:
        return []
    return [f"{case.id}: 含确认轮但未声明事件/终态断言"]


def _d_track_problems(case: Case) -> list[str]:
    if case.track != "D":
        return []
    problems: list[str] = []
    if not case.offline:
        problems.append(f"{case.id}: D 赛道必须 offline（不依赖模型）")
    if not case.expected.input_stages:
        problems.append(f"{case.id}: D 赛道必须声明输入阶段断言")
    return problems


def _case_problems(case: Case, known_fixtures: set[str]) -> list[str]:
    """单条用例的静态问题；与配额无关。"""
    problems: list[str] = []
    if case.fixture not in known_fixtures:
        problems.append(f"{case.id}: fixture {case.fixture!r} 不存在")
    if case.track == "B" and not _has_security_assertion(case):
        problems.append(f"{case.id}: B 赛道必须声明权限类断言")
    problems.extend(_confirm_problem(case))
    problems.extend(_d_track_problems(case))
    return problems


def validate(cases: list[Case], fixtures: Path, *, strict: bool = True) -> list[str]:
    """校验题库：配比、fixture 存在、断言非空、确认轮与权限匹配。

    1. 先收 fixture 目录名，用例引用不存在的 fixture 直接算问题；
    2. 逐条跑单例检查：B 赛道要有权限类断言、确认轮要有事件/终态、
       D 赛道必须 offline 且声明输入阶段；
    3. `strict=True` 时再比 `TRACK_QUOTA`，防止题库被悄悄删薄。

    返回问题列表（空 = 通过）。`strict=True` 时配比不足也算问题，
    与 CI 的 `eval validate --strict` 口径一致。
    """
    problems: list[str] = []
    counts = {track: 0 for track in TRACKS}
    known_fixtures = {path.name for path in fixtures.iterdir() if path.is_dir()}
    for case in cases:
        counts[case.track] += 1
        problems.extend(_case_problems(case, known_fixtures))
    if strict:
        for track, quota in TRACK_QUOTA.items():
            if counts[track] < quota:
                problems.append(f"赛道 {track} 仅 {counts[track]} 条，低于配额 {quota}")
    return problems


def coverage(cases: list[Case]) -> dict[str, Any]:
    """能力覆盖矩阵：工具 / 权限 / 赛道 / 风险 的去重计数。"""
    tools: dict[str, int] = {}
    permissions: dict[str, int] = {}
    subjects: dict[str, int] = {}
    risks: dict[str, int] = {}
    for case in cases:
        for name in (*case.expected.tools_required, *case.expected.tool_args):
            tools[name] = tools.get(name, 0) + 1
        permissions[case.permission] = permissions.get(case.permission, 0) + 1
        subjects[case.subject] = subjects.get(case.subject, 0) + 1
        risks[case.risk] = risks.get(case.risk, 0) + 1
    return {
        "tracks": {track: sum(1 for c in cases if c.track == track) for track in TRACKS},
        "tools": tools,
        "permissions": permissions,
        "subjects": subjects,
        "risks": risks,
    }


def compare_coverage(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """覆盖基线只能涨不能跌。

    为什么需要它：`validate` 只校验「单条用例是否合法」，删掉半个题库它依然全绿。
    基线把「当前覆盖」钉成可 diff 的事实，删题必须先改基线，改基线才会出现在 PR 里。
    """
    problems: list[str] = []
    for key in ("tracks", "tools", "subjects", "risks"):
        declared = baseline.get(key) or {}
        actual = current.get(key) or {}
        for name, count in sorted(declared.items()):
            now = int(actual.get(name, 0))
            if now < int(count):
                problems.append(f"覆盖回退：{key}.{name} {count} → {now}")
    return problems
