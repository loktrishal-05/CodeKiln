from pathlib import Path

import pytest

from codekiln.planner import plan
from codekiln.planner.diversity_guard import fingerprint, is_diverse, jaccard, select
from codekiln.planner.forward_synth import forward_seeds
from codekiln.planner.models import Category, Plan
from codekiln.planner.reverse_extract import reverse_plans, scan


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "def add(a, b):\n    return a + b\n\ndef _hidden():\n    pass\n",
        "src/pkg/validate.py": "from pkg.core import add\n\ndef check(x):\n    return add(x, 1)\n",
        "src/pkg/db.py": "class Store:\n    pass\n",
        "src/pkg/sub/__init__.py": "",
        "src/pkg/sub/cli.py": "from ..core import add\nfrom pkg import db\n\ndef main():\n    return add(1, 2)\n",
        "src/pkg/broken.py": "def (:\n",
        "tests/test_core.py": "from pkg.core import add\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_scan_graph_symbols_and_exclusions(repo: Path):
    s = scan(repo)
    assert s.graph["src/pkg/validate.py"] == {"src/pkg/core.py"}
    assert s.graph["src/pkg/sub/cli.py"] == {"src/pkg/core.py", "src/pkg/db.py"}  # relative + from-import
    assert s.symbols["src/pkg/core.py"] == {"add"}  # private hidden
    assert "tests/test_core.py" not in s.graph and "src/pkg/broken.py" not in s.graph


def test_dependents_bfs_depth(repo: Path):
    s = scan(repo)
    assert s.dependents("src/pkg/core.py", 1) == {"src/pkg/validate.py", "src/pkg/sub/cli.py"}
    assert s.dependents("src/pkg/db.py", 0) == set()


def test_reverse_plans_categories(repo: Path):
    cats = {p.files and min(p.files): p.category for p in reverse_plans(repo)}
    assert cats["src/pkg/core.py"] is Category.CROSS_MODULE  # dependents span directories
    assert cats["src/pkg/db.py"] is Category.CROSS_MODULE  # cli.py in sub/ depends on it
    assert cats["src/pkg/validate.py"] is Category.VALIDATION


def test_forward_seeds_cover_categories(repo: Path):
    seeds = forward_seeds(repo)
    assert {p.category for p in seeds} >= {Category.VALIDATION, Category.PERSISTENCE, Category.API_CLI}
    assert all(p.mode == "forward" for p in seeds)


def test_jaccard_and_diversity():
    a = Plan(Category.CORE_LOGIC, frozenset({"a.py"}), frozenset({"f"}), "reverse")
    dup = Plan(Category.CORE_LOGIC, frozenset({"a.py"}), frozenset({"f"}), "forward")
    other = Plan(Category.VALIDATION, frozenset({"b.py"}), frozenset({"g"}), "reverse")
    assert jaccard(a.items, dup.items) == 1.0 and jaccard(a.items, other.items) == 0.0
    assert not is_diverse(dup, [a]) and is_diverse(other, [a])
    assert fingerprint(a) == fingerprint(dup) != fingerprint(other)
    assert select([a, dup, other], 5) == [a, other]


def test_plan_is_pairwise_diverse_and_capped(repo: Path):
    chosen = plan(repo, 3)
    assert 0 < len(chosen) <= 3
    assert all(jaccard(x.items, y.items) < 0.4 for i, x in enumerate(chosen) for y in chosen[i + 1 :])


def test_forward_seeds_target_click_subsystems(tmp_path: Path):
    for rel in ("types", "parser", "core", "termui", "utils"):
        p = tmp_path / "src/click" / f"{rel}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"def {rel}_api():\n    pass\n")
    seeds = [p for p in forward_seeds(tmp_path) if p.mode == "forward"]
    files = {p.files for p in seeds}
    assert frozenset({"src/click/types.py"}) in files
    assert frozenset({"src/click/parser.py", "src/click/core.py"}) in files  # parser/options
    assert frozenset({"src/click/termui.py"}) in files
    assert frozenset({"src/click/formatting.py"}) not in files  # absent module -> no seed
