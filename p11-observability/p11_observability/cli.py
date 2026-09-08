"""CLI for P11 — dashboards over the traces P1-P10 actually emitted."""

from __future__ import annotations

import typer
from agentcore import bootstrap
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from . import canary as canary_mod
from .alerts import Severity, evaluate
from .analysis import (
    by_model,
    by_project,
    costliest_runs,
    error_shapes,
    load,
    overview,
    slowest_runs,
    trace,
)

app = typer.Typer(help="Observability over the other ten projects' real traces.")
console = Console()

_SEV = {Severity.critical: "red", Severity.warning: "yellow", Severity.info: "cyan"}


@app.callback()
def _setup():
    bootstrap(quiet=True)


def _spans():
    spans = load()
    if not spans:
        console.print(
            "[yellow]no traces found.[/yellow] run `make smoke` from the repo root "
            "first — it generates ~400 spans across eight projects."
        )
        raise typer.Exit(1)
    return spans


@app.command()
def dashboard():
    """Top-line health across every project."""
    spans = _spans()
    o = overview(spans)

    console.print(Panel(
        f"[bold]{o['spans']}[/bold] spans · [bold]{o['runs']}[/bold] runs · "
        f"[bold]{o['projects']}[/bold] projects\n"
        f"[bold]${o['total_cost_usd']:.4f}[/bold] spent · "
        f"{o['input_tokens']:,} in / {o['output_tokens']:,} out\n"
        f"error rate [bold]{o['error_rate']:.2%}[/bold] · "
        f"{o['degraded']} degraded",
        title="overview",
    ))

    lat = Table(title="latency (ms) — percentiles, because the mean hides the tail")
    for col in ("scope", "count", "p50", "p95", "p99", "max", "mean"):
        lat.add_column(col, justify="right")
    for scope, key in (("run", "run_latency_ms"), ("llm call", "llm_latency_ms")):
        p = o[key]
        lat.add_row(scope, str(p["count"]), f"{p['p50']:.1f}", f"{p['p95']:.1f}",
                    f"{p['p99']:.1f}", f"{p['max']:.1f}", f"{p['mean']:.1f}")
    console.print(lat)

    proj = Table(title="by project")
    for col in ("project", "runs", "spans", "cost", "$/run", "errors", "p95 ms"):
        proj.add_column(col, justify="right")
    for r in by_project(spans):
        colour = "red" if r["error_rate"] > 0.05 else "white"
        proj.add_row(r["project"], str(r["runs"]), str(r["spans"]),
                     f"${r['cost_usd']:.4f}", f"${r['cost_per_run_usd']:.4f}",
                     f"[{colour}]{r['error_rate']:.1%}[/{colour}]",
                     f"{r['p95_run_ms']:.0f}")
    console.print(proj)

    models = Table(title="by model")
    for col in ("model", "calls", "cost", "$/call", "out tokens", "p95 ms"):
        models.add_column(col, justify="right")
    for r in by_model(spans):
        models.add_row(r["model"], str(r["calls"]), f"${r['cost_usd']:.4f}",
                       f"${r['mean_cost_usd']:.5f}", f"{r['output_tokens']:,}",
                       f"{r['p95_ms']:.0f}")
    console.print(models)


@app.command()
def alerts():
    """Agent-shaped alerts, each carrying the evidence to act on it."""
    found = evaluate(_spans())
    if not found:
        console.print("[green]no alerts[/green]")
        raise typer.Exit(0)

    for a in found:
        colour = _SEV[a.severity]
        console.print(
            f"[{colour}]{a.severity.value.upper():8s}[/{colour}] "
            f"[bold]{a.rule}[/bold]  {a.title}"
        )
        console.print(f"  {a.detail}")
        for key, value in a.evidence.items():
            console.print(f"    [dim]{key}: {value}[/dim]")
        console.print()


@app.command()
def errors(limit: int = typer.Option(10)):
    """Errors grouped by shape — '47 errors' is not a ticket, this is."""
    table = Table(title="error shapes")
    for col in ("count", "type", "shape", "projects"):
        table.add_column(col, overflow="fold")
    for row in error_shapes(_spans(), limit):
        table.add_row(str(row["count"]), row["error_type"], row["shape"],
                      ", ".join(row["projects"]))
    console.print(table)


