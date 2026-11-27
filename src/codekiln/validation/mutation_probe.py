"""Four-class mutation probes: does the F2P suite actually detect wrong implementations?

Each mutant is run as a real Harbor trial with a swapped ``solution/``; a mutant is killed when its
reward is below 1.0. No-Op and Hardcoded Stub must be killed 100%, semantic mutants at least
``mutant_kill_threshold`` (0.8). The grader is execution, not a claim by the writer.
"""
import json
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from codekiln.validation import harbor_runner

Kind = Literal["nop", "stub", "partial", "semantic"]
ProbeRun = Callable[[Path, Path], float | None]  # (swapped task dir, -o dir) -> reward

_NOOP = (
    "diff --git a/.codekiln_noop b/.codekiln_noop\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/.codekiln_noop\n"
    "@@ -0,0 +1 @@\n"
    "+noop\n"
)
_HUNK_RE = re.compile(r"^(@@ -\d+(?:,\d+)? )\+(?:\d+)(?:,\d+)?( @@.*)$")
_DEF_RE = re.compile(r"^\+(\s*)(?:async\s+)?def\s+(\w+)\(")
_KINDS: tuple[Kind, ...] = ("nop", "stub", "partial", "semantic")
_SEMANTIC = (
    ("flip-true", "True", "False"),
    ("flip-false", "False", "True"),
    ("flip-eq", "==", "!="),
    ("flip-neq", "!=", "=="),
    ("bump-one", "1", "2"),
    ("flip-zero", "0", "1"),
    ("flip-and", " and ", " or "),
    ("flip-lt", " < ", " > "),
)


class ProbeError(ValueError):
    pass


@dataclass(frozen=True)
class Mutant:
    name: str
    kind: Kind
    diff: str


@dataclass(frozen=True)
class ProbeResult:
    name: str
    kind: Kind
    killed: bool
    reward: float | None


@dataclass(frozen=True)
class Dossier:
    task: str
    probes: tuple[ProbeResult, ...]
    semantic_kill_rate: float
    threshold: float
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.reasons

    def to_json(self) -> str:
        return json.dumps(
            {
                "task": self.task,
                "ok": self.ok,
                "semantic_kill_rate": self.semantic_kill_rate,
                "threshold": self.threshold,
                "reasons": list(self.reasons),
                "probes": [
                    {"name": p.name, "kind": p.kind, "killed": p.killed, "reward": p.reward}
                    for p in self.probes
                ],
            },
            indent=2,
        )


@dataclass(frozen=True)
class _Hunk:
    header: str
    lines: tuple[str, ...]


def build_mutants(reference_diff: str) -> list[Mutant]:
    """No-Op, Hardcoded Stub, Partial-Impl and up to five small semantic mutants."""
    prefix, hunks = _parse(reference_diff)
    out = [
        Mutant("noop", "nop", _NOOP),
        Mutant("stub", "stub", _stub(prefix, hunks) or _NOOP),
        Mutant("partial", "partial", _partial(prefix, hunks) or _NOOP),
    ]
    for name, diff in _semantics(prefix, hunks):
        out.append(Mutant(f"semantic-{name}", "semantic", diff))
    return out


def probe_package(
    task: Path,
    work: Path,
    run: ProbeRun | None = None,
    threshold: float = 0.8,
) -> Dossier:
    """Run every mutant against a copy of TASK and record the kill rate."""
    diff_file = task / "solution" / "reference.diff"
    if not diff_file.is_file() or not (task / "task.toml").is_file():
        raise ProbeError(f"{task} is not a Harbor task package")
    runner = run or harbor_probe
    results: list[ProbeResult] = []
    for m in build_mutants(diff_file.read_text(encoding="utf-8")):
        dst = work / task.name / m.name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(task, dst)
        (dst / "solution" / "reference.diff").write_text(m.diff, encoding="utf-8")
        reward = runner(dst, work / "jobs" / m.name)
        results.append(ProbeResult(m.name, m.kind, reward is not None and reward < 1.0, reward))
    return _dossier(task.name, tuple(results), threshold)


def harbor_probe(task: Path, out: Path) -> float | None:
    """Oracle agent on the mutated reference diff: reward 1.0 means the mutant survived."""
    return harbor_runner.run(task, out, "oracle").get(task.name)


def write_dossier(path: Path, dossier: Dossier) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dossier.to_json(), encoding="utf-8")
    return path


def read_dossier(path: Path) -> Dossier:
    data: object = json.loads(path.read_text(encoding="utf-8"))
    return dossier_from_mapping(data, default_task=path.parent.name)


def dossier_from_mapping(data: object, *, default_task: str = "task") -> Dossier:
    """Read a dossier back (also used by the report and by tests)."""
    if not isinstance(data, Mapping):
        raise ProbeError("mutation dossier must be a JSON object")
    probes: list[ProbeResult] = []
    raw_probes = data.get("probes", [])
    if not isinstance(raw_probes, list):
        raise ProbeError("mutation dossier probes must be a list")
    for raw in raw_probes:
        if not isinstance(raw, Mapping):
            raise ProbeError("mutation probe must be an object")
        kind = raw.get("kind")
        reward = raw.get("reward")
        if kind not in _KINDS or (reward is not None and not isinstance(reward, (int, float))):
            raise ProbeError("mutation probe has an invalid kind or reward")
        probes.append(
            ProbeResult(
                str(raw.get("name", "")),
                cast(Kind, kind),
                bool(raw.get("killed")),
                None if reward is None else float(reward),
            )
        )
    threshold = data.get("threshold", 0.8)
    if not isinstance(threshold, (int, float)):
        raise ProbeError("mutation threshold must be a number")
    return _dossier(str(data.get("task", default_task)), tuple(probes), float(threshold))


