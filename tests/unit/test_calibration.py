"""#43: measured solve rates, not the AST heuristic, say how hard a task really was."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.db import connect
from codekiln.evaluation.calibration import (
    Calibration,
    calibrate,
    learn_remap,
    observed_level,
    read_remap,
    write_calibration,
)
from codekiln.evaluation.eval_runner import EvalReceipt, EvalRow
from codekiln.planner.difficulty import balance, level
from codekiln.planner.models import Category, Plan


def row(task: str, model: str, reward: float | None) -> EvalRow:
    return EvalRow(task, model, reward, reward == 1.0, 1.0, 0, "", 0.0, None)


def receipt(rows: list[EvalRow]) -> EvalReceipt:
    tasks = tuple(dict.fromkeys(r.task for r in rows))
    models = tuple(dict.fromkeys(r.model_id for r in rows))
    matrix = {t: {m: any(r.task == t and r.model_id == m and r.resolved for r in rows) for m in models} for t in tasks}
    return EvalReceipt(tasks, models, tuple(rows), matrix, ())


def test_observed_level_is_coarse_and_honest() -> None:
    assert [observed_level(2, 2), observed_level(1, 2), observed_level(0, 2)] == ["L1", "L2", "L3"]


def test_calibration_uses_solve_rates_and_ignores_crashed_trials() -> None:
    rec = receipt([
        row("easy", "a", 1.0), row("easy", "b", 1.0),
        row("mid", "a", 1.0), row("mid", "b", 0.5),
        row("hard", "a", 0.0), row("hard", "b", 0.0),
        row("flaky", "a", 1.0), row("flaky", "b", None),  # b crashed: that is no evidence of difficulty
    ])
    cals = {c.task: c for c in calibrate(rec, {"easy": "L3", "mid": "L2"})}
    assert (cals["easy"].observed, cals["easy"].heuristic, cals["easy"].discriminates) == ("L1", "L3", False)
    assert (cals["mid"].observed, cals["mid"].solve_rate, cals["mid"].discriminates) == ("L2", 0.5, True)
    assert cals["hard"].observed == "L3" and not cals["hard"].discriminates
    assert "flaky" not in cals  # one valid trial is not enough to call a difficulty


def cal(task: str, heuristic: str, observed: str) -> Calibration:
    return Calibration(task, 1, 2, observed, heuristic)


def test_a_remap_needs_enough_samples_and_a_clear_majority() -> None:
    few = (cal("a", "L3", "L1"), cal("b", "L3", "L1"))
    assert learn_remap(few) == {}  # two tasks are not enough to trust
    enough = (cal("a", "L3", "L1"), cal("b", "L3", "L1"), cal("c", "L3", "L2"), cal("d", "L2", "L2"))
    assert learn_remap(enough) == {"L3": "L1"}  # L3 guesses were mostly really L1; L2 was right
    split = (cal("a", "L3", "L1"), cal("b", "L3", "L2"), cal("c", "L3", "L3"))
    assert learn_remap(split) == {}  # no majority: leave the heuristic alone


def plans() -> list[Plan]:
    one = Plan(Category.CORE_LOGIC, frozenset({"a.py"}), frozenset({"f"}), "forward")  # heuristic L1
    big = Plan(Category.CORE_LOGIC, frozenset({f"d{i}/m.py" for i in range(5)}), frozenset({"g"}), "forward")
    return [one, big]


def test_the_dial_follows_the_remap_when_balancing() -> None:
    one, big = plans()
    assert (level(one), level(big)) == ("L1", "L3")
    # eval showed what the heuristic calls L3 is really easy: ask for hard (L3) work and get the other plan
    assert balance([one, big], 1, mix=(0, 0, 1)) == [big]
    assert balance([one, big], 1, mix=(0, 0, 1), remap={"L3": "L1", "L1": "L3"}) == [one]
    assert balance([one, big], 2) == balance([one, big], 2, remap={})  # no remap: unchanged behaviour


def test_remap_file_round_trips_and_rejects_nonsense(tmp_path: Path) -> None:
    path = write_calibration(tmp_path / "c.json", (cal("a", "L3", "L1"),), {"L3": "L1"})
    assert read_remap(path) == {"L3": "L1"}
    assert json.loads(path.read_text())["tasks"][0]["observed"] == "L1"
    bad = tmp_path / "bad.json"
    bad.write_text('{"remap": {"L3": "hard"}}')
    with pytest.raises(ValueError):
        read_remap(bad)


def test_cli_reports_heuristic_vs_observed_from_the_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = receipt([row("click-a", "m1", 1.0), row("click-a", "m2", 1.0), row("click-b", "m1", 1.0), row("click-b", "m2", 0.0)])
    rec_path = tmp_path / "eval_receipt.json"
    rec_path.write_text(rec_json(rec))
    db = tmp_path / "k.sqlite"
    con = connect(str(db))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES ('u', 'c')")
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (1, 'dual', 1)")
    for i, (name, diff) in enumerate((("click-a", "L3"), ("click-b", "L2")), 1):
        con.execute("INSERT INTO task_candidates (run_id, idempotency_key, difficulty, status) VALUES (1, ?, ?, 'PACKAGED')", (f"k{i}", diff))
        con.execute("INSERT INTO task_plans (candidate_id, plan_json, package_path) VALUES (?, '{}', ?)", (i, f"/p/{name}"))
    con.commit()
    con.close()
    monkeypatch.setenv("CODEKILN_DB_PATH", str(db))
    out = tmp_path / "calibration.json"
    res = CliRunner().invoke(app, ["calibrate", str(rec_path), "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "click-a: guessed L3, observed L1 (2/2 solved)  (does not separate models)" in res.output
    assert "click-b: guessed L2, observed L2 (1/2 solved)" in res.output
    assert json.loads(out.read_text())["remap"] == {}  # two tasks: too few to trust a remap yet
    assert CliRunner().invoke(app, ["calibrate", str(tmp_path / "nope.json")]).exit_code == 2


def rec_json(rec: EvalReceipt) -> str:
    return rec.to_json()


def test_a_bad_calibration_file_stops_run_and_factory_before_any_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"remap": {"L9": "L1"}}')
    monkeypatch.setenv("CODEKILN_DB_PATH", str(tmp_path / "k.sqlite"))
    monkeypatch.setenv("CODEKILN_LLM_API_KEY", "sk-test")
    for cmd in ("run", "factory"):
        res = CliRunner().invoke(app, [cmd, "https://example.invalid/r.git", "--calibration", str(bad)])
        assert res.exit_code == 2 and "bad calibration file" in res.output, (cmd, res.output)
