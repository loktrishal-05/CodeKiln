"""Difficulty calibration: compare the planner's L1/L2/L3 guess with what real models achieved.

The dial's level comes from AST fan-out (a heuristic). After an eval, each task has a measured solve
rate across the evaluated models, and that is the better evidence. This module labels each task by
what happened, records heuristic vs observed, and learns a small remap (heuristic level -> the level
tasks of that kind really turned out to be) that the planner can apply.

Honest limits: with two or three models a 2PL/IRT fit is not identifiable, so the observed level is
coarse (everyone solved = L1, nobody = L3, otherwise L2) and says nothing about *how* hard beyond
that. The remap is only learned from levels with enough samples, and a trial that crashed is never
counted as "unsolved". More models or repeated trials sharpen it; the structure stays the same.
"""
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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
    """One Calibration per task that has at least MIN_MODELS valid trials; crashes are excluded."""
    out: list[Calibration] = []
    for task in receipt.tasks:
        valid = [r for r in receipt.rows if r.task == task and r.reward is not None]
        if len(valid) < MIN_MODELS:
            continue
        solved = sum(r.resolved for r in valid)
        out.append(Calibration(task, solved, len(valid), observed_level(solved, len(valid)),
                               (heuristics or {}).get(task, "")))
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
                 "models": c.models, "solve_rate": round(c.solve_rate, 3), "discriminates": c.discriminates}
                for c in calibrations
            ],
        },
        indent=2,
    ), encoding="utf-8")
    return path


def read_remap(path: Path) -> dict[str, str]:
    """The remap from a calibration file; anything not an L1/L2/L3 -> L1/L2/L3 pair is rejected."""
    data: object = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("remap", {}) if isinstance(data, dict) else {}
    levels = {"L1", "L2", "L3"}
    if not isinstance(raw, dict) or any(k not in levels or v not in levels for k, v in raw.items()):
        raise ValueError(f"{path}: remap must map L1/L2/L3 to L1/L2/L3")
    return {str(k): str(v) for k, v in raw.items()}
