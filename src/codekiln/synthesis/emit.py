"""FeatureSpec -> Harbor task dir, gated by the RTM audit.

`p2p` is intentionally empty here: pass_to_pass is computed inside the task container by the
3-state gate (State A), and the packager fills it in. Staging, receipt and manifest stay with the
packager item; this only proves the synthesizer's shape is packagable.
"""
from pathlib import Path

from codekiln.packaging.harbor import write_task
from codekiln.synthesis.models import FeatureSpec, validate
from codekiln.synthesis.rtm import require_ok, write_map


def write_package(
    dst: Path,
    spec: FeatureSpec,
    *,
    repo_url: str,
    base_commit: str,
    license: str = "BSD-3-Clause",
) -> Path:
    """Validate SPEC, then write the Harbor layout with rtm_map.json beside instruction.md."""
    validate(spec)
    require_ok(spec)
    task = write_task(
        dst,
        instruction=spec.instruction,
        reference_diff=spec.reference_diff,
        feature_tests=dict(spec.feature_tests),
        f2p=list(spec.f2p),
        p2p=[],
        repo_url=repo_url,
        base_commit=base_commit,
        category=spec.category,
        difficulty=spec.difficulty,
        license=license,
    )
    write_map(task / "rtm_map.json", spec)
    return task
