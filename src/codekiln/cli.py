import json
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
def report(run_id: int) -> None:
    """Build the static HTML dashboard for RUN_ID."""
    _todo("report")


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
