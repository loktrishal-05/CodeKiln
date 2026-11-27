"""Host-side hardening: scrubber, generation receipt, SHA-256 manifest, atomic promotion."""
import json
import sqlite3
from pathlib import Path

import pytest
from spec_examples import DIFF, FEATURE_TESTS, INSTRUCTION, REPO, REQ1, REQ2, SHA
from typer.testing import CliRunner

from codekiln.cli import app
from codekiln.db import connect
from codekiln.packaging.harbor import write_task
from codekiln.packaging.packager import (
    PackageRejected,
    PackageSpec,
    promote,
    verify_manifest,
)
from codekiln.packaging.scrubber import ScrubError, audit_task

MODELS = ("claude-code", "codex")


def staged(root: Path, name: str = "click-echo-uppercase") -> Path:
    return write_task(
        root / name,
        instruction=INSTRUCTION,
        reference_diff=DIFF,
        feature_tests=FEATURE_TESTS,
        f2p=[REQ1, REQ2],
        p2p=[],
        repo_url=REPO,
        base_commit=SHA,
        category="api_cli",
        difficulty="L2",
    )


def spec(name: str, **over: object) -> PackageSpec:
    kw: dict[str, object] = {"task": name, "model": MODELS[0], "cost_usd": 0.42,
                             "gate_ok": True, "spec_sha256": "a" * 64}
    kw.update(over)
    return PackageSpec(**kw)


def test_audit_accepts_a_clean_package(tmp_path: Path) -> None:
    task = staged(tmp_path)
    audit = audit_task(task)
    assert audit.ok and not audit.reasons
    assert all(audit.checks.values())
    assert set(audit.checks) == {
        "single_commit_snapshot", "editable_install", "no_site_packages_leak",
        "no_network", "diff_policy", "canary", "secret_scan",
    }
    assert json.loads(audit.to_json())["checks"] == audit.checks


