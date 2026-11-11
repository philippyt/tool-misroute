import asyncio
import json
import shlex
import sys
import time
import webbrowser
from dataclasses import dataclass, field
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
from misroute.models.base import BudgetExceeded
from misroute.models.presets import detect
from misroute.project import PILOT_CALLS, Project
from misroute.store.cache import no_price
from misroute.store.runs import SCHEMA_VERSION

OK, FAILED, INSUFFICIENT, PROBLEM = 0, 1, 2, 3

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    epilog="Start with misroute quick. Using misroute from an agent: run misroute guide for commands, exit "
    "codes and JSON fields.",
)
requests_app = typer.Typer(no_args_is_help=True, help="Build a set of real user requests")
app.add_typer(requests_app, name="requests")
out = Console()
err = Console(stderr=True)

Cfg = Annotated[Path, typer.Option("--config", "-c")]
Names = Annotated[str | None, typer.Option(help="comma separated")]
Pairs = Annotated[list[str] | None, typer.Option("--pair", help="tool_a,tool_b")]
Json = Annotated[bool, typer.Option("--json", help="print one JSON object")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="do not ask before paid runs")]
MaxCost = Annotated[float | None, typer.Option("--max-cost", help="stop when spending reaches this many dollars")]
Pilot = Annotated[
    bool,
    typer.Option(help="run 3 real trials per model and mode first, so the estimate uses measured reply sizes"),
]
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
    extra: dict[str, Any] = field(default_factory=dict)


def emit(opts: Opts, data: dict[str, Any], text=None, code: int = OK) -> None:
    if opts.json:
        body = {"schema_version": SCHEMA_VERSION, "command": opts.command, **opts.extra, **data}
        print(json.dumps(body, default=str))
    elif text:
        text()
    if code != OK:
        raise typer.Exit(code)


def fail(opts: Opts, message: str, code: int = PROBLEM, **extra: Any) -> None:
    if not opts.json:
        err.print(f"[red]{message}[/red]")
    emit(opts, {"error": message, "exit_code": code, **extra}, code=code)


def load(opts: Opts, config: Path) -> Project:
    system_certs(config)
    try:
        proj = Project.from_yaml(config)
    except (OSError, ValidationError, yaml.YAMLError) as e:
        fail(opts, f"config problem: {e}")
        raise
    if opts.max_cost is not None:
        proj.cap(opts.max_cost)
    return proj


def gate(opts: Opts, proj: Project, calls: int, cost: float, note: str = "") -> None:
    if missing := proj.unpriced():
        fail(opts, no_price(", ".join(missing)))
    estimate = {"estimate": {"calls": calls, "cost_usd": round(cost, 4), "upper_bound": False, "note": note}}
    if opts.estimate_only:

        def show() -> None:
            out.print(f"about {calls} model calls, about ${cost:.2f}")
            if note:
                out.print(note, style="dim")

        emit(opts, estimate, show, OK)
        raise typer.Exit(OK)
    opts.extra.update(estimate)
    if opts.max_cost is not None and cost > opts.max_cost:
        estimate["estimate"]["above_max_cost"] = True
        err.print(
            f"estimate ${cost:.2f} is above max cost ${opts.max_cost:.2f}. The estimate is a guide, the run starts "
            f"and stops at ${proj.budget.limit:.2f} if spending gets there, keeping finished trials",
            style="yellow",
        )
    if not opts.json:
        out.print(f"about {calls} model calls, about ${cost:.2f}, stops at ${proj.budget.limit:.2f}")
        if note:
            out.print(note, style="dim")
    if cost > 0 and not opts.yes and opts.max_cost is None:
        if not sys.stdin.isatty():
            fail(opts, "paid run without a terminal, pass --yes or --max-cost")
        if not typer.confirm("continue"):
            raise typer.Exit(FAILED)


def piloted(opts: Opts, proj: Project, wanted: bool, models_: list[str], modes: list[str]) -> None:
    """With --pilot, run a few real trials before the estimate and say what they cost."""
    if not wanted:
        return
    if missing := proj.unpriced():
        fail(opts, no_price(", ".join(missing)))
    n = len(models_) * len(modes) * PILOT_CALLS
    done = run(opts, proj, proj.pilot(models_, modes), n)
    opts.extra["pilot"] = {"trials": done, "cost_usd": round(proj.budget.spent, 4)}
    if not opts.json:
        out.print(f"pilot: {done} trials, ${proj.budget.spent:.2f}, the estimate below uses their reply sizes")


def run(opts: Opts, proj: Project, coro, total: int):
    if opts.progress == "ndjson" or (opts.json and opts.progress == "bar"):
        state = {"done": 0, "at": 0.0, "total": total}

        def tick(n: int) -> None:
            state["done"] += n
            now = time.monotonic()
            if opts.progress == "ndjson" and (now - state["at"] > 1 or state["done"] >= state["total"]):
                state["at"] = now
                line = json.dumps({"event": "progress", "done": state["done"], "total": state["total"]})
                print(line, file=sys.stderr, flush=True)

        proj.progress = tick
        proj.add_total = lambda n: state.update(total=state["total"] + n)
        try:
            return asyncio.run(coro)
        except BudgetExceeded as e:
            fail(opts, str(e), spent=round(proj.budget.spent, 4))
    with Progress(console=err, transient=True, disable=opts.progress == "none") as bar:
        task = bar.add_task("running", total=total)
        proj.progress = lambda n: bar.advance(task, n)
        proj.add_total = lambda n: bar.update(task, total=(bar.tasks[0].total or 0) + n)
        try:
            return asyncio.run(coro)
        except BudgetExceeded as e:
            fail(opts, str(e), spent=round(proj.budget.spent, 4))


