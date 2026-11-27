"""One valid FeatureSpec shared by synthesizer tests (importable because pytest adds tests/unit)."""
from typing import Any

from codekiln.synthesis.models import FeatureSpec, Requirement

SHA = "cf19179e8e2fd54c601378cec9e0d21bb12b6fb1"
REPO = "https://github.com/pallets/click"

INSTRUCTION = """# Uppercase echo

## Requirements
- REQ-1: `echo` takes `uppercase: bool = False` and upper-cases the message.
- REQ-2: `CliRunner` captures the uppercased output.

## Errors
- `TypeError` when `uppercase` is not a bool.
"""
FEATURE_TESTS = {
    "test_echo_uppercase.py": (
        "from click import echo\n"
        "from click.testing import CliRunner\n\n"
        "def test_req1_uppercases():\n    assert echo\n\n"
        "def test_req2_runner():\n    assert CliRunner\n"
    )
}
DIFF = (
    "diff --git a/src/click/utils.py b/src/click/utils.py\n"
    "--- a/src/click/utils.py\n"
    "+++ b/src/click/utils.py\n"
    "@@ -1 +1,2 @@\n"
    "-def echo():\n"
    "+def echo(uppercase=False):\n"
    "+    pass\n"
)
REQ1 = "tests/test_echo_uppercase.py::test_req1_uppercases"
REQ2 = "tests/test_echo_uppercase.py::test_req2_runner"


def make_spec(**over: Any) -> FeatureSpec:
    kw: dict[str, Any] = {
        "task": "click-echo-uppercase",
        "instruction": INSTRUCTION,
        "reference_diff": DIFF,
        "feature_tests": FEATURE_TESTS,
        "f2p": (REQ1, REQ2),
        "requirements": (
            Requirement("REQ-1", "echo uppercases", (REQ1,)),
            Requirement("REQ-2", "CliRunner sees it", (REQ2,)),
        ),
        "category": "api_cli",
        "difficulty": "L2",
    }
    kw.update(over)
    return FeatureSpec(**kw)
