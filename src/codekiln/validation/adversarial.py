"""Model-written plausible-but-wrong mutants for the mutation probes (issue #46).

The model proposes a diff that *looks* like a reasonable implementation but is subtly wrong. Code
checks that it is a valid, distinct, near-reference patch before it joins the four deterministic
classes; Harbor then decides whether the F2P suite kills it. A survivor is recorded as a test gap,
never excused by the writer's claim that it was "tricky".
"""
import json
import re
from pathlib import Path
from typing import Protocol

from codekiln.synthesis.edits import EDITS_FORMAT, diff_from_reply, reference_context
from codekiln.synthesis.llm_router import ModelConfig, Post, complete
from codekiln.synthesis.models import SynthesisError
from codekiln.validation.mutation_probe import Mutant, patch_problem

ADVERSARIAL_SYSTEM = """\
You are CodeKiln's adversarial implementer. Given instruction.md and the reference patch, write a
PLAUSIBLE BUT WRONG implementation of the same feature: keep the same files, shape and style, but
introduce one subtle behavioural bug that the tests should catch. Do not delete tests, do not add
prose, and do not claim what the tests will do. Return only a raw unified git diff.
"""
_FENCE = re.compile(r"^```(?:diff|patch)?\s*\n(.*?)\n```\s*$", re.DOTALL)
MIN_OVERLAP = 0.3  # keep the mutant near the reference; skip wholesale rewrites


class AdversarialError(ValueError):
    pass


class AdversarialWriter(Protocol):
    def write(self, instruction: str, reference_diff: str) -> str: ...


def clean_diff(text: str) -> str:
    body = text.strip()
    if m := _FENCE.match(body):
        body = m.group(1).strip()
    if "diff --git " not in body or "@@ " not in body:
        raise AdversarialError("completion is not a unified git diff")
    if bad := patch_problem(body):
        raise AdversarialError(f"invalid patch: {bad}")
    return body.rstrip() + "\n"


class LLMAdversarialWriter:
    """Host-side proposer; validation and execution stay in code."""

    def __init__(self, config: ModelConfig, post: Post | None = None, retries: int = 1,
                 workspace: Path | None = None) -> None:
        self._config = config
        self._post = post
        self._retries = retries
        self._workspace = workspace  # the repo at the base commit: with it the model sends edits, code builds the diff

    def write(self, instruction: str, reference_diff: str) -> str:
        ws = self._workspace
        ask = ("Return one plausible but wrong implementation as edits.\n" + EDITS_FORMAT if ws
               else "Return one plausible but wrong unified diff.")
        messages = [
            {"role": "system", "content": ADVERSARIAL_SYSTEM},
            {
                "role": "user",
                "content": (
                    "# instruction.md\n" + instruction
                    + "\n\n# reference patch\n```diff\n" + reference_diff + "```\n\n"
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
            except (AdversarialError, SynthesisError) as e:
                last = str(e)
                messages += [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": f"Rejected: {last}\n"
                     + ("Reply with valid edits JSON only." if ws else "Return only a valid raw diff.")},
                ]
        raise AdversarialError(f"no valid mutant after {self._retries + 1} attempts: {last}")


def _added(diff: str) -> frozenset[str]:
    return frozenset(
        line[1:].strip()
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
    )


def _near_reference(diff: str, reference: str) -> float:
    added, expected = _added(diff), _added(reference)
    return len(added & expected) / len(added | expected) if added and expected else 0.0


def propose_mutants(
    instruction: str, reference_diff: str, writer: AdversarialWriter, *, n: int
) -> tuple[tuple[Mutant, ...], tuple[str, ...]]:
    """Ask for N mutants; admit only valid, distinct, near-reference diffs.

    Rejections are returned with exact reasons so a skipped mutant is visible in the report.
    """
    if n < 1:
        raise AdversarialError("at least one adversarial mutant is required")
    out: list[Mutant] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for index in range(n):
        name = f"adversarial-{index}"
        try:
            diff = clean_diff(writer.write(instruction, reference_diff))
            if diff.strip() == reference_diff.strip():
                raise AdversarialError("mutant copied the reference patch")
            score = _near_reference(diff, reference_diff)
            if score < MIN_OVERLAP:
                raise AdversarialError(f"mutant is not near the reference (overlap {score:.2f})")
            if diff in seen:
                raise AdversarialError("duplicate mutant")
        except (AdversarialError, ValueError, OSError) as e:
            rejected.append(f"{name}: {e}")
            continue
        seen.add(diff)
        out.append(Mutant(name, "adversarial", diff))
    return tuple(out), tuple(rejected)


def report_rejected(rejected: tuple[str, ...], path: Path) -> None:
    """Persist rejected proposals so a skipped mutant is visible, not silently dropped."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(rejected), indent=2), encoding="utf-8", newline="\n")
