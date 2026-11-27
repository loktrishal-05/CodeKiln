"""Reverse mode: scan real repo code and build the import graph to LOCATE where features live.

Nothing is stripped or modified: tasks start from the unmodified commit. A located slot is a
module's public API plus every file that depends on it (BFS over reverse imports).
ponytail: static import graph only; add coverage.py weighting once the sandbox can run tests.
"""
import ast
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from codekiln.planner.models import Category, Plan

_SKIP_DIRS = {"tests", "test", "venv", "node_modules", "site-packages", "build", "dist", "__pycache__", "examples", "docs"}
_HINTS = (
    (Category.API_CLI, ("cli", "api", "route", "view", "handler", "command", "main")),
    (Category.VALIDATION, ("valid", "schema", "check", "lint", "sanit")),
    (Category.PERSISTENCE, ("db", "store", "repo", "model", "cache", "storage", "sql")),
)


@dataclass
class Scan:
    graph: dict[str, set[str]]  # file -> files it imports
    symbols: dict[str, frozenset[str]]  # file -> public top-level names

    def dependents(self, file: str, depth: int) -> set[str]:
        rev: dict[str, set[str]] = {}
        for src, dsts in self.graph.items():
            for dst in dsts:
                rev.setdefault(dst, set()).add(src)
        seen: set[str] = set()
        queue = deque([(file, 0)])
        while queue:
            cur, d = queue.popleft()
            if d == depth:
                continue
            for nxt in rev.get(cur, ()):
                if nxt != file and nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, d + 1))
        return seen


def _is_source(rel: Path) -> bool:
    if any(p.startswith(".") or p in _SKIP_DIRS for p in rel.parts[:-1]):
        return False
    name = rel.name
    return not (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py")


def _dotted(rel: Path) -> list[str]:
    parts = list(rel.with_suffix("").parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def scan(root: Path) -> Scan:
    trees: dict[str, ast.Module] = {}
    names: dict[str, str] = {}
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if not _is_source(rel):
            continue
        try:
            trees[rel.as_posix()] = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        dotted = _dotted(rel)
        names[".".join(dotted)] = rel.as_posix()
        if dotted and dotted[0] == "src":
            names[".".join(dotted[1:])] = rel.as_posix()  # src/ layout imports omit "src"

    graph: dict[str, set[str]] = {f: set() for f in trees}
    symbols: dict[str, frozenset[str]] = {}
    for f, tree in trees.items():
        pkg = list(Path(f).parent.parts)
        symbols[f] = frozenset(
            n.name
            for n in tree.body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and not n.name.startswith("_")
        )
        for node in ast.walk(tree):
            targets: list[str | None] = []
            if isinstance(node, ast.Import):
                targets = [names.get(a.name) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = pkg[: len(pkg) - (node.level - 1)] if node.level else []
                mod = ".".join([*base, *(node.module.split(".") if node.module else [])])
                subs = [names.get(f"{mod}.{a.name}" if mod else a.name) for a in node.names]
                targets = subs if any(subs) else [names.get(mod)]  # `from pkg import db` -> db.py only
            graph[f].update(t for t in targets if t and t != f)
    return Scan(graph, symbols)


def classify(file: str) -> Category:
    low = Path(file).name.lower()  # filename only: "click/" in the path would match the "cli" hint
    for cat, keys in _HINTS:
        if any(k in low for k in keys):
            return cat
    return Category.CORE_LOGIC


def reverse_plans(root: Path, depth: int = 2) -> list[Plan]:
    s = scan(root)
    plans = []
    for f, syms in sorted(s.symbols.items()):
        if not syms or Path(f).name == "__init__.py":
            continue
        deps = s.dependents(f, depth)
        files = frozenset({f, *deps})
        cross = len({Path(x).parent for x in files}) > 1
        cat = Category.CROSS_MODULE if deps and cross else classify(f)
        plans.append(Plan(cat, files, syms, "reverse", len(deps)))
    return plans
