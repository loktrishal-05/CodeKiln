import json
import sqlite3
from pathlib import Path

import pytest
from test_synth_pipeline import invoke, run_and_id

from codekiln.billing.ledger import Budget, BudgetExceeded


def fake_post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
    """A provider-shaped reply carrying a valid spec and usage counters."""
    from spec_examples import make_spec
    return {
        "choices": [{"message": {"content": json.dumps(make_spec().to_dict())}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def statuses(db: Path) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT status FROM task_candidates ORDER BY id")]
    finally:
        con.close()


def events(csv: Path) -> list[str]:
    import csv as csvlib
    with csv.open(newline="", encoding="utf-8") as f:
        return [r["event"] for r in csvlib.DictReader(f)]


def test_budget_stop_then_idempotent_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = run_and_id(monkeypatch, tmp_path)
    monkeypatch.setattr("codekiln.synthesis.llm_router.http_post", fake_post)
    ledger = tmp_path / "billing.csv"
    packages = tmp_path / "packages"

    # Trip the breaker after the first candidate: its package is kept, the rest wait for resume.
    with monkeypatch.context() as m:
        calls = 0
        def guard(self: Budget) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise BudgetExceeded("test cap")
        m.setattr(Budget, "guard", guard)
        res = invoke(
            monkeypatch, tmp_path, "synth", str(run_id), "--packages", str(packages),
            "--ledger-csv", str(ledger),
        )
    assert res.exit_code == 1, res.output
    assert "BUDGET_EXCEEDED" in res.output
    assert statuses(tmp_path / "k.sqlite") == ["BLIND_READER_OK", "DIVERSITY_OK"]

    resumed = invoke(
        monkeypatch, tmp_path, "resume", str(run_id), "--packages", str(packages),
        "--ledger-csv", str(ledger),
    )
    assert resumed.exit_code == 0, resumed.output
    assert "finished slots skipped" in resumed.output
    assert statuses(tmp_path / "k.sqlite") == ["BLIND_READER_OK", "BLIND_READER_OK"]

    ev = events(ledger)
    assert ev.count("BUDGET_ALLOCATION") == 1  # resume reuses the original cap
    assert "PREAUTH_HOLD" in ev and "PREAUTH_RELEASE" in ev and "LLM_DEBIT" in ev
    assert ev.count("LLM_DEBIT") == 2  # one real completion per candidate


def test_cli_max_budget_usd_stops_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = run_and_id(monkeypatch, tmp_path)
    monkeypatch.setattr("codekiln.synthesis.llm_router.http_post", fake_post)
    ledger = tmp_path / "billing.csv"
    res = invoke(
        monkeypatch, tmp_path, "synth", str(run_id), "--packages", str(tmp_path / "packages"),
        "--ledger-csv", str(ledger), "--max-budget-usd", "0.0000001",
    )
    assert res.exit_code == 1 and "BUDGET_EXCEEDED" in res.output
    assert statuses(tmp_path / "k.sqlite") == ["DIVERSITY_OK", "DIVERSITY_OK"]
    assert "LLM_DEBIT" not in events(ledger)
