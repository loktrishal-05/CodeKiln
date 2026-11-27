import sqlite3
import subprocess
from pathlib import Path

import pytest
from spec_examples import INSTRUCTION, make_spec
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.planner.models import Category, Plan, dump_plan, load_plan
from codekiln.synthesis.llm_router import LLMWriter
from codekiln.synthesis.models import SynthesisError

FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def add(a, b):\n    return a + b\n",
    "pkg/validate.py": "from pkg.core import add\n\ndef check(x):\n    return add(x, 1)\n",
    "pkg/db.py": "class Store:\n    pass\n",
    "pkg/cli.py": "from pkg import db\n\ndef main():\n    return db.Store()\n",
    "pkg/format.py": "def show(x):\n    return str(x)\n",
}


def make_repo(root: Path) -> Path:
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    cfg = ["-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "-A"], [*cfg, "commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def invoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str):
    monkeypatch.setenv("CODEKILN_DB_PATH", str(tmp_path / "k.sqlite"))
    monkeypatch.setenv("CODEKILN_LLM_API_KEY", "sk-test")
    return CliRunner().invoke(app, list(args))


def run_and_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    res = invoke(monkeypatch, tmp_path, "run", str(make_repo(tmp_path / "target")), "--slots", "2",
                 "--workdir", str(tmp_path / "ws"))
    assert res.exit_code == 0, res.output
    con = sqlite3.connect(tmp_path / "k.sqlite")
    try:
        run_id = con.execute("SELECT MAX(id) FROM factory_runs").fetchone()[0]
        # the packager needs a git URL; a local dir is only an ingestion source
        con.execute("UPDATE repositories SET url = 'https://github.com/pallets/click'")
        con.commit()
        return run_id
    finally:
        con.close()


def rows(tmp_path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(tmp_path / "k.sqlite")
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def fake_success(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int] = {}

    def write(self: LLMWriter, plan: Plan, *, repo: str, base_commit: str):
        slug = plan.category.value.replace("_", "-")
        seen[slug] = seen.get(slug, 0) + 1
        return make_spec(task=f"click-{slug}-{seen[slug]}")

    monkeypatch.setattr(LLMWriter, "write", write)


def test_plan_json_roundtrip() -> None:
    plan = Plan(Category.CROSS_MODULE, frozenset({"a.py", "b.py"}), frozenset({"x"}), "forward", 3)
    assert load_plan(dump_plan(plan)) == plan
    with pytest.raises(ValueError):
        load_plan("{oops")


def test_run_persists_the_plan_for_every_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = run_and_id(monkeypatch, tmp_path)
    cands = rows(tmp_path, f"SELECT COUNT(*) FROM task_candidates WHERE run_id = {run_id}")[0][0]
    plans = rows(tmp_path, "SELECT plan_json FROM task_plans")
    assert len(plans) == cands > 0
    for (text,) in plans:
        assert load_plan(text).files


def test_synth_writes_packages_and_marks_synthesized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = run_and_id(monkeypatch, tmp_path)
    fake_success(monkeypatch)
    res = invoke(monkeypatch, tmp_path, "synth", str(run_id), "--packages", str(tmp_path / "pkg"))
    assert res.exit_code == 0, res.output
    assert "synthesized" in res.output
    statuses = rows(tmp_path, "SELECT status FROM task_candidates WHERE run_id = " + str(run_id))
    assert statuses and {s for (s,) in statuses} == {"BLIND_READER_OK"}
    assert rows(tmp_path, "SELECT COUNT(*) FROM task_plans WHERE spec_sha256 IS NOT NULL")[0][0] > 0
    for d in (tmp_path / "pkg").iterdir():
        assert (d / "task.toml").exists() and (d / "rtm_map.json").exists()


def test_synth_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = run_and_id(monkeypatch, tmp_path)
    fake_success(monkeypatch)
    assert invoke(monkeypatch, tmp_path, "synth", str(run_id)).exit_code == 0
    again = invoke(monkeypatch, tmp_path, "synth", str(run_id))
    assert again.exit_code == 0 and "0 synthesized" in again.output and "skipped" in again.output


def test_blind_reader_rejects_a_leaky_instruction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = run_and_id(monkeypatch, tmp_path)
    leaked = INSTRUCTION + "\nThe hidden tests live in tests/feature/.\n"

    def write(self: LLMWriter, plan: Plan, *, repo: str, base_commit: str):
        return make_spec(instruction=leaked)

    monkeypatch.setattr(LLMWriter, "write", write)
    res = invoke(monkeypatch, tmp_path, "synth", str(run_id), "--packages", str(tmp_path / "pkg"))
    assert res.exit_code == 1 and "BLIND_READER" in res.output
    statuses = rows(tmp_path, "SELECT status, rejection_reason FROM task_candidates")
    assert {s for s, _ in statuses} == {"REJECTED"}
    assert all("BLIND_READER" in (reason or "") for _, reason in statuses)
    assert not list((tmp_path / "pkg").iterdir())


def test_synth_dead_letters_after_three_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = run_and_id(monkeypatch, tmp_path)

    def always_reject(self: LLMWriter, plan: Plan, *, repo: str, base_commit: str):
        raise SynthesisError("model kept inventing REQ-9")

    monkeypatch.setattr(LLMWriter, "write", always_reject)
    for _ in range(3):
        res = invoke(monkeypatch, tmp_path, "synth", str(run_id))
        assert res.exit_code == 1, res.output
    statuses = rows(tmp_path, "SELECT status, rejection_reason FROM task_candidates")
    assert {s for s, _ in statuses} == {"REJECTED"}
    assert all(reason and "dead-letter" in reason for _, reason in statuses)


def test_synth_without_key_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = run_and_id(monkeypatch, tmp_path)
    monkeypatch.setenv("CODEKILN_LLM_API_KEY", "")
    res = CliRunner().invoke(app, ["synth", str(run_id), "--db", str(tmp_path / "k.sqlite")])
    assert res.exit_code == 2 and "API_KEY" in res.output
