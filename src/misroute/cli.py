import asyncio
import json
import shlex
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.progress import Progress
from rich.table import Table
from misroute import export, mcpconfig
from misroute.config import Config, env_path, write_env
from misroute.doctor import system_certs
from misroute.fix.stats import ALPHA
from misroute.fix.stats import MIN_PAIRS_SIGNFLIP as MIN_PAIRS
from misroute.models.base import BudgetExceeded
from misroute.models.presets import detect
from misroute.project import Project
from misroute.store.runs import SCHEMA_VERSION

OK, FAILED, INSUFFICIENT, PROBLEM = 0, 1, 2, 3

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    epilog="Using misroute from an agent: run misroute guide for commands, exit codes and JSON fields.",
)
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
ProgressMode = Annotated[str, typer.Option("--progress", help="bar, ndjson on stderr, or none")]
ADVANCED = "Advanced"
ModelSpec = Annotated[
    str | None, typer.Option("--model", help="provider:model, for example anthropic:claude-sonnet-5-5")
]

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
    progress: str = "bar"


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
    system_certs(config)
    try:
        return Project.from_yaml(config)
    except (OSError, ValidationError, yaml.YAMLError) as e:
        fail(opts, f"config problem: {e}")
        raise


def gate(opts: Opts, calls: int, cost: float, note: str = "") -> None:
    estimate = {"estimate": {"calls": calls, "cost_usd": round(cost, 4), "upper_bound": not note, "note": note}}
    if opts.estimate_only:

        def show() -> None:
            out.print(f"up to {calls} model calls, up to ${cost:.2f}")
            if note:
                out.print(note, style="dim")

        emit(opts, estimate, show, OK)
        raise typer.Exit(OK)
    if opts.max_cost is not None and cost > opts.max_cost:
        fail(opts, f"estimate ${cost:.2f} is above max cost ${opts.max_cost:.2f}")
    if not opts.json:
        out.print(f"up to {calls} model calls, up to ${cost:.2f}")
        if note:
            out.print(note, style="dim")
    if cost > 0 and not opts.yes and opts.max_cost is None:
        if not sys.stdin.isatty():
            fail(opts, "paid run without a terminal, pass --yes or --max-cost")
        if not typer.confirm("continue"):
            raise typer.Exit(FAILED)


def run(opts: Opts, proj: Project, coro, total: int):
    if opts.progress == "ndjson" or (opts.json and opts.progress == "bar"):
        state = {"done": 0, "at": 0.0, "total": total}

        def tick(n: int) -> None:
            state["done"] += n
            now = time.monotonic()
            if opts.progress == "ndjson" and (now - state["at"] > 1 or state["done"] >= state["total"]):
                state["at"] = now
                print(
                    json.dumps({"event": "progress", "done": state["done"], "total": state["total"]}), file=sys.stderr
                )

        proj.progress = tick
        proj.add_total = lambda n: state.update(total=state["total"] + n)
        try:
            return asyncio.run(coro)
        except BudgetExceeded as e:
            fail(opts, str(e))
    with Progress(console=err, transient=True, disable=opts.progress == "none") as bar:
        task = bar.add_task("running", total=total)
        proj.progress = lambda n: bar.advance(task, n)
        proj.add_total = lambda n: bar.update(task, total=(bar.tasks[0].total or 0) + n)
        try:
            return asyncio.run(coro)
        except BudgetExceeded as e:
            fail(opts, str(e))


def warned(res: dict, text):
    def show() -> None:
        text()
        for w in res.get("roles_warning", []):
            out.print(w, style="yellow")

    return show


