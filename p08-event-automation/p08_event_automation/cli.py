"""CLI for P8."""

from __future__ import annotations

import json

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.table import Table

from . import queue
from .worker import Worker, default_router, ledger

app = typer.Typer(help="Event automation — idempotent, retried, dead-lettered, replayable.")
console = Console()


@app.callback()
def _setup():
    bootstrap(quiet=True)


@app.command()
def send(
    event_type: str,
    data: str = typer.Option("{}", "--data", "-d", help="JSON payload."),
    key: str = typer.Option(None, "--key", "-k", help="Idempotency key."),
    times: int = typer.Option(1, help="Deliver this many times, as a flaky sender would."),
):
    """Deliver an event — repeatedly, to show the duplicate suppression."""
    payload = json.loads(data)
    for i in range(times):
        result = queue.enqueue(event_type, payload, idempotency_key=key)
        mark = "[yellow]duplicate suppressed[/yellow]" if result.duplicate else "[green]accepted[/green]"
        console.print(f"  delivery {i + 1}: {mark} → {result.event.id}")


@app.command()
def work(max_events: int = typer.Option(50, "--max", help="Cap on events processed.")):
    """Drain the queue once."""
    stats = Worker(default_router()).drain(max_events=max_events)
    console.print(f"[bold]worker[/bold] {stats.as_dict()}")
    console.print(f"[bold]queue[/bold]  {queue.stats()}")
    if ledger():
        console.print(f"[bold]ledger[/bold] {ledger()}")


@app.command()
def events(status: str = typer.Option(None), limit: int = typer.Option(30)):
    """Show the queue."""
    from .queue import Status

    table = Table(title="events")
    for col in ("id", "type", "status", "att", "error / result"):
        table.add_column(col, overflow="fold")
    colours = {"done": "green", "dead": "red", "failed": "yellow",
               "pending": "cyan", "in_flight": "magenta"}
    for e in queue.all_events(Status(status) if status else None, limit):
        table.add_row(
            e.id, e.type,
            f"[{colours.get(e.status.value, 'white')}]{e.status.value}[/]",
            f"{e.attempts}/{e.max_attempts}",
            (e.result or e.last_error or "")[:70],
        )
    console.print(table)
    console.print(f"[dim]{queue.stats()}[/dim]")


@app.command()
def dead(replay_id: str = typer.Option(None, "--replay", help="Replay this letter.")):
    """Inspect or replay the dead-letter table."""
    if replay_id:
        result = queue.replay(replay_id)
        console.print(f"[green]replayed[/green] as {result.event.id}")
        console.print("[dim]note the new idempotency key — replaying under the "
                      "original would be suppressed as a duplicate[/dim]")
        raise typer.Exit(0)

    letters = queue.dead_letters()
    if not letters:
        console.print("no pending dead letters")
        raise typer.Exit(0)
    table = Table(title="dead letters")
    for col in ("id", "type", "attempts", "last error"):
        table.add_column(col, overflow="fold")
    for dl in letters:
        table.add_row(dl["id"], dl["type"], str(dl["attempts"]), dl["last_error"][:70])
    console.print(table)


@app.command()
def demo():
    """The whole story: duplicates, retries, dead letters, replay."""
    queue.purge()
    console.rule("a flaky sender delivers the same payment three times")
    send("payment.succeeded",
                  data='{"account": "ACC-1001", "amount": 4820.0}',
                  key="pay_88", times=3)

    console.rule("a malformed ticket, and an unroutable event")
    send("ticket.created", data='{"missing": "subject"}', key="tkt-1", times=1)
    send("nobody.handles.this", data='{}', key="unr-1", times=1)

    console.rule("worker")
    work(max_events=50)
    console.print("\n[dim]the payment was delivered three times and credited "
                  "once[/dim]")

    console.rule("dead letters")
    dead(replay_id=None)


@app.command()
def purge():
    """Clear the queue and dead letters."""
    queue.purge()
    console.print("purged")


if __name__ == "__main__":
    app()
