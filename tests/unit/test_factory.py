"""#35: the factory drives every candidate through every gate, resumes, and never trusts a model."""
import json
import sqlite3
from pathlib import Path

from spec_examples import REQ1, REQ2
from test_six_slot_synthesis import _ledger, _writer

from codekiln.billing.ledger import BillingLedger, Budget, Pricing
from codekiln.db import connect
from codekiln.factory import FactorySummary, run_factory
from codekiln.packaging.packager import verify_manifest

P2P_NODE = "tests/test_core.py::test_old"


def verify_run(oracle: str = "passed"):
    """Fake `harbor run`: nop leaves F2P failing, oracle passes everything (or `oracle`)."""
    def run(task: Path, out: Path, agent: str) -> None:
        results = {P2P_NODE: "passed", REQ1: "failed", REQ2: "failed"}
        if agent == "oracle":
            results[REQ1] = results[REQ2] = oracle
        v = out / "2026-01-01__00-00-00" / "task__abc" / "verifier"
        v.mkdir(parents=True)
        (v / "results.json").write_text(json.dumps(results))
    return run


def drive(tmp_path: Path, *, slots: int = 3, probe: float = 0.0, oracle: str = "passed",
          max_steps: int = 200) -> tuple[FactorySummary, int, Path]:
    run_id = _ledger(tmp_path, slots)
    db = tmp_path / "k.sqlite"
    budget = Budget(BillingLedger(tmp_path / "billing.csv", Pricing()), run_id, 5.0, "fake-model")
    con = connect(str(db))
    try:
        out = run_factory(
            con, run_id, writer=_writer(budget, []), staging=tmp_path / "staging",
            dest=tmp_path / "packages", work=tmp_path / "work", db=db, budget=budget,
            verify_run=verify_run(oracle), probe_run=lambda task, o: probe, max_steps=max_steps,
            model="fake-model",
        )
    finally:
        con.close()
    return out, run_id, db


def statuses(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {s for (s,) in con.execute("SELECT status FROM task_candidates")}
    finally:
        con.close()


def test_every_candidate_is_gated_and_packaged_with_evidence(tmp_path: Path) -> None:
    out, _, db = drive(tmp_path)
    assert len(out.packaged) == 3 and not out.rejected and out.unfinished == 0
    assert statuses(db) == {"PACKAGED"}
    for name in out.packaged:
        package = tmp_path / "packages" / name
        assert verify_manifest(package)
        # the gate's verified P2P set is in the package, so the reward formula sees regressions
        assert json.loads((package / "tests" / "pass_to_pass.json").read_text()) == [P2P_NODE]
        receipt = json.loads((package / "generation_receipt.json").read_text())
        assert receipt["gates"]["state_c_all_pass"] is True
        assert receipt["gates"]["mutation_semantic_kill_rate"] == 1.0  # measured by the probes
        assert receipt["cost_usd"] > 0 and receipt["spec_sha256"]
        assert (package / "mutation_dossier.json").is_file()  # the evidence travels with the package


def test_rerunning_is_a_no_op_resume(tmp_path: Path) -> None:
    out, run_id, db = drive(tmp_path)
    con = connect(str(db))
    try:
        again = run_factory(
            con, run_id, writer=_writer(Budget(BillingLedger(tmp_path / "billing.csv", Pricing()),
                                               run_id, 5.0, "fake-model"), []),
            staging=tmp_path / "staging", dest=tmp_path / "packages", work=tmp_path / "work", db=db,
            verify_run=verify_run(), probe_run=lambda t, o: 0.0,
        )
    finally:
        con.close()
    assert again.steps == 1  # only the (all-skipped) synthesis pass; no stage re-executed
    assert sorted(again.packaged) == sorted(out.packaged)


def test_a_surviving_mutant_rejects_the_candidate_and_nothing_is_promoted(tmp_path: Path) -> None:
    out, _, db = drive(tmp_path, probe=1.0)  # every mutant passes the suite: it kills nothing
    assert not out.packaged and len(out.rejected) == 3
    assert all(r.startswith("mutation:") for r in out.rejected.values())
    assert statuses(db) == {"REJECTED"} and not (tmp_path / "packages").exists()


def test_a_failing_reference_patch_is_rejected_by_the_gate(tmp_path: Path) -> None:
    out, _, db = drive(tmp_path, oracle="failed")
    assert not out.packaged and statuses(db) == {"REJECTED"}
    assert all("STATE_C" in r for r in out.rejected.values())


def test_the_step_limit_bounds_work_and_a_second_call_finishes(tmp_path: Path) -> None:
    out, run_id, db = drive(tmp_path, max_steps=3)
    assert out.steps <= 3 + 1 and out.unfinished > 0 and len(out.packaged) < 3
    con = connect(str(db))
    try:
        rest = run_factory(
            con, run_id, writer=_writer(Budget(BillingLedger(tmp_path / "billing.csv", Pricing()),
                                               run_id, 5.0, "fake-model"), []),
            staging=tmp_path / "staging", dest=tmp_path / "packages", work=tmp_path / "work", db=db,
            verify_run=verify_run(), probe_run=lambda t, o: 0.0,
        )
    finally:
        con.close()
    assert len(rest.packaged) == 3 and rest.unfinished == 0


def test_cli_needs_exactly_one_of_repo_or_run_id(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from codekiln.cli import app

    monkeypatch.setenv("CODEKILN_DB_PATH", str(tmp_path / "k.sqlite"))
    for args in ([], ["https://example.invalid/r.git", "--run-id", "1"]):
        res = CliRunner().invoke(app, ["factory", *args])
        assert res.exit_code == 2 and "exactly one of" in res.output
