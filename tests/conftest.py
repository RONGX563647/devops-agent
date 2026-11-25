"""测试基线夹具：把「本机运行态」挡在测试之外。

为什么需要这个：健康分（`observability/health/quality.py`）会读最新一批
openjudge 评测的赛道通过率，作为效果质量的机器侧样本。默认库落在
`workspace/evals/openjudge.db` —— 那是开发者本地真跑评测的位置。

如果测试读它，「我昨天跑过一次评测」就会让一批断言变红，而且失败信息完全指
不到原因。测试必须与机器状态无关，因此这里把 openjudge 库指到临时目录：
需要真库的测试自己 `monkeypatch.setenv` 覆盖（如 `test_openjudge_online.py`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_openjudge_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试用独立的空库与空覆盖层。

    - DB：避免读到本机 `workspace/evals/openjudge.db`（跑过一次评测就会让健康分
      相关断言变红，且失败信息指不到原因）。
    - 覆盖层：避免测试写到本机 `workspace/openjudge/cases/`。那是「在线改用例」
      的真实落点，测试碰它等于改用户的题库。
    """
    monkeypatch.setenv("DEVOPS_AGENT_OPENJUDGE_DB_PATH", str(tmp_path / "openjudge.db"))
    monkeypatch.setenv("DEVOPS_AGENT_OPENJUDGE_OVERLAY", str(tmp_path / "openjudge-overlay"))
