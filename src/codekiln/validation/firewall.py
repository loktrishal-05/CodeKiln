"""Contamination firewall: does the "new" feature already exist in a later release of the repo?

A task is only a fair test of building something new if models could not have seen the answer. If a
later release of the repo (source or changelog) already has the public function, class or
parameter the reference diff introduces, a model trained on that release may reproduce it from
memory, so the package is flagged. The check is static and offline: the caller supplies later
checkouts (for click, any later tag cloned into a directory) and nothing is fetched or executed.
Identifiers are matched structurally (a `def`/`class` of that name, a parameter inside the named
function's signature), not as bare words, so common names like `echo` do not cause false alarms.
"""
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_ADDED_DEF = re.compile(r"^\+[ \t]*(?:async[ \t]+)?def[ \t]+(\w+)[ \t]*\(([^)]*)\)", re.MULTILINE)
_REMOVED_DEF = re.compile(r"^-[ \t]*(?:async[ \t]+)?def[ \t]+(\w+)[ \t]*\(([^)]*)\)", re.MULTILINE)
_ADDED_CLASS = re.compile(r"^\+[ \t]*class[ \t]+(\w+)", re.MULTILINE)
_SUFFIXES = {".py", ".rst", ".md", ".txt"}
_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Marker:
    """Something the reference diff introduces: a def/class, or a parameter of an existing def."""

    kind: str  # "def" | "param"
    name: str
    owner: str = ""  # the function a "param" belongs to

    def label(self) -> str:
        return f"{self.owner}({self.name}=...)" if self.kind == "param" else self.name


@dataclass(frozen=True)
class Hit:
    marker: str
    file: str
    line: int


@dataclass(frozen=True)
class FirewallReport:
    task: str
    markers: tuple[str, ...]
    hits: tuple[Hit, ...]
    corpus: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.hits

    def to_json(self) -> str:
        return json.dumps(
            {
                "task": self.task, "ok": self.ok, "markers": list(self.markers),
                "corpus": list(self.corpus),
                "hits": [{"marker": h.marker, "file": h.file, "line": h.line} for h in self.hits],
            },
            indent=2,
        )


def _params(signature: str) -> set[str]:
    names: set[str] = set()
    for part in signature.split(","):
        m = re.match(r"\s*\*{0,2}\s*(\w+)", part)
        if m and m.group(1) not in {"self", "cls"}:
            names.add(m.group(1))
    return names


def markers(reference_diff: str) -> list[Marker]:
    """The public API the diff adds: new defs/classes, and new parameters of existing defs."""
    removed = {m.group(1): _params(m.group(2)) for m in _REMOVED_DEF.finditer(reference_diff)}
    out: list[Marker] = []
    for m in _ADDED_DEF.finditer(reference_diff):
        name = m.group(1)
        if name.startswith("_"):
            continue
        if name in removed:
            out += [Marker("param", p, name) for p in sorted(_params(m.group(2)) - removed[name])]
        else:
            out.append(Marker("def", name))
    out += [Marker("def", m.group(1)) for m in _ADDED_CLASS.finditer(reference_diff)
            if not m.group(1).startswith("_")]
    return out


def _files(corpus: Iterable[Path]) -> Iterable[Path]:
    for root in corpus:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in _SUFFIXES and path.stat().st_size <= _MAX_BYTES:
                yield path


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _search(marker: Marker, path: Path, text: str) -> list[Hit]:
    label, hits = marker.label(), []
    if marker.kind == "def":
        pattern = rf"\b(?:def|class)\s+{re.escape(marker.name)}\b" if path.suffix == ".py" \
            else rf"`{re.escape(marker.name)}\b"
        hits = [Hit(label, str(path), _line_of(text, m.start())) for m in re.finditer(pattern, text)]
    elif path.suffix == ".py":
        for sig in re.finditer(rf"\bdef\s+{re.escape(marker.owner)}\s*\(([^)]*)\)", text):
            if marker.name in _params(sig.group(1)):
                hits.append(Hit(label, str(path), _line_of(text, sig.start())))
    else:  # docs: the owner and the parameter named on the same line
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(rf"\b{re.escape(marker.owner)}\b", line) and re.search(
                rf"\b{re.escape(marker.name)}\b", line
            ):
                hits.append(Hit(label, str(path), n))
    return hits


def check(package: Path, corpus: Sequence[Path]) -> FirewallReport:
    """Search every later-release file in CORPUS for what PACKAGE's reference diff introduces."""
    diff = (package / "solution" / "reference.diff").read_text(encoding="utf-8")
    found = markers(diff)
    hits: list[Hit] = []
    for path in _files(corpus):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for marker in found:
            hits += _search(marker, path, text)
    return FirewallReport(package.name, tuple(m.label() for m in found), tuple(hits),
                          tuple(str(c) for c in corpus))
