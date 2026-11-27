import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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

