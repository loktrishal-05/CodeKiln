"""3-state gate (docs/SPEC.md). Execution-based: only runner results count, never LLM claims.

A: base passes existing tests (defines P2P).  B: base + F2P tests -> every F2P fails, P2P passes (x N).
C: base + reference patch -> everything passes (x N). P2P nodes that flip within a state are
quarantined as flaky; a flaky F2P node rejects the candidate.
"""
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from codekiln.states import State

Phase = Literal["A", "B", "C"]
Runner = Callable[[Phase], dict[str, str]]  # injected: sandbox run -> {node_id: status}


@dataclass
class GateResult:
    state: State
    reason: str = ""
    p2p: set[str] = field(default_factory=set)
    quarantined: set[str] = field(default_factory=set)


def _flaky(runs: list[dict[str, str]], nodes: set[str]) -> set[str]:
    return {n for n in nodes if len({r.get(n) for r in runs}) > 1}


def _reject(why: str, p2p: set[str], quarantined: set[str]) -> GateResult:
    return GateResult(State.REJECTED, why, p2p, quarantined)


def run_gate(runner: Runner, f2p: set[str], runs: int = 3) -> GateResult:
    if not f2p:
        return _reject("no F2P tests", set(), set())
    a = runner("A")
    p2p = {n for n, s in a.items() if s == "passed"}
    if not p2p or any(s == "failed" for s in a.values()):
        return _reject("STATE_A: base tests do not all pass", p2p, set())

    b = [runner("B") for _ in range(runs)]
    if _flaky(b, f2p):
        return _reject("STATE_B: flaky F2P test", p2p, set())
    quarantined = _flaky(b, p2p)
    for r in b:
        for n in f2p:
            if r.get(n) != "failed":  # passed = feature already present; missing = harness crash
                return _reject(f"STATE_B: F2P {n} is {r.get(n, 'missing')} on base", p2p, quarantined)
        if bad := {n for n in p2p - quarantined if r.get(n) != "passed"}:
            return _reject(f"STATE_B: P2P broken by F2P tests: {sorted(bad)}", p2p, quarantined)

    c = [runner("C") for _ in range(runs)]
    quarantined |= _flaky(c, p2p)
    if _flaky(c, f2p):
        return _reject("STATE_C: flaky F2P test", p2p, quarantined)
    for r in c:
        if bad := {n for n in (f2p | p2p) - quarantined if r.get(n) != "passed"}:
            return _reject(f"STATE_C: reference patch fails: {sorted(bad)}", p2p, quarantined)
    return GateResult(State.STATE_C_ALL_PASS, "", p2p - quarantined, quarantined)


def write_quarantine(path: Path, result: GateResult) -> None:
    path.write_text(json.dumps({"quarantined": sorted(result.quarantined)}, indent=2))
