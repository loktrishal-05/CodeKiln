"""Executable 3-state gate: Harbor trials, validation_executions and candidate state."""
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.db import connect
from codekiln.states import State
from codekiln.validation.verify import VerificationError, find_candidate, verify_package

F2P = {"t::new"}


def make_task(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    (task / "tests").mkdir(parents=True)
    (task / "tests" / "fail_to_pass.json").write_text(json.dumps(sorted(F2P)))
    return task


def fake_run(oracle_new: str = "passed", crash: bool = False):
    def run(task: Path, out: Path, agent: str, concurrency: int = 4) -> None:
        if crash:
            return
        results = {"t::old": "passed", "t::new": "failed"}
        if agent == "oracle":
            results["t::new"] = oracle_new
        v = out / "2026-01-01__00-00-00" / "task__abc" / "verifier"
        v.mkdir(parents=True)
        (v / "results.json").write_text(json.dumps(results))

    return run


def db_with_candidate(tmp_path: Path, task: Path, status: str = "BLIND_READER_OK") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "k.sqlite"
    con = connect(str(db))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES ('u', 'c')")
    rid = con.execute("SELECT id FROM repositories").fetchone()[0]
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (?, 'dual', 1)", (rid,))
    run_id = con.execute("SELECT id FROM factory_runs").fetchone()[0]
    con.execute(
        "INSERT INTO task_candidates (run_id, idempotency_key, status) VALUES (?, 'k', ?)",
        (run_id, status),
    )
    cid = con.execute("SELECT id FROM task_candidates").fetchone()[0]
    con.execute(
        "INSERT INTO task_plans (candidate_id, plan_json, package_path) VALUES (?, '{}', ?)",
        (cid, str(task)),
    )
    con.commit()
    con.close()
    return db


def test_verify_passes_and_records_every_trial(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    db = db_with_candidate(tmp_path, task)
    result = verify_package(task, tmp_path / "work", db=db, run=fake_run())
    assert result.ok and result.state is State.STATE_C_ALL_PASS
    assert result.p2p == ("t::old",) and result.trials == 7
    assert json.loads((tmp_path / "work" / "task" / "flakiness_quarantine.json").read_text()) == {
        "quarantined": [],
    }
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT phase, run_index, exit_code, f2p_passed, p2p_passed, ctrf_path"
            " FROM validation_executions ORDER BY id"
        ).fetchall()
        assert len(rows) == 7
        assert [r[0] for r in rows] == ["A", "B", "B", "B", "C", "C", "C"]
        assert all(r[2] == 0 and r[4] == 1 and r[5].endswith("results.json") for r in rows)
        assert [r[3] for r in rows] == [0, 0, 0, 0, 1, 1, 1]
        status, reason = con.execute(
            "SELECT status, rejection_reason FROM task_candidates WHERE id = 1"
        ).fetchone()
        assert status == State.STATE_C_ALL_PASS.value and reason is None
    finally:
        con.close()
    assert json.loads(result.to_json())["state"] == State.STATE_C_ALL_PASS.value


def test_verify_rejects_with_exact_reason(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    db = db_with_candidate(tmp_path, task)
    result = verify_package(task, tmp_path / "work", db=db, run=fake_run(oracle_new="failed"))
    assert not result.ok and result.state is State.REJECTED
    assert "STATE_C: reference patch fails" in result.reason
    con = sqlite3.connect(db)
    try:
        status, reason = con.execute(
            "SELECT status, rejection_reason FROM task_candidates WHERE id = 1"
        ).fetchone()
        assert status == State.REJECTED.value and reason == result.reason
    finally:
        con.close()


def test_verify_never_regresses_a_packed_candidate(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    db = db_with_candidate(tmp_path, task, State.PACKAGED.value)
    result = verify_package(task, tmp_path / "work", db=db, run=fake_run())
    assert result.ok  # trials still ran and were recorded
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT status FROM task_candidates WHERE id = 1").fetchone()[0] == (
            State.PACKAGED.value
        )
        assert con.execute("SELECT COUNT(*) FROM validation_executions").fetchone()[0] == 7
    finally:
        con.close()


def test_crashed_trials_are_recorded_and_reject(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    result = verify_package(task, tmp_path / "work", run=fake_run(crash=True))
    assert result.state is State.REJECTED and "STATE_A" in result.reason
    assert result.trials == 1  # the gate stops at A
    assert json.loads((tmp_path / "work" / "task" / "flakiness_quarantine.json").read_text()) == {
        "quarantined": [],
    }


def test_verify_requires_f2p_tests(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    (task / "tests" / "fail_to_pass.json").write_text("[]")
    with pytest.raises(VerificationError, match="no fail_to_pass"):
        verify_package(task, tmp_path / "work", run=fake_run())
    (task / "tests" / "fail_to_pass.json").unlink()
    with pytest.raises(VerificationError, match="no tests/fail_to_pass.json"):
        verify_package(task, tmp_path / "work", run=fake_run())


def test_find_candidate_matches_path_or_name(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    db = db_with_candidate(tmp_path, task)
    con = sqlite3.connect(db)
    try:
        assert find_candidate(con, task) == 1
        assert find_candidate(con, tmp_path / "other" / "task") == 1
        assert find_candidate(con, tmp_path / "unknown") is None
    finally:
        con.close()


def test_cli_verify_records_verdict_and_exits_by_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = make_task(tmp_path)
    db = db_with_candidate(tmp_path, task)
    work = tmp_path / "work"
    monkeypatch.setattr("codekiln.validation.harbor_runner.run", fake_run())

    ok = CliRunner().invoke(app, ["verify", str(task), "--work", str(work), "--db", str(db),
                                  "--concurrency", "2"])
    assert ok.exit_code == 0, ok.output
    assert "STATE_C_ALL_PASS" in ok.output

    db2 = db_with_candidate(tmp_path / "bad", task)
    monkeypatch.setattr("codekiln.validation.harbor_runner.run", fake_run(oracle_new="failed"))
    bad = CliRunner().invoke(app, ["verify", str(task), "--work", str(work), "--db", str(db2)])
    assert bad.exit_code == 1, bad.output
    assert "STATE_C: reference patch fails" in bad.output


def test_passing_gate_writes_the_verified_p2p_into_an_unsealed_package(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    (task / "tests" / "pass_to_pass.json").write_text("[]")  # what the emitter ships
    assert verify_package(task, tmp_path / "work", run=fake_run()).ok
    assert json.loads((task / "tests" / "pass_to_pass.json").read_text()) == ["t::old"]


def test_p2p_is_left_alone_when_sealed_or_when_the_gate_fails(tmp_path: Path) -> None:
    sealed = make_task(tmp_path / "a")
    (sealed / "tests" / "pass_to_pass.json").write_text("[]")
    (sealed / "manifest.sha256").write_text("sealed")
    assert verify_package(sealed, tmp_path / "work", run=fake_run()).ok
    assert (sealed / "tests" / "pass_to_pass.json").read_text() == "[]"
    failing = make_task(tmp_path / "b")
    (failing / "tests" / "pass_to_pass.json").write_text("[]")
    assert not verify_package(failing, tmp_path / "work", run=fake_run("failed")).ok
    assert (failing / "tests" / "pass_to_pass.json").read_text() == "[]"


def test_quarantine_file_is_kept_per_task(tmp_path: Path) -> None:
    one = make_task(tmp_path / "one")
    two = make_task(tmp_path / "two").rename(tmp_path / "two" / "other-task")
    verify_package(one, tmp_path / "work", run=fake_run())
    verify_package(two, tmp_path / "work", run=fake_run())
    assert (tmp_path / "work" / "task" / "flakiness_quarantine.json").is_file()
    assert (tmp_path / "work" / "other-task" / "flakiness_quarantine.json").is_file()
