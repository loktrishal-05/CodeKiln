"""The wrapper is the trust boundary to the real Harbor CLI; assert its exact command and logs."""
import json
import subprocess
from pathlib import Path

import pytest

from codekiln.validation import harbor_runner


def _proc(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _job(out: Path, task: Path, reward: float | None = 0.5) -> None:
    job = out / "2026-01-01__00-00-00" / "task__abc"
    (job / "verifier").mkdir(parents=True, exist_ok=True)
    (job / "result.json").write_text(json.dumps({
        "task_id": {"path": str(task)},
        "verifier_result": {"rewards": {} if reward is None else {"reward": reward}},
    }))


def test_run_matches_judge_command_and_reads_reward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, out = tmp_path / "task", tmp_path / "out"
    task.mkdir()
    (task / "task.toml").write_text("")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        _job(out, task)
        return _proc()

    monkeypatch.setattr(harbor_runner.subprocess, "run", fake_run)
    assert harbor_runner.run(task, out, "nop", 2) == {"task": 0.5}
    cmd, kwargs = calls[0]
    assert cmd[:10] == ["harbor", "run", "-p", str(task), "-a", "nop", "-k", "1", "-n", "2"]
    assert cmd[10:] == ["-o", str(out), "--no-delete", "-y"]
    assert kwargs["text"] is True and kwargs["check"] is False


def test_dataset_run_uses_directory_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dataset, out = tmp_path / "dataset", tmp_path / "out"
    (dataset / "a").mkdir(parents=True)
    (dataset / "b").mkdir()

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        _job(out, dataset / "a", 1.0)
        return _proc()

    monkeypatch.setattr(harbor_runner.subprocess, "run", fake_run)
    assert harbor_runner.run(dataset, out) == {"a": 1.0, "b": None}


def test_crash_logs_stderr_instead_of_swallowing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    task, out = tmp_path / "task", tmp_path / "out"
    task.mkdir()
    (task / "task.toml").write_text("")
    out.mkdir()
    monkeypatch.setattr(harbor_runner.subprocess, "run",
                        lambda cmd, **kw: _proc(1, "docker: daemon is not running"))

    with caplog.at_level("WARNING", logger="codekiln.validation.harbor_runner"):
        assert harbor_runner.run(task, out) == {"task": None}
    assert "docker: daemon is not running" in caplog.text
    assert "failed (1)" in caplog.text


def test_nonzero_exit_with_rewards_does_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    task, out = tmp_path / "task", tmp_path / "out"
    task.mkdir()
    (task / "task.toml").write_text("")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        _job(out, task, 0.0)
        return _proc(1, "one sibling trial failed")

    monkeypatch.setattr(harbor_runner.subprocess, "run", fake_run)
    with caplog.at_level("WARNING", logger="codekiln.validation.harbor_runner"):
        assert harbor_runner.run(task, out) == {"task": 0.0}
    assert caplog.text == ""
