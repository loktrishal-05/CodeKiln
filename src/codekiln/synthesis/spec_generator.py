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
  "edits": [{"op": "insert_after", "path": "<existing file>", "symbol": "<existing top-level def/class>", "code": "<new python code>"}],
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
        "# Upper-case helper\n\n## Requirements\n"
        "- REQ-1: `click.utils.echo_upper(message: str) -> str` returns the message upper-cased.\n"
        "- REQ-2: `echo_upper` returns an empty string unchanged.\n\n"
        "## Errors\n- `AttributeError` when `message` is not a string.\n"
    ),
    "edits": [
        {"op": "add_import", "path": "src/click/utils.py", "line": "import re"},
        {"op": "insert_after", "path": "src/click/utils.py", "symbol": "echo",
         "code": "def echo_upper(message: str) -> str:\n    \"\"\"Return MESSAGE upper-cased.\"\"\"\n"
                 "    return message.upper()"},
    ],
    "feature_tests": {
        "test_echo_upper.py": (
            "from click.utils import echo_upper\n\n"
            "def test_req1_uppercases():\n    assert echo_upper('abc') == 'ABC'\n\n"
            "def test_req2_empty_string():\n    assert echo_upper('') == ''\n"
        )
    },
    "f2p": [
        "tests/test_echo_upper.py::test_req1_uppercases",
        "tests/test_echo_upper.py::test_req2_empty_string",
    ],
    "requirements": [
        {"id": "REQ-1", "statement": "echo_upper upper-cases",
         "tests": ["tests/test_echo_upper.py::test_req1_uppercases"]},
        {"id": "REQ-2", "statement": "echo_upper keeps an empty string empty",
         "tests": ["tests/test_echo_upper.py::test_req2_empty_string"]},
    ],
}
# The files the example's edits refer to, so a test can prove the example applies for real.
EXAMPLE_WORKSPACE = {"src/click/utils.py": "import os\n\n\ndef echo(message):\n    print(message)\n"}

# The format rules that code enforces, in the order they are usually broken. Also restated on every
# repair message, because a weaker model forgets them by the second attempt.
FORMAT_RULES = """\
FORMAT (checked by code):
1. In "instruction", write every requirement as its own line exactly `- REQ-1: <statement>`,
   `- REQ-2: ...`, using the SAME ids as in "requirements". Never leave the ids out.
2. "feature_tests" keys are bare file names like "test_json_param.py": no directory, no tests/ prefix.
3. Every id in "f2p" and in each requirement's "tests" is "tests/<that same file name>::<test function>".
   Never write tests/feature/ in an id; the harness places the files itself.
4. Do NOT write a diff. "edits" says where the new code goes and the tool builds the patch from the
   real files. Each edit is one of: {"op": "add_import", "path": P, "line": "import json"} |
   {"op": "insert_after", "path": P, "symbol": "<an existing top-level def or class in P>", "code": "..."} |
   {"op": "append", "path": P, "code": "..."} | {"op": "new_file", "path": P, "code": "..."}.
   Use only paths and symbol names that appear in the context pack. Edits only ADD code (never
   rewrite existing code) and never touch tests/, conftest.py or CI files.
5. "task" is kebab-case with at least two words, e.g. "click-json-param".
6. Import inside the new function, not at the top of the module, any module the file does not already
   import: libraries often test that importing the package stays light (a new top-level `import json`
   made such a test fail on a real run). Write tests that assert exact values and error cases.
7. The new code needs at least 3 real decisions (if/else, and/or, comparisons, an error branch) and at
   least 3 tests that exercise them both ways: tiny one-line features cannot be checked for strength.
   Tests may only call names and arguments your edits actually add; a test that passes an argument the
   patch never defines failed a real run."""

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
