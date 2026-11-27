"""Issue #46: model-written wrong solutions join the probes; code decides what counts."""
import json
import sqlite3
from pathlib import Path

import pytest
from spec_examples import DIFF, INSTRUCTION, REPO, SHA, make_spec
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.db import connect, idempotency_key
from codekiln.planner.models import Category, Plan, dump_plan
from codekiln.states import State
from codekiln.synthesis.emit import write_package
from codekiln.synthesis.llm_router import ModelConfig, RouterError
from codekiln.validation import adversarial as adv
from codekiln.validation.mutation_probe import Mutant, probe_package

WRONG_DIFF = (
    "diff --git a/src/click/utils.py b/src/click/utils.py\n"
    "--- a/src/click/utils.py\n"
    "+++ b/src/click/utils.py\n"
    "@@ -1 +1,2 @@\n"
    "-def echo():\n"
    "+def echo(uppercase=False):\n"
    "+    return None\n"
)
OTHER_DIFF = (
    "diff --git a/src/click/other.py b/src/click/other.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/click/other.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+def other():\n"
    "+    return 1\n"
    "+    return 2\n"
)
BAD_DIFF = (
    "diff --git a/src/click/utils.py b/src/click/utils.py\n"
    "--- a/src/click/utils.py\n"
    "+++ b/src/click/utils.py\n"
    "@@ -1 +1,9 @@\n"
    "+def echo(uppercase=False):\n"
    "+    pass\n"
)


class FakeWriter:
    def __init__(self, diffs: list[str]) -> None:
        self.diffs = diffs
        self.calls = 0

    def write(self, instruction: str, reference_diff: str) -> str:
        diff = self.diffs[self.calls % len(self.diffs)]
        self.calls += 1
        return diff


@pytest.fixture
def package(tmp_path: Path) -> Path:
    return write_package(tmp_path / "task", make_spec(), repo_url=REPO, base_commit=SHA)


def test_propose_mutants_admits_only_valid_distinct_near_reference_diffs() -> None:
    writer = FakeWriter([BAD_DIFF, DIFF, WRONG_DIFF, OTHER_DIFF, WRONG_DIFF])
    mutants, rejected = adv.propose_mutants(INSTRUCTION, DIFF, writer, n=5)
    # Names keep the proposal index so a skipped mutant is traceable in the report.
    assert [m.name for m in mutants] == ["adversarial-2"]
    assert mutants[0].kind == "adversarial" and mutants[0].diff == WRONG_DIFF
    assert len(rejected) == 4
    assert rejected[0].startswith("adversarial-0:") and "invalid patch" in rejected[0]
    assert rejected[1].startswith("adversarial-1:") and "copied the reference" in rejected[1]
    assert rejected[2].startswith("adversarial-3:") and "not near the reference" in rejected[2]
    assert rejected[3].startswith("adversarial-4:") and "duplicate" in rejected[3]
    with pytest.raises(adv.AdversarialError, match="at least one"):
        adv.propose_mutants(INSTRUCTION, DIFF, writer, n=0)


