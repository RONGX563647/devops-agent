"""`scripts/check_manifest_sync.py` 的单测。

这条闸门的存在理由：`sha256` 是 `compare` 判断「同一套题」的依据。改了 JSONL
却忘了跑 `eval manifest` 时，新旧批次的 delta 会无声失效 —— 而 `validate` 只在
**读题库时**报 sha 不匹配，容易在别处被忽略。

要求：
- 清单与题库一致 -> 退出 0；
- 任一字段漂移 -> 退出 1，且报出字段名与预期/实际值；
- 比对按 JSON 语义而非字节（行尾/缩进差异不算过期）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_manifest_sync.py"
SUITE = "eval/suites/devops-core.v2.jsonl"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """以 UTF-8 调脚本；保留父进程环境（子进程要能 import devops_agent）。"""
    import os

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
    )


def test_current_suite_is_in_sync() -> None:
    proc = _run()
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


class TestDrift:
    """用临时题库副本验证漂移检测，不动仓库里的真文件。"""

    @pytest.fixture()
    def suite_copy(self, tmp_path: Path) -> Path:
        """把 v2 题库 + 清单复制到临时目录，返回题库路径（相对 ROOT）。"""
        target = tmp_path / "suites"
        target.mkdir()
        src = ROOT / "eval" / "suites"
        for suffix in (".jsonl", ".manifest.json"):
            name = f"devops-core.v2{suffix}"
            (target / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
        return target / "devops-core.v2.jsonl"

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    def test_sha_drift_fails(self, suite_copy: Path) -> None:
        manifest = suite_copy.with_name("devops-core.v2.manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["sha256"] = "0" * 64
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        proc = _run("--suite", self._relative(suite_copy))
        assert proc.returncode == 1
        assert "sha256" in proc.stderr

    def test_counts_drift_fails(self, suite_copy: Path) -> None:
        manifest = suite_copy.with_name("devops-core.v2.manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["counts"] = {"common": 99}
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        proc = _run("--suite", self._relative(suite_copy))
        assert proc.returncode == 1
        assert "counts" in proc.stderr

    def test_threshold_drift_fails(self, suite_copy: Path) -> None:
        """调低阈值 = 悄悄关掉门禁，必须拦住。"""
        manifest = suite_copy.with_name("devops-core.v2.manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["thresholds"]["pass_rate"]["min"] = 0.5
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        proc = _run("--suite", self._relative(suite_copy))
        assert proc.returncode == 1
        assert "低于项目下界" in proc.stderr

    def test_threshold_out_of_range_fails(self, suite_copy: Path) -> None:
        """越量纲（把 0.8 写成 80）要报。"""
        manifest = suite_copy.with_name("devops-core.v2.manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["thresholds"]["pass_rate"]["min"] = 80
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        proc = _run("--suite", self._relative(suite_copy))
        assert proc.returncode == 1
        assert "超出量纲" in proc.stderr

    def test_min_samples_zeroed_fails(self, suite_copy: Path) -> None:
        """把 judge 的 min_samples 归零 -> 3 条样本就能给结论，必须拦住。"""
        manifest = suite_copy.with_name("devops-core.v2.manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["thresholds"]["judge_correctness"]["min_samples"] = 0
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        proc = _run("--suite", self._relative(suite_copy))
        assert proc.returncode == 1
        assert "min_samples" in proc.stderr

    def test_unknown_gate_key_fails(self, suite_copy: Path) -> None:
        manifest = suite_copy.with_name("devops-core.v2.manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["thresholds"]["bogus_gate"] = {"min": 1, "min_samples": 0}
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        proc = _run("--suite", self._relative(suite_copy))
        assert proc.returncode == 1
        assert "未知门禁" in proc.stderr

    def test_formatting_only_difference_passes(self, suite_copy: Path) -> None:
        """只改缩进/换行不算过期 —— 闸门比较 JSON 语义，不是字节。"""
        manifest = suite_copy.with_name("devops-core.v2.manifest.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(
            json.dumps(data, ensure_ascii=False, indent=8).replace("\n", "\r\n"),
            encoding="utf-8",
        )

        proc = _run("--suite", self._relative(suite_copy))
        assert proc.returncode == 0, proc.stderr


def test_failure_message_tells_how_to_fix() -> None:
    """报错必须带上修复命令，否则读到的人还得自己找。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "python -m devops_agent.eval manifest" in text
