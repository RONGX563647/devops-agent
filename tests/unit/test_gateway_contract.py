"""线协议契约与错误码的单点性测试。

这些用例不是在验「函数算得对」，而是钉住三件事：
1. 每个错误码都有状态码，且码值不重复（否则前端分不出该重试还是该改请求）；
2. `RUN_DONE` 必须带齐四个用量字段（漏一个就是命中率算错）；
3. 挂起载荷的写读必须闭环，且要容忍脏数据（老记录 / 手工改坏）。
"""

from __future__ import annotations

import json

import pytest

from devops_agent.gateway import contract
from devops_agent.gateway.errors import STATUS_OF, ErrorCode, http_error


def _named(frame: str) -> tuple[str, dict[str, object]]:
    """拆出 `event: <name>` 与 data 负载。"""
    lines = frame.strip().splitlines()
    name = lines[0].removeprefix("event: ")
    return name, json.loads(lines[1].removeprefix("data: "))


def test_error_codes_have_unique_status_mapping() -> None:
    assert set(STATUS_OF) == set(ErrorCode), "每个错误码都必须有状态码，不能漏"
    values = [code.value for code in ErrorCode]
    assert len(values) == len(set(values)), "码值重复会让前端无法区分语义"


def test_http_error_carries_code_and_status() -> None:
    exc = http_error(ErrorCode.EXPIRED)
    assert exc.status_code == 409
    assert exc.detail == "expired"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorCode.SESSION_BUSY, 409),
        (ErrorCode.SESSION_MISMATCH, 403),
        (ErrorCode.REQUEST_MISMATCH, 400),
        (ErrorCode.RUN_NOT_FOUND, 404),
    ],
)
def test_status_codes_keep_their_meaning(code: ErrorCode, status: int) -> None:
    """400=请求自相矛盾 / 403=越权 / 404=不存在 / 409=状态不允许，四者不能混。"""
    assert http_error(code).status_code == status


def test_done_frame_carries_all_usage_fields() -> None:
    name, payload = _named(
        contract.done_frame(
            run_id=7,
            text="做完了",
            usage=contract.TokenUsage(
                input_tokens=1,
                output_tokens=2,
                cache_input_tokens=3,
                cache_creation_input_tokens=4,
            ),
        )
    )
    assert name == "done"
    assert payload == {
        "type": "RUN_DONE",
        "run_id": 7,
        "status": "success",
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_input_tokens": 3,
        "cache_creation_input_tokens": 4,
        "text": "做完了",
    }


def test_parked_frame_is_named_and_never_done() -> None:
    name, payload = _named(
        contract.parked_frame(
            run_id=3,
            session_id="s1",
            expires_at="2026-01-01T00:10:00+00:00",
            tool_calls=[{"tool_call_id": "c1", "risk": "danger"}],
        )
    )
    assert name == "parked"
    assert payload["type"] == "RUN_PARKED"
    assert payload["expires_at"].endswith("+00:00")
    assert payload["tool_calls"] == [{"tool_call_id": "c1", "risk": "danger"}]


def test_pending_payload_roundtrip() -> None:
    payload = contract.PendingPayload(
        tool_calls=[{"tool_call_id": "c1", "input": "rm -rf build"}],
        expires_at="2026-01-01T00:10:00+00:00",
    )
    assert contract.PendingPayload.from_json(payload.to_json()) == payload


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", '{"tool_calls": 3}'])
def test_pending_payload_tolerates_dirty_rows(raw: str | None) -> None:
    """脏数据一律按空载荷处理：一张空卡片比一次 500 好，用户还能重新发消息。"""
    assert contract.PendingPayload.from_json(raw) == contract.PendingPayload()


def test_transport_constants_are_single_valued() -> None:
    """媒体类型 / 心跳帧 / 请求号头只在这一处定义，值变了前端必须跟着变。"""
    assert contract.SSE_MEDIA_TYPE == "text/event-stream"
    assert contract.PING_FRAME == ": ping\n\n"
    assert contract.REQUEST_ID_HEADER == "X-Request-ID"
    assert contract.SSE_HEADERS["X-Accel-Buffering"] == "no", "关不掉 nginx 缓冲就发不出流"
