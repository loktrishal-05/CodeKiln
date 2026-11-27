"""Dry-run blind reader: the instruction must stand alone without leaking the grading surface.

No model call yet: these are the deterministic checks a blind reader would flag anyway. The agent
sees instruction.md only, so any reference to hidden tests, the oracle patch or host paths is a
leak. `BLIND_READER_OK` is granted by this linter, never by a writer's own claim.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from codekiln.synthesis.models import REQ_ID

_LEAKS = (
    "tests/feature",
    "fail_to_pass",
    "pass_to_pass",
    "reference.diff",
    "solution/",
    "tests/reward.py",
    "generation_receipt",
    "oracle",
    "gold patch",
)
_HOST_PATHS = (r"[A-Za-z]:[\\/]", r"/Users/", r"/home/", r"/tmp/")
_SECRETS = (r"sk-[A-Za-z0-9]{8,}", r"ghp_[A-Za-z0-9]{8,}", r"AKIA[0-9A-Z]{8,}")
_SYMBOL = re.compile(r"`[^`\n]+`")


class BlindReaderError(ValueError):
    pass


@dataclass(frozen=True)
class BlindReport:
    task: str
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> str:
        return json.dumps({"task": self.task, "ok": self.ok, "problems": list(self.problems)}, indent=2)


def lint_instruction(instruction: str) -> BlindReport:
    """Check the only file the evaluated agent sees, without seeing the tests or the patch."""
    problems: list[str] = []
    if len(instruction.strip()) < 40:
        problems.append("instruction.md is too short to specify a feature")
    if not instruction.lstrip().startswith("#"):
        problems.append("instruction.md needs a title heading")
    if not REQ_ID.search(instruction):
        problems.append("instruction.md declares no REQ-n requirement")
    if not _SYMBOL.search(instruction):
        problems.append("instruction.md names no explicit API symbol in backticks")

    low = instruction.lower()
    for leak in _LEAKS:
        if leak in low:
            problems.append(f"instruction.md leaks the grading surface: {leak}")
    for pattern in (*_HOST_PATHS, *_SECRETS):
        if re.search(pattern, instruction):
            problems.append(f"instruction.md contains a host path or secret matching {pattern}")
    return BlindReport("instruction.md", tuple(problems))


def lint_package(task: Path) -> BlindReport:
    p = task / "instruction.md"
    if not p.is_file():
        return BlindReport(task.name, (f"missing {p.name}",))
    report = lint_instruction(p.read_text(encoding="utf-8"))
    return BlindReport(task.name, report.problems)


def require_ok(instruction: str) -> BlindReport:
    report = lint_instruction(instruction)
    if not report.ok:
        raise BlindReaderError("blind reader rejected: " + "; ".join(report.problems))
    return report
