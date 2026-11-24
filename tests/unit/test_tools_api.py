"""工具 schema 接口单测：前端 Inspector 的「Schema」页签依赖它。"""

from fastapi.testclient import TestClient

from devops_agent.gateway.app import app

client = TestClient(app)


def test_tool_schema_exposes_readonly_tools() -> None:
    resp = client.get("/api/tools/schema")
    assert resp.status_code == 200
    items = resp.json()
    names = {item["function"]["name"] for item in items}
    assert {"Bash", "Read", "Glob", "Grep"} <= names

    bash = next(item for item in items if item["function"]["name"] == "Bash")
    params = bash["function"]["parameters"]
    assert params["type"] == "object"
    assert "properties" in params
