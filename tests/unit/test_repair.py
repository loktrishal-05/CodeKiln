"""Execution-guided repair: a gate rejection gets one corrected attempt, shown the real evidence."""
import json
from pathlib import Path

from spec_examples import REQ1, REQ2, make_spec
from test_factory import P2P_NODE
from test_six_slot_synthesis import _ledger

from codekiln.db import connect
from codekiln.factory import run_factory
from codekiln.repair import MAX_EVIDENCE_CHARS, MAX_REPAIRS, evidence
from codekiln.synthesis.models import FeatureSpec

PYTEST_OUT = (
    "collected 3 items\n=================================== FAILURES ===================================\n"
    "______________________________ test_light_imports ______________________________\n"
    "E   AssertionError: assert 'json' in {'__future__', 'abc'}\n"
    "==================================== PASSES ====================================\nPASSED x\n"
)


class Rewriter:
    """Fake writer: the first spec is bad (the oracle will fail it); a repair returns a good one."""

    def __init__(self, repair_works: bool) -> None:
        self.repair_works = repair_works
        self.calls: list[tuple[FeatureSpec | None, str]] = []

    def write(self, plan, *, repo, base_commit, previous=None, feedback=""):  # type: ignore[no-untyped-def]
        self.calls.append((previous, feedback))
        return make_spec(task="click-repair-me")


def drive(tmp_path: Path, writer: Rewriter):  # type: ignore[no-untyped-def]
    run_id = _ledger(tmp_path, 1)
    db = tmp_path / "k.sqlite"

    def verify_run(task: Path, out: Path, agent: str) -> None:
        good = len(writer.calls) > 1 and writer.repair_works  # only the repaired package passes
        results = {P2P_NODE: "passed", REQ1: "failed", REQ2: "failed"}
        if agent == "oracle":
            results[REQ1] = results[REQ2] = "passed" if good or len(writer.calls) == 0 else "failed"
            if len(writer.calls) == 1:  # first attempt: the oracle fails
                results[REQ1] = results[REQ2] = "failed"
        v = out / "2026-01-01__00-00-00" / "task__abc" / "verifier"
        v.mkdir(parents=True)
        (v / "results.json").write_text(json.dumps(results))
        (v / "pytest.txt").write_text(PYTEST_OUT)

    con = connect(str(db))
    try:
        summary = run_factory(
            con, run_id, writer=writer, staging=tmp_path / "staging", dest=tmp_path / "packages",
            work=tmp_path / "work", db=db, verify_run=verify_run, probe_run=lambda t, o: 0.0,
            model="fake-model",
        )
        repairs = con.execute("SELECT repairs FROM task_plans").fetchone()[0]
    finally:
        con.close()
    return summary, repairs


def test_a_rejected_candidate_is_repaired_with_the_real_pytest_failure_then_packaged(tmp_path: Path) -> None:
    writer = Rewriter(repair_works=True)
    summary, repairs = drive(tmp_path, writer)
    assert len(writer.calls) == 2 and repairs == 1
    previous, feedback = writer.calls[1]
    assert previous is not None and previous.task == "click-repair-me"
    assert "STATE_C" in feedback and "assert 'json' in" in feedback  # the real evidence, not a self-report
    assert "PASSED x" not in feedback  # only the failure section
    assert summary.packaged == ["click-repair-me"] and not summary.rejected


def test_repairs_are_bounded_then_the_rejection_is_final(tmp_path: Path) -> None:
    writer = Rewriter(repair_works=False)
    summary, repairs = drive(tmp_path, writer)
    assert len(writer.calls) == 1 + MAX_REPAIRS and repairs == MAX_REPAIRS  # the first write, then the bounded repairs
    assert not summary.packaged and "STATE_C" in next(iter(summary.rejected.values()))


def test_evidence_lists_the_mutants_the_tests_failed_to_kill(tmp_path: Path) -> None:
    run_id = _ledger(tmp_path, 1)
    con = connect(str(tmp_path / "k.sqlite"))
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    probes = [{"name": "noop", "kind": "nop", "killed": True},
              {"name": "semantic-flip-and", "kind": "semantic", "killed": False}]
    (pkg / "mutation_dossier.json").write_text(json.dumps({"probes": probes}))
    text = evidence(con, run_id, pkg, "mutation: semantic kill rate 33% below 80%")
    assert "semantic-flip-and" in text and "noop" not in text and "exact values" in text


def test_evidence_is_capped_so_the_request_stays_under_a_token_limit(tmp_path: Path) -> None:
    _ledger(tmp_path, 1)
    con = connect(str(tmp_path / "k.sqlite"))
    log = tmp_path / "verifier"
    log.mkdir()
    (log / "results.json").write_text("{}")
    (log / "pytest.txt").write_text("=== FAILURES ===\n" + "E   boom\n" * 5000 + "=== PASSES ===\n")
    con.execute("INSERT INTO validation_executions (candidate_id, phase, run_index, exit_code, f2p_passed,"
                " p2p_passed, ctrf_path) VALUES (1, 'C', 0, 0, 0, 0, ?)", (str(log / "results.json"),))
    text = evidence(con, 1, tmp_path, "STATE_C: reference patch fails: ['x']")
    assert len(text) < MAX_EVIDENCE_CHARS + 400


def test_an_import_error_with_no_failures_section_is_still_shown_to_the_writer(tmp_path: Path) -> None:
    """Real run: the patch broke `import click`, pytest ran no test, so there was no FAILURES section and
    the writer was told nothing. The log's end must be the evidence then."""
    _ledger(tmp_path, 1)
    con = connect(str(tmp_path / "k.sqlite"))
    log = tmp_path / "verifier"
    log.mkdir()
    (log / "results.json").write_text("{}")
    (log / "pytest.txt").write_text("ImportError while loading conftest\nE   ImportError: cannot import name 'x'\n")
    con.execute("INSERT INTO validation_executions (candidate_id, phase, run_index, exit_code, f2p_passed,"
                " p2p_passed, ctrf_path) VALUES (1, 'C', 0, 0, 0, 0, ?)", (str(log / "results.json"),))
    assert "cannot import name 'x'" in evidence(con, 1, tmp_path, "STATE_C: reference patch fails: ['t']")


def test_too_few_mutants_tells_the_writer_to_add_real_decisions(tmp_path: Path) -> None:
    _ledger(tmp_path, 1)
    con = connect(str(tmp_path / "k.sqlite"))
    text = evidence(con, 1, tmp_path, "mutation: only 2 semantic mutants could be built (need >= 3)")
    assert "at least 3 real decisions" in text
