import json
from pathlib import Path
from typing import Any

import pytest
from spec_examples import DIFF, INSTRUCTION, REPO, SHA, make_spec
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.planner.models import Category, Plan
from codekiln.synthesis.emit import write_package
from codekiln.synthesis.models import FeatureSpec, Requirement, SynthesisError, from_dict, validate
from codekiln.synthesis.rtm import audit, defined_nodes, write_map
from codekiln.synthesis.spec_generator import render_prompt

_spec = make_spec


def _touch(path: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1,2 @@\n+x = 1\n"


def test_valid_spec_survives_json_roundtrip() -> None:
    spec = _spec()
    validate(spec)
    again = from_dict(json.loads(json.dumps(spec.to_dict())))
    assert again == spec
    assert audit(spec).ok


def test_rtm_maps_each_requirement_to_its_tests() -> None:
    m = audit(_spec())
    assert m.requirements == {
        "REQ-1": ["tests/test_echo_uppercase.py::test_req1_uppercases"],
        "REQ-2": ["tests/test_echo_uppercase.py::test_req2_runner"],
    }


def test_defined_nodes_handles_classes_and_parametrised_ids() -> None:
    src = "class TestEcho:\n    def test_upper(self):\n        pass\n"
    assert defined_nodes({"test_x.py": src}) == {"tests/test_x.py::TestEcho::test_upper"}
    node = "tests/test_echo_uppercase.py::test_req1_uppercases[case-1]"
    spec = _spec(
        f2p=(node, "tests/test_echo_uppercase.py::test_req2_runner"),
        requirements=(
            Requirement("REQ-1", "echo uppercases", (node,)),
            Requirement("REQ-2", "CliRunner sees it", ("tests/test_echo_uppercase.py::test_req2_runner",)),
        ),
    )
    assert audit(spec).ok


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"task": "Click Echo"}, "kebab-case"),
        ({"feature_tests": {"../../evil.py": ""}}, "test_x.py"),
        ({"f2p": ()}, "at least one fail_to_pass"),
        ({"f2p": ("tests/test_other.py::test_x",)}, "outside the feature tests"),
        ({"difficulty": "hard"}, "L1/L2/L3"),
        ({"reference_diff": "--- a/x\n+++ b/x\n"}, "diff --git"),
        ({"reference_diff": f"{DIFF}deleted file mode 100644\n"}, "delete files"),
        ({"reference_diff": DIFF + _touch("conftest.py")}, "test infrastructure"),
        ({"reference_diff": DIFF + _touch("tests/test_core.py")}, "test infrastructure"),
        ({"reference_diff": DIFF + _touch("pytest.ini")}, "test infrastructure"),
        ({"requirements": ()}, "at least one REQ"),
    ],
)
def test_validate_rejects_bad_specs(over: dict[str, Any], message: str) -> None:
    with pytest.raises(SynthesisError, match=message):
        validate(_spec(**over))


def test_validate_rejects_instruction_and_requirement_drift() -> None:
    with pytest.raises(SynthesisError, match="does not declare"):
        validate(_spec(instruction=INSTRUCTION.replace("- REQ-2:", "- second:")))
    with pytest.raises(SynthesisError, match="no Requirement"):
        validate(_spec(requirements=(Requirement("REQ-1", "only one", ()),)))


def test_rtm_reports_every_problem_not_just_the_first() -> None:
    spec = _spec(
        requirements=(
            Requirement("REQ-1", "echo uppercases", ()),
            Requirement("REQ-2", "CliRunner sees it", ("tests/test_echo_uppercase.py::test_ghost",)),
        ),
    )
    m = audit(spec)
    assert not m.ok
    assert any("REQ-1 has no fail_to_pass test" in p for p in m.problems)
    assert any("test_ghost" in p for p in m.problems)


def test_rtm_rejects_imports_the_instruction_never_names() -> None:
    m = audit(_spec(instruction=INSTRUCTION.replace("`CliRunner`", "the runner")))
    assert any("CliRunner" in p and "never names" in p for p in m.problems)


def test_rtm_rejects_f2p_ids_no_test_defines() -> None:
    spec = _spec(f2p=("tests/test_echo_uppercase.py::test_req1_uppercases", "tests/test_echo_uppercase.py::test_typo"))
    assert any("test_typo" in p for p in audit(spec).problems)


def test_write_map_is_json(tmp_path: Path) -> None:
    path = tmp_path / "rtm_map.json"
    m = write_map(path, _spec())
    data = json.loads(path.read_text())
    assert m.ok and data["ok"] is True and data["requirements"]["REQ-1"]


def test_write_package_emits_harbor_layout_with_rtm(tmp_path: Path) -> None:
    task = write_package(tmp_path / "pkg", _spec(), repo_url=REPO, base_commit=SHA)
    assert (task / "instruction.md").read_text() == INSTRUCTION
    assert json.loads((task / "tests" / "fail_to_pass.json").read_text())[0].startswith("tests/")
    assert json.loads((task / "rtm_map.json").read_text())["ok"] is True
    assert (task / "tests" / "feature" / "test_echo_uppercase.py").exists()


def test_write_package_writes_nothing_when_rtm_fails(tmp_path: Path) -> None:
    spec = _spec(requirements=(Requirement("REQ-1", "echo uppercases", ()), Requirement("REQ-2", "x", ("tests/test_echo_uppercase.py::test_req2_runner",))))
    with pytest.raises(SynthesisError, match="RTM audit failed"):
        write_package(tmp_path / "pkg", spec, repo_url=REPO, base_commit=SHA)
    assert not (tmp_path / "pkg").exists()


def _spec_file(tmp_path: Path, spec: FeatureSpec) -> Path:
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec.to_dict()), encoding="utf-8")
    return p


def test_cli_spec_check_writes_map(tmp_path: Path) -> None:
    res = CliRunner().invoke(app, ["spec-check", str(_spec_file(tmp_path, _spec()))])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "rtm_map.json").exists()


def test_cli_spec_check_exits_1_with_problems(tmp_path: Path) -> None:
    bad = _spec(requirements=(Requirement("REQ-1", "echo", ()), Requirement("REQ-2", "runner", ("tests/test_echo_uppercase.py::test_req2_runner",))))
    res = CliRunner().invoke(app, ["spec-check", str(_spec_file(tmp_path, bad))])
    assert res.exit_code == 1 and "REQ-1 has no fail_to_pass test" in res.output


def test_cli_spec_check_reports_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "spec.json"
    p.write_text("{not json", encoding="utf-8")
    res = CliRunner().invoke(app, ["spec-check", str(p)])
    assert res.exit_code == 1 and "not valid JSON" in res.output


def test_prompt_names_the_slot_and_wraps_repo_text() -> None:
    plan = Plan(Category.API_CLI, frozenset({"src/click/utils.py"}), frozenset({"echo"}), "forward", 2)
    prompt = render_prompt(plan, repo="click", base_commit=SHA)
    assert "src/click/utils.py" in prompt and "echo" in prompt
    assert "<untrusted_repo_content>" in prompt and prompt.count("</untrusted_repo_content>") == 1
    assert "REQ-1" in prompt and "api_cli" in prompt
