import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.progress import Progress
from rich.table import Table
from misroute.models.base import BudgetExceeded
from misroute.project import Project
from misroute.store.runs import SCHEMA_VERSION

OK, FAILED, INSUFFICIENT, PROBLEM = 0, 1, 2, 3

app = typer.Typer(no_args_is_help=True, add_completion=False)
guard_app = typer.Typer(no_args_is_help=True, help="Protect production calls")
app.add_typer(guard_app, name="guard")
out = Console()
err = Console(stderr=True)

Cfg = Annotated[Path, typer.Option("--config", "-c")]
Names = Annotated[str | None, typer.Option(help="comma separated")]
Pairs = Annotated[list[str] | None, typer.Option("--pair", help="tool_a,tool_b")]
Json = Annotated[bool, typer.Option("--json", help="print one JSON object")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="do not ask before paid runs")]
MaxCost = Annotated[float | None, typer.Option("--max-cost", help="refuse runs estimated above this many dollars")]
EstimateOnly = Annotated[bool, typer.Option("--estimate-only", help="print the estimate and exit")]

EXAMPLE = """server:
  command: ["{python}", -m, misroute.testing.fake_server]
models:
  - name: mock
    provider: mock
    options:
      confuse: {get_case_comments: {get_case_events: 0.4}}
      hedge: {update_case: [update_case_meta]}
modes: [json, code]
budget_usd: 5.00
"""


@dataclass
class Opts:
    command: str
    json: bool = False
    yes: bool = False
    max_cost: float | None = None
    estimate_only: bool = False


def emit(opts: Opts, data: dict[str, Any], text=None, code: int = OK) -> None:
    if opts.json:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "command": opts.command, **data}, default=str))
    elif text:
        text()
    if code != OK:
        raise typer.Exit(code)


def fail(opts: Opts, message: str, code: int = PROBLEM) -> None:
    if not opts.json:
        err.print(f"[red]{message}[/red]")
    emit(opts, {"error": message, "exit_code": code}, code=code)


def load(opts: Opts, config: Path) -> Project:
    try:
        return Project.from_yaml(config)
    except (OSError, ValidationError, yaml.YAMLError) as e:
        fail(opts, f"config problem: {e}")
        raise


def gate(opts: Opts, calls: int, cost: float) -> None:
    estimate = {"estimate": {"calls": calls, "cost_usd": round(cost, 4), "upper_bound": True}}
    if opts.estimate_only:
        emit(opts, estimate, lambda: out.print(f"up to {calls} model calls, up to ${cost:.2f}"), OK)
        raise typer.Exit(OK)
    if opts.max_cost is not None and cost > opts.max_cost:
        fail(opts, f"estimate ${cost:.2f} is above max cost ${opts.max_cost:.2f}")
    if not opts.json:
        out.print(f"up to {calls} model calls, up to ${cost:.2f}")
    if cost > 0 and not opts.yes and opts.max_cost is None:
        if not sys.stdin.isatty():
            fail(opts, "paid run without a terminal, pass --yes or --max-cost")
        if not typer.confirm("continue"):
            raise typer.Exit(FAILED)


def run(opts: Opts, proj: Project, coro, total: int):
    with Progress(console=err, transient=True, disable=opts.json) as bar:
        task = bar.add_task("running", total=total)
        proj.progress = lambda n: bar.advance(task, n)
        try:
            return asyncio.run(coro)
        except BudgetExceeded as e:
            fail(opts, str(e))


