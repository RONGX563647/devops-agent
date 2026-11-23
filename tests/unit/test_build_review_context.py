"""build_review_context.py 单测：脚本用 importlib 从 scripts/ 载入。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load():
    path = SCRIPTS / "build_review_context.py"
    spec = importlib.util.spec_from_file_location("build_review_context", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ctx(monkeypatch):
    module = _load()
    responses: dict[tuple[str, ...], str] = {}

    def fake_git(*args: str) -> str:
        return responses.get(args, "")

    monkeypatch.setattr(module, "git", fake_git)
    return module, responses


def test_changed_symbols_dedup_and_limit(ctx):
    module, _ = ctx
    diff = "\n".join(
        [
            "+def alpha():",
            "+class Beta:",
            "+    def alpha():",  # 重复名，应去重
            "-def removed():",  # 删除行不算
            "+async def gamma():",
        ]
    )
    assert module.changed_symbols(diff) == ["alpha", "Beta", "gamma"]


def test_changed_symbols_caps_at_max(ctx):
    module, _ = ctx
    diff = "\n".join(f"+def f{i}():" for i in range(module.MAX_SYMBOLS + 5))
    assert len(module.changed_symbols(diff)) == module.MAX_SYMBOLS


def test_caller_snippets_skips_changed_files(ctx):
    module, responses = ctx
    responses[("grep", "-n", "alpha", "--", "src")] = (
        "src/devops_agent/changed.py:10:alpha\nsrc/devops_agent/caller.py:20:alpha = alpha()\n"
    )
    responses[("show", "HEAD:src/devops_agent/caller.py")] = "\n".join(
        f"line {i}" for i in range(1, 40)
    )
    snippets = module.caller_snippets(["alpha"], {"src/devops_agent/changed.py"})
    assert "caller.py:20" in snippets
    assert "changed.py" not in snippets
    assert "line 19" in snippets and "line 20" in snippets


def test_caller_snippets_ignores_grep_with_no_hits(ctx):
    module, _ = ctx  # fake_git 对未登记命令返回空串，模拟 git grep 退出码 1
    assert module.caller_snippets(["missing"], set()) == ""


def test_build_packs_diff_and_callers(ctx):
    module, responses = ctx
    responses[("diff", "abc123", "HEAD", "--", "src", "tests")] = (
        "diff --git a/src/devops_agent/main.py b/src/devops_agent/main.py\n"
        "+++ b/src/devops_agent/main.py\n"
        "+def new_api():\n"
    )
    responses[("grep", "-n", "new_api", "--", "src")] = "src/devops_agent/caller.py:5:new_api()\n"
    responses[("show", "HEAD:src/devops_agent/caller.py")] = "new_api()\n"
    text = module.build("abc123")
    assert "=== DIFF ===" in text
    assert "+def new_api():" in text
    assert "=== CALLER CONTEXT ===" in text
    assert "caller.py:5" in text


def test_build_truncates_long_diff(ctx):
    module, responses = ctx
    responses[("diff", "base", "HEAD", "--", "src", "tests")] = "\n".join(
        f"line {i}" for i in range(module.MAX_DIFF_LINES + 50)
    )
    text = module.build("base")
    assert text.count("line ") == module.MAX_DIFF_LINES
