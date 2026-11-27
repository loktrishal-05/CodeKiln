import subprocess
from pathlib import Path

import pytest

from codekiln.db import connect
from codekiln.ingestion.env_bootstrap import (
    BootstrapError,
    bootstrap_env,
    image_tag,
    normalize_platform,
    render_dockerfile,
)
from codekiln.ingestion.profiler import RepoProfile


def _cp(args: list[str], rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["docker", *args], returncode=rc, stdout=stdout, stderr=stderr)


def test_normalize_platform_arm64_and_x86():
    assert normalize_platform("x86_64") == ("linux/amd64", "amd64")
    assert normalize_platform("amd64") == ("linux/amd64", "amd64")
    assert normalize_platform("arm64") == ("linux/arm64", "arm64")
    assert normalize_platform("aarch64") == ("linux/arm64", "arm64")
    with pytest.raises(BootstrapError, match="unsupported architecture"):
        normalize_platform("riscv64")


def test_image_tag_per_repo_commit_arch():
    t1 = image_tag("https://github.com/org/repo", "abcdef1234567890", "amd64")
    t2 = image_tag("https://github.com/org/repo", "abcdef1234567890", "arm64")
    t3 = image_tag("https://github.com/org/other", "abcdef1234567890", "amd64")
    t4 = image_tag("https://github.com/org/repo", "9999999999999999", "amd64")
    assert t1 == image_tag("https://github.com/org/repo", "abcdef1234567890", "x86_64")
    assert len({t1, t2, t3, t4}) == 4
    assert "-amd64-" in t1 and "-arm64-" in t2


def test_render_dockerfile_hardened_and_rejects_non_python():
    profile = RepoProfile(
        stack="python",
        python_version="3.12",
        package_manager="pip",
        test_runner="pytest",
        test_command=("pytest", "-q"),
        install_commands=('python -m pip install --no-cache-dir ".[dev]"',),
        dependency_files=("pyproject.toml",),
        extras=("dev",),
    )
    df = render_dockerfile(profile)
    assert "FROM python:3.12-slim" in df
    assert "USER codekiln" in df
    assert 'RUN python -m pip install --no-cache-dir ".[dev]"' in df
    assert 'CMD ["pytest", "-q"]' in df

    node_profile = RepoProfile("node", "3.12", "none", "unknown", (), (), ("package.json",))
    with pytest.raises(BootstrapError, match="unsupported stack"):
        render_dockerfile(node_profile)


def test_bootstrap_env_builds_once_caches_and_records_db(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (repo / "test_sample.py").write_text("def test_x(): assert True\n", encoding="utf-8")

    calls: list[list[str]] = []
    built: dict[str, str] = {}

    def fake_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["image", "inspect"]:
            tag = args[-1]
            if tag in built:
                return _cp(args, 0, stdout=f"{built[tag]}|arm64\n")
            return _cp(args, 1, stderr="No such image")
        if args[0] == "build":
            tag = args[args.index("-t") + 1]
            assert "--platform" in args and args[args.index("--platform") + 1] == "linux/arm64"
            df_path = Path(args[args.index("-f") + 1])
            assert df_path.parent != repo  # repo working tree stays clean
            assert "USER codekiln" in df_path.read_text(encoding="utf-8")
            built[tag] = "sha256:deadbeef1234"
            return _cp(args, 0)
        raise AssertionError(f"unexpected docker command: {args}")

    con = connect(":memory:")
    img1 = bootstrap_env(repo, "https://example.com/demo", "abc123456789", arch="arm64", con=con, runner=fake_docker)
    assert not img1.cached
    assert img1.platform == "linux/arm64"
    assert img1.digest == "sha256:deadbeef1234"
    assert sorted(p.name for p in repo.iterdir()) == ["pyproject.toml", "test_sample.py"]

    row = con.execute("SELECT stack, image_digest FROM repositories").fetchone()
    assert row == ("python", "sha256:deadbeef1234")

    calls.clear()
    img2 = bootstrap_env(repo, "https://example.com/demo", "abc123456789", arch="arm64", con=con, runner=fake_docker)
    assert img2.cached
    assert img2.tag == img1.tag
    assert all(c[0] != "build" for c in calls)

    calls.clear()
    img3 = bootstrap_env(
        repo, "https://example.com/demo", "abc123456789", arch="arm64", force_rebuild=True, runner=fake_docker
    )
    assert not img3.cached
    assert any(c[0] == "build" for c in calls)


def test_bootstrap_env_build_failure_raises(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("x = 1\n", encoding="utf-8")

    def failing_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["image", "inspect"]:
            return _cp(args, 1)
        return _cp(args, 1, stderr="pip failed")

    with pytest.raises(BootstrapError, match="docker build failed"):
        bootstrap_env(repo, "repo", "commit1", arch="amd64", runner=failing_docker)
