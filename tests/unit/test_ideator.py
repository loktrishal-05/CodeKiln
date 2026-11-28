"""Issue #44: the model proposes, the critic decides. No proposal grades itself."""
import json
import sqlite3
from pathlib import Path

import pytest
from spec_examples import INSTRUCTION, REPO, SHA, make_spec
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.db import connect, idempotency_key
from codekiln.planner.models import Category, Plan, dump_plan
from codekiln.states import State
from codekiln.synthesis.ideator import critique, ideate, propose
from codekiln.synthesis.llm_router import LLMWriter
from codekiln.synthesis.models import FeatureSpec, Requirement, SynthesisError

PLAN = Plan(Category.API_CLI, frozenset({"src/click/utils.py"}), frozenset({"echo", "CliRunner"}), "forward")
NOVEL_INSTRUCTION = """# Reversed message output

## Requirements
- REQ-1: `echo` accepts a `reverse` switch that emits each argument backwards.
- REQ-2: `CliRunner` records the transformed text produced by that switch.

## Errors
- `ValueError` if `reverse` is passed twice in one invocation.
"""
NOVEL_TESTS = {
    "test_echo_reverse.py": (
        "from click import echo\nfrom click.testing import CliRunner\n\n"
        "def test_swap():\n    assert echo\n\n"
        "def test_capture():\n    assert CliRunner\n"
    )
}
N1 = "tests/test_echo_reverse.py::test_swap"
N2 = "tests/test_echo_reverse.py::test_capture"


def novel(task: str = "click-echo-reverse") -> FeatureSpec:
    return make_spec(
        task=task,
        instruction=NOVEL_INSTRUCTION,
        feature_tests=NOVEL_TESTS,
        f2p=(N1, N2),
        requirements=(
            Requirement("REQ-1", "reverse switch emits arguments backwards", (N1,)),
            Requirement("REQ-2", "CliRunner records transformed text", (N2,)),
        ),
    )


class FakeWriter:
    def __init__(self, specs: list[FeatureSpec]) -> None:
        self.specs = specs
        self.calls: list[tuple[Plan, str, str]] = []

    def write(self, plan: Plan, *, repo: str, base_commit: str) -> FeatureSpec:
        self.calls.append((plan, repo, base_commit))
        return self.specs[(len(self.calls) - 1) % len(self.specs)]


def test_propose_calls_the_writer_n_times() -> None:
    writer = FakeWriter([make_spec(), novel()])
    specs = propose(PLAN, writer, n=3, repo=REPO, base_commit=SHA)
    assert len(specs) == 3 and len(writer.calls) == 3
    assert all(plan is PLAN and repo == REPO and commit == SHA for plan, repo, commit in writer.calls)
    with pytest.raises(ValueError, match="at least one"):
        propose(PLAN, writer, n=0, repo=REPO, base_commit=SHA)


def test_critic_keeps_novel_and_drops_copy_leak_and_invalid() -> None:
    baseline = make_spec()
    copy = make_spec(task="click-echo-uppercase-copy")
    leaky = make_spec(task="click-echo-leak", instruction=INSTRUCTION + "\nHidden tests in tests/feature/.")
    invalid = make_spec(task="Bad_Name")
    good = novel()
    report = critique([baseline, copy, leaky, invalid, good], PLAN)
    assert report.proposed == 5 and report.baseline == baseline.task
    assert report.kept == (baseline, good)
    reasons = dict(report.rejected)
    assert "too similar" in reasons["click-echo-uppercase-copy"]
    assert "blind-reader" in reasons["click-echo-leak"]
    assert "validate" in reasons["Bad_Name"]
    assert report.ok
    data = json.loads(report.to_json())
    assert data["kept"][1]["task"] == good.task and len(data["rejected"]) == 3


def test_critic_rejects_a_spec_that_ignores_the_planned_symbol() -> None:
    plan = Plan(Category.CORE_LOGIC, frozenset({"src/click/types.py"}), frozenset({"Choice"}), "forward")
    report = critique([make_spec()], plan)
    assert not report.ok and "no planned symbol" in report.rejected[0][1]


def test_ideate_runs_both_steps() -> None:
    # A renamed copy of the baseline is the single-writer failure mode the critic must catch.
    report = ideate(PLAN, FakeWriter([make_spec(), make_spec(task="click-echo-copy")]),
                    n=2, repo=REPO, base_commit=SHA)
    assert report.proposed == 2 and len(report.kept) == 1
    assert "too similar" in report.rejected[0][1]


