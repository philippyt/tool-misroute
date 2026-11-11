from collections import defaultdict
from typing import Any
from misroute.check import decide
from misroute.config import Config
from misroute.detectors import confusion, hedge, rank
from misroute.server import merge
from misroute.trial import Trial

ORDER = ("worse", "insufficient_data", "no_difference", "better")


def with_models(cfg: Config, specs: list[str]) -> tuple[Config, list[str]]:
    """The config with each model added when it is not there yet, and the names to use. A spec is a configured
    name or provider:model shorthand."""
    from misroute.models.presets import expand

    data = cfg.model_dump()
    names = {m["name"] for m in data["models"]}
    out = []
    for spec in specs:
        if spec not in names:
            block = expand(spec)
            if block["name"] not in names:
                data["models"].append(block)
                names.add(block["name"])
            spec = block["name"]
        out.append(spec)
    return Config.model_validate(data), out


def shared(trials: list[Trial], model: str, mode: str) -> dict[tuple, Trial]:
    return {("", *t.key[1:]): t for t in trials if t.model == model and t.mode == mode}


def rates(ts: list[Trial]) -> dict[str, Any]:
    n = len(ts) or 1
    return {
        "trials": len(ts),
        "accuracy": sum(not t.error for t in ts) / n,
        "reached_rate": sum(t.reached for t in ts) / n,
        "wrong_rate": sum(t.wrong for t in ts) / n,
        "hedge_rate": hedge.hedge_rate(ts),
        "bad_args_rate": sum(bool(t.bad_args) for t in ts) / n,
        "wrong_operation_rate": sum(t.wrong_operation for t in ts) / n,
    }


def by_tool(old: dict[tuple, Trial], new: dict[tuple, Trial]) -> list[dict]:
    """Tools whose error count moved between the two models, largest change first."""
    count: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for k in old.keys() & new.keys():
        c = count[old[k].target]
        c[0] += old[k].error
        c[1] += new[k].error
        c[2] += 1
    rows = [{"tool": t, "errors_from": a, "errors_to": b, "trials": n} for t, (a, b, n) in count.items() if a != b]
    return sorted(rows, key=lambda r: (-abs(r["errors_to"] - r["errors_from"]), r["tool"]))


def flagged(trials: list[Trial], writes: set[str]) -> set[str]:
    return {s.pair for s in rank.rank(trials, {}, writes) if s.flagged}


async def compare(proj, old: str, new: str, modes: list[str] | None = None) -> dict[str, Any]:
    """Run both models on the same requests, tools and orders and test whether routing differs, with the request
    template as the unit. better means the model moved to makes fewer errors than the model moved from. With
    more than one mode the p values are multiplied by the number of modes."""
    from misroute.project import orders

    modes = modes or ["json"]
    start = proj.budget.spent
    tools = await proj.tools()
    writes = {t.name for t in tools if t.is_write}
    rots = orders(tools, proj.cfg.rotations)
    reqs = await proj.request_set(tools)
    todo = proj.discover_trials(reqs, [old, new], modes)
    for t in todo:
        t.phase = "compare"
    trials = await proj.run_trials(todo, lambda t: rots[t.order])
    rows = []
    for mode in modes:
        a, b = shared(trials, old, mode), shared(trials, new, mode)
        keys = a.keys() & b.keys()
        test = decide({k: a[k].error for k in keys}, {k: b[k].error for k in keys}, len(modes))
        was = flagged([t for t in trials if t.model == old and t.mode == mode], writes)
        now = flagged([t for t in trials if t.model == new and t.mode == mode], writes)
        rows.append(
            {
                "mode": mode,
                **test,
                "rates": {old: rates([a[k] for k in keys]), new: rates([b[k] for k in keys])},
                "tools": by_tool(a, b),
                "pairs": {"new": sorted(now - was), "gone": sorted(was - now), "both": sorted(was & now)},
            }
        )
    cat = await proj.catalog()
    run = proj.store.run_id("compare", cat)
    proj.store.save(run, "compare", cat, trials, {"from": old, "to": new, "modes": modes})
    result = {
        "from": old,
        "to": new,
        "verdict": min((r["verdict"] for r in rows), key=ORDER.index),
        "modes": rows,
        "templates": sum(len(v) for v in reqs.values()),
        "by_kind": sorted({r.kind for rs in reqs.values() for r in rs}),
        "roles_warning": proj.cfg.role_warnings(),
        "run": run,
        "cost_usd": round(proj.budget.spent - start, 4),
        "usage": proj.usage(),
    }
    proj.store.write("compare", result)
    return result


