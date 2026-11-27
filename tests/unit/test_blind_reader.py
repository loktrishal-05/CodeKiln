import json
from pathlib import Path

import pytest
from spec_examples import INSTRUCTION
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.validation.blind_reader import (
    BlindReaderError,
    lint_instruction,
    lint_package,
    require_ok,
)


def test_valid_instruction_passes() -> None:
    assert lint_instruction(INSTRUCTION).ok
    assert require_ok(INSTRUCTION).ok


@pytest.mark.parametrize(
    ("instruction", "needle"),
    [
        ("", "too short"),
        ("no heading but long enough text to pass the length check and more", "title heading"),
        ("# Task\n\nNo requirement here, just words and more words.", "no REQ"),
        ("# Task\n\nREQ-1: do a thing but no symbol is named and plenty of words here", "no explicit"),
        ("# Task\n\nREQ-1: `echo` hidden tests are in tests/feature/ and more words", "leaks"),
        ("# Task\n\nREQ-1: `echo` see /Users/someone/secret and more words", "host path"),
        ("# Task\n\nREQ-1: `echo` use sk-abcdefgh1234 as a key and more words", "secret"),
    ],
)
def test_blind_reader_flags_leaks(instruction: str, needle: str) -> None:
    report = lint_instruction(instruction)
    assert not report.ok and any(needle in p for p in report.problems), report.problems
    with pytest.raises(BlindReaderError):
        require_ok(instruction)


def test_package_audit_and_cli(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "instruction.md").write_text(INSTRUCTION, encoding="utf-8")
    report = lint_package(task)
    assert report.task == "task" and report.ok
    res = CliRunner().invoke(app, ["blind-read", str(task)])
    assert res.exit_code == 0, res.output
    data = json.loads((task / "blind_reader.json").read_text())
    assert data["ok"] is True


def test_package_without_instruction_is_rejected(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    assert not lint_package(task).ok
    res = CliRunner().invoke(app, ["blind-read", str(task)])
    assert res.exit_code == 1 and "missing instruction.md" in res.output