def test_llm_writer_cleans_a_fenced_mutant(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        calls.append(payload)
        return {
            "choices": [{"message": {"content": "```diff\n" + WRONG_DIFF + "\n```"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }

    config = ModelConfig(base_url="https://provider.invalid/v1", api_key="sk-test", model="fake")
    assert adv.LLMAdversarialWriter(config, post=post).write(INSTRUCTION, DIFF) == WRONG_DIFF
    assert len(calls) == 1


def test_surviving_adversarial_mutant_fails_the_dossier(package: Path, tmp_path: Path) -> None:
    mutant = Mutant("adversarial-0", "adversarial", WRONG_DIFF)

    def run(task: Path, out: Path) -> float:
        return 1.0 if task.name == mutant.name else 0.0

    dossier = probe_package(package, tmp_path / "work", run=run, extra_mutants=(mutant,))
    assert any(p.name == mutant.name and not p.killed for p in dossier.probes)
    assert any("adversarial mutant survived: ['adversarial-0']" in r for r in dossier.reasons)
    assert not dossier.ok
    assert json.loads(dossier.to_json())["probes"][-1]["kind"] == "adversarial"


def test_killed_adversarial_mutant_keeps_the_dossier_green(package: Path, tmp_path: Path) -> None:
    mutant = Mutant("adversarial-0", "adversarial", WRONG_DIFF)
    dossier = probe_package(package, tmp_path / "work", run=lambda *_: 0.0, extra_mutants=(mutant,))
    assert dossier.ok
    assert all(p.killed for p in dossier.probes)


def test_cli_generates_meters_and_reports_an_adversarial_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = write_package(tmp_path / "task", make_spec(), repo_url=REPO, base_commit=SHA)
    db = tmp_path / "k.sqlite"
    con = connect(str(db))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES (?, ?)", (REPO, SHA))
    rid = con.execute("SELECT id FROM repositories").fetchone()[0]
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (?, 'dual', 1)", (rid,))
    run_id = con.execute("SELECT id FROM factory_runs").fetchone()[0]
    key = idempotency_key(REPO, SHA, "dual", 0)
    con.execute(
        "INSERT INTO task_candidates (run_id, idempotency_key, category, status)"
        " VALUES (?, ?, 'api_cli', ?)", (run_id, key, State.STATE_C_ALL_PASS.value),
    )
    cid = con.execute("SELECT id FROM task_candidates").fetchone()[0]
    plan = Plan(Category.API_CLI, frozenset({"src/click/utils.py"}), frozenset({"echo"}), "forward")
    con.execute(
        "INSERT INTO task_plans (candidate_id, plan_json, package_path) VALUES (?, ?, ?)",
        (cid, dump_plan(plan), str(package)),
    )
    con.commit()
    con.close()

    def fake_post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        return {
            "choices": [{"message": {"content": WRONG_DIFF}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

    def fake_probe(task: Path, out: Path) -> float:
        return 1.0 if task.name == "adversarial-0" else 0.0

    monkeypatch.setattr("codekiln.synthesis.llm_router.http_post", fake_post)
    monkeypatch.setattr("codekiln.validation.mutation_probe.harbor_probe", fake_probe)
    monkeypatch.setenv("CODEKILN_DB_PATH", str(db))
    monkeypatch.setenv("CODEKILN_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("CODEKILN_BILLING_LEDGER", str(tmp_path / "billing.csv"))
    out = tmp_path / "dossier.json"
    res = CliRunner().invoke(app, [
        "probe", str(package), "--adversarial", "1", "--run-id", str(run_id),
        "--work", str(tmp_path / "work"), "--out", str(out),
    ])
    assert res.exit_code == 1, res.output
    data = json.loads(out.read_text())
    assert any(p["kind"] == "adversarial" and not p["killed"] for p in data["probes"])
    assert any("adversarial mutant survived" in r for r in data["reasons"])
    assert "LLM_DEBIT" in (tmp_path / "billing.csv").read_text()
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT status FROM task_candidates").fetchone()[0] == (
            State.STATE_C_ALL_PASS.value
        )
    finally:
        con.close()


def test_cli_reports_a_provider_failure_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = write_package(tmp_path / "task", make_spec(), repo_url=REPO, base_commit=SHA)
    db = tmp_path / "k.sqlite"
    connect(str(db)).close()  # the ledger only has to exist for this path

    def down(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        raise RouterError("provider returned HTTP 503")

    monkeypatch.setattr("codekiln.synthesis.llm_router.http_post", down)
    monkeypatch.setenv("CODEKILN_DB_PATH", str(db))
    monkeypatch.setenv("CODEKILN_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("CODEKILN_BILLING_LEDGER", str(tmp_path / "billing.csv"))
    res = CliRunner().invoke(app, [
        "probe", str(package), "--adversarial", "1", "--run-id", "1",
        "--work", str(tmp_path / "work"), "--out", str(tmp_path / "dossier.json"),
    ])
    assert res.exit_code == 2, res.output
    assert "adversarial proposal failed: provider returned HTTP 503" in res.output
    assert isinstance(res.exception, SystemExit)  # a clean exit, not a traceback
