"""CLI for P10. `python -m p10_self_reflective.cli run`."""

from __future__ import annotations

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .agent import SelfReflectiveAgent, aggregate, history
from .rubric import DIMENSIONS, render_rubric
from .smoke import CASE_NOTES, TASK

app = typer.Typer(help="Self-reflective agent — generate, judge, rewrite, keep the best.")
console = Console()


@app.callback()
def _setup():
    bootstrap()


@app.command()
def run(
    task: str = typer.Option(TASK, help="What to draft."),
    max_iterations: int = typer.Option(3, "--max-iterations", "-n"),
    target: float = typer.Option(4.2, help="Weighted score that ends the loop."),
    show_drafts: bool = typer.Option(False, "--drafts", help="Print every draft."),
):
    """Run the reflection loop and show the trajectory."""
    result = SelfReflectiveAgent(max_iterations=max_iterations, target_score=target).run(
        task, CASE_NOTES
    )

    table = Table(title="Score trajectory")
    table.add_column("iter", justify="right")
    table.add_column("score", justify="right")
    for d in DIMENSIONS:
        table.add_column(d.key[:6], justify="right")
    table.add_column("critique", overflow="fold")

    best_iter = result.best.iteration if result.best else -1
    for a in result.attempts:
        mark = " [green]★[/green]" if a.iteration == best_iter else ""
        table.add_row(
            f"{a.iteration}{mark}",
            f"{a.score:.2f}",
            *[str(a.per_dimension.get(d.key, "—")) for d in DIMENSIONS],
            a.critique[:80],
        )
    console.print(table)

    if show_drafts:
        for a in result.attempts:
            console.print(Panel(a.text, title=f"iteration {a.iteration} — {a.score:.2f}"))

    if result.best:
        console.print(Panel(result.best.text, title="[green]returned draft[/green]",
                            border_style="green"))

    s = result.summary()
    console.print(
        f"stop=[bold]{s['stop']}[/bold]  "
        f"{s['first_score']:.2f} → {s['best_score']:.2f} "
        f"([green]+{s['improvement']:.2f}[/green])  "
        f"${s['cost_usd']:.5f}"
        + (f"  [dim]${s['cost_per_point']:.5f}/point[/dim]" if s["cost_per_point"] else "")
    )
    if result.regressed:
        console.print(
            "[yellow]a later iteration scored below the best — returned the best, "
            "not the last[/yellow]"
        )


@app.command()
def rubric():
    """Show the scoring rubric and its anchors."""
    console.print(render_rubric())


@app.command()
def metrics():
    """Does reflection actually pay on this workload?"""
    agg = aggregate()
    if not agg.get("runs"):
        console.print("no runs recorded yet — run `cli run` first")
        raise typer.Exit(0)

    for key, value in agg.items():
        console.print(f"  {key:22s} {value}")

    if agg["mean_improvement"] < 0.2:
        console.print(
            "\n[yellow]mean improvement is marginal — on this workload the honest "
            "recommendation is to switch the loop off and spend the budget on a "
            "better first-draft prompt.[/yellow]"
        )

    table = Table(title="\nrecent runs")
    table.add_column("trajectory")
    table.add_column("Δ", justify="right")
    table.add_column("stop")
    for row in history(10):
        table.add_row(
            " → ".join(f"{s:.2f}" for s in row["trajectory"]),
            f"{row['improvement']:+.2f}",
            row["stop"],
        )
    console.print(table)


if __name__ == "__main__":
    app()
