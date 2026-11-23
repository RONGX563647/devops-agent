"""上下文接口单测：轨迹「第 0 步」展示的是模型动手前手里真正有的东西。"""

from fastapi.testclient import TestClient

from devops_agent.main import app

client = TestClient(app)


def test_context_returns_system_prompt_tools_and_workdir() -> None:
    resp = client.get("/api/context")
    assert resp.status_code == 200
    body = resp.json()

    prompt = body["system_prompt"]
    # 系统提示词里必须真写着工具约定，否则模型不知道自己有工具
    assert "DevOps 助手" in prompt
    assert "Bash" in prompt
    assert body["system_prompt_chars"] == len(prompt)

    names = {tool["name"] for tool in body["tools"]}
    assert {"Bash", "Read", "Glob", "Grep"} <= names
    assert all(tool["description"] for tool in body["tools"] if tool["name"] == "Bash")

    assert body["workdir"]


def test_context_tools_match_schema_endpoint() -> None:
    """两处清单必须同源，否则界面上「装了哪些工具」会和调用时对不上。"""
    context = client.get("/api/context").json()
    schema = client.get("/api/tools/schema").json()
    assert [tool["name"] for tool in context["tools"]] == [
        item["function"]["name"] for item in schema
    ]
