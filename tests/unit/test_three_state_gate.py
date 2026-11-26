import itertools
import json
from pathlib import Path

from codekiln.states import State
from codekiln.validation.three_state_gate import Phase, run_gate, write_quarantine

BASE = {"p1": "passed", "p2": "passed"}
F2P = {"f1"}


def make(b: dict[str, str], c: dict[str, str]):
    def runner(phase: Phase) -> dict[str, str]:
        return {"A": BASE, "B": {**BASE, **b}, "C": {**BASE, **c}}[phase]
    return runner


def test_happy_path():
    r = run_gate(make({"f1": "failed"}, {"f1": "passed"}), F2P)
    assert r.state is State.STATE_C_ALL_PASS and not r.quarantined


def test_f2p_already_passing_on_base_rejected():
    r = run_gate(make({"f1": "passed"}, {"f1": "passed"}), F2P)
    assert r.state is State.REJECTED and "on base" in r.reason


def test_f2p_missing_is_harness_crash():
    r = run_gate(make({}, {"f1": "passed"}), F2P)
    assert r.state is State.REJECTED and "missing" in r.reason


def test_reference_patch_failing_rejected():
    r = run_gate(make({"f1": "failed"}, {"f1": "failed"}), F2P)
    assert r.state is State.REJECTED and r.reason.startswith("STATE_C")


def test_p2p_broken_by_patch_rejected():
    r = run_gate(make({"f1": "failed"}, {"f1": "passed", "p2": "failed"}), F2P)
    assert r.state is State.REJECTED and "p2" in r.reason


def test_flaky_p2p_quarantined_not_fatal(tmp_path: Path):
    flips = itertools.cycle(["passed", "failed"])

    def runner(phase: Phase) -> dict[str, str]:
        if phase == "A":
            return BASE
        return {"p1": "passed", "p2": next(flips), "f1": "failed" if phase == "B" else "passed"}

    r = run_gate(runner, F2P)
    assert r.state is State.STATE_C_ALL_PASS and r.quarantined == {"p2"}
    write_quarantine(tmp_path / "q.json", r)
    assert json.loads((tmp_path / "q.json").read_text()) == {"quarantined": ["p2"]}


def test_flaky_f2p_rejected():
    flips = itertools.cycle(["failed", "passed"])

    def runner(phase: Phase) -> dict[str, str]:
        return BASE if phase == "A" else {**BASE, "f1": next(flips)}

    assert run_gate(runner, F2P).state is State.REJECTED


def test_base_failing_rejected_and_no_f2p_rejected():
    assert run_gate(lambda p: {"p1": "failed"}, F2P).state is State.REJECTED
    assert run_gate(make({}, {}), set()).state is State.REJECTED
