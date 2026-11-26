"""AST/file-overlap dedupe: reject near-duplicate candidates (Jaccard >= 0.4), balance categories."""
import hashlib
from itertools import zip_longest

from codekiln.planner.models import Category, Plan

THRESHOLD = 0.4


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def is_diverse(plan: Plan, accepted: list[Plan], threshold: float = THRESHOLD) -> bool:
    return all(jaccard(plan.items, q.items) < threshold for q in accepted)


def fingerprint(plan: Plan) -> str:
    """Stable id for task_candidates.ast_fingerprint."""
    return hashlib.sha256("|".join(sorted(plan.items)).encode()).hexdigest()


def select(plans: list[Plan], slots: int, threshold: float = THRESHOLD) -> list[Plan]:
    """Round-robin across categories, taking each plan that is diverse from all accepted."""
    queues = [[p for p in plans if p.category is c] for c in Category]
    accepted: list[Plan] = []
    # ponytail: single pass; a plan skipped as non-diverse is not retried after later accepts
    for row in zip_longest(*queues):
        for p in row:
            if p and len(accepted) < slots and is_diverse(p, accepted, threshold):
                accepted.append(p)
    return accepted
