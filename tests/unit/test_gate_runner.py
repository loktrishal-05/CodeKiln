import json
from pathlib import Path

from codekiln.states import State
from codekiln.validation.gate_runner import gate_runner, read_results
from codekiln.validation.three_state_gate import run_gate

F2P = {"t::new"}


def make_task(tmp_path: Path) -> Path:
    (tmp_path / "task/tests").mkdir(parents=True)
    (tmp_path / "task/tests/fail_to_pass.json").write_text(json.dumps(sorted(F2P)))
    return tmp_path / "task"


def fake_run(calls: list[tuple[str, Path]], oracle_results: dict[str, str] | None = None):
    def run(task: Path, out: Path, agent: str) -> None:
        calls.append((agent, out))
        res = oracle_results or {"t::old": "passed", "t::new": "passed"}
        if agent == "nop":
            res = {"t::old": "passed", "t::new": "failed"}
        v = out / "2026-01-01__00-00-00" / "task__abc" / "verifier"
        v.mkdir(parents=True)
        (v / "results.json").write_text(json.dumps(res))

    return run


def test_gate_passes_end_to_end_with_fresh_dirs(tmp_path: Path):
    calls: list[tuple[str, Path]] = []
    r = run_gate(gate_runner(make_task(tmp_path), tmp_path / "work", fake_run(calls)), F2P)
    assert r.state is State.STATE_C_ALL_PASS and r.p2p == {"t::old"}
    agents = [a for a, _ in calls]
    assert agents == ["nop"] * 4 + ["oracle"] * 3  # A + 3xB on nop, 3xC on oracle
    assert len({o for _, o in calls}) == len(calls)  # one -o dir per call


def test_oracle_failure_rejects(tmp_path: Path):
    run = fake_run([], {"t::old": "passed", "t::new": "failed"})
    r = run_gate(gate_runner(make_task(tmp_path), tmp_path / "work", run), F2P)
    assert r.state is State.REJECTED and "reference patch fails" in r.reason


def test_crashed_trial_reads_as_empty(tmp_path: Path):
    assert read_results(tmp_path / "missing") == {}
    (tmp_path / "out/job/trial").mkdir(parents=True)  # no verifier/ -> crash before verifying
    assert read_results(tmp_path / "out") == {}
