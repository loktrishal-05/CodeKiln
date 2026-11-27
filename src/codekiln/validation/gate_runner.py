"""Adapter: Harbor trials -> the 3-state gate's Runner (Phase -> {node_id: status}).

B runs `nop`, C runs `oracle`; per-test statuses come from the trial's verifier/results.json
(written by templates/harbor/reward.py, already {node_id: status}). Each call gets a fresh -o dir.
Resolved option for issue #19: State A = nop results minus F2P nodes, because `test.sh` always
copies the feature tests in (so a nop trial is really State B). Revisit only if the packager grows
a run that omits them.
"""
import json
from collections.abc import Callable
from itertools import count
from pathlib import Path

from codekiln.validation import harbor_runner
from codekiln.validation.three_state_gate import Phase, Runner

Run = Callable[[Path, Path, str], object]  # (task dir, -o dir, agent); results are read from disk
TrialRecord = Callable[[Phase, int, Path, dict[str, str]], None]  # (phase, index, -o dir, results)


def read_results(out: Path) -> dict[str, str]:
    """Per-test results of the newest job under OUT; {} when the trial crashed before the verifier."""
    jobs = sorted(d for d in out.iterdir() if d.is_dir()) if out.is_dir() else []
    for f in jobs[-1].glob("*/verifier/results.json") if jobs else []:
        return dict(json.loads(f.read_text(encoding="utf-8")))
    return {}


def gate_runner(
    task: Path, work: Path, run: Run = harbor_runner.run, record: TrialRecord | None = None
) -> Runner:
    f2p = set(json.loads((task / "tests" / "fail_to_pass.json").read_text(encoding="utf-8")))
    n = count()

    def runner(phase: Phase) -> dict[str, str]:
        index = next(n)
        out = work / f"{phase}-{index}"
        run(task, out, "oracle" if phase == "C" else "nop")
        results = read_results(out)
        if record is not None:
            record(phase, index, out, results)
        return {k: v for k, v in results.items() if k not in f2p} if phase == "A" else results

    return runner
