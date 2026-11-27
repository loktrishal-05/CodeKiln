"""Synthesis stage: DIVERSITY_OK candidates -> SYNTHESIZED packages, or REJECTED with a reason.

Execution is the only judge later (the gate), so this stage refuses to guess: a writer result must
pass `validate` + RTM before a package is written, and repeated failures dead-letter the candidate
instead of looping forever. One candidate per transaction so an interrupted run keeps its progress.
"""
import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path

from codekiln.billing.ledger import Budget, BudgetExceeded
from codekiln.packaging.harbor import PackageError
from codekiln.planner.models import Plan, load_plan
from codekiln.states import State
from codekiln.synthesis.emit import write_package
from codekiln.synthesis.llm_router import RouterError, Writer
from codekiln.synthesis.models import FeatureSpec, SynthesisError, validate
from codekiln.synthesis.scout import context_pack
from codekiln.validation.blind_reader import lint_instruction

MAX_ATTEMPTS = 3

_CANDIDATES = """
SELECT c.id, c.status, p.plan_json, COALESCE(p.attempts, 0)
FROM task_candidates c
LEFT JOIN task_plans p ON p.candidate_id = c.id
WHERE c.run_id = ? ORDER BY c.id
"""


@dataclass
class SynthSummary:
    synthesized: int = 0
    failed: int = 0  # retryable; candidate stays DIVERSITY_OK
    rejected: int = 0  # dead-lettered after MAX_ATTEMPTS
    skipped: int = 0  # already past DIVERSITY_OK
    stopped: str = ""  # hard stop reason (e.g. budget exhausted)
    packages: list[Path] = field(default_factory=list)


def build_plan(plan_json: str, workspace: Path | None) -> Plan:
    """The stored slot plus, when a checkout exists, the workspace (edits apply to it) and Repo Scout pack."""
    plan = load_plan(plan_json)
    if workspace is not None:
        plan = replace(plan, workspace=str(workspace))
        try:  # the pack is a hint: a repo it cannot read must not block synthesis
            plan = replace(plan, context=context_pack(workspace, plan.files))
        except (OSError, ValueError):
            pass
    return plan


def synthesize(
    con: sqlite3.Connection,
    run_id: int,
    writer: Writer,
    packages: Path,
    max_attempts: int = MAX_ATTEMPTS,
    budget: Budget | None = None,
    workspace: Path | None = None,
) -> SynthSummary:
    run = con.execute(
        "SELECT r.url, r.base_commit FROM factory_runs f JOIN repositories r ON r.id = f.repo_id"
        " WHERE f.id = ?",
        (run_id,),
    ).fetchone()
    if not run:
        raise ValueError(f"run {run_id} not found in the ledger")
    repo_url, base_commit = run
    packages.mkdir(parents=True, exist_ok=True)
    out = SynthSummary()

    for cid, status, plan_json, attempts in con.execute(_CANDIDATES, (run_id,)).fetchall():
        if status != State.DIVERSITY_OK.value:
            out.skipped += 1
            continue
        if not plan_json:
            _reject(con, cid, "no persisted plan; re-run `codekiln run` on this repo")
            out.rejected += 1
            continue
        if budget is not None:
            budget.start_candidate(cid)
            try:
                budget.guard()
            except BudgetExceeded as e:
                # Progress so far is committed; remaining candidates stay DIVERSITY_OK for resume.
                out.stopped = f"BUDGET_EXCEEDED: {e}"
                break
        try:
            plan = build_plan(plan_json, workspace)
            spec = writer.write(plan, repo=repo_url, base_commit=base_commit)
            validate(spec)  # the task name becomes a path: check it before it is used (and rmtree'd)
        except BudgetExceeded as e:
            out.stopped = f"BUDGET_EXCEEDED: {e}"
            break
        except (SynthesisError, RouterError, OSError, ValueError) as e:
            _fail(con, out, cid, attempts, e, max_attempts)
            continue

        # The blind reader judges only what the agent will see, before any file is written.
        report = lint_instruction(spec.instruction)
        if not report.ok:
            _reject(con, cid, "BLIND_READER: " + "; ".join(report.problems))
            out.rejected += 1
            continue

        dst = packages / spec.task
        if dst.exists():  # write_task overwrites, so another candidate's package would be lost
            dst = packages / f"{spec.task}-c{cid}"
        try:
            write_package(dst, spec, repo_url=repo_url, base_commit=base_commit)
        except (PackageError, OSError, ValueError) as e:
            shutil.rmtree(dst, ignore_errors=True)
            _fail(con, out, cid, attempts, e, max_attempts)
            continue

        con.execute(
            "UPDATE task_candidates SET status = ?, rejection_reason = NULL WHERE id = ?",
            (State.BLIND_READER_OK.value, cid),
        )
        con.execute(
            "UPDATE task_plans SET spec_sha256 = ?, package_path = ?, last_error = NULL,"
            " updated_at = datetime('now') WHERE candidate_id = ?",
            (spec_hash(spec), str(dst), cid),
        )
        con.commit()
        out.synthesized += 1
        out.packages.append(dst)
    return out


def spec_hash(spec: FeatureSpec) -> str:
    """Stable identity of a proposal; used for idempotency and the ledger."""
    blob = json.dumps(spec.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _fail(
    con: sqlite3.Connection,
    summary: SynthSummary,
    candidate_id: int,
    attempts: int,
    error: Exception,
    max_attempts: int,
) -> None:
    summary.failed += 1
    message = f"{type(error).__name__}: {error}"
    summary.rejected += _record_failure(con, candidate_id, attempts + 1, message, max_attempts)


def _record_failure(
    con: sqlite3.Connection, candidate_id: int, attempts: int, error: str, max_attempts: int
) -> int:
    """Increment attempts, dead-letter at the cap; returns 1 when this call rejected the candidate."""
    con.execute(
        "UPDATE task_plans SET attempts = ?, last_error = ?, updated_at = datetime('now')"
        " WHERE candidate_id = ?",
        (attempts, error, candidate_id),
    )
    rejected = 0
    if attempts >= max_attempts:
        _reject(con, candidate_id, f"dead-letter after {attempts} attempts: {error}")
        rejected = 1
    con.commit()
    return rejected


def _reject(con: sqlite3.Connection, candidate_id: int, reason: str) -> None:
    con.execute(
        "UPDATE task_candidates SET status = ?, rejection_reason = ? WHERE id = ?",
        (State.REJECTED.value, reason, candidate_id),
    )
    con.commit()