def warned(res: dict, text):
    def show() -> None:
        text()
        for w in res.get("roles_warning", []):
            out.print(w, style="yellow")

    return show


def usage_note(opts: Opts, res: dict) -> None:
    """Measured calls, tokens and dollars per model, and the estimate they are held against."""
    for name, u in (res.get("usage") or {}).items():
        out.print(
            f"  {name} ({', '.join(u['roles']) or 'unused'}): {u['calls']} calls, {u['cached']} from cache, "
            f"{u['tokens_in']} tokens in, {u['tokens_out']} out, ${u['cost_usd']:.2f}",
            style="dim",
        )
    if est := opts.extra.get("estimate"):
        out.print(f"  the estimate was ${est['cost_usd']:.2f} for {est['calls']} calls", style="dim")


def cells_note(res: dict) -> None:
    """One line per model and mode that keeps the first choice, reaching the target and the arguments apart."""
    for c in res["cells"]:
        ended = ", ".join(f"{k} {v:.0%}" for k, v in c["failures"].items())
        out.print(
            f"{c['model']} {c['mode']}: {c['trials']} trials, first choice right {c['accuracy']:.0%}, target "
            f"reached {c['reached_rate']:.0%}, wrong tool {c['wrong_rate']:.0%}, both called {c['hedge_rate']:.0%}"
            + (f", {ended}" if ended else "")
        )
        open_ = c["trials"] - c["decided"]
        if open_:
            out.print(
                f"  {open_} trials ended before a choice was seen. Of the {c['decided']} where one was, "
                f"{c['accuracy_decided']:.0%} were right. Raise max_turns to see more of them through",
                style="dim",
            )
        if c["unread_pages_rate"]:
            out.print(
                f"  {c['unread_pages_rate']:.0%} of programs left later pages unread, a fault only when the request "
                "needed everything",
                style="dim",
            )
        if c["bad_args"]:
            worst = c["bad_args"][0]
            out.print(
                f"  arguments broke the tool's schema in {c['bad_args_rate']:.0%} of trials, most often "
                f"{worst['problem']!r} ({worst['trials']}). The tool choice score does not see this",
                style="yellow",
            )


def lookup_note(res: dict) -> None:
    if res["lookup_tools"]:
        out.print(f"lookup tools, not scored as choices: {', '.join(res['lookup_tools'])}", style="dim")
    for row in res["lookup_candidates"]:
        out.print(
            f"{row['tool']} was called before the right tool in {row['trials']} trials, on the way to "
            f"{', '.join(row['targets'])}. If agents are meant to call it first, add it to lookup_tools in "
            "misroute.yaml so those calls stop counting against the tools it leads to",
            style="yellow",
        )
    ended = sum(c["failures"].get("lookup_only", 0) + c["failures"].get("read_first", 0) for c in res["cells"])
    if ended and res["max_turns"] == 1:
        out.print(
            "Some trials ended on a lookup or a read before the choice was seen. Set max_turns to 2 or more to "
            "let them continue",
            style="dim",
        )


def effect_note(names: list[str]) -> None:
    if names:
        out.print(
            f"{', '.join(names)}: no read or write annotation and the name does not say, so these count as reads. "
            "Set readOnlyHint or destructiveHint on the tools, a write scored as a read counts its own "
            "preparatory reads as confusion",
            style="yellow",
        )


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


