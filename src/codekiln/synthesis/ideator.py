"""Feature Ideator + Critic: N proposals per slot, deterministic admission (issue #44).

The model only proposes. ``critique`` applies the same static gates synthesis uses (validate, RTM,
blind reader), then keeps only proposals that are realistic for the slot and novel against the
single-writer baseline (proposal 0) and against earlier kept proposals. Execution correctness is
never decided here: the Harbor gate and mutation probes remain the judge.
"""
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from codekiln.planner.models import Plan
from codekiln.synthesis.llm_router import Writer
from codekiln.synthesis.models import FeatureSpec, SynthesisError, validate
from codekiln.synthesis.rtm import require_ok
from codekiln.validation.blind_reader import lint_instruction

_WORD = re.compile(r"[a-z0-9_]+")
DEFAULT_CANDIDATES = 3
DEFAULT_NOVELTY = 0.5  # reject Jaccard overlap above this against baseline / kept specs


@dataclass(frozen=True)
class Ideation:
    category: str
    baseline: str
    proposed: int
    kept: tuple[FeatureSpec, ...]
    rejected: tuple[tuple[str, str], ...]  # (task, reason)

    @property
    def ok(self) -> bool:
        return bool(self.kept)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "category": self.category,
                "baseline": self.baseline,
                "proposed": self.proposed,
                "kept": [s.to_dict() for s in self.kept],
                "rejected": [{"task": t, "reason": r} for t, r in self.rejected],
            },
            indent=2,
        )


def propose(
    plan: Plan, writer: Writer, *, n: int, repo: str, base_commit: str
) -> tuple[FeatureSpec, ...]:
    """Ask the writer for N candidates for one slot; proposal 0 is the single-writer baseline."""
    if n < 1:
        raise ValueError("at least one candidate per slot is required")
    return tuple(writer.write(plan, repo=repo, base_commit=base_commit) for _ in range(n))


def _tokens(spec: FeatureSpec) -> frozenset[str]:
    text = " ".join([spec.instruction, *(r.statement for r in spec.requirements), *spec.f2p])
    return frozenset(_WORD.findall(text.lower()))


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def critique(
    specs: Sequence[FeatureSpec], plan: Plan, *, novelty: float = DEFAULT_NOVELTY
) -> Ideation:
    """Gate each proposal with code; the first valid one is the baseline for novelty checks."""
    if not specs:
        return Ideation(plan.category.value, "", 0, (), ())
    kept: list[FeatureSpec] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    baseline_tokens = _tokens(specs[0])
    for index, spec in enumerate(specs):
        problems: list[str] = []
        try:
            validate(spec)
        except (SynthesisError, ValueError) as e:
            problems.append(f"validate: {e}")
        try:
            require_ok(spec)
        except (SynthesisError, ValueError) as e:
            problems.append(f"RTM: {e}")
        report = lint_instruction(spec.instruction)
        if not report.ok:
            problems.append("blind-reader: " + "; ".join(report.problems))
        if plan.symbols and not any(s in spec.instruction for s in plan.symbols):
            problems.append(f"slot has no planned symbol named in the instruction: {sorted(plan.symbols)[:3]}")
        if spec.task in seen:
            problems.append("duplicate task name among kept proposals")
        # Proposal 0 *is* the baseline; comparing it to itself would reject it by definition.
        score = 0.0 if index == 0 else _overlap(_tokens(spec), baseline_tokens)
        for other in kept:
            score = max(score, _overlap(_tokens(spec), _tokens(other)))
        if score > novelty:
            problems.append(f"too similar to baseline/kept (Jaccard {score:.2f} > {novelty:.2f})")
        if problems:
            rejected.append((spec.task, "; ".join(problems)))
            continue
        kept.append(spec)
        seen.add(spec.task)
    return Ideation(plan.category.value, specs[0].task, len(specs), tuple(kept), tuple(rejected))


def ideate(
    plan: Plan,
    writer: Writer,
    *,
    n: int = DEFAULT_CANDIDATES,
    repo: str,
    base_commit: str,
    novelty: float = DEFAULT_NOVELTY,
) -> Ideation:
    """Propose N specs for one slot and return only those a deterministic critic admits."""
    return critique(propose(plan, writer, n=n, repo=repo, base_commit=base_commit),
                    plan, novelty=novelty)
