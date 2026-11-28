"""Turn a model's *edit plan* into an exact unified diff, built from the real files.

A language model cannot reliably write a unified diff for a file it cannot see: it does not know the
line numbers or the exact context, so its hunks almost never apply (found on the first real run).
So the model only says WHERE new code goes, by symbol, and this module computes the diff itself:

    {"op": "add_import",   "path": P, "line": "import json"}              after the last top-level import
    {"op": "insert_after", "path": P, "symbol": "StringParamType", "code": "..."}  after that def/class
    {"op": "append",       "path": P, "code": "..."}                      at the end of the file
    {"op": "new_file",     "path": P, "code": "..."}                      a file that does not exist yet

Additive on purpose: a task is a NEW feature on an unmodified base, so nothing existing is rewritten.
Every edit is checked against the real workspace and the finished diff is proven with `git apply --check`.
"""
import ast
import difflib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from codekiln.synthesis.models import SynthesisError

MAX_EDITS = 12

# What a writer is told when it must propose code without seeing line numbers (shared by every agent that
# proposes a patch: the test critic's alternative solution, the adversarial mutants).
EDITS_FORMAT = """\
Do NOT write a diff: you cannot see line numbers. Reply with JSON only, {"edits": [...]}, where each edit is
{"op": "add_import", "path": P, "line": "import json"} |
{"op": "insert_after", "path": P, "symbol": "<an existing top-level def or class in P>", "code": "..."} |
{"op": "append", "path": P, "code": "..."} | {"op": "new_file", "path": P, "code": "..."}.
Use only paths and symbol names from the context pack. Edits only ADD code and never touch tests."""


def reference_context(root: Path, reference_diff: str) -> str:
    """The Repo Scout pack for the files REFERENCE_DIFF touches: the real symbols a writer may anchor edits to."""
    from codekiln.synthesis.models import _DIFF_PATH
    from codekiln.synthesis.scout import context_pack

    try:
        return context_pack(root, dict.fromkeys(_DIFF_PATH.findall(reference_diff)))
    except (OSError, ValueError):
        return ""  # the pack is a hint; a repo it cannot read must not stop the proposal


