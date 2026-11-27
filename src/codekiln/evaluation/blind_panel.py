"""Blind solver panel + ambiguity report (issue #47).

Two or three agents solve from ``instruction.md`` alone. Code compares the *contractual* choices in
their diffs — added signatures, defaults, touched files and required literals — and reports where
they disagree as questions for the spec. Model confidence is never the judge: the comparison is
syntactic and deterministic, and Harbor still owns correctness.
"""
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from codekiln.evaluation.eval_runner import Trial, TrialRun, harbor_trial
from codekiln.synthesis.models import FeatureSpec, validate
from codekiln.synthesis.rtm import require_ok
from codekiln.validation.blind_reader import lint_package

_SIG = re.compile(r"^\+\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)
_DEFAULT = re.compile(r"\b(\w+)\s*=\s*([^,)\n]+)")
_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)
_LITERAL = re.compile(
    r"\b(?:True|False|None|-?\d+(?:\.\d+)?|'[^'\n]*'|\"[^\"\n]*\")\b"
)


class PanelError(ValueError):
    pass


@dataclass(frozen=True)
class SolverAnswer:
    model: str
    reward: float | None
    signatures: tuple[str, ...]
    defaults: tuple[str, ...]
    files: tuple[str, ...]
    literals: tuple[str, ...]


@dataclass(frozen=True)
class Ambiguity:
    topic: str
    question: str
    observations: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "topic": self.topic,
            "question": self.question,
            "observations": list(self.observations),
        }


@dataclass(frozen=True)
class AmbiguityReport:
    task: str
    models: tuple[str, ...]
    answers: tuple[SolverAnswer, ...]
    ambiguities: tuple[Ambiguity, ...]

    @property
    def ok(self) -> bool:
        return not self.ambiguities

    @property
    def feedback(self) -> str:
        """Text that goes back into the spec revision loop, one concrete question each."""
        return "\n".join(
            f"- {a.question} Evidence: {'; '.join(a.observations)}" for a in self.ambiguities
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "task": self.task,
                "ok": self.ok,
                "models": list(self.models),
                "answers": [
                    {
                        "model": a.model,
                        "reward": a.reward,
                        "signatures": list(a.signatures),
                        "defaults": list(a.defaults),
                        "files": list(a.files),
                        "literals": list(a.literals),
                    }
                    for a in self.answers
                ],
                "ambiguities": [a.to_json() for a in self.ambiguities],
                "spec_feedback": self.feedback,
            },
            indent=2,
        )