def setup(
    opts: Opts,
    config: Path,
    target: str | None,
    from_: str | None,
    server: str | None,
    demo: bool,
    model: str | None,
    settings: dict,
) -> None:
    """Write misroute.yaml for the bundled demo, a given target, or a server from an MCP client config."""
    note = err if opts.json else out
    if demo:
        config.write_text(EXAMPLE.replace("{python}", Path(sys.executable).as_posix()), encoding="utf-8")
        note.print(f"wrote {config} with the bundled test server and a mock model", style="dim")
        return
    if target:
        block, secrets = target_server(target)
        note.print(f"wrote {config}", style="dim")
    else:
        servers = mcpconfig.found(from_)
        chosen = next((x for x in servers if x[1] == server), None) if server else (servers or [None])[0]
        if not chosen:
            if server and servers:
                fail(opts, f"{server} not found, choose from {', '.join(x[1] for x in servers)}")
            fail(
                opts,
                f"no MCP servers found in {from_}"
                if from_
                else "give a server: a URL, an OpenAPI file, a tool list or a command, or --demo for the bundled one",
            )
            return
        _, name, source, spec = chosen
        block, secrets = mcpconfig.to_server(spec)
        note.print(f"wrote {config} for {name} from {source}", style="dim")
        others = [x[1] for x in servers if x is not chosen]
        if others:
            note.print(f"other servers: {', '.join(others)}, pick one with --server", style="dim")
    write_config(config, block, pick_model(model, opts.json), secrets, settings)
    if secrets:
        note.print(f"secrets saved to {env_path(config)}: {', '.join(secrets)}", style="dim")


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
    config: Cfg = Path("misroute.yaml"),
    from_: Annotated[
        str | None,
        typer.Option("--from", help="take the server from claude, cursor, vscode or a path to an MCP config"),
    ] = None,
    server: Annotated[str | None, typer.Option(help="server name in that config")] = None,
    demo: Annotated[bool, typer.Option(help="use the bundled test server and a mock model, free")] = False,
    config_only: Annotated[bool, typer.Option("--config-only", help="write misroute.yaml and stop")] = False,
) -> None:
    """Set up misroute.yaml when there is none, then lint and a small capped discover, and open the report"""
    from misroute.report.html import render

    opts = Opts("quick", json_, True, max_cost, progress=progress)
    system_certs(config)
    if config_only and config.exists():
        fail(opts, f"{config} exists")
    if not config.exists():
        setup(opts, config, target, from_, server, demo, model, {} if config_only else {"budget_usd": max_cost})
    if config_only:
        emit(opts, {"config": str(config)})
        return
    proj = load(opts, config)
    proj.cfg.modes = [m for m in split(modes) or ["json"] if m in ("json", "code")]  # type: ignore[assignment]
    # quick trades power for price: fewer rotations and single repeats keep the
    # capped run cheap. Plain discover keeps the powered defaults.
    proj.cfg.rotations = min(proj.cfg.rotations, 2)
    proj.cfg.repeats = 1
    lint_res = asyncio.run(proj.lint(False))
    calls, cost = asyncio.run(proj.discover_cost(proj.cfg.tested(), list(proj.cfg.modes)))
    gate(opts, proj, calls, cost)
    res = run(opts, proj, proj.discover(None, list(proj.cfg.modes)), calls)
    report = render(proj.store, proj.store.root / "report.html")

    def text() -> None:
        out.print(f"{lint_res['tools']} tools, {len(lint_res['findings'])} with weak descriptions")
        for s_ in res["spots"][:5]:
            out.print(f"  {s_['a']}  {s_['b']}  {s_['score']:.2f}")
        out.print(f"spent ${res['spent']:.2f}, report {report}")
        out.print("next: misroute discover for the full sample, misroute report --pair a,b to read a pair", style="dim")

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


def pair_report(opts: Opts, proj: Project, pair: str, limit: int) -> None:
    """The misrouted requests between two tools in both directions, with what the model did."""
    from misroute.explain import explain as explain_pair

    if "," not in pair:
        fail(opts, "--pair takes two tool names, tool_a,tool_b")
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


SCHEMAS = "lint, discover, check, check_pair, compare, compare_design, improve, report_pair, doctor, estimate or error"


@app.command()
def guide(
    schema: Annotated[str | None, typer.Option(help=f"print the JSON Schema of one output instead: {SCHEMAS}")] = None,
) -> None:
    """Print the agent guide and reference, or with --schema the JSON Schema of a command's output"""
    from importlib import resources

    if not schema:
        print(guide_text())
        return
    found = resources.files("misroute.schemas").joinpath(f"{schema}.json")
    if not found.is_file():
        fail(Opts("guide", True), f"no schema named {schema}, schemas are {SCHEMAS}")
    print(found.read_text(encoding="utf-8"))


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
    try:
        res = asyncio.run(proj.lint(draft))
    except BudgetExceeded as e:
        fail(opts, str(e))
        return
    except Exception as e:
        if not draft:
            raise
        from misroute.doctor import hint

        why = f"{type(e).__name__}: {e}"
        fail(opts, f"drafting needs the writer model and the call failed, {why}. {hint(why)}".strip())
        return
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
        effect_note(res["effect_unknown"])

    emit(opts, res, text, FAILED if strict and res["findings"] else OK)


