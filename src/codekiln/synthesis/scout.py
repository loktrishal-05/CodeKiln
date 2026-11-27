"""Repo Scout: a bounded context pack per plan slot, so writers match the repo instead of guessing.

For the files a slot touches it lists the public API (signatures plus the first docstring line),
how many files depend on each, and the names of the existing tests, so a new feature and its tests
look like they belong. Deterministic and static (nothing is imported or run), and capped in size.
The pack is repo-derived text: callers put it inside the untrusted-content wrapper.
"""
import ast
from collections.abc import Iterable
from pathlib import Path

from codekiln.planner.reverse_extract import scan

LIMIT_CHARS = 6000  # the prompt budget for one slot
_MAX_METHODS = 8
_MAX_TESTS = 12


def _doc(node: ast.AST) -> str:
    doc = ast.get_docstring(node) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module) else None
    return doc.strip().splitlines()[0][:100] if doc and doc.strip() else ""


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
    return f"{prefix} {fn.name}({ast.unparse(fn.args)}){returns}"


def _api(tree: ast.Module) -> list[str]:
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_"):
            lines.append(f"- {_signature(node)}" + (f"  # {_doc(node)}" if _doc(node) else ""))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            lines.append(f"- class {node.name}({bases})" + (f"  # {_doc(node)}" if _doc(node) else ""))
            methods = [m for m in node.body
                       if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef) and not m.name.startswith("_")]
            lines += [f"    - {_signature(m)}" for m in methods[:_MAX_METHODS]]
            if len(methods) > _MAX_METHODS:
                lines.append(f"    - ... {len(methods) - _MAX_METHODS} more")
    return lines


def related_tests(root: Path, rel: str) -> list[str]:
    """Names of existing tests for REL (tests/test_<stem>*.py), to show the repo's test style."""
    stem = Path(rel).stem.lstrip("_")
    names: list[str] = []
    for test in sorted((root / "tests").glob(f"test_{stem}*.py")):
        try:
            tree = ast.parse(test.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        names += [f"{test.name}::{n.name}" for n in tree.body
                  if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test")]
    return names[:_MAX_TESTS]


def context_pack(root: Path, files: Iterable[str], limit: int = LIMIT_CHARS) -> str:
    """Markdown context for FILES under ROOT, capped at LIMIT characters at a line boundary."""
    graph = scan(root)
    out: list[str] = []
    for rel in sorted(set(files)):
        path = root / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        out.append(f"### {rel}" + (f" - {_doc(tree)}" if _doc(tree) else ""))
        out.append(f"{len(graph.dependents(rel, 1))} file(s) import this module.")
        out += _api(tree)
        if tests := related_tests(root, rel):
            out.append("existing tests: " + ", ".join(tests))
        out.append("")
    text = "\n".join(out).rstrip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n[truncated]"
