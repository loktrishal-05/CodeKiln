"""Harbor reward for a CodeKiln task. Runs inside the task container: stdlib only.

Official Innodata formula: any pass_to_pass test not passing -> 0.0; else the share of
fail_to_pass tests that pass. Statuses come from pytest's `-rA` summary (exact node ids).
Also writes results.json ({node_id: status}) so the factory's gate can read per-test results.
"""
import json
import sys
from pathlib import Path

_OUTCOMES = {"PASSED": "passed", "FAILED": "failed", "ERROR": "failed", "XPASS": "failed",
             "XFAIL": "skipped"}


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        word, _, rest = line.partition(" ")
        if word in _OUTCOMES and "::" in rest:
            node = rest.strip() if word == "PASSED" else rest.split(" - ")[0].strip()
            out[node] = _OUTCOMES[word]
    return out


def reward(status: dict[str, str], f2p: list[str], p2p: list[str]) -> float:
    if not f2p or any(status.get(n) != "passed" for n in p2p):
        return 0.0
    return sum(status.get(n) == "passed" for n in f2p) / len(f2p)


def main(pytest_out: str, f2p_path: str, p2p_path: str, out_dir: str) -> None:
    text = Path(pytest_out).read_text(errors="replace")
    status = parse(text)
    f2p, p2p = json.loads(Path(f2p_path).read_text()), json.loads(Path(p2p_path).read_text())
    # "ERROR tests/test_x.py - ImportError": the whole file failed to collect (feature missing)
    broken = {ln.split()[1] for ln in text.splitlines() if ln.startswith("ERROR ") and "::" not in ln.split(" - ")[0]}
    for n in f2p:
        if n not in status and n.split("::")[0] in broken:
            status[n] = "failed"
    Path(out_dir, "results.json").write_text(json.dumps(status, indent=0))
    Path(out_dir, "reward.txt").write_text(str(reward(status, f2p, p2p)))


if __name__ == "__main__":
    main(*sys.argv[1:5])
