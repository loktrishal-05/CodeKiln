"""Issue #47: blind solvers disagree, and code turns that into spec questions."""
import json
from pathlib import Path

import pytest
from spec_examples import DIFF, REPO, SHA, make_spec
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.evaluation.blind_panel import PanelError, run_panel, write_report
from codekiln.evaluation.eval_runner import Trial
from codekiln.synthesis.emit import write_package
from codekiln.synthesis.models import load_spec, validate
from codekiln.synthesis.rtm import require_ok

TRUE_DIFF = DIFF.replace("uppercase=False", "uppercase=True").replace("+    pass", "+    return True")


@pytest.fixture
def package(tmp_path: Path) -> Path:
    return write_package(tmp_path / "task", make_spec(), repo_url=REPO, base_commit=SHA)


def fake_run(diffs: dict[str, str]):
    def run(task: Path, model: str, out: Path) -> Trial:
        out.mkdir(parents=True, exist_ok=True)
        return Trial(1.0 if model == "a" else 0.5, diffs[model], atif_path=str(out / "atif.json"))
    return run


def test_agreement_produces_no_ambiguities(package: Path, tmp_path: Path) -> None:
    report = run_panel(package, ["a", "b"], tmp_path / "work", fake_run({"a": DIFF, "b": DIFF}))
    assert report.ok and report.ambiguities == ()
    assert report.feedback == ""
    assert [a.model for a in report.answers] == ["a", "b"]
    assert report.answers[0].signatures == ("echo(uppercase=False)",)
    assert report.answers[0].files == ("src/click/utils.py",)
    assert json.loads(write_report(tmp_path / "r.json", report).read_text())["ok"]


def test_disagreement_becomes_a_spec_question(package: Path, tmp_path: Path) -> None:
    report = run_panel(package, ["a", "b"], tmp_path / "work", fake_run({"a": DIFF, "b": TRUE_DIFF}))
    topics = {a.topic for a in report.ambiguities}
    assert {"signature", "default", "literal"} <= topics
    assert "file" not in topics
    assert not report.ok
    assert "Which parameter defaults" in report.feedback
    assert any("a: uppercase=False" in o for a in report.ambiguities for o in a.observations)
    data = json.loads(report.to_json())
    assert data["spec_feedback"] == report.feedback
    assert len(data["answers"]) == 2


def test_three_model_panel_is_supported(package: Path, tmp_path: Path) -> None:
    report = run_panel(
        package, ["a", "b", "c"], tmp_path / "work",
        fake_run({"a": DIFF, "b": DIFF, "c": TRUE_DIFF}),
    )
    assert len(report.answers) == 3 and not report.ok


def test_panel_requires_distinct_models_and_a_clean_instruction(
    package: Path, tmp_path: Path
) -> None:
    with pytest.raises(PanelError, match="at least two distinct"):
        run_panel(package, ["a", "a"], tmp_path / "work", fake_run({"a": DIFF}))
    (package / "instruction.md").write_text(
        (package / "instruction.md").read_text() + "\nSee tests/feature for the hidden cases.\n"
    )
    with pytest.raises(PanelError, match="blind reader failed"):
        run_panel(package, ["a", "b"], tmp_path / "work", fake_run({"a": DIFF, "b": DIFF}))


def test_cli_writes_feedback_and_fails_on_ambiguity(
    package: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codekiln.evaluation.blind_panel.harbor_trial",
        fake_run({"claude-code": DIFF, "codex": TRUE_DIFF}),
    )
    out = tmp_path / "ambiguity.json"
    res = CliRunner().invoke(app, [
        "blind-panel", str(package), "--model", "claude-code", "--model", "codex",
        "--work", str(tmp_path / "work"), "--out", str(out),
    ])
    assert res.exit_code == 1, res.output
    data = json.loads(out.read_text())
    assert data["ok"] is False and data["spec_feedback"]
    assert "spec feedback" in res.output


def test_report_feedback_is_applied_to_the_spec_with_gates_rechecked(
    package: Path, tmp_path: Path
) -> None:
    report = run_panel(package, ["a", "b"], tmp_path / "work", fake_run({"a": DIFF, "b": TRUE_DIFF}))
    report_path = write_report(tmp_path / "ambiguity.json", report)
    spec_path = tmp_path / "spec.json"
    spec = make_spec()
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")
    out = tmp_path / "revised.json"
    res = CliRunner().invoke(app, ["revise-spec", str(spec_path), str(report_path), str(out)])
    assert res.exit_code == 0, res.output
    revised = load_spec(out)
    assert "Clarifications from blind solvers" in revised.instruction
    assert "Which parameter defaults" in revised.instruction
    validate(revised)
    require_ok(revised)
    assert revised.requirements == spec.requirements
