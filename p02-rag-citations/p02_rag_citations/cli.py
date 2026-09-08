"""CLI for P2. `python -m p02_rag_citations.cli ask "..."`."""

from __future__ import annotations

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .agent import Action, RagAgent, index_corpus
from .corpus import ANSWERABLE, UNANSWERABLE, all_chunks

app = typer.Typer(help="Grounded RAG — cites its sources, and refuses when it can't.")
console = Console()

_STYLE = {
    Action.answer: ("green", "ANSWERED"),
    Action.answer_caveated: ("yellow", "ANSWERED WITH CAVEAT"),
    Action.fallback_search: ("cyan", "FELL BACK TO WEB SEARCH"),
    Action.refuse: ("red", "REFUSED"),
}


@app.callback()
def _setup():
    bootstrap()


def _render(result) -> None:
    colour, label = _STYLE[result.action]
    console.print(Panel(result.answer or "(no answer)", title=f"[{colour}]{label}[/{colour}]",
                        border_style=colour))

    if result.confidence:
        c = result.confidence
        console.print(
            f"confidence [bold]{c.score:.2f}[/bold]  "
            f"[dim]retrieval {c.retrieval:.2f} · grounding {c.grounding:.2f} · "
            f"model self-report {c.self_reported:.2f}[/dim]"
        )
        for reason in c.reasons:
            console.print(f"  [dim]· {reason}[/dim]")

    if result.claims:
        table = Table(title="\nClaim verification", show_lines=False)
        table.add_column("", width=3)
        table.add_column("claim", overflow="fold")
        table.add_column("cites")
        table.add_column("score", justify="right")
        for claim in result.claims:
            mark = "[green]✓[/green]" if claim.verified else "[red]✗[/red]"
            table.add_row(
                mark, claim.text, ", ".join(claim.citation_ids) or "—",
                f"{claim.support_score:.2f}",
            )
            if not claim.verified:
                table.add_row("", f"[dim red]{claim.reason}[/dim red]", "", "")
        console.print(table)

    used = [c for c in result.citations if c["used"]]
    if used:
        console.print("\n[bold]Sources[/bold]")
        for c in used:
            console.print(f"  [{c['id']}] {c['title']} — {c['heading']}  [dim]{c['source']}[/dim]")

    console.print(f"\n[dim]{result.retrieved} chunks retrieved · ${result.cost_usd:.5f}[/dim]")


@app.command()
def ask(
    question: str,
    k: int = typer.Option(4, help="Chunks to retrieve."),
    search: bool = typer.Option(True, help="Allow the web-search fallback."),
):
    """Ask a question against the knowledge base."""
    result = RagAgent(k=k, allow_search_fallback=search).ask(question)
    _render(result)
    raise typer.Exit(0 if result.answered else 2)


@app.command()
def demo(search: bool = typer.Option(False, help="Allow the web-search fallback.")):
    """Run the covered and uncovered questions side by side.

    The second half is the point: these are questions the corpus deliberately
    does not answer, and the system says so instead of inventing something.
    """
    agent = RagAgent(allow_search_fallback=search)
    for label, questions in (
        ("[green]covered by the knowledge base[/green]", ANSWERABLE),
        ("[red]NOT covered — the interesting half[/red]", UNANSWERABLE),
    ):
        console.rule(label)
        for q in questions:
            result = agent.ask(q)
            colour, action = _STYLE[result.action]
            conf = result.confidence.score if result.confidence else 0.0
            console.print(f"[{colour}]{action:22s}[/{colour}] {conf:.2f}  {q}")


@app.command()
def index(force: bool = typer.Option(False, help="Re-embed even if already indexed.")):
    """Build the vector index."""
    n = index_corpus(force=force)
    console.print(f"indexed [bold]{n}[/bold] chunks from {len(set(c.title for c in all_chunks()))} documents")


@app.command()
def chunks():
    """Show how the corpus was chunked."""
    for c in all_chunks():
        console.print(f"[cyan]{c.id:16s}[/cyan] {c.title} — {c.heading}  [dim]{c.source}[/dim]")


if __name__ == "__main__":
    app()
