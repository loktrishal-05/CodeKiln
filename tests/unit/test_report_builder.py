import json
import tempfile
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


SAMPLES = Path(__file__).resolve().parents[2] / "samples"


def test_checklist_on_the_real_sealed_samples_and_their_logged_rewards() -> None:
    """The five real packages from the factory run: every check passes, and the nop/oracle columns come
    from the Harbor logs kept beside them (a sealed package cannot hold them)."""
    html = build_report(_ledger(Path(tempfile.mkdtemp())), 1, SAMPLES, None)
    check = html.split('id="h-check"')[1].split("</section>")[0]
    assert check.count(">pass<") == 8 * 5 and ">FAIL<" not in check  # 8 checks x 5 packages
    tasks = html.split('id="h-tasks"')[1].split("</section>")[0]
    assert tasks.count("0.00 &times;1") == 5 and tasks.count("1.00 &times;1") == 5


def test_checklist_marks_a_broken_package_and_never_guesses(tmp_path: Path) -> None:
    pkg = tmp_path / "pk"
    _package(pkg)  # a bare package: no rtm map, no audit, no manifest, and a task.toml without no-network
    check = build_report(_ledger(tmp_path), 1, pkg, None).split('id="h-check"')[1].split("</section>")[0]
    rows = {r.split("</th>")[0].split(">")[-1]: r for r in check.split("<tr>")[2:]}
    for missing in ("Every requirement is mapped to a feature test", "Snapshot, canary, secret scan and diff policy are clean",
                    "Files are tamper-evident (SHA-256 manifest verifies)"):
        assert "<td>-</td>" in rows[missing]  # missing evidence is unknown, never a pass and never a failure
    assert ">FAIL<" in rows["No network while the task runs"]  # a real finding stays a failure


def test_funnel_places_a_mutation_rejection_after_state_c_not_after_diversity() -> None:
    """Real run: 'mutation: ...' (lower case) matched no stage, so candidates that had cleared State C
    were drawn as dropping right after the diversity check."""
    from codekiln.evaluation.report_builder import _funnel

    cands = [
        {"status": "PACKAGED", "rejection_reason": None},
        {"status": "REJECTED", "rejection_reason": "mutation: semantic kill rate 60% below 80%"},
        {"status": "REJECTED", "rejection_reason": "STATE_C: reference patch fails: ['t']"},
    ]
    html = _funnel(cands)

    def reached(stage: str) -> str:
        return html.split(f">{stage}<")[1].split('class="num">')[1].split("<")[0]

    assert reached("state b f2p fail") == "3"  # all three passed State B
    assert reached("state c all pass") == "2"  # the State C rejection did not; the mutation one did
    assert reached("packaged") == "1"
