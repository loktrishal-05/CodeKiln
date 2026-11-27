"""#34 prerequisite: the base commit can be pinned, so a run starts from the judges' click 8.5.1."""
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.db import connect
from codekiln.ingestion.repo_loader import IngestionError, load_repo
from codekiln.pipeline import run_pipeline


def git(repo: Path, *args: str) -> str:
    cfg = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false"]
    return subprocess.run(["git", *cfg, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def two_commit_repo(root: Path) -> tuple[Path, str, str]:
    root.mkdir()
    git(root, "init", "-q")
    (root / "a.py").write_text("def one():\n    return 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    first = git(root, "rev-parse", "HEAD")
    (root / "b.py").write_text("def two():\n    return 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "second")
    return root, first, git(root, "rev-parse", "HEAD")


def test_the_run_starts_from_the_pinned_commit_not_the_branch_tip(tmp_path: Path) -> None:
    repo, first, tip = two_commit_repo(tmp_path / "r")
    con = connect(str(tmp_path / "k.sqlite"))
    try:
        pinned = run_pipeline(str(repo), 1, con, tmp_path / "ws1", commit=first)
        latest = run_pipeline(str(repo), 1, con, tmp_path / "ws2")
    finally:
        con.close()
    assert pinned.base_commit == first and latest.base_commit == tip != first
    assert not (pinned.workspace / "b.py").exists() and (latest.workspace / "b.py").exists()


def test_a_pin_that_looks_like_a_git_option_is_refused(tmp_path: Path) -> None:
    repo, _, _ = two_commit_repo(tmp_path / "r")
    with pytest.raises(IngestionError, match="invalid commit"):
        load_repo(str(repo), tmp_path / "dest", "--upload-pack=evil")
    assert not (tmp_path / "dest").exists()  # a refused load leaves nothing behind


def test_cli_pin_is_passed_through_and_a_bad_one_fails_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, first, _ = two_commit_repo(tmp_path / "r")
    monkeypatch.setenv("CODEKILN_DB_PATH", str(tmp_path / "k.sqlite"))
    ok = CliRunner().invoke(app, ["run", str(repo), "--slots", "1", "--workdir", str(tmp_path / "ws"), "--commit", first])
    assert ok.exit_code == 0 and first[:12] in ok.output
    bad = CliRunner().invoke(app, ["run", str(repo), "--slots", "1", "--workdir", str(tmp_path / "ws2"), "--commit=--evil"])
    assert bad.exit_code == 1 and "invalid commit" in bad.output  # the loader refused it; no traceback
