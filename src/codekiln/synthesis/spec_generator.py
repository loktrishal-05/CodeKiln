"""Contract-first prompt for the writer; deterministic and model-free.

The LLM router lands next and only has to turn this prompt into `FeatureSpec` JSON. Keeping the
prompt here means the output shape and the rules live beside the validator, not inside a client.
"""
import json

from codekiln.planner.models import Plan

_OUTPUT = """{
  "task": "<kebab-case package name>",
  "category": "<same as the plan>",
  "difficulty": "L1 | L2 | L3",
  "instruction": "<markdown; the only file the agent sees>",
  "reference_diff": "<git diff adding the feature; no file deletions>",
  "feature_tests": {"test_<feature>.py": "<pytest source>"},
  "f2p": ["tests/test_<feature>.py::test_<name>"],
  "requirements": [
    {"id": "REQ-1", "statement": "<one behaviour>", "tests": ["tests/test_<feature>.py::test_<name>"]}
  ]
}"""

# A complete valid reply (a different feature), so a model copies the exact format instead of guessing
# it. tests/unit/test_synthesis.py keeps it valid: it must pass validate + the RTM audit.
EXAMPLE_SPEC = {
    "task": "click-echo-uppercase",
    "category": "api_cli",
    "difficulty": "L2",
    "instruction": (
        "# Uppercase echo\n\n## Requirements\n"
        "- REQ-1: `echo` takes `uppercase: bool = False` and upper-cases the message.\n"
        "- REQ-2: `CliRunner` captures the uppercased output.\n\n"
        "## Errors\n- `TypeError` when `uppercase` is not a bool.\n"
    ),
    "reference_diff": (
        "diff --git a/src/click/utils.py b/src/click/utils.py\n"
        "--- a/src/click/utils.py\n+++ b/src/click/utils.py\n"
        "@@ -1 +1,2 @@\n-def echo():\n+def echo(uppercase=False):\n+    pass\n"
    ),
    "feature_tests": {
        "test_echo_uppercase.py": (
            "from click import echo\nfrom click.testing import CliRunner\n\n"
            "def test_req1_uppercases():\n    assert echo\n\n"
            "def test_req2_runner():\n    assert CliRunner\n"
        )
    },
    "f2p": [
        "tests/test_echo_uppercase.py::test_req1_uppercases",
        "tests/test_echo_uppercase.py::test_req2_runner",
    ],
    "requirements": [
        {"id": "REQ-1", "statement": "echo uppercases",
         "tests": ["tests/test_echo_uppercase.py::test_req1_uppercases"]},
        {"id": "REQ-2", "statement": "CliRunner sees it",
         "tests": ["tests/test_echo_uppercase.py::test_req2_runner"]},
    ],
}

# The format rules that code enforces, in the order they are usually broken. Also restated on every
# repair message, because a weaker model forgets them by the second attempt.
FORMAT_RULES = """\
FORMAT (checked by code):
1. In "instruction", write every requirement as its own line exactly `- REQ-1: <statement>`,
   `- REQ-2: ...`, using the SAME ids as in "requirements". Never leave the ids out.
2. "feature_tests" keys are bare file names like "test_json_param.py": no directory, no tests/ prefix.
3. Every id in "f2p" and in each requirement's "tests" is "tests/<that same file name>::<test function>".
   Never write tests/feature/ in an id; the harness places the files itself.
4. "reference_diff" starts with "diff --git ", only ADDS behaviour, and every @@ hunk header's line
   counts must match its body exactly. Do not touch tests/, conftest.py or CI files.
5. "task" is kebab-case with at least two words, e.g. "click-json-param"."""

_RULES = """\
Rules (violations are rejected before packaging):
- The feature must NOT exist in the base commit: every F2P test must fail there and pass with the
  reference diff. Never delete or rewrite upstream code to make room.
- instruction.md names every click symbol the tests import, with exact signatures and error types.
- Each REQ-n maps to at least one F2P test.
- No network, no new third-party dependencies.
""" + FORMAT_RULES + """

Return JSON only, matching this shape exactly:
""" + _OUTPUT + """

A complete valid reply for a DIFFERENT feature, to copy the format from (do not copy the feature):
""" + json.dumps(EXAMPLE_SPEC, indent=1)


def render_prompt(plan: Plan, *, repo: str, base_commit: str) -> str:
    """Prompt for one plan slot. Repo-derived text is wrapped as untrusted per the trust model."""
    files = "\n".join(f"- {f}" for f in sorted(plan.files))
    symbols = "\n".join(f"- {s}" for s in sorted(plan.symbols))
    scout = (
        "\nexisting API, dependents and tests (match these conventions):\n" + plan.context + "\n"
        if plan.context else ""
    )
    return f"""\
Design one new {plan.category} feature for {repo} at {base_commit}.

The planner only says WHERE the feature belongs; you choose WHAT it does. The feature must be a
new public behaviour, small enough for a single reference diff, and different from the other
candidates.

<untrusted_repo_content>
files it should touch:
{files}

public symbols already there:
{symbols}
{scout}</untrusted_repo_content>

{_RULES}
"""