def test_cli_ideate_writes_a_report_and_meters_the_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "k.sqlite"
    con = connect(str(db))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES (?, ?)", (REPO, SHA))
    rid = con.execute("SELECT id FROM repositories").fetchone()[0]
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (?, 'dual', 1)", (rid,))
    run_id = con.execute("SELECT id FROM factory_runs").fetchone()[0]
    key = idempotency_key(REPO, SHA, "dual", 0)
    con.execute(
        "INSERT INTO task_candidates (run_id, idempotency_key, category, status)"
        " VALUES (?, ?, 'api_cli', ?)", (run_id, key, State.DIVERSITY_OK.value),
    )
    cid = con.execute("SELECT id FROM task_candidates").fetchone()[0]
    con.execute("INSERT INTO task_plans (candidate_id, plan_json) VALUES (?, ?)", (cid, dump_plan(PLAN)))
    con.commit()
    con.close()

    monkeypatch.setattr(LLMWriter, "write",
                        FakeWriter([make_spec(), make_spec(task="click-echo-copy")]).write)
    monkeypatch.setenv("CODEKILN_DB_PATH", str(db))
    monkeypatch.setenv("CODEKILN_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("CODEKILN_BILLING_LEDGER", str(tmp_path / "billing.csv"))
    out = tmp_path / "reports" / "ideation.json"
    res = CliRunner().invoke(app, ["ideate", str(run_id), "--candidates", "2", "--out", str(out)])
    assert res.exit_code == 0, res.output
    reports = json.loads(out.read_text())
    assert len(reports) == 1 and len(reports[0]["kept"]) == 1
    # The method is faked, so no provider call is metered here; the allocation still is.
    rows = (tmp_path / "billing.csv").read_text()
    assert "BUDGET_ALLOCATION" in rows and "$0.0000 spent" in res.output

    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT status FROM task_candidates").fetchone()[0] == State.DIVERSITY_OK.value
    finally:
        con.close()


class FlakyWriter:
    """Fails (as an exhausted repair does) on the listed 0-based calls, otherwise returns SPECS in turn."""

    def __init__(self, fail_on: set[int], specs: list[FeatureSpec]) -> None:
        self.fail_on, self.specs, self.n = fail_on, specs, 0

    def write(self, plan: Plan, *, repo: str, base_commit: str) -> FeatureSpec:
        i, self.n = self.n, self.n + 1
        if i in self.fail_on:
            raise SynthesisError("no valid spec after 3 attempts: RTM audit failed")
        return self.specs[i % len(self.specs)]


def test_a_failed_proposal_does_not_abort_the_slot() -> None:
    writer = FlakyWriter({1}, [make_spec(), novel()])
    report = ideate(PLAN, writer, n=3, repo=REPO, base_commit=SHA)
    assert report.proposed == 3 and writer.n == 3  # all three attempted
    assert ("proposal-1", "writer: no valid spec after 3 attempts: RTM audit failed") in report.rejected
    assert len(report.kept) >= 1


def test_cli_ideate_keeps_other_slots_when_one_fails_and_tags_candidate_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "k.sqlite"
    con = connect(str(db))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES (?, ?)", (REPO, SHA))
    rid = con.execute("SELECT id FROM repositories").fetchone()[0]
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (?, 'dual', 2)", (rid,))
    run_id = con.execute("SELECT id FROM factory_runs").fetchone()[0]
    for i in range(2):
        con.execute("INSERT INTO task_candidates (run_id, idempotency_key, category, status) VALUES (?, ?, 'api_cli', ?)",
                    (run_id, idempotency_key(REPO, SHA, "dual", i), State.DIVERSITY_OK.value))
    for (cid,) in con.execute("SELECT id FROM task_candidates ORDER BY id").fetchall():
        con.execute("INSERT INTO task_plans (candidate_id, plan_json) VALUES (?, ?)", (cid, dump_plan(PLAN)))
    con.commit()
    ids = [c for (c,) in con.execute("SELECT id FROM task_candidates ORDER BY id")]
    con.close()

    def write(self: LLMWriter, plan: Plan, *, repo: str, base_commit: str) -> FeatureSpec:
        raise SynthesisError("provider is down")  # every proposal fails: slot 1 must still not crash slot 2

    monkeypatch.setattr(LLMWriter, "write", write)
    monkeypatch.setenv("CODEKILN_DB_PATH", str(db))
    monkeypatch.setenv("CODEKILN_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("CODEKILN_BILLING_LEDGER", str(tmp_path / "billing.csv"))
    out = tmp_path / "ideation.json"
    res = CliRunner().invoke(app, ["ideate", str(run_id), "--candidates", "2", "--out", str(out)])
    assert res.exit_code == 0, res.output
    reports = json.loads(out.read_text())
    assert [r["candidate_id"] for r in reports] == ids  # both slots reported, each tagged
    assert all(r["ok"] is False for r in reports)


def test_cli_ideate_gives_the_writer_the_repo_checkout_so_its_edits_can_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real run: every proposal failed with 'edits need the repo checkout; none is available', because
    ideate passed the stored plan to the writer without the checkout the factory gives it."""
    from codekiln.pipeline import workspace_path

    db = tmp_path / "k.sqlite"
    con = connect(str(db))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES (?, ?)", (REPO, SHA))
    rid = con.execute("SELECT id FROM repositories").fetchone()[0]
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (?, 'dual', 1)", (rid,))
    run_id = con.execute("SELECT id FROM factory_runs").fetchone()[0]
    con.execute("INSERT INTO task_candidates (run_id, idempotency_key, category, status) VALUES (?, ?, 'api_cli', ?)",
                (run_id, idempotency_key(REPO, SHA, "dual", 0), State.DIVERSITY_OK.value))
    cid = con.execute("SELECT id FROM task_candidates").fetchone()[0]
    con.execute("INSERT INTO task_plans (candidate_id, plan_json) VALUES (?, ?)", (cid, dump_plan(PLAN)))
    con.commit()
    con.close()
    checkout = workspace_path(REPO, SHA, tmp_path / "ws")
    checkout.mkdir(parents=True)
    seen: list[str] = []

    def write(self: LLMWriter, plan: Plan, *, repo: str, base_commit: str, **_: object) -> FeatureSpec:
        seen.append(plan.workspace)
        return make_spec()

    monkeypatch.setattr(LLMWriter, "write", write)
    monkeypatch.setenv("CODEKILN_DB_PATH", str(db))
    monkeypatch.setenv("CODEKILN_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("CODEKILN_BILLING_LEDGER", str(tmp_path / "billing.csv"))
    res = CliRunner().invoke(app, ["ideate", str(run_id), "--candidates", "1", "--workdir", str(tmp_path / "ws"),
                                   "--out", str(tmp_path / "i.json")])
    assert res.exit_code == 0, res.output
    assert seen == [str(checkout)]
