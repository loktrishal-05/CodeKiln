"""Separate verifier container (issue #49): grade only ``agent.diff`` in a fresh hardened container.

Trust boundary 4 (docs/SPEC.md): the agent container's filesystem, site-packages, /tmp, and
/logs/verifier are never reused for grading. Only the extracted unified diff is validated against
test-infra/runner tampering policies and applied inside a brand-new, network-isolated verifier
container with dropped capabilities and resource caps.
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codekiln.config import Settings
from codekiln.synthesis.models import _DIFF_PATH, _TEST_INFRA
from codekiln.validation.mutation_probe import patch_problem

DockerRunner = Callable[[list[str], int | None], subprocess.CompletedProcess[str]]

_FORBIDDEN_VERIFIER_PATHS = re.compile(
    r"(?:^|/)(?:pyproject\.toml|setup\.py|sitecustomize\.py|usercustomize\.py)$"
    r"|\.pth$"
    r"|^\.(?:github/workflows|circleci)/"
)
_TIMEOUT_EXIT = 124


class VerifierError(ValueError):
    pass


@dataclass(frozen=True)
class IsolatedVerifyResult:
    task: str
    reward: float | None
    results: dict[str, str]
    diff_allowed: bool
    blocked_reason: str
    image: str
    container_isolated: bool = True

    @property
    def ok(self) -> bool:
        return self.diff_allowed and self.reward == 1.0

    def to_json(self) -> str:
        return json.dumps(
            {
                "task": self.task,
                "ok": self.ok,
                "reward": self.reward,
                "diff_allowed": self.diff_allowed,
                "blocked_reason": self.blocked_reason,
                "image": self.image,
                "container_isolated": self.container_isolated,
                "results": self.results,
            },
            indent=2,
        )


def _run_docker(args: list[str], timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            encoding="utf-8",  # not text=True: that decodes with the platform default (cp1252 on Windows)
            errors="replace",  # and test output may hold any byte
            check=False,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise VerifierError("docker binary not found on PATH") from exc
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["docker", *args],
            returncode=_TIMEOUT_EXIT,
            stdout="",
            stderr=f"verifier container timed out after {timeout_s}s",
        )


def validate_agent_diff(diff: str) -> str | None:
    """Return a rejection reason if DIFF touches test infra, runners, or Python startup hooks."""
    if not diff.strip():
        return None  # empty diff = nop agent; safe to grade (must score 0.0)
    if bad := patch_problem(diff):
        return f"invalid diff: {bad}"
    paths: list[str] = list(_DIFF_PATH.findall(diff))
    for line in diff.splitlines():
        if line.startswith(("+++ b/", "--- a/")):
            paths.append(line[6:].split("\t")[0])
    for path in dict.fromkeys(paths):
        if _TEST_INFRA.search(path) or _FORBIDDEN_VERIFIER_PATHS.search(path):
            return f"diff touches forbidden test/runner path: {path}"
    return None


def extract_agent_diff(trial_dir: Path) -> str:
    """Locate and read ``agent.diff`` from a Harbor trial output directory."""
    if not trial_dir.is_dir():
        raise VerifierError(f"trial directory does not exist: {trial_dir}")
    for candidate in sorted(trial_dir.rglob("agent.diff")):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    raise VerifierError(f"no agent.diff found under {trial_dir}")


def verifier_image_tag(task: Path) -> str:
    df = task / "environment" / "Dockerfile"
    if not df.is_file():
        raise VerifierError(f"{task} has no environment/Dockerfile")
    digest = hashlib.sha256(df.read_bytes()).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9_.-]", "-", task.name.lower()).strip("-") or "task"
    return f"codekiln/verifier:{slug}-{digest}"


def ensure_verifier_image(
    task: Path,
    *,
    image: str | None = None,
    runner: DockerRunner | None = None,
) -> str:
    """Build or reuse the clean base image from TASK/environment/Dockerfile."""
    run_docker = runner or _run_docker
    tag = image or verifier_image_tag(task)
    if image is None:
        inspected = run_docker(["image", "inspect", "--format", "{{.Id}}", tag], 30)
        if inspected.returncode == 0 and inspected.stdout.strip():
            return tag
        build = run_docker(["build", "-t", tag, str(task / "environment")], 900)
        if build.returncode != 0:
            raise VerifierError(f"verifier image build failed for {tag}: {build.stderr.strip()}")
    return tag


def build_hardened_run_args(
    *,
    image: str,
    verifier_in: Path,
    verifier_out: Path,
    memory: str,
    cpus: int,
    pids_limit: int = 256,
) -> list[str]:
    """Build hardened docker run arguments matching SPEC execution plane isolation."""
    cmd = (
        "set -u; cd /app; "
        "if [ -s /verifier_in/agent.diff ]; then "
        "  git apply --whitespace=nowarn /verifier_in/agent.diff || exit 1; "
        "fi; "
        "cp -r /verifier_in/tests/feature/. /app/tests/ && "
        "python -m pytest -p no:cacheprovider -rA --tb=short "
        "--continue-on-collection-errors tests > /logs/verifier/pytest.txt 2>&1; "
        "python /verifier_in/tests/reward.py /logs/verifier/pytest.txt "
        "/verifier_in/tests/fail_to_pass.json /verifier_in/tests/pass_to_pass.json /logs/verifier"
    )
    return [
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(pids_limit),
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "-v",
        f"{verifier_in.resolve()}:/verifier_in:ro",
        "-v",
        f"{verifier_out.resolve()}:/logs/verifier:rw",
        "--entrypoint",
        "bash",
        image,
        "-c",
        cmd,
    ]


def verify_in_separate_container(
    task: Path,
    agent_diff: str,
    work: Path,
    *,
    image: str | None = None,
    timeout_s: int | None = None,
    memory: str | None = None,
    cpus: int | None = None,
    runner: DockerRunner | None = None,
) -> IsolatedVerifyResult:
    """Grade AGENT_DIFF in a fresh, network-isolated container separate from the agent."""
    for req in ("task.toml", "environment/Dockerfile", "tests/reward.py", "tests/fail_to_pass.json"):
        if not (task / req).is_file():
            raise VerifierError(f"{task} is missing {req}")

    if reason := validate_agent_diff(agent_diff):
        return IsolatedVerifyResult(
            task=task.name,
            reward=0.0,
            results={},
            diff_allowed=False,
            blocked_reason=reason,
            image=image or verifier_image_tag(task),
        )

    run_docker = runner or _run_docker
    settings = Settings()
    eff_timeout = settings.sandbox_timeout_s if timeout_s is None else timeout_s
    eff_memory = settings.sandbox_memory if memory is None else memory
    eff_cpus = settings.sandbox_cpus if cpus is None else cpus
    tag = ensure_verifier_image(task, image=image, runner=run_docker)

    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="verifier-", dir=work) as tmp:
        tmp_path = Path(tmp)
        vin = tmp_path / "in"
        vout = tmp_path / "out"
        vin.mkdir()
        vout.mkdir()
        (vin / "agent.diff").write_text(agent_diff, encoding="utf-8", newline="\n")
        shutil.copytree(task / "tests", vin / "tests")

        args = build_hardened_run_args(
            image=tag,
            verifier_in=vin,
            verifier_out=vout,
            memory=eff_memory,
            cpus=eff_cpus,
        )
        proc = run_docker(args, eff_timeout)

        reward_file = vout / "reward.txt"
        results_file = vout / "results.json"
        reward: float | None = None
        results: dict[str, str] = {}
        if reward_file.is_file():
            try:
                reward = float(reward_file.read_text(encoding="utf-8").strip())
            except ValueError:
                reward = None
        if results_file.is_file():
            try:
                raw = json.loads(results_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    results = {str(k): str(v) for k, v in raw.items()}
            except json.JSONDecodeError:
                results = {}

        blocked = ""
        if reward is None and proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            blocked = f"verifier container exited {proc.returncode}: {tail or 'no reward emitted'}"

        return IsolatedVerifyResult(
            task=task.name,
            reward=reward,
            results=results,
            diff_allowed=True,
            blocked_reason=blocked,
            image=tag,
        )


def write_verifier_receipt(path: Path, result: IsolatedVerifyResult) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.to_json(), encoding="utf-8", newline="\n")
    return path
