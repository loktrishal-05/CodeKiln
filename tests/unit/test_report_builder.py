import json
from pathlib import Path

import pytest

from codekiln.db import connect
from codekiln.evaluation.report_builder import build_report, heat

COMMIT = "cf19179e8e2fd54c601378cec9e0d21bb12b6fb1"


def _ledger(tmp_path: Path):
    con = connect(str(tmp_path / "l.sqlite"))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES (?, ?)",
                ("https://github.com/pallets/click", COMMIT))
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (1, 'dual', 3)")
    for i, (status, reason) in enumerate([
        ("PACKAGED", None),
        ("REJECTED", "STATE_B: F2P t::x is passed on base"),
        ("REJECTED", "<script>alert(1)</script>"),
    ]):
        con.execute(
            "INSERT INTO task_candidates (run_id, idempotency_key, category, difficulty, status,"
            " rejection_reason) VALUES (1, ?, 'validation', 'L2', ?, ?)", (f"k{i}", status, reason))
    con.execute("INSERT INTO eval_runs (candidate_id, model_id, resolved) VALUES (1, 'model-a', 1)")
    return con


def _package(root: Path) -> None:
    d = root / "env-prefix-options"
    (d / "tests").mkdir(parents=True)
    (d / "task.toml").write_text(
        f'schema_version = "1.4"\n[metadata]\ncategory = "api_cli"\ndifficulty = "L2"\n'
        f'base_commit = "{COMMIT}"\n')
    (d / "instruction.md").write_text("# Add env-var prefixes to options\n\nDetails <b>here</b>.")
    (d / "tests" / "fail_to_pass.json").write_text(json.dumps(["a", "b"]))
    (d / "tests" / "pass_to_pass.json").write_text(json.dumps(["c"] * 7))
    (d / "generation_receipt.json").write_text(json.dumps(
        {"nop_rewards": [0.0, 0.0, 0.0], "oracle_rewards": [1.0, 1.0, 1.0], "mutation_kill_rate": 0.9}))


def test_full_report(tmp_path: Path) -> None:
    _package(tmp_path / "pk")
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"tasks": ["a", "b"], "reward": [[1.0, 0.75], [0.0, None]],
                             "overlaps": [{"solution": "a", "tests": "b", "reward": 0.75}],
                             "broken": ["b"], "crashed": ["s1-t1"], "distinct": False}))
    html = build_report(_ledger(tmp_path), 1, tmp_path / "pk", m)
    assert "1 of 3 candidates from click made it through every gate" in html
    assert "<script>" not in html and "&lt;script&gt;" in html  # repo text is escaped
    assert "Add env-var prefixes to options" in html and "&lt;b&gt;here" in html
    assert "0.00 &times;3" in html and "90%" in html
    assert "a's solution scores 0.75 on b's tests" in html and "b fails its own tests" in html
    assert "model-a" in html and "{{" not in html
    # funnel keeps only drops: 3 passed diversity, 1 reject with no known gate stops there, 1 at STATE_B
    assert ">diversity ok<" in html and ">packaged<" in html and ">queued<" not in html


def test_metrics_fall_back_to_dossier_and_nested_gates(tmp_path: Path) -> None:
    _package(tmp_path / "pk")
    d = tmp_path / "pk" / "env-prefix-options"
    receipt = json.loads((d / "generation_receipt.json").read_text())
    receipt.pop("mutation_kill_rate")
    receipt.pop("nop_rewards")
    receipt.pop("oracle_rewards")
    receipt["gates"] = {"nop_rewards": [0.0], "oracle_rewards": [1.0]}
    (d / "generation_receipt.json").write_text(json.dumps(receipt))
    (d / "mutation_dossier.json").write_text(json.dumps({"semantic_kill_rate": 0.75}))
    html = build_report(_ledger(tmp_path), 1, tmp_path / "pk")
    assert "75%" in html  # from mutation_dossier.json, not the receipt
    assert "0.00 &times;1" in html and "1.00 &times;1" in html  # from receipt.gates


def test_empty_sections_say_what_to_run(tmp_path: Path) -> None:
    html = build_report(_ledger(tmp_path), 1)
    assert "codekiln matrix" in html and "No packaged tasks found" in html


def test_unknown_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_report(_ledger(tmp_path), 99)


def test_heat_scale_ends() -> None:
    assert heat(0.0)[0] == "#2b2f38" and heat(1.0)[0] == "#fff6dc"
    assert heat(1.0)[1] == "#1b2330" and heat(0.0)[1] == "#f4f1ec"  # text stays readable
