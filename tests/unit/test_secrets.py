"""密钥保险箱：加密落库、按需解密、占位符注入与回抹。

这里最该被钉住的不是「能存能取」，而是三条安全性质：
1. 库里存的不是明文（sqlite 文件被看到也不等于密钥泄漏）；
2. 换密钥后解不开时按未命中处理，不把整轮对话带崩；
3. 列表接口只给元信息；明文读回走单独端点且每次记一次 uses 留痕。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import devops_agent.secrets.vault as vault_mod
from devops_agent.gateway.app import app
from devops_agent.observability.repository import connect
from devops_agent.secrets.vault import (
    SecretVault,
    get_vault,
    hint,
    inject,
    mask,
    placeholder,
    reset_vault,
    safe_name,
)

client = TestClient(app)
SECRET = "sk-abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture
def store(tmp_path: Path) -> SecretVault:
    """每个用例一个独立库 + 独立密钥，互不干扰。"""
    return SecretVault(db_path=tmp_path / "s.db", key=Fernet.generate_key())


def test_put_get_roundtrip(store: SecretVault) -> None:
    meta = store.put("openai_key", SECRET)
    assert meta.hint == hint(SECRET)
    assert store.get("openai_key") == SECRET


def test_database_holds_ciphertext_not_plaintext(tmp_path: Path) -> None:
    """核心性质：落库的密文里不含明文片段。"""
    db = tmp_path / "s.db"
    SecretVault(db_path=db, key=Fernet.generate_key()).put("openai_key", SECRET)
    with connect(db) as conn:
        row = conn.execute("SELECT ciphertext FROM secrets").fetchone()
    assert SECRET not in row["ciphertext"]
    assert SECRET.encode() not in db.read_bytes()


def test_get_counts_usage(store: SecretVault) -> None:
    store.put("token", SECRET)
    assert store.list()[0].uses == 0
    assert store.get("token") == SECRET
    assert store.get("token") == SECRET
    meta = store.list()[0]
    assert meta.uses == 2
    assert meta.last_used_at is not None


def test_put_same_value_keeps_created_at(store: SecretVault) -> None:
    first = store.put("token", SECRET)
    second = store.put("token", SECRET)
    assert second.created_at == first.created_at


def test_put_new_value_replaces_ciphertext(store: SecretVault) -> None:
    store.put("token", SECRET)
    store.put("token", SECRET + "x")
    assert store.get("token") == SECRET + "x"


def test_hint_hides_chars_for_short_values() -> None:
    """≤12 位只报长度：4 位露前 4 位就是整串，12 位也泄了三分之一。"""
    for value in ("abcd", "abcdefghijkl"):
        result = hint(value)
        assert not set(result) & set(value), result
        assert f"{len(value)} 位" in result
    assert hint("abcdefghijklm").startswith("abcd")  # 13 位才允许露前 4 位


def test_redact_fingerprint_matches_vault_hint_threshold() -> None:
    """输入管线的阶段明细指纹必须与保险箱同一套阈值，短值不能露字符。

    两条链路曾各自实现：vault 有 ≤12 位守卫，redact 无条件露前 4 位，
    于是同一把短 key 进保险箱被遮、进 input_stages.detail 却露明文前缀。
    """
    from devops_agent.input.pipeline.redact import _fingerprint

    for value in ("abcd", "sk-abc", "abcdefghijkl", "abcdefghijklm", "sk-" + "x" * 40):
        assert _fingerprint(value) == hint(value), value


def test_get_missing_returns_none(store: SecretVault) -> None:
    assert store.get("nope") is None


def test_wrong_key_degrades_to_none(tmp_path: Path) -> None:
    """换过密钥就解不开——此时按未命中处理，占位符原样透传。"""
    db = tmp_path / "s.db"
    SecretVault(db_path=db, key=Fernet.generate_key()).put("token", SECRET)
    other = SecretVault(db_path=db, key=Fernet.generate_key())
    assert other.get("token") is None


def test_list_and_delete(store: SecretVault) -> None:
    store.put("b_key", SECRET)
    store.put("a_key", SECRET + "2")
    assert [item.name for item in store.list()] == ["a_key", "b_key"]
    assert store.delete("a_key") is True
    assert store.delete("a_key") is False
    assert [item.name for item in store.list()] == ["b_key"]


def test_safe_name_keeps_placeholder_syntax() -> None:
    assert safe_name("api-key.v2") == "api_key_v2"
    assert safe_name("!!!") == "secret"


def test_placeholder_inject_and_mask() -> None:
    text = "curl -H 'Authorization: Bearer {{secret:token}}'"
    filled, resolved = inject(text, {"token": SECRET}.get)
    assert SECRET in filled
    assert resolved == {"token": SECRET}
    assert mask(filled, resolved) == text
    assert "{{secret:token}}" in mask(filled, resolved)


def test_inject_leaves_unknown_placeholder() -> None:
    text = "echo {{secret:missing}}"
    filled, resolved = inject(text, {"token": SECRET}.get)
    assert filled == text
    assert resolved == {}


def test_mask_ignores_empty_secret() -> None:
    assert mask("abc", {"token": ""}) == "abc"


def test_placeholder_renders_canonical_form() -> None:
    assert placeholder("token") == "{{secret:token}}"


def test_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(vault_mod.KEY_ENV, key)
    assert vault_mod.load_key() == key.encode()


def test_key_file_is_created_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(vault_mod.KEY_ENV, raising=False)
    path = tmp_path / "k" / "secret.key"
    first = vault_mod.load_key(path)
    assert path.is_file()
    assert vault_mod.load_key(path) == first


def test_vault_singleton_and_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "v.db"))
    monkeypatch.setenv(vault_mod.KEY_ENV, Fernet.generate_key().decode())
    reset_vault()
    try:
        first = get_vault()
        assert get_vault() is first
        reset_vault()
        # 用保存下来的引用比对：直接写 get_vault() is not get_vault() 会被
        # 解释器回收地址后复用同一内存，偶发误判
        assert get_vault() is not first
    finally:
        reset_vault()


def test_resolver_returns_none_when_vault_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """保箱不可用时解析函数必须返回 None，而不是把异常抛给工具调用。"""

    def boom() -> object:
        raise RuntimeError("broken")

    monkeypatch.setattr(vault_mod, "get_vault", boom)
    assert vault_mod.resolver()("token") is None


def test_api_lists_metadata_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """接口层不给读回明文的通道：返回体里连 ciphertext 字段都没有。"""
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv(vault_mod.KEY_ENV, Fernet.generate_key().decode())
    reset_vault()
    try:
        get_vault().put("openai_key", SECRET)
        resp = client.get("/api/secrets")
        assert resp.status_code == 200
        body = resp.json()
        assert body[0]["name"] == "openai_key"
        assert body[0]["hint"] == hint(SECRET)
        assert set(body[0]) == {
            "name",
            "hint",
            "source",
            "created_at",
            "last_used_at",
            "uses",
        }
        assert SECRET not in resp.text

        assert client.delete("/api/secrets/openai_key").json() == {"deleted": True}
        assert client.get("/api/secrets").json() == []
    finally:
        reset_vault()


def test_placeholder_syntax_matches_the_parser() -> None:
    """占位符由解析管线产出、由工具中间件消费：两边语法漂移会静默失效。"""
    from devops_agent.input import pipeline as pipe

    assert pipe._PLACEHOLDER_RE.pattern == vault_mod.PLACEHOLDER_PATTERN


def test_name_for_value_reuses_existing_entry(store: SecretVault) -> None:
    """按值反查名字：输出捕获同值时复用已入库记录，不重复登记。"""
    store.put("openai_key", SECRET, source="input")
    assert store.name_for_value(SECRET) == "openai_key"
    assert store.name_for_value("some-other-value") is None


def test_api_read_value_records_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """受控读回返回明文，但每次都经 get() 记一次 uses。"""
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "api-value.db"))
    monkeypatch.setenv(vault_mod.KEY_ENV, Fernet.generate_key().decode())
    reset_vault()
    try:
        get_vault().put("captured", SECRET, source="output")
        before = next(m for m in get_vault().list() if m.name == "captured").uses

        resp = client.get("/api/secrets/captured/value")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"name": "captured", "value": SECRET, "found": True}

        after = next(m for m in get_vault().list() if m.name == "captured").uses
        assert after == before + 1  # 读回留痕
    finally:
        reset_vault()


def test_api_read_value_missing_is_not_distinguishable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不存在的名字返回 found=false，不暴露「库里有哪些 key」。"""
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "api-miss.db"))
    monkeypatch.setenv(vault_mod.KEY_ENV, Fernet.generate_key().decode())
    reset_vault()
    try:
        resp = client.get("/api/secrets/nope/value")
        assert resp.status_code == 200
        assert resp.json() == {"name": "nope", "value": "", "found": False}
    finally:
        reset_vault()
