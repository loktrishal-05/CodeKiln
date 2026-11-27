import csv
from pathlib import Path

import pytest

from codekiln.billing.ledger import (
    EVENTS,
    BillingLedger,
    Budget,
    BudgetExceeded,
    Pricing,
    Usage,
)


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_ledger_records_four_event_types_and_idempotent_allocation(tmp_path: Path) -> None:
    p = tmp_path / "billing.csv"
    ledger = BillingLedger(p, Pricing(0.001, 0.002))
    assert [r["event"] for r in rows(p)] == []
    assert ledger.allocate(1, 1.0) == 1.0
    assert ledger.allocate(1, 99.0) == 1.0  # resume must not raise the cap
    ledger.hold(1, 7, 0.2, "estimate")
    ledger.release(1, 7, 0.2, "estimate")
    ledger.debit(1, 7, Usage(1000, 500), "test-model", "completion")
    assert {r["event"] for r in rows(p)} == set(EVENTS)
    assert ledger.spent(1) == pytest.approx(0.002)  # 1000*0.001 + 500*0.002 / 1000
    assert ledger.held(1) == pytest.approx(0.0)
    assert ledger.remaining(1) == pytest.approx(0.998)
    assert rows(p)[-1]["model"] == "test-model"


def test_hard_budget_breaker_before_a_call(tmp_path: Path) -> None:
    ledger = BillingLedger(tmp_path / "b.csv")
    budget = Budget(ledger, 2, 0.001, "m")
    with pytest.raises(BudgetExceeded, match="budget exhausted"):
        ledger.hold(2, 1, 0.002)
    assert len([r for r in ledger.rows(2) if r["event"] == "PREAUTH_HOLD"]) == 0
    budget.guard()
    ledger.hold(2, 1, 0.0005)
    with pytest.raises(BudgetExceeded, match="budget exhausted"):
        ledger.hold(2, 1, 0.0006)


def test_response_usage_and_estimate() -> None:
    usage = Usage.from_response({"usage": {"prompt_tokens": 12, "completion_tokens": 3}})
    assert usage == Usage(12, 3)
    assert Usage.from_response({"usage": {"input_tokens": 4, "output_tokens": 2}}) == Usage(4, 2)
    assert Usage.from_response({}) == Usage()
    hold = Pricing(0.001, 0.002).estimate(
        {"messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 100}
    )
    assert hold == pytest.approx((100 * 0.001 + 100 * 0.002) / 1000)


def test_metered_post_holds_releases_and_debits(tmp_path: Path) -> None:
    ledger = BillingLedger(tmp_path / "b.csv")
    budget = Budget(ledger, 3, 1.0, "m")
    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        return {"usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
    result = budget.meter_post(post)("https://x/v1/chat/completions", {}, {"messages": []})
    assert "usage" in result
    assert [r["event"] for r in ledger.rows(3)] == [
        "BUDGET_ALLOCATION", "PREAUTH_HOLD", "PREAUTH_RELEASE", "LLM_DEBIT"
    ]
    assert ledger.held(3) == 0
    assert ledger.spent(3) > 0


def test_failed_call_releases_the_hold(tmp_path: Path) -> None:
    ledger = BillingLedger(tmp_path / "b.csv")
    budget = Budget(ledger, 4, 1.0, "m")
    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        raise OSError("offline")
    with pytest.raises(OSError):
        budget.meter_post(post)("https://x", {}, {})
    assert ledger.held(4) == 0 and ledger.spent(4) == 0
