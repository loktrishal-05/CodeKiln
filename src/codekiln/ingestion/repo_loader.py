"""Load an untrusted repo (git URL, directory or zip) into DEST and pin its base commit.

Trust boundary 1 (docs/SPEC.md): git hooks are disabled, zip-slip paths and escaping
symlinks are rejected. Nothing from the source is executed.
"""
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

_NO_HOOKS = ["-c", f"core.hooksPath={os.devnull}", "-c", "protocol.ext.allow=never"]
_IDENTITY = ["-c", "user.name=codekiln", "-c", "user.email=codekiln@localhost"]
_URL_PREFIXES = ("https://", "http://", "ssh://", "git://", "git@")


class IngestionError(Exception):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(
        ["git", *_NO_HOOKS, *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if r.returncode:
        raise IngestionError(f"git {args[0]} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _reject_escaping_symlinks(root: Path) -> None:
    base = root.resolve()
    for p in root.rglob("*"):
        if ".git" in p.relative_to(root).parts:
            continue
        if p.is_symlink() and not p.resolve().is_relative_to(base):
            raise IngestionError(f"symlink escapes repo: {p.relative_to(root)}")


def _safe_extract(zip_path: Path, dest: Path) -> None:
    base = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(base):
                raise IngestionError(f"zip-slip path rejected: {info.filename}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise IngestionError(f"zip symlink rejected: {info.filename}")
        # ponytail: no decompressed-size cap (zip bombs); add one if untrusted zips are accepted
        z.extractall(dest)


def _init_snapshot(dest: Path) -> None:
    """A tree with no history becomes a one-commit repo so it still has a base_commit."""
    _git("init", "-q", cwd=dest)
    _git("add", "-A", cwd=dest)
    _git(*_IDENTITY, "commit", "-q", "-m", "snapshot", cwd=dest)


def load_repo(source: str, dest: Path, commit: str | None = None) -> str:
    """Materialise SOURCE into DEST (must not exist) and return the pinned base_commit SHA."""
    if dest.exists():
        raise IngestionError(f"dest already exists: {dest}")
    src = Path(source)
    try:
        if source.startswith(_URL_PREFIXES):
            _git("clone", "-q", "--", source, str(dest))
        elif src.is_dir():
            if (src / ".git").exists():
                # clone, never copy: the source .git/config is untrusted
                _git("clone", "-q", "--no-hardlinks", "--", str(src.resolve()), str(dest))
            else:
                shutil.copytree(src, dest, symlinks=True)
                _reject_escaping_symlinks(dest)
                _init_snapshot(dest)
        elif src.is_file() and zipfile.is_zipfile(src):
            dest.mkdir(parents=True)
            _safe_extract(src, dest)
            _reject_escaping_symlinks(dest)
            if not (dest / ".git").exists():
                _init_snapshot(dest)
        else:
            raise IngestionError(f"unsupported source: {source}")
        _git("config", "core.hooksPath", os.devnull, cwd=dest)
        if commit:
            if commit.startswith("-"):  # a pin goes to `git checkout`: never let it read as an option
                raise IngestionError(f"invalid commit/ref: {commit!r}")
            _git("checkout", "-q", "--detach", commit, cwd=dest)
        _reject_escaping_symlinks(dest)
        return _git("rev-parse", "HEAD", cwd=dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
