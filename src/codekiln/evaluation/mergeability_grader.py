"""Mergeability & blast-radius grader: would a maintainer accept the agent's diff?

Compares an agent diff against the reference diff: LOC bloat (<2.0x), files touched outside
the reference's scope (blast radius), and optional linter/type-checker deltas measured by the caller.
"""
from dataclasses import dataclass

MAX_BLOAT = 2.0


@dataclass(frozen=True)
class DiffStats:
    files: frozenset[str]
    loc: int  # added + removed lines


@dataclass(frozen=True)
class Grade:
    bloat_ratio: float
    blast_radius: int  # files the agent touched that the reference did not
    lint_delta: int
    type_delta: int
    mergeable: bool


def diff_stats(diff: str) -> DiffStats:
    files: set[str] = set()
    loc = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0]
            if path != "/dev/null":
                files.add(path.removeprefix("b/"))
        elif line.startswith("--- ") and line[4:].startswith("a/"):
            files.add(line[6:].split("\t")[0])  # deletions: +++ is /dev/null
        elif line[:1] in "+-" and not line.startswith(("+++", "---")):
            loc += 1
    return DiffStats(frozenset(files), loc)


def grade(reference: str, agent: str, lint_delta: int = 0, type_delta: int = 0) -> Grade:
    ref, ag = diff_stats(reference), diff_stats(agent)
    bloat = ag.loc / max(ref.loc, 1)
    blast = len(ag.files - ref.files)
    # ponytail: no cyclomatic-complexity delta; add a radon-style check if graders want it
    ok = bloat < MAX_BLOAT and blast == 0 and lint_delta <= 0 and type_delta <= 0
    return Grade(round(bloat, 2), blast, lint_delta, type_delta, ok)
