"""#42: a feature a later release already has is flagged; common names do not cause false alarms."""
import json
from pathlib import Path

from spec_examples import DIFF, REPO, SHA, make_spec
from test_factory import drive
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.synthesis.emit import write_package
from codekiln.validation.firewall import Marker, check, markers

NEW_FUNC = (
    "diff --git a/src/click/utils.py b/src/click/utils.py\n"
    "--- a/src/click/utils.py\n"
    "+++ b/src/click/utils.py\n"
    "@@ -1 +1,7 @@\n"
    " import os\n"
    "+def reverse_text(value: str, keep_case=True):\n"
    "+    return value[::-1]\n"
    "+class TextFlipper:\n"
    "+    pass\n"
    "+def _internal(x):\n"
    "+    return x\n"
)


def package(tmp_path: Path, diff: str = DIFF) -> Path:
    return write_package(tmp_path / "task", make_spec(reference_diff=diff), repo_url=REPO, base_commit=SHA)


def later_release(root: Path, source: str, notes: str = "") -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "utils.py").write_text(source)
    if notes:
        (root / "CHANGES.rst").write_text(notes)
    return root


def test_markers_are_the_public_api_the_diff_adds() -> None:
    assert markers(DIFF) == [Marker("param", "uppercase", "echo")]  # new parameter on an existing def
    names = {(m.kind, m.name) for m in markers(NEW_FUNC)}
    assert names == {("def", "reverse_text"), ("def", "TextFlipper")}  # private helper ignored


def test_a_new_parameter_already_in_a_later_signature_is_flagged(tmp_path: Path) -> None:
    corpus = later_release(tmp_path / "later", "def echo(message=None, nl=True, uppercase=False):\n    pass\n")
    report = check(package(tmp_path), [corpus])
    assert not report.ok and report.hits[0].marker == "echo(uppercase=...)"
    assert report.hits[0].line == 1


def test_a_common_name_alone_is_not_a_hit(tmp_path: Path) -> None:
    # `echo` exists in every release, but without the new parameter the feature is not there.
    corpus = later_release(tmp_path / "later", "def echo(message=None, nl=True):\n    pass\n")
    assert check(package(tmp_path), [corpus]).ok


def test_a_new_function_or_class_is_matched_in_source_and_in_the_changelog(tmp_path: Path) -> None:
    src = later_release(tmp_path / "a", "class TextFlipper:\n    pass\n")
    notes = later_release(tmp_path / "b", "x = 1\n", "Added `reverse_text` helper.\n")
    unrelated = later_release(tmp_path / "c", "def other():\n    pass\n", "Fixed a typo.\n")
    task = package(tmp_path, NEW_FUNC)
    assert {h.marker for h in check(task, [src]).hits} == {"TextFlipper"}
    assert {h.marker for h in check(task, [notes]).hits} == {"reverse_text"}
    assert check(task, [unrelated]).ok


def test_cli_exits_1_on_contamination_and_0_when_clear(tmp_path: Path) -> None:
    task = package(tmp_path)
    bad = later_release(tmp_path / "bad", "def echo(m, uppercase=False):\n    pass\n")
    good = later_release(tmp_path / "good", "def echo(m):\n    pass\n")
    res = CliRunner().invoke(app, ["firewall", str(task), "--corpus", str(bad)])
    assert res.exit_code == 1 and "CONTAMINATED" in res.output
    assert json.loads((task / "firewall.json").read_text())["ok"] is False
    assert CliRunner().invoke(app, ["firewall", str(task), "--corpus", str(good)]).exit_code == 0
    assert CliRunner().invoke(app, ["firewall", str(task), "--corpus", str(tmp_path / "nope")]).exit_code == 2


def test_the_factory_rejects_a_contaminated_candidate_before_spending_probe_time(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    corpus = later_release(tmp_path / "later", "def echo(m, uppercase=False):\n    pass\n")
    out, _, _ = drive(tmp_path / "run", corpus=[corpus])
    assert not out.packaged and len(out.rejected) == 3
    assert all(r.startswith("firewall: already in a later release") for r in out.rejected.values())


def test_a_renamed_parameter_upstream_is_a_near_match_not_a_rejection(tmp_path: Path) -> None:
    corpus = later_release(tmp_path / "later", "def echo(message=None, nl=True, upper=False):\n    pass\n")
    report = check(package(tmp_path), [corpus])  # ours adds `uppercase`; upstream spells it `upper`
    assert report.ok and report.hits == ()  # only a warning: a similar name is not proof
    assert [(n.marker, n.similar_to) for n in report.near] == [("echo(uppercase=...)", "echo(upper=...)")]


def test_a_renamed_function_or_class_is_a_near_match(tmp_path: Path) -> None:
    corpus = later_release(tmp_path / "later", "def reversed_text(v):\n    pass\nclass TextFlipperBase:\n    pass\n")
    report = check(package(tmp_path, NEW_FUNC), [corpus])
    assert report.ok
    assert {(n.marker, n.similar_to) for n in report.near} == {
        ("reverse_text", "reversed_text"), ("TextFlipper", "TextFlipperBase")}


def test_unrelated_names_and_exact_hits_do_not_add_near_noise(tmp_path: Path) -> None:
    unrelated = later_release(tmp_path / "a", "def echo(message=None, color=None, err=False):\n    pass\n")
    assert check(package(tmp_path), [unrelated]).near == ()
    exact = later_release(tmp_path / "b", "def echo(m, uppercase=False, upper=False):\n    pass\n")
    report = check(package(tmp_path), [exact])
    assert not report.ok and report.near == ()  # an exact hit already rejects; no double reporting


def test_cli_prints_near_matches_as_warnings_and_still_exits_zero(tmp_path: Path) -> None:
    task = package(tmp_path)
    corpus = later_release(tmp_path / "later", "def echo(m, upper=False):\n    pass\n")
    res = CliRunner().invoke(app, ["firewall", str(task), "--corpus", str(corpus)])
    assert res.exit_code == 0 and "clear" in res.output and "NEAR echo(uppercase=...)" in res.output
    assert json.loads((task / "firewall.json").read_text())["near"][0]["similar_to"] == "echo(upper=...)"
