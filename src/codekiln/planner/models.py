from dataclasses import dataclass
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

    @property
    def items(self) -> frozenset[str]:
        return self.files | frozenset(f"sym:{s}" for s in self.symbols)
