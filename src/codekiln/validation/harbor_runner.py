"""Thin wrapper over the Harbor 0.25 CLI: the same `harbor run` the judges use (docs/SPEC.md).

The wrapper returns rewards only. ``validation.gate_runner`` reads per-test statuses from each
trial's ``verifier/results.json`` (CTRF-shaped, written by ``templates/harbor/reward.py``); State A
is the nop results with F2P nodes removed because ``test.sh`` always copies the feature tests in.
"""
import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def read_rewards(job_out: Path) -> dict[str, float | None]:
    """Task dir name -> reward for the newest job under a `harbor run -o` dir. None = env crash."""
    jobs = sorted(d for d in job_out.iterdir() if d.is_dir()) if job_out.is_dir() else []
    out: dict[str, float | None] = {}
    for f in jobs[-1].glob("*/result.json") if jobs else []:  # job dirs are timestamp-named
        r = json.loads(f.read_text())
        vr = r.get("verifier_result") or {}
        out[Path(r["task_id"]["path"]).name] = (vr.get("rewards") or {}).get("reward")
    return out


def run(path: Path, out: Path, agent: str = "oracle", concurrency: int = 4) -> dict[str, float | None]:
    """Run AGENT on a task or dataset dir; trials missing from the output count as crashes."""
    # Keep the judge's exact flags (docs/SPEC.md), including --no-delete, so our runs match theirs
    # and trial artifacts stay available for reward / per-test read-back.
    cmd = ["harbor", "run", "-p", str(path), "-a", agent, "-k", "1", "-n", str(concurrency),
           "-o", str(out), "--no-delete", "-y"]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    names = [path.name] if (path / "task.toml").exists() else [d.name for d in path.iterdir()]
    got = read_rewards(out)
    if proc.returncode != 0 and not any(v is not None for v in got.values()):
        # A crash with no rewards must be diagnosable; callers see only {name: None}.
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        log.warning("harbor run failed (%s) for %s: %s", proc.returncode, path.name, tail or "no output")
    return {n: got.get(n) for n in names}
