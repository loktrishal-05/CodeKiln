"""Blind model evaluation: Harbor trials by 2+ agents, one executable scorecard.

The agent sees only instruction.md; solution/ and tests/ stay inside the Harbor task and the
verifier container. Every row carries the reward the verifier measured, the agent diff graded by
the mergeability grader, and the ATIF trace path. `eval_receipt.json` is the hand-off to the
leaderboard; the same rows are mirrored into the SQLite `eval_runs` table.
"""
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from codekiln.evaluation.mergeability_grader import grade
from codekiln.validation import harbor_runner
from codekiln.validation.blind_reader import lint_package


class EvalError(ValueError):
    pass


@dataclass(frozen=True)
class Trial:
    reward: float | None
    agent_diff: str = ""
    atif_path: str = ""
    cost_usd: float = 0.0


TrialRun = Callable[[Path, str, Path], Trial]  # (task, model, -o dir) -> measured trial


@dataclass(frozen=True)
class EvalRow:
    task: str
    model_id: str
    reward: float | None
    resolved: bool
    bloat_ratio: float
    blast_radius: int
    atif_path: str
    cost_usd: float
    usd_per_solve: float | None

    def to_json(self) -> dict[str, object]:
        return {
            "task": self.task,
            "model_id": self.model_id,
            "reward": self.reward,
            "resolved": self.resolved,
            "bloat_ratio": self.bloat_ratio,
            "blast_radius": self.blast_radius,
            "atif_path": self.atif_path,
            "cost_usd": round(self.cost_usd, 6),
            "usd_per_solve": None if self.usd_per_solve is None else round(self.usd_per_solve, 6),
        }


@dataclass(frozen=True)
class EvalReceipt:
    tasks: tuple[str, ...]
    models: tuple[str, ...]
    rows: tuple[EvalRow, ...]
    solve_matrix: Mapping[str, Mapping[str, bool]]
    weak: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return all(r.reward is not None for r in self.rows) and not self.weak

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "tasks": list(self.tasks),
                "models": list(self.models),
                "rows": [r.to_json() for r in self.rows],
                "solve_matrix": {t: dict(m) for t, m in self.solve_matrix.items()},
                "weak": list(self.weak),
            },
            indent=2,
        )


def harbor_trial(task: Path, model: str, out: Path) -> Trial:
    """One `harbor run -a MODEL` trial; reward, agent diff and ATIF are read from its output."""
    reward = harbor_runner.run(task, out, model).get(task.name)
    jobs = sorted(d for d in out.iterdir() if d.is_dir()) if out.is_dir() else []
    diff, atif, cost = "", "", 0.0
    if jobs:
        for p in jobs[-1].rglob("*"):
            name = p.name.lower()
            if p.is_file() and name == "agent.diff" and not diff:
                diff = p.read_text(encoding="utf-8", errors="replace")
            elif p.is_file() and ("atif" in name) and not atif:
                atif = str(p)
        for p in jobs[-1].rglob("result.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                agent = data.get("agent_result") or {}
                value = agent.get("cost_usd", 0.0)
                if isinstance(value, (int, float)):
                    cost += float(value)
            except (OSError, ValueError):
                continue
    return Trial(reward, diff, atif, cost)


def run_eval(
    packages: Sequence[Path],
    models: Sequence[str],
    work: Path,
    run: TrialRun | None = None,
) -> EvalReceipt:
    """Run every model on every package; refuse a task the blind reader would reject."""
    tasks = [p.resolve() for p in packages]
    names = [p.name for p in tasks]
    if len(set(names)) != len(names):
        raise EvalError("task package names must be unique")
    if len(set(models)) < 2:
        raise EvalError("blind eval needs at least two distinct models")
    for task in tasks:
        report = lint_package(task)
        if not report.ok:
            raise EvalError(f"{task.name}: blind reader failed: {'; '.join(report.problems)}")

    trial = run or harbor_trial
    rows: list[EvalRow] = []
    for task in tasks:
        reference = (task / "solution" / "reference.diff").read_text(encoding="utf-8")
        for model in models:
            result = trial(task, model, work / model / task.name)
            g = grade(reference, result.agent_diff)
            cost = result.cost_usd
            rows.append(
                EvalRow(
                    task.name, model, result.reward, result.reward == 1.0,
                    g.bloat_ratio, g.blast_radius, result.atif_path, cost,
                    cost if result.reward == 1.0 else None,
                )
            )
    matrix = {
        name: {m: any(r.task == name and r.model_id == m and r.resolved for r in rows) for m in models}
        for name in names
    }
    weak = tuple(name for name in names if all(matrix[name].values()) or not any(matrix[name].values()))
    return EvalReceipt(tuple(names), tuple(models), tuple(rows), matrix, weak)


def write_receipt(path: Path, receipt: EvalReceipt) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(receipt.to_json(), encoding="utf-8")
    return path


def read_receipt(path: Path) -> EvalReceipt:
    """Read a scorecard back for the leaderboard / CI; rejects malformed files early."""
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise EvalError("eval receipt must be a JSON object")
    rows: list[EvalRow] = []
    raw_rows = data.get("rows", [])
    if not isinstance(raw_rows, list):
        raise EvalError("eval receipt rows must be a list")
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise EvalError("eval receipt row must be an object")
        reward = raw.get("reward")
        if reward is not None and not isinstance(reward, (int, float)):
            raise EvalError("eval reward must be a number or null")
        cost = raw.get("cost_usd", 0.0)
        per_solve = raw.get("usd_per_solve")
        rows.append(
            EvalRow(
                str(raw.get("task", "")), str(raw.get("model_id", "")),
                None if reward is None else float(reward), bool(raw.get("resolved")),
                float(raw.get("bloat_ratio", 0.0)), int(raw.get("blast_radius", 0)),
                str(raw.get("atif_path", "")),
                float(cost) if isinstance(cost, (int, float)) else 0.0,
                float(per_solve) if isinstance(per_solve, (int, float)) else None,
            )
        )
    names = data.get("tasks", [])
    models = data.get("models", [])
    if not isinstance(names, list) or not isinstance(models, list):
        raise EvalError("eval receipt tasks/models must be lists")
    task_names = tuple(str(t) for t in names)
    model_ids = tuple(str(m) for m in models)
    matrix = {
        t: {m: any(r.task == t and r.model_id == m and r.resolved for r in rows) for m in model_ids}
        for t in task_names
    }
    weak = tuple(t for t in task_names if all(matrix[t].values()) or not any(matrix[t].values()))
    return EvalReceipt(task_names, model_ids, tuple(rows), matrix, weak)


def record_rows(con: sqlite3.Connection, receipt: EvalReceipt) -> int:
    """Mirror the scorecard into `eval_runs` for the dashboard; rerunning replaces old rows."""
    from codekiln.db import SCHEMA
    con.executescript(SCHEMA)
    by_package = {
        Path(path).name: cid
        for cid, path in con.execute(
            "SELECT candidate_id, package_path FROM task_plans WHERE package_path IS NOT NULL"
        )
    }
    n = 0
    for row in receipt.rows:
        if (cid := by_package.get(row.task)) is None:
            continue
        con.execute("DELETE FROM eval_runs WHERE candidate_id = ? AND model_id = ?", (cid, row.model_id))
        con.execute(
            "INSERT INTO eval_runs (candidate_id, model_id, resolved, bloat_ratio, blast_radius, atif_path)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (cid, row.model_id, int(row.resolved), row.bloat_ratio, row.blast_radius, row.atif_path),
        )
        n += 1
    con.commit()
    return n
