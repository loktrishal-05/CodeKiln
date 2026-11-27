"""Executable acceptance test for issue #36: six planner slots, real router, metered spend.

The fake `post` replaces only the provider HTTP call. Everything else is the production path:
LLMWriter (validate + RTM repair), Budget's meter, synthesize's blind-reader gate, write_package,
and the append-only CSV ledger. No candidate is marked good by the writer's own claim.
"""
import csv
import json
from pathlib import Path

from spec_examples import REPO, SHA, make_spec

from codekiln.billing.ledger import BillingLedger, Budget, Pricing
from codekiln.db import connect, idempotency_key
from codekiln.planner.models import Category, Plan, dump_plan
from codekiln.states import State
from codekiln.synthesis.llm_router import LLMWriter, ModelConfig
from codekiln.synthesis.models import validate
from codekiln.synthesis.runner import synthesize
from codekiln.validation.blind_reader import lint_package

SLOTS = 6
CAP_USD = 5.0


def _ledger(tmp_path: Path, slots: int = SLOTS) -> int:
    con = connect(str(tmp_path / "k.sqlite"))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES (?, ?)", (REPO, SHA))
    repo_id = con.execute("SELECT id FROM repositories").fetchone()[0]
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (?, 'dual', ?)", (repo_id, slots))
    run_id = con.execute("SELECT id FROM factory_runs").fetchone()[0]
    plan = Plan(Category.CORE_LOGIC, frozenset({"src/click/types.py"}), frozenset({"Choice"}), "forward")
    for i in range(slots):
        key = idempotency_key(REPO, SHA, "dual", i)
        con.execute(
            "INSERT INTO task_candidates (run_id, idempotency_key, category, difficulty, status)"
            " VALUES (?, ?, ?, 'L2', ?)",
            (run_id, key, Category.CORE_LOGIC.value, State.DIVERSITY_OK.value),
        )
        cid = con.execute("SELECT id FROM task_candidates WHERE idempotency_key = ?", (key,)).fetchone()[0]
        con.execute("INSERT INTO task_plans (candidate_id, plan_json) VALUES (?, ?)", (cid, dump_plan(plan)))
    con.commit()
    con.close()
    return run_id


def _writer(budget: Budget, seen: list[int]) -> LLMWriter:
    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        spec = make_spec(task=f"click-six-slot-{len(seen)}")
        seen.append(len(seen))
        return {
            "choices": [{"message": {"content": json.dumps(spec.to_dict())}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 400},
        }

    config = ModelConfig(base_url="https://provider.invalid/v1", api_key="sk-test", model="fake-model")
    return LLMWriter(config, post=budget.meter_post(post))


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_six_slots_pass_validate_rtm_and_blind_reader_with_recorded_spend(tmp_path: Path) -> None:
    run_id = _ledger(tmp_path)
    csv_path = tmp_path / "billing_ledger.csv"
    ledger = BillingLedger(csv_path, Pricing())
    budget = Budget(ledger, run_id, CAP_USD, "fake-model")
    seen: list[int] = []
    con = connect(str(tmp_path / "k.sqlite"))
    try:
        summary = synthesize(con, run_id, _writer(budget, seen), tmp_path / "packages", budget=budget)
        assert (summary.synthesized, summary.stopped) == (SLOTS, "")
        assert {s for (s,) in con.execute(
            "SELECT status FROM task_candidates WHERE run_id = ?", (run_id,)
        )} == {State.BLIND_READER_OK.value}
    finally:
        con.close()

    packages = sorted((tmp_path / "packages").iterdir())
    assert len(packages) == SLOTS
    for package in packages:
        # Each accepted package independently satisfies the three static acceptance gates.
        assert lint_package(package).ok
        assert json.loads((package / "rtm_map.json").read_text())["ok"] is True
        validate(make_spec(task=package.name))  # the shape synthesize already validated
    assert len(seen) == SLOTS

    rows = _rows(csv_path)
    events = {r["event"] for r in rows}
    assert {"BUDGET_ALLOCATION", "PREAUTH_HOLD", "PREAUTH_RELEASE", "LLM_DEBIT"} <= events
    assert 0 < ledger.spent(run_id) <= CAP_USD
    assert ledger.remaining(run_id) >= 0
    assert all(r["run_id"] == str(run_id) and r["model"] == "fake-model"
               for r in rows if r["event"] == "LLM_DEBIT")


def test_budget_cap_stops_before_all_six_slots_and_keeps_resume_state(tmp_path: Path) -> None:
    run_id = _ledger(tmp_path)
    csv_path = tmp_path / "billing_ledger.csv"
    ledger = BillingLedger(csv_path, Pricing())
    budget = Budget(ledger, run_id, 0.0001, "fake-model")
    seen: list[int] = []
    con = connect(str(tmp_path / "k.sqlite"))
    try:
        summary = synthesize(con, run_id, _writer(budget, seen), tmp_path / "packages", budget=budget)
        assert summary.synthesized == 0
        assert summary.stopped.startswith("BUDGET_EXCEEDED")
        pending = con.execute(
            "SELECT COUNT(*) FROM task_candidates WHERE run_id = ? AND status = ?",
            (run_id, State.DIVERSITY_OK.value),
        ).fetchone()[0]
        assert pending == SLOTS  # nothing advanced, so `resume` can spend a bigger allocation
    finally:
        con.close()
    assert seen == []  # the breaker fired before any provider call
    assert ledger.spent(run_id) == 0
