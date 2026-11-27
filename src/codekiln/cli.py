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
    max_budget_usd: float = 5.0,  # accepted, not used here: the breaker lives in `synth` / `resume`
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


@app.command()
def ideate(
    run_id: int,
    candidates: int = typer.Option(3, min=1),
    out: Path = Path("ideation.json"),
    db: Path | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_budget_usd: float | None = None,
    ledger_csv: Path | None = None,
) -> None:
    """Propose N specs per DIVERSITY_OK slot; a deterministic critic keeps the novel ones.

    Every proposal is metered by the cost ledger; only the model's HTTP call is provider-specific.
    The JSON report carries the admitted specs and the exact reason each other one was dropped.
    """
    from codekiln.billing.ledger import BillingLedger, Budget, BudgetExceeded, Pricing
    from codekiln.planner.models import load_plan
    from codekiln.synthesis.ideator import ideate as run_ideation
    from codekiln.synthesis.llm_router import LLMWriter, ModelConfig, RouterError, http_post
    from codekiln.synthesis.models import SynthesisError

    settings = Settings()
    ledger_path = db or Path(settings.db_path)
    if not ledger_path.exists():
        typer.echo(f"No ledger at {ledger_path}. Run `codekiln run <repo>` first.", err=True)
        raise typer.Exit(2)
    try:
        config = ModelConfig(
            base_url=base_url or settings.llm_base_url, api_key=settings.llm_api_key,
            model=model or settings.llm_model,
        )
    except RouterError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    pricing = Pricing(settings.llm_input_usd_per_1k, settings.llm_output_usd_per_1k)
    bills = BillingLedger(ledger_csv or Path(settings.billing_ledger), pricing)
    budget = Budget(
        bills, run_id, settings.max_budget_usd if max_budget_usd is None else max_budget_usd,
        config.model, pricing,
    )
    con = connect(str(ledger_path))
    try:
        run = con.execute(
            "SELECT r.url, r.base_commit FROM factory_runs f JOIN repositories r"
            " ON r.id = f.repo_id WHERE f.id = ?", (run_id,),
        ).fetchone()
        if not run:
            typer.echo(f"run {run_id} not found in the ledger", err=True)
            raise typer.Exit(2)
        repo, base_commit = run
        slots = con.execute(
            "SELECT c.id, p.plan_json FROM task_candidates c LEFT JOIN task_plans p"
            " ON p.candidate_id = c.id WHERE c.run_id = ? AND c.status = ? ORDER BY c.id",
            (run_id, State.DIVERSITY_OK.value),
        ).fetchall()
        writer = LLMWriter(config, post=budget.meter_post(http_post))
        reports: list[dict[str, object]] = []
        stopped = ""
        for cid, plan_json in slots:
            if not plan_json:
                continue
            budget.start_candidate(cid)
            try:
                budget.guard()
                report = json.loads(run_ideation(load_plan(plan_json), writer, n=candidates,
                                                 repo=repo, base_commit=base_commit).to_json())
            except BudgetExceeded as e:
                stopped = f"BUDGET_EXCEEDED: {e}"
                reports.append({"candidate_id": cid, "ok": False, "error": stopped})
                break
            except (SynthesisError, RouterError, ValueError) as e:  # one slot must not lose the rest
                report = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            report["candidate_id"] = cid
            reports.append(report)
    finally:
        con.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"run {run_id}: {len(reports)} slot report(s) -> {out}; ${bills.spent(run_id):.4f} spent")
    if stopped:
        typer.echo(f"stopped: {stopped}", err=True)
        raise typer.Exit(1)


