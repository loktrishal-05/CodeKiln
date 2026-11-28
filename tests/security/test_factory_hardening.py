import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# On Windows `bash` is often WSL, which cannot see Windows paths; CI (Linux) runs these.
needs_posix_bash = pytest.mark.skipif(os.name == "nt", reason="bash may resolve to WSL on Windows")


def test_dockerignore_excludes_secrets_and_runtime_outputs():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for required in (".env", "*.sqlite*", "workspaces/", "runs/", "staging/", "packages/"):
        assert required in ignore


def test_factory_dockerfile_hardening():
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in df
    assert "USER codekiln" in df
    assert 'ENTRYPOINT ["codekiln"]' in df
    assert not re.search(r"(ANTHROPIC_API_KEY|OPENAI_API_KEY|CODEKILN_LLM_API_KEY)\s*=", df)


def test_ci_workflow_hardening():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "permissions:" in ci and "contents: read" in ci
    assert "Secret & env guard" in ci
    assert "docker build -t codekiln-factory:ci ." in ci
    assert "ruff check ." in ci and "mypy" in ci and "pytest" in ci


@needs_posix_bash
def test_demo_run_script_syntax_and_dry_run(tmp_path: Path):
    script = ROOT / "scripts" / "demo_run.sh"
    assert script.is_file()
    if os.name != "nt":
        assert os.access(script, os.X_OK)
    bash = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
    assert bash.returncode == 0, bash.stderr

    help_res = subprocess.run([str(script), "--help"], capture_output=True, text=True, check=False)
    assert help_res.returncode == 0
    assert "--dry-run" in help_res.stdout

    repo = tmp_path / "sample_repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (repo / "mod.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    out_dir = tmp_path / "demo_out"
    env = os.environ.copy()
    env["PYTHON_BIN"] = sys.executable
    dry = subprocess.run(
        [str(script), "--repo", str(repo), "--slots", "1", "--out", str(out_dir), "--dry-run"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert (out_dir / "codekiln.sqlite").is_file()
    assert "Dry run complete:" in dry.stdout


FAKE_CODEKILN = """#!/usr/bin/env bash
# Stand-in CLI: factory packages one task, then exits 1 as it does when some candidate was rejected.
cmd="$1"; shift
case "$cmd" in
  run) "$PYTHON_BIN" -c "import os, sqlite3; c = sqlite3.connect(os.environ['CODEKILN_DB_PATH']); c.execute('CREATE TABLE IF NOT EXISTS factory_runs (id INTEGER PRIMARY KEY)'); c.execute('INSERT INTO factory_runs DEFAULT VALUES'); c.commit()" ;;
  factory) "$PYTHON_BIN" -c "import os, sqlite3; c = sqlite3.connect(os.environ['CODEKILN_DB_PATH']); c.execute('CREATE TABLE IF NOT EXISTS factory_runs (id INTEGER PRIMARY KEY)'); c.execute('INSERT INTO factory_runs DEFAULT VALUES'); c.commit()"
    while [ $# -gt 0 ]; do [ "$1" = "--dest" ] && mkdir -p "$2/click-task-a"; shift; done; exit 1 ;;
  report) while [ $# -gt 0 ]; do [ "$1" = "--out" ] && touch "$2"; shift; done ;;
esac
exit 0
"""


@needs_posix_bash
def test_demo_run_keeps_going_when_some_candidates_are_rejected(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("codekiln", FAKE_CODEKILN), ("harbor", "#!/usr/bin/env bash\nexit 0\n"),
                       ("docker", "#!/usr/bin/env bash\nexit 0\n")):
        (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
        (bin_dir / name).chmod(0o755)
    env = os.environ.copy()
    env.update({"PYTHON_BIN": sys.executable, "CODEKILN_BIN": str(bin_dir / "codekiln"),
                "CODEKILN_LLM_API_KEY": "sk-test",
                "PATH": str(bin_dir) + os.pathsep + env.get("PATH", "")})
    res = subprocess.run(
        ["bash", str(ROOT / "scripts" / "demo_run.sh"), "--out", str(tmp_path / "out")],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Packaged 1 task(s)" in res.stdout
    assert "Static HTML report" in res.stdout