@pytest.mark.parametrize("mutation, expected", [
    ("network", "no_network"),
    ("history", "single_commit_snapshot"),
    ("diff", "diff_policy"),
    ("secret", "secret_scan"),
    ("canary", "canary"),
    ("egress", "no_network"),
])
def test_audit_blocks_each_anti_gaming_violation(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    task = staged(tmp_path)
    if mutation == "network":
        p = task / "task.toml"
        p.write_text(p.read_text().replace('"no-network"', '"bridge"'))
        audit = audit_task(task)
    elif mutation == "history":
        p = task / "environment" / "Dockerfile"
        p.write_text(p.read_text().replace("rm -rf .git", "# kept .git"))
        audit = audit_task(task)
    elif mutation == "diff":
        (task / "solution" / "reference.diff").write_text(
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1 +1 @@\n-a\n+b\n"
        )
        audit = audit_task(task)
    elif mutation == "secret":
        (task / "instruction.md").write_text(INSTRUCTION + "\napi_key = sk-abcdef1234567890\n")
        audit = audit_task(task)
    elif mutation == "canary":
        (task / "tests" / "feature" / "test_echo_uppercase.py").unlink()
        audit = audit_task(task)
    else:
        audit = audit_task(task, egress_probe=lambda: False)
    assert not audit.ok and any(expected in reason for reason in audit.reasons)
    assert not audit.checks[expected]


def test_audit_rejects_non_packages_and_malformed_f2p(tmp_path: Path) -> None:
    with pytest.raises(ScrubError, match="not a Harbor task"):
        audit_task(tmp_path)
    task = staged(tmp_path)
    (task / "tests" / "fail_to_pass.json").write_text("{}")
    with pytest.raises(ScrubError, match="must be a list"):
        audit_task(task)


def test_promote_seals_staging_and_moves_atomically(tmp_path: Path) -> None:
    staging = staged(tmp_path)
    dest = tmp_path / "packages" / staging.name
    audit = audit_task(staging)
    assert promote(staging, dest, spec(staging.name), audit) == dest
    assert not staging.exists() and dest.is_dir()
    assert (dest / "security_audit.json").is_file()
    assert verify_manifest(dest)
    receipt = json.loads((dest / "generation_receipt.json").read_text())
    assert receipt["model"] == MODELS[0]
    assert receipt["cost_usd"] == 0.42
    assert receipt["spec_sha256"] == "a" * 64
    assert receipt["security"]["ok"] is True
    assert receipt["hashes"]["task.toml"]
    assert "manifest.sha256" not in receipt["hashes"]


def test_manifest_detects_tampering(tmp_path: Path) -> None:
    staging = staged(tmp_path)
    dest = tmp_path / "packages" / staging.name
    promote(staging, dest, spec(staging.name), audit_task(staging))
    assert verify_manifest(dest)
    (dest / "instruction.md").write_text("changed after promotion")
    assert not verify_manifest(dest)


@pytest.mark.parametrize("gate_ok, audit_ok, reason", [
    (False, True, "gates did not pass"),
    (True, False, "security audit failed"),
])
def test_promote_refuses_unsafe_packages_without_touching_dest(
    tmp_path: Path, gate_ok: bool, audit_ok: bool, reason: str
) -> None:
    staging = staged(tmp_path)
    audit = audit_task(staging)
    if not audit_ok:
        audit = type(audit)(
            audit.task, {**audit.checks, "canary": False}, ("canary: failed",)
        )
    dest = tmp_path / "packages" / staging.name
    with pytest.raises(PackageRejected, match=reason):
        promote(staging, dest, spec(staging.name, gate_ok=gate_ok), audit)
    assert staging.is_dir() and not dest.exists()


def _db_with_candidate(tmp_path: Path, staging: Path) -> Path:
    db = tmp_path / "k.sqlite"
    con = connect(str(db))
    con.execute("INSERT INTO repositories (url, base_commit) VALUES (?, ?)", (REPO, SHA))
    rid = con.execute("SELECT id FROM repositories").fetchone()[0]
    con.execute("INSERT INTO factory_runs (repo_id, mode, slots) VALUES (?, 'dual', 1)", (rid,))
    run_id = con.execute("SELECT id FROM factory_runs").fetchone()[0]
    con.execute(
        "INSERT INTO task_candidates (run_id, idempotency_key, status) VALUES (?, 'k', 'BLIND_READER_OK')",
        (run_id,),
    )
    cid = con.execute("SELECT id FROM task_candidates").fetchone()[0]
    con.execute(
        "INSERT INTO task_plans (candidate_id, plan_json, package_path) VALUES (?, '{}', ?)",
        (cid, str(staging)),
    )
    con.commit()
    con.close()
    return db


def test_cli_scrub_and_package_promote_and_update_ledger(tmp_path: Path) -> None:
    staging = staged(tmp_path)
    audit_path = tmp_path / "security_audit.json"
    scrub = CliRunner().invoke(app, ["scrub", str(staging), "--out", str(audit_path)])
    assert scrub.exit_code == 0, scrub.output
    assert json.loads(audit_path.read_text())["ok"] is True

    db = _db_with_candidate(tmp_path, staging)
    dest = tmp_path / "packages" / staging.name
    pack = CliRunner().invoke(app, [
        "package", str(staging), str(dest), "--model", MODELS[0], "--cost-usd", "0.42",
        "--spec-sha256", "a" * 64, "--candidate-id", "1", "--db", str(db),
    ])
    assert pack.exit_code == 0, pack.output
    assert verify_manifest(dest)
    con = sqlite3.connect(db)
    try:
        status, digest = con.execute(
            "SELECT status, package_sha256 FROM task_candidates WHERE id = 1"
        ).fetchone()
        assert status == "PACKAGED" and len(digest) == 64
        path = con.execute(
            "SELECT package_path FROM task_plans WHERE candidate_id = 1"
        ).fetchone()[0]
        assert path == str(dest)
    finally:
        con.close()


def test_cli_blocked_package_leaves_ledger_untouched(tmp_path: Path) -> None:
    staging = staged(tmp_path)
    db = _db_with_candidate(tmp_path, staging)
    dest = tmp_path / "packages" / staging.name
    pack = CliRunner().invoke(app, [
        "package", str(staging), str(dest), "--no-gates-ok", "--candidate-id", "1", "--db", str(db),
    ])
    assert pack.exit_code == 1, pack.output
    assert staging.is_dir() and not dest.exists()
    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT status FROM task_candidates WHERE id = 1"
        ).fetchone()[0] == "BLIND_READER_OK"
    finally:
        con.close()


def test_cli_scrub_exits_nonzero_on_leaks(tmp_path: Path) -> None:
    task = staged(tmp_path)
    (task / "instruction.md").write_text(INSTRUCTION + "\ntoken = ghp_abcdef1234567890\n")
    res = CliRunner().invoke(app, ["scrub", str(task)])
    assert res.exit_code == 1, res.output
    assert "secret_scan" in res.output
