"""CLI for P9."""

from __future__ import annotations

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .debate import PERSONAS, DebateSystem, Verdict

app = typer.Typer(help="Multi-agent debate — independent proposals, a critic, a counted vote.")
console = Console()

_STYLE = {
    Verdict.consensus: "green",
    Verdict.majority: "yellow",
    Verdict.contested: "magenta",
    Verdict.blocked: "red",
    Verdict.failed: "red",
}


@app.callback()
def _setup():
    bootstrap()


@app.command()
def personas():
    """The four perspectives, and why they are structural rather than cosmetic."""
    for p in PERSONAS:
        console.print(f"[bold cyan]{p.key}[/bold cyan] — {p.name}")
        console.print(f"  [dim]{p.brief}[/dim]\n")


@app.command()
def run(question: str, show_proposals: bool = typer.Option(True, "--proposals/--no-proposals")):
    """Hold a debate."""
    result = DebateSystem().run(question)

    if show_proposals:
        for key, p in result.proposals.items():
            console.print(f"[bold cyan]{key}[/bold cyan]  [dim]self-confidence "
                          f"{p.confidence:.2f}[/dim]")
            console.print(f"  {p.recommendation}")
            console.print(f"  [dim]against itself: {p.strongest_counterargument}[/dim]\n")

    if result.critique and result.critique.flaws:
        console.print("[bold]critic[/bold]")
        for f in result.critique.flaws:
            mark = "[red]FATAL[/red]" if f.fatal else "[yellow]flaw [/yellow]"
            console.print(f"  {mark} [{f.proposal_key}] {f.issue}")
        console.print()

    table = Table(title="vote")
    table.add_column("proposal")
    table.add_column("votes", justify="right")
    table.add_column("voters")
    for key, count in sorted(result.tally.items(), key=lambda kv: -kv[1]):
        voters = ", ".join(v for v, c in result.votes.items() if c == key) or "—"
        mark = " [green]★[/green]" if key == result.winner else ""
        table.add_row(f"{key}{mark}", str(count), voters)
    console.print(table)

    colour = _STYLE[result.verdict]
    console.print(Panel(result.recommendation,
                        title=f"[{colour}]{result.verdict.value.upper()}[/{colour}]",
                        border_style=colour))
    console.print(
        f"agreement [bold]{result.agreement:.0%}[/bold] · "
        f"confidence [bold]{result.confidence:.2f}[/bold] · "
        f"${result.cost_usd:.5f}"
    )
    for note in result.notes:
        console.print(f"  [yellow]note:[/yellow] [dim]{note}[/dim]")

    raise typer.Exit(0 if result.decided else 2)


if __name__ == "__main__":
    app()
