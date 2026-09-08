"""CLI for P4."""

from __future__ import annotations

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.table import Table

from .orchestrator import Orchestrator
from .registry import Caller
from .tools import default_registry

app = typer.Typer(help="Multi-tool orchestrator — scoped, parallel, conflict-resolving.")
console = Console()

CALLERS = {
    "analyst": Caller.of("analyst", "billing:read", "crm:read", "analytics:read"),
    "readonly": Caller.of("readonly", "crm:read"),
    "admin": Caller.of("admin", "*"),
}


@app.callback()
def _setup():
    bootstrap(quiet=True)


def _caller(name: str) -> Caller:
    if name not in CALLERS:
        raise typer.BadParameter(f"unknown caller; try {', '.join(CALLERS)}")
    return CALLERS[name]


@app.command()
def tools(caller: str = typer.Option("analyst", "--caller", "-c")):
    """Show the tools this caller may use — a hint, not the control."""
    who = _caller(caller)
    console.print(f"[bold]{caller}[/bold] holds {[s.name for s in who.scopes]}\n")
    console.print(default_registry().describe(who))
    console.print(
        "\n[dim]tools absent from this list are still protected at invoke() — "
        "filtering a menu is not access control[/dim]"
    )


@app.command()
def route(
    capabilities: list[str],
    caller: str = typer.Option("analyst", "--caller", "-c"),
):
    """Show which tool serves a capability set."""
    who = _caller(caller)
    registry = default_registry()
    matches = registry.find(set(capabilities), who)
    if not matches:
        console.print(f"[red]no tool available to {caller} for {set(capabilities)}[/red]")
        raise typer.Exit(1)
    for i, t in enumerate(matches):
        mark = "[green]→[/green]" if i == 0 else " "
        console.print(f"{mark} {t.name:20s} authority {t.authority:3d}  {t.description}")


@app.command()
def gather(
    account: str = typer.Argument("ACC-1001"),
    caller: str = typer.Option("analyst", "--caller", "-c"),
    timeout: float = typer.Option(0.5, help="Per-tool timeout in seconds."),
):
    """Fan out across every tool, then merge and resolve conflicts."""
    who = _caller(caller)
    orchestrator = Orchestrator(default_registry(), per_tool_timeout=timeout)
    batch = orchestrator.run_batch_sync(
        [
            ("billing_service", {"account": account}),
            ("billing_cache", {"account": account}),
            ("crm", {"account": account}),
            ("usage_analytics", {"account": account}),
            ("billing_legacy", {"account": account}),
            ("slow_report", {"account": account}),
            ("apply_credit", {"account": account, "amount": 100}),
        ],
        who,
    )

    table = Table(title="tool results")
    for col in ("tool", "ok", "ms", "value / error"):
        table.add_column(col, overflow="fold")
    for r in batch.results:
        table.add_row(
            r.tool,
            "[green]✓[/green]" if r.ok else "[red]✗[/red]",
            f"{r.duration_ms:.0f}",
            str(r.value) if r.ok else f"[dim]{r.error}[/dim]",
        )
    console.print(table)

    if batch.conflicts:
        console.print("\n[bold]conflicts[/bold]")
        for c in batch.conflicts:
            colour = "red" if c.escalate else "yellow"
            console.print(f"  [{colour}]{c.field}[/{colour}]: {c.values}")
            console.print(f"    → {c.resolution}")
            if c.escalate:
                console.print("    [red]escalated — equal authority, no winner taken[/red]")

    console.print(f"\n[bold]merged[/bold] {batch.merged}")
    console.print(f"[dim]{batch.summary()}[/dim]")
    if orchestrator.abandoned:
        console.print(
            f"[yellow]{orchestrator.abandoned} worker thread(s) abandoned — a timeout "
            f"bounds our wait, it cannot cancel a blocking call[/yellow]"
        )
    orchestrator.close()


if __name__ == "__main__":
    app()
