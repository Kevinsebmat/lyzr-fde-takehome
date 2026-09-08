"""CLI for P5. `python -m p05_memory_agent.cli demo`."""

from __future__ import annotations

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.table import Table

from .agent import MemoryAgent
from .memory import MemoryStore

app = typer.Typer(help="Memory agent — remembers across sessions, and forgets what changed.")
console = Console()


@app.callback()
def _setup():
    bootstrap()


@app.command()
def chat(
    message: str,
    user: str = typer.Option("demo-user", "--user", "-u"),
    show_memory: bool = typer.Option(True, help="Show what was recalled and learned."),
):
    """Send one message."""
    result = MemoryAgent(user).chat(message)
    console.print(f"[bold]assistant[/bold] {result.reply}\n")

    if show_memory:
        for s in result.recalled:
            console.print(
                f"  [cyan]recalled[/cyan] [{s.fact.kind.value}] {s.fact.text} "
                f"[dim](sim {s.similarity:.2f} · recency {s.recency:.2f})[/dim]"
            )
        for f in result.learned:
            console.print(f"  [green]learned[/green] [{f.kind.value}] {f.text}")
        for fid in result.superseded:
            console.print(f"  [yellow]superseded[/yellow] {fid}")
        if result.compressed:
            console.print("  [dim]buffer compressed[/dim]")


@app.command()
def facts(
    user: str = typer.Option("demo-user", "--user", "-u"),
    show_all: bool = typer.Option(False, "--all", help="Include superseded facts."),
):
    """Show what is remembered about a user."""
    memory = MemoryStore(user)
    table = Table(title=f"memory for {user}")
    table.add_column("kind")
    table.add_column("fact", overflow="fold")
    table.add_column("uses", justify="right")
    table.add_column("state")

    for f in memory.all_facts(include_superseded=show_all):
        state = "[green]active[/green]" if f.active else f"[dim]→ {f.superseded_by}[/dim]"
        table.add_row(f.kind.value, f.text, str(f.uses), state)
    console.print(table)

    for key, value in memory.stats().items():
        console.print(f"  [dim]{key:20s} {value}[/dim]")


@app.command()
def end_session(user: str = typer.Option("demo-user", "--user", "-u")):
    """Clear the transcript. Facts survive — that is the point."""
    MemoryAgent(user).end_session()
    console.print(f"session ended for {user}; {len(MemoryStore(user).all_facts())} facts kept")


@app.command()
def demo(user: str = typer.Option("demo-wexler", "--user", "-u")):
    """Two sessions and a correction — the whole project in one command."""
    agent = MemoryAgent(user)

    console.rule("session 1")
    for msg in (
        "I'm at Wexler Industries. All our data has to stay in the EU region.",
        "Thanks, that's helpful!",
    ):
        console.print(f"[bold]user[/bold] {msg}")
        chat.callback(msg, user=user, show_memory=True)  # type: ignore[misc]

    agent.end_session()
    console.print("[dim]— session ended: transcript cleared, facts kept —[/dim]")

    console.rule("session 2 (new session, same user)")
    for msg in (
        "Remind me where our data is hosted?",
        "We've migrated. Everything is in the US region now.",
        "So where is our data?",
    ):
        console.print(f"[bold]user[/bold] {msg}")
        chat.callback(msg, user=user, show_memory=True)  # type: ignore[misc]

    console.rule("final memory")
    facts.callback(user=user, show_all=True)  # type: ignore[misc]


if __name__ == "__main__":
    app()
