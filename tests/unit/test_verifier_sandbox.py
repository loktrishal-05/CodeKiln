import json
import subprocess
from pathlib import Path

import pytest
from spec_examples import DIFF, FEATURE_TESTS, INSTRUCTION, REPO, REQ1, REQ2, SHA
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.packaging.harbor import write_task
from codekiln.validation.verifier_sandbox import (
    VerifierError,
    build_hardened_run_args,
    extract_agent_diff,
    validate_agent_diff,
    verifier_image_tag,
    verify_in_separate_container,
)


def _staged(root: Path, name: str = "click-echo-uppercase") -> Path:
    return write_task(
        root / name,
        instruction=INSTRUCTION,
        reference_diff=DIFF,
        feature_tests=FEATURE_TESTS,
        f2p=[REQ1, REQ2],
        p2p=[],
        repo_url=REPO,
        base_commit=SHA,
        category="api_cli",
        difficulty="L2",
    )


def _cp(args: list[str], rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["docker", *args], returncode=rc, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize(
    "bad_diff",
    [
        "diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1 +1 @@\n-a\n+b\n",
        "diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+x=1\n",
        "diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -1 +1 @@\n-a\n+b\n",
        "diff --git a/sitecustomize.py b/sitecustomize.py\nnew file mode 100644\n--- /dev/null\n+++ b/sitecustomize.py\n@@ -0,0 +1 @@\n+x=1\n",
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n",
    ],
)
def test_validate_agent_diff_blocks_test_infra_and_hooks(bad_diff: str) -> None:
    reason = validate_agent_diff(bad_diff)
    assert reason is not None and "forbidden" in reason


def test_validate_agent_diff_allows_reference_and_empty() -> None:
    assert validate_agent_diff("") is None
    assert validate_agent_diff(DIFF) is None


def test_build_hardened_run_args_enforces_isolation(tmp_path: Path) -> None:
    vin, vout = tmp_path / "in", tmp_path / "out"
    vin.mkdir()
    vout.mkdir()
    args = build_hardened_run_args(
        image="codekiln/verifier:test",
        verifier_in=vin,
        verifier_out=vout,
        memory="4g",
        cpus=2,
    )
    joined = " ".join(args)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 256" in joined
    assert ":/verifier_in:ro" in joined
    assert ":/logs/verifier:rw" in joined


def test_verify_in_separate_container_blocks_bad_diff_without_running_docker(tmp_path: Path) -> None:
    task = _staged(tmp_path)
    called: list[list[str]] = []

    def runner(args: list[str], _timeout: int | None) -> subprocess.CompletedProcess[str]:
        called.append(args)
        return _cp(args, 0)

    bad = "diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+x=1\n"
    res = verify_in_separate_container(task, bad, tmp_path / "work", runner=runner)
    assert not res.ok and not res.diff_allowed and res.reward == 0.0
    assert "conftest.py" in res.blocked_reason
    assert called == []


def test_verify_in_separate_container_builds_and_reads_verifier_outputs(tmp_path: Path) -> None:
    task = _staged(tmp_path)
    calls: list[list[str]] = []

    def fake_docker(args: list[str], _timeout: int | None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return _cp(args, 1)
        if args[0] == "build":
            return _cp(args, 0)
        if args[0] == "run":
            mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
            vin = Path(next(m for m in mounts if ":/verifier_in:ro" in m).split(":/verifier_in:ro")[0])
            vout = Path(next(m for m in mounts if ":/logs/verifier:rw" in m).split(":/logs/verifier:rw")[0])
            assert (vin / "agent.diff").read_text(encoding="utf-8") == DIFF
            assert (vin / "tests" / "feature" / "test_echo_uppercase.py").is_file()
            (vout / "reward.txt").write_text("1.0", encoding="utf-8")
            (vout / "results.json").write_text(json.dumps({REQ1: "passed", REQ2: "passed"}), encoding="utf-8")
            return _cp(args, 0)
        raise AssertionError(f"unexpected docker args: {args}")

    res = verify_in_separate_container(task, DIFF, tmp_path / "work", runner=fake_docker)
    assert res.ok and res.diff_allowed and res.container_isolated
    assert res.reward == 1.0
    assert res.results == {REQ1: "passed", REQ2: "passed"}
    assert res.image == verifier_image_tag(task)
    assert [c[0] for c in calls] == ["image", "build", "run"]


def test_extract_agent_diff_and_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _staged(tmp_path)
    trial = tmp_path / "trial"
    (trial / "sub").mkdir(parents=True)
    (trial / "sub" / "agent.diff").write_text(DIFF, encoding="utf-8")
    assert extract_agent_diff(trial) == DIFF

    empty = tmp_path / "empty_trial"
    empty.mkdir()
    with pytest.raises(VerifierError, match="no agent.diff"):
        extract_agent_diff(empty)
    with pytest.raises(VerifierError, match="does not exist"):
        extract_agent_diff(tmp_path / "missing_trial")

    def fake_docker(args: list[str], _timeout: int | None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["image", "inspect"]:
            return _cp(args, 0, stdout="sha256:abc\n")
        if args[0] == "run":
            mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
            vout = Path(next(m for m in mounts if ":/logs/verifier:rw" in m).split(":/logs/verifier:rw")[0])
            (vout / "reward.txt").write_text("1.0", encoding="utf-8")
            (vout / "results.json").write_text(json.dumps({REQ1: "passed", REQ2: "passed"}), encoding="utf-8")
            return _cp(args, 0)
        raise AssertionError(args)

    monkeypatch.setattr("codekiln.validation.verifier_sandbox._run_docker", fake_docker)
    out = tmp_path / "verifier_receipt.json"
    res = CliRunner().invoke(
        app,
        ["verify-isolated", str(task), "--trial-dir", str(trial), "--work", str(tmp_path / "w"), "--out", str(out)],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is True and data["container_isolated"] is True


def test_docker_output_is_decoded_as_utf8_not_the_platform_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`text=True` decodes with the platform default (cp1252 on Windows), so any non-ASCII byte in a
    test's output would crash the verifier there; the decoding must be explicit and forgiving."""
    import subprocess

    from codekiln.validation import verifier_sandbox

    seen: dict[str, object] = {}

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(verifier_sandbox.subprocess, "run", fake_run)
    verifier_sandbox._run_docker(["version"], 5)
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace" and "text" not in seen
