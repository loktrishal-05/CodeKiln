"""Difficulty calibration: compare the planner's L1/L2/L3 guess with what real models achieved.

The dial's level comes from AST fan-out (a heuristic). After an eval, each task has a measured solve
rate across the evaluated models, and that is the better evidence. This module labels each task by
what happened, records heuristic vs observed, and learns a small remap (heuristic level -> the level
tasks of that kind really turned out to be) that the planner can apply.

Two methods. With >= irt.MIN_MODELS models and >= irt.MIN_TASKS tasks it fits a Rasch model: a
graded difficulty scale and stays correct when not every model ran every task (a crash, a model
added late): 1 of 2 strong models solving a task is harder than 1 of 2 weak models solving another.
With complete data it ranks tasks by how many models solved them, like the solve rate does. Below that it uses the coarse solve rate (everyone solved = L1, nobody = L3, otherwise L2), which
is all 2-3 models can support. Discrimination is the item-rest correlation (not a 2PL fit, which
needs far more models); a negative value flags a task that stronger models fail and weaker ones
pass, which usually means the task itself is broken.
The remap is only learned from levels with enough samples, and a crashed trial is never counted as
"unsolved".
"""
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from codekiln.evaluation import irt
from codekiln.evaluation.eval_runner import EvalReceipt

MIN_MODELS = 2  # fewer valid trials than this say nothing about difficulty
MIN_SAMPLES = 3  # tasks of one heuristic level needed before a remap is trusted


@dataclass(frozen=True)
class Calibration:
    task: str
    solved: int
    models: int  # models with a valid (non-crashed) trial
    observed: str
    heuristic: str = ""
    difficulty: float | None = None  # Rasch difficulty (relative, mean 0) when the fit was possible
    discrimination: float | None = None  # item-rest correlation; negative = stronger models fail it

    @property
    def inverted(self) -> bool:
        """Stronger models do worse than weaker ones here: suspect the task, not the models."""
        return self.discrimination is not None and self.discrimination < 0

    @property
    def solve_rate(self) -> float:
        return self.solved / self.models

    @property
    def discriminates(self) -> bool:
        return 0 < self.solved < self.models  # all-or-nothing separates no models


def observed_level(solved: int, models: int) -> str:
    if solved == models:
        return "L1"
    return "L3" if solved == 0 else "L2"


def calibrate(receipt: EvalReceipt, heuristics: Mapping[str, str] | None = None) -> tuple[Calibration, ...]:
    """One Calibration per task that has at least MIN_MODELS valid trials; crashes are excluded.

    With enough models and tasks (irt.sufficient) the level comes from a Rasch fit, which also
    accounts for how strong the models that failed were; otherwise from the plain solve rate.
    """
    valid_rows = [r for r in receipt.rows if r.reward is not None]
    matrix = {(r.task, r.model_id): r.resolved for r in valid_rows}
    fit = irt.fit_rasch(matrix) if irt.sufficient(matrix) else None
    sd = irt.spread(fit) if fit else 0.0
    disc = irt.discrimination(matrix) if irt.sufficient(matrix) else {}
    out: list[Calibration] = []
    for task in receipt.tasks:
        valid = [r for r in valid_rows if r.task == task]
        if len(valid) < MIN_MODELS:
            continue
        solved = sum(r.resolved for r in valid)
        b = fit.difficulty.get(task) if fit else None
        observed = irt.level_for(b, sd) if b is not None else observed_level(solved, len(valid))
        out.append(Calibration(task, solved, len(valid), observed, (heuristics or {}).get(task, ""), b, disc.get(task)))
    return tuple(out)


def learn_remap(calibrations: tuple[Calibration, ...], min_samples: int = MIN_SAMPLES) -> dict[str, str]:
    """heuristic level -> the level those tasks mostly turned out to be (only where it differs)."""
    by_heuristic: dict[str, list[str]] = {}
    for c in calibrations:
        if c.heuristic:
            by_heuristic.setdefault(c.heuristic, []).append(c.observed)
    remap: dict[str, str] = {}
    for heuristic, observed in by_heuristic.items():
        level, count = Counter(observed).most_common(1)[0]
        if len(observed) >= min_samples and count * 2 > len(observed) and level != heuristic:
            remap[heuristic] = level
    return remap


def write_calibration(path: Path, calibrations: tuple[Calibration, ...], remap: Mapping[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {
            "remap": dict(remap),
            "tasks": [
                {"task": c.task, "heuristic": c.heuristic, "observed": c.observed, "solved": c.solved,
                 "models": c.models, "solve_rate": round(c.solve_rate, 3), "discriminates": c.discriminates,
                 "difficulty": None if c.difficulty is None else round(c.difficulty, 3),
                 "discrimination": None if c.discrimination is None else round(c.discrimination, 3),
                 "inverted": c.inverted}
                for c in calibrations
            ],
        },
        indent=2,
    ), encoding="utf-8", newline="\n")
    return path


def read_remap(path: Path) -> dict[str, str]:
    """The remap from a calibration file; anything not an L1/L2/L3 -> L1/L2/L3 pair is rejected."""
    data: object = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("remap", {}) if isinstance(data, dict) else {}
    levels = {"L1", "L2", "L3"}
    if not isinstance(raw, dict) or any(k not in levels or v not in levels for k, v in raw.items()):
        raise ValueError(f"{path}: remap must map L1/L2/L3 to L1/L2/L3")
    return {str(k): str(v) for k, v in raw.items()}
