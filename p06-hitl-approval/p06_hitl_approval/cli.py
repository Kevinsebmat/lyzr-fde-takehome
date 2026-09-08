"""CLI for P6. The approve/resume flow works across separate invocations —
which is the point."""

from __future__ import annotations

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import approvals
from .agent import ApprovalAgent, Outcome

app = typer.Typer(help="Human-in-the-loop approvals with a durable pause and an audit trail.")
console = Console()

_RISK = {"low": "green", "medium": "yellow", "high": "red"}


@app.callback()
def _setup():
    bootstrap()


@app.command()
def handle(case: str):
    """Ask the agent to act on a case. It may queue the action instead."""
    result = ApprovalAgent().handle(case)

    if result.outcome is Outcome.executed:
        console.print(f"[green]executed[/green] {result.result}")
        return
    if result.outcome is Outcome.failed:
        console.print(f"[red]failed[/red] {result.result}")
        raise typer.Exit(1)

    r = result.request
    colour = _RISK[r.risk.value]
    console.print(
        Panel(r.rendered, title=f"[{colour}]AWAITING APPROVAL — {r.risk.value} risk[/{colour}]",
              border_style=colour)
    )
    console.print(f"request id: [bold]{r.id}[/bold]")
    console.print(
        f"[dim]approve it in a separate command — the pause survives this process exiting:[/dim]\n"
        f"  python -m p06_hitl_approval.cli approve {r.id} --actor you"
    )


@app.command()
def queue(all_statuses: bool = typer.Option(False, "--all")):
    """Show the approval queue."""
    rows = approvals.all_requests() if all_statuses else approvals.pending()
    table = Table(title="approval queue")
    for col in ("id", "action", "risk", "status", "reason"):
        table.add_column(col, overflow="fold")
    for r in rows:
        colour = _RISK[r.risk.value]
        table.add_row(r.id, r.action, f"[{colour}]{r.risk.value}[/{colour}]",
                      r.status.value, r.reason[:60])
    console.print(table)


@app.command()
def show(request_id: str):
    """Show exactly what the approver is being asked to decide."""
    r = approvals.get(request_id)
    if r is None:
        console.print(f"[red]no request {request_id}[/red]")
        raise typer.Exit(1)
    console.print(Panel(r.rendered, title=f"{r.id} — {r.status.value}"))


@app.command()
def approve(
    request_id: str,
    actor: str = typer.Option(..., "--actor", help="Who is approving. Recorded."),
    note: str = typer.Option("", "--note"),
    set_amount: float = typer.Option(None, "--set-amount",
                                     help="Correct the amount before executing."),
):
    """Approve and execute. The approver's corrections win."""
    context = {"amount": set_amount} if set_amount is not None else {}
    approvals.decide(request_id, approved=True, actor=actor, note=note,
                     supplied_context=context)
    result = ApprovalAgent().resume(request_id)
    console.print(f"[green]{result.outcome.value}[/green] {result.result}")


@app.command()
def reject(
    request_id: str,
    actor: str = typer.Option(..., "--actor"),
    note: str = typer.Option("", "--note"),
):
    """Reject. The action never runs."""
    approvals.decide(request_id, approved=False, actor=actor, note=note)
    result = ApprovalAgent().resume(request_id)
    console.print(f"[red]{result.outcome.value}[/red] {result.result}")


@app.command()
def trail(request_id: str = typer.Argument(None, help="Omit for the whole trail.")):
    """The append-only audit trail."""
    table = Table(title="audit trail")
    for col in ("when", "request", "event", "actor", "detail"):
        table.add_column(col, overflow="fold")
    import datetime as dt

    for e in approvals.audit_trail(request_id):
        table.add_row(
            dt.datetime.fromtimestamp(e["at"]).strftime("%H:%M:%S"),
            e["request_id"], e["event"], e["actor"], str(e["detail"])[:70],
        )
    console.print(table)
    for key, value in approvals.stats().items():
        console.print(f"  [dim]{key:24s} {value}[/dim]")


if __name__ == "__main__":
    app()
