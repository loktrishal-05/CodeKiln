"""Contract-first prompt for the writer; deterministic and model-free.

The LLM router lands next and only has to turn this prompt into `FeatureSpec` JSON. Keeping the
prompt here means the output shape and the rules live beside the validator, not inside a client.
"""
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

_RULES = """\
Rules (violations are rejected before packaging):
- The feature must NOT exist in the base commit: every F2P test must fail there and pass with the
  reference diff. Never delete or rewrite upstream code to make room.
- instruction.md declares each requirement as `- REQ-n: ...` and names every click symbol the
  tests import, with exact signatures and error types.
- Each REQ-n maps to at least one F2P test; use pytest node ids under tests/test_<feature>.py.
- Feature tests go to tests/feature/ and are hidden from the agent until grading.
- No network, no new third-party dependencies, no edits to conftest.py or test runners.
- Return JSON only, matching this shape exactly:
""" + _OUTPUT


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
