"""Rasch (1PL) item-response fit: model ability and task difficulty from solved / not-solved trials.

P(model m solves task t) = sigmoid(ability[m] - difficulty[t]). Fit by alternating Newton steps on a
ridge-regularised log-likelihood, so it is deterministic, needs no libraries, handles missing trials
(crashes are simply absent), and stays finite for tasks everyone or nobody solved, where an
unregularised fit diverges.

Honest limits. Only difficulty and ability are identifiable: a 2PL *discrimination* per task needs
far more models than we evaluate, so it is not fit. And a Rasch fit is only meaningful with enough
models and tasks (`MIN_MODELS`, `MIN_TASKS`); below that callers must fall back to the coarse
solve-rate labelling. Difficulties are relative to this evaluation set (mean zero), not absolute.
"""
import math
from collections.abc import Mapping
from dataclasses import dataclass

MIN_MODELS = 4  # fewer evaluated models cannot separate ability from difficulty
MIN_TASKS = 5
RIDGE = 0.5  # a weak N(0, 1/ridge) prior: keeps all-solved / none-solved items finite


@dataclass(frozen=True)
class Fit:
    difficulty: Mapping[str, float]  # higher = harder; mean zero over tasks
    ability: Mapping[str, float]  # higher = stronger model


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def fit_rasch(solved: Mapping[tuple[str, str], bool], iterations: int = 200, ridge: float = RIDGE) -> Fit:
    """SOLVED maps (task, model) -> whether that model solved that task; absent pairs are unknown."""
    tasks = sorted({t for t, _ in solved})
    models = sorted({m for _, m in solved})
    b = dict.fromkeys(tasks, 0.0)
    theta = dict.fromkeys(models, 0.0)
    for _ in range(iterations):
        for t in tasks:
            g = h = 0.0
            for m in models:
                if (t, m) in solved:
                    p = _sigmoid(theta[m] - b[t])
                    g += p - solved[(t, m)]  # d logL / d difficulty
                    h -= p * (1 - p)
            b[t] -= (g - ridge * b[t]) / (h - ridge)
        for m in models:
            g = h = 0.0
            for t in tasks:
                if (t, m) in solved:
                    p = _sigmoid(theta[m] - b[t])
                    g += solved[(t, m)] - p
                    h -= p * (1 - p)
            theta[m] -= (g - ridge * theta[m]) / (h - ridge)
    centre = sum(b.values()) / len(b) if b else 0.0  # difficulty is only defined up to a shift
    return Fit({t: v - centre for t, v in b.items()}, {m: v - centre for m, v in theta.items()})


def sufficient(solved: Mapping[tuple[str, str], bool]) -> bool:
    return len({m for _, m in solved}) >= MIN_MODELS and len({t for t, _ in solved}) >= MIN_TASKS


def level_for(difficulty: float, spread: float) -> str:
    """Relative band within this evaluation set: below -0.5 sd easy, above +0.5 sd hard."""
    if difficulty < -0.5 * spread:
        return "L1"
    return "L3" if difficulty > 0.5 * spread else "L2"


def spread(fit: Fit) -> float:
    values = list(fit.difficulty.values())
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.0


def discrimination(solved: Mapping[tuple[str, str], bool]) -> dict[str, float | None]:
    """Item-rest correlation per task: do models that do well elsewhere solve this one?

    Near +1 the task separates strong from weak models; near 0 it tells them apart no better than
    chance; **negative** means stronger models fail it while weaker ones pass, which usually means
    the task (its tests or its instruction) is broken. None when there is no variance to correlate.
    """
    tasks = sorted({t for t, _ in solved})
    models = sorted({m for _, m in solved})
    out: dict[str, float | None] = {}
    for t in tasks:
        xs: list[float] = []
        ys: list[float] = []
        for m in models:
            if (t, m) not in solved:
                continue
            rest = [solved[(u, m)] for u in tasks if u != t and (u, m) in solved]
            if rest:
                xs.append(float(solved[(t, m)]))
                ys.append(sum(rest) / len(rest))
        out[t] = _pearson(xs, ys)
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)
