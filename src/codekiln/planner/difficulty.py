"""Goldilocks difficulty dial: L1 focused, L2 multi-file, L3 architectural, balanced across slots.

Level comes from AST fan-out: files touched, directories spanned, dependents affected.
"""
from itertools import zip_longest
from pathlib import Path

from codekiln.planner.models import Plan

LEVELS = ("L1", "L2", "L3")


def level(plan: Plan) -> str:
    dirs = len({Path(f).parent for f in plan.files})
    if len(plan.files) >= 5 or dirs >= 3 or plan.fanout >= 5:
        return "L3"
    if len(plan.files) >= 2:
        return "L2"
    return "L1"


def balance(plans: list[Plan], slots: int, mix: tuple[int, int, int] = (1, 1, 1)) -> list[Plan]:
    """Pick up to SLOTS plans following the L1:L2:L3 MIX; unfilled levels are topped up from the rest."""
    by_level = {lv: [p for p in plans if level(p) == lv] for lv in LEVELS}
    total = sum(mix)
    want = {lv: slots * m // total for lv, m in zip(LEVELS, mix, strict=True)}
    for lv in LEVELS[: slots - sum(want.values())]:  # remainder goes to the lowest levels
        want[lv] += 1
    picked = [p for lv in LEVELS for p in by_level[lv][: want[lv]]]
    tails = [by_level[lv][want[lv] :] for lv in LEVELS]
    rest = [p for row in zip_longest(*tails) for p in row if p]
    return picked + rest[: slots - len(picked)]
