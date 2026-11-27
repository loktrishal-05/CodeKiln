import json
import sqlite3
from pathlib import Path

import pytest
from spec_examples import DIFF, INSTRUCTION
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.db import connect
from codekiln.evaluation.eval_runner import (
    EvalError,
    harbor_trial,
    read_receipt,
    record_rows,
    run_eval,
    write_receipt,
)


def make_task(root: Path, name: str, instruction: str = INSTRUCTION) -> Path:
    task = root / name
    (task / "solution").mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text('[task]\nname = "' + name + '"\n')
    (task / "instruction.md").write_text(instruction)
    (task / "solution" / "reference.diff").write_text(DIFF)
    return task


def trial_factory(results: dict[tuple[str, str], float | None]):
    def trial(task: Path, model: str, out: Path):
        from codekiln.evaluation.eval_runner import Trial
        out.mkdir(parents=True, exist_ok=True)
        return Trial(
            results[(task.name, model)],
            DIFF,
            str(out / "trace.atif.json"),
            0.1,
        )
    return trial


MODELS = ["claude-code", "codex"]


def test_eval_builds_solve_matrix_and_flags_weak_tasks(tmp_path: Path) -> None:
    a, b = make_task(tmp_path, "task-a"), make_task(tmp_path, "task-b")
    trial = trial_factory({
        ("task-a", "claude-code"): 1.0, ("task-a", "codex"): 1.0,
        ("task-b", "claude-code"): 0.0, ("task-b", "codex"): 1.0,
    })
    r = run_eval([a, b], MODELS, tmp_path / "work", trial)
    assert r.tasks == ("task-a", "task-b") and r.models == tuple(MODELS)
    assert len(r.rows) == 4
    assert r.solve_matrix["task-b"] == {"claude-code": False, "codex": True}
    assert r.weak == ("task-a",)  # every model solved it -> weak discriminator
    assert not r.ok
    assert next(row for row in r.rows if row.resolved).usd_per_solve == 0.1
    assert next(row for row in r.rows if not row.resolved).usd_per_solve is None
    p = write_receipt(tmp_path / "eval_receipt.json", r)
    assert read_receipt(p) == r


def test_eval_rejects_weak_setup(tmp_path: Path) -> None:
    task = make_task(tmp_path, "task-a")
    trial = trial_factory({("task-a", "claude-code"): 1.0, ("task-a", "codex"): 1.0})
    with pytest.raises(EvalError, match="two distinct models"):
        run_eval([task], ["claude-code"], tmp_path / "work", trial)
    with pytest.raises(EvalError, match="blind reader"):
        run_eval([make_task(tmp_path, "leaky", INSTRUCTION + "\nSolution in solution/.\n")],
                 MODELS, tmp_path / "work", trial)


def test_crashed_trial_is_not_resolved(tmp_path: Path) -> None:
    a, b = make_task(tmp_path, "task-a"), make_task(tmp_path, "task-b")
    trial = trial_factory({
        ("task-a", "claude-code"): None, ("task-a", "codex"): 1.0,
        ("task-b", "claude-code"): 0.0, ("task-b", "codex"): 1.0,
    })
    r = run_eval([a, b], MODELS, tmp_path / "work", trial)
    assert not r.ok and all(not row.resolved for row in r.rows if row.reward is None)


def test_harbor_trial_reads_reward_diff_and_cost(tmp_path: Path, monkeypatch) -> None:
    task = make_task(tmp_path, "task-a")
    out = tmp_path / "out"
    trial = out / "job" / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "agent" / "agent.diff").write_text(DIFF)
    (trial / "trace.atif.json").write_text("{}")
    (trial / "result.json").write_text(json.dumps({"agent_result": {"cost_usd": 0.25}}))
    monkeypatch.setattr(
        "codekiln.validation.harbor_runner.run",
        lambda path, out, agent="oracle", concurrency=4: {"task-a": 1.0},
    )
    t = harbor_trial(task, "claude-code", out)
    assert t.reward == 1.0 and t.agent_diff == DIFF and Path(t.atif_path).name == "trace.atif.json"
    assert t.cost_usd == 0.25


def _db_with_task(tmp_path: Path, task: Path) -> Path:
    db = tmp_path / "k.sqlite"
    con = connect(str(db))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES ('u', 'c')")
    rid = con.execute("SELECT id FROM repositories").fetchone()[0]
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (?, 'dual', 1)", (rid,))
    run_id = con.execute("SELECT id FROM factory_runs").fetchone()[0]
    con.execute(
        "INSERT INTO task_candidates (run_id, idempotency_key, status) VALUES (?, 'k', 'PACKAGED')",
        (run_id,),
    )
    cid = con.execute("SELECT id FROM task_candidates").fetchone()[0]
    con.execute("INSERT INTO task_plans (candidate_id, plan_json, package_path) VALUES (?, '{}', ?)",
                (cid, str(task)))
    con.commit()
    con.close()
    return db


def test_cli_eval_writes_receipt_and_leaderboard_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_task(tmp_path, "task-a"), make_task(tmp_path, "task-b")
    db = _db_with_task(tmp_path, a)
    monkeypatch.setattr(
        "codekiln.evaluation.eval_runner.harbor_trial",
        trial_factory({
            ("task-a", "claude-code"): 1.0, ("task-a", "codex"): 0.0,
            ("task-b", "claude-code"): 0.0, ("task-b", "codex"): 1.0,
        }),
    )
    out = tmp_path / "eval_receipt.json"
    res = CliRunner().invoke(
        app, ["eval", str(a), str(b), "--work", str(tmp_path / "work"),
              "--out", str(out), "--db", str(db)]
    )
    assert res.exit_code == 0, res.output
    assert read_receipt(out).rows[0].model_id == "claude-code"
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0] == 2
    finally:
        con.close()
    # Re-running replaces rows instead of double-counting them.
    again = CliRunner().invoke(
        app, ["eval", str(a), str(b), "--work", str(tmp_path / "work"),
              "--out", str(out), "--db", str(db)]
    )
    assert again.exit_code == 0, again.output
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0] == 2
    finally:
        con.close()


def test_record_rows_skips_unknown_packages(tmp_path: Path) -> None:
    task = make_task(tmp_path, "task-a")
    r = run_eval([task], MODELS, tmp_path / "work", trial_factory({
        ("task-a", "claude-code"): 1.0, ("task-a", "codex"): 0.0,
    }))
    con = connect(str(tmp_path / "empty.sqlite"))
    try:
        assert record_rows(con, r) == 0
    finally:
        con.close()
