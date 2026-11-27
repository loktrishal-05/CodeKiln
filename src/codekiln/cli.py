import json
from pathlib import Path
from typing import Annotated, Protocol

import typer

from codekiln.config import Settings
from codekiln.db import connect
from codekiln.ingestion.repo_loader import IngestionError
from codekiln.pipeline import run_pipeline
from codekiln.states import State

app = typer.Typer(no_args_is_help=True, help="CodeKiln: repo -> validated feature tasks.")


def _todo(name: str) -> None:
    typer.echo(f"`{name}` is not implemented yet. See docs/ROADMAP.md.", err=True)
    raise typer.Exit(2)


@app.command()
def run(
    repo: str,
    slots: int = typer.Option(6, min=1),  # the six click planner subsystems
    max_budget_usd: float = 5.0,
    workdir: Path = Path("workspaces"),
) -> None:
    """Plan task candidates from REPO (git URL, directory or zip) and record them in the ledger.

    Stops at DIVERSITY_OK: synthesis, gates and packaging are not implemented yet.
    """
    con = connect(Settings().db_path)
    try:
        s = run_pipeline(repo, slots, con, workdir)
    except IngestionError as e:
        typer.echo(f"ingestion failed: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"run {s.run_id}: base_commit {s.base_commit[:12]}, workspace {s.workspace}")
    typer.echo(f"{s.new} new + {s.existing} existing candidates at {State.DIVERSITY_OK}")
    typer.echo(f"next: codekiln synth {s.run_id}  (gates and packaging still pending)")


class _SynthResult(Protocol):
    synthesized: int
    failed: int
    rejected: int
    skipped: int
    stopped: str
    packages: list[Path]


def _synth_run(
    run_id: int,
    db: Path,
    packages: Path,
    model: str | None,
    base_url: str | None,
    max_attempts: int,
    max_budget_usd: float | None,
    ledger_csv: Path | None,
) -> tuple[_SynthResult, list[str], float]:
    """One shared synthesis path for `synth` and `resume`, metered by the cost ledger."""
    from codekiln.billing.ledger import BillingLedger, Budget, Pricing
    from codekiln.synthesis.llm_router import LLMWriter, ModelConfig, RouterError, http_post
    from codekiln.synthesis.runner import synthesize

    settings = Settings()
    if not db.exists():
        raise ValueError(f"No ledger at {db}. Run `codekiln run <repo>` first.")
    try:
        config = ModelConfig(
            base_url=base_url or settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=model or settings.llm_model,
        )
    except RouterError as e:
        raise ValueError(str(e)) from e
    pricing = Pricing(settings.llm_input_usd_per_1k, settings.llm_output_usd_per_1k)
    bills = BillingLedger(ledger_csv or Path(settings.billing_ledger), pricing)
    budget = Budget(
        bills, run_id, settings.max_budget_usd if max_budget_usd is None else max_budget_usd,
        config.model, pricing,
    )
    con = connect(str(db))
    try:
        writer = LLMWriter(config, post=budget.meter_post(http_post))
        summary = synthesize(con, run_id, writer, packages, max_attempts, budget)
        reasons = [
            r for (r,) in con.execute(
                "SELECT rejection_reason FROM task_candidates"
                " WHERE run_id = ? AND rejection_reason IS NOT NULL ORDER BY id",
                (run_id,),
            )
        ]
        return summary, reasons, bills.spent(run_id)
    finally:
        con.close()


def _report_synth(run_id: int, summary: _SynthResult, reasons: list[str], spent: float) -> None:
    stopped = summary.stopped
    typer.echo(
        f"run {run_id}: {summary.synthesized} synthesized, {summary.failed} retryable,"
        f" {summary.rejected} rejected, {summary.skipped} skipped, ${spent:.4f} spent"
    )
    for reason in reasons:
        typer.echo(f"  reject: {reason}", err=True)
    for p in summary.packages:
        typer.echo(f"  {p}")
    if stopped:
        typer.echo(f"  stopped: {stopped}", err=True)
        raise typer.Exit(1)
    if summary.synthesized == 0 and (summary.failed or summary.rejected):
        raise typer.Exit(1)


@app.command()
def synth(
    run_id: int,
    db: Path | None = None,
    packages: Path = Path("packages"),
    model: str | None = None,
    base_url: str | None = None,
    max_attempts: int = typer.Option(3, min=1),
    max_budget_usd: float | None = None,
    ledger_csv: Path | None = None,
) -> None:
    """Synthesize RUN_ID's DIVERSITY_OK candidates into Harbor packages.

    Needs CODEKILN_LLM_API_KEY (and optionally _MODEL/_BASE_URL); the key stays on the host.
    A hard --max-budget-usd breaker stops the run before the next provider call.
    """
    settings = Settings()
    try:
        summary, reasons, spent = _synth_run(
            run_id, db or Path(settings.db_path), packages, model, base_url, max_attempts,
            max_budget_usd, ledger_csv,
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    _report_synth(run_id, summary, reasons, spent)


@app.command()
def resume(
    run_id: int,
    db: Path | None = None,
    packages: Path = Path("packages"),
    max_attempts: int = typer.Option(3, min=1),
    max_budget_usd: float | None = None,
    ledger_csv: Path | None = None,
) -> None:
    """Resume RUN_ID from a fresh checkout: skip finished slots, retry failures, dead-letter poison."""
    settings = Settings()
    try:
        summary, reasons, spent = _synth_run(
            run_id, db or Path(settings.db_path), packages, None, None, max_attempts,
            max_budget_usd, ledger_csv,
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    typer.echo(f"resume {run_id}: {summary.skipped} finished slots skipped")
    _report_synth(run_id, summary, reasons, spent)


@app.command(name="blind-read")
def blind_read(package: Path, out: Path | None = None) -> None:
    """Dry-run the blind reader on PACKAGE's agent-visible instruction."""
    from codekiln.validation.blind_reader import lint_package

    report = lint_package(package)
    dst = out or package / "blind_reader.json"
    dst.write_text(report.to_json(), encoding="utf-8")
    typer.echo(f"{package.name}: {'BLIND_READER_OK' if report.ok else 'rejected'} -> {dst}")
    if not report.ok:
        for p in report.problems:
            typer.echo(f"  {p}", err=True)
        raise typer.Exit(1)


@app.command()
def probe(
    package: Path,
    work: Path = Path("runs/probes"),
    out: Path | None = None,
    threshold: float | None = None,
) -> None:
    """Run four-class mutation probes on PACKAGE and write mutation_dossier.json."""
    from codekiln.config import Settings
    from codekiln.validation.mutation_probe import probe_package, write_dossier

    try:
        d = probe_package(
            package,
            work,
            threshold=Settings().mutant_kill_threshold if threshold is None else threshold,
        )
    except (OSError, ValueError) as e:
        typer.echo(f"probe failed: {e}", err=True)
        raise typer.Exit(2) from e
    dst = out or package / "mutation_dossier.json"
    write_dossier(dst, d)
    typer.echo(
        f"{package.name}: {len(d.probes)} probes, semantic kill {d.semantic_kill_rate:.0%}"
        f" (min {d.threshold:.0%}) -> {dst}"
    )
    for p in d.probes:
        typer.echo(f"  {p.name:>20} {'killed' if p.killed else 'SURVIVED'} reward={p.reward}")
    if not d.ok:
        for reason in d.reasons:
            typer.echo(f"  FAIL: {reason}", err=True)
        raise typer.Exit(1)


@app.command()
def scrub(task: Path, out: Path | None = None) -> None:
    """Audit a staged Harbor task against the anti-gaming policy (writes security_audit.json)."""
    from codekiln.packaging.scrubber import audit_task, write_audit

    try:
        audit = audit_task(task)
    except (OSError, ValueError) as e:
        typer.echo(f"scrub failed: {e}", err=True)
        raise typer.Exit(2) from e
    dst = write_audit(out or task / "security_audit.json", audit)
    typer.echo(f"{task.name}: {'SCRUBBED' if audit.ok else 'blocked'} -> {dst}")
    if not audit.ok:
        for reason in audit.reasons:
            typer.echo(f"  {reason}", err=True)
        raise typer.Exit(1)


@app.command()
def package(
    staging: Path,
    dest: Path,
    model: str = "codekiln",
    cost_usd: float = 0.0,
    gates_ok: bool = typer.Option(
        False, "--gates-ok/--no-gates-ok",
        help="Caller asserts the execution gates passed; without it nothing is promoted.",
    ),
    spec_sha256: str = "",
    candidate_id: int | None = None,
    run_id: int | None = None,
    db: Path | None = None,
) -> None:
    """Seal a staged task (receipt + manifest) and promote it atomically to DEST."""
    import hashlib

    from codekiln.packaging.packager import PackageRejected, PackageSpec, promote
    from codekiln.packaging.scrubber import audit_task
    from codekiln.states import State

    ledger = db or Path(Settings().db_path)
    if candidate_id is None and run_id is not None and ledger.exists():
        with connect(str(ledger)) as con:
            row = con.execute(
                "SELECT c.id FROM task_candidates c JOIN task_plans p ON p.candidate_id = c.id"
                " WHERE c.run_id = ? AND p.package_path LIKE ? ORDER BY c.id LIMIT 1",
                (run_id, f"%{staging.name}%"),
            ).fetchone()
            if row:
                candidate_id = int(row[0])
    try:
        audit = audit_task(staging)
        placed = promote(
            staging, dest,
            PackageSpec(staging.name, model, cost_usd, gates_ok, spec_sha256=spec_sha256),
            audit,
        )
    except (PackageRejected, OSError, ValueError) as e:
        typer.echo(f"package blocked: {e}", err=True)
        raise typer.Exit(1) from e
    manifest = hashlib.sha256((placed / "manifest.sha256").read_bytes()).hexdigest()
    if ledger.exists() and candidate_id is not None:
        with connect(str(ledger)) as con:
            con.execute(
                "UPDATE task_candidates SET status = ? WHERE id = ?",
                (State.SCRUBBED.value, candidate_id),
            )
            con.execute(
                "UPDATE task_candidates SET status = ?, package_sha256 = ? WHERE id = ?",
                (State.PACKAGED.value, manifest, candidate_id),
            )
            # A promoted package is the resume key for eval/resume on a fresh checkout.
            con.execute(
                "UPDATE task_plans SET package_path = ? WHERE candidate_id = ?",
                (str(placed), candidate_id),
            )
    typer.echo(f"{placed} -> PACKAGED (manifest {manifest[:12]})")


@app.command()
def verify(
    package: Path,
    work: Path = Path("runs/verify"),
    db: Path | None = None,
    candidate_id: int | None = None,
    runs: int = 3,
    concurrency: int = 4,
) -> None:
    """Run the 3-state gate on PACKAGE with Harbor (nop x N+1, oracle x N)."""
    from codekiln.validation import harbor_runner
    from codekiln.validation.verify import VerificationError, find_candidate, verify_package

    ledger = db or Path(Settings().db_path)
    if candidate_id is None and ledger.exists():
        with connect(str(ledger)) as con:
            candidate_id = find_candidate(con, package)

    def run(task: Path, out: Path, agent: str) -> object:
        return harbor_runner.run(task, out, agent, concurrency)

    try:
        result = verify_package(
            package, work, runs=runs, db=ledger, candidate_id=candidate_id, run=run,
        )
    except (VerificationError, OSError, ValueError) as e:
        typer.echo(f"verify failed: {e}", err=True)
        raise typer.Exit(2) from e
    typer.echo(
        f"{package.name}: {result.state.value} p2p={len(result.p2p)}"
        f" quarantined={len(result.quarantined)} trials={result.trials}"
    )
    if result.reason:
        typer.echo(f"  {result.reason}", err=True)
    if not result.ok:
        raise typer.Exit(1)


@app.command(name="spec-check")
def spec_check(spec: Path, out: Path | None = None) -> None:
    """Validate a synthesizer FeatureSpec (JSON) and write its RTM map.

    This is the contract writers (LLMs, fixtures) must satisfy: docs/SYNTHESIS.md.
    """
    from codekiln.synthesis.models import SynthesisError, load_spec, validate
    from codekiln.synthesis.rtm import write_map

    dst = out or spec.with_name("rtm_map.json")
    try:
        s = load_spec(spec)
        validate(s)
        m = write_map(dst, s)
    except (OSError, SynthesisError) as e:
        typer.echo(f"spec rejected: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"{s.task}: {len(s.requirements)} REQ, {len(s.f2p)} F2P -> {dst}")
    if not m.ok:
        for p in m.problems:
            typer.echo(f"  RTM: {p}", err=True)
        raise typer.Exit(1)


@app.command(name="eval")
def eval_(
    packages: list[Path],
    model: Annotated[list[str] | None, typer.Option("--model")] = None,
    work: Path = Path("runs/eval"),
    out: Path = Path("eval_receipt.json"),
    db: Path | None = None,
) -> None:
    """Blind-evaluate PACKAGES with 2+ Harbor agents; write eval_receipt.json."""
    from codekiln.evaluation.eval_runner import EvalError, record_rows, run_eval, write_receipt

    models = model or ["claude-code", "codex"]
    try:
        r = run_eval(packages, models, work)
    except (EvalError, OSError, ValueError) as e:
        typer.echo(f"eval failed: {e}", err=True)
        raise typer.Exit(2) from e
    write_receipt(out, r)
    for row in r.rows:
        typer.echo(
            f"{row.task:>24} {row.model_id:<12} reward={row.reward} resolved={row.resolved}"
            f" bloat={row.bloat_ratio} blast={row.blast_radius} ${row.usd_per_solve}"
        )
    for task in r.weak:
        typer.echo(f"  weak discriminator: {task} (every model solved it, or none did)", err=True)
    ledger = db or Path(Settings().db_path)
    if ledger.exists():
        with connect(str(ledger)) as con:
            typer.echo(f"recorded {record_rows(con, r)} scorecard rows in {ledger}")
    if not r.ok:
        raise typer.Exit(1)


@app.command()
def report(
    run_id: int,
    db: Path = Path("codekiln.sqlite"),
    packages: Path = Path("packages"),
    matrix: Path = Path("variation_matrix.json"),
    out: Path = Path("reports/factory_report.html"),
) -> None:
    """Build the static HTML dashboard for RUN_ID (one offline file, no JS)."""
    from codekiln.db import connect
    from codekiln.evaluation.report_builder import build_report

    if not db.exists():
        typer.echo(f"No ledger at {db}. Run `codekiln run <repo>` first.", err=True)
        raise typer.Exit(2)
    try:
        html = build_report(connect(str(db)), run_id, packages if packages.is_dir() else None, matrix)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    typer.echo(f"Wrote {out}")


@app.command()
def matrix(
    tasks: list[Path],
    out: Path = Path("variation_matrix.json"),
    work: Path = Path("runs/matrix"),
    concurrency: int = 4,
) -> None:
    """Cross-task oracle matrix over TASKS (Harbor task dirs): proves they ask for different behaviour."""
    from codekiln.evaluation.variation_matrix import run_matrix
    from codekiln.validation import harbor_runner

    def runner(dataset: Path) -> dict[str, float | None]:
        return harbor_runner.run(dataset, work / "jobs", "oracle", concurrency)

    m = run_matrix([t.resolve() for t in tasks], work, runner)
    out.write_text(json.dumps(m, indent=2))
    for name, row in zip(m["tasks"], m["reward"], strict=True):
        typer.echo(f"{name:>24} " + " ".join("  -- " if r is None else f"{r:5.2f}" for r in row))
    typer.echo(f"distinct={m['distinct']} overlaps={len(m['overlaps'])} broken={m['broken']} -> {out}")
    if not m["distinct"]:
        raise typer.Exit(1)