def _dossier(task: str, probes: tuple[ProbeResult, ...], threshold: float) -> Dossier:
    semantic = [p for p in probes if p.kind == "semantic"]
    rate = sum(p.killed for p in semantic) / len(semantic) if semantic else 0.0
    reasons: list[str] = []
    for kind in ("nop", "stub", "partial"):
        fixed = [p for p in probes if p.kind == kind]
        if not fixed or not all(p.killed for p in fixed):
            reasons.append(f"{kind} mutant survived")
    if len(semantic) < 3:
        reasons.append(f"only {len(semantic)} semantic mutants could be built (need >= 3)")
    if rate < threshold:
        reasons.append(f"semantic kill rate {rate:.0%} below {threshold:.0%}")
    if crashed := [p.name for p in probes if p.reward is None]:
        reasons.append(f"trials crashed: {crashed}")
    return Dossier(task, probes, rate, threshold, tuple(reasons))


def _parse(diff: str) -> tuple[list[str], list[_Hunk]]:
    prefix: list[str] = []
    hunks: list[_Hunk] = []
    for line in diff.splitlines():
        if line.startswith("@@ "):
            hunks.append(_Hunk(line, ()))
        elif hunks:
            hunks[-1] = _Hunk(hunks[-1].header, (*hunks[-1].lines, line))
        else:
            prefix.append(line)
    return prefix, hunks


def _dump(prefix: list[str], hunks: list[_Hunk]) -> str:
    lines = [*prefix, *(line for h in hunks for line in (h.header, *h.lines))]
    return "\n".join(lines) + "\n"


def _added(hunk: _Hunk) -> tuple[int, ...]:
    return tuple(i for i, line in enumerate(hunk.lines) if line.startswith("+") and not line.startswith("+++"))


def _recount(hunk: _Hunk) -> _Hunk:
    """Keep the `+` start line; only the added-line count changes when we rewrite a hunk."""
    m = _HUNK_RE.match(hunk.header)
    if not m:
        return hunk
    return _Hunk(f"{m.group(1)}+{len(_added(hunk))}{m.group(2)}", hunk.lines)


def _stub(prefix: list[str], hunks: list[_Hunk]) -> str:
    """Keep an added function's signature, replace its added body with `return None`."""
    out: list[_Hunk] = []
    replaced = False
    for h in hunks:
        lines: list[str] = []
        i = 0
        while i < len(h.lines):
            line = h.lines[i]
            m = _DEF_RE.match(line)
            if not m:
                lines.append(line)
                i += 1
                continue
            replaced = True
            lines.append(line)
            indent = len(m.group(1))
            i += 1
            while i < len(h.lines) and h.lines[i].startswith("+") and not h.lines[i].startswith("+++"):
                body = h.lines[i][1:]
                if not body.strip() or len(body) - len(body.lstrip()) > indent:
                    i += 1  # added function body: drop it
                    continue
                break
            lines.append("+" + " " * (indent + 4) + "return None")
        out.append(_recount(_Hunk(h.header, tuple(lines))))
    return _dump(prefix, out) if replaced else ""


def _partial(prefix: list[str], hunks: list[_Hunk]) -> str:
    """Implement only the first hunk, or neuter one added statement in a single-hunk diff."""
    if len(hunks) > 1:
        return _dump(prefix, hunks[:1])
    if not hunks:
        return ""
    h = hunks[0]
    added = [i for i in _added(h) if h.lines[i][1:].strip()]
    if not added:
        return ""
    i = added[-1]
    body = h.lines[i][1:]
    indent = body[: len(body) - len(body.lstrip())]
    # Preserve syntax: an indented statement becomes a bare return; top-level becomes pass.
    placeholder = f"+{indent}{'return None' if indent else 'pass'}"
    h = _Hunk(h.header, (*h.lines[:i], placeholder, *h.lines[i + 1 :]))
    return _dump(prefix, [_recount(h)])


def _semantics(prefix: list[str], hunks: list[_Hunk]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name, old, new in _SEMANTIC:
        if len(out) >= 5:
            break
        for hi, h in enumerate(hunks):
            for li in _added(h):
                if old in h.lines[li]:
                    changed = list(h.lines)
                    changed[li] = changed[li].replace(old, new, 1)
                    copy = [*hunks]
                    copy[hi] = _recount(_Hunk(h.header, tuple(changed)))
                    out.append((name, _dump(prefix, copy)))
                    break
            else:
                continue
            break
    # Structural fallbacks for diffs without literals: renaming a symbol breaks its callers.
    if len(out) < 3:
        for hi, h in enumerate(hunks):
            for li in _added(h):
                if m := _DEF_RE.match(h.lines[li]):
                    changed = list(h.lines)
                    changed[li] = h.lines[li].replace(m.group(2), f"{m.group(2)}_mutant", 1)
                    copy = [*hunks]
                    copy[hi] = _recount(_Hunk(h.header, tuple(changed)))
                    out.append(("rename-def", _dump(prefix, copy)))
                    break
            else:
                continue
            break
    if len(out) < 3:
        for hi, h in enumerate(hunks):
            for li in _added(h):
                m = re.search(r"\b(\w+)=(\s*)(?:True|False|\d+)", h.lines[li])
                if m:
                    changed = list(h.lines)
                    changed[li] = changed[li].replace(m.group(1), f"{m.group(1)}_mutant", 1)
                    copy = [*hunks]
                    copy[hi] = _recount(_Hunk(h.header, tuple(changed)))
                    out.append(("rename-param", _dump(prefix, copy)))
                    break
            else:
                continue
            if len(out) >= 3:
                break
    return out[:5]