def split(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def pairs(v: list[str] | None) -> list[tuple[str, str]] | None:
    return [(p.split(",", 1)[0], p.split(",", 1)[1]) for p in v] if v else None


def table(*cols: str) -> Table:
    t = Table(box=None, pad_edge=False)
    for c in cols:
        t.add_column(c, overflow="fold")
    return t


@app.command()
def init(path: Path = Path("misroute.yaml")) -> None:
    """Write an example config"""
    if path.exists():
        raise typer.BadParameter(f"{path} exists")
    path.write_text(EXAMPLE.replace("{python}", Path(sys.executable).as_posix()), encoding="utf-8")
    out.print(f"wrote {path}")


@app.command()
def lint(config: Cfg = Path("misroute.yaml"), draft: bool = False, json_: Json = False) -> None:
    """Flag missing, name only and path only descriptions"""
    opts = Opts("lint", json_)
    proj = load(opts, config)
    res = asyncio.run(proj.lint(draft))

    def text() -> None:
        out.print(f"{res['tools']} tools, {len(res['findings'])} with weak descriptions")
        if not res["findings"]:
            out.print(
                "lint only checks each description on its own, run discover to see which tools they fail to separate",
                style="dim",
            )
        for f in res["findings"]:
            flag = " [red]write[/red]" if f["write"] else ""
            out.print(f"  {f['tool']}  {f['issue']}{flag}")
            if f["tool"] in res["drafts"]:
                out.print(f"    draft: {res['drafts'][f['tool']]}", style="dim")

    emit(opts, res, text)


@app.command()
def catalog(config: Cfg = Path("misroute.yaml")) -> None:
    """Print tool names, hashes and descriptions as JSON, for change aware CI"""
    proj = Project.from_yaml(config)
    tools = asyncio.run(proj.tools())
    rows = [{"name": t.name, "hash": t.hash, "description": t.description, "write": t.is_write} for t in tools]
    print(json.dumps({"schema_version": SCHEMA_VERSION, "tools": rows}, indent=1))


@app.command()
def discover(
    config: Cfg = Path("misroute.yaml"),
    models: Names = None,
    modes: Names = None,
    json_: Json = False,
    yes: Yes = False,
    max_cost: MaxCost = None,
    estimate_only: EstimateOnly = False,
) -> None:
    """Find risky tools from model behaviour"""
    opts = Opts("discover", json_, yes, max_cost, estimate_only)
    proj = load(opts, config)
    ms, md = split(models), split(modes)
    calls, cost = asyncio.run(proj.discover_cost(ms or proj.cfg.tested(), md or list(proj.cfg.modes)))
    gate(opts, calls, cost)
    res = run(opts, proj, proj.discover(ms, md), calls)

    def text() -> None:
        t = table("tool", "sibling", "score", "signals", "direction", "seen in", "note")
        for s in res["spots"][:15]:
            sig = "  ".join(f"{k} {v:.2f}" for k, v in s["signals"].items())
            d = s["directions"]
            dirs = f"{d.get(s['a'] + '>' + s['b'], 0)} to {d.get(s['b'] + '>' + s['a'], 0)}" if d else ""
            gone = [x for x in (s["a"], s["b"]) if x in res["untested"]]
            flags = (("gate failed", s["gate_failed"]), ("few requests", s["thin"] and not gone))
            note = ", ".join([*(f"UNTESTED {x}" for x in gone), *(x for x, on in flags if on)])
            row = (s["a"], s["b"], f"{s['score']:.2f}", sig, dirs, ", ".join(s["where"]), note)
            t.add_row(*row, style="red" if s["write"] else "")
        out.print(t)
        for tool in res["untested"]:
            out.print(
                f"UNTESTED {tool}: the validator kept none of its requests, so it was never tested as a target. "
                "Its scores only count other tools' requests landing on it, a low score does not clear it",
                style="bold red",
            )
        for c in res["cells"]:
            for tool, rate in c.get("unanswered", {}).items():
                out.print(f"{c['model']} {c['mode']}: {tool} answered without a tool call in {rate:.0%} of trials")
        if res["gate_failed"]:
            out.print(f"{len(res['gate_failed'])} tool pairs have request sets that surface words can tell apart")
        if res["near_misses"]:
            out.print("near misses, unvalidated, not in the ranking", style="dim")
            for n in res["near_misses"][:10]:
                out.print(f"  {n['tool']}  {n['rival']}  {n['score']:.2f}  {n['where']} {n['source']}", style="dim")
        for tool, s in res["short"].items():
            picked = ", ".join(f"{k} {v}" for k, v in sorted(s["validator_picked"].items(), key=lambda kv: -kv[1]))
            out.print(f"{tool}: validator kept {s['kept']} of {proj.cfg.requests_per_tool} requests", style="yellow")
            if picked:
                out.print(f"  it picked instead: {picked}", style="yellow")
        if res["short"]:
            out.print(
                "When the validator picks a sibling, the two descriptions may not separate the tools, which is itself "
                "a finding. When it picks nothing, try a different generator or add real requests with real_requests",
                style="dim",
            )
        out.print(f"spent ${res['spent']:.2f}, results in {proj.store.root}")

    emit(opts, res, text)


@app.command()
def probe(
    config: Cfg = Path("misroute.yaml"),
    top: int = 5,
    pair: Pairs = None,
    models: Names = None,
    modes: Names = None,
    json_: Json = False,
    yes: Yes = False,
    max_cost: MaxCost = None,
    estimate_only: EstimateOnly = False,
) -> None:
    """Harder requests on the risky pairs"""
    opts = Opts("probe", json_, yes, max_cost, estimate_only)
    proj = load(opts, config)
    ms = split(models) or proj.cfg.tested()
    md = split(modes) or list(proj.cfg.modes)
    calls, cost = asyncio.run(proj.cost("probe", ms, md, len(pair) if pair else top))
    gate(opts, calls, cost)
    res = run(opts, proj, proj.probe(top, pairs(pair), ms, md), calls)

    def text() -> None:
        t = table("requests for", "sibling", "model", "mode", "wrong", "hedge", "leak", "gate p")
        for r in res["rows"]:
            for d, v in r["directions"].items():
                src, dst = d.split(">")
                h = f"[red]{v['hedge']:.2f}[/red]" if r["write"] and v["hedge"] else f"{v['hedge']:.2f}"
                t.add_row(
                    src, dst, r["model"], r["mode"], f"{v['wrong']:.2f}", h, f"{v['leak']:.2f}", f"{r['gate_p']:.2f}"
                )
        out.print(t)

    emit(opts, res, text)


@app.command()
def fix(
    config: Cfg = Path("misroute.yaml"),
    top: int = 3,
    pair: Pairs = None,
    trials: int = 96,
    cheap: bool = False,
    models: Names = None,
    modes: Names = None,
    json_: Json = False,
    yes: Yes = False,
    max_cost: MaxCost = None,
    estimate_only: EstimateOnly = False,
) -> None:
    """Test a boundary clause against a neutral edit or a drafted description"""
    opts = Opts("fix", json_, yes, max_cost, estimate_only)
    proj = load(opts, config)
    trials = 24 if cheap else trials
    ms = split(models) or proj.cfg.tested()
    md = split(modes) or list(proj.cfg.modes)
    calls, cost = asyncio.run(proj.cost("fix", ms, md, len(pair) if pair else top, trials))
    gate(opts, calls, cost)
    res = run(opts, proj, proj.fix(top, pairs(pair), ms, md, trials), calls)

    def text() -> None:
        t = table("tool", "sibling", "model", "mode", "original", "baseline", "with clause", "p", "verdict")
        for r in res["pairs"]:
            e, p = r["errors"], r.get("p_holm")
            t.add_row(
                *r["pair"].split("|"),
                r["model"],
                r["mode"],
                str(e["original"]),
                f"{e['baseline']} {r['baseline_kind']}",
                str(e["clause"]),
                "" if p is None else f"{p:.3f}",
                r["verdict"],
            )
        out.print(t)
        for a in res["across"]:
            note = "  under 5 pairs, cannot reach p below 0.05" if a["underpowered"] else ""
            out.print(
                f"{a['model']} {a['mode']}  {a['pairs']} flagged  mean {a['mean_diff']:+.2f}  p {a['p']:.3f}{note}"
            )
        if cheap:
            out.print("cheap mode, per pair numbers are indicative")

    emit(opts, res, text, OK if any(r["flagged"] for r in res["pairs"]) else INSUFFICIENT)


def check_code(verdict: str) -> int:
    return {"worse": FAILED, "insufficient_data": INSUFFICIENT}.get(verdict, OK)


@app.command()
def check(
    tool: Annotated[str, typer.Option(help="tool to check")],
    description: Annotated[str, typer.Option(help="proposed description")],
    config: Cfg = Path("misroute.yaml"),
    refresh: Annotated[bool, typer.Option(help="draw a fresh held out set")] = False,
    models: Names = None,
    modes: Annotated[str, typer.Option(help="comma separated")] = "json",
    json_: Json = False,
    yes: Yes = False,
    max_cost: MaxCost = None,
    estimate_only: EstimateOnly = False,
) -> None:
    """Check whether a proposed description beats the current one on held out requests"""
    from misroute import check as checker

    opts = Opts("check", json_, yes, max_cost, estimate_only)
    proj = load(opts, config)
    ms, md = split(models) or proj.cfg.tested(), split(modes) or ["json"]
    tools = asyncio.run(proj.tools())
    calls, cost = checker.estimate(proj, 2 + len(checker.neighbours(proj, tool)), ms, md, tools)
    gate(opts, calls, cost)
    res = run(opts, proj, checker.check(proj, tool, description, refresh=refresh, models_=ms, modes=md), calls)

    def text() -> None:
        w, h = res["wrong_rate"], res["hedge_rate"]
        out.print(f"{res['verdict']}  {res['reason']}  baseline {res['baseline_kind']}  trials {res['trials']}")
        for cond in w:
            out.print(f"  {cond:9} wrong {w[cond]:.2f}  hedge {h[cond]:.2f}")
        if res["leakage"]:
            out.print(f"  leakage to {', '.join(res['leakage'])}")
        out.print(f"  held out {res['held_out']}, verdicts left {res['verdicts_left']}, spent ${res['cost_usd']:.2f}")

    emit(opts, res, text, check_code(res["verdict"]))


@app.command()
def report(config: Cfg = Path("misroute.yaml"), output: Path = Path("report.html")) -> None:
    """Write a self contained HTML report"""
    from misroute.report.html import render

    proj = Project.from_yaml(config)
    out.print(f"wrote {render(proj.store, output)}")


def changed_markdown(rows: list[dict]) -> str:
    lines = [
        "| Tool | Verdict | Wrong rate, current to proposed | Hedge rate | Trials | Held out |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "verdict" not in r:
            lines.append(f"| `{r['tool']}` | {r['note']} | | | | |")
            continue
        w, h = r["wrong_rate"], r["hedge_rate"]
        lines.append(
            f"| `{r['tool']}` | {r['verdict']} | {w.get('current', 0):.2f} to {w.get('proposed', 0):.2f} "
            f"| {h.get('current', 0):.2f} to {h.get('proposed', 0):.2f} | {r['trials']} | {r['held_out']} |"
        )
    return "\n".join(lines) + "\n"


@app.command()
def ci(
    config: Cfg = Path("misroute.yaml"),
    models: Names = None,
    modes: Names = None,
    changed: Annotated[bool, typer.Option(help="check only tools whose definition changed")] = False,
    baseline: Annotated[Path | None, typer.Option(help="catalog JSON from the base branch")] = None,
    markdown: Annotated[Path | None, typer.Option(help="write a markdown summary for a PR comment")] = None,
    json_: Json = False,
    max_cost: MaxCost = None,
) -> None:
    """Exit non zero when limits are exceeded, or when a changed description got worse"""
    opts = Opts("ci", json_, True, max_cost)
    proj = load(opts, config)
    if changed:
        if not baseline:
            fail(opts, "--changed needs --baseline")
            return
        rows, code = run(opts, proj, ci_changed(proj, json.loads(baseline.read_text()), split(models), opts), 0)
        if markdown:
            markdown.write_text(changed_markdown(rows), encoding="utf-8")
        emit(opts, {"changed": rows}, lambda: out.print(changed_markdown(rows)), code)
        return
    ms, md = split(models), split(modes)
    calls, cost = asyncio.run(proj.discover_cost(ms or proj.cfg.tested(), md or list(proj.cfg.modes)))
    gate(opts, calls, cost)
    run(opts, proj, proj.discover(ms, md), calls)
    bad = proj.breaches()

    def text() -> None:
        for b in bad:
            out.print(f"[red]{b}[/red]")

    emit(opts, {"breaches": bad}, text, FAILED if bad else OK)


async def ci_changed(proj: Project, base: dict, models_: list[str] | None, opts: Opts) -> tuple[list[dict], int]:
    from misroute import check as checker

    before = {t["name"]: t for t in base.get("tools", [])}
    rows = []
    for t in await proj.tools():
        old = before.get(t.name)
        if old and old["hash"] == t.hash:
            continue
        if not old:
            rows.append({"tool": t.name, "note": "new tool, run discover"})
        elif old["description"] == t.description:
            rows.append({"tool": t.name, "note": "schema changed, description unchanged"})
        else:
            n = 2 + len(checker.neighbours(proj, t.name))
            _, cost = checker.estimate(proj, n, models_ or proj.cfg.tested(), ["json"], await proj.tools())
            if opts.max_cost is not None and proj.budget.spent + cost > opts.max_cost:
                rows.append({"tool": t.name, "note": f"skipped, estimate ${cost:.2f} would pass max cost"})
                continue
            res = await checker.check(proj, t.name, t.description, current=old["description"], models_=models_)
            rows.append(res)
    verdicts = [r.get("verdict") for r in rows]
    code = FAILED if "worse" in verdicts else INSUFFICIENT if "insufficient_data" in verdicts else OK
    return rows, code


@app.command()
def ui(config: Cfg = Path("misroute.yaml"), port: int = 8765) -> None:
    """Open the local web interface"""
    from misroute.ui.app import serve

    out.print(f"http://127.0.0.1:{port}")
    serve(config, port)


@app.command("serve-mcp")
def serve_mcp(config: Cfg = Path("misroute.yaml")) -> None:
    """Run misroute itself as an MCP server over stdio, for agents"""
    from misroute.agent_server import create

    create(Project.from_yaml(config)).run()


@app.command()
def site(
    results: Annotated[list[str], typer.Argument(help="name=workdir")],
    output: Path = Path("site"),
    title: str = "misroute results",
) -> None:
    """Build a static results site from one or more workdirs"""
    from misroute.site.build import build

    entries = [(r.split("=", 1)[0], Path(r.split("=", 1)[1])) for r in results]
    out.print(f"wrote {build(entries, output, title)}")


@guard_app.command("serve")
def guard_serve(config: Cfg = Path("misroute.yaml")) -> None:
    """Run as an MCP proxy over stdio in front of the configured server"""
    from misroute.guard.proxy import serve

    proj = Project.from_yaml(config)
    asyncio.run(serve(proj.cfg, proj.store.read("discover")))


@guard_app.command("confirm")
def guard_confirm(call_id: str, config: Cfg = Path("misroute.yaml")) -> None:
    """Approve a held call"""
    from misroute.config import load as load_config
    from misroute.guard.wrapper import approve

    approve(load_config(config).guard.log, call_id)
    out.print(f"approved {call_id}")
