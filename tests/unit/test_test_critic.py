"""Issue #45: the critic writes a second correct implementation; the verifier judges the tests."""
import json
import sqlite3
from pathlib import Path

import pytest
from spec_examples import DIFF, REPO, SHA, make_spec
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.db import connect, idempotency_key
from codekiln.planner.models import Category, Plan, dump_plan
from codekiln.states import State
from codekiln.synthesis.emit import write_package
from codekiln.synthesis.llm_router import ModelConfig
from codekiln.validation import test_critic as critic
from codekiln.validation.test_critic import (
    LLMAlternativeWriter,
    clean_diff,
    critique_tests,
    write_report,
)

ALT_DIFF = (
    "diff --git a/src/click/upper.py b/src/click/upper.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/click/upper.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+def echo(uppercase=False):\n"
    "+    if uppercase:\n"
    "+        return None\n"
)
BAD_DIFF = (
    "diff --git a/src/click/utils.py b/src/click/utils.py\n"
    "--- a/src/click/utils.py\n"
    "+++ b/src/click/utils.py\n"
    "@@ -1 +1,9 @@\n"
    "+def echo(uppercase=False):\n"
    "+    pass\n"
)


class FixedWriter:
    def __init__(self, diffs: list[str]) -> None:
        self.diffs = diffs
        self.calls: list[tuple[str, str]] = []

    def write(self, instruction: str, reference_diff: str) -> str:
        self.calls.append((instruction, reference_diff))
        return self.diffs[(len(self.calls) - 1) % len(self.diffs)]


@pytest.fixture
def package(tmp_path: Path) -> Path:
    return write_package(tmp_path / "task", make_spec(), repo_url=REPO, base_commit=SHA)


def test_clean_diff_normalises_fences_and_rejects_prose_or_bad_hunks() -> None:
    assert clean_diff("```diff\n" + ALT_DIFF + "\n```\n") == ALT_DIFF
    with pytest.raises(critic.CriticError, match="not a unified git diff"):
        clean_diff("this patch is perfect, trust me")
    with pytest.raises(critic.CriticError, match="invalid patch"):
        clean_diff(BAD_DIFF)


def test_correct_alternative_with_low_reward_flags_tests_as_too_narrow(
    package: Path, tmp_path: Path
) -> None:
    seen: list[str] = []

    def run(task: Path, out: Path) -> float:
        seen.append((task / "solution" / "reference.diff").read_text())
        out.mkdir(parents=True)
        return 0.5

    result = critique_tests(package, tmp_path / "work", FixedWriter([ALT_DIFF]), run=run)
    assert not result.ok and result.too_narrow
    assert result.alternatives[0].reward == 0.5
    assert "tests too narrow" in result.reasons[0] and "0.50" in result.reasons[0]
    assert seen == [ALT_DIFF]  # the trial really used the swapped patch
    assert (package / "solution" / "reference.diff").read_text() == DIFF  # original untouched
    assert json.loads(write_report(tmp_path / "out.json", result).read_text())["too_narrow"]


def test_alternative_with_full_reward_passes(package: Path, tmp_path: Path) -> None:
    result = critique_tests(package, tmp_path / "work", FixedWriter([ALT_DIFF]), run=lambda *_: 1.0)
    assert result.ok and not result.too_narrow
    assert result.alternatives[0].passed
    assert result.to_json().count('"too_narrow": false') == 2


def test_invalid_and_copied_patches_are_rejected_before_any_trial(
    package: Path, tmp_path: Path
) -> None:
    def run(*_: object) -> float:
        raise AssertionError("an invalid patch must never be executed")

    writer = FixedWriter([BAD_DIFF, DIFF])
    result = critique_tests(package, tmp_path / "work", writer, run=run, alternatives=2)
    assert not result.ok and not result.too_narrow
    reasons = [a.reason for a in result.alternatives]
    assert reasons[0].startswith("invalid alternative: invalid patch:")
    assert "'@@ -1 +1,9 @@' says (1, 9) but the body has (0, 2)" in reasons[0]
    assert reasons[1] == "invalid alternative: alternative copied the reference patch"
    assert "no valid alternative implementation could be executed" in result.reasons


def test_crashed_trial_is_not_counted_as_narrow(package: Path, tmp_path: Path) -> None:
    result = critique_tests(package, tmp_path / "work", FixedWriter([ALT_DIFF]), run=lambda *_: None)
    assert not result.ok and not result.too_narrow
    assert "trial crashed" in result.reasons[0]


def test_llm_writer_repairs_a_fenced_completion(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        calls.append(payload)
        return {
            "choices": [{"message": {"content": "```diff\n" + ALT_DIFF + "\n```"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        }

    config = ModelConfig(base_url="https://provider.invalid/v1", api_key="sk-test", model="fake")
    assert LLMAlternativeWriter(config, post=post).write("# Task\n", DIFF) == ALT_DIFF
    assert len(calls) == 1


def test_cli_meters_the_alternative_and_writes_a_narrow_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no network: the fixture package points at a real URL, and the writers then send raw diffs
    monkeypatch.setattr("codekiln.pipeline.checkout_for_package", lambda package, workdir: None)
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
        " VALUES (?, ?, 'api_cli', ?)", (run_id, key, State.BLIND_READER_OK.value),
    )
    cid = con.execute("SELECT id FROM task_candidates").fetchone()[0]
    plan = Plan(
        Category.API_CLI, frozenset({"src/click/utils.py"}), frozenset({"echo"}), "forward"
    )
    con.execute(
        "INSERT INTO task_plans (candidate_id, plan_json, package_path) VALUES (?, ?, ?)",
        (cid, dump_plan(plan), str(package)),
    )
    con.commit()
    con.close()

    def fake_post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        return {
            "choices": [{"message": {"content": ALT_DIFF}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

    monkeypatch.setattr("codekiln.synthesis.llm_router.http_post", fake_post)
    monkeypatch.setattr("codekiln.validation.test_critic.harbor_probe", lambda *_: 0.25)
    monkeypatch.setenv("CODEKILN_DB_PATH", str(db))
    monkeypatch.setenv("CODEKILN_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("CODEKILN_BILLING_LEDGER", str(tmp_path / "billing.csv"))
    out = tmp_path / "report.json"
    res = CliRunner().invoke(app, [
        "test-critic", str(package), str(run_id), "--work", str(tmp_path / "work"),
        "--out", str(out),
    ])
    assert res.exit_code == 1, res.output
    report = json.loads(out.read_text())
    assert report["too_narrow"] and report["alternatives"][0]["reward"] == 0.25
    csv_text = (tmp_path / "billing.csv").read_text()
    assert "LLM_DEBIT" in csv_text and "PREAUTH_HOLD" in csv_text
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT status FROM task_candidates").fetchone()[0] == (
            State.BLIND_READER_OK.value
        )
    finally:
        con.close()
