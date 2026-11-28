"""The model says WHERE code goes; code computes the diff, so it applies by construction (found on the
first real run: a model cannot know line numbers, so the diffs it wrote were corrupt)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from spec_examples import REPO, SHA, make_spec

from codekiln.planner.models import Category, Plan
from codekiln.synthesis.edits import MAX_EDITS, build_diff
from codekiln.synthesis.llm_router import LLMWriter, ModelConfig, parse_spec
from codekiln.synthesis.models import SynthesisError
from codekiln.validation.mutation_probe import patch_problem

TYPES = '''"""Types."""
import os
import typing as t


class ParamType:
    name = "base"


class StringParamType(ParamType):
    name = "text"

    def convert(self, value):
        return str(value)


def helper():
    return 1
'''


def repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "click").mkdir(parents=True)
    (tmp_path / "src" / "click" / "types.py").write_text(TYPES, encoding="utf-8", newline="\n")
    return tmp_path


def applied(root: Path, diff: str, rel: str, tmp_path: Path) -> str:
    """Really apply DIFF with git to a copy of the tree and return the resulting file."""
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    subprocess.run(["git", "apply", "-"], cwd=copy, input=diff.encode(), check=True, capture_output=True)
    return (copy / rel).read_text(encoding="utf-8")


def test_insert_after_a_class_and_add_an_import_produce_a_patch_git_really_applies(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    diff = build_diff(root, [
        {"op": "add_import", "path": "src/click/types.py", "line": "import json"},
        {"op": "insert_after", "path": "src/click/types.py", "symbol": "StringParamType",
         "code": "class JsonParamType(ParamType):\n    name = \"json\"\n"},
    ])
    assert patch_problem(diff) == "" and "\r" not in diff and diff.startswith("diff --git a/src/click/types.py")
    result = applied(root, diff, "src/click/types.py", tmp_path)
    assert result.index("import typing as t\nimport json\n") < result.index("class ParamType")  # after the last import
    assert result.index("class StringParamType") < result.index("class JsonParamType") < result.index("def helper")
    assert "def convert(self, value):\n        return str(value)\n\n\nclass JsonParamType" in result  # after the whole class


def test_append_new_file_and_idempotent_import(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    diff = build_diff(root, [
        {"op": "add_import", "path": "src/click/types.py", "line": "import os"},  # already there: no change
        {"op": "append", "path": "src/click/types.py", "code": "def tail():\n    return 2"},
        {"op": "new_file", "path": "src/click/extra.py", "code": "VALUE = 1"},
    ])
    assert "new file mode 100644" in diff and "--- /dev/null" in diff
    assert applied(root, diff, "src/click/types.py", tmp_path).rstrip().endswith("def tail():\n    return 2")
    assert (tmp_path / "copy" / "src" / "click" / "extra.py").read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.parametrize("edits,message", [
    ([], "non-empty list"),
    ("nope", "non-empty list"),
    ([{"op": "append", "path": "../etc/passwd", "code": "x = 1"}], "relative path inside the repo"),
    ([{"op": "append", "path": "/abs.py", "code": "x = 1"}], "relative path inside the repo"),
    ([{"op": "append", "path": "src/click/missing.py", "code": "x = 1"}], "does not exist in the repo"),
    ([{"op": "new_file", "path": "src/click/types.py", "code": "x = 1"}], "already exists"),
    ([{"op": "delete", "path": "src/click/types.py"}], "unknown op"),
    ([{"op": "append", "path": "src/click/types.py", "code": "  "}], "non-empty `code`"),
    ([{"op": "add_import", "path": "src/click/types.py", "line": "import a\nimport b"}], "one-line"),
    (["not an object"], "must be an object"),
])
def test_bad_edits_are_rejected_with_a_reason_the_model_can_act_on(tmp_path: Path, edits: object, message: str) -> None:
    with pytest.raises(SynthesisError, match=message):
        build_diff(repo(tmp_path / "r"), edits)


def test_an_unknown_symbol_lists_what_is_available(tmp_path: Path) -> None:
    with pytest.raises(SynthesisError) as err:
        build_diff(repo(tmp_path / "r"), [{"op": "insert_after", "path": "src/click/types.py",
                                           "symbol": "Nope", "code": "x = 1"}])
    assert "no top-level def/class named 'Nope'" in str(err.value)
    assert "ParamType" in str(err.value) and "StringParamType" in str(err.value) and "helper" in str(err.value)


def test_too_many_edits_and_no_op_edits_are_rejected(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    with pytest.raises(SynthesisError, match="at most"):
        build_diff(root, [{"op": "append", "path": "src/click/types.py", "code": "x = 1"}] * (MAX_EDITS + 1))
    with pytest.raises(SynthesisError, match="change nothing"):
        build_diff(root, [{"op": "add_import", "path": "src/click/types.py", "line": "import os"}])


def test_a_crlf_source_file_is_refused_because_its_patch_would_not_apply(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    (root / "src" / "click" / "types.py").write_bytes(TYPES.replace("\n", "\r\n").encode())
    with pytest.raises(SynthesisError, match="CRLF"):
        build_diff(root, [{"op": "append", "path": "src/click/types.py", "code": "x = 1"}])


def spec_json(edits: list[dict[str, str]]) -> str:
    d = make_spec().to_dict()
    d.pop("reference_diff")
    d["edits"] = edits
    return json.dumps(d)


def test_parse_spec_builds_the_reference_diff_from_edits_when_it_has_a_workspace(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    edits = [{"op": "insert_after", "path": "src/click/types.py", "symbol": "helper", "code": "def more():\n    return 3"}]
    spec = parse_spec(spec_json(edits), root)
    assert spec.reference_diff.startswith("diff --git a/src/click/types.py") and "+def more():" in spec.reference_diff
    with pytest.raises(SynthesisError, match="need the repo checkout"):
        parse_spec(spec_json(edits))  # no workspace: it cannot build a diff, and says so


def test_the_writer_repairs_a_bad_anchor_using_the_real_symbol_list(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    good = [{"op": "insert_after", "path": "src/click/types.py", "symbol": "helper", "code": "def more():\n    return 3"}]
    bad = [{"op": "insert_after", "path": "src/click/types.py", "symbol": "Missing", "code": "x = 1"}]
    replies = [spec_json(bad), spec_json(good)]
    seen: list[str] = []

    def post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        messages = payload["messages"]
        assert isinstance(messages, list)
        seen.append(messages[-1]["content"])
        return {"choices": [{"message": {"content": replies.pop(0)}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    plan = Plan(Category.CORE_LOGIC, frozenset({"src/click/types.py"}), frozenset({"helper"}), "forward",
                workspace=str(root))
    cfg = ModelConfig("https://p.invalid/v1", "sk-test", "m")
    spec = LLMWriter(cfg, post=post).write(plan, repo=REPO, base_commit=SHA)
    assert "+def more():" in spec.reference_diff
    assert "no top-level def/class named 'Missing'" in seen[1] and "StringParamType" in seen[1]  # the model sees the fix


def test_the_workspace_is_cloned_with_the_repos_real_lf_bytes_even_if_git_would_convert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git for Windows defaults to core.autocrlf=true, which checks LF files out as CRLF; edits cannot
    apply to those, and neither could a patch built from them inside the Linux container."""
    from codekiln.ingestion.repo_loader import load_repo

    src = tmp_path / "src_repo"
    src.mkdir()
    cfg = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false"]
    subprocess.run(["git", *cfg, "init", "-q"], cwd=src, check=True, capture_output=True)
    (src / "a.py").write_bytes(b"def one():\n    return 1\n")
    subprocess.run(["git", *cfg, "add", "-A"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", *cfg, "commit", "-q", "-m", "x"], cwd=src, check=True, capture_output=True)
    for key, value in (("GIT_CONFIG_COUNT", "1"), ("GIT_CONFIG_KEY_0", "core.autocrlf"), ("GIT_CONFIG_VALUE_0", "true")):
        monkeypatch.setenv(key, value)  # simulate a user's global autocrlf=true
    load_repo(str(src), tmp_path / "ws")
    assert b"\r\n" not in (tmp_path / "ws" / "a.py").read_bytes()


# --- the agent writers (test critic, adversarial mutants) send edits too -----------------------------------------

REF = (
    "diff --git a/src/click/types.py b/src/click/types.py\n--- a/src/click/types.py\n+++ b/src/click/types.py\n"
    "@@ -1,0 +1,2 @@\n+class Json(ParamType):\n+    name = \"json\"\n"
)
GOOD_EDITS = json.dumps({"edits": [{"op": "insert_after", "path": "src/click/types.py", "symbol": "StringParamType",
                                    "code": "class AltJson(ParamType):\n    name = \"alt\"\n"}]})


def _post(replies: list[str], seen: list[str] | None = None):  # type: ignore[no-untyped-def]
    def post(url, headers, payload):  # type: ignore[no-untyped-def]
        if seen is not None:
            seen.append(payload["messages"][1]["content"])
        return {"choices": [{"message": {"content": replies.pop(0)}}]}
    return post


CFG = ModelConfig(base_url="https://x.invalid/v1", api_key="sk-test", model="m")


def test_diff_from_reply_builds_a_real_patch_from_fenced_or_bare_json(tmp_path: Path) -> None:
    from codekiln.synthesis.edits import diff_from_reply

    root = repo(tmp_path / "r")
    for reply in (GOOD_EDITS, f"```json\n{GOOD_EDITS}\n```"):
        assert patch_problem(diff_from_reply(reply, root)) == ""
    for bad, why in (("no json here", "no JSON object"), ("{not json}", "not valid JSON"), ('{"x": 1}', "edits")):
        with pytest.raises(SynthesisError, match=why):
            diff_from_reply(bad, root)


def test_alternative_writer_with_a_checkout_sends_edits_and_returns_a_patch_that_applies(tmp_path: Path) -> None:
    """Real run: asked for a raw diff the model returned prose or a corrupt hunk, so the critic never ran."""
    from codekiln.validation.test_critic import LLMAlternativeWriter

    root, seen = repo(tmp_path / "r"), []
    diff = LLMAlternativeWriter(CFG, post=_post([GOOD_EDITS], seen), workspace=root).write("do it", REF)
    assert "AltJson" in applied(root, diff, "src/click/types.py", tmp_path)
    assert "Do NOT write a diff" in seen[0] and "StringParamType" in seen[0]  # edits format + the real symbols


def test_alternative_writer_repairs_a_bad_symbol_with_the_real_symbol_list(tmp_path: Path) -> None:
    from codekiln.validation.test_critic import LLMAlternativeWriter

    root = repo(tmp_path / "r")
    bad = GOOD_EDITS.replace("StringParamType", "NoSuchClass")
    diff = LLMAlternativeWriter(CFG, post=_post([bad, GOOD_EDITS]), workspace=root).write("do it", REF)
    assert "AltJson" in applied(root, diff, "src/click/types.py", tmp_path)


def test_adversarial_writer_with_a_checkout_sends_edits(tmp_path: Path) -> None:
    from codekiln.validation.adversarial import LLMAdversarialWriter

    root, seen = repo(tmp_path / "r"), []
    diff = LLMAdversarialWriter(CFG, post=_post([GOOD_EDITS], seen), workspace=root).write("do it", REF)
    assert patch_problem(diff) == "" and "wrong implementation as edits" in seen[0]


def test_checkout_for_package_reuses_an_existing_checkout_and_never_guesses(tmp_path: Path) -> None:
    from codekiln.pipeline import checkout_for_package, workspace_path

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "task.toml").write_text('[metadata]\nrepo = "https://example.invalid/r"\nbase_commit = "abc123def456aa"\n',
                                   encoding="utf-8", newline="\n")
    dest = workspace_path("https://example.invalid/r", "abc123def456aa", tmp_path / "ws")
    dest.mkdir(parents=True)
    assert checkout_for_package(pkg, tmp_path / "ws") == dest  # already there: no network
    (pkg / "task.toml").write_text("[metadata]\n", encoding="utf-8", newline="\n")
    assert checkout_for_package(pkg, tmp_path / "ws") is None  # no repo or commit recorded


def test_a_writer_asks_for_a_small_output_budget_so_it_fits_a_free_tier_minute(tmp_path: Path) -> None:
    """Real run: the default 8000 requested output tokens plus the prompt exceeded a small model's
    8000-token-per-minute limit (HTTP 413) on every request."""
    from codekiln.synthesis.edits import AGENT_MAX_TOKENS
    from codekiln.validation.adversarial import LLMAdversarialWriter
    from codekiln.validation.test_critic import LLMAlternativeWriter

    root, sizes = repo(tmp_path / "r"), []

    def post(url, headers, payload):  # type: ignore[no-untyped-def]
        sizes.append(payload["max_tokens"])
        return {"choices": [{"message": {"content": GOOD_EDITS}}]}

    for writer in (LLMAlternativeWriter, LLMAdversarialWriter):
        writer(CFG, post=post, workspace=root).write("do it", REF)
    assert sizes == [AGENT_MAX_TOKENS, AGENT_MAX_TOKENS] and AGENT_MAX_TOKENS < CFG.max_tokens


def test_an_empty_completion_is_retried_once_and_then_given_up() -> None:
    from codekiln.synthesis.llm_router import RouterError, complete

    empty, ok = {"choices": [{"message": {"content": ""}}]}, {"choices": [{"message": {"content": "hi"}}]}
    calls: list[int] = []

    def flaky(url, headers, payload):  # type: ignore[no-untyped-def]
        calls.append(1)
        return ok if len(calls) == 2 else empty

    assert complete(CFG, [{"role": "user", "content": "x"}], flaky) == "hi" and len(calls) == 2
    calls.clear()
    with pytest.raises(RouterError, match="empty completion"):
        complete(CFG, [{"role": "user", "content": "x"}], lambda *_: empty)
