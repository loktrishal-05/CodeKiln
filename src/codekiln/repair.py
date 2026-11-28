"""Execution-guided repair: a candidate a gate rejected gets one more try, told what really happened.

The model is never asked whether its work is good. It is shown the evidence the gates produced
(pytest's own failure text from the State C trial, or the mutants its tests failed to kill) and asked
for a corrected spec; the corrected package then goes through the same gates from the start.
"""
import json
import re
import shutil
import sqlite3
from pathlib import Path

from codekiln.packaging.harbor import PackageError
from codekiln.planner.models import load_plan
from codekiln.states import State
from codekiln.synthesis.emit import write_package
from codekiln.synthesis.llm_router import RouterError, Writer
from codekiln.synthesis.models import SynthesisError, from_dict, validate
from codekiln.synthesis.runner import build_plan, spec_hash
from codekiln.validation.blind_reader import lint_instruction

MAX_REPAIRS = 2  # a repair fixes what the last run showed; the next run can show a new problem (real runs did)
MAX_EVIDENCE_CHARS = 2500  # the request must stay under a provider's per-request token cap

_FAILURES = re.compile(r"=+ FAILURES =+\n(.*?)(?=\n=+ (?:PASSES|short test summary)|\Z)", re.DOTALL)


def evidence(con: sqlite3.Connection, cid: int, package: Path, reason: str) -> str:
    """What the gates actually saw, for the writer: the reason plus pytest's failure text or the survivors."""
    parts = [f"Gate verdict: {reason}"]
    row = con.execute(
        "SELECT ctrf_path FROM validation_executions WHERE candidate_id = ? AND phase = 'C'"
        " AND ctrf_path != '' ORDER BY id DESC LIMIT 1", (cid,),
    ).fetchone()
    if reason.startswith("STATE_C") and row:
        log = Path(row[0]).with_name("pytest.txt")  # Harbor's verifier dir keeps pytest's own output
        if log.is_file():
            text = log.read_text(encoding="utf-8", errors="replace")
            m = _FAILURES.search(text)
            # No FAILURES section means pytest ran no test (an ImportError in conftest, a syntax
            # error): the end of the log is where the error is, so that is the evidence then.
            shown = m.group(1) if m else text[-MAX_EVIDENCE_CHARS:]
            parts.append("pytest output of the failing run:\n" + shown.strip()[:MAX_EVIDENCE_CHARS])
    if reason.startswith("mutation"):
        dossier = package / "mutation_dossier.json"
        if dossier.is_file():
            survivors = [p["name"] for p in json.loads(dossier.read_text(encoding="utf-8")).get("probes", [])
                         if p.get("kind") == "semantic" and not p.get("killed")]
            if survivors:
                parts.append("These wrong implementations of your feature STILL PASSED all your tests: "
                             + ", ".join(survivors) + ". Your feature tests must assert exact values, "
                             "both branches and error cases, so that a wrong implementation fails them. "
                             "The reference patch also needs real logic (comparisons, branches) to test.")
    if "could be built" in reason:  # the probes make wrong variants out of decisions in the new code
        parts.append("Fewer than 3 wrong variants could be made from the reference patch: it needs at least "
                     "3 real decisions in the NEW code (if/else, and/or, comparisons, error branches) that "
                     "your tests then exercise both ways.")
    return "\n\n".join(parts)


def repair(con: sqlite3.Connection, cid: int, writer: Writer, workspace: Path | None,
           package: Path, repo: str, commit: str) -> bool:
    """One corrected attempt for a REJECTED candidate; True when a new package is ready to re-verify."""
    plan_json, repairs = con.execute(
        "SELECT plan_json, COALESCE(repairs, 0) FROM task_plans WHERE candidate_id = ?", (cid,)).fetchone()
    reason = con.execute("SELECT rejection_reason FROM task_candidates WHERE id = ?", (cid,)).fetchone()[0] or ""
    if repairs >= MAX_REPAIRS or not (package / "spec.json").is_file():
        return False
    con.execute("UPDATE task_plans SET repairs = ? WHERE candidate_id = ?", (repairs + 1, cid))  # count tries, not wins
    con.commit()
    try:
        previous = from_dict(json.loads((package / "spec.json").read_text(encoding="utf-8")))
        plan = build_plan(plan_json, workspace) if workspace else load_plan(plan_json)
        spec = writer.write(plan, repo=repo, base_commit=commit, previous=previous,
                            feedback=evidence(con, cid, package, reason))
        validate(spec)
        if not lint_instruction(spec.instruction).ok:
            return False
        shutil.rmtree(package, ignore_errors=True)  # nothing of the failed attempt (dossier, P2P) may survive
        write_package(package, spec, repo_url=repo, base_commit=commit)
    except (SynthesisError, RouterError, PackageError, OSError, ValueError, KeyError):
        return False
    con.execute("UPDATE task_candidates SET status = ?, rejection_reason = NULL WHERE id = ?",
                (State.BLIND_READER_OK.value, cid))
    con.execute("UPDATE task_plans SET spec_sha256 = ?, updated_at = datetime('now') WHERE candidate_id = ?",
                (spec_hash(spec), cid))
    con.commit()
    return True
