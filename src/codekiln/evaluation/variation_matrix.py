"""Cross-task oracle matrix: proves the tasks ask for different behaviour, by execution.

reward[i][j] = task j's tests run against task i's reference solution (a Harbor oracle trial of
task j with task i's solution/). Distinct tasks: 1.0 on the diagonal, ~0.0 off it. File/AST
Jaccard only shows tasks touch different code; this shows one feature does not satisfy another.
"""
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

Runner = Callable[[Path], dict[str, float | None]]  # dataset dir -> {task dir name: reward}
OVERLAP = 0.5  # off-diagonal reward above this = the two tasks overlap (docs/SPEC.md)


def run_matrix(tasks: list[Path], work: Path, runner: Runner) -> dict[str, Any]:
    dataset = work / "matrix_dataset"
    shutil.rmtree(dataset, ignore_errors=True)
    for i, sol in enumerate(tasks):
        for j, tst in enumerate(tasks):
            d = dataset / f"s{i}-t{j}"
            shutil.copytree(tst, d)
            shutil.rmtree(d / "solution")
            shutil.copytree(sol / "solution", d / "solution")
    got = runner(dataset)
    n, names = len(tasks), [t.name for t in tasks]
    reward = [[got.get(f"s{i}-t{j}") for j in range(n)] for i in range(n)]
    overlaps = [
        {"solution": names[i], "tests": names[j], "reward": r}
        for i in range(n)
        for j in range(n)
        if i != j and (r := reward[i][j]) is not None and r > OVERLAP
    ]
    broken = [names[i] for i in range(n) if reward[i][i] != 1.0]
    crashed = [f"s{i}-t{j}" for i in range(n) for j in range(n) if reward[i][j] is None]
    return {
        "tasks": names,
        "reward": reward,
        "overlap_threshold": OVERLAP,
        "overlaps": overlaps,
        "broken": broken,
        "crashed": crashed,
        "distinct": not overlaps and not broken and not crashed,
    }
