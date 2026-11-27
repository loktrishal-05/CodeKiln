"""Docker environment bootstrap: one cached image per (repo, commit, arch), ARM64/x86.

Renders a hardened non-root Dockerfile from templates/dockerfiles/python.Dockerfile.j2 and
builds/caches the Docker image without writing any files into the target repository.
"""
import hashlib
import json
import platform
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codekiln.ingestion.profiler import RepoProfile, profile_repo
from codekiln.templates import render_template

DockerRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]

_ARCH_ALIASES: dict[str, tuple[str, str]] = {
    "amd64": ("linux/amd64", "amd64"),
    "x86_64": ("linux/amd64", "amd64"),
    "x86": ("linux/amd64", "amd64"),
    "linux/amd64": ("linux/amd64", "amd64"),
    "arm64": ("linux/arm64", "arm64"),
    "aarch64": ("linux/arm64", "arm64"),
    "linux/arm64": ("linux/arm64", "arm64"),
}


class BootstrapError(Exception):
    pass


@dataclass(frozen=True)
class EnvImage:
    tag: str
    digest: str
    platform: str
    arch: str
    profile: RepoProfile
    cached: bool
    dockerfile: str


def _run_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise BootstrapError("docker binary not found on PATH") from exc


def normalize_platform(arch: str = "auto") -> tuple[str, str]:
    """Return (docker_platform, short_arch) for ARM64 or x86_64/amd64."""
    key = platform.machine().lower() if arch == "auto" else arch.lower()
    if key not in _ARCH_ALIASES:
        raise BootstrapError(f"unsupported architecture: {arch}")
    return _ARCH_ALIASES[key]


def image_tag(repo_id: str, commit: str, arch: str = "auto") -> str:
    """Deterministic Docker tag scoped to (repo_id, commit, arch)."""
    if not repo_id or not commit:
        raise BootstrapError("repo_id and commit must be non-empty")
    docker_platform, short_arch = normalize_platform(arch)
    key_hash = hashlib.sha256(f"{repo_id}|{commit}|{docker_platform}".encode()).hexdigest()[:12]
    short_commit = re.sub(r"[^a-z0-9_.-]", "", commit.lower())[:12] or "head"
    return f"codekiln/env:{short_commit}-{short_arch}-{key_hash}"


def render_dockerfile(profile: RepoProfile) -> str:
    """Render the Dockerfile for a profiled repository."""
    if profile.stack != "python":
        raise BootstrapError(f"unsupported stack for Wave 1 bootstrap: {profile.stack}")
    cmd = list(profile.test_command) if profile.test_command else ["pytest", "-q"]
    return render_template(
        "dockerfiles/python.Dockerfile.j2",
        python_version=profile.python_version,
        install_commands=profile.install_commands,
        test_command_json=json.dumps(cmd),
    )


def inspect_cached_image(
    tag: str, expected_arch: str, runner: DockerRunner = _run_docker
) -> str | None:
    """Return the local image digest if TAG exists and matches EXPECTED_ARCH, else None."""
    res = runner(["image", "inspect", "--format", "{{.Id}}|{{.Architecture}}", tag])
    if res.returncode != 0:
        return None
    line = res.stdout.strip().splitlines()[0] if res.stdout.strip() else ""
    if "|" not in line:
        return None
    digest, actual_arch = line.split("|", 1)
    if actual_arch.strip().lower() != expected_arch or not digest.strip():
        return None
    return digest.strip()


def _record_repository(
    con: sqlite3.Connection, repo_id: str, commit: str, stack: str, digest: str
) -> None:
    with con:
        con.execute(
            """
            INSERT INTO repositories (url, base_commit, stack, image_digest)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url, base_commit) DO UPDATE SET
              stack = excluded.stack,
              image_digest = excluded.image_digest
            """,
            (repo_id, commit, stack, digest),
        )


def bootstrap_env(
    repo_dir: Path,
    repo_id: str,
    commit: str,
    *,
    arch: str = "auto",
    force_rebuild: bool = False,
    con: sqlite3.Connection | None = None,
    runner: DockerRunner = _run_docker,
) -> EnvImage:
    """Profile REPO_DIR and ensure one cached Docker image exists for (repo_id, commit, arch)."""
    docker_platform, short_arch = normalize_platform(arch)
    profile = profile_repo(repo_dir)
    dockerfile = render_dockerfile(profile)
    tag = image_tag(repo_id, commit, arch)

    if not force_rebuild and (cached_digest := inspect_cached_image(tag, short_arch, runner)):
        if con is not None:
            _record_repository(con, repo_id, commit, profile.stack, cached_digest)
        return EnvImage(tag, cached_digest, docker_platform, short_arch, profile, True, dockerfile)

    with tempfile.TemporaryDirectory(prefix="codekiln-build-") as tmp:
        df_path = Path(tmp) / "Dockerfile"
        df_path.write_text(dockerfile, encoding="utf-8", newline="\n")
        build = runner(
            ["build", "--platform", docker_platform, "-t", tag, "-f", str(df_path), str(repo_dir)]
        )
        if build.returncode != 0:
            raise BootstrapError(f"docker build failed for {tag}: {build.stderr.strip()}")

    digest = inspect_cached_image(tag, short_arch, runner)
    if not digest:
        raise BootstrapError(f"built image missing or wrong arch after build: {tag}")
    if con is not None:
        _record_repository(con, repo_id, commit, profile.stack, digest)
    return EnvImage(tag, digest, docker_platform, short_arch, profile, False, dockerfile)
