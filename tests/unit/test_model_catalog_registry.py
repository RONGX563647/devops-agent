"""供应商注册表（SPEC-0018 §4）：零配置等价、URL 校验、非法配置可诊断。

这份测试的基准不是「跑通」，是「升级不改运行参数」：A35 是硬约束，
匿名 provider 路径下任何一个既有字段的值或来源变了都算回归。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devops_agent import config as cfg
from devops_agent.model_catalog import registry
from devops_agent.model_catalog.schema import ProviderError
from devops_agent.model_settings import Source, resolve, sources_of, values_of


@pytest.fixture(autouse=True)
def _clear() -> None:
    """每个用例前后清缓存：注册表缓存键含 env，不清会串。"""
    registry.invalidate()
    cfg.clear_cache()
    yield
    registry.invalidate()
    cfg.clear_cache()


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    (tmp_path / "providers.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("DEVOPS_AGENT_CONFIG_DIR", str(tmp_path))
    registry.invalidate()
    cfg.clear_cache()


# --- A35 零配置等价 ----------------------------------------------------


def test_anonymous_registry_is_equivalent_to_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """A35：没有 providers.yaml 时，既有字段的值与来源逐项不变。

    新增的 `provider` / `context_auto` 单独断言 —— 「多了一个键」不是回归，
    把它们混进等价性比较会让这条用例永远红。
    """
    monkeypatch.delenv("DEVOPS_AGENT_MODEL", raising=False)
    monkeypatch.delenv("DEVOPS_AGENT_BASE_URL", raising=False)
    reg = registry.load_registry()
    assert reg.anonymous is True
    assert reg.ids() == ["default"]
    assert reg.providers[0].kind == "openai_chat"

    resolved = resolve()
    values = values_of(resolved)
    sources = sources_of(resolved)

    # 既有字段：值与来源都必须和改动前一致（model.yaml 里显式写了这些）
    assert values["context_size"] == 128_000
    assert sources["context_size"] == Source.YAML.value
    assert values["temperature"] is None
    assert sources["temperature"] == Source.YAML.value
    assert values["max_iters"] == 12
    assert sources["max_iters"] == Source.YAML.value
    # model.yaml 的 name 是空串，且没有 env → 回落框架默认（与改动前同）
    assert values["model"] == ""
    assert sources["model"] == Source.FRAMEWORK.value

    # 新增字段：匿名路径下不开启跟随
    assert values["provider"] == ""
    assert values["context_auto"] is False


def test_anonymous_kind_comes_from_env_not_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """匿名 provider 的 kind 不做主机名推断。

    DashScope 兼容模式今天跑的是 `OpenAIChatModel`；若按主机名判成
    `dashscope_chat`，请求形状与 `Parameters` 字段集全变，A35 立刻破掉。
    """
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    registry.invalidate()
    assert registry.load_registry().providers[0].kind == "openai_chat"

    monkeypatch.setenv("DEVOPS_AGENT_PROVIDER_KIND", "dashscope_chat")
    registry.invalidate()
    assert registry.load_registry().providers[0].kind == "dashscope_chat"


def test_empty_providers_list_is_anonymous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`providers: []` 与文件缺失等价（§13 的回滚开关 2）。"""
    _write_config(tmp_path, monkeypatch, "providers: []\n")
    assert registry.load_registry().anonymous is True


# --- A42 URL 校验 ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://1.2.3.4/v1",  # 明文发 key 到公网
        "https://u:p@host/v1",  # 凭据藏在 URL 里
        "file:///x",  # 非 http 协议
        "ftp://example.com/v1",
        "https://",  # 缺主机
    ],
)
def test_check_url_rejects(url: str) -> None:
    """A42：这四类都必须拒绝，理由都是「会把 key 发错地方」。"""
    with pytest.raises(ProviderError):
        registry.check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/v1",  # 内网 vLLM
        "http://localhost:11434",
        "http://[::1]:8000/v1",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "",  # 匿名 provider 可能没配地址
    ],
)
def test_check_url_accepts(url: str) -> None:
    """A42：回环 http 必须可用 —— 拒绝的理由是明文发 key，不是「内网不安全」。"""
    registry.check_url(url)


def test_url_rejection_message_says_how_to_fix() -> None:
    """错误文案要给改法，否则运维只知道被拒、不知道怎么办。"""
    with pytest.raises(ProviderError) as exc:
        registry.check_url("http://10.0.0.5/v1")
    assert "https" in exc.value.reason
    assert "127.0.0.1" in exc.value.reason


# --- 非法配置在加载期报错 ----------------------------------------------


def _provider(body: str) -> str:
    return "providers:\n" + body


