"""Synthesizer contract: one `FeatureSpec` per candidate, checked before any file is written.

A writer (the LLM router later, a fixture in tests today) only produces a `FeatureSpec`.
`validate` enforces the shape and the no-deletion rule; `synthesis.rtm` enforces REQ/test
traceability. Whether the feature already exists upstream is proven by the gate (State B must
fail on the base commit), never by the writer's own claim.
"""
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REQ_ID = re.compile(r"REQ-\d+")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_TEST_FILE = re.compile(r"test_[a-z0-9_]+\.py")
# Cheap static guards. Real anti-stripping evidence is the gate: State A/B run every P2P test,
# so a package that deletes behaviour cannot pass even if this lint lets the diff through.
_ADDED = re.compile(r"^\+(?!\+\+)", re.MULTILINE)
_DELETED_FILE = re.compile(r"^deleted file mode ", re.MULTILINE)
_DIFF_HEADER = "diff --git "
_DIFF_PATH = re.compile(r"^diff --git a/\S+ b/(\S+)", re.MULTILINE)
# Feature tests ship separately in feature_tests; a reference diff must not touch the test setup.
_TEST_INFRA = re.compile(r"(?:^|/)(?:conftest\.py|pytest\.ini|tox\.ini|setup\.cfg)$|^tests/")


class SynthesisError(ValueError):
    pass


@dataclass(frozen=True)
class Requirement:
    """One line of the instruction's contract plus the F2P tests that prove it."""

    id: str
    statement: str
    tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeatureSpec:
    """Everything the packager and the gates need, before a single file is written."""

    task: str  # kebab-case, also the package dir name
    instruction: str  # the only file the evaluated agent sees
    reference_diff: str  # adds the feature to the base commit; never deletes upstream code
    feature_tests: Mapping[str, str]  # test file name -> source, hidden until grading
    f2p: tuple[str, ...]  # pytest node ids, e.g. tests/test_x.py::test_y
    requirements: tuple[Requirement, ...]
    category: str
    difficulty: str  # L1 / L2 / L3

    def to_dict(self) -> dict[str, object]:
        return {
            "task": self.task,
            "category": self.category,
            "difficulty": self.difficulty,
            "instruction": self.instruction,
            "reference_diff": self.reference_diff,
            "feature_tests": dict(self.feature_tests),
            "f2p": list(self.f2p),
            "requirements": [
                {"id": r.id, "statement": r.statement, "tests": list(r.tests)}
                for r in self.requirements
            ],
        }


def validate(spec: FeatureSpec) -> None:
    """Raise SynthesisError on anything the packager or the RTM audit would reject later."""
    if not _SLUG.fullmatch(spec.task):
        raise SynthesisError(f"task must be kebab-case, got {spec.task!r}")
    if bad := [n for n in spec.feature_tests if not _TEST_FILE.fullmatch(n)]:
        raise SynthesisError(f"feature test names must look like test_x.py: {bad}")
    if not spec.f2p:
        raise SynthesisError("a task needs at least one fail_to_pass test")
    if dupes := sorted({n for n in spec.f2p if spec.f2p.count(n) > 1}):
        raise SynthesisError(f"duplicate fail_to_pass ids: {dupes}")
    for node in spec.f2p:
        file = node.split("::")[0].removeprefix("tests/")
        if not node.startswith("tests/") or "::" not in node or file not in spec.feature_tests:
            raise SynthesisError(f"fail_to_pass id outside the feature tests: {node!r}")

    ids = [r.id for r in spec.requirements]
    if not ids:
        raise SynthesisError("a task needs at least one REQ-n requirement")
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise SynthesisError(f"duplicate requirement ids: {dupes}")
    if spec.difficulty not in ("L1", "L2", "L3"):
        raise SynthesisError(f"difficulty must be L1/L2/L3, got {spec.difficulty!r}")

    if not spec.reference_diff.startswith(_DIFF_HEADER):
        raise SynthesisError("reference_diff must be a git diff starting with 'diff --git '")
    if not _ADDED.search(spec.reference_diff):
        raise SynthesisError("reference_diff must add the feature (no added lines found)")
    if deleted := _DELETED_FILE.findall(spec.reference_diff):
        raise SynthesisError(f"feature tasks may not delete files ({len(deleted)} deletions)")

    if infra := sorted(p for p in _DIFF_PATH.findall(spec.reference_diff) if _TEST_INFRA.search(p)):
        raise SynthesisError(f"reference_diff touches test infrastructure: {infra}")

    declared = set(REQ_ID.findall(spec.instruction))
    if missing := sorted(set(ids) - declared):
        raise SynthesisError(f"instruction.md does not declare {missing}")
    if orphan := sorted(declared - set(ids)):
        raise SynthesisError(f"instruction.md has requirement ids with no Requirement: {orphan}")
    for r in spec.requirements:
        if not r.statement.strip():
            raise SynthesisError(f"{r.id} has an empty statement")


def from_dict(raw: Mapping[str, object]) -> FeatureSpec:
    """Parse the JSON a writer returns; every field is checked by type before use."""
    return FeatureSpec(
        task=_text(raw, "task"),
        category=_text(raw, "category"),
        difficulty=_text(raw, "difficulty"),
        instruction=_text(raw, "instruction"),
        reference_diff=_text(raw, "reference_diff"),
        feature_tests=_tests(raw),
        f2p=tuple(_texts(raw, "f2p")),
        requirements=_requirements(raw),
    )


def load_spec(path: Path) -> FeatureSpec:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SynthesisError(f"{path}: not valid JSON ({e.msg})") from e
    if not isinstance(data, dict):
        raise SynthesisError(f"{path}: expected a JSON object")
    return from_dict(data)


def _text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise SynthesisError(f"{key} must be a string")
    return value


def _texts(raw: Mapping[str, object], key: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SynthesisError(f"{key} must be a list of strings")
    return list(value)


def _tests(raw: Mapping[str, object]) -> dict[str, str]:
    value = raw.get("feature_tests")
    if not isinstance(value, dict):
        raise SynthesisError("feature_tests must map file names to source text")
    out: dict[str, str] = {}
    for name, source in value.items():
        if not isinstance(name, str) or not isinstance(source, str):
            raise SynthesisError("feature_tests must map file names to source text")
        out[name] = source
    return out


def _requirements(raw: Mapping[str, object]) -> tuple[Requirement, ...]:
    value = raw.get("requirements")
    if not isinstance(value, list):
        raise SynthesisError("requirements must be a list")
    out: list[Requirement] = []
    for item in value:
        if not isinstance(item, dict):
            raise SynthesisError("each requirement must be an object")
        out.append(Requirement(_text(item, "id"), _text(item, "statement"), tuple(_texts(item, "tests"))))
    return tuple(out)