def snapshot(config: Path) -> None:
    """Tool names, hashes and descriptions as JSON, the baseline a later ci --changed compares against."""
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
    pilot: Pilot = False,
    probe: Annotated[
        bool, typer.Option(help="then test the flagged pairs with harder requests that borrow the sibling's words")
    ] = False,
    pair: Annotated[
        list[str] | None, typer.Option("--pair", help="tool_a,tool_b to probe instead of the flagged pairs")
    ] = None,
) -> None:
    """Find the tools a model confuses, and with --probe press the risky pairs with harder requests"""
    opts = Opts("discover", json_, yes, max_cost, estimate_only, progress)
    proj = load(opts, config)
    ms, md = split(models), split(modes)
    tested, used = ms or proj.cfg.tested(), md or [str(m) for m in proj.cfg.modes]
    piloted(opts, proj, pilot, tested, used)
    calls, cost = asyncio.run(proj.discover_cost(tested, used))
    probing, note, extra = probe or bool(pair), "", 0
    if probing:
        extra, more = asyncio.run(proj.cost("probe", tested, used, None, pairs(pair)))
        cost += more
        note = "" if pair else "The probe part counts the pairs the last discover flagged, a new ranking can change it"
    if not proj.turns_measured(tested):
        upper = (
            f"Assumes every trial uses all {proj.cfg.max_turns} turns, the upper end. --pilot measures the real number"
        )
        note = f"{note}. {upper}" if note else upper
    gate(opts, proj, calls + extra, cost, note)
    res = run(opts, proj, proj.discover(ms, md), calls)
    if probing:
        res["probe"] = run(opts, proj, proj.probe(None, pairs(pair), tested, used), extra)

    def text() -> None:
        t = table("tool", "sibling", "score", "95% ci", "signals", "direction", "seen in", "note")
        for s in res["spots"][:15]:
            sig = "  ".join(f"{k} {v:.2f}" for k, v in s["signals"].items())
            d = s["directions"]
            dirs = f"{d.get(s['a'] + '>' + s['b'], 0)} to {d.get(s['b'] + '>' + s['a'], 0)}" if d else ""
            gone = [x for x in (s["a"], s["b"]) if x in res["untested"]]
            flags = (
                ("one wording", s["one_wording"]),
                ("few requests", s["thin"] and not gone),
                ("cross server", s["cross_server"]),
            )
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
        cells_note(res)
        out.print(t)
        lookup_note(res)
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
        flagged = sum(s["flagged"] for s in res["spots"])
        lone = [s for s in res["spots"] if s["one_wording"]]
        out.print(f"{flagged} pairs flagged: at least 6 errors in one model and mode, over 2 or more wordings")
        across = sum(s["flagged"] and s["cross_server"] for s in res["spots"])
        if across:
            out.print(f"{across} of them join tools from different servers, which neither server shows on its own")
        if not res["system_prompt"]:
            out.print(
                "run with a one line system prompt. Set system_prompt to your agent's own, as text or a file, to "
                "measure routing under the instructions it has in production",
                style="dim",
            )
        if lone:
            out.print(
                f"{len(lone)} pairs marked 'one wording' reached 6 errors from a single request wording repeated "
                "across slots and orders. That is one observation, so they are not flagged. Read the request "
                "with explain, and add real_requests to test the pair properly",
                style="yellow",
            )
        if res["near_misses"]:
            out.print("near misses, unvalidated, not in the ranking", style="dim")
            for n in res["near_misses"][:10]:
                out.print(f"  {n['tool']}  {n['rival']}  {n['score']:.2f}  {n['where']} {n['source']}", style="dim")
        use = res["request_use"]
        out.print(
            f"requests: written now for {use['written']} tools, reused from an earlier run for {use['reused']}. "
            "Stored requests are reused only when the instructions, domain, source and languages are the same",
            style="dim",
        )
        if res["thin_interface"]:
            out.print(
                f"{len(res['thin_interface'])} tools have no output schema. With request_source: spec the helper "
                "sees only their name and parameters, so it cannot tell what they return and may write requests "
                "that ask for more than they hold. Add output schemas, or use request_source: description when "
                "the descriptions are not the thing under test",
                style="yellow",
            )
        dropped = [r for rs in res["rejected_requests"].values() for r in rs]
        if dropped:
            unfair = [r for r in dropped if not r["picked"]]
            why = f", {len(unfair)} because no tool could do them as asked" if unfair else ""
            out.print(
                f"the validator dropped {len(dropped)} written requests before any trial ran{why}. Each one, the "
                "tool it picked and its reason is under rejected_requests in discover.json",
                style="dim",
            )
            for r in unfair[:3]:
                out.print(f"  {r['request']!r}: {r['reason'] or 'no reason given'}", style="dim")
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
        effect_note(res["effect_unknown"])
        for c in res["cells"]:
            for tool, rows in c["operations"].items():
                missed = sum(r["wrong_operation"] for r in rows.values())
                if missed:
                    out.print(
                        f"{tool} ({c['model']} {c['mode']}): right tool with the wrong selector value in {missed} "
                        f"of {sum(r['trials'] for r in rows.values())} trials",
                        style="yellow",
                    )
        if probing:
            probe_table(res["probe"])
        out.print(f"spent ${proj.budget.spent:.2f}, results in {proj.store.root}")
        usage_note(opts, res)
        if flagged:
            first = next(s for s in res["spots"] if s["flagged"])
            out.print(
                f"next: misroute report --pair {first['a']},{first['b']} to read the requests, misroute improve "
                "to write and test better descriptions",
                style="dim",
            )

    emit(opts, res, warned(res, text))


def probe_table(res: dict) -> None:
    t = table("requests for", "sibling", "model", "mode", "wrong", "hedge", "leak", "gate p")
    for r in res["rows"]:
        for d, v in r["directions"].items():
            src, dst = d.split(">")
            h = f"[red]{v['hedge']:.2f}[/red]" if r["write"] and v["hedge"] else f"{v['hedge']:.2f}"
            t.add_row(src, dst, r["model"], r["mode"], f"{v['wrong']:.2f}", h, f"{v['leak']:.2f}", f"{r['gate_p']:.2f}")
    if res["rows"]:
        out.print("harder requests that borrow the sibling's words", style="dim")
        out.print(t)


def pairs_verdict(rows: list[dict]) -> str:
    """One verdict in check's words over the clause tests: worse when a clause hurts, better when one is worth
    deploying, insufficient_data when no pair could be decided, else no_difference."""
    said = [r["verdict"] for r in rows]
    if any(v == "clause hurts" for v in said):
        return "worse"
    if any(v.startswith("deploy") for v in said):
        return "better"
    if all(v == "not flagged" or v.startswith("too few") for v in said):
        return "insufficient_data"
    return "no_difference"