def split(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def pairs(v: list[str] | None) -> list[tuple[str, str]] | None:
    return [(p.split(",", 1)[0], p.split(",", 1)[1]) for p in v] if v else None


def table(*cols: str) -> Table:
    t = Table(box=None, pad_edge=False)
    for c in cols:
        t.add_column(c, overflow="fold")
    return t


def write_config(path: Path, server: dict, model: str, secrets: dict[str, str], extra: dict | None = None) -> None:
    data = {"server": server, "models": [model], "modes": ["json"], "budget_usd": 5.0, **(extra or {})}
    Config.model_validate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    write_env(path, secrets)


def pick_model(model: str | None, quiet: bool = False) -> str:
    if model:
        return model
    found, why = detect()
    if not found:
        raise typer.BadParameter(why, param_hint="--model")
    (err if quiet else out).print(f"using {found}, {why}", style="dim")
    return found


@app.command()
def init(
    path: Path = Path("misroute.yaml"),
    from_: Annotated[
        str | None, typer.Option("--from", help="claude, cursor, vscode or a path to an MCP config")
    ] = None,
    server: Annotated[str | None, typer.Option(help="server name in that config")] = None,
    model: ModelSpec = None,
    demo: Annotated[bool, typer.Option(help="write a demo config with the bundled test server")] = False,
) -> None:
    """Write misroute.yaml, from your MCP client config and the model keys in your environment"""
    if path.exists():
        raise typer.BadParameter(f"{path} exists")
    servers = [] if demo else mcpconfig.found(from_)
    if not servers:
        if from_:
            raise typer.BadParameter(f"no MCP servers found in {from_}", param_hint="--from")
        path.write_text(EXAMPLE.replace("{python}", Path(sys.executable).as_posix()), encoding="utf-8")
        out.print(f"wrote {path} with the bundled test server and a mock model")
        return
    chosen = next((x for x in servers if x[1] == server), None) if server else servers[0]
    if not chosen:
        raise typer.BadParameter(f"{server} not found, choose from {', '.join(x[1] for x in servers)}")
    _, name, source, spec = chosen
    block, secrets = mcpconfig.to_server(spec)
    write_config(path, block, pick_model(model), secrets)
    out.print(f"wrote {path} for {name} from {source}")
    if len(servers) > 1:
        out.print(
            f"other servers: {', '.join(x[1] for x in servers if x is not chosen)}, pick one with --server", style="dim"
        )
    if secrets:
        out.print(f"secrets saved to {env_path(path)}: {', '.join(secrets)}", style="dim")


def target_server(target: str) -> tuple[dict, dict[str, str]]:
    """A server block from a URL, an OpenAPI file, a tool list, an MCP client config or a command."""
    if target.startswith(("http://", "https://")):
        return {"url": target}, {}
    path = Path(target)
    if path.suffix.lower() in (".json", ".yaml", ".yml") and path.exists():
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
            return {"openapi": target}, {}
        if isinstance(data, dict) and ("mcpServers" in data or "servers" in data):
            return mcpconfig.to_server(next(iter(mcpconfig.servers_in(path).values())))
        return {"tools": target}, {}
    return {"command": shlex.split(target, posix=sys.platform != "win32")}, {}


@app.command()
def quick(
    target: Annotated[str | None, typer.Argument(help="URL, OpenAPI file, tool list, MCP config or command")] = None,
    model: ModelSpec = None,
    max_cost: Annotated[float, typer.Option("--max-cost")] = 1.0,
    modes: Annotated[str, typer.Option(help="comma separated")] = "json",
    open_: Annotated[bool, typer.Option("--open/--no-open")] = True,
    json_: Json = False,
    progress: ProgressMode = "bar",
) -> None:
    """Lint and a small capped discover in one go, then open the report"""
    from misroute.report.html import render

    opts = Opts("quick", json_, True, max_cost, progress=progress)
    note = err if json_ else out
    config = Path("misroute.yaml")
    system_certs(config)
    if not config.exists():
        if target:
            block, secrets = target_server(target)
        else:
            servers = mcpconfig.found()
            if not servers:
                fail(opts, "give a server: a URL, an OpenAPI file, a tool list or a command")
                return
            block, secrets = mcpconfig.to_server(servers[0][3])
            note.print(f"using {servers[0][1]} from {servers[0][2]}", style="dim")
        settings = {"budget_usd": max_cost}
        write_config(config, block, pick_model(model, json_), secrets, settings)
        note.print(f"wrote {config}", style="dim")
    proj = load(opts, config)
    proj.cfg.modes = [m for m in split(modes) or ["json"] if m in ("json", "code")]  # type: ignore[assignment]
    # quick trades power for price: fewer rotations and single repeats keep the
    # capped run cheap. Plain discover keeps the powered defaults.
    proj.cfg.rotations = min(proj.cfg.rotations, 2)
    proj.cfg.repeats = 1
    lint_res = asyncio.run(proj.lint(False))
    calls, cost = asyncio.run(proj.discover_cost(proj.cfg.tested(), list(proj.cfg.modes)))
    gate(opts, calls, cost)
    res = run(opts, proj, proj.discover(None, list(proj.cfg.modes)), calls)
    report = render(proj.store, proj.store.root / "report.html")

    def text() -> None:
        out.print(f"{lint_res['tools']} tools, {len(lint_res['findings'])} with weak descriptions")
        for s_ in res["spots"][:5]:
            out.print(f"  {s_['a']}  {s_['b']}  {s_['score']:.2f}")
        out.print(f"spent ${res['spent']:.2f}, report {report}")

    if open_ and not json_:
        webbrowser.open(report.resolve().as_uri())
    emit(opts, {"lint": lint_res, "discover": res, "report": str(report)}, text)


@app.command()
def doctor(config: Cfg = Path("misroute.yaml"), offline: bool = False, json_: Json = False) -> None:
    """Check Python, config, server, keys, prices and one call per model, with a fix for each problem"""
    from misroute import doctor as doc

    opts = Opts("doctor", json_)
    system_certs(config)
    rows = asyncio.run(doc.run(config, offline))
    ok = all(r["ok"] for r in rows)

    def text() -> None:
        for r in rows:
            mark = "[green]ok[/green]  " if r["ok"] else "[red]fail[/red]"
            out.print(f"{mark} {r['check']:18} {r['detail']}")
            if not r["ok"] and r["fix"]:
                out.print(f"       {r['fix']}", style="yellow")

    emit(opts, {"checks": rows, "ok": ok}, text, OK if ok else PROBLEM)


@app.command()
def explain(
    pair: Annotated[str, typer.Option("--pair", help="tool_a,tool_b")],
    config: Cfg = Path("misroute.yaml"),
    limit: int = 5,
    json_: Json = False,
) -> None:
    """Show misrouted requests between two tools in both directions, with what the model did"""
    from misroute.explain import explain as explain_pair

    opts = Opts("explain", json_)
    proj = load(opts, config)
    a, b = pair.split(",", 1)
    res = explain_pair(proj.store, a.strip(), b.strip(), limit)

    def text() -> None:
        for d, v in res["directions"].items():
            src, dst = d.split(">")
            out.print(
                f"[bold]requests for {src}[/bold]: {v['wrong']} went to {dst}, "
                f"{v['hedge']} called both, of {v['trials']}"
            )
            for e in v["examples"]:
                out.print(f"  {e['kind']:5} {e['model']} {e['mode']}  {e['request']}")
                out.print(f"        called {', '.join(e['called']) or 'nothing'}", style="dim")

    emit(opts, res, text)


def guide_text() -> str:
    from importlib import resources

    docs = resources.files("misroute.docs")
    return "\n\n".join(docs.joinpath(f).read_text(encoding="utf-8") for f in ("AGENTS.md", "llms.txt"))


@app.command()
def guide() -> None:
    """Print the agent guide and reference: commands, rules, exit codes and JSON fields"""
    print(guide_text())


@app.command()
def schema(
    name: Annotated[
        str, typer.Argument(help="lint, discover, fix, check, improve, explain, doctor, estimate or error")
    ],
) -> None:
    """Print the JSON Schema for a command's --json output"""
    from importlib import resources

    print(resources.files("misroute.schemas").joinpath(f"{name}.json").read_text(encoding="utf-8"))


@app.command()
def lint(
    config: Cfg = Path("misroute.yaml"),
    draft: bool = False,
    json_: Json = False,
    strict: Annotated[bool, typer.Option(help="exit 1 when any description is weak")] = False,
    sarif: Annotated[Path | None, typer.Option(help="write findings as SARIF for code scanning")] = None,
) -> None:
    """Flag missing, name only and path only descriptions"""
    opts = Opts("lint", json_)
    proj = load(opts, config)
    res = asyncio.run(proj.lint(draft))
    if sarif:
        source = proj.cfg.server.openapi or proj.cfg.server.tools or str(config)
        results = [
            (f"description-{f['issue'].replace(' ', '-')}", "warning", f"{f['tool']}: {f['issue']} description", source)
            for f in res["findings"]
        ]
        export.sarif(results, sarif)

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

    emit(opts, res, text, FAILED if strict and res["findings"] else OK)


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
    progress: ProgressMode = "bar",
) -> None:
    """Find risky tools from model behaviour"""
    opts = Opts("discover", json_, yes, max_cost, estimate_only, progress)
    proj = load(opts, config)
    ms, md = split(models), split(modes)
    calls, cost = asyncio.run(proj.discover_cost(ms or proj.cfg.tested(), md or list(proj.cfg.modes)))
    gate(opts, calls, cost)
    res = run(opts, proj, proj.discover(ms, md), calls)

    def text() -> None:
        t = table("tool", "sibling", "score", "95% ci", "signals", "direction", "seen in", "note")
        for s in res["spots"][:15]:
            sig = "  ".join(f"{k} {v:.2f}" for k, v in s["signals"].items())
            d = s["directions"]
            dirs = f"{d.get(s['a'] + '>' + s['b'], 0)} to {d.get(s['b'] + '>' + s['a'], 0)}" if d else ""
            gone = [x for x in (s["a"], s["b"]) if x in res["untested"]]
            flags = (("gate failed", s["gate_failed"]), ("few requests", s["thin"] and not gone))
            note = ", ".join([*(f"UNTESTED {x}" for x in gone), *(x for x, on in flags if on)])
            lo, hi = s["ci95"]
            row = (s["a"], s["b"], f"{s['score']:.2f}", f"{lo:.2f}-{hi:.2f}", sig, dirs, ", ".join(s["where"]), note)
            t.add_row(*row, style="red" if s["write"] else "")
        for u in res["unrouted"]:
            if not any(u["tool"] in (s["a"], s["b"]) for s in res["spots"]) or u["rate"] >= 0.2:
                row = (
                    u["tool"],
                    "(no tool called)",
                    f"{u['rate']:.2f}",
                    f"{u['count']} of {u['trials']}",
                    "unrouted",
                    "",
                    ", ".join(u["where"]),
                    "",
                )
                t.add_row(*row, style="yellow")
        out.print(t)
        for tool in res["untested"]:
            out.print(
                f"UNTESTED {tool}: the validator kept none of its requests, so it was never tested as a target. "
                "Its scores only count other tools' requests landing on it, a low score does not clear it",
                style="bold red",
            )
        for u in res["unrouted"]:
            out.print(
                f"{u['tool']}: {u['rate']:.0%} of its requests ended with no tool call ({u['count']} of "
                f"{u['trials']}, {', '.join(u['where'])}), e.g. {u['example']!r}",
                style="yellow",
            )
        if res["gate_failed"]:
            out.print(
                f"{len(res['gate_failed'])} tool pairs have request sets that surface words can tell apart. "
                "Their spots stay in the ranking marked 'gate failed': the rate is real model behaviour, but "
                "surface cues may have helped or hurt it, so confirm with probe or real_requests before acting"
            )
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

    emit(opts, res, warned(res, text))


