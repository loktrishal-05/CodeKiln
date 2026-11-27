import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class Category(StrEnum):
    CORE_LOGIC = "core_logic"
    API_CLI = "api_cli"
    VALIDATION = "validation"
    PERSISTENCE = "persistence"
    CROSS_MODULE = "cross_module"


@dataclass(frozen=True)
class Plan:
    """A candidate feature slot: where it lives and what it touches. No LLM involved yet."""

    category: Category
    files: frozenset[str]
    symbols: frozenset[str]
    mode: Literal["reverse", "forward"]
    fanout: int = 0  # files that depend on the feature's module (blast radius hint)
    # Repo Scout text for the writer prompt: not part of the slot's identity, so it never changes
    # equality, the diversity guard or the ledger fingerprint.
    context: str = field(default="", compare=False, repr=False)
    # Checkout the writer's edits are applied to (so the reference diff is computed, not guessed).
    workspace: str = field(default="", compare=False, repr=False)

    @property
    def items(self) -> frozenset[str]:
        return self.files | frozenset(f"sym:{s}" for s in self.symbols)


def dump_plan(plan: Plan) -> str:
    """Persist the slot on task_candidates so `synth --run-id` works from a fresh process."""
    return json.dumps(
        {
            "category": plan.category.value,
            "files": sorted(plan.files),
            "symbols": sorted(plan.symbols),
            "mode": plan.mode,
            "fanout": plan.fanout,
        },
        sort_keys=True,
    )


def load_plan(text: str) -> Plan:
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"plan is not JSON: {e.msg}") from e
    if not isinstance(raw, Mapping):
        raise TypeError("plan must be a JSON object")
    category, files, symbols, mode, fanout = (
        raw.get("category"),
        raw.get("files"),
        raw.get("symbols"),
        raw.get("mode"),
        raw.get("fanout", 0),
    )
    if not isinstance(category, str) or mode not in ("reverse", "forward"):
        raise ValueError("plan has an invalid category or mode")
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ValueError("plan files must be a list of strings")
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise ValueError("plan symbols must be a list of strings")
    if not isinstance(fanout, int):
        raise TypeError("plan fanout must be an integer")
    try:
        cat = Category(category)
    except ValueError as e:
        raise ValueError(f"unknown plan category {category!r}") from e
    plan_mode: Literal["reverse", "forward"] = "reverse" if mode == "reverse" else "forward"
    return Plan(cat, frozenset(files), frozenset(symbols), plan_mode, fanout)
