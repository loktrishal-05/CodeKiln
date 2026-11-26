"""reverse_extract, forward_synth, diversity_guard (Jaccard < 0.4)."""
from itertools import chain, zip_longest
from pathlib import Path

from codekiln.planner.diversity_guard import select
from codekiln.planner.forward_synth import forward_seeds
from codekiln.planner.models import Plan
from codekiln.planner.reverse_extract import reverse_plans


def plan(root: Path, slots: int) -> list[Plan]:
    """Dual-mode planning: interleave reverse and forward candidates, keep a diverse set."""
    mixed = chain.from_iterable(zip_longest(reverse_plans(root), forward_seeds(root)))
    return select([p for p in mixed if p], slots)
