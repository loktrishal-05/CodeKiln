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
def report(run_id: int) -> None:
    """Build the static HTML dashboard for RUN_ID."""
    _todo("report")
