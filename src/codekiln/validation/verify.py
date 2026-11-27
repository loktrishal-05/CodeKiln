"""Executable 3-state gate: Harbor trials -> ledger rows, flakiness quarantine, candidate state.

The gate runner owns the Harbor calls; this module makes the result durable. Nothing here reads
an LLM claim: P2P/F2P statuses come from each trial's verifier/results.json. A failed gate rejects
the candidate with the exact reason, and a passing gate never regresses an already-packed candidate.
"""
import json
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from codekiln.states import State
from codekiln.validation import harbor_runner
from codekiln.validation.gate_runner import Run, gate_runner
from codekiln.validation.three_state_gate import Phase, run_gate, write_quarantine

# The gate only advances candidates that have not reached STATE_C yet.
_ADVANCEABLE = frozenset({
    State.BLIND_READER_OK.value, State.STATE_A_P2P_OK.value, State.STATE_B_F2P_FAIL.value,
})


class VerificationError(ValueError):
    pass


@dataclass(frozen=True)
class VerifyResult:
    task: str
    state: State
    reason: str
    p2p: tuple[str, ...]
    quarantined: tuple[str, ...]
    trials: int

    @property
    def ok(self) -> bool:
        return self.state is State.STATE_C_ALL_PASS

    def to_json(self) -> str:
        return json.dumps({
            "task": self.task, "ok": self.ok, "state": self.state.value, "reason": self.reason,
            "p2p": list(self.p2p), "quarantined": list(self.quarantined), "trials": self.trials,
        }, indent=2)


def find_candidate(con: sqlite3.Connection, task: Path) -> int | None:
    """Resolve a packaged task back to its candidate (exact path, then task name)."""
    name = task.resolve().name
    row = con.execute(
        "SELECT candidate_id FROM task_plans WHERE package_path = ? OR package_path LIKE ?"
        " ORDER BY candidate_id DESC LIMIT 1",
        (str(task.resolve()), f"%{name}"),
    ).fetchone()
    return None if row is None else int(row[0])


def verify_package(
    task: Path,
    work: Path,
    *,
    runs: int = 3,
    db: Path | None = None,
    candidate_id: int | None = None,
    run: Run | None = None,
    quarantine: Path | None = None,
) -> VerifyResult:
    """Run A (x1) + B (xN) + C (xN) through Harbor; persist every trial and the verdict."""
    f2p_path = task / "tests" / "fail_to_pass.json"
    if not f2p_path.is_file():
        raise VerificationError(f"{task} has no tests/fail_to_pass.json")
    f2p = set(json.loads(f2p_path.read_text(encoding="utf-8")))
    if not f2p:
        raise VerificationError(f"{task} has no fail_to_pass nodes")
    executions: list[tuple[Phase, int, int, int, int, str]] = []

    def record(phase: Phase, index: int, out: Path, results: dict[str, str]) -> None:
        p2p_nodes = {n for n in results if n not in f2p}
        ctrf = sorted(out.rglob("verifier/results.json"))
        executions.append((
            phase, index,
            0 if results else 1,  # an empty verifier result means the trial crashed
            sum(results.get(n) == "passed" for n in f2p),
            sum(results.get(n) == "passed" for n in p2p_nodes),
            str(ctrf[-1]) if ctrf else "",
        ))

    # Each invocation gets its own -o root, so a re-run never collides with old trial dirs.
    work.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="verify-", dir=work))
    gate = gate_runner(task, session, run=run or harbor_runner.run, record=record)
    verdict = run_gate(gate, f2p, runs)
    qpath = quarantine or work / "flakiness_quarantine.json"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    write_quarantine(qpath, verdict)

    if db is not None and db.exists():
        with sqlite3.connect(db) as con:
            cid = candidate_id if candidate_id is not None else find_candidate(con, task)
            if cid is not None:
                con.executemany(
                    "INSERT INTO validation_executions"
                    " (candidate_id, phase, run_index, exit_code, f2p_passed, p2p_passed, ctrf_path)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(cid, *row) for row in executions],
                )
                # Re-verifying past STATE_C records trials but never regresses candidate status.
                con.execute(
                    "UPDATE task_candidates SET status = ?, rejection_reason = ?"
                    " WHERE id = ? AND status IN (?, ?, ?)",
                    (
                        verdict.state.value, verdict.reason or None, cid,
                        *sorted(_ADVANCEABLE),
                    ),
                )
    return VerifyResult(
        task.name, verdict.state, verdict.reason, tuple(sorted(verdict.p2p)),
        tuple(sorted(verdict.quarantined)), len(executions),
    )
