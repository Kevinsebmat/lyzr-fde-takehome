"""CLI for P1. Runs standalone: `python -m p01_structured_output.cli extract --help`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.table import Table

from .agent import StructuredAgent, failure_log, failure_stats
from .schemas import TicketTriage
from .tools import ToolContractError, build_account_tool, build_broken_tool

app = typer.Typer(help="Structured output agent — schema enforcement with a repair loop.")
console = Console()


@app.callback()
def _setup():
    """Resolve credentials and pick live vs mock before any command runs."""
    bootstrap()


@app.command()
def extract(
    text: str = typer.Argument(None, help="Ticket text. Omit to read stdin."),
    file: Path = typer.Option(None, "--file", "-f", help="Read the ticket from a file."),
    attempts: int = typer.Option(3, "--attempts", help="Max validation attempts."),
    escalate: bool = typer.Option(True, help="Escalate the model after a failure."),
    raw: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Extract triage data from a support ticket."""
    if file:
        text = file.read_text(encoding="utf-8")
    elif not text:
        text = sys.stdin.read()
    if not text.strip():
        raise typer.BadParameter("no ticket text supplied")

    result = StructuredAgent(max_attempts=attempts, escalate=escalate).extract(
        text, TicketTriage
    )

    if raw:
        console.print_json(
            json.dumps(
                {
                    **result.summary(),
                    "value": result.value.model_dump(mode="json") if result.value else None,
                    "error": result.error,
                }
            )
        )
        raise typer.Exit(0 if result.ok else 1)

    if not result.ok:
        console.print(f"[red]extraction failed[/red] after {result.attempts} attempts")
        for f in result.failures:
            console.print(f"  [dim]{f[:200]}[/dim]")
        raise typer.Exit(1)

    t = result.value
    table = Table(title="Ticket triage", show_header=False, box=None)
    table.add_row("severity", f"[bold]{t.severity.value}[/bold]")
    table.add_row("category", t.category.value)
    table.add_row("summary", t.summary)
    table.add_row("sentiment", f"{t.customer_sentiment:+.2f}")
    table.add_row("affected users", f"{t.affected_users:,}")
    table.add_row("human review", "yes" if t.requires_human_review else "no")
    table.add_row(
        "refund", f"${t.refund_amount_usd:,.2f}" if t.refund_amount_usd else "—"
    )
    for i, item in enumerate(t.action_items, 1):
        table.add_row(f"action {i}", f"{item.description} [dim]({item.owner_team}, "
                                     f"{item.due_within_hours}h)[/dim]")
    console.print(table)

    marker = "[yellow]repaired[/yellow]" if result.repaired else "[green]clean[/green]"
    console.print(
        f"\n{marker}  {result.attempts} attempt(s), {len(result.failures)} validation "
        f"failure(s), {result.model}, ${result.cost_usd:.5f}"
    )


@app.command()
def tools(broken: bool = typer.Option(False, help="Invoke the contract-violating tool.")):
    """Demonstrate tool input and output validation."""
    tool = build_broken_tool() if broken else build_account_tool()

    console.print("[bold]valid arguments[/bold]")
    try:
        outcome = tool.invoke({"account_id": "ACC-1001"})
        console.print(f"  ok={outcome.ok} {outcome.value}")
    except ToolContractError as exc:
        console.print(f"  [red]contract violation caught:[/red] {exc}")
        console.print("  [dim]this is our bug, not the model's — it must not reach the model[/dim]")

    console.print("\n[bold]invalid arguments (as a model might emit)[/bold]")
    bad = tool.invoke({"account_id": "oops"})
    console.print(f"  ok={bad.ok} recoverable={bad.model_recoverable}")
    console.print(f"  [dim]{bad.error}[/dim]")
    console.print("  [dim]handed back as an error tool_result for the model to fix[/dim]")


@app.command()
def failures(limit: int = typer.Option(20, help="How many to show.")):
    """Show the persisted validation-failure log."""
    stats = failure_stats()
    console.print(f"[bold]{stats['total']}[/bold] recorded validation failures {stats['by_schema']}\n")
    for row in failure_log(limit):
        console.print(f"[yellow]{row['schema']}[/yellow] attempt {row['attempt']} "
                      f"[dim]{row['model']}[/dim]")
        console.print(f"  {row['error'][:300]}")


if __name__ == "__main__":
    app()
