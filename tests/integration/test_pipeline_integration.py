"""集成测试：从工厂装配到网关调用的最小闭环。"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from devops_agent.main import app
from devops_agent.runtime.assembly import build_agent
from devops_agent.runtime.supervisor import as_member, build_team_pipeline


def test_team_assembles_with_three_domains() -> None:
    leader = build_agent("leader", "route", MagicMock())
    members = [
        as_member(build_agent("delivery", "交付", MagicMock()), "可持续交付"),
        as_member(build_agent("schedule", "排期", MagicMock()), "项目排期"),
        as_member(build_agent("debug", "定位", MagicMock()), "Bug 测试定位"),
    ]
    team = build_team_pipeline(leader, members)
    assert team is not None


def test_health_endpoint() -> None:
    resp = TestClient(app).get("/health")
    assert resp.json()["status"] == "ok"
