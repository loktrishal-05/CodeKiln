"""Factory driver: the ledger status of each candidate picks its next stage, so a run can resume.

DIVERSITY_OK --synthesize--> BLIND_READER_OK --verify--> STATE_C_ALL_PASS --probe--> MUTATION_OK
--package--> PACKAGED. A failed gate moves the candidate to REJECTED with the exact reason.
Nothing here trusts a model: every transition is made by a gate that ran (Harbor trials, mutation
probes, the scrubber). The Harbor runners are injected so the whole loop is testable without Docker.
"""
import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from codekiln.billing.ledger import Budget, BudgetExceeded
from codekiln.config import Settings
from codekiln.orchestrator import Chooser, Decision, Pending, pick
from codekiln.packaging.packager import PackageRejected, PackageSpec, promote
from codekiln.packaging.scrubber import audit_task
from codekiln.repair import repair
from codekiln.states import State
from codekiln.synthesis.llm_router import Writer
from codekiln.synthesis.runner import synthesize
from codekiln.validation import harbor_runner
from codekiln.validation.firewall import check as firewall_check
from codekiln.validation.gate_runner import Run
from codekiln.validation.mutation_probe import ProbeRun, probe_package, read_dossier, write_dossier
from codekiln.validation.verify import verify_package

# Status -> the stage that can advance it. PACKAGED and REJECTED are terminal.
NEXT_STAGE = {
    State.BLIND_READER_OK.value: "verify",
    State.STATE_C_ALL_PASS.value: "probe",
    State.MUTATION_OK.value: "package",
}
DEFAULT_MAX_STEPS = 200  # a hard bound on stage executions per call, whatever the ledger says


@dataclass
class FactorySummary:
    steps: int = 0
    packaged: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)  # task -> reason
    unfinished: int = 0  # candidates left mid-pipeline (budget stop, step limit, retryable failure)
    stopped: str = ""
    decisions: list[Decision] = field(default_factory=list)  # who picked each action, and why


@dataclass
class Context:
    con: sqlite3.Connection
    db: Path
    work: Path
    dest: Path
    runs: int = 3
    model: str = "codekiln"
    cost_usd: float = 0.0
    verify_run: Run | None = None
    probe_run: ProbeRun | None = None
    corpus: Sequence[Path] = ()
    writer: Writer | None = None  # set => a gate-rejected candidate gets one corrected attempt
    workspace: Path | None = None
    repo: str = ""
    commit: str = ""


def _reject(con: sqlite3.Connection, cid: int, reason: str) -> None:
    con.execute(
        "UPDATE task_candidates SET status = ?, rejection_reason = ? WHERE id = ?",
        (State.REJECTED.value, reason, cid),
    )
    con.commit()


def _set(con: sqlite3.Connection, cid: int, state: State) -> None:
    con.execute("UPDATE task_candidates SET status = ? WHERE id = ?", (state.value, cid))
    con.commit()


def _verify(ctx: Context, cid: int, package: Path) -> None:
    ctx.con.commit()  # release our write lock: verify_package opens its own connection
    verify_package(package, ctx.work / "verify", runs=ctx.runs, db=ctx.db, candidate_id=cid, run=ctx.verify_run)


def _probe(ctx: Context, cid: int, package: Path) -> None:
    if ctx.corpus:  # cheapest gate first: a feature a later release already has is not new
        report = firewall_check(package, ctx.corpus)
        (package / "firewall.json").write_text(report.to_json(), encoding="utf-8", newline="\n")
        if not report.ok:
            _reject(ctx.con, cid, "firewall: already in a later release: "
                    + ", ".join(sorted({h.marker for h in report.hits})))
            return
    dossier = probe_package(
        package, ctx.work / "probes", run=ctx.probe_run, threshold=Settings().mutant_kill_threshold
    )
    write_dossier(package / "mutation_dossier.json", dossier)  # travels with the sealed package
    if dossier.ok:
        _set(ctx.con, cid, State.MUTATION_OK)
    else:
        _reject(ctx.con, cid, "mutation: " + "; ".join(dossier.reasons))


def _trial_rewards(ctx: Context, cid: int, package: Path) -> dict[str, list[float]]:
    """Rewards of the gate's latest nop (State B) and oracle (State C) trials, by the judges' formula:
    any regression -> 0.0, else passed feature tests / feature tests. A repaired candidate has older
    trials of its earlier packages in the ledger, so only the last `runs` of each phase count."""
    tests = package / "tests"
    f2p = len(json.loads((tests / "fail_to_pass.json").read_text(encoding="utf-8")))
    p2p = len(json.loads((tests / "pass_to_pass.json").read_text(encoding="utf-8")))
    out: dict[str, list[float]] = {}
    for phase, key in (("B", "nop_rewards"), ("C", "oracle_rewards")):
        rows = ctx.con.execute(
            "SELECT f2p_passed, p2p_passed FROM validation_executions WHERE candidate_id = ? AND phase = ?"
            " ORDER BY id DESC LIMIT ?", (cid, phase, ctx.runs)).fetchall()
        out[key] = [0.0 if q < p2p else f / f2p for f, q in reversed(rows)]
    return out