def _features(diff: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    # Private helpers are an implementation choice, not part of the contract the spec must pin down.
    signatures = tuple(sorted(
        f"{m.group(1)}({m.group(2).strip()})" for m in _SIG.finditer(diff)
        if not m.group(1).startswith("_")
    ))
    defaults = tuple(
        sorted(
            f"{m.group(1)}={m.group(2).strip()}"
            for m in _DEFAULT.finditer("\n".join(
                line[1:] for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            ))
        )
    )
    files = tuple(sorted({m.group(2) for m in _FILE.finditer(diff)}))
    literals = tuple(
        sorted({
            m.group(0)
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
            for m in _LITERAL.finditer(line[1:])
        })
    )
    return signatures, defaults, files, literals


_TOPICS: tuple[tuple[str, str, str], ...] = (
    ("signature", "signatures", "Which exact callable signature(s) must the feature expose?"),
    ("default", "defaults", "Which parameter defaults are part of the public contract?"),
    ("file", "files", "Which source file(s) is the feature allowed to touch?"),
    ("literal", "literals", "Which concrete values must the feature produce?"),
)


def run_panel(
    package: Path,
    models: Sequence[str],
    work: Path,
    run: TrialRun | None = None,
) -> AmbiguityReport:
    """Run each blind solver and report every contractual choice they disagree on."""
    names = tuple(models)
    if len(set(names)) < 2:
        raise PanelError("blind panel needs at least two distinct models")
    report = lint_package(package)
    if not report.ok:
        raise PanelError(f"{package.name}: blind reader failed: {'; '.join(report.problems)}")
    trial: Callable[[Path, str, Path], Trial] = run or harbor_trial
    answers: list[SolverAnswer] = []
    for model in names:
        result = trial(package, model, work / model / package.name)
        signatures, defaults, files, literals = _features(result.agent_diff)
        answers.append(
            SolverAnswer(model, result.reward, signatures, defaults, files, literals)
        )

    ambiguities: list[Ambiguity] = []
    # A solver that crashed or produced no diff has no features; comparing it would report every
    # topic as "ambiguous" when the truth is "no data".
    usable = [a for a in answers if a.reward is not None and (a.signatures or a.defaults or a.files or a.literals)]
    if len(usable) < 2:
        seen = tuple(f"{a.model}: reward={a.reward}, usable={a in usable}" for a in answers)
        ambiguities.append(Ambiguity(
            "insufficient-data", "Fewer than two solvers produced a comparable diff; rerun the panel.", seen,
        ))
        return AmbiguityReport(package.name, names, tuple(answers), tuple(ambiguities))
    for topic, field, question in _TOPICS:
        values = {
            a.model: tuple(getattr(a, field)) for a in usable
        }
        if len(set(values.values())) < 2:
            continue
        observations = tuple(
            f"{model}: {', '.join(v) if v else '(none)'}" for model, v in values.items()
        )
        ambiguities.append(Ambiguity(topic, question, observations))
    return AmbiguityReport(package.name, names, tuple(answers), tuple(ambiguities))


def write_report(path: Path, report: AmbiguityReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json(), encoding="utf-8", newline="\n")
    return path


def read_report(path: Path) -> AmbiguityReport:
    """Read a report back so the spec revision loop can consume it in a fresh process."""
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise PanelError("ambiguity report must be a JSON object")
    raw_models = data.get("models", [])
    raw_answers = data.get("answers", [])
    raw_ambiguities = data.get("ambiguities", [])
    if not all(isinstance(x, list) for x in (raw_models, raw_answers, raw_ambiguities)):
        raise PanelError("ambiguity report models/answers/ambiguities must be lists")

    def strings(raw: object, key: str) -> tuple[str, ...]:
        value = raw.get(key, []) if isinstance(raw, Mapping) else []
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    answers: list[SolverAnswer] = []
    for raw in raw_answers:
        if not isinstance(raw, Mapping):
            raise PanelError("ambiguity report answer must be an object")
        reward = raw.get("reward")
        answers.append(
            SolverAnswer(
                str(raw.get("model", "")),
                None if reward is None else float(reward),
                strings(raw, "signatures"), strings(raw, "defaults"),
                strings(raw, "files"), strings(raw, "literals"),
            )
        )
    ambiguities: list[Ambiguity] = []
    for raw in raw_ambiguities:
        if not isinstance(raw, Mapping):
            raise PanelError("ambiguity report ambiguity must be an object")
        ambiguities.append(
            Ambiguity(
                str(raw.get("topic", "")), str(raw.get("question", "")), strings(raw, "observations")
            )
        )
    task = str(data.get("task", path.parent.name))
    return AmbiguityReport(
        task, tuple(str(m) for m in raw_models), tuple(answers), tuple(ambiguities)
    )


def apply_feedback(spec: FeatureSpec, report: AmbiguityReport) -> FeatureSpec:
    """Append the panel's questions to the instruction, then re-run the static spec gates."""
    if report.ok:
        return spec
    questions = "\n".join(f"- {a.question}" for a in report.ambiguities)
    revised = replace(
        spec,
        instruction=spec.instruction.rstrip()
        + "\n\n## Clarifications from blind solvers\n" + questions + "\n",
    )
    validate(revised)
    require_ok(revised)
    return revised