@app.command()
def probe(
    config: Cfg = Path("misroute.yaml"),
    top: Annotated[int | None, typer.Option(help="top pairs from discover, default every flagged pair")] = None,
    pair: Pairs = None,
    models: Names = None,
    modes: Names = None,
    json_: Json = False,
    yes: Yes = False,
    max_cost: MaxCost = None,
    estimate_only: EstimateOnly = False,
    progress: ProgressMode = "bar",
) -> None:
    """Harder requests on the risky pairs"""
    opts = Opts("probe", json_, yes, max_cost, estimate_only, progress)
    proj = load(opts, config)
    ms = split(models) or proj.cfg.tested()
    md = split(modes) or list(proj.cfg.modes)
    calls, cost = asyncio.run(proj.cost("probe", ms, md, top, pairs(pair)))
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
    top: Annotated[int | None, typer.Option(help="top pairs from discover, default every flagged pair")] = None,
    pair: Pairs = None,
    trials: Annotated[int, typer.Option(help="trials per condition, 96 as in the thesis design")] = 96,
    models: Names = None,
    modes: Names = None,
    json_: Json = False,
    yes: Yes = False,
    max_cost: MaxCost = None,
    estimate_only: EstimateOnly = False,
    progress: ProgressMode = "bar",
) -> None:
    """Test a boundary clause against a neutral edit or a drafted description"""
    opts = Opts("fix", json_, yes, max_cost, estimate_only, progress)
    proj = load(opts, config)
    ms = split(models) or proj.cfg.tested()
    md = split(modes) or list(proj.cfg.modes)
    calls, cost = asyncio.run(proj.cost("fix", ms, md, top, pairs(pair), trials))
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
            note = f"  under {MIN_PAIRS} pairs, cannot reach p below {ALPHA}" if a["underpowered"] else ""
            out.print(
                f"{a['model']} {a['mode']}  {a['pairs']} flagged  mean {a['mean_diff']:+.2f}  p {a['p']:.3f}{note}"
            )

    emit(opts, res, warned(res, text), OK if any(r["flagged"] for r in res["pairs"]) else INSUFFICIENT)


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
    progress: ProgressMode = "bar",
) -> None:
    """Check whether a proposed description beats the current one on held out requests"""
    from misroute import check as checker

    opts = Opts("check", json_, yes, max_cost, estimate_only, progress)
    proj = load(opts, config)
    ms, md = split(models) or proj.cfg.tested(), split(modes) or ["json"]
    tools = asyncio.run(proj.tools())
    calls, cost = checker.estimate(proj, 1 + len(checker.neighbours(proj, tool)), ms, md, tools)
    gate(opts, calls, cost)
    res = run(opts, proj, checker.check(proj, tool, description, refresh=refresh, models_=ms, modes=md), calls)

    def text() -> None:
        w, h = res["wrong_rate"], res["hedge_rate"]
        out.print(f"{res['verdict']}  {res['reason']}  baseline {res['baseline_kind']}  trials {res['trials']}")
        for cond in w:
            out.print(f"  {cond:9} wrong {w[cond]:.2f}  hedge {h[cond]:.2f}")
        if res["leakage"]:
            out.print(f"  leakage to {', '.join(res['leakage'])}")
        out.print(
            f"  held out verdict {res['looks']} on this set, p scaled by {res['looks']}, spent ${res['cost_usd']:.2f}"
        )

    emit(opts, res, warned(res, text), check_code(res["verdict"]))


