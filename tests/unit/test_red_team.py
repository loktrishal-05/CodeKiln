import json
import sys
from pathlib import Path

import pytest
from spec_examples import DIFF, FEATURE_TESTS, INSTRUCTION, REPO, REQ1, REQ2, SHA
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.packaging.harbor import write_task
from codekiln.validation.red_team import (
    Attack,
    RedTeamError,
    build_cheats,
    clean_script,
    propose_cheats,
    red_team_package,
    write_red_team_report,
)


def _staged(root: Path, name: str = "click-echo-uppercase") -> Path:
    return write_task(
        root / name,
        instruction=INSTRUCTION,
        reference_diff=DIFF,
        feature_tests=FEATURE_TESTS,
        f2p=[REQ1, REQ2],
        p2p=[],
        repo_url=REPO,
        base_commit=SHA,
        category="api_cli",
        difficulty="L2",
    )


def test_build_cheats_covers_four_vectors_without_leaking_solution(tmp_path: Path) -> None:
    task = _staged(tmp_path)
    cheats = build_cheats(task)
    assert [c.kind for c in cheats] == [
        "edit-tests",
        "read-hidden",
        "hardcode",
        "tamper-verifier",
    ]
    for c in cheats:
        assert c.script.startswith("#!/bin/bash")
        assert DIFF not in c.script


def test_red_team_passes_when_no_cheat_scores(tmp_path: Path) -> None:
    task = _staged(tmp_path)
    seen: list[str] = []

    def fake_run(copy_dir: Path, _out: Path) -> float | None:
        seen.append((copy_dir / "solution" / "solve.sh").read_text(encoding="utf-8"))
        if sys.platform != "win32":
            assert (copy_dir / "solution" / "solve.sh").stat().st_mode & 0o111
        return 0.0

    report = red_team_package(task, tmp_path / "work", run=fake_run)
    assert report.ok and not report.blocked
    assert len(report.attacks) == 4
    assert len(seen) == 4
    # Source package solve.sh must remain untouched.
    assert "git apply" in (task / "solution" / "solve.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("bad_reward", [0.5, 1.0])
def test_red_team_blocks_package_if_any_cheat_scores(tmp_path: Path, bad_reward: float) -> None:
    task = _staged(tmp_path)

    def leaky_run(copy_dir: Path, _out: Path) -> float | None:
        return bad_reward if copy_dir.name == "edit-tests" else 0.0

    report = red_team_package(task, tmp_path / "work", run=leaky_run)
    assert not report.ok and report.blocked
    assert any("edit-tests" in r for r in report.reasons)
    parsed = json.loads(report.to_json())
    assert parsed["blocked"] is True


def test_red_team_handles_crash_reward_none_without_false_score(tmp_path: Path) -> None:
    task = _staged(tmp_path)
    report = red_team_package(task, tmp_path / "work", run=lambda _d, _o: None)
    assert report.ok and all(not a.scored and a.reward is None for a in report.attacks)


def test_clean_script_and_propose_cheats(tmp_path: Path) -> None:
    assert clean_script("```bash\necho hi\n```") == "#!/bin/bash\necho hi\n"
    with pytest.raises(RedTeamError, match="empty"):
        clean_script("   ")
    with pytest.raises(RedTeamError, match="secret"):
        clean_script("echo sk-1234567890abcdef")

    class FakeWriter:
        def __init__(self) -> None:
            self._replies = iter(["#!/bin/bash\necho 1", "#!/bin/bash\necho 1", "   "])

        def write(self, _instruction: str) -> str:
            return next(self._replies)

    attacks, rejected = propose_cheats(INSTRUCTION, FakeWriter(), n=3)
    assert len(attacks) == 1 and attacks[0].kind == "agent-cheat"
    assert len(rejected) == 2

    task = _staged(tmp_path)
    report = red_team_package(
        task, tmp_path / "work", run=lambda d, _o: 1.0 if d.name == "agent-cheat-0" else 0.0,
        extra_attacks=attacks,
    )
    assert report.blocked and any("agent-cheat-0" in r for r in report.reasons)


def test_write_report_and_cli_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _staged(tmp_path)
    out = tmp_path / "red_team_report.json"
    runner = CliRunner()

    monkeypatch.setattr("codekiln.validation.red_team.harbor_cheat_run", lambda _d, _o: 0.0)
    ok_res = runner.invoke(
        app, ["red-team", str(task), "--work", str(tmp_path / "w1"), "--out", str(out)]
    )
    assert ok_res.exit_code == 0, ok_res.output
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True

    monkeypatch.setattr(
        "codekiln.validation.red_team.harbor_cheat_run",
        lambda d, _o: 1.0 if d.name == "hardcode" else 0.0,
    )
    bad_res = runner.invoke(
        app, ["red-team", str(task), "--work", str(tmp_path / "w2"), "--out", str(out)]
    )
    assert bad_res.exit_code == 1
    assert "BLOCKED" in bad_res.output
    assert json.loads(out.read_text(encoding="utf-8"))["blocked"] is True

    written = write_red_team_report(
        tmp_path / "sub" / "r.json",
        red_team_package(task, tmp_path / "w3", run=lambda _d, _o: 0.0),
    )
    assert written.is_file()


def test_red_team_rejects_invalid_task_dir(tmp_path: Path) -> None:
    with pytest.raises(RedTeamError, match="Harbor task package"):
        red_team_package(tmp_path, tmp_path / "w")
    bad = _staged(tmp_path)
    (bad / "instruction.md").unlink()
    with pytest.raises(RedTeamError, match="instruction.md"):
        red_team_package(bad, tmp_path / "w")
    assert isinstance(Attack("x", "hardcode", "#!/bin/bash\n"), Attack)