def check_pairs(
    opts: Opts, proj: Project, top: int | None, pair: list[str] | None, trials: int, ms: list[str], md: list[str]
) -> None:
    """Draft a boundary clause for each pair and test it against the original and a neutral edit."""
    calls, cost = asyncio.run(proj.cost("fix", ms, md, top, pairs(pair), trials))
    gate(opts, proj, calls, cost)
    res = run(opts, proj, proj.fix(top, pairs(pair), ms, md, trials), calls)
    res["verdict"] = pairs_verdict(res["pairs"])

    def text() -> None:
        t = table(
            "tool", "sibling", "model", "mode", "original", "baseline", "with clause", "vs original", "p", "verdict"
        )
        for r in res["pairs"]:
            e, p = r["errors"], r.get("p_holm")
            t.add_row(
                *r["pair"].split("|"),
                r["model"],
                r["mode"],
                str(e["original"]),
                f"{e['baseline']} {r['baseline_kind']}",
                str(e["clause"]),
                f"{e['clause'] - e['original']:+d} (p {r['vs_original']['p']:.3f})",
                "" if p is None else f"{p:.3f}",
                r["verdict"],
            )
        out.print(t)
        for pair_name, ratio in sorted({(r["pair"], r["edits"].get("host_ratio", 0)) for r in res["pairs"]}):
            if ratio > 5:
                out.print(
                    f"{pair_name}: a description is {ratio:.0f}x the clause, one appended sentence may drown; "
                    "consider improve for a full rewrite",
                    style="yellow",
                )
        # below the pair minimum the across-pairs sign flip cannot reach
        # significance; printing its p reads as a failed result instead of an
        # inapplicable one
        for a in res["across"]:
            if a["underpowered"]:
                continue
            out.print(f"{a['model']} {a['mode']}  {a['pairs']} flagged  mean {a['mean_diff']:+.2f}  p {a['p']:.3f}")

    emit(opts, res, warned(res, text), check_code(res["verdict"]))


def check_code(verdict: str) -> int:
    return {"worse": FAILED, "insufficient_data": INSUFFICIENT}.get(verdict, OK)


@app.command()
def check(
    tool: Annotated[str | None, typer.Option(help="tool whose description you changed")] = None,
    description: Annotated[str | None, typer.Option(help="your proposed description for it")] = None,
    pair: Annotated[
        list[str] | None,
        typer.Option("--pair", help="tool_a,tool_b: draft a boundary clause for the two and test it instead"),
    ] = None,
    flagged: Annotated[bool, typer.Option(help="draft and test a clause for every pair discover flagged")] = False,
    config: Cfg = Path("misroute.yaml"),
    refresh: Annotated[bool, typer.Option(help="draw a fresh held out set")] = False,
    models: Names = None,
    modes: Names = None,
    json_: Json = False,
    yes: Yes = False,
    max_cost: MaxCost = None,
    estimate_only: EstimateOnly = False,
    progress: ProgressMode = "bar",
    top: Annotated[
        int | None, typer.Option(help="with --flagged, only the top pairs from discover", rich_help_panel=ADVANCED)
    ] = None,
    trials: Annotated[
        int, typer.Option(help="with --pair, trials per condition, 96 as in the thesis", rich_help_panel=ADVANCED)
    ] = 96,
    strict_roles: Annotated[
        bool,
        typer.Option(
            "--strict-roles",
            help="with --pair, refuse to run when a role shares a family with a tested model",
            rich_help_panel=ADVANCED,
        ),
    ] = False,
) -> None:
    """Test a description change: your own text for one tool, or a drafted boundary clause for a confused pair"""
    from misroute import check as checker

    opts = Opts("check", json_, yes, max_cost, estimate_only, progress)
    proj = load(opts, config)
    ms = split(models) or proj.cfg.tested()
    if pair or flagged or top:
        # the writer authors the clause under test and the validator keeps the
        # requests it is tested on, so same-family roles make the result
        # circular rather than merely biased
        if strict_roles and proj.cfg.role_warnings():
            fail(opts, "; ".join(proj.cfg.role_warnings()))
        check_pairs(opts, proj, top, pair, trials, ms, split(modes) or list(proj.cfg.modes))
        return
    if not tool or description is None:
        fail(
            opts,
            "give --tool with --description to test your own text, --pair tool_a,tool_b to draft and test a "
            "boundary clause, or --flagged for every pair discover flagged",
        )
        return
    md = split(modes) or ["json"]
    tools = asyncio.run(proj.tools())
    calls, cost = checker.estimate(proj, 1 + len(checker.neighbours(proj, tool)), ms, md, tools)
    gate(opts, proj, calls, cost)
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


