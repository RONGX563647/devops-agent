"""评测配置与用例覆盖层：设置校验、覆盖层读写、以及可写 HTTP 契约。

重点不在「能存能读」，而在几件不该发生的事：

- 非法配置被静默接受（并发 99、模型名带空格、未知字段）；
- 非法用例落盘后才在采集阶段炸掉（题库里出现一条跑不起来的用例）；
- 在线改用例把 git 里的题库文件也改了（`eval/openjudge/cases/` 必须零改动）；
- 删掉仓库用例后覆盖率基线假红（`validate` 的口径只看仓库）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devops_agent.openjudge import benchmark, casestore, settings, store
from devops_agent.openjudge.api_edit import router


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "oj.db"
    monkeypatch.setenv("DEVOPS_AGENT_OPENJUDGE_DB_PATH", str(target))
    with store.connect() as conn:
        store.init_db(conn)
    return target


@pytest.fixture()
def overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """覆盖层指到临时目录：测试不许碰开发者的 workspace。"""
    target = tmp_path / "overlay"
    monkeypatch.setenv(casestore.OVERLAY_ENV, str(target))
    return target


@pytest.fixture()
def client(db: Path, overlay: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _case_raw(**kwargs: Any) -> dict[str, Any]:
    raw = {
        "id": "a-new-01",
        "track": "A",
        "risk": "low",
        "permission": "readonly",
        "fixture": "python-basic",
        "subject": "repo_qa",
        "turns": ["这个仓库是做什么的？"],
        "expected": {"must_contain": ["devops"]},
    }
    raw.update(kwargs)
    return raw


# ---------------------------------------------------------------- 设置


def test_settings_defaults_when_table_empty(db: Path) -> None:
    values = settings.load()
    assert values == settings.DEFAULTS


def test_settings_has_checker_for_every_field(db: Path) -> None:
    """新增一个配置项却忘了写校验函数，必须在这里红，而不是在界面上崩。

    `validate()` 直接按键查 `_CHECKERS`：KeyError 会以 500 露出去。
    """
    assert set(settings._CHECKERS) == set(settings.DEFAULTS)


def test_settings_patch_merges_and_persists(db: Path) -> None:
    settings.save({"target_model": "deepseek-v4.1-flash"})
    values = settings.save({"concurrency": 4})
    assert values["target_model"] == "deepseek-v4.1-flash"
    assert values["concurrency"] == 4
    assert settings.load()["concurrency"] == 4


@pytest.mark.parametrize(
    ("patch", "fragment"),
    [
        ({"concurrency": 99}, "concurrency"),
        ({"concurrency": 0}, "concurrency"),
        ({"timeout": 5}, "timeout"),
        ({"judge": "yes"}, "judge"),
        ({"target_model": "带 空格 的名字"}, "target_model"),
        ({"unknown": 1}, "未知字段"),
    ],
)
def test_settings_rejects_bad_values(db: Path, patch: dict[str, Any], fragment: str) -> None:
    with pytest.raises(settings.SettingsError) as exc:
        settings.save(patch)
    assert fragment in str(exc.value)


def test_settings_empty_model_follows_default(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "fallback-model")
    assert settings.resolved()["target_model"] == "fallback-model"
    settings.save({"target_model": "eval-only-model"})
    assert settings.resolved()["target_model"] == "eval-only-model"


def test_settings_judge_falls_back_to_target(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "base-model")
    monkeypatch.delenv("DEVOPS_AGENT_JUDGE_MODEL", raising=False)
    assert settings.resolved()["judge_model"] == "base-model"


def test_patch_settings_endpoint_reports_effective(client: TestClient) -> None:
    payload = client.patch("/api/openjudge/settings", json={"judge": True}).json()
    assert payload["values"]["judge"] is True
    assert set(payload["limits"]) >= {"max_concurrency", "max_timeout", "tracks"}
    assert "target_model" in payload["effective"]


def test_patch_settings_endpoint_rejects_bad_value(client: TestClient) -> None:
    response = client.patch("/api/openjudge/settings", json={"concurrency": 9})
    assert response.status_code == 422


# ---------------------------------------------------------------- 覆盖层


def test_save_case_writes_overlay_not_repo(overlay: Path) -> None:
    casestore.save(_case_raw())
    assert (overlay / "overlay.jsonl").is_file()
    ids = {row["id"] for row in casestore.list_cases()}
    assert "a-new-01" in ids
    # 仓库文件一个字节都不许动
    assert "a-new-01" not in {raw["id"] for raw in casestore.repo_raw().values()}


def test_save_case_rejects_broken_structure(overlay: Path) -> None:
    with pytest.raises(casestore.CaseError):
        casestore.save(_case_raw(track="Z"))
    with pytest.raises(casestore.CaseError):
        casestore.save(_case_raw(expected={"unknown_field": 1}))
    # 校验失败时磁盘上不该留下半成品
    assert not (overlay / "overlay.jsonl").exists()


def test_save_case_rejects_unknown_fixture(overlay: Path) -> None:
    with pytest.raises(casestore.CaseError) as exc:
        casestore.save(_case_raw(fixture="nope-fixture"))
    assert "fixture" in str(exc.value)


def test_overlay_overrides_repo_case(overlay: Path) -> None:
    target = sorted(casestore.repo_raw())[0]
    original = casestore.repo_raw()[target]
    casestore.save({**original, "owner": "改过的人"})
    row = casestore.get_raw(target)
    assert row is not None
    assert row["owner"] == "改过的人"
    assert row["_source"] == "overlay"
    assert casestore.get_raw(target)["_deleted_from_repo"] is True


def test_delete_repo_case_hides_it_but_keeps_file(overlay: Path) -> None:
    target = sorted(casestore.repo_raw())[0]
    before = len(casestore.load_cases())
    assert casestore.delete(target) is True
    assert len(casestore.load_cases()) == before - 1
    assert target in casestore.deleted_ids()
    assert target in casestore.repo_raw()  # 文件里还在，只是被隐藏
    assert len(casestore.repo_cases()) == before  # validate 口径不受影响


def test_restore_brings_repo_version_back(overlay: Path) -> None:
    target = sorted(casestore.repo_raw())[0]
    casestore.delete(target)
    assert casestore.restore(target) is True
    assert target not in casestore.deleted_ids()
    assert casestore.get_raw(target)["_source"] == "repo"


def test_restore_drops_locally_added_case(overlay: Path) -> None:
    casestore.save(_case_raw())
    assert casestore.restore("a-new-01") is True
    assert "a-new-01" not in {row["id"] for row in casestore.list_cases()}


def test_suite_sha_changes_with_overlay(overlay: Path) -> None:
    before = casestore.suite_view()["suite_sha"]
    raw = casestore.repo_raw()[sorted(casestore.repo_raw())[0]]
    casestore.save({**raw, "turns": ["换了一个问题，指纹必须变"]})
    assert casestore.suite_view()["suite_sha"] != before


def test_overlay_files_are_valid_jsonl(overlay: Path) -> None:
    casestore.save(_case_raw())
    casestore.save(_case_raw(id="a-new-02"))
    lines = (overlay / "overlay.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["id"] for line in lines] == ["a-new-01", "a-new-02"]


# ---------------------------------------------------------------- HTTP


def test_list_cases_endpoint_marks_source(client: TestClient) -> None:
    payload = client.get("/api/openjudge/cases").json()
    assert payload["total"] >= 1
    assert {row["source"] for row in payload["items"]} <= {"repo", "overlay"}
    assert payload["suite"]["cases"] == payload["total"]


def test_write_case_endpoint_and_read_back(client: TestClient) -> None:
    response = client.put("/api/openjudge/cases/a-new-01", json=_case_raw())
    assert response.status_code == 200
    assert response.json()["case"]["_source"] == "overlay"
    got = client.get("/api/openjudge/cases/a-new-01").json()
    assert got["case"]["turns"] == ["这个仓库是做什么的？"]


def test_write_case_endpoint_rejects_rename(client: TestClient) -> None:
    response = client.put("/api/openjudge/cases/a-new-01", json=_case_raw(id="a-other-01"))
    assert response.status_code == 422
    assert "id" in response.json()["detail"]


def test_write_case_endpoint_rejects_invalid(client: TestClient) -> None:
    response = client.put("/api/openjudge/cases/a-new-01", json=_case_raw(track="Z"))
    assert response.status_code == 422
    assert client.get("/api/openjudge/cases/a-new-01").status_code == 404


def test_delete_and_restore_endpoints(client: TestClient) -> None:
    target = sorted(casestore.repo_raw())[0]
    assert client.delete(f"/api/openjudge/cases/{target}").status_code == 200
    assert target not in {row["id"] for row in client.get("/api/openjudge/cases").json()["items"]}
    assert client.post(f"/api/openjudge/cases/{target}/restore").status_code == 200
    assert target in {row["id"] for row in client.get("/api/openjudge/cases").json()["items"]}


def test_unknown_case_is_404(client: TestClient) -> None:
    assert client.get("/api/openjudge/cases/does-not-exist").status_code == 404
    assert client.delete("/api/openjudge/cases/does-not-exist").status_code == 404
    assert client.post("/api/openjudge/cases/does-not-exist/restore").status_code == 404


def test_case_id_with_slash_is_rejected(client: TestClient) -> None:
    assert client.get("/api/openjudge/cases/a%2Fb").status_code == 404


def test_batch_case_detail_is_nested_under_batch(db: Path) -> None:
    """单条批次明细挂在 /batches/{id}/cases/{row}，不能和题库路径抢同一个 URL。"""
    app = FastAPI()
    from devops_agent.openjudge.api import router as read_router

    app.include_router(read_router)
    client = TestClient(app)
    with store.connect() as conn:
        store.init_db(conn)
        batch_id = store.insert_batch(
            conn,
            suite_sha="s",
            plan_sha="p",
            rubric_sha="",
            prompt_sha="",
            model="m",
            oj_version="",
            config_id="default",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
            verdict="pass",
            summary={"kpi": {}},
        )
    assert client.get(f"/api/openjudge/batches/{batch_id}/cases/999").status_code == 404


def test_validate_uses_repo_cases_only(overlay: Path) -> None:
    """本地删题不该让覆盖基线假红：validate 口径只看仓库。"""
    target = sorted(casestore.repo_raw())[0]
    casestore.delete(target)
    repo = casestore.repo_cases()
    coverage = benchmark.coverage(repo)
    assert coverage["tracks"]["A"] >= 1
    assert len({case.id for case in repo}) == len(casestore.repo_raw())


def test_overlay_survives_reopen(overlay: Path) -> None:
    casestore.save(_case_raw())
    del overlay  # 只用来标记「这里依赖的是文件系统而不是内存状态」
    again = casestore.get_raw("a-new-01")
    assert again is not None and again["_source"] == "overlay"


def test_settings_table_is_created_by_init_db(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "oj_settings" in tables