def _package(ctx: Context, cid: int, package: Path) -> None:
    dossier = read_dossier(package / "mutation_dossier.json")
    sha = ctx.con.execute("SELECT spec_sha256 FROM task_plans WHERE candidate_id = ?", (cid,)).fetchone()
    spec = PackageSpec(
        package.name, ctx.model, ctx.cost_usd, True,  # True: this stage is only reached through the gates
        gates={"state_c_all_pass": True, "mutation_semantic_kill_rate": dossier.semantic_kill_rate,
               **_trial_rewards(ctx, cid, package)},
        spec_sha256=(sha[0] or "") if sha else "",
    )
    try:
        placed = promote(package, ctx.dest / package.name, spec, audit_task(package))
    except (PackageRejected, OSError, ValueError) as e:
        _reject(ctx.con, cid, f"package: {e}")
        return
    manifest = hashlib.sha256((placed / "manifest.sha256").read_bytes()).hexdigest()
    ctx.con.execute(
        "UPDATE task_candidates SET status = ?, package_sha256 = ?, rejection_reason = NULL WHERE id = ?",
        (State.PACKAGED.value, manifest, cid),
    )
    ctx.con.execute("UPDATE task_plans SET package_path = ? WHERE candidate_id = ?", (str(placed), cid))
    ctx.con.commit()


STAGES: dict[str, Callable[[Context, int, Path], None]] = {
    "verify": _verify, "probe": _probe, "package": _package,
}


# A rejection the writer can act on; a firewall or packaging verdict is not something it can fix.
_REPAIRABLE = ("STATE_", "mutation")


def _try_repair(ctx: Context, cid: int, package: Path, budget: Budget | None) -> bool:
    status, _, reason = _row(ctx.con, cid)
    if ctx.writer is None or status != State.REJECTED.value or not (reason or "").startswith(_REPAIRABLE):
        return False
    try:
        if budget is not None:
            budget.start_candidate(cid)  # the retry is billed to the candidate it repairs
            budget.guard()
        return repair(ctx.con, cid, ctx.writer, ctx.workspace, package, ctx.repo, ctx.commit)
    except BudgetExceeded:
        return False


def _row(con: sqlite3.Connection, cid: int) -> tuple[str, str, str | None]:
    status, path, reason = con.execute(
        "SELECT c.status, p.package_path, c.rejection_reason FROM task_candidates c"
        " LEFT JOIN task_plans p ON p.candidate_id = c.id WHERE c.id = ?", (cid,),
    ).fetchone()
    return status, path or "", reason


def _pending(con: sqlite3.Connection, run_id: int, stuck: set[int]) -> list[Pending]:
    rows = con.execute(
        "SELECT c.id, c.status, p.package_path, COALESCE(c.category, ''), COALESCE(c.difficulty, '')"
        " FROM task_candidates c JOIN task_plans p ON p.candidate_id = c.id"
        " WHERE c.run_id = ? AND p.package_path IS NOT NULL ORDER BY c.id", (run_id,),
    ).fetchall()
    return [Pending(cid, st, NEXT_STAGE[st], path, cat, diff)
            for cid, st, path, cat, diff in rows if st in NEXT_STAGE and cid not in stuck]


def run_factory(
    con: sqlite3.Connection,
    run_id: int,
    *,
    writer: Writer,
    staging: Path,
    dest: Path,
    work: Path,
    db: Path,
    budget: Budget | None = None,
    verify_run: Run | None = None,
    probe_run: ProbeRun | None = None,
    runs: int = 3,
    model: str = "codekiln",
    max_steps: int = DEFAULT_MAX_STEPS,
    chooser: Chooser | None = None,
    workspace: Path | None = None,
    corpus: Sequence[Path] = (),
) -> FactorySummary:
    """Drive every candidate of RUN_ID as far as its gates allow; safe to call again to resume."""
    out = FactorySummary()
    synth = synthesize(con, run_id, writer, staging, budget=budget, workspace=workspace)
    out.steps += 1
    out.stopped = synth.stopped
    repo, commit = con.execute(
        "SELECT r.url, r.base_commit FROM factory_runs f JOIN repositories r ON r.id = f.repo_id WHERE f.id = ?",
        (run_id,)).fetchone()
    ctx = Context(con, db, work, dest, runs, model,
                  cost_usd=0.0, verify_run=verify_run or harbor_runner.run, probe_run=probe_run, corpus=corpus,
                  writer=writer, workspace=workspace, repo=repo, commit=commit)
    ids = [r[0] for r in con.execute("SELECT id FROM task_candidates WHERE run_id = ? ORDER BY id", (run_id,))]
    stuck: set[int] = set()  # a gate that did not move its candidate is not retried in this call
    while out.steps < max_steps:
        pending = _pending(con, run_id, stuck)
        if not pending:
            break
        chosen, decision = pick(chooser, pending)  # the agent (if any) only orders allowed actions
        out.decisions.append(decision)
        if chosen is None:
            break
        ctx.cost_usd = sum(  # this candidate's own LLM spend, from the append-only billing ledger
            float(r["amount_usd"]) for r in (budget.ledger.rows(run_id) if budget else [])
            if r["event"] == "LLM_DEBIT" and r["candidate_id"] == str(chosen.candidate_id)
        )
        STAGES[chosen.stage](ctx, chosen.candidate_id, Path(chosen.package))
        out.steps += 1
        if _try_repair(ctx, chosen.candidate_id, Path(chosen.package), budget):
            continue  # a corrected package is back at BLIND_READER_OK: verify it from the start
        if _row(con, chosen.candidate_id)[0] == chosen.status:
            stuck.add(chosen.candidate_id)
    for cid in ids:
        status, path, reason = _row(con, cid)
        name = Path(path).name if path else f"candidate-{cid}"
        if status == State.PACKAGED.value:
            out.packaged.append(name)
        elif status == State.REJECTED.value:
            out.rejected[name] = reason or ""
        elif status in NEXT_STAGE or status == State.DIVERSITY_OK.value:
            out.unfinished += 1
    return out
