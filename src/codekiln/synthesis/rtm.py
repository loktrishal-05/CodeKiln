"""RTM audit: every REQ-n has real F2P tests, and tests only import symbols the instruction names.

docs/SPEC.md -> Operational artifacts -> RTM audit. The result is `rtm_map.json`, written next to
the package and later folded into the ledger; the gate refuses RTM_OK while `problems` is non-empty.
The audit is static on purpose: the tests' behaviour is the gate's job, this checks the contract.
"""
import ast
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from codekiln.synthesis.models import REQ_ID, FeatureSpec, SynthesisError


@dataclass(frozen=True)
class RtmMap:
    task: str
    requirements: dict[str, list[str]]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> str:
        return json.dumps(
            {
                "task": self.task,
                "ok": self.ok,
                "requirements": self.requirements,
                "problems": list(self.problems),
            },
            indent=2,
        )


def defined_nodes(feature_tests: Mapping[str, str]) -> set[str]:
    """Every pytest node id a feature test file defines (parametrised cases share a base id)."""
    out: set[str] = set()
    for name, source in feature_tests.items():
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            raise SynthesisError(f"{name} does not parse: {e.msg}") from e
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if _is_test(sub):
                        out.add(f"tests/{name}::{node.name}::{sub.name}")
            elif _is_test(node):
                out.add(f"tests/{name}::{node.name}")
    return out


def click_imports(feature_tests: Mapping[str, str]) -> set[str]:
    """Names the tests `from click... import`; each must appear in instruction.md."""
    out: set[str] = set()
    for source in feature_tests.values():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "click":
                out |= {a.name for a in node.names if a.name != "*"}
    return out


def audit(spec: FeatureSpec) -> RtmMap:
    """Full RTM check; `problems` lists every violation, not just the first one."""
    nodes = defined_nodes(spec.feature_tests)
    f2p = {_base(n) for n in spec.f2p}
    problems: list[str] = []

    for node in sorted(f2p - nodes):
        problems.append(f"fail_to_pass id has no test function: {node}")

    mapped: dict[str, list[str]] = {}
    for req in spec.requirements:
        mapped[req.id] = list(req.tests)
        if not req.tests:
            problems.append(f"{req.id} has no fail_to_pass test")
        for node in req.tests:
            if _base(node) not in f2p:
                problems.append(f"{req.id} lists {node}, which is not in fail_to_pass")
            if _base(node) not in nodes:
                problems.append(f"{req.id} lists {node}, which no feature test defines")

    declared = set(REQ_ID.findall(spec.instruction))
    for req_id in sorted(declared - set(mapped)):
        problems.append(f"instruction declares {req_id} but no Requirement maps it to a test")

    for name in sorted(click_imports(spec.feature_tests)):
        if not re.search(rf"\b{re.escape(name)}\b", spec.instruction):
            problems.append(f"tests import `{name}` but instruction.md never names it")
    return RtmMap(spec.task, mapped, tuple(problems))


def require_ok(spec: FeatureSpec) -> RtmMap:
    """Audit and raise; use before writing files so a rejected spec leaves no package behind."""
    m = audit(spec)
    if not m.ok:
        raise SynthesisError("RTM audit failed: " + "; ".join(m.problems))
    return m


def write_map(path: Path, spec: FeatureSpec) -> RtmMap:
    m = audit(spec)
    path.write_text(m.to_json(), encoding="utf-8", newline="\n")
    return m


def _is_test(node: ast.stmt) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def _base(node_id: str) -> str:
    return node_id.split("[", 1)[0]
