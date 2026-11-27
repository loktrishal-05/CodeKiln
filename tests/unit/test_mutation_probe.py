import json
from pathlib import Path

import pytest
from spec_examples import DIFF
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.validation.mutation_probe import (
    ProbeError,
    build_mutants,
    dossier_from_mapping,
    probe_package,
    read_dossier,
)


def make_task(root: Path) -> Path:
    task = root / "task"
    (task / "solution").mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text('[task]\nname = "task"\n')
    (task / "solution" / "reference.diff").write_text(DIFF)
    return task


def all_killed(task: Path, out: Path) -> float:
    return 0.0


def test_build_mutants_has_all_four_classes() -> None:
    mutants = build_mutants(DIFF)
    assert {m.kind for m in mutants} == {"nop", "stub", "partial", "semantic"}
    assert sum(m.kind == "semantic" for m in mutants) >= 3
    assert all(m.diff.startswith("diff --git") for m in mutants)
    assert "return None" in next(m for m in mutants if m.kind == "stub").diff
    assert any("echo_mutant" in m.diff for m in mutants if m.kind == "semantic")


def test_probe_kills_everything(tmp_path: Path) -> None:
    d = probe_package(make_task(tmp_path), tmp_path / "work", all_killed)
    assert d.ok and d.semantic_kill_rate == 1.0
    assert all(p.killed for p in d.probes)
    assert not d.reasons
    payload = json.loads(d.to_json())
    assert payload["ok"] is True
    assert dossier_from_mapping(payload).probes == d.probes


def test_semantic_kill_rate_threshold(tmp_path: Path) -> None:
    def one_survivor(task: Path, out: Path) -> float:
        return 1.0 if "rename-def" in str(task) else 0.0

    d = probe_package(make_task(tmp_path), tmp_path / "work", one_survivor)
    assert not d.ok and "semantic kill rate 67%" in " ".join(d.reasons)
    assert probe_package(make_task(tmp_path), tmp_path / "work", one_survivor, 0.6).ok


def test_surviving_nop_is_reported(tmp_path: Path) -> None:
    def survivor(task: Path, out: Path) -> float:
        diff = (task / "solution" / "reference.diff").read_text()
        return 1.0 if "codekiln_noop" in diff else 0.0

    d = probe_package(make_task(tmp_path), tmp_path / "work", survivor)
    assert not d.ok and any("nop" in r for r in d.reasons)


def test_crashed_trial_is_reported(tmp_path: Path) -> None:
    def crash(task: Path, out: Path) -> None:
        return None

    d = probe_package(make_task(tmp_path), tmp_path / "work", crash)  # type: ignore[arg-type]
    assert not d.ok and any("trials crashed" in r for r in d.reasons)


def test_dossier_roundtrip_and_validation(tmp_path: Path) -> None:
    good = probe_package(make_task(tmp_path), tmp_path / "work", all_killed)
    p = tmp_path / "d.json"
    p.write_text(good.to_json())
    assert read_dossier(p) == good
    with pytest.raises(ProbeError):
        dossier_from_mapping({"probes": [{"kind": "magic"}]})


def test_cli_probe_writes_dossier_and_fails_on_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = make_task(tmp_path)
    monkeypatch.setattr(
        "codekiln.validation.harbor_runner.run",
        lambda path, out, agent="oracle", concurrency=4: {path.name: 0.0},
    )
    res = CliRunner().invoke(app, ["probe", str(task), "--work", str(tmp_path / "work")])
    assert res.exit_code == 0, res.output
    assert read_dossier(task / "mutation_dossier.json").ok

    monkeypatch.setattr(
        "codekiln.validation.harbor_runner.run",
        lambda path, out, agent="oracle", concurrency=4: {path.name: 1.0},
    )
    bad = CliRunner().invoke(app, ["probe", str(task), "--work", str(tmp_path / "work")])
    assert bad.exit_code == 1 and "semantic kill rate" in bad.output
    assert not read_dossier(task / "mutation_dossier.json").ok


def test_probe_rejects_non_package(tmp_path: Path) -> None:
    with pytest.raises(ProbeError, match="not a Harbor task"):
        probe_package(tmp_path / "empty", tmp_path / "work", all_killed)