@app.command()
def runs(limit: int = typer.Option(5)):
    """The slowest and costliest runs, so you know which trace to open."""
    spans = _spans()
    slow = Table(title="slowest runs")
    for col in ("run", "project", "ms", "status"):
        slow.add_column(col)
    for r in slowest_runs(spans, limit):
        slow.add_row(r["run_id"], r["project"] or "—", f"{r['duration_ms']:.0f}",
                     r["status"])
    console.print(slow)

    dear = Table(title="costliest runs")
    for col in ("run", "project", "cost"):
        dear.add_column(col)
    for r in costliest_runs(spans, limit):
        dear.add_row(r["run_id"], r["project"] or "—", f"${r['cost_usd']:.5f}")
    console.print(dear)
    console.print("[dim]open one with: cli show <run-id>[/dim]")


@app.command()
def show(run_id: str):
    """Print one run as a trace tree."""
    node = trace(_spans(), run_id)
    if node is None:
        console.print(f"[red]no run {run_id}[/red]")
        raise typer.Exit(1)

    def render(n, parent_tree):
        s = n.span
        colour = {"error": "red", "degraded": "yellow"}.get(s["status"], "white")
        label = (
            f"[{colour}]{s['name']}[/{colour}] "
            f"[dim]{s['duration_ms']:.1f}ms"
            + (f" · ${s['cost_usd']:.5f}" if s.get("cost_usd") else "")
            + (f" · {s['error_type']}" if s.get("error_type") else "")
            + "[/dim]"
        )
        branch = parent_tree.add(label)
        for child in n.children:
            render(child, branch)

    tree = Tree(f"[bold]{node.span['project']}[/bold] · {run_id}")
    render(node, tree)
    console.print(tree)
    console.print(f"[dim]total cost ${node.total_cost:.5f}[/dim]")


@app.command()
def canary(
    name: str = typer.Argument(None, help="Omit to list all canaries."),
    demo: bool = typer.Option(False, "--demo", help="Run a good and a bad release."),
):
    """Canary status, or a demonstration of automatic rollback."""
    if demo:
        _canary_demo()
        raise typer.Exit(0)

    canaries = [canary_mod.load(name)] if name else canary_mod.all_canaries()
    canaries = [c for c in canaries if c]
    if not canaries:
        console.print("no canaries — try `cli canary --demo`")
        raise typer.Exit(0)

    for c in canaries:
        colour = {"promoted": "green", "healthy": "green",
                  "rolled_back": "red", "pending": "yellow"}[c.state.value]
        console.print(
            f"[bold]{c.name}[/bold] {c.baseline_version} → {c.canary_version}  "
            f"[{colour}]{c.state.value}[/{colour}]  {c.traffic_pct:.0f}% traffic"
        )
        m = c.metrics()
        table = Table(show_header=True)
        for col in ("arm", "samples", "error rate", "latency", "cost/call"):
            table.add_column(col, justify="right")
        for arm in ("baseline", "canary"):
            table.add_row(arm, str(m[arm]["samples"]), f"{m[arm]['error_rate']:.1%}",
                          f"{m[arm]['mean_latency_ms']:.0f}ms",
                          f"${m[arm]['mean_cost_usd']:.5f}")
        console.print(table)
        if c.rollback_reason:
            console.print(f"  [red]rolled back:[/red] {c.rollback_reason}\n")


def _canary_demo():
    rails = canary_mod.GuardRails(min_samples=20)

    console.rule("a good release")
    good = canary_mod.start("demo-good", "v1", "v2", traffic_pct=10.0, rails=rails)
    for _ in range(40):
        good.record("baseline", ok=True, latency_ms=120, cost_usd=0.002)
    for _ in range(5):
        good.record("canary", ok=True, latency_ms=115, cost_usd=0.002)
    ok, why = good.promote()
    console.print(f"  after 5 observations: [yellow]{why}[/yellow]")
    for _ in range(20):
        good.record("canary", ok=True, latency_ms=115, cost_usd=0.002)
    ok, why = good.promote()
    console.print(f"  after 25 observations: [green]{why}[/green]")

    console.rule("a bad release")
    bad = canary_mod.start("demo-bad", "v1", "v3", traffic_pct=10.0, rails=rails)
    for _ in range(40):
        bad.record("baseline", ok=True, latency_ms=120, cost_usd=0.002)
    for i in range(25):
        bad.record("canary", ok=(i % 4 != 0), latency_ms=120, cost_usd=0.002)
        if bad.state is canary_mod.State.rolled_back:
            console.print(f"  [red]rolled back after {i + 1} canary requests[/red]")
            console.print(f"  {bad.rollback_reason}")
            break
    console.print(f"  traffic now {bad.traffic_pct:.0f}%, "
                  f"routing to [bold]{bad.route('any')}[/bold]")


if __name__ == "__main__":
    app()
