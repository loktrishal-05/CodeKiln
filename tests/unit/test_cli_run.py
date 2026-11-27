import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codekiln.cli import app

FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def add(a, b):\n    return a + b\n",
    "pkg/validate.py": "from pkg.core import add\n\ndef check(x):\n    return add(x, 1)\n",
    "pkg/db.py": "class Store:\n    pass\n",
    "pkg/cli.py": "from pkg import db\n\ndef main():\n    return db.Store()\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "target"
    for rel, text in FILES.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    cfg = ["-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "-A"], [*cfg, "commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    return r


def invoke(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slots: int = 3):
    monkeypatch.setenv("CODEKILN_DB_PATH", str(tmp_path / "k.sqlite"))
    return CliRunner().invoke(
        app, ["run", str(repo), "--slots", str(slots), "--workdir", str(tmp_path / "ws")]
    )


def rows(tmp_path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(tmp_path / "k.sqlite")
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_run_records_candidates_and_is_idempotent(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    first = invoke(repo, tmp_path, monkeypatch)
    assert first.exit_code == 0, first.output
    cands = rows(tmp_path, "SELECT status, difficulty, ast_fingerprint FROM task_candidates")
    assert 0 < len(cands) <= 3
    assert {c[0] for c in cands} == {"DIVERSITY_OK"} and all(c[1] and c[2] for c in cands)

    second = invoke(repo, tmp_path, monkeypatch)
    assert second.exit_code == 0 and "0 new" in second.output
    assert rows(tmp_path, "SELECT COUNT(*) FROM task_candidates") == [(len(cands),)]
    assert rows(tmp_path, "SELECT COUNT(*) FROM repositories") == [(1,)]
    assert len(list((tmp_path / "ws").iterdir())) == 1  # no leftover .tmp checkout


def test_run_bad_source_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    res = invoke(tmp_path / "nope", tmp_path, monkeypatch)
    assert res.exit_code == 1 and "ingestion failed" in res.output
