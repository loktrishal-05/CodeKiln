"""#40: a model may order the allowed actions; it can never run a disallowed one or move a gate."""
import json
from pathlib import Path

import pytest
from test_factory import verify_run
from test_six_slot_synthesis import _ledger, _writer

from codekiln.billing.ledger import BillingLedger, Budget, Pricing
from codekiln.db import connect
from codekiln.factory import run_factory
from codekiln.orchestrator import (
    STOP,
    ChooserError,
    LLMChooser,
    Pending,
    parse_choice,
    pick,
)
from codekiln.synthesis.llm_router import ModelConfig

CONFIG = ModelConfig(base_url="https://p.invalid/v1", api_key="sk-test", model="fake")
P = [Pending(1, "BLIND_READER_OK", "verify", "/p/a", "api_cli", "L1"),
     Pending(2, "BLIND_READER_OK", "verify", "/p/b", "core_logic", "L2")]


def test_default_is_the_fixed_order() -> None:
    chosen, d = pick(None, P)
    assert chosen == P[0] and d.source == "default"


def test_an_allowed_index_is_honoured_and_logged_as_the_agent() -> None:
    chosen, d = pick(lambda p: 1, P)
    assert chosen == P[1] and (d.source, d.candidate_id) == ("agent", 2)


@pytest.mark.parametrize("bad", [5, -2, 2])
def test_an_index_outside_the_allowed_list_falls_back(bad: int) -> None:
    chosen, d = pick(lambda p: bad, P)
    assert chosen == P[0] and d.source == "fallback" and "not an allowed action" in d.note


def test_a_chooser_failure_falls_back() -> None:
    def broken(p):  # type: ignore[no-untyped-def]
        raise ChooserError("provider down")

    chosen, d = pick(broken, P)
    assert chosen == P[0] and d.source == "fallback" and "provider down" in d.note


def test_stop_ends_the_run() -> None:
    chosen, d = pick(lambda p: STOP, P)
    assert chosen is None and d.stage == "stop"


@pytest.mark.parametrize("text,want", [('{"index": 1}', 1), ('noise {"stop": true} noise', STOP),
                                       ('```json\n{"index": 0}\n```', 0)])
def test_parse_accepts_only_the_strict_shapes(text: str, want: int) -> None:
    assert parse_choice(text) == want


@pytest.mark.parametrize("text", ["I pick the first", '{"index": "1"}', '{"index": true}', "[1]", '{"go": 1}'])
def test_parse_rejects_everything_else(text: str) -> None:
    with pytest.raises(ChooserError):
        parse_choice(text)


def reply(content: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 50, "completion_tokens": 5}}


def test_llm_chooser_sends_only_the_allowed_actions_and_is_metered(tmp_path: Path) -> None:
    ledger = BillingLedger(tmp_path / "b.csv", Pricing())
    budget = Budget(ledger, 1, 5.0, "fake")
    seen: list[dict[str, object]] = []

    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        seen.append(payload)
        return reply('{"index": 1}')

    chooser = LLMChooser(CONFIG, post=budget.meter_post(post), budget=budget)
    assert chooser(P) == 1
    sent = json.loads(seen[0]["messages"][1]["content"])  # type: ignore[index]
    assert [o["stage"] for o in sent] == ["verify", "verify"] and {"index", "candidate"} <= set(sent[0])
    assert ledger.spent(1) > 0


def test_llm_chooser_turns_provider_errors_and_exhausted_budget_into_fallbacks(tmp_path: Path) -> None:
    from codekiln.synthesis.llm_router import RouterError

    def down(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        raise RouterError("HTTP 503")

    assert pick(LLMChooser(CONFIG, post=down), P)[1].source == "fallback"
    budget = Budget(BillingLedger(tmp_path / "b.csv", Pricing()), 1, 1.0, "fake")
    budget.ledger.hold(1, 0, 1.0)  # reserve the whole allocation: nothing is left to spend
    _, d = pick(LLMChooser(CONFIG, post=lambda *a: reply('{"index": 1}'), budget=budget), P)
    assert d.source == "fallback"  # the guard fired before any provider call


def run_with(tmp_path: Path, chooser, **kw):  # type: ignore[no-untyped-def]
    run_id = _ledger(tmp_path, 3)
    db = tmp_path / "k.sqlite"
    budget = Budget(BillingLedger(tmp_path / "billing.csv", Pricing()), run_id, 5.0, "fake-model")
    order: list[str] = []
    inner = verify_run()

    def traced(task: Path, out: Path, agent: str) -> None:
        if agent == "nop" and not order.count(task.name):
            order.append(task.name)
        inner(task, out, agent)

    con = connect(str(db))
    try:
        res = run_factory(con, run_id, writer=_writer(budget, []), staging=tmp_path / "staging",
                          dest=tmp_path / "packages", work=tmp_path / "work", db=db, budget=budget,
                          verify_run=traced, probe_run=lambda t, o: 0.0, chooser=chooser, **kw)
    finally:
        con.close()
    return res, order


def test_the_agent_reorders_work_but_every_package_still_passes_the_same_gates(tmp_path: Path) -> None:
    res, order = run_with(tmp_path, lambda pending: len(pending) - 1)  # always the LAST pending action
    assert len(res.packaged) == 3 and not res.rejected
    assert order == sorted(order, reverse=True)  # the last candidate was verified first
    assert {d.source for d in res.decisions} == {"agent"}


def test_the_step_limit_binds_even_when_the_agent_never_stops(tmp_path: Path) -> None:
    res, _ = run_with(tmp_path, lambda pending: 0, max_steps=3)
    assert res.steps <= 3 and res.unfinished > 0


def test_an_agent_that_stops_leaves_the_rest_unfinished(tmp_path: Path) -> None:
    res, _ = run_with(tmp_path, lambda pending: STOP)
    assert not res.packaged and res.unfinished == 3 and res.decisions[-1].stage == "stop"
