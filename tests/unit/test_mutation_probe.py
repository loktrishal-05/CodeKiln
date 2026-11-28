import json
from pathlib import Path

import pytest
from spec_examples import DIFF
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.validation.mutation_probe import (
    ProbeError,
    build_mutants,
    dossier_from_mapping,
    probe_package,
    read_dossier,
)


def make_task(root: Path) -> Path:
    task = root / "task"
    (task / "solution").mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text('[task]\nname = "task"\n')
    (task / "solution" / "reference.diff").write_text(DIFF)
    return task


def all_killed(task: Path, out: Path) -> float:
    return 0.0


def test_build_mutants_has_all_four_classes() -> None:
    mutants = build_mutants(DIFF)
    assert {m.kind for m in mutants} == {"nop", "stub", "partial", "semantic"}
    assert sum(m.kind == "semantic" for m in mutants) >= 3
    assert all(m.diff.startswith("diff --git") for m in mutants)
    assert "return None" in next(m for m in mutants if m.kind == "stub").diff
    assert any("echo_mutant" in m.diff for m in mutants if m.kind == "semantic")


def test_probe_kills_everything(tmp_path: Path) -> None:
    d = probe_package(make_task(tmp_path), tmp_path / "work", all_killed)
    assert d.ok and d.semantic_kill_rate == 1.0
    assert all(p.killed for p in d.probes)
    assert not d.reasons
    payload = json.loads(d.to_json())
    assert payload["ok"] is True
    assert dossier_from_mapping(payload).probes == d.probes


def test_semantic_kill_rate_threshold(tmp_path: Path) -> None:
    def one_survivor(task: Path, out: Path) -> float:
        return 1.0 if "rename-def" in str(task) else 0.0

    d = probe_package(make_task(tmp_path), tmp_path / "work", one_survivor)
    assert not d.ok and "semantic kill rate 67%" in " ".join(d.reasons)
    assert probe_package(make_task(tmp_path), tmp_path / "work", one_survivor, 0.6).ok


def test_surviving_nop_is_reported(tmp_path: Path) -> None:
    def survivor(task: Path, out: Path) -> float:
        diff = (task / "solution" / "reference.diff").read_text()
        return 1.0 if "codekiln_noop" in diff else 0.0

    d = probe_package(make_task(tmp_path), tmp_path / "work", survivor)
    assert not d.ok and any("nop" in r for r in d.reasons)


def test_crashed_trial_is_reported(tmp_path: Path) -> None:
    def crash(task: Path, out: Path) -> None:
        return None

    d = probe_package(make_task(tmp_path), tmp_path / "work", crash)  # type: ignore[arg-type]
    assert not d.ok and any("trials crashed" in r for r in d.reasons)


def test_dossier_roundtrip_and_validation(tmp_path: Path) -> None:
    good = probe_package(make_task(tmp_path), tmp_path / "work", all_killed)
    p = tmp_path / "d.json"
    p.write_text(good.to_json())
    assert read_dossier(p) == good
    with pytest.raises(ProbeError):
        dossier_from_mapping({"probes": [{"kind": "magic"}]})


def test_cli_probe_writes_dossier_and_fails_on_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = make_task(tmp_path)
    monkeypatch.setattr(
        "codekiln.validation.harbor_runner.run",
        lambda path, out, agent="oracle", concurrency=4: {path.name: 0.0},
    )
    res = CliRunner().invoke(app, ["probe", str(task), "--work", str(tmp_path / "work")])
    assert res.exit_code == 0, res.output
    assert read_dossier(task / "mutation_dossier.json").ok

    monkeypatch.setattr(
        "codekiln.validation.harbor_runner.run",
        lambda path, out, agent="oracle", concurrency=4: {path.name: 1.0},
    )
    bad = CliRunner().invoke(app, ["probe", str(task), "--work", str(tmp_path / "work")])
    assert bad.exit_code == 1 and "semantic kill rate" in bad.output
    assert not read_dossier(task / "mutation_dossier.json").ok


