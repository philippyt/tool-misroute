import asyncio
from pathlib import Path
from typing import Annotated
import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table
from misroute.models.base import BudgetExceeded
from misroute.project import Project

app = typer.Typer(no_args_is_help=True, add_completion=False)
guard_app = typer.Typer(no_args_is_help=True, help="Protect production calls")
app.add_typer(guard_app, name="guard")
out = Console()

Cfg = Annotated[Path, typer.Option("--config", "-c")]
Names = Annotated[str | None, typer.Option(help="comma separated")]
Yes = Annotated[bool, typer.Option("--yes", "-y")]
Pairs = Annotated[list[str] | None, typer.Option("--pair", help="tool_a,tool_b")]

EXAMPLE = """server:
  command: [uv, run, python, -m, misroute.testing.fake_server]
models:
  - name: mock
    provider: mock
    options:
      confuse: {get_case_comments: {get_case_events: 0.4}}
      hedge: {update_case: [update_case_meta]}
modes: [json, code]
budget_usd: 5.00
"""


def split(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def pairs(v: list[str] | None) -> list[tuple[str, str]] | None:
    return [(p.split(",", 1)[0], p.split(",", 1)[1]) for p in v] if v else None


def table(*cols: str) -> Table:
    t = Table(box=None, pad_edge=False)
    for c in cols:
        t.add_column(c, overflow="fold")
    return t


def confirm(cost: float, calls: int, yes: bool) -> None:
    out.print(f"{calls} model calls, est ${cost:.2f}")
    if cost > 0 and not yes and not typer.confirm("continue"):
        raise typer.Exit(1)


def run(proj: Project, coro, total: int):
    with Progress(console=out, transient=True) as bar:
        task = bar.add_task("running", total=total)
        proj.progress = lambda n: bar.advance(task, n)
        try:
            return asyncio.run(coro)
        except BudgetExceeded as e:
            out.print(f"[red]{e}[/red]")
            raise typer.Exit(2) from e


@app.command()
def init(path: Path = Path("misroute.yaml")) -> None:
    """Write an example config"""
    if path.exists():
        raise typer.BadParameter(f"{path} exists")
    path.write_text(EXAMPLE, encoding="utf-8")
    out.print(f"wrote {path}")


@app.command()
def lint(config: Cfg = Path("misroute.yaml"), draft: bool = False) -> None:
    """Flag missing, name only and path only descriptions"""
    proj = Project.from_yaml(config)
    res = asyncio.run(proj.lint(draft))
    out.print(f"{res['tools']} tools, {len(res['findings'])} with weak descriptions")
    for f in res["findings"]:
        flag = " [red]write[/red]" if f["write"] else ""
        out.print(f"  {f['tool']}  {f['issue']}{flag}")
        if f["tool"] in res["drafts"]:
            out.print(f"    draft: {res['drafts'][f['tool']]}", style="dim")


@app.command()
def discover(config: Cfg = Path("misroute.yaml"), models: Names = None, modes: Names = None, yes: Yes = False) -> None:
    """Find risky tools from model behaviour"""
    proj = Project.from_yaml(config)
    ms, md = split(models), split(modes)
    calls, cost = asyncio.run(proj.discover_cost(ms or [m.name for m in proj.cfg.models], md or list(proj.cfg.modes)))
    confirm(cost, calls, yes)
    res = run(proj, proj.discover(ms, md), calls)
    t = table("tool", "sibling", "score", "signals", "seen in")
    for s in res["spots"][:15]:
        style = "red" if s["write"] else ""
        sig = "  ".join(f"{k} {v:.2f}" for k, v in s["signals"].items())
        t.add_row(s["a"], s["b"], f"{s['score']:.2f}", sig, ", ".join(s["where"]), style=style)
    out.print(t)
    for tool, n in res["short"].items():
        out.print(f"{tool}: validator kept {n} of {proj.cfg.requests_per_tool} requests", style="yellow")
    out.print(f"spent ${res['spent']:.2f}, results in {proj.store.root}")


@app.command()
def probe(
    config: Cfg = Path("misroute.yaml"),
    top: int = 5,
    pair: Pairs = None,
    models: Names = None,
    modes: Names = None,
    yes: Yes = False,
) -> None:
    """Harder requests on the risky pairs"""
    proj = Project.from_yaml(config)
    n_pairs = len(pair) if pair else top
    ms = split(models) or [m.name for m in proj.cfg.models]
    md = split(modes) or proj.cfg.modes
    calls = n_pairs * 4 * proj.cfg.requests_per_tool * 2 * 2 * len(ms) * len(md)
    tools = asyncio.run(proj.tools())
    confirm(sum(proj.estimate(calls // len(ms), m, tools) for m in ms), calls, yes)
    res = run(proj, proj.probe(top, pairs(pair), ms, md), calls)
    t = table("requests for", "sibling", "model", "mode", "wrong", "hedge", "leak", "gate p")
    for r in res["rows"]:
        for d, v in r["directions"].items():
            src, dst = d.split(">")
            hedge = f"[red]{v['hedge']:.2f}[/red]" if r["write"] and v["hedge"] else f"{v['hedge']:.2f}"
            t.add_row(
                src, dst, r["model"], r["mode"], f"{v['wrong']:.2f}", hedge, f"{v['leak']:.2f}", f"{r['gate_p']:.2f}"
            )
    out.print(t)


@app.command()
def fix(
    config: Cfg = Path("misroute.yaml"),
    top: int = 3,
    pair: Pairs = None,
    trials: int = 96,
    cheap: bool = False,
    models: Names = None,
    modes: Names = None,
    yes: Yes = False,
) -> None:
    """Test a boundary clause against a sham edit"""
    proj = Project.from_yaml(config)
    trials = 24 if cheap else trials
    n_pairs = len(pair) if pair else top
    ms = split(models) or [m.name for m in proj.cfg.models]
    md = split(modes) or proj.cfg.modes
    calls = n_pairs * trials * 3 * len(ms) * len(md)
    tools = asyncio.run(proj.tools())
    confirm(sum(proj.estimate(calls // len(ms), m, tools) for m in ms), calls, yes)
    res = run(proj, proj.fix(top, pairs(pair), ms, md, trials), calls)
    t = table("tool", "sibling", "model", "mode", "original", "sham", "clause", "p", "verdict")
    for r in res["pairs"]:
        e = r["errors"]
        p = r.get("p_holm")
        t.add_row(
            *r["pair"].split("|"),
            r["model"],
            r["mode"],
            str(e["original"]),
            str(e["sham"]),
            str(e["clause"]),
            "" if p is None else f"{p:.3f}",
            r["verdict"],
        )
    out.print(t)
    for a in res["across"]:
        note = "  under 5 pairs, cannot reach p below 0.05" if a["underpowered"] else ""
        out.print(f"{a['model']} {a['mode']}  {a['pairs']} flagged  mean {a['mean_diff']:+.2f}  p {a['p']:.3f}{note}")
    if cheap:
        out.print("cheap mode, per pair numbers are indicative")


@app.command()
def report(config: Cfg = Path("misroute.yaml"), output: Path = Path("report.html")) -> None:
    """Write a self contained HTML report"""
    from misroute.report.html import render

    proj = Project.from_yaml(config)
    out.print(f"wrote {render(proj.store, output)}")


@app.command()
def ci(config: Cfg = Path("misroute.yaml"), models: Names = None, modes: Names = None) -> None:
    """Run discover and exit non zero when limits are exceeded"""
    proj = Project.from_yaml(config)
    try:
        asyncio.run(proj.discover(split(models), split(modes)))
    except BudgetExceeded as e:
        out.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    bad = proj.breaches()
    for b in bad:
        out.print(f"[red]{b}[/red]")
    raise typer.Exit(1 if bad else 0)


@guard_app.command("serve")
def guard_serve(config: Cfg = Path("misroute.yaml")) -> None:
    """Run as an MCP proxy over stdio in front of the configured server"""
    from misroute.guard.proxy import serve

    proj = Project.from_yaml(config)
    asyncio.run(serve(proj.cfg, proj.store.read("discover")))


@guard_app.command("confirm")
def guard_confirm(call_id: str, config: Cfg = Path("misroute.yaml")) -> None:
    """Approve a held call"""
    from misroute.config import load
    from misroute.guard.wrapper import approve

    approve(load(config).guard.log, call_id)
    out.print(f"approved {call_id}")
