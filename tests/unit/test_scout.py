"""#41: the Repo Scout gives writers the repo's real API and test style, bounded and deterministic."""
from dataclasses import replace
from pathlib import Path

from spec_examples import make_spec
from test_six_slot_synthesis import _ledger

from codekiln.billing.ledger import BillingLedger, Budget, Pricing
from codekiln.db import connect
from codekiln.planner.diversity_guard import fingerprint
from codekiln.planner.models import Category, Plan
from codekiln.synthesis.scout import LIMIT_CHARS, context_pack, related_tests
from codekiln.synthesis.spec_generator import render_prompt

UTILS = '''"""Small helpers."""


def echo(message: str, nl: bool = True) -> None:
    """Print a message to stdout."""


def _private(x):
    return x


class Group(Base):
    """A command group."""

    def add(self, name: str) -> None: ...

    def _hidden(self): ...
'''


def make_repo(root: Path) -> Path:
    (root / "src" / "click").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "click" / "utils.py").write_text(UTILS)
    (root / "src" / "click" / "cli.py").write_text("from click.utils import echo\n")
    (root / "src" / "click" / "broken.py").write_text("def (:\n")
    (root / "tests" / "test_utils.py").write_text("def test_echo():\n    pass\n\ndef helper():\n    pass\n")
    return root


def test_pack_lists_public_api_dependents_and_existing_tests(tmp_path: Path) -> None:
    pack = context_pack(make_repo(tmp_path), ["src/click/utils.py"])
    assert "### src/click/utils.py - Small helpers." in pack
    assert "def echo(message: str, nl: bool=True) -> None  # Print a message to stdout." in pack
    assert "class Group(Base)  # A command group." in pack and "def add(self, name: str)" in pack
    assert "_private" not in pack and "_hidden" not in pack  # private names are not the contract
    assert "1 file(s) import this module." in pack  # cli.py imports it
    assert "existing tests: test_utils.py::test_echo" in pack and "test_utils.py::helper" not in pack


def test_pack_is_deterministic_and_skips_unreadable_files(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    files = ["src/click/utils.py", "src/click/broken.py", "src/click/missing.py"]
    assert context_pack(root, files) == context_pack(root, list(reversed(files)))
    assert "broken" not in context_pack(root, files)


def test_pack_is_bounded(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    many = "\n".join(f"def api_{i}(value: int) -> int:\n    \"\"\"Doc {i}.\"\"\"\n" for i in range(400))
    (root / "src" / "click" / "big.py").write_text(many)
    pack = context_pack(root, ["src/click/big.py"])
    assert len(pack) <= LIMIT_CHARS + len("\n[truncated]") and pack.endswith("[truncated]")
    assert related_tests(root, "src/click/none.py") == []


PLAN = Plan(Category.API_CLI, frozenset({"src/click/utils.py"}), frozenset({"echo"}), "forward", 2)


def test_context_is_inside_the_untrusted_block_and_is_not_part_of_the_slots_identity() -> None:
    with_ctx = replace(PLAN, context="### src/click/utils.py\ndef echo(message)")
    prompt = render_prompt(with_ctx, repo="click", base_commit="abc")
    inner = prompt.split("<untrusted_repo_content>")[1].split("</untrusted_repo_content>")[0]
    assert "def echo(message)" in inner and "def echo(message)" not in render_prompt(PLAN, repo="c", base_commit="a")
    assert with_ctx == PLAN and fingerprint(with_ctx) == fingerprint(PLAN)  # never affects dedupe or the ledger


def test_synthesize_hands_the_writer_the_pack_only_when_a_workspace_exists(tmp_path: Path) -> None:
    from codekiln.synthesis.runner import synthesize

    run_id = _ledger(tmp_path, 1)
    seen: list[str] = []

    class Spy:
        def write(self, plan: Plan, *, repo: str, base_commit: str):  # type: ignore[no-untyped-def]
            seen.append(plan.context)
            return make_spec(task=f"click-spy-{len(seen)}")

    budget = Budget(BillingLedger(tmp_path / "b.csv", Pricing()), run_id, 5.0, "m")
    workspace = make_repo(tmp_path / "ws")
    con = connect(str(tmp_path / "k.sqlite"))
    try:
        # _ledger plans touch src/click/types.py, which this tiny repo lacks: an unreadable file is skipped
        synthesize(con, run_id, Spy(), tmp_path / "p1", budget=budget, workspace=workspace)
        con.execute("UPDATE task_candidates SET status = 'DIVERSITY_OK'")
        con.commit()
        synthesize(con, run_id, Spy(), tmp_path / "p2", budget=budget)
    finally:
        con.close()
    assert seen == ["", ""]  # no matching file => empty pack; no workspace => no pack, and no crash
