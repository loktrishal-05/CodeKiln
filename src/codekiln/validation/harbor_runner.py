"""Thin wrapper over the Harbor 0.25 CLI: the same `harbor run` the judges use (docs/SPEC.md).

ponytail: reward only; per-test CTRF from each trial's verifier/ dir lands with the gate runner item.
"""
import json
import subprocess
from pathlib import Path


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
    cmd = ["harbor", "run", "-p", str(path), "-a", agent, "-k", "1", "-n", str(concurrency)]
    subprocess.run([*cmd, "-o", str(out), "-y"], check=False, capture_output=True)
    names = [path.name] if (path / "task.toml").exists() else [d.name for d in path.iterdir()]
    got = read_rewards(out)
    return {n: got.get(n) for n in names}
