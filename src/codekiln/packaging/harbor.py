"""Write one Harbor 0.25 task dir (layout: docs/SPEC.md -> Task package).

The scripts are proven on real Harbor + click 8.5.1: nop -> 0.0, oracle -> 1.0 (no-network).
Inputs come from the synthesizer (LLM output), so names and ids are validated before any write.
ponytail: no staging/receipt/manifest/scrub here; the packager item wraps this with those.
"""
import json
import re
import shutil
from importlib.resources import files
from pathlib import Path

from codekiln.templates import render_template

_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"https://[\w.-]+(/[\w.-]+)+")
_TEST_FILE = re.compile(r"test_[a-z0-9_]+\.py")
_STATIC = ("solve.sh", "test.sh", "reward.py")


class PackageError(ValueError):
    pass


def write_task(
    dst: Path,
    *,
    instruction: str,
    reference_diff: str,
    feature_tests: dict[str, str],
    f2p: list[str],
    p2p: list[str],
    repo_url: str,
    base_commit: str,
    category: str,
    difficulty: str,
    license: str = "BSD-3-Clause",
    python_version: str = "3.12",
    pytest_version: str = "8.4.1",
) -> Path:
    """FEATURE_TESTS maps file name -> source; they land in the repo's tests/ only at grading."""
    if not _SHA.fullmatch(base_commit):
        raise PackageError(f"base_commit must be a full 40-char sha, got {base_commit!r}")
    if not _REPO.fullmatch(repo_url):
        raise PackageError(f"unsupported repo url {repo_url!r}")
    if not re.fullmatch(r"3\.\d+", python_version):
        raise PackageError(f"bad python version {python_version!r}")
    if bad := [n for n in feature_tests if not _TEST_FILE.fullmatch(n)]:
        raise PackageError(f"feature test names must look like test_x.py: {bad}")
    if not f2p:
        raise PackageError("a task needs at least one fail_to_pass test")
    if stray := [n for n in f2p if n.split("::")[0].removeprefix("tests/") not in feature_tests]:
        raise PackageError(f"fail_to_pass ids outside the feature tests: {stray}")
    if overlap := set(f2p) & set(p2p):
        raise PackageError(f"ids in both fail_to_pass and pass_to_pass: {sorted(overlap)}")

    if dst.exists():
        shutil.rmtree(dst)
    for sub in ("environment", "solution", "tests/feature"):
        (dst / sub).mkdir(parents=True)
    ctx = {"repo_url": repo_url, "base_commit": base_commit, "license": license,
           "category": category, "difficulty": difficulty,
           "python_version": python_version, "pytest_version": pytest_version}
    (dst / "task.toml").write_text(render_template("harbor/task.toml.j2", **ctx), encoding="utf-8", newline="\n")
    (dst / "environment" / "Dockerfile").write_text(render_template("harbor/Dockerfile.j2", **ctx), encoding="utf-8", newline="\n")
    (dst / "instruction.md").write_text(instruction, encoding="utf-8", newline="\n")
    (dst / "solution" / "reference.diff").write_text(reference_diff, encoding="utf-8", newline="\n")
    tpl = files("codekiln.templates").joinpath("harbor")
    for name, sub in zip(_STATIC, ("solution", "tests", "tests"), strict=True):
        p = dst / sub / name
        p.write_text(tpl.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        p.chmod(0o755)
    for name, src in feature_tests.items():
        (dst / "tests" / "feature" / name).write_text(src, encoding="utf-8", newline="\n")
    (dst / "tests" / "fail_to_pass.json").write_text(json.dumps(sorted(f2p), indent=1), encoding="utf-8", newline="\n")
    (dst / "tests" / "pass_to_pass.json").write_text(json.dumps(sorted(p2p), indent=1), encoding="utf-8", newline="\n")
    return dst
