import hashlib
from typing import Any
from misroute.detectors import confusion, hedge
from misroute.fix import clause, stats
from misroute.lint import descriptions
from misroute.server.catalog import Tool, replace_descriptions
from misroute.trial import Request, Trial

MAX_VERDICTS = 3


def split(req: Request, salt: int) -> str:
    h = int(hashlib.sha256(f"{req.id}|{salt}".encode()).hexdigest()[:8], 16)
    return "held_out" if h % 2 else "tuning"


def neighbours(proj, name: str, limit: int = 3) -> list[str]:
    found = proj.store.read("discover") or {}
    out = [s["b"] if s["a"] == name else s["a"] for s in found.get("spots", []) if name in (s["a"], s["b"])]
    out += list((found.get("removal") or {}).get(name, {}))
    return [n for n in dict.fromkeys(out) if n != name and not n.startswith("(")][:limit]


def rates(trials: list[Trial], focus: set[str]) -> dict[str, Any]:
    n = len(trials) or 1
    leak = confusion.Counter(t.chosen for t in trials if t.chosen and t.chosen not in focus)
    return {
        "trials": len(trials),
        "errors": sum(t.error for t in trials),
        "wrong_rate": sum(t.wrong for t in trials) / n,
        "hedge_rate": hedge.hedge_rate(trials),
        "leakage": dict(leak),
    }


def decide(base: dict, prop: dict) -> dict:
    """Verdict on the request template level, with trial level counts and the effect size alongside."""
    test = stats.template_test(base, prop)
    n, p = test["discordant_templates"], test["p"]
    if n == 0 and test["templates"] >= stats.MIN_DISCORDANT_TEMPLATES:
        verdict, reason = "no_difference", f"no change on any of {test['templates']} templates"
    elif n < stats.MIN_DISCORDANT_TEMPLATES:
        verdict, reason = "insufficient_data", f"{n} request templates differ, need {stats.MIN_DISCORDANT_TEMPLATES}"
    elif p >= 0.05:
        verdict, reason = "no_difference", f"p {p:.3f} over {n} differing templates"
    else:
        verdict = "better" if test["diff"] < 0 else "worse"
        reason = f"p {p:.3f} over {n} differing templates"
    return {"verdict": verdict, "reason": reason, **test}


async def check(
    proj,
    tool_name: str,
    proposed: str,
    current: str | None = None,
    refresh: bool = False,
    models_: list[str] | None = None,
    modes: list[str] | None = None,
) -> dict[str, Any]:
    """Compare a proposed description with the current one and a baseline. The verdict uses held out requests."""
    models_ = models_ or proj.cfg.tested()
    modes = modes or ["json"]
    tools = await proj.tools()
    by = {t.name: t for t in tools}
    if tool_name not in by:
        raise KeyError(f"unknown tool {tool_name}")
    if current is not None:
        tools = replace_descriptions(tools, {tool_name: current})
        by = {t.name: t for t in tools}
    target = by[tool_name]
    state = proj.store.read("checks") or {}
    entry = state.get(tool_name, {"round": 0, "verdicts": 0})
    if refresh:
        entry = {"round": entry["round"] + 1, "verdicts": 0}
    spent = entry["verdicts"] >= MAX_VERDICTS
    near = neighbours(proj, tool_name)
    focus = {tool_name, *near}
    reqs: list[Request] = []
    for t in [target, *[by[n] for n in near if n in by]]:
        reqs += await proj.requests_for(t, tools, seed=entry["round"])
    if near:
        reqs += await proj.requests_for(target, tools, by[near[0]], seed=entry["round"])
    halves = {"tuning": [r for r in reqs if split(r, entry["round"]) == "tuning"]}
    halves["held_out"] = [r for r in reqs if split(r, entry["round"]) == "held_out"]
    flagged = descriptions.classify(target)
    writer = proj.role("writer")
    if flagged:
        baseline = await descriptions.draft(writer, target, tools)
        kind = "description"
    else:
        baseline = await clause.neutral_one(writer, target, len(proposed))
        kind = "neutral"
    conds = {
        "current": tools,
        "baseline": replace_descriptions(tools, {tool_name: baseline}),
        "proposed": replace_descriptions(tools, {tool_name: proposed}),
    }
    out: dict[str, Any] = {"tool": tool_name, "neighbours": near, "baseline_kind": kind, "baseline": baseline}
    start = proj.budget.spent
    use = ["tuning"] if spent else ["tuning", "held_out"]
    for name in use:
        rs = halves[name]
        res: dict[str, Any] = {}
        errs: dict[str, dict] = {}
        for cond, ts in conds.items():
            orders = [ts, ts[::-1]]
            trials = await proj.run_trials(
                proj.pair_trials(rs, models_, modes, proj.cfg.slots, phase="check", condition=cond),
                lambda t, orders=orders: orders[t.order],
            )
            res[cond] = rates(trials, focus)
            errs[cond] = {t.key: t.error for t in trials}
        out[name] = res
        out[f"{name}_errors"] = errs
    if spent:
        result: dict = {
            "verdict": "insufficient_data",
            "reason": f"held out set used for {MAX_VERDICTS} verdicts, rerun with refresh",
            "fixed": 0,
            "broke": 0,
            "p": 1.0,
        }
    else:
        result = decide(out["held_out_errors"]["baseline"], out["held_out_errors"]["proposed"])
        vs = decide(out["held_out_errors"]["current"], out["held_out_errors"]["proposed"])
        out["vs_current"] = {k: vs[k] for k in ("verdict", "fixed", "broke", "p", "diff", "ci")}
        entry["verdicts"] += 1
    state[tool_name] = entry
    proj.store.write("checks", state)
    for name in use:
        out.pop(f"{name}_errors")
    held = out.get("held_out", {})
    out.update(
        **result,
        held_out=not spent,
        trials=held.get("proposed", {}).get("trials", 0),
        wrong_rate={c: v["wrong_rate"] for c, v in held.items()},
        hedge_rate={c: v["hedge_rate"] for c, v in held.items()},
        leakage=sorted({k for v in held.values() for k in v["leakage"]}),
        verdicts_left=max(MAX_VERDICTS - entry["verdicts"], 0),
        roles_warning=proj.cfg.role_warnings(),
        round=entry["round"],
        cost_usd=round(proj.budget.spent - start, 4),
    )
    return out


def estimate(proj, n_tools: int, models_: list[str], modes: list[str], tools: list[Tool]) -> tuple[int, float]:
    per_model = n_tools * proj.cfg.requests_per_tool * proj.cfg.slots * 2 * 3 * len(modes)
    return per_model * len(models_), sum(proj.estimate(per_model, m, tools) for m in models_)
