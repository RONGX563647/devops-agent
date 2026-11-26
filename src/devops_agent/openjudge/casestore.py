"""题库的本地覆盖层：在线改用例，但不碰 git 里的题库文件。

为什么不直接写 `eval/openjudge/cases/*.jsonl`：

那个目录是仓库资产，CI 用 `validate --strict` 按它算覆盖率基线。在线编辑若直接改
源文件，会同时污染「正在跑的实现」和「待提交的 diff」——用户点一下删除，PR 里
就多一个意料外的改动。所以编辑一律落到覆盖层：

    workspace/openjudge/cases/overlay.jsonl   被改过 / 新增的用例（同 id 覆盖仓库版本）
    workspace/openjudge/cases/deleted.json    被隐藏的用例 id（仓库里仍在）

三条不变量：

1. 读合并写覆盖 2. 写入前过 parse_case+校验 3. 基线只看仓库，本地删题不假红。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from devops_agent.openjudge import benchmark

# openjudge/casestore.py -> openjudge -> devops_agent -> src -> 仓库根
REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_CASES = REPO_ROOT / "eval" / "openjudge" / "cases"
FIXTURES = REPO_ROOT / "eval" / "openjudge" / "fixtures"

#: 覆盖层的默认位置（`workspace/` 已 gitignore）。
DEFAULT_OVERLAY = REPO_ROOT / "workspace" / "openjudge" / "cases"
#: 测试与多环境隔离用；指到别处即可拿到一套干净的题库视图。
OVERLAY_ENV = "DEVOPS_AGENT_OPENJUDGE_OVERLAY"


class CaseError(ValueError):
    """用例不合法；接口层转 422，且保证磁盘上什么都没写。"""


def overlay_dir() -> Path:
    override = os.environ.get(OVERLAY_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_OVERLAY


def _overlay_file() -> Path:
    return overlay_dir() / "overlay.jsonl"


def _deleted_file() -> Path:
    return overlay_dir() / "deleted.json"


def repo_paths() -> list[Path]:
    """仓库里的题库文件，按文件名排序（A → E）。"""
    return sorted(REPO_CASES.glob("*.jsonl"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseError(f"{path.name}:{lineno}: 覆盖层 JSON 解析失败: {exc}") from exc
        if isinstance(raw, dict) and raw.get("id"):
            rows.append(raw)
    return rows


def repo_raw() -> dict[str, dict[str, Any]]:
    """仓库题库：id -> 原始 JSON（保留排版外的全部字段，编辑时原样回显）。"""
    out: dict[str, dict[str, Any]] = {}
    for path in repo_paths():
        for raw in _read_jsonl(path):
            out[str(raw["id"])] = raw
    return out


def overlay_raw() -> dict[str, dict[str, Any]]:
    return {str(raw["id"]): raw for raw in _read_jsonl(_overlay_file())}


def deleted_ids() -> list[str]:
    path = _deleted_file()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return []
    ids = raw.get("ids", []) if isinstance(raw, dict) else raw
    return sorted({str(item) for item in ids if str(item).strip()})


def effective_raw() -> list[dict[str, Any]]:
    """合并视图：仓库 → 覆盖 → 去掉隐藏项；按 (赛道, id) 排序，前端列表直接用。"""
    hidden = set(deleted_ids())
    merged = {**repo_raw(), **overlay_raw()}
    rows = [raw for key, raw in merged.items() if key not in hidden]
    return sorted(rows, key=lambda raw: (str(raw.get("track", "Z")), str(raw["id"])))


def list_cases() -> list[dict[str, Any]]:
    """带 `source` 标记的合并视图：`repo` 是原有、`overlay` 是本地改过或新增。"""
    base = repo_raw()
    overlay = set(overlay_raw())
    rows: list[dict[str, Any]] = []
    for raw in effective_raw():
        case_id = str(raw["id"])
        rows.append(
            {
                "id": case_id,
                "track": str(raw.get("track", "")),
                "risk": str(raw.get("risk", "")),
                "permission": str(raw.get("permission", "")),
                "subject": str(raw.get("subject", "")),
                "fixture": str(raw.get("fixture", "")),
                "offline": bool(raw.get("offline", False)),
                "source": "overlay" if case_id in overlay else "repo",
                "edited": case_id in overlay and case_id in base,
            }
        )
    return rows


def get_raw(case_id: str) -> dict[str, Any] | None:
    """单条原始 JSON；仓库版本带 `_source`，覆盖版本额外带 `_edited`。"""
    local = overlay_raw()
    for raw in effective_raw():
        if str(raw["id"]) == case_id:
            return {
                **raw,
                "_source": "overlay" if case_id in local else "repo",
                "_edited": case_id in local,
                "_deleted_from_repo": case_id in repo_raw(),
            }
    return None


def load_cases(track: str = "", limit: int = 0) -> list[benchmark.Case]:
    """执行用：合并视图 → `Case` 列表。任何一行坏掉都会抛，不做「跳过坏的」。"""
    cases = [benchmark.parse_case(raw) for raw in effective_raw()]
    if track:
        cases = [case for case in cases if case.track == track]
    if limit:
        cases = cases[:limit]
    return cases


def load_cases_by_ids(ids: list[str]) -> list[benchmark.Case]:
    """按传入顺序从合并视图取用例；未知 id 汇总一次报出。"""
    by_id = {case.id: case for case in load_cases()}
    unknown = list(dict.fromkeys(case_id for case_id in ids if case_id not in by_id))
    if unknown:
        raise ValueError(f"未知用例 id：{', '.join(unknown)}")
    return [by_id[case_id] for case_id in dict.fromkeys(ids)]


def repo_cases() -> list[benchmark.Case]:
    """`validate` / 覆盖率基线口径：只看仓库，不看本地覆盖层。"""
    paths = repo_paths()
    return benchmark.load_cases(paths) if paths else []


def suite_view() -> dict[str, Any]:
    """前端需要的「题库现状」：条数 + 指纹，覆盖层改动后指纹会变。"""
    cases = load_cases()
    return {
        "cases": len(cases),
        "suite_sha": benchmark.suite_sha(cases) if cases else "",
        "overlay": len(overlay_raw()),
        "deleted": len(deleted_ids()),
        "dir": str(overlay_dir()),
    }


def check(raw: dict[str, Any]) -> benchmark.Case:
    """结构校验：解析成功 + 单条静态检查通过，才允许落盘。"""
    if not isinstance(raw, dict):
        raise CaseError("用例必须是一个 JSON 对象")
    try:
        case = benchmark.parse_case(raw)
    except ValueError as exc:
        raise CaseError(str(exc)) from exc
    problems = benchmark.validate([case], FIXTURES, strict=False)
    if problems:
        raise CaseError("；".join(problems))
    return case


def save(raw: dict[str, Any]) -> dict[str, Any]:
    """新增或覆盖一条用例（同 id 覆盖），原子写覆盖层。"""
    check(raw)
    rows = overlay_raw()
    rows[str(raw["id"])] = raw
    _write_overlay(rows.values())
    _drop_deleted(str(raw["id"]))
    return {"id": str(raw["id"]), "source": "overlay"}


def delete(case_id: str) -> bool:
    """从题库里隐藏一条：覆盖层里有就删掉，仓库里有就记进隐藏清单。"""
    in_repo = case_id in repo_raw()
    rows = overlay_raw()
    removed_overlay = rows.pop(case_id, None) is not None
    if removed_overlay:
        _write_overlay(rows.values())
    if in_repo:
        _write_deleted(sorted({*deleted_ids(), case_id}))
    return removed_overlay or in_repo


def restore(case_id: str) -> bool:
    """恢复仓库版本：删掉覆盖并去掉隐藏标记。本地新增的用例会被直接丢弃。"""
    rows = overlay_raw()
    removed = rows.pop(case_id, None) is not None
    if removed:
        _write_overlay(rows.values())
    hidden = [item for item in deleted_ids() if item != case_id]
    if hidden != deleted_ids():
        _write_deleted(hidden)
    return removed or case_id in repo_raw()


def _drop_deleted(case_id: str) -> None:
    hidden = [item for item in deleted_ids() if item != case_id]
    if hidden != deleted_ids():
        _write_deleted(hidden)


def _write_overlay(rows: Any) -> None:
    ordered = sorted(rows, key=lambda raw: (str(raw.get("track", "Z")), str(raw["id"])))
    if not ordered:
        _overlay_file().unlink(missing_ok=True)
        return
    _atomic_write(
        _overlay_file(),
        "".join(json.dumps(raw, ensure_ascii=False) + "\n" for raw in ordered),
    )


def _write_deleted(ids: list[str]) -> None:
    if not ids:
        _deleted_file().unlink(missing_ok=True)
        return
    _atomic_write(_deleted_file(), json.dumps({"ids": ids}, ensure_ascii=False, indent=2) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再 `os.replace`：半截文件会让整个题库读不出来。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