def design_cost(proj, source: list, mirror: list, merged: list, models_: list[str], modes: list[str]):
    """Model calls and dollars to test a catalog plan: requests written and judged once per operation, then
    every request run against the one tool per operation catalog and against the merged one."""
    langs = len(proj.cfg.request_languages or ["en"])
    requests = len(mirror) * proj.cfg.requests_per_tool * langs
    calls = len(mirror) * langs + requests
    cost = proj.estimate(len(mirror) * langs, proj.cfg.role("generator").name, [], 1)
    cost += proj.estimate(requests, proj.cfg.role("validator").name, source, 1)
    for m in models_:
        for mode in modes:
            n = requests * proj.cfg.rotations * proj.repeats(m, mode)
            calls += 2 * round(n * proj.turns_per_trial(m))
            cost += proj.estimate(n, m, mirror) + proj.estimate(n, m, merged)
    return calls, cost


async def designs(proj, models_: list[str], modes: list[str] | None = None) -> dict[str, Any]:
    """Run the same requests against the one tool per operation catalog and against the catalog the plan
    describes, and test whether routing differs with the request template as the unit. A trial on a merged tool
    is right only when the tool and the selector value are both right, so a merge that moves the confusion into
    its selector gains nothing. better means the merged catalog makes fewer errors. Excluded operations are left
    out of both, since excluding is a separate decision."""
    from misroute.project import orders

    modes = modes or ["json"]
    start = proj.budget.spent
    plan = proj.plan() or {}
    source = await proj.source_tools()
    merged = await proj.tools()
    mirror = merge.apply(source, {"exclude": plan.get("exclude") or {}})

    async def run_on(label: str, tools: list) -> list[Trial]:
        rots = orders(tools, proj.cfg.rotations)
        todo = proj.discover_trials(await proj.request_set(tools), models_, modes)
        for t in todo:
            t.phase, t.condition = "design", label
        return await proj.run_trials(todo, lambda t: rots[t.order])

    before, after = await run_on("mirror", mirror), await run_on("merged", merged)
    cells = []
    for m in models_:
        for mode in modes:
            a, b = shared(before, m, mode), shared(after, m, mode)
            keys = a.keys() & b.keys()
            test = decide({k: a[k].error for k in keys}, {k: b[k].error for k in keys}, len(models_) * len(modes))
            cells.append(
                {
                    "model": m,
                    "mode": mode,
                    **test,
                    "rates": {"mirror": rates([a[k] for k in keys]), "merged": rates([b[k] for k in keys])},
                }
            )
    tools_rows = []
    for t in merged:
        if not t.operations:
            continue
        inside = set(t.operations.values())
        was = [x for x in before if x.target in inside]
        now = [x for x in after if x.target == t.name]
        tools_rows.append(
            {
                "tool": t.name,
                "operations": len(inside),
                "trials": len(now),
                "errors_mirror": sum(x.error for x in was),
                "errors_merged": sum(x.error for x in now),
                "wrong_operation": sum(x.wrong_operation for x in now),
                "by_operation": confusion.operations(now).get(t.name, {}),
            }
        )
    cat = await proj.catalog()
    run = proj.store.run_id("design", cat)
    proj.store.save(run, "design", cat, before + after, {"models": models_, "modes": modes})
    result = {
        "design": proj.cfg.server.catalog,
        "verdict": min((c["verdict"] for c in cells), key=ORDER.index),
        "cells": cells,
        "merged_tools": tools_rows,
        "coverage": merge.coverage(source, plan),
        "tools": {"mirror": len(mirror), "merged": len(merged)},
        "roles_warning": proj.cfg.role_warnings(),
        "run": run,
        "cost_usd": round(proj.budget.spent - start, 4),
        "usage": proj.usage(),
    }
    proj.store.write("design", result)
    return result