def compare_designs(opts: Opts, config: Path, design: Path, models: str | None, md: list[str]) -> None:
    """Test a catalog plan against the one tool per operation catalog. Writes a proposed plan first when the
    file is not there yet."""
    from misroute import compare as comparer
    from misroute.config import load as load_config
    from misroute.server import merge, openapi

    try:
        cfg = load_config(config)
        proposed = not design.exists()
        if proposed:
            source = asyncio.run(Project(cfg, root=config.parent / cfg.workdir).source_tools())
            doc = openapi.load(cfg.server.openapi) if cfg.server.openapi else None
            plan = merge.propose(source, doc)
            if not plan["tools"]:
                fail(opts, "found nothing to merge: no repeated listings, query variants or bare lists", INSUFFICIENT)
            design.write_text(yaml.safe_dump(plan, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
        cfg = cfg.model_copy(update={"server": cfg.server.model_copy(update={"catalog": str(design)})})
        proj = Project(cfg, root=config.parent / cfg.workdir)
        source, merged = asyncio.run(proj.source_tools()), asyncio.run(proj.tools())
        mirror = merge.apply(source, {"exclude": (proj.plan() or {}).get("exclude") or {}})
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as e:
        fail(opts, f"config problem: {e}")
        return
    if opts.max_cost is not None:
        proj.cap(opts.max_cost)
    ms = split(models) or proj.cfg.tested()
    calls, cost = comparer.design_cost(proj, source, mirror, merged, ms, md)
    note = f"wrote a proposed plan to {design}, tested as proposed. Review it before adopting it" if proposed else ""
    gate(opts, proj, calls, cost, note)
    res = run(opts, proj, comparer.designs(proj, ms, md), calls)
    res["proposed"] = proposed

    def text() -> None:
        cov = res["coverage"]
        inside = sum(len(v) for v in cov["merged"].values())
        out.print(
            f"{cov['operations']} operations: {res['tools']['mirror']} tools one per operation, "
            f"{res['tools']['merged']} with the plan. {len(cov['merged'])} merged tools reach {inside} operations, "
            f"{len(cov['excluded'])} operations excluded"
        )
        for c in res["cells"]:
            was, now = c["rates"]["mirror"], c["rates"]["merged"]
            out.print(
                f"{c['model']} {c['mode']}: {c['verdict']}, {c['reason']}. Right {was['accuracy']:.0%} one per "
                f"operation, {now['accuracy']:.0%} merged, change in error rate {c['diff']:+.1%} "
                f"(95% {c['ci'][0]:+.1%} to {c['ci'][1]:+.1%}) over {c['templates']} request wordings"
            )
            out.print(
                f"  wrong tool {was['wrong_rate']:.0%} to {now['wrong_rate']:.0%}, right tool with the wrong "
                f"selector value {now['wrong_operation_rate']:.0%}",
                style="dim",
            )
        for t in res["merged_tools"]:
            out.print(
                f"  {t['tool']}: {t['operations']} operations, errors {t['errors_mirror']} apart to "
                f"{t['errors_merged']} merged of {t['trials']}, {t['wrong_operation']} from the selector"
            )
        if res["proposed"]:
            out.print(f"the plan in {design} was proposed from routes and parameters, review it", style="yellow")
        out.print(f"to adopt the plan set server.catalog: {design} in misroute.yaml", style="dim")
        out.print(f"spent ${res['cost_usd']:.2f}", style="dim")
        usage_note(opts, res)

    emit(opts, res, warned(res, text), check_code(res["verdict"]))


@app.command()
def compare(
    old: Annotated[
        str | None, typer.Option("--from", help="model in use now, provider:model or a configured name")
    ] = None,
    new: Annotated[str | None, typer.Option("--to", help="model you are moving to")] = None,
    design: Annotated[
        Path | None,
        typer.Option(
            help="catalog plan to test against one tool per operation instead of comparing models. A proposed "
            "plan is written there when the file does not exist"
        ),
    ] = None,
    models: Annotated[str | None, typer.Option(help="with --design, models to test, comma separated")] = None,
    config: Cfg = Path("misroute.yaml"),
    modes: Annotated[str, typer.Option(help="comma separated")] = "json",
    json_: Json = False,
    yes: Yes = False,
    max_cost: MaxCost = None,
    estimate_only: EstimateOnly = False,
    progress: ProgressMode = "bar",
    pilot: Pilot = False,
) -> None:
    """Test two conditions on the same requests: two models with --from and --to, or a merged catalog against
    one tool per operation with --design"""
    from misroute import compare as comparer
    from misroute.config import load as load_config

    opts = Opts("compare", json_, yes, max_cost, estimate_only, progress)
    system_certs(config)
    if design:
        compare_designs(opts, config, design, models, split(modes) or ["json"])
        return
    if not old or not new:
        fail(opts, "give --from and --to to compare two models, or --design to test a catalog plan")
        return
    try:
        cfg, (a, b) = comparer.with_models(load_config(config), [old, new])
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as e:
        fail(opts, f"config problem: {e}")
        return
    if a == b:
        fail(opts, "--from and --to name the same model")
    proj = Project(cfg, root=config.parent / cfg.workdir)
    if max_cost is not None:
        proj.cap(max_cost)
    md = split(modes) or ["json"]
    piloted(opts, proj, pilot, [a, b], md)
    calls, cost = asyncio.run(proj.discover_cost([a, b], md))
    gate(opts, proj, calls, cost)
    res = run(opts, proj, comparer.compare(proj, a, b, md), calls)

    def text() -> None:
        for r in res["modes"]:
            was, now = r["rates"][a], r["rates"][b]
            out.print(
                f"{r['mode']}: {r['verdict']}, {r['reason']}. First choice right {was['accuracy']:.0%} with {a}, "
                f"{now['accuracy']:.0%} with {b}, change in error rate {r['diff']:+.1%} "
                f"(95% {r['ci'][0]:+.1%} to {r['ci'][1]:+.1%}) over {r['templates']} request wordings"
            )
            out.print(
                f"  target reached {was['reached_rate']:.0%} to {now['reached_rate']:.0%}, arguments off schema "
                f"{was['bad_args_rate']:.0%} to {now['bad_args_rate']:.0%}",
                style="dim",
            )
            for t in r["tools"][:8]:
                out.print(f"  {t['tool']}: {t['errors_from']} to {t['errors_to']} errors of {t['trials']}")
            for label, key in (("newly confused", "new"), ("no longer confused", "gone")):
                if r["pairs"][key]:
                    out.print(f"  {label}: {', '.join(p.replace('|', ' and ') for p in r['pairs'][key])}")
        if "real" not in res["by_kind"]:
            out.print("all requests were written by a model. Add real_requests to compare on your own traffic")
        out.print(f"spent ${res['cost_usd']:.2f}", style="dim")
        usage_note(opts, res)

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
    strict_roles: Annotated[
        bool, typer.Option("--strict-roles", help="refuse to run when a role shares a family with a tested model")
    ] = False,
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
    # improve's candidates are written and judged by the roles, so same-family
    # roles let a model pass its own descriptions
    if strict_roles and cfg.role_warnings():
        fail(opts, "; ".join(cfg.role_warnings()))
        return
    proj = Project(cfg, root=config.parent / cfg.workdir)
    if max_cost is not None:
        proj.cap(max_cost)
    md = split(modes) or ["json"]
    calls, cost = asyncio.run(improver.estimate(proj, ms, md))
    gate(opts, proj, calls, cost, improver.STAGES_NOTE)
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
        usage_note(opts, res)

    emit(opts, res, warned(res, text))


@app.command()
def report(
    config: Cfg = Path("misroute.yaml"),
    output: Annotated[
        Path | None, typer.Option(help="where to write, default report.html, or site with --site")
    ] = None,
    pair: Annotated[
        str | None, typer.Option("--pair", help="tool_a,tool_b: show the misrouted requests between the two instead")
    ] = None,
    limit: Annotated[int, typer.Option(help="with --pair, examples per direction")] = 5,
    site: Annotated[
        list[str] | None,
        typer.Option("--site", help="name=workdir, repeatable: build a static site from several results instead"),
    ] = None,
    title: Annotated[str, typer.Option(help="with --site, the page title")] = "misroute results",
    json_: Json = False,
) -> None:
    """Read results, free: an HTML report, one pair's misrouted requests, or a static site of several runs"""
    opts = Opts("report", json_)
    if site:
        from misroute.site.build import build

        if bad := [r for r in site if "=" not in r]:
            fail(opts, f"--site takes name=workdir, got {', '.join(bad)}")
        entries = [(r.split("=", 1)[0], Path(r.split("=", 1)[1])) for r in site]
        made = build(entries, output or Path("site"), title)
        emit(opts, {"site": str(made)}, lambda: out.print(f"wrote {made}"))
        return
    proj = load(opts, config)
    if pair:
        pair_report(opts, proj, pair, limit)
        return
    from misroute.report.html import render

    made = render(proj.store, output or Path("report.html"), proj.cfg.guard.log)
    emit(opts, {"report": str(made)}, lambda: out.print(f"wrote {made}"))


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
    baseline: Annotated[Path | None, typer.Option(help="snapshot JSON from the base branch")] = None,
    snapshot_: Annotated[
        bool,
        typer.Option("--snapshot", help="print tool names, hashes and descriptions as JSON for --baseline, free"),
    ] = False,
    markdown: Annotated[Path | None, typer.Option(help="write a markdown summary for a PR comment")] = None,
    junit: Annotated[Path | None, typer.Option(help="write results as JUnit XML")] = None,
    sarif: Annotated[Path | None, typer.Option(help="write problems as SARIF for code scanning")] = None,
    json_: Json = False,
    max_cost: MaxCost = None,
) -> None:
    """Exit non zero when limits are exceeded, or when a changed description got worse"""
    if snapshot_:
        snapshot(config)
        return
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
    gate(opts, proj, calls, cost)
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
def serve(
    what: Annotated[
        str,
        typer.Argument(
            help="mcp: misroute as an MCP server for agents. ui: the local web interface. "
            "guard: an MCP proxy that logs and guards real calls to your server"
        ),
    ],
    config: Cfg = Path("misroute.yaml"),
    port: Annotated[int, typer.Option(help="with ui")] = 8765,
    approve: Annotated[str | None, typer.Option(help="with guard, approve a held call by its id and exit")] = None,
) -> None:
    """Run misroute as a server: for agents over MCP, as a web interface, or as a guard in front of your server"""
    opts = Opts("serve")
    if what == "ui":
        from misroute.ui.app import serve as serve_ui

        out.print(f"http://127.0.0.1:{port}")
        serve_ui(config, port)
    elif what == "mcp":
        from misroute.agent_server import create

        create(Project.from_yaml(config)).run()
    elif what == "guard" and approve:
        from misroute.config import load as load_config
        from misroute.guard.wrapper import approve as approve_call

        approve_call(load_config(config).guard.log, approve)
        out.print(f"approved {approve}")
    elif what == "guard":
        from misroute.guard.proxy import serve as serve_guard

        proj = Project.from_yaml(config)
        asyncio.run(serve_guard(proj.cfg, proj.store.read("discover")))
    else:
        fail(opts, f"serve takes mcp, ui or guard, not {what}")


MOVED = {
    "init": "quick --config-only",
    "explain": "report --pair tool_a,tool_b",
    "schema": "guide --schema NAME",
    "probe": "discover --probe",
    "fix": "check --pair tool_a,tool_b",
    "ui": "serve ui",
    "site": "report --site name=workdir",
    "guard": "serve guard",
}
PASS_ON = {"allow_extra_args": True, "ignore_unknown_options": True}


def moved(old: str, new: str) -> None:
    @app.command(old, hidden=True, context_settings=PASS_ON)
    def gone(ctx: typer.Context) -> None:
        fail(Opts(old, "--json" in ctx.args), f"misroute {old} is now misroute {new}")


for _old, _new in MOVED.items():
    moved(_old, _new)


@app.command("serve-mcp", hidden=True)
def serve_mcp(config: Cfg = Path("misroute.yaml")) -> None:
    """Old name of serve mcp, kept working because MCP client configs call it"""
    from misroute.agent_server import create

    err.print("misroute serve-mcp is now misroute serve mcp", style="dim")
    create(Project.from_yaml(config)).run()


@app.command("catalog", hidden=True)
def catalog(config: Cfg = Path("misroute.yaml")) -> None:
    """Old name of ci --snapshot, kept working because CI workflows call it"""
    err.print("misroute catalog is now misroute ci --snapshot", style="dim")
    snapshot(config)


def real_file(proj: Project, path: Path | None) -> Path:
    return path or Path(proj.cfg.real_requests or "real.jsonl")


@requests_app.command("collect")
def requests_collect(
    source: Annotated[
        Path | None,
        typer.Option("--from", help="guard log or a JSON lines trace with text and calls, default the guard log"),
    ] = None,
    file: Annotated[Path | None, typer.Option(help="file to add candidates to, default real_requests")] = None,
    config: Cfg = Path("misroute.yaml"),
    json_: Json = False,
) -> None:
    """Add user requests from a guard log or a trace as candidates for review"""
    from misroute.requests import real

    opts = Opts("requests collect", json_)
    proj = load(opts, config)
    src, dest = source or Path(proj.cfg.guard.log), real_file(proj, file)
    if not src.exists():
        fail(opts, f"{src} not found, pass --from with a guard log or a JSON lines trace")
    rows = real.read(dest)
    tools = asyncio.run(proj.tools())
    found = real.collect(src, tools, rows)
    real.write(dest, rows + found)
    res: dict[str, Any] = {"file": str(dest), "added": len(found), **real.status(rows + found, tools)}

    def text() -> None:
        out.print(f"added {len(found)} candidates to {dest}, {res['pending']} waiting for review")
        out.print("run misroute requests review to confirm the right tool for each", style="dim")
        if not proj.cfg.real_requests:
            out.print(f"then set real_requests: {dest} in misroute.yaml", style="dim")

    emit(opts, res, text)


@requests_app.command("review")
def requests_review(
    file: Annotated[Path | None, typer.Option(help="file to review, default real_requests")] = None,
    config: Cfg = Path("misroute.yaml"),
) -> None:
    """Confirm the right tool for each candidate request, one at a time"""
    from misroute.requests import real

    opts = Opts("requests review")
    proj = load(opts, config)
    dest = real_file(proj, file)
    rows = real.read(dest)
    names = {t.name for t in asyncio.run(proj.tools())}
    pending = [r for r in rows if not r.get("reviewed", True)]
    if not sys.stdin.isatty():
        fail(opts, "review needs a terminal, a person confirms each request")
    out.print(f"{len(pending)} to review. Enter keeps the suggested tool, a tool name replaces it, s skips, d drops")
    dropped: list[dict] = []
    for i, r in enumerate(pending, 1):
        out.print(f"\n[{i}/{len(pending)}] {r['text']}")
        out.print(f"  called: {', '.join(r.get('called') or []) or 'nothing'}", style="dim")
        while True:
            answer = typer.prompt("  right tool", default=r["tool"]).strip()
            if answer in ("s", "d", "q") or answer in names:
                break
            out.print(f"  {answer} is not a tool on this server", style="yellow")
        if answer == "q":
            break
        if answer == "d":
            dropped.append(r)
        elif answer != "s":
            r["tool"], r["reviewed"] = answer, True
    kept = [r for r in rows if not any(r is d for d in dropped)]
    real.write(dest, kept)
    left = sum(not r.get("reviewed", True) for r in kept)
    out.print(f"{sum(bool(r.get('reviewed', True)) for r in kept)} reviewed, {left} still waiting, saved to {dest}")


@requests_app.command("status")
def requests_status(
    file: Annotated[Path | None, typer.Option(help="file to count, default real_requests")] = None,
    config: Cfg = Path("misroute.yaml"),
    json_: Json = False,
) -> None:
    """Count reviewed and waiting real requests per tool"""
    from misroute.requests import real

    opts = Opts("requests status", json_)
    proj = load(opts, config)
    dest = real_file(proj, file)
    res: dict[str, Any] = {"file": str(dest), **real.status(real.read(dest), asyncio.run(proj.tools()))}

    def text() -> None:
        out.print(f"{dest}: {res['reviewed']} reviewed, {res['pending']} waiting for review")
        for name, n in res["per_tool"].items():
            out.print(f"  {name}  {n}")
        if res["tools_without"]:
            out.print(f"no real requests yet for: {', '.join(res['tools_without'])}", style="yellow")

    emit(opts, res, text)
