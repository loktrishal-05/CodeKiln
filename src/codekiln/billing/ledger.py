"""Append-only cost ledger and hard budget breaker for host-side LLM spend.

Four event types (docs/SPEC.md): BUDGET_ALLOCATION, PREAUTH_HOLD, LLM_DEBIT, PREAUTH_RELEASE.
Holds reserve an estimate before a provider call; the release + debit pair records the real usage
from the response. A candidate never sees a key or a provider: the CLI meters the writer's post.
"""
import csv
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

EVENTS = ("BUDGET_ALLOCATION", "PREAUTH_HOLD", "LLM_DEBIT", "PREAUTH_RELEASE")
Event = Literal["BUDGET_ALLOCATION", "PREAUTH_HOLD", "LLM_DEBIT", "PREAUTH_RELEASE"]
HEADER = (
    "ts", "run_id", "candidate_id", "event", "amount_usd", "model", "tokens_in", "tokens_out",
    "reference",
)


class BudgetExceeded(RuntimeError):
    """Hard stop: no further provider calls are allowed under the current allocation."""


@dataclass(frozen=True)
class Pricing:
    input_per_1k: float = 0.00015   # gpt-4o-mini defaults; override from Settings
    output_per_1k: float = 0.0006

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input_per_1k + tokens_out * self.output_per_1k) / 1000

    def estimate(self, payload: Mapping[str, object]) -> float:
        """Reserve from prompt text length and the requested output cap (4 chars ~= 1 token)."""
        raw_messages = payload.get("messages", [])
        messages = raw_messages if isinstance(raw_messages, list) else []
        chars = sum(
            len(str(m.get("content", ""))) for m in messages if isinstance(m, Mapping)
        )
        out_cap = payload.get("max_tokens", 0)
        return self.cost(chars // 4, int(out_cap) if isinstance(out_cap, int) else 0)


@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0

    @classmethod
    def from_response(cls, response: Mapping[str, object]) -> "Usage":
        raw = response.get("usage")
        if not isinstance(raw, Mapping):
            return cls()
        def count(key: str, legacy: str) -> int:
            value = raw.get(key, raw.get(legacy, 0))
            return value if isinstance(value, int) and value >= 0 else 0
        return cls(count("prompt_tokens", "input_tokens"), count("completion_tokens", "output_tokens"))


class BillingLedger:
    """CSV ledger; every write is one row, so an interrupted run keeps an exact spend record."""

    def __init__(self, path: Path, pricing: Pricing | None = None) -> None:
        self.path = path
        self.pricing = pricing or Pricing()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            with path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(HEADER)

    def _append(
        self,
        run_id: int,
        candidate_id: int,
        event: Event,
        amount: float,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        reference: str = "",
    ) -> None:
        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ("", run_id, candidate_id, event, f"{amount:.8f}", model, tokens_in, tokens_out,
                 reference)
            )

    def _sum(self, run_id: int, events: tuple[Event, ...]) -> float:
        total = 0.0
        for row in self.rows(run_id):
            if row["event"] in events:
                total += float(row["amount_usd"])
        return total

    def rows(self, run_id: int | None = None) -> list[dict[str, str]]:
        with self.path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return [r for r in rows if run_id is None or r["run_id"] == str(run_id)]

    def allocate(self, run_id: int, amount: float) -> float:
        """Idempotent: a resumed run keeps its original allocation even if flags differ."""
        existing = self._sum(run_id, ("BUDGET_ALLOCATION",))
        if existing:
            return existing
        if amount <= 0:
            raise ValueError("budget allocation must be positive")
        self._append(run_id, 0, "BUDGET_ALLOCATION", amount, "")
        return amount

    def held(self, run_id: int) -> float:
        return self._sum(run_id, ("PREAUTH_HOLD",)) - self._sum(run_id, ("PREAUTH_RELEASE",))

    def spent(self, run_id: int) -> float:
        return self._sum(run_id, ("LLM_DEBIT",))

    def remaining(self, run_id: int) -> float:
        return self._sum(run_id, ("BUDGET_ALLOCATION",)) - self.spent(run_id) - self.held(run_id)

    def hold(self, run_id: int, candidate_id: int, amount: float, reference: str = "") -> None:
        if amount < 0:
            raise ValueError("hold cannot be negative")
        if self.remaining(run_id) - amount < -1e-12:
            raise BudgetExceeded(
                f"run {run_id}: budget exhausted (spent {self.spent(run_id):.4f},"
                f" held {self.held(run_id):.4f})"
            )
        self._append(run_id, candidate_id, "PREAUTH_HOLD", amount, "", reference=reference)

    def release(self, run_id: int, candidate_id: int, amount: float, reference: str = "") -> None:
        self._append(run_id, candidate_id, "PREAUTH_RELEASE", amount, "", reference=reference)

    def debit(
        self, run_id: int, candidate_id: int, usage: Usage, model: str, reference: str = ""
    ) -> float:
        cost = self.pricing.cost(usage.tokens_in, usage.tokens_out)
        self._append(
            run_id, candidate_id, "LLM_DEBIT", cost, model, usage.tokens_in, usage.tokens_out,
            reference,
        )
        return cost


class Budget:
    """Run-level guard wired into the writer's HTTP post; the breaker trips before a call."""

    def __init__(
        self,
        ledger: BillingLedger,
        run_id: int,
        cap: float,
        model: str,
        pricing: Pricing | None = None,
    ) -> None:
        self.ledger = ledger
        self.run_id = run_id
        self.model = model
        self.pricing = pricing or ledger.pricing
        allocated = ledger.allocate(run_id, cap)
        self.cap = min(cap, allocated)
        self.candidate_id = 0

    def start_candidate(self, candidate_id: int) -> None:
        self.candidate_id = candidate_id

    def guard(self) -> None:
        if self.ledger.remaining(self.run_id) <= 1e-12:
            raise BudgetExceeded(f"run {self.run_id}: no budget remaining")

    def meter_post(
        self, post: Callable[[str, dict[str, str], dict[str, object]], Mapping[str, object]]
    ) -> Callable[[str, dict[str, str], dict[str, object]], Mapping[str, object]]:
        """Wrap an OpenAI-compatible post so every call is held, then debited for real usage."""
        def metered(url: str, headers: dict[str, str], payload: dict[str, object]) -> Mapping[str, object]:
            hold = self.pricing.estimate(payload)
            self.ledger.hold(self.run_id, self.candidate_id, hold, url)
            try:
                response = post(url, headers, payload)
            except BaseException:
                self.ledger.release(self.run_id, self.candidate_id, hold, url)
                raise
            usage = Usage.from_response(response)
            self.ledger.release(self.run_id, self.candidate_id, hold, url)
            self.ledger.debit(self.run_id, self.candidate_id, usage, self.model, url)
            return response
        return metered
