"""Staging -> promoted Harbor package: receipt, SHA-256 manifest and atomic export.

Nothing is written to the destination until every gate passed and the security audit is clean.
The receipt and manifest live in `staging/` first, then the whole directory moves with
`os.replace()` so judges never observe a half-written package.
"""
import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from codekiln.packaging.scrubber import SecurityAudit, write_audit

_RECEIPT = "generation_receipt.json"
_MANIFEST = "manifest.sha256"


class PackageRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class PackageSpec:
    task: str
    model: str
    cost_usd: float
    gate_ok: bool
    gates: Mapping[str, object] | None = None
    spec_sha256: str = ""


def hash_tree(root: Path) -> dict[str, str]:
    """Relative path -> SHA-256 for every regular file; stable and missing the manifest itself."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name == _MANIFEST or "__pycache__" in p.parts:
            continue
        out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def write_manifest(root: Path) -> Path:
    lines = [f"{digest}  {name}" for name, digest in sorted(hash_tree(root).items())]
    p = root / _MANIFEST
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def verify_manifest(root: Path) -> bool:
    p = root / _MANIFEST
    if not p.is_file():
        return False
    expected = {
        name: digest
        for line in p.read_text(encoding="utf-8").splitlines()
        for digest, name in [line.split("  ", 1)]
    }
    return expected == hash_tree(root)


def write_receipt(root: Path, spec: PackageSpec, audit: SecurityAudit) -> dict[str, object]:
    """One JSON object tracing task -> model, measured gates, cost, security and file hashes."""
    receipt: dict[str, object] = {
        "task": spec.task,
        "model": spec.model,
        "cost_usd": round(spec.cost_usd, 6),
        "spec_sha256": spec.spec_sha256,
        "gates": dict(spec.gates or {}),
        "security": {"ok": audit.ok, "checks": audit.checks, "reasons": list(audit.reasons)},
        "hashes": hash_tree(root),
    }
    (root / _RECEIPT).write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return receipt


def promote(
    staging: Path, dest: Path, spec: PackageSpec, audit: SecurityAudit
) -> Path:
    """Seal STAGING and move it to DEST, or raise without touching DEST."""
    if not spec.gate_ok:
        raise PackageRejected(f"{staging.name}: gates did not pass")
    if not audit.ok:
        raise PackageRejected(f"{staging.name}: security audit failed: {'; '.join(audit.reasons)}")
    if not (staging / "task.toml").is_file():
        raise PackageRejected(f"{staging} is not a staged Harbor task")
    write_audit(staging / "security_audit.json", audit)
    write_receipt(staging, spec, audit)
    write_manifest(staging)
    if not verify_manifest(staging):
        raise PackageRejected(f"{staging}: manifest verification failed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if (dest / "task.toml").is_file() or not any(dest.iterdir()):
            shutil.rmtree(dest)  # replacing an earlier package of the same task
        else:  # e.g. DEST was mistyped as the packages/ parent: never wipe that
            raise PackageRejected(f"{dest} exists and is not a Harbor task package; refusing to replace")
    os.replace(staging, dest)
    return dest
