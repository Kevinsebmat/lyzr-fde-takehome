"""CLI for P7."""

from __future__ import annotations

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.table import Table

from .economics import break_even, classifier_verdict, price_table, projected_cost
from .router import CostAwareRouter, analytics, classify, reset_history

app = typer.Typer(help="Cost-aware router — and the arithmetic that says whether it pays.")
console = Console()


@app.callback()
def _setup():
    bootstrap()


@app.command()
def prices():
    """The price table and each model's break-even against Opus 5."""
    table = Table(title="model economics (per 1M tokens; task = 2k in / 600 out)")
    for col in ("model", "tier", "in $", "out $", "cost/task", "break-even vs opus"):
        table.add_column(col, justify="right")
    for row in price_table():
        table.add_row(
            row["model"], row["tier"], f"{row['input_per_mtok']:.2f}",
            f"{row['output_per_mtok']:.2f}", f"${row['cost_per_task']:.4f}",
            f"{row['break_even_vs_opus']:.0%}",
        )
    console.print(table)
    console.print(
        "\n[dim]break-even = the share of traffic the cheap model must handle "
        "unaided. Below it, the cascade pays for two calls and costs more than "
        "always using the strong model.[/dim]"
    )


@app.command()
def model(
    cheap: str = typer.Option("claude-haiku-4-5"),
    strong: str = typer.Option("claude-opus-5"),
    tasks: int = typer.Option(1000, help="Monthly task volume."),
):
    """Project the cost of a cascade across success rates."""
    be = break_even(cheap, strong)
    console.print(
        f"[bold]{cheap}[/bold] vs [bold]{strong}[/bold] — break-even "
        f"[bold]{be.required_success_rate:.0%}[/bold]\n"
    )
    table = Table(title=f"{tasks:,} tasks/month")
    for col in ("cheap handles", "cascade", "always-strong", "saved", "%", "verdict"):
        table.add_column(col, justify="right")
    for rate in (0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 0.95):
        p = projected_cost(cheap, strong, rate, tasks=tasks)
        colour = "green" if p["worth_it"] else "red"
        table.add_row(
            f"{rate:.0%}", f"${p['cascade_usd']:,.2f}", f"${p['baseline_usd']:,.2f}",
            f"${p['saved_usd']:,.2f}", f"{p['saved_pct']:.0f}%",
            f"[{colour}]{'pays' if p['worth_it'] else 'costs more'}[/{colour}]",
        )
    console.print(table)

    console.print("\n[bold]per-request LLM classifier overhead[/bold]")
    for rate in (0.9, 0.8, 0.5, 0.25):
        v = classifier_verdict(cheap, strong, rate)
        colour = "green" if v["affordable"] else "red"
        console.print(
            f"  at {rate:.0%} success: classifier ${v['classifier_cost_usd']:.5f} is "
            f"[{colour}]{v['share_of_saving_consumed']:.0%}[/{colour}] of the "
            f"${v['saving_per_request_usd']:.5f} saving"
        )
    console.print(
        "[dim]the overhead bites hardest exactly when routing is already "
        "marginal — which is why the default classifier is heuristic[/dim]"
    )


@app.command()
def route(task: str, budget: float = typer.Option(0.05, help="Task budget in USD.")):
    """Route one task and show the decision."""
    c = classify(task)
    console.print(f"classified [bold]{c.complexity.value}[/bold] "
                  f"[dim]({c.method}: {'; '.join(c.reasons)})[/dim]\n")

    result = CostAwareRouter(task_budget_usd=budget).route(task)
    for a in result.attempts:
        mark = "[green]accepted[/green]" if not a.escalated_because else "[yellow]escalated[/yellow]"
        console.print(f"  {a.model:20s} confidence {a.confidence:.2f}  ${a.cost_usd:.5f}  {mark}")
        if a.escalated_because:
            console.print(f"    [dim]{a.escalated_because}[/dim]")

    console.print(f"\n{result.answer}\n")
    console.print(f"[dim]{result.summary()}[/dim]")
    if result.budget_exceeded:
        console.print(f"[red]stopped at the ${budget} task budget[/red]")


@app.command()
def stats(reset: bool = typer.Option(False, "--reset", help="Clear the decision log.")):
    """Cost per decision, and whether routing is actually paying."""
    if reset:
        reset_history()
        console.print("decision log cleared")
        raise typer.Exit(0)

    a = analytics()
    if not a.get("decisions"):
        console.print("no decisions recorded yet — run `cli route` first")
        raise typer.Exit(0)

    for key in ("decisions", "cost_per_decision_usd", "escalation_rate",
                "early_exit_rate", "actual_usd", "baseline_usd", "saved_usd",
                "saved_pct", "calls_by_model"):
        console.print(f"  {key:24s} {a[key]}")

    colour = "green" if a["routing_pays"] else "red"
    console.print(f"\n[{colour}]{a['verdict']}[/{colour}]")


if __name__ == "__main__":
    app()
