from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, help="CodeKiln: repo -> validated feature tasks.")


def _todo(name: str) -> None:
    typer.echo(f"`{name}` is not implemented yet. See docs/ROADMAP.md.", err=True)
    raise typer.Exit(2)


@app.command()
def run(repo: str, slots: int = 5, max_budget_usd: float = 5.0) -> None:
    """Generate task packages from REPO (git URL, directory or zip)."""
    _todo("run")


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
