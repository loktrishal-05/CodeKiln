"""Orchestration of the stages that exist so far: ingest -> plan -> diversity -> ledger.

Candidates stop at DIVERSITY_OK. Synthesis, the sandbox gates and packaging are not built yet,
and PROFILED/ENV_READY are skipped until the profiler lands.
"""
import shutil
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from codekiln.db import idempotency_key
from codekiln.ingestion.repo_loader import load_repo
from codekiln.planner import plan
from codekiln.planner.difficulty import balance, level
from codekiln.planner.diversity_guard import fingerprint
from codekiln.planner.models import dump_plan
from codekiln.states import State

MODE = "dual"


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    base_commit: str
    workspace: Path
    new: int
    existing: int


def workspace_path(source: str, commit: str, workdir: Path) -> Path:
    """Where `run_pipeline` checks out SOURCE at COMMIT (so a resumed run can find it again)."""
    name = Path(source.rstrip("/")).stem or "repo"
    return workdir / f"{name}-{commit[:12]}"


def run_pipeline(
    source: str, slots: int, con: sqlite3.Connection, workdir: Path,
    remap: Mapping[str, str] | None = None,
) -> RunSummary:
    if Path(source).exists():
        source = str(Path(source).resolve())  # same repo, same key, however it was spelled
    workdir.mkdir(parents=True, exist_ok=True)
    tmp = workdir / f".tmp-{uuid.uuid4().hex[:8]}"
    commit = load_repo(source, tmp)
    ws = workspace_path(source, commit, workdir)
    if ws.exists():  # resume: this exact (repo, commit) is already checked out
        shutil.rmtree(tmp)
    else:
        tmp.rename(ws)

    # pool larger than SLOTS so the difficulty dial has levels to choose from
    chosen = balance(plan(ws, slots * 3), slots, remap=remap)

    with con:
        con.execute(
            "INSERT OR IGNORE INTO repositories (url, base_commit) VALUES (?, ?)", (source, commit)
        )
        repo_id = con.execute(
            "SELECT id FROM repositories WHERE url = ? AND base_commit = ?", (source, commit)
        ).fetchone()[0]
        run_id = con.execute(
            "INSERT INTO factory_runs (repo_id, mode, slots) VALUES (?, ?, ?)",
            (repo_id, MODE, slots),
        ).lastrowid
        assert run_id is not None
        new = 0
        for i, p in enumerate(chosen):
            key = idempotency_key(source, commit, MODE, i)
            cur = con.execute(
                "INSERT OR IGNORE INTO task_candidates (run_id, idempotency_key, category, difficulty,"
                " ast_fingerprint, status) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, key, p.category.value, level(p), fingerprint(p), State.DIVERSITY_OK.value),
            )
            new += cur.rowcount
            candidate_id = con.execute(
                "SELECT id FROM task_candidates WHERE idempotency_key = ?", (key,)
            ).fetchone()[0]
            con.execute(
                "INSERT OR IGNORE INTO task_plans (candidate_id, plan_json) VALUES (?, ?)",
                (candidate_id, dump_plan(p)),
            )
    return RunSummary(run_id, commit, ws, new, len(chosen) - new)
