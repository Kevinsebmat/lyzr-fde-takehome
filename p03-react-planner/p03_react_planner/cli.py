"""CLI for P3. `python -m p03_react_planner.cli run "..."`."""

from __future__ import annotations

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.panel import Panel

from .agent import Outcome, ReActAgent
from .tools import default_registry

app = typer.Typer(help="ReAct planner — observe, think, act, reflect, and always stop.")
console = Console()

_STYLE = {
    Outcome.solved: "green",
    Outcome.max_iterations: "yellow",
    Outcome.loop_detected: "red",
    Outcome.no_progress: "yellow",
    Outcome.budget_exhausted: "red",
    Outcome.failed: "red",
}


@app.callback()
def _setup():
    bootstrap()


@app.command()
def run(
    task: str,
    max_iterations: int = typer.Option(8, "--max-iterations", "-n"),
    max_repeats: int = typer.Option(2, help="Identical actions before it's a loop."),
    max_stalls: int = typer.Option(2, help="Unproductive steps tolerated."),
    reflect: bool = typer.Option(True, help="Run the self-critique step."),
):
    """Run a task through the ReAct loop."""
    result = ReActAgent(
        max_iterations=max_iterations,
        max_repeats=max_repeats,
        max_stalls=max_stalls,
        reflect=reflect,
    ).run(task)

    for s in result.steps:
        console.print(f"[bold cyan]step {s.iteration}[/bold cyan]  [dim]{s.thought}[/dim]")
        if s.action:
            console.print(f"  → [bold]{s.action}[/bold]({s.action_input})")
            obs = (s.observation or "")[:220]
            colour = "red" if obs.startswith("ERROR") else "white"
            console.print(f"  ← [{colour}]{obs}[/{colour}]")
        if s.critique:
            mark = "[green]progress[/green]" if s.progressed else "[yellow]no progress[/yellow]"
            console.print(f"  {mark}: [dim]{s.critique}[/dim]")

    colour = _STYLE[result.outcome]
    console.print(
        Panel(result.answer, title=f"[{colour}]{result.outcome.value.upper()}[/{colour}]",
              border_style=colour)
    )
    if result.reason:
        console.print(f"[dim]stopped because: {result.reason}[/dim]")
    console.print(
        f"[dim]{len(result.steps)} iterations · {result.tool_calls} tool calls · "
        f"${result.cost_usd:.5f}[/dim]"
    )
    raise typer.Exit(0 if result.solved else 2)


@app.command()
def tools():
    """List the available tools, including the deliberately unreliable ones."""
    for name, tool in default_registry().tools.items():
        console.print(f"[bold]{name:16s}[/bold] {tool.description}")


if __name__ == "__main__":
    app()
