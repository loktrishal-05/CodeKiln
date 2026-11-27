import json
from pathlib import Path

from codekiln.evaluation.variation_matrix import run_matrix
from codekiln.validation.harbor_runner import read_rewards


def _task(root: Path, name: str) -> Path:
    t = root / name
    for sub in ("solution", "tests", "environment"):
        (t / sub).mkdir(parents=True)
    (t / "solution" / "solve.sh").write_text(f"echo {name}\n")
    (t / "tests" / "test.sh").write_text(f"check {name}\n")
    (t / "instruction.md").write_text(name)
    return t


def _fake_runner(scores: dict[tuple[str, str], float | None]):
    """Reward = scores[(solution owner, tests owner)], read back from the hybrid task's files."""

    def run(dataset: Path) -> dict[str, float | None]:
        out = {}
        for d in dataset.iterdir():
            sol = (d / "solution" / "solve.sh").read_text().split()[1]
            tst = (d / "tests" / "test.sh").read_text().split()[1]
            out[d.name] = scores.get((sol, tst), 0.0)
        return out

    return run


def test_distinct_tasks(tmp_path: Path) -> None:
    tasks = [_task(tmp_path, n) for n in ("a", "b", "c")]
    m = run_matrix(tasks, tmp_path / "work", _fake_runner({(n, n): 1.0 for n in "abc"}))
    assert m["reward"] == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    assert m["distinct"] and not m["overlaps"] and not m["broken"]


def test_overlap_and_broken_diagonal(tmp_path: Path) -> None:
    tasks = [_task(tmp_path, n) for n in ("a", "b")]
    scores = {("a", "a"): 1.0, ("a", "b"): 0.75, ("b", "b"): None}
    m = run_matrix(tasks, tmp_path / "work", _fake_runner(scores))
    assert m["overlaps"] == [{"solution": "a", "tests": "b", "reward": 0.75}]
    assert m["broken"] == ["b"] and not m["distinct"]


def _trial(job: Path, i: int, path: str, vr: object) -> None:
    trial = job / f"trial{i}"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"task_id": {"path": path}, "verifier_result": vr}))


def test_read_rewards_uses_newest_job_only(tmp_path: Path) -> None:
    _trial(tmp_path / "2026-11-27__09-00-00", 0, "ds/x", None)  # stale crashed job
    new = tmp_path / "2026-11-27__10-00-00"
    _trial(new, 0, "ds/x", {"rewards": {"reward": 1.0}})
    _trial(new, 1, "ds/y", None)
    assert read_rewards(tmp_path) == {"x": 1.0, "y": None}
