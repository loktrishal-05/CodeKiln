"""Orchestrator agent: the model picks WHICH allowed action runs next; code decides everything else.

The model sees only the pending (candidate, stage) pairs the state machine offers, and answers with
the index of one of them or "stop". Code enforces the rest: an answer outside the offered list, a
garbled reply, a provider failure or an exhausted budget falls back to the deterministic order and
is logged as such, the step limit lives in the caller, and the stage that runs is always the one the
ledger status dictates, so a gate (never the model) moves a candidate. The default (no chooser) is
the fixed order, which is what the factory does without `--agent`.
"""
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

from codekiln.billing.ledger import Budget, BudgetExceeded
from codekiln.synthesis.llm_router import ModelConfig, Post, RouterError, complete

STOP = -1

SYSTEM = """\
You are CodeKiln's orchestrator. You are given the actions the state machine currently allows, as a
JSON list. Choose the one to run next, to use limited gate time on the most informative candidate
first. Reply with JSON only: {"index": <position in the list>} or {"stop": true}. You cannot create
actions, change a candidate's state, or declare anything passed: gates do that.
"""


class ChooserError(RuntimeError):
    """The chooser could not produce a usable answer; the caller falls back to the default order."""


@dataclass(frozen=True)
class Pending:
    candidate_id: int
    status: str
    stage: str
    package: str
    category: str
    difficulty: str


@dataclass(frozen=True)
class Decision:
    candidate_id: int
    stage: str
    source: str  # "default" | "agent" | "fallback"
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


Chooser = Callable[[Sequence[Pending]], int]


def pick(chooser: Chooser | None, pending: Sequence[Pending]) -> tuple[Pending | None, Decision]:
    """Resolve the next action. Always returns something runnable unless the agent chose to stop."""
    first = pending[0]
    if chooser is None:
        return first, Decision(first.candidate_id, first.stage, "default")
    try:
        index = chooser(pending)
    except (ChooserError, BudgetExceeded) as e:
        return first, Decision(first.candidate_id, first.stage, "fallback", f"chooser failed: {e}")
    if index == STOP:
        return None, Decision(0, "stop", "agent", "the agent chose to stop")
    if not 0 <= index < len(pending):
        return first, Decision(
            first.candidate_id, first.stage, "fallback", f"index {index} is not an allowed action"
        )
    chosen = pending[index]
    return chosen, Decision(chosen.candidate_id, chosen.stage, "agent")


_JSON = re.compile(r"\{.*\}", re.DOTALL)


def parse_choice(text: str) -> int:
    """Strict: a JSON object with an integer `index`, or `stop: true`; anything else is an error."""
    m = _JSON.search(text)
    if not m:
        raise ChooserError("reply contains no JSON object")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ChooserError(f"reply is not valid JSON: {e.msg}") from e
    if not isinstance(data, dict):
        raise ChooserError("reply must be a JSON object")
    if data.get("stop") is True:
        return STOP
    index = data.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise ChooserError("reply has no integer `index`")
    return index


class LLMChooser:
    """Chooser backed by a chat model; every call is metered when a budget is given."""

    def __init__(self, config: ModelConfig, post: Post | None = None, budget: Budget | None = None) -> None:
        self._config, self._post, self._budget = config, post, budget

    def __call__(self, pending: Sequence[Pending]) -> int:
        if self._budget is not None:
            self._budget.guard()
        post = self._post
        if post is None:
            from codekiln.synthesis.llm_router import http_post

            post = http_post
        options = [
            {"index": i, "candidate": p.candidate_id, "stage": p.stage, "status": p.status,
             "category": p.category, "difficulty": p.difficulty}
            for i, p in enumerate(pending)
        ]
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(options)},
        ]
        try:
            reply = complete(self._config, messages, post)
        except RouterError as e:
            raise ChooserError(str(e)) from e
        return parse_choice(reply)