@app.command()
def improve(
    target: Annotated[
        str | None,
        typer.Argument(help="OpenAPI or Swagger file, MCP URL, tool list or command, else the server in misroute.yaml"),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="models under test, provider:model or a configured name, comma separated"),
    ] = None,
    helper: Annotated[
        str | None,
        typer.Option(help="model that writes and judges requests and writes candidates, from another family"),
    ] = None,
    languages: Annotated[str | None, typer.Option(help="languages to write requests in, for example no,en")] = None,
    fastmcp: Annotated[
        bool, typer.Option(help="also write a FastMCP server.py and confirm against it, OpenAPI only")
    ] = False,
    out_dir: Annotated[Path, typer.Option("--out", help="folder for the results")] = Path("improve"),
    config: Cfg = Path("misroute.yaml"),
    json_: Json = False,
    yes: Yes = False,
    max_cost: MaxCost = None,
    estimate_only: EstimateOnly = False,
    progress: ProgressMode = "bar",
    domain: Annotated[
        str | None, typer.Option(help="glossary for writing requests, text or a file", rich_help_panel=ADVANCED)
    ] = None,
    real_requests: Annotated[
        Path | None,
        typer.Option(help="JSON lines of real user requests with text, tool and lang", rich_help_panel=ADVANCED),
    ] = None,
    requests_from: Annotated[
        str | None,
        typer.Option(
            help="interface writes requests from routes, parameters and fields, descriptions from the descriptions. "
            "Default interface",
            rich_help_panel=ADVANCED,
        ),
    ] = None,
    max_groups: Annotated[
        int | None,
        typer.Option(
            help="work on the first groups only, default every group discover flagged", rich_help_panel=ADVANCED
        ),
    ] = None,
    styles: Annotated[
        str | None,
        typer.Option(
            help="candidate styles: boundary, returns, example, minimal. Default all", rich_help_panel=ADVANCED
        ),
    ] = None,
    max_chars: Annotated[
        int | None, typer.Option(help="cut candidate descriptions to this many characters", rich_help_panel=ADVANCED)
    ] = None,
    modes: Annotated[str, typer.Option(help="json, code or both, comma separated", rich_help_panel=ADVANCED)] = "json",
    drafts: Annotated[
        bool, typer.Option(help="draft descriptions for weak tools outside any group", rich_help_panel=ADVANCED)
    ] = True,
) -> None:
    """Write, race and confirm new descriptions for confused tools, then export them for review"""
    from misroute.config import ServerConfig
    from misroute.config import load as load_config
    from misroute.improve import run as improver

    opts = Opts("improve", json_, yes, max_cost, estimate_only, progress)
    system_certs(config)
    block, secrets = target_server(target) if target else (None, {})
    tested = split(model)
    try:
        if config.exists():
            cfg = load_config(config)
            if block:
                cfg = cfg.model_copy(update={"server": ServerConfig.model_validate(block)})
        elif block:
            first = tested[0] if tested else pick_model(None, json_)
            cfg = Config.model_validate({"server": block, "models": [first], "modes": ["json"]})
        else:
            fail(opts, "give a target: an OpenAPI or Swagger file, an MCP URL, a tool list or a command")
            return
        write_env(config, secrets)
        cfg, ms = improver.configure(
            cfg, tested, helper, requests_from, split(languages), domain, str(real_requests) if real_requests else None
        )
        chosen = improver.pick_styles(split(styles))
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as e:
        fail(opts, f"config problem: {e}")
        return
    if fastmcp and not cfg.server.openapi:
        fail(opts, "--fastmcp needs an OpenAPI or Swagger source")
    if max_cost is not None:
        cfg.budget_usd = min(cfg.budget_usd, max_cost)
    proj = Project(cfg, root=config.parent / cfg.workdir)
    md = split(modes) or ["json"]
    calls, cost = asyncio.run(improver.estimate(proj, ms, md))
    gate(opts, calls, cost, improver.STAGES_NOTE)
    res = run(
        opts,
        proj,
        improver.improve(proj, ms, md, max_groups, chosen, max_chars, out_dir, fastmcp, drafts),
        calls,
    )

    def text() -> None:
        a = res["audit"]
        out.print(f"{a['tools']} tools, {a['reads']} read, {a['writes']} write")
        for kind, n in sorted(a["counts"].items()):
            out.print(f"  {n} {kind} notes")
        for g in res["groups"]:
            t = g["confirm"]["tests"]["winner_vs_baseline"]
            r = g["confirm"]["rates"]
            rates = "  ".join(f"{c} {v['error_rate']:.2f}" for c, v in r.items())
            out.print(f"group {g['group']}: {', '.join(g['tools'])}")
            out.print(f"  held out errors  {rates}")
            out.print(f"  winner vs baseline {t['verdict']}, {t['reason']}, export {g['export']}: {g['why']}")
            for code, tests in g["confirm"]["by_language"].items():
                x = tests["winner_vs_baseline"]
                out.print(f"  {code}: {x['verdict']}, {x['reason']}", style="dim")
        counts: dict[str, int] = {}
        for e in res["entries"]:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        out.print("  ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        if res.get("fastmcp"):
            f = res["fastmcp"]
            out.print(f"FastMCP: {'matches' if f.get('ok') else 'differs'}, {f.get('tools', 0)} tools")
            for k in ("error", "missing", "descriptions_differ", "hints_differ"):
                if f.get(k):
                    out.print(f"  {k.replace('_', ' ')}: {f[k]}", style="yellow")
        for name, path in res.get("files", {}).items():
            out.print(f"  {name}: {path}")
        for w in res.get("warnings", []):
            out.print(w, style="yellow")
        out.print(f"spent ${res['cost_usd']:.2f}. Review the files, nothing was applied", style="dim")

    emit(opts, res, warned(res, text))


@app.command()
def report(config: Cfg = Path("misroute.yaml"), output: Path = Path("report.html")) -> None:
    """Write a self contained HTML report"""
    from misroute.report.html import render

    proj = Project.from_yaml(config)
    out.print(f"wrote {render(proj.store, output, proj.cfg.guard.log)}")


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
    junit: Annotated[Path | None, typer.Option(help="write results as JUnit XML")] = None,
    sarif: Annotated[Path | None, typer.Option(help="write problems as SARIF for code scanning")] = None,
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
        status = {"worse": "failed", "insufficient_data": "skipped"}
        if junit:
            cases = [
                (
                    r["tool"],
                    status.get(r.get("verdict", ""), "passed" if "verdict" in r else "skipped"),
                    r.get("reason") or r.get("note", ""),
                )
                for r in rows
            ]
            export.junit("misroute changed tools", cases, junit)
        if sarif:
            source = proj.cfg.server.openapi or proj.cfg.server.tools or str(config)
            export.sarif(
                [
                    ("description-worse", "error", f"{r['tool']}: new description routes worse, {r['reason']}", source)
                    for r in rows
                    if r.get("verdict") == "worse"
                ],
                sarif,
            )
        emit(opts, {"changed": rows}, lambda: out.print(changed_markdown(rows)), code)
        return
    ms, md = split(models), split(modes)
    calls, cost = asyncio.run(proj.discover_cost(ms or proj.cfg.tested(), md or list(proj.cfg.modes)))
    gate(opts, calls, cost)
    run(opts, proj, proj.discover(ms, md), calls)
    bad = proj.breaches()
    if junit:
        export.junit("misroute limits", [(b, "failed", b) for b in bad] or [("limits", "passed", "")], junit)
    if sarif:
        export.sarif([("limit-exceeded", "error", b, str(config)) for b in bad], sarif)

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
            n = 1 + len(checker.neighbours(proj, t.name))
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