def test_probe_rejects_non_package(tmp_path: Path) -> None:
    with pytest.raises(ProbeError, match="not a Harbor task"):
        probe_package(tmp_path / "empty", tmp_path / "work", all_killed)


BASE_UTILS = """import os

def echo(message, nl=True):
    if nl:
        print(message)
    else:
        print(message, end="")

def other():
    return 0
"""
NEW_UTILS = """import os

def echo(message, nl=True, upper=False):
    if upper and nl == True:
        message = message.upper()
    if nl:
        print(message)
    else:
        print(message, end="")

def other():
    return 0
"""


def test_mutants_of_a_realistic_diff_really_apply(tmp_path: Path) -> None:
    """Context lines and several files: a mutant that does not `git apply` scores 0.0 in Harbor,
    which would count as killed, so every mutant must be a valid patch."""
    import subprocess

    def git(*args: str) -> str:
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                           cwd=tmp_path, check=True, capture_output=True, text=True)
        return r.stdout

    (tmp_path / "src").mkdir()
    (tmp_path / "src/utils.py").write_text(BASE_UTILS, newline="")
    (tmp_path / "CHANGES.md").write_text("# Changes\n\n- old entry\n", newline="")
    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    (tmp_path / "src/utils.py").write_text(NEW_UTILS, newline="")
    (tmp_path / "CHANGES.md").write_text("# Changes\n\n- old entry\n- added upper\n", newline="")
    ref = git("diff")
    git("checkout", "-q", "--", ".")

    mutants = build_mutants(ref)
    assert sum(m.kind == "semantic" for m in mutants) >= 3
    for m in mutants:
        patch = tmp_path / "mutant.diff"
        patch.write_text(m.diff, newline="")
        git("apply", "--check", str(patch))  # raises CalledProcessError on a corrupt patch


def test_semantic_mutants_never_edit_a_docstring_or_a_comment() -> None:
    """Real run: the first `False` of a good patch was in its docstring, so the 'mutant' equalled the
    original, could not be killed, and good tests scored 0%. Only code may be mutated."""
    diff = (
        "diff --git a/src/m.py b/src/m.py\n--- a/src/m.py\n+++ b/src/m.py\n@@ -1,0 +1,9 @@\n"
        "+def f(x, strict=False):\n"
        '+    """Return x; strict defaults to ``False`` and 1 means one."""\n'
        "+    # True here is only a comment, x == 0 as well\n"
        "+    msg = 'x != 1 and True'\n"
        "+    if strict:\n"
        "+        return None\n"
        "+    return x\n"
    )
    sem = [m.diff for m in build_mutants(diff) if m.kind == "semantic"]
    assert sem, "the code line `strict=False` still gives mutants"
    for d in sem:
        body = [line for line in d.splitlines() if line.startswith("+") and not line.startswith("+++")]
        assert body[1].endswith('1 means one."""') and "``False``" in body[1]  # docstring untouched
        assert body[2] == "+    # True here is only a comment, x == 0 as well"
        assert body[3] == "+    msg = 'x != 1 and True'"


def test_patches_built_from_decisions_not_literals_still_get_enough_mutants() -> None:
    """Real run: a patch of isinstance/or/>= decisions had one literal, so only 2 mutants could be built."""
    diff = (
        "diff --git a/src/m.py b/src/m.py\n--- a/src/m.py\n+++ b/src/m.py\n@@ -1,0 +1,8 @@\n"
        "+def f(value, limit):\n"
        "+    ok = isinstance(limit, int)\n"
        "+    if not isinstance(value, str) or value is not None:\n"
        "+        raise TypeError(value)\n"
        "+    if len(value) >= limit:\n"
        "+        return value[:limit]\n"
        "+    return value\n"
    )
    names = {m.name for m in build_mutants(diff) if m.kind == "semantic"}
    assert len(names) >= 3 and "semantic-negate-isinstance" in names
