"""上下文接口单测：轨迹「第 0 步」展示的是模型动手前手里真正有的东西。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_agent.main import app
from devops_agent.tools.context_api import estimate_tokens

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


def test_context_reports_size_and_prefix() -> None:
    """工具 schema 是上下文的大头：只说「4 个工具」等于把它藏起来。"""
    body = client.get("/api/context").json()
    assert body["tools_chars"] == sum(tool["schema_chars"] for tool in body["tools"])
    assert body["tools_tokens"] == sum(tool["tokens"] for tool in body["tools"])
    assert body["tools_tokens"] > body["system_prompt_tokens"] * 5
    assert body["prefix_tokens"] == body["system_prompt_tokens"] + body["tools_tokens"]
    assert len(body["prefix_fingerprint"]) == 8
    # 每个工具都带完整 parameters，界面点开就能看，不用再猜
    assert all(tool["parameters"] for tool in body["tools"])


def test_fingerprint_survives_profile_switch() -> None:
    """切档不改前缀：指纹必须相同——这正是历史还能命中缓存的依据。"""
    first = client.get("/api/context?profile=readonly").json()
    second = client.get("/api/context?profile=readonly").json()
    workspace = client.get("/api/context?profile=workspace").json()
    assert first["prefix_fingerprint"] == second["prefix_fingerprint"]
    assert first["prefix_fingerprint"] == workspace["prefix_fingerprint"]
    # 档位说明本身仍然不同，只是放在消息末尾而不是前缀里
    assert first["policy_note"] != workspace["policy_note"]
    assert "只读" in first["policy_note"]


def test_fingerprint_tracks_workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """工作目录变了（换了部署/实验环境），前缀也就变了，指纹必须跟着变。"""
    from devops_agent.tools.schema import WORKDIR_ENV

    before = client.get("/api/context").json()["prefix_fingerprint"]
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path))
    after = client.get("/api/context").json()["prefix_fingerprint"]
    assert before != after


def test_context_unknown_profile_falls_back_to_default() -> None:
    body = client.get("/api/context?profile=not-a-profile").json()
    assert body["profile"]  # 不报错，回落到默认档


def test_estimate_tokens_calibrated_on_real_context() -> None:
    """估算要和实测同量级：409 字提示 + 一万字符 schema 实测约 3060 token。"""
    body = client.get("/api/context").json()
    assert 2600 < body["prefix_tokens"] < 3400
    assert estimate_tokens("中文四个字") == 3  # 5 个汉字 ÷ 1.4
