"""Test Author + Test Critic (issue #45): a second correct implementation stresses the suite.

A model may author tests and may propose an alternative solution, but it never grades either. The
critic copies the package, swaps in the alternative patch and runs Harbor. A correct alternative
that scores below 1.0 is executable evidence that the F2P tests are too narrow; a malformed or
copied patch is rejected before the trial so it cannot masquerade as a failing test.
"""
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codekiln.synthesis.llm_router import ModelConfig, Post, complete
from codekiln.validation.mutation_probe import harbor_probe, patch_problem

AltRun = Callable[[Path, Path], float | None]  # (swapped task dir, -o dir) -> reward

ALTERNATIVE_SYSTEM = """\
You are CodeKiln's independent implementer. You are given an instruction.md and the reference
patch for context only. Write a DIFFERENT but correct implementation of the same feature as a
unified git diff. Keep all existing behaviour; do not delete or weaken code. Return only the raw
diff (no prose, no markdown fence). Never claim a test result.
"""


class CriticError(ValueError):
    pass


@dataclass(frozen=True)
class Alternative:
    name: str
    reward: float | None
    passed: bool
    reason: str  # empty when the alternative passed, otherwise the exact rejection reason

    @property
    def too_narrow(self) -> bool:
        return self.reward is not None and self.reward < 1.0


@dataclass(frozen=True)
class TestCritique:
    task: str
    alternatives: tuple[Alternative, ...]
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.reasons

    @property
    def too_narrow(self) -> bool:
        return any(a.too_narrow for a in self.alternatives)

    def to_json(self) -> str:
        return json.dumps(
            {
                "task": self.task,
                "ok": self.ok,
                "too_narrow": self.too_narrow,
                "alternatives": [
                    {
                        "name": a.name,
                        "reward": a.reward,
                        "passed": a.passed,
                        "too_narrow": a.too_narrow,
                        "reason": a.reason,
                    }
                    for a in self.alternatives
                ],
                "reasons": list(self.reasons),
            },
            indent=2,
        )


class AlternativeWriter(Protocol):
    """Anything that can propose a different correct patch; the critic does the grading."""

    def write(self, instruction: str, reference_diff: str) -> str: ...


_FENCE = re.compile(r"^```(?:diff|patch)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def clean_diff(text: str) -> str:
    """Accept a raw unified diff or a fenced one; reject prose and half-patches."""
    body = text.strip()
    if m := _FENCE.match(body):
        body = m.group(1).strip()
    if "diff --git " not in body or "@@ " not in body:
        raise CriticError("completion is not a unified git diff")
    if bad := patch_problem(body):
        raise CriticError(f"invalid patch: {bad}")
    return body.rstrip() + "\n"


class LLMAlternativeWriter:
    """Host-side proposer: one completion, bounded repair, no verdict of its own."""

    def __init__(self, config: ModelConfig, post: Post | None = None, retries: int = 1) -> None:
        self._config = config
        self._post = post
        self._retries = retries

    def write(self, instruction: str, reference_diff: str) -> str:
        messages = [
            {"role": "system", "content": ALTERNATIVE_SYSTEM},
            {
                "role": "user",
                "content": (
                    "# instruction.md\n" + instruction
                    + "\n\n# reference patch (context only — do not copy it)\n```diff\n"
                    + reference_diff + "```\n\nReturn a different correct implementation as a diff."
                ),
            },
        ]
        post = self._post
        if post is None:
            from codekiln.synthesis.llm_router import http_post

            post = http_post
        last = "no attempt made"
        for _ in range(self._retries + 1):
            reply = complete(self._config, messages, post)
            try:
                return clean_diff(reply)
            except CriticError as e:
                last = str(e)
                messages += [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": f"Rejected: {last}\nReturn only a valid raw diff."},
                ]
        raise CriticError(f"no valid alternative after {self._retries + 1} attempts: {last}")


def critique_tests(
    task: Path,
    work: Path,
    writer: AlternativeWriter,
    *,
    run: AltRun | None = None,
    alternatives: int = 1,
) -> TestCritique:
    """Execute each proposed alternative and report any that the F2P suite wrongly rejects."""
    if alternatives < 1:
        raise CriticError("at least one alternative implementation is required")
    instruction_path = task / "instruction.md"
    diff_path = task / "solution" / "reference.diff"
    f2p_path = task / "tests" / "fail_to_pass.json"
    if not all(p.is_file() for p in (instruction_path, diff_path, f2p_path)):
        raise CriticError(f"{task} is not a Harbor task package")
    f2p = json.loads(f2p_path.read_text(encoding="utf-8"))
    if not isinstance(f2p, list):
        raise CriticError("fail_to_pass.json must be a list")
    instruction = instruction_path.read_text(encoding="utf-8")
    reference = diff_path.read_text(encoding="utf-8")
    runner = run or harbor_probe
    results: list[Alternative] = []
    for index in range(alternatives):
        name = f"alt-{index}"
        reason = ""
        reward: float | None = None
        try:
            diff = writer.write(instruction, reference)
            diff = clean_diff(diff)
            if diff.strip() == reference.strip():
                raise CriticError("alternative copied the reference patch")
        except (CriticError, ValueError, OSError) as e:
            results.append(Alternative(name, None, False, f"invalid alternative: {e}"))
            continue
        dst = work / task.name / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(task, dst)
        (dst / "solution" / "reference.diff").write_text(diff, encoding="utf-8")
        reward = runner(dst, work / "jobs" / name)
        if reward is None:
            reason = "trial crashed; no executable verdict"
        elif reward < 1.0:
            reason = (
                f"tests too narrow: a correct alternative scored {reward:.2f} < 1.0"
                f" on {len(f2p)} F2P nodes"
            )
        results.append(Alternative(name, reward, reward == 1.0, reason))

    reasons = [a.reason for a in results if a.reason]
    if not any(a.passed or a.too_narrow for a in results):
        reasons.append("no valid alternative implementation could be executed")
    return TestCritique(task.name, tuple(results), tuple(reasons))


def write_report(path: Path, critique: TestCritique) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(critique.to_json(), encoding="utf-8")
    return path
