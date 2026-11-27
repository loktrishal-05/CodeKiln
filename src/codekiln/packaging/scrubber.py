"""Host-side security scrubber for a Harbor task before it is promoted.

Checks the anti-gaming rules in docs/SPEC.md: one-commit snapshot, editable install (no
site-packages copy), no-network runtime, diff policy, canary F2P tests and secret scan. Nothing here
touches a container; a failing check blocks export before `os.replace()` can promote the package.
"""
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codekiln.synthesis.models import _DIFF_PATH, _TEST_INFRA

_SECRETS = (r"sk-[A-Za-z0-9]{8,}", r"ghp_[A-Za-z0-9]{8,}", r"AKIA[0-9A-Z]{8,}")
_HOST_PATHS = (r"(?<![A-Za-z])[A-Za-z]:[\\/]", r"/Users/", r"/home/[a-z]+/")
_NETWORK_CALLS = ("subprocess", "socket.", "urllib.request", "requests.get", "requests.post")
_RUNTIME_EGRESS = ("curl ", "wget ", "--network", "urllib.request", "requests.get",
                   "requests.post", "socket.", "http.client")
_RUNTIME_FILES = ("tests/test.sh", "solution/solve.sh", "tests/reward.py", "instruction.md")
_CHECK_NAMES = (
    "single_commit_snapshot", "editable_install", "no_site_packages_leak", "no_network",
    "diff_policy", "canary", "secret_scan",
)
EgressProbe = Callable[[], bool]


class ScrubError(ValueError):
    pass


@dataclass(frozen=True)
class SecurityAudit:
    task: str
    checks: dict[str, bool]
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.reasons

    def to_json(self) -> str:
        return json.dumps(
            {"task": self.task, "ok": self.ok, "checks": self.checks, "reasons": list(self.reasons)},
            indent=2,
        )


def audit_task(task: Path, egress_probe: EgressProbe | None = None) -> SecurityAudit:
    """Audit a staged task dir; an injected egress probe can replace the static network check."""
    if not (task / "task.toml").is_file():
        raise ScrubError(f"{task} is not a Harbor task package")
    toml = (task / "task.toml").read_text(encoding="utf-8")
    docker = (task / "environment" / "Dockerfile").read_text(encoding="utf-8")
    diff = (task / "solution" / "reference.diff").read_text(encoding="utf-8")
    f2p = json.loads((task / "tests" / "fail_to_pass.json").read_text(encoding="utf-8"))
    if not isinstance(f2p, list):
        raise ScrubError("fail_to_pass.json must be a list")

    checks = {
        "single_commit_snapshot": "rm -rf .git" in docker and "git init" in docker,
        "editable_install": "pip install" in docker and "-e ." in docker,
        "no_site_packages_leak": not re.search(r"(cp|mv)\s+\S*site-packages", docker),
        "no_network": 'network_mode = "no-network"' in toml
        and (egress_probe() if egress_probe else _egress_blocked(task)),
        "diff_policy": _diff_policy(diff),
        "canary": bool(f2p) and any((task / "tests" / "feature").glob("test_*.py")),
        "secret_scan": _secret_scan(task),
    }
    reasons = tuple(
        f"{name}: failed" for name in _CHECK_NAMES if not checks.get(name, False)
    )
    return SecurityAudit(task.name, checks, reasons)


def write_audit(path: Path, audit: SecurityAudit) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(audit.to_json(), encoding="utf-8", newline="\n")
    return path


def _egress_blocked(task: Path) -> bool:
    """Static proof: the runtime scripts a graded agent can reach cannot fetch from the network."""
    text = "\n".join(
        (task / name).read_text(encoding="utf-8", errors="replace")
        for name in _RUNTIME_FILES if (task / name).is_file()
    )
    return not any(marker in text for marker in _RUNTIME_EGRESS)


def _diff_policy(diff: str) -> bool:
    if any(_TEST_INFRA.search(path) for path in _DIFF_PATH.findall(diff)):
        return False
    if re.search(r"^\+\+\+ b/\.github/workflows/|^\+\+\+ b/\.circleci/", diff, re.MULTILINE):
        return False
    added = [line for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    return not any(token in line for line in added for token in _NETWORK_CALLS)


def _secret_scan(task: Path) -> bool:
    for p in task.rglob("*"):
        if not p.is_file() or p.suffix in {".png", ".jpg", ".pyc"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(re.search(pattern, text) for pattern in (*_SECRETS, *_HOST_PATHS)):
            return False
    return True
