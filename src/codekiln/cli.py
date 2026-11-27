import json
from pathlib import Path

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
    slots: int = typer.Option(5, min=1),
    max_budget_usd: float = 5.0,  # unused until synthesis spends money
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
    typer.echo("stopped: synthesis/gates/packaging not implemented. See docs/ROADMAP.md.")


@app.command()
def verify(package: str) -> None:
    """Re-run the 3-state gate on a generated PACKAGE."""
    _todo("verify")


@app.command(name="eval")
def eval_(package: str, model: str) -> None:
    """Blind-evaluate MODEL against PACKAGE in a separate verifier container."""
    _todo("eval")


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
