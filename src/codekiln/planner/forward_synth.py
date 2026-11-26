"""Forward mode: category-targeted seeds for NEW features (an LLM later turns a seed into a spec).

Seeds are deterministic: a few modules per category plus cross-directory import edges for
CROSS_MODULE, so the 5-category taxonomy is covered even when reverse mode clusters.
"""
from pathlib import Path

from codekiln.planner.models import Category, Plan
from codekiln.planner.reverse_extract import classify, scan


def forward_seeds(root: Path, per_category: int = 2) -> list[Plan]:
    s = scan(root)
    out: list[Plan] = []
    taken: dict[Category, int] = {}
    for f, syms in sorted(s.symbols.items()):
        cat = classify(f)
        if not syms or Path(f).name == "__init__.py" or taken.get(cat, 0) >= per_category:
            continue
        taken[cat] = taken.get(cat, 0) + 1
        out.append(Plan(cat, frozenset({f}), syms, "forward", len(s.dependents(f, 1))))
    cross = 0
    for src, dsts in sorted(s.graph.items()):
        for dst in sorted(dsts):
            if cross < per_category and Path(src).parent != Path(dst).parent and s.symbols[dst]:
                cross += 1
                out.append(
                    Plan(Category.CROSS_MODULE, frozenset({src, dst}), s.symbols[dst], "forward")
                )
    return out