def test_duplicate_id_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复 id 不静默取第一个：否则「配了三个只跑一个」无从发现。"""
    _write_config(
        tmp_path,
        monkeypatch,
        _provider(
            "  - {id: a, kind: openai_chat, base_url: 'https://x/v1', api_key: none}\n"
            "  - {id: a, kind: openai_chat, base_url: 'https://y/v1', api_key: none}\n"
        ),
    )
    with pytest.raises(ProviderError) as exc:
        registry.load_registry()
    assert "重复" in exc.value.reason


def test_unknown_kind_lists_valid_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(
        tmp_path,
        monkeypatch,
        _provider("  - {id: a, kind: nope_chat, base_url: 'https://x/v1', api_key: none}\n"),
    )
    with pytest.raises(ProviderError) as exc:
        registry.load_registry()
    assert "openai_chat" in exc.value.reason


def test_bad_id_pattern_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(
        tmp_path,
        monkeypatch,
        _provider(
            "  - {id: 'Bad Id', kind: openai_chat, base_url: 'https://x/v1', api_key: none}\n"
        ),
    )
    with pytest.raises(ProviderError):
        registry.load_registry()


def test_default_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`default` 指向不存在的 id：报错并列出可用值，而不是回落到第一个。"""
    _write_config(
        tmp_path,
        monkeypatch,
        _provider("  - {id: a, kind: openai_chat, base_url: 'https://x/v1', api_key: none}\n")
        + "default: ghost\n",
    )
    with pytest.raises(ProviderError) as exc:
        registry.load_registry()
    assert "ghost" in exc.value.reason
    assert "a" in exc.value.reason


def test_bad_discover_mode_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(
        tmp_path,
        monkeypatch,
        _provider(
            "  - {id: a, kind: openai_chat, base_url: 'https://x/v1', api_key: none,"
            " discover: sometimes}\n"
        ),
    )
    with pytest.raises(ProviderError):
        registry.load_registry()


def test_discover_off_survives_yaml_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`off` 在 YAML 1.1 里是布尔 False，不能被 `or "models"` 悄悄改回联网模式。

    这个坑的后果很具体：用户写 `discover: off` 想「别打上游」，结果每次打开面板
    都发一次请求，而且没有任何报错 —— 声明与行为不一致是最难查的一类配置缺陷。
    """
    _write_config(
        tmp_path,
        monkeypatch,
        _provider(
            "  - {id: a, kind: openai_chat, base_url: 'https://x/v1', api_key: none,"
            " discover: off}\n"
            "  - {id: b, kind: openai_chat, base_url: 'https://y/v1', api_key: none,"
            " discover: on}\n"
        ),
    )
    loaded = registry.load_registry()
    assert loaded.get("a").discover == "off"
    # `on` 同理被解析成 True：它等价于默认的 models，而不是非法值
    assert loaded.get("b").discover == "models"


# --- 凭据解析与指纹 ----------------------------------------------------


def test_key_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    """三种写法语义必须可区分：`env:` 缺变量是「没配」，`none` 是「不需要」。"""
    monkeypatch.setenv("SOME_KEY", "sk-abcdefghijklmnopqrstuvwxyz012345")
    from devops_agent.model_catalog.schema import ProviderSpec

    env_spec = ProviderSpec(
        id="a", kind="openai_chat", base_url="https://x/v1", api_key="env:SOME_KEY"
    )
    assert env_spec.resolved_key == "sk-abcdefghijklmnopqrstuvwxyz012345"
    assert env_spec.configured is True

    missing = ProviderSpec(id="b", kind="openai_chat", base_url="https://x/v1", api_key="env:NOPE")
    assert missing.resolved_key is None
    assert missing.has_key is False
    assert missing.configured is False

    no_auth = ProviderSpec(
        id="c", kind="ollama_chat", base_url="http://127.0.0.1:11434", api_key="none"
    )
    assert no_auth.resolved_key == ""
    assert no_auth.has_key is True
    assert no_auth.configured is True


def test_key_fingerprint_tracks_key_not_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A41 的一半：换 key 指纹变，不换不变；且指纹与 provider id 无关。"""
    from devops_agent.model_catalog.schema import ProviderSpec

    monkeypatch.setenv("KEY_ONE", "sk-" + "a" * 32)
    monkeypatch.setenv("KEY_TWO", "sk-" + "b" * 32)
    one = ProviderSpec(id="x", kind="openai_chat", base_url="https://h/v1", api_key="env:KEY_ONE")
    two = ProviderSpec(id="x", kind="openai_chat", base_url="https://h/v1", api_key="env:KEY_TWO")
    other_id = ProviderSpec(
        id="y", kind="openai_chat", base_url="https://h/v1", api_key="env:KEY_ONE"
    )
    assert one.key_fingerprint != two.key_fingerprint
    assert one.key_fingerprint == other_id.key_fingerprint
    assert len(one.key_fingerprint) == 8


def test_host_hides_path_and_query() -> None:
    """A41 的一半：只回 host，URL 里可能带 token 查询参数。"""
    from devops_agent.model_catalog.schema import ProviderSpec

    spec = ProviderSpec(
        id="x",
        kind="openai_chat",
        base_url="https://gw.example.com:8443/v1?token=secret",
        api_key="none",
    )
    assert spec.host == "gw.example.com"


def test_missing_key_is_reported_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 key 的 provider 不阻止其他 provider 使用（A47 的一半）。"""
    monkeypatch.delenv("HAS_KEY", raising=False)
    monkeypatch.setenv("REAL_KEY", "sk-" + "c" * 32)
    _write_config(
        tmp_path,
        monkeypatch,
        _provider(
            "  - {id: broken, kind: openai_chat, base_url: 'https://x/v1', api_key: env:HAS_KEY}\n"
            "  - {id: good, kind: openai_chat, base_url: 'https://y/v1', api_key: env:REAL_KEY}\n"
        ),
    )
    reg = registry.load_registry()
    assert reg.missing_key() == ["broken"]
    assert reg.has_any_key() is True
    assert reg.get("good").configured is True
