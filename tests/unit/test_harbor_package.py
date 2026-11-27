import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

from codekiln.packaging.harbor import PackageError, write_task

SHA = "cf19179e8e2fd54c601378cec9e0d21bb12b6fb1"
_spec = importlib.util.spec_from_file_location(
    "reward", Path(__file__).parents[2] / "src/codekiln/templates/harbor/reward.py")
assert _spec and _spec.loader
reward_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reward_mod)

PYTEST_RA = """\
=========================== short test summary info ============================
PASSED tests/test_basic.py::test_ok
PASSED tests/test_utils.py::test_echo_via_pager[test2- less ]
PASSED tests/test_feat.py::test_a
FAILED tests/test_feat.py::test_b - AssertionError: nope - really
ERROR tests/test_feat.py::test_c - fixture 'x' not found
SKIPPED [1] tests/test_basic.py:10: no windows
"""


def test_parse_keeps_exact_node_ids() -> None:
    s = reward_mod.parse(PYTEST_RA)
    assert s["tests/test_utils.py::test_echo_via_pager[test2- less ]"] == "passed"
    assert s["tests/test_feat.py::test_b"] == "failed" and s["tests/test_feat.py::test_c"] == "failed"
    assert len(s) == 5  # SKIPPED lines carry file:line, not node ids


def test_official_reward_formula() -> None:
    s = reward_mod.parse(PYTEST_RA)
    f2p = ["tests/test_feat.py::test_a", "tests/test_feat.py::test_b"]
    assert reward_mod.reward(s, f2p, ["tests/test_basic.py::test_ok"]) == 0.5
    assert reward_mod.reward(s, f2p, ["tests/test_basic.py::gone"]) == 0.0  # missing P2P = fail
    assert reward_mod.reward(s, [], []) == 0.0


def test_collection_error_marks_its_f2p_failed(tmp_path: Path) -> None:
    out = "ERROR tests/test_feat.py - ImportError: cannot import name 'x'\nPASSED tests/test_a.py::t\n"
    (tmp_path / "py.txt").write_text(out)
    (tmp_path / "f2p.json").write_text('["tests/test_feat.py::test_x"]')
    (tmp_path / "p2p.json").write_text('["tests/test_a.py::t"]')
    reward_mod.main(*(str(tmp_path / n) for n in ("py.txt", "f2p.json", "p2p.json")), str(tmp_path))
    res = json.loads((tmp_path / "results.json").read_text())
    assert res == {"tests/test_a.py::t": "passed", "tests/test_feat.py::test_x": "failed"}
    assert (tmp_path / "reward.txt").read_text() == "0.0"


def _ok(tmp_path: Path, **over: object) -> Path:
    kw: dict[str, object] = {
        "instruction": "# Add pluralize\n", "reference_diff": "diff --git a/x b/x\n",
        "feature_tests": {"test_pluralize.py": "def test_one():\n    assert True\n"},
        "f2p": ["tests/test_pluralize.py::test_one"], "p2p": ["tests/test_basic.py::test_ok"],
        "repo_url": "https://github.com/pallets/click", "base_commit": SHA,
        "category": "core_logic", "difficulty": "L1"}
    kw.update(over)
    return write_task(tmp_path / "t", **kw)  # type: ignore[arg-type]


def test_writes_harbor_layout(tmp_path: Path) -> None:
    d = _ok(tmp_path, category='he said "hi"')
    meta = tomllib.loads((d / "task.toml").read_text())["metadata"]
    assert meta["base_commit"] == SHA and meta["category"] == 'he said "hi"'
    assert tomllib.loads((d / "task.toml").read_text())["environment"]["network_mode"] == "no-network"
    docker = (d / "environment/Dockerfile").read_text()
    assert "--continue-on-collection-errors" in (d / "tests/test.sh").read_text()
    assert f"git checkout --quiet {SHA}" in docker and "pip install --no-cache-dir -e ." in docker
    if sys.platform != "win32":  # Windows has no exec bit
        for f in ("solution/solve.sh", "tests/test.sh", "tests/reward.py"):
            assert (d / f).stat().st_mode & 0o111
    assert json.loads((d / "tests/fail_to_pass.json").read_text()) == ["tests/test_pluralize.py::test_one"]
    assert (d / "tests/feature/test_pluralize.py").exists()


@pytest.mark.parametrize("over", [
    {"base_commit": "8.5.1"},
    {"repo_url": "https://x.com/a; rm -rf /"},
    {"feature_tests": {"../../evil.py": ""}},
    {"f2p": []},
    {"f2p": ["tests/test_other.py::test_x"]},
    {"p2p": ["tests/test_pluralize.py::test_one"]},
])
def test_rejects_bad_inputs(tmp_path: Path, over: dict[str, object]) -> None:
    with pytest.raises(PackageError):
        _ok(tmp_path, **over)