def diff_from_reply(text: str, root: Path) -> str:
    """The unified diff for a reply of `{"edits": [...]}` (fenced or not), built from the real files under ROOT."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise SynthesisError("completion contains no JSON object")
    try:
        raw: object = json.loads(body[start : end + 1])
    except json.JSONDecodeError as e:
        raise SynthesisError(f"completion is not valid JSON: {e.msg}") from e
    if not isinstance(raw, dict) or "edits" not in raw:
        raise SynthesisError('reply must be a JSON object {"edits": [...]}')
    return build_diff(root, raw["edits"])


def build_diff(root: Path, edits: object) -> str:
    """The unified diff for EDITS applied to the files under ROOT; raises SynthesisError with a
    reason the model can act on (the repair loop sends it back)."""
    if not isinstance(edits, list) or not edits:
        raise SynthesisError("edits must be a non-empty list")
    if len(edits) > MAX_EDITS:
        raise SynthesisError(f"at most {MAX_EDITS} edits, got {len(edits)}")
    before: dict[str, str | None] = {}  # path -> original text (None = new file)
    after: dict[str, str] = {}
    for i, edit in enumerate(edits):
        if not isinstance(edit, Mapping):
            raise SynthesisError(f"edit {i} must be an object")
        path = _safe_path(edit.get("path"), i)
        if path not in after:
            before[path] = _read(root, path, new=edit.get("op") == "new_file", i=i)
            after[path] = before[path] or ""
        after[path] = _apply(after[path], edit, path, i)
    parts = [_file_diff(p, before[p], after[p]) for p in after if after[p] != (before[p] or "")]
    if not parts:
        raise SynthesisError("the edits change nothing")
    diff = "".join(parts)
    _prove_it_applies(root, diff)
    return diff


def _safe_path(raw: object, i: int) -> str:
    if not isinstance(raw, str) or not raw:
        raise SynthesisError(f"edit {i}: path must be a string")
    p = PurePosixPath(raw)
    if p.is_absolute() or ".." in p.parts or "\\" in raw:
        raise SynthesisError(f"edit {i}: path must be a relative path inside the repo, got {raw!r}")
    return str(p)


def _read(root: Path, path: str, *, new: bool, i: int) -> str | None:
    target = root / path
    if new:
        if target.exists():
            raise SynthesisError(f"edit {i}: new_file {path} already exists; use insert_after or append")
        return None
    if not target.is_file():
        raise SynthesisError(f"edit {i}: {path} does not exist in the repo (use new_file to create it)")
    text = target.read_bytes().decode("utf-8", "replace")
    if "\r\n" in text:
        raise SynthesisError(f"edit {i}: {path} uses CRLF line endings, which edits do not support")
    return text if text.endswith("\n") or not text else text + "\n"


def _apply(text: str, edit: Mapping[str, object], path: str, i: int) -> str:
    op = edit.get("op")
    code = edit.get("code")
    if op == "add_import":
        line = edit.get("line")
        if not isinstance(line, str) or not line.strip() or "\n" in line.strip():
            raise SynthesisError(f"edit {i}: add_import needs a one-line `line`")
        return _add_import(text, line.strip(), path, i)
    if op in {"insert_after", "append", "new_file"}:
        if not isinstance(code, str) or not code.strip():
            raise SynthesisError(f"edit {i}: {op} needs non-empty `code`")
        block = code.strip("\n") + "\n"
        if op == "new_file":
            return block
        if op == "append":
            return text + ("\n\n" if text.strip() else "") + block
        symbol = edit.get("symbol")
        if not isinstance(symbol, str):
            raise SynthesisError(f"edit {i}: insert_after needs a top-level `symbol` name")
        end = _symbol_end(text, symbol, path, i)
        lines = text.splitlines(keepends=True)
        return "".join(lines[:end]) + "\n\n" + block + ("\n" + "".join(lines[end:]) if lines[end:] else "")
    raise SynthesisError(f"edit {i}: unknown op {op!r}; use add_import, insert_after, append or new_file")


def _parse(text: str, path: str, i: int) -> ast.Module:
    try:
        return ast.parse(text)
    except SyntaxError as e:
        raise SynthesisError(f"edit {i}: {path} does not parse after earlier edits: {e.msg}") from e


def _add_import(text: str, line: str, path: str, i: int) -> str:
    if line in text.splitlines():
        return text
    tree = _parse(text, path, i)
    imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    lines = text.splitlines(keepends=True)
    at = max((n.end_lineno or n.lineno for n in imports), default=0)
    return "".join(lines[:at]) + line + "\n" + "".join(lines[at:])


def _symbol_end(text: str, symbol: str, path: str, i: int) -> int:
    tree = _parse(text, path, i)
    names = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)}
    node = names.get(symbol)
    if node is None:
        shown = ", ".join(sorted(names)[:40])
        raise SynthesisError(f"edit {i}: no top-level def/class named {symbol!r} in {path}; available: {shown}")
    return node.end_lineno or node.lineno


def _file_diff(path: str, old: str | None, new: str) -> str:
    a = (old or "").splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    head = f"diff --git a/{path} b/{path}\n"
    if old is None:
        head += "new file mode 100644\n"
    body = difflib.unified_diff(a, b, "/dev/null" if old is None else f"a/{path}", f"b/{path}", n=3)
    return head + "".join(line if line.endswith("\n") else line + "\n" for line in body)


def _prove_it_applies(root: Path, diff: str) -> None:
    """The real test: git itself must accept the patch on the unmodified tree."""
    proc = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=root, input=diff.encode("utf-8"),
        capture_output=True, check=False,
    )
    if proc.returncode:
        raise SynthesisError("the edits do not produce an applicable patch: "
                             + proc.stderr.decode("utf-8", "replace").strip()[:200])