@app.command(name="test-critic")
def test_critic(
    package: Path,
    run_id: int,
    alternatives: int = typer.Option(1, min=1),
    work: Path = Path("critic-work"),
    out: Path = Path("test_critic.json"),
    db: Path | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_budget_usd: float | None = None,
    ledger_csv: Path | None = None,
) -> None:
    """Stress PACKAGE's F2P suite with N independently written correct solutions.

    The model only proposes a patch. The critic executes it in a copied Harbor task; a correct
    patch that scores below 1.0 is recorded as a signal that the tests may be too narrow (or the
    alternative wrong): review it, nothing is gated on it.
    """
    from codekiln.billing.ledger import BillingLedger, Budget, BudgetExceeded, Pricing
    from codekiln.synthesis.llm_router import ModelConfig, RouterError, http_post
    from codekiln.validation import test_critic as critic
    from codekiln.validation.verify import find_candidate

    settings = Settings()
    db_path = db or Path(settings.db_path)
    if not db_path.exists():
        typer.echo(f"No ledger at {db_path}. Run `codekiln run <repo>` first.", err=True)
        raise typer.Exit(2)
    try:
        config = ModelConfig(
            base_url=base_url or settings.llm_base_url, api_key=settings.llm_api_key,
            model=model or settings.llm_model,
        )
    except RouterError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    pricing = Pricing(settings.llm_input_usd_per_1k, settings.llm_output_usd_per_1k)
    bills = BillingLedger(ledger_csv or Path(settings.billing_ledger), pricing)
    budget = Budget(
        bills, run_id, settings.max_budget_usd if max_budget_usd is None else max_budget_usd,
        config.model, pricing,
    )
    con = connect(str(db_path))
    try:
        budget.start_candidate(find_candidate(con, package) or 0)
    finally:
        con.close()
    writer = critic.LLMAlternativeWriter(config, post=budget.meter_post(http_post))
    try:
        budget.guard()
        result = critic.critique_tests(package, work, writer, alternatives=alternatives)
    except BudgetExceeded as e:
        typer.echo(f"stopped: BUDGET_EXCEEDED: {e}", err=True)
        raise typer.Exit(1) from e
    except critic.CriticError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    critic.write_report(out, result)
    typer.echo(
        f"{package.name}: {sum(a.passed for a in result.alternatives)}/{alternatives}"
        f" alternative(s) passed; ${bills.spent(run_id):.4f} spent -> {out}"
    )
    for reason in result.reasons:
        typer.echo(f"  reject: {reason}", err=True)
    if not result.ok:
        raise typer.Exit(1)


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
    adversarial: int = typer.Option(0, min=0),
    run_id: int | None = None,
    ledger_csv: Path | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> None:
    """Run mutation probes on PACKAGE; --adversarial N adds model-written wrong solutions."""
    from codekiln.billing.ledger import BillingLedger, Budget, BudgetExceeded, Pricing
    from codekiln.synthesis.llm_router import ModelConfig, RouterError, http_post
    from codekiln.validation import adversarial as adv
    from codekiln.validation.mutation_probe import Mutant, probe_package, write_dossier
    from codekiln.validation.verify import find_candidate

    settings = Settings()
    extra: tuple[Mutant, ...] = ()
    if adversarial:
        if run_id is None:
            typer.echo("--run-id is required when --adversarial is used (for metering)", err=True)
            raise typer.Exit(2)
        db = Path(settings.db_path)
        if not db.exists():
            typer.echo(f"No ledger at {db}. Run `codekiln run <repo>` first.", err=True)
            raise typer.Exit(2)
        try:
            config = ModelConfig(
                base_url=base_url or settings.llm_base_url, api_key=settings.llm_api_key,
                model=model or settings.llm_model,
            )
        except RouterError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(2) from e
        pricing = Pricing(settings.llm_input_usd_per_1k, settings.llm_output_usd_per_1k)
        bills = BillingLedger(ledger_csv or Path(settings.billing_ledger), pricing)
        budget = Budget(
            bills, run_id, settings.max_budget_usd, config.model, pricing
        )
        con = connect(str(db))
        try:
            budget.start_candidate(find_candidate(con, package) or 0)
        finally:
            con.close()
        writer = adv.LLMAdversarialWriter(config, post=budget.meter_post(http_post))
        try:
            budget.guard()
            mutants, rejected = adv.propose_mutants(
                (package / "instruction.md").read_text(encoding="utf-8"),
                (package / "solution" / "reference.diff").read_text(encoding="utf-8"),
                writer, n=adversarial,
            )
        except BudgetExceeded as e:
            typer.echo(f"stopped: BUDGET_EXCEEDED: {e}", err=True)
            raise typer.Exit(1) from e
        except (adv.AdversarialError, RouterError, OSError) as e:  # RouterError: provider down
            typer.echo(f"adversarial proposal failed: {e}", err=True)
            raise typer.Exit(2) from e
        adv.report_rejected(rejected, work / package.name / "adversarial_rejected.json")
        extra = tuple(mutants)
        for reason in rejected:
            typer.echo(f"  skipped mutant: {reason}", err=True)

    try:
        d = probe_package(
            package,
            work,
            threshold=settings.mutant_kill_threshold if threshold is None else threshold,
            extra_mutants=extra,
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
