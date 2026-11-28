"""Test Author + Test Critic (issue #45): a second correct implementation stresses the suite.

A model may author tests and may propose an alternative solution, but it never grades either. The
critic copies the package, swaps in the alternative patch and runs Harbor. An alternative that
scores 1.0 is real evidence the tests accept more than one implementation. One that scores below
1.0 is only a *signal*: the tests may be too narrow, or the alternative may simply be wrong (its
correctness is the model's claim, not a measurement), so a person reviews it and nothing is gated
on it automatically. A malformed or copied patch is rejected before the trial so it cannot
masquerade as a failing test.
"""
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codekiln.synthesis.edits import EDITS_FORMAT, diff_from_reply, reference_context
from codekiln.synthesis.llm_router import ModelConfig, Post, complete
from codekiln.synthesis.models import SynthesisError
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
    broke_existing: tuple[str, ...] = ()  # existing tests it broke while passing every feature test

    @property
    def too_narrow(self) -> bool:
        # An alternative that passes every feature test and only breaks click's own tests says nothing
        # about the feature tests: the alternative is wrong, and the regression guard did its job.
        return self.reward is not None and self.reward < 1.0 and not self.broke_existing


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
                        "broke_existing": list(a.broke_existing),
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

    def __init__(self, config: ModelConfig, post: Post | None = None, retries: int = 1,
                 workspace: Path | None = None) -> None:
        self._config = config
        self._post = post
        self._retries = retries
        self._workspace = workspace  # the repo at the base commit: with it the model sends edits, code builds the diff

    def write(self, instruction: str, reference_diff: str) -> str:
        ws = self._workspace
        ask = ("Return a different correct implementation as edits.\n" + EDITS_FORMAT if ws
               else "Return a different correct implementation as a diff.")
        messages = [
            {"role": "system", "content": ALTERNATIVE_SYSTEM},
            {
                "role": "user",
                "content": (
                    "# instruction.md\n" + instruction
                    + "\n\n# reference patch (context only — do not copy it)\n```diff\n"
                    + reference_diff + "```\n\n"
                    + (f"# context pack\n{reference_context(ws, reference_diff)}\n\n" if ws else "") + ask
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
                return clean_diff(diff_from_reply(reply, ws) if ws else reply)
            except (CriticError, SynthesisError) as e:
                last = str(e)
                messages += [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": f"Rejected: {last}\n"
                     + ("Reply with valid edits JSON only." if ws else "Return only a valid raw diff.")},
                ]
        raise CriticError(f"no valid alternative after {self._retries + 1} attempts: {last}")


def _failed_nodes(out: Path, f2p: set[str]) -> tuple[list[str], list[str]]:
    """(failing feature tests, failing other tests) from the trial's per-test results, ([], []) if unreadable."""
    found = sorted(out.rglob("verifier/results.json")) if out.is_dir() else []
    if not found:
        return [], []
    try:
        results = json.loads(found[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    bad = sorted(n for n, status in results.items() if status not in ("passed", "skipped")) if isinstance(results, dict) else []
    return [n for n in bad if n in f2p], [n for n in bad if n not in f2p]


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
        broke: tuple[str, ...] = ()
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
        (dst / "solution" / "reference.diff").write_text(diff, encoding="utf-8", newline="\n")
        reward = runner(dst, work / "jobs" / name)
        if reward is None:
            reason = "trial crashed; no executable verdict"
        elif reward < 1.0:
            # The alternative's correctness is the model's claim, not a measurement: a buggy patch
            # scores the same. So this is a signal for a person to check, not a verdict. The per-test
            # results say which kind of failure it was.
            failed_f2p, failed_other = _failed_nodes(work / "jobs" / name, set(map(str, f2p)))
            if failed_other and not failed_f2p:
                broke = tuple(failed_other[:5])
                reason = (f"the alternative passes every feature test but breaks existing test(s) {', '.join(broke)}:"
                          f" the alternative is wrong, not the tests")
            else:
                shown = f" ({', '.join(failed_f2p[:5])})" if failed_f2p else ""
                reason = (
                    f"tests too narrow (or the alternative is wrong; review the failing F2P nodes):"
                    f" the alternative scored {reward:.2f} < 1.0, failing {len(failed_f2p) or len(f2p)} F2P nodes{shown}"
                )
        results.append(Alternative(name, reward, reward == 1.0, reason, broke))

    reasons = [a.reason for a in results if a.reason]
    if not any(a.passed or a.too_narrow or a.broke_existing for a in results):
        reasons.append("no valid alternative implementation could be executed")
    return TestCritique(task.name, tuple(results), tuple(reasons))


def write_report(path: Path, critique: TestCritique) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(critique.to_json(), encoding="utf-8", newline="\n")
    return path
