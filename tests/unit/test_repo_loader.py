import subprocess
import zipfile
from pathlib import Path

import pytest

from codekiln.ingestion.repo_loader import IngestionError, load_repo


def _git(cwd: Path, *args: str) -> str:
    cfg = ["-c", "user.name=t", "-c", "user.email=t@t"]
    r = subprocess.run(["git", *cfg, *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "src"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    return repo


def test_git_dir_pins_head_and_disables_hooks(git_repo: Path, tmp_path: Path):
    sha = _git(git_repo, "rev-parse", "HEAD")
    dest = tmp_path / "out"
    assert load_repo(str(git_repo), dest) == sha
    assert _git(dest, "config", "core.hooksPath") in ("/dev/null", "nul", "NUL")


def test_explicit_commit_is_pinned(git_repo: Path, tmp_path: Path):
    first = _git(git_repo, "rev-parse", "HEAD")
    (git_repo / "a.py").write_text("x = 2\n")
    _git(git_repo, "commit", "-qam", "two")
    dest = tmp_path / "out"
    assert load_repo(str(git_repo), dest, commit=first) == first
    assert (dest / "a.py").read_text() == "x = 1\n"


def test_plain_dir_becomes_single_commit(tmp_path: Path):
    src = tmp_path / "plain"
    src.mkdir()
    (src / "m.py").write_text("y = 1\n")
    dest = tmp_path / "out"
    load_repo(str(src), dest)
    assert _git(dest, "rev-list", "--count", "HEAD") == "1"


def test_zip_slip_rejected_and_dest_cleaned(tmp_path: Path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../escape.txt", "boom")
    dest = tmp_path / "out"
    with pytest.raises(IngestionError, match="zip-slip"):
        load_repo(str(z), dest)
    assert not dest.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_zip_ok(tmp_path: Path):
    z = tmp_path / "ok.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("pkg/m.py", "z = 1\n")
    dest = tmp_path / "out"
    assert len(load_repo(str(z), dest)) == 40


def test_existing_dest_refused(git_repo: Path, tmp_path: Path):
    with pytest.raises(IngestionError, match="exists"):
        load_repo(str(git_repo), tmp_path)


def test_unsupported_source(tmp_path: Path):
    with pytest.raises(IngestionError, match="unsupported"):
        load_repo("ext::sh -c 'echo pwned'", tmp_path / "out")
