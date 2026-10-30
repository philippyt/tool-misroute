import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Any
from misroute.check import decide, split
from misroute.config import Config
from misroute.fix import clause, stats
from misroute.improve import audit, candidates, export
from misroute.lint import descriptions
from misroute.models.base import BudgetExceeded
from misroute.server import openapi
from misroute.server.catalog import Tool, replace_descriptions
from misroute.trial import Request, Trial

PAIRS = (("winner", "baseline"), ("winner", "original"), ("baseline", "original"))
STAGES_NOTE = (
    "This covers discover. Candidates, race and confirmation depend on the groups discover finds, so they are "
    "priced after discover and the run stops before racing if they would pass the budget"
)


SOURCES = {"interface": "spec", "descriptions": "description", "spec": "spec", "description": "description"}
PATHLIKE = (".txt", ".md", ".yaml", ".yml", ".json")


def pick_styles(names: list[str] | None) -> list[str]:
    if not names:
        return list(candidates.STYLES)
    unknown = [n for n in names if n not in candidates.STYLES]
    if unknown:
        raise ValueError(f"unknown style {', '.join(unknown)}, styles are {', '.join(candidates.STYLES)}")
    return list(dict.fromkeys(names))


def configure(
    cfg: Config,
    models: list[str] | None = None,
    helper: str | None = None,
    requests_from: str | None = None,
    languages: list[str] | None = None,
    domain: str | None = None,
    real_requests: str | None = None,
) -> tuple[Config, list[str]]:
    """Config for an improve run and the models under test. models and helper take provider:model shorthand or a
    configured model name. The helper writes requests, validates them and writes candidates, so the tested model
    never grades itself. Requests come from the interface unless requests_from or the config says descriptions."""
    from misroute.models.presets import expand

    data = cfg.model_dump()
    names = {m["name"] for m in data["models"]}

    def add(spec: str) -> str:
        if spec in names:
            return spec
        block = expand(spec)
        if block["name"] not in names:
            data["models"].append(block)
            names.add(block["name"])
        return block["name"]

    tested = [add(m) for m in models or []]
    if helper:
        name = add(helper)
        data.update(generator=name, validator=name, writer=name)
    if requests_from:
        if requests_from not in SOURCES:
            raise ValueError("requests_from must be interface or descriptions")
        data["request_source"] = SOURCES[requests_from]
    elif "request_source" not in cfg.model_fields_set:
        data["request_source"] = "spec"
    if languages:
        data["request_languages"] = languages
    if domain:
        text = domain.strip()
        pathlike = text.lower().endswith(PATHLIKE) or (" " not in text and ("/" in text or "\\" in text))
        if "\n" not in text and pathlike and not Path(text).is_file():
            raise ValueError(f"domain file {text} not found, pass the glossary text itself or an existing file")
        data["domain"] = text
    if real_requests:
        if not Path(real_requests).is_file():
            raise ValueError(f"real requests file {real_requests} not found")
        data["real_requests"] = real_requests
    new = Config.model_validate(data)
    return new, tested or new.tested()


def groups(spots: list[dict], limit: int | None = None) -> list[list[str]]:
    """Confused groups: tools connected by pairs that meet the flag rule, most errors first. limit keeps the
    first groups only."""
    edges = [s for s in spots if s.get("flagged")]
    parent: dict[str, str] = {}

    def root(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s in edges:
        parent[root(s["a"])] = root(s["b"])
    weight: dict[str, int] = defaultdict(int)
    members: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in edges:
        r = root(s["a"])
        weight[r] += s["errors"]
        members[r][s["a"]] += s["errors"]
        members[r][s["b"]] += s["errors"]
    ranked = sorted(weight, key=lambda r: (-weight[r], r))
    found = [sorted(members[r], key=lambda n: (-members[r][n], n)) for r in ranked]
    return found[:limit] if limit else found


def partners(spots: list[dict], group: list[str]) -> list[tuple[str, str]]:
    """Every flagged pair inside the group, in both directions, for borrowed requests."""
    out = []
    for s in spots:
        if s.get("flagged") and s["a"] in group and s["b"] in group:
            out += [(s["a"], s["b"]), (s["b"], s["a"])]
    return out


class Improver:
    def __init__(
        self, proj, models_: list[str], modes: list[str], max_chars: int | None = None, styles: list[str] | None = None
    ):
        self.proj, self.models, self.modes = proj, models_, modes
        self.max_chars = max_chars
        self.styles = styles or list(candidates.STYLES)

    async def trials(self, tools: list[Tool], desc: dict[str, str], reqs: list[Request], label: str) -> list[Trial]:
        ts = replace_descriptions(tools, desc)
        orders = [ts, ts[::-1]]
        todo = self.proj.pair_trials(
            reqs, self.models, self.modes, self.proj.cfg.slots, phase="improve", condition=label
        )
        return await self.proj.run_trials(todo, lambda t: orders[t.order])

    async def requests(self, tools: list[Tool], group: list[str], spots: list[dict]) -> list[Request]:
        by = {t.name: t for t in tools}
        base = await self.proj.request_set(tools, [by[n] for n in group])
        borrowed = await asyncio.gather(
            *(self.proj.requests_for(by[a], tools, by[b]) for a, b in partners(spots, group))
        )
        out = [r for n in group for r in base.get(n, [])] + [r for rs in borrowed for r in rs]
        return list({r.id: r for r in out}.values())

    async def race(
        self, tools: list[Tool], group: list[str], cands: dict[str, list[dict]], reqs: list[Request]
    ) -> dict[str, Any]:
        """Coordinate pass on tuning requests: each tool's candidates run in full with the other tools fixed at the
        current best. Fewer errors win and the shorter description breaks ties."""
        by = {t.name: t for t in tools}
        best = {n: by[n].description for n in group}
        memo: dict[tuple, int] = {}

        async def score(desc: dict[str, str]) -> int:
            k = tuple(sorted(desc.items()))
            if k not in memo:
                memo[k] = sum(t.error for t in await self.trials(tools, desc, reqs, "race"))
            return memo[k]

        best_score = start = await score(best)
        tried = []
        for name in group:
            options = [c["text"] for c in cands.get(name, [])]
            scores = await asyncio.gather(*(score({**best, name: o}) for o in options))
            for o, sc in zip(options, scores, strict=True):
                tried.append({"tool": name, "text": o, "errors": sc})
                if (sc, len(o)) < (best_score, len(best[name])):
                    best, best_score = {**best, name: o}, sc
        return {"best": best, "errors": best_score, "start_errors": start, "tried": tried}

    async def baseline(
        self, tools: list[Tool], group: list[str], winner: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Lint's draft for tools with a missing or path only description, else a neutral rewrite of the winner's
        length, so a gain cannot come from length or any rewrite at all. When the writer gives no usable rewrite
        the draft stands in. Returns the texts and which kind each one is."""
        by = {t.name: t for t in tools}
        writer = self.proj.role("writer")

        async def one(n: str) -> tuple[str, str]:
            if descriptions.classify(by[n]):
                return await descriptions.draft(writer, by[n], tools), "draft"
            try:
                return await clause.neutral_one(writer, by[n], len(winner[n])), "neutral"
            except ValueError:
                return await descriptions.draft(writer, by[n], tools), "draft, the neutral rewrite failed"

        found = await asyncio.gather(*(one(n) for n in group))
        return {n: f[0] for n, f in zip(group, found, strict=True)}, {
            n: f[1] for n, f in zip(group, found, strict=True)
        }

    async def confirm(self, tools: list[Tool], conds: dict[str, dict[str, str]], reqs: list[Request]) -> dict:
        errs, rates, seen = {}, {}, {}
        for cond, desc in conds.items():
            ts = await self.trials(tools, desc, reqs, cond)
            errs[cond] = {t.key: t.error for t in ts}
            seen[cond] = {t.key: t for t in ts}
            n = len(ts) or 1
            rates[cond] = {
                "trials": len(ts),
                "error_rate": sum(t.error for t in ts) / n,
                "wrong_rate": sum(t.wrong for t in ts) / n,
            }
        real = {r.id for r in reqs if r.kind == "real"}
        if real:
            for cond in conds:
                hit = [v for k, v in errs[cond].items() if k[2] in real]
                rates[cond]["real_error_rate"] = sum(hit) / len(hit) if hit else None
                rates[cond]["real_trials"] = len(hit)
        lang = {r.id: r.lang for r in reqs}
        tests = {f"{a}_vs_{b}": decide(errs[b], errs[a]) for a, b in PAIRS}
        per_lang = {}
        if len(set(lang.values())) > 1:
            for code in sorted(set(lang.values())):
                keep = {r for r, x in lang.items() if x == code}
                sub = {c: {k: v for k, v in e.items() if k[2] in keep} for c, e in errs.items()}
                per_lang[code] = {f"{a}_vs_{b}": decide(sub[b], sub[a]) for a, b in PAIRS}
        return {
            "rates": rates,
            "tests": tests,
            "by_language": per_lang,
            "templates": len(lang),
            "examples": examples(seen),
        }


def examples(seen: dict[str, dict[tuple, Trial]]) -> list[dict]:
    """Held out requests where any condition picked wrong, with each condition's picks. Fixed ones first, then
    broken ones, then those every condition got wrong."""
    keys = set.intersection(*(set(v) for v in seen.values())) if seen else set()
    out: list[dict[str, Any]] = []
    for k in sorted(keys):
        row = {c: v[k] for c, v in seen.items()}
        if not any(t.error for t in row.values()):
            continue
        first = next(iter(row.values()))
        orig, win = row.get("original"), row.get("winner")
        if orig and win and orig.error and not win.error:
            outcome = "fixed"
        elif orig and win and win.error and not orig.error:
            outcome = "broke"
        elif all(t.error for t in row.values()):
            outcome = "all wrong"
        else:
            outcome = "mixed"
        out.append(
            {
                "request": first.text,
                "target": first.target,
                "lang": first.lang,
                "kind": first.kind,
                "outcome": outcome,
                "picked": {c: t.picks or [] for c, t in row.items()},
            }
        )
    rank = {"fixed": 0, "broke": 1, "all wrong": 2, "mixed": 3}
    seen_text: set[str] = set()
    unique = []
    for e in sorted(out, key=lambda e: rank[e["outcome"]]):
        if e["request"] not in seen_text:
            seen_text.add(e["request"])
            unique.append(e)
    return unique


def thin(proj, disc: dict, results: list[dict], n_tools: int) -> list[str]:
    """Plain reasons a run gives little evidence, so a short result is not mistaken for a clean one."""
    out = []
    unread = sum(proj.unreadable.values())
    if unread:
        out.append(
            f"{unread} helper replies could not be read, often cut off or not JSON. Try a stronger helper or a "
            "max_tokens set"
        )
    if disc.get("untested"):
        out.append(f"{len(disc['untested'])} of {n_tools} tools got no usable test requests and were not tested")
    if not results:
        out.append("no group of confused tools was found, so nothing was raced")
    for g in results:
        n = g["confirm"]["templates"]
        if n < stats.MIN_DISCORDANT_TEMPLATES:
            out.append(
                f"group {g['group'] + 1} has {n} held out request templates, a sign flip test needs "
                f"{stats.MIN_DISCORDANT_TEMPLATES} to reach p below {stats.ALPHA}"
            )
    return out


def beats(test: dict) -> bool:
    return test["verdict"] == "better" and test["p_holm"] < stats.ALPHA


def adjust(found: list[dict]) -> None:
    """Holm across groups for each comparison, then the export rule: the winner when it beats the baseline, else
    the baseline when it beats the original, else keep the original."""
    for name in ("winner_vs_baseline", "baseline_vs_original", "winner_vs_original"):
        ps = [g["confirm"]["tests"][name]["p"] for g in found]
        for g, p in zip(found, stats.holm(ps), strict=True):
            g["confirm"]["tests"][name]["p_holm"] = p
    for g in found:
        t = g["confirm"]["tests"]

        changed = g["winner"] != g["original"]
        if changed and beats(t["winner_vs_baseline"]):
            g["export"], g["why"] = "winner", "beats the baseline on held out requests"
        elif beats(t["baseline_vs_original"]):
            g["export"], g["why"] = "baseline", "the baseline beats the original, the winner does not beat the baseline"
        else:
            x = t["winner_vs_baseline"]
            g["export"] = "original"
            g["why"] = x["reason"] if x["verdict"] == "insufficient_data" else "no tested change beats the original"


async def estimate(proj, models_: list[str], modes: list[str]) -> tuple[int, float]:
    """Cost of discover, the first stage. The later stages depend on which groups discover finds, so improve
    computes their cost once it has them and stops before racing when that would pass the budget."""
    return await proj.discover_cost(models_, modes)


def stage_cost(proj, found: list[list[str]], spots: list[dict], models_: list[str], modes: list[str], n_styles: int):
    """Model calls and dollars for candidates, race and confirmation over the groups discover found."""
    langs = len(proj.cfg.request_languages or ["en"])
    per_set = proj.cfg.requests_per_tool * langs
    calls, cost = 0, 0.0
    helper = proj.cfg.role("writer").name
    for group in found:
        sets = len(group) + len(partners(spots, group))
        half = sets * per_set / 2
        runs = 1 + len(group) * n_styles + 3
        trials = round(half * proj.cfg.slots * 2 * runs) * len(modes)
        side = len(group) * (2 + n_styles) + len(partners(spots, group)) * (1 + per_set)
        calls += trials * len(models_) + side
        cost += sum(proj.estimate(trials, m, []) for m in models_) + proj.estimate(side, helper, [])
    return calls, cost


async def improve(
    proj,
    models_: list[str] | None = None,
    modes: list[str] | None = None,
    n_groups: int | None = None,
    styles: list[str] | None = None,
    max_chars: int | None = None,
    out: Path | None = None,
    fastmcp: bool = False,
    drafts: bool = True,
) -> dict[str, Any]:
    """Find confused groups, write candidate descriptions, race them on tuning requests, confirm the winner against
    the original and a baseline on held out requests, and write the result for review. Nothing is applied."""
    models_ = models_ or proj.cfg.tested()
    modes = modes or ["json"]
    start = proj.budget.spent
    tools = await proj.tools()
    by = {t.name: t for t in tools}
    spec = proj.cfg.server.openapi
    doc = openapi.load(spec) if spec else None
    found = audit.notes(tools, doc)
    disc = await proj.discover(models_, modes)
    spots = disc["spots"]
    imp = Improver(proj, models_, modes, max_chars, styles)
    domain = proj.domain()
    found_groups = groups(spots, n_groups)
    calls, cost = stage_cost(proj, found_groups, spots, models_, modes, len(imp.styles))
    if proj.budget.spent + cost > proj.budget.limit:
        raise BudgetExceeded(
            f"discover found {len(found_groups)} groups, racing them needs about {calls} calls and ${cost:.2f}, "
            f"above the ${proj.budget.limit - proj.budget.spent:.2f} left. Raise the budget or pass max_groups"
        )
    results, held_by, warnings = [], {}, []
    for i, group in enumerate(found_groups):
        reqs = await imp.requests(tools, group, spots)
        tuning = [r for r in reqs if split(r, 0) == "tuning"]
        held = held_by[i] = [r for r in reqs if split(r, 0) == "held_out"]
        written, dropped = {}, {}
        for n in group:
            sibs = [by[x] for x in group if x != n]
            raw = await candidates.write(proj.role("writer"), by[n], sibs, imp.styles, max_chars, domain)
            written[n], dropped[n] = await candidates.screen(proj.role("validator"), by[n], raw)
            if not raw:
                warnings.append(f"the helper wrote no readable candidates for {n}, so it kept its original")
        race = await imp.race(tools, group, written, tuning)
        original = {n: by[n].description for n in group}
        winner = race["best"]
        base, base_kind = await imp.baseline(tools, group, winner)
        conf = await imp.confirm(tools, {"original": original, "baseline": base, "winner": winner}, held)
        style = {n: next((c["style"] for c in written[n] if c["text"] == winner[n]), "original") for n in group}
        results.append(
            {
                "group": i,
                "tools": group,
                "requests": {"tuning": len(tuning), "held_out": len(held)},
                "candidates": written,
                "dropped": dropped,
                "race": {k: v for k, v in race.items() if k != "best"},
                "original": original,
                "baseline": base,
                "baseline_kind": base_kind,
                "winner": winner,
                "style": style,
                "confirm": conf,
            }
        )
    adjust(results)
    warnings = thin(proj, disc, results, len(tools)) + warnings
    entries = await build_entries(proj, tools, results, found, drafts)
    result: dict[str, Any] = {
        "source": str(Path(spec).resolve())
        if spec
        else proj.cfg.server.tools or proj.cfg.server.url or " ".join(proj.cfg.server.command or []),
        "audit": audit.summary(tools, found),
        "groups": results,
        "entries": entries,
        "request_source": proj.cfg.request_source,
        "languages": proj.cfg.request_languages,
        "roles_warning": proj.cfg.role_warnings(),
        "warnings": warnings,
        "discover_run": disc.get("run"),
    }
    if not spec:
        result["note"] = "descriptions.yaml maps tool names to descriptions, apply them in the server's code"
    if out is not None:
        result["files"] = export.write(out, entries, spec, doc, fastmcp)
        if fastmcp and "server" in result["files"]:
            result["fastmcp"] = await served(proj, Path(result["files"]["server"]), entries, results, held_by, imp)
    result["cost_usd"] = round(proj.budget.spent - start, 4)
    proj.store.write("improve", result)
    return result


async def build_entries(
    proj, tools: list[Tool], results: list[dict], found: list[audit.Note], drafts: bool
) -> list[dict]:
    notes: dict[str, list[str]] = defaultdict(list)
    exclude: dict[str, str] = {}
    for n in found:
        if n.kind == "exclude":
            exclude[n.tool] = n.detail
        elif n.kind != "description":
            notes[n.tool].append(n.detail)
    chosen: dict[str, dict] = {}
    for g in results:
        t = g["confirm"]["tests"]
        evidence = {
            k: {f: t[k][f] for f in ("verdict", "p", "p_holm", "templates", "discordant_templates", "diff", "ci")}
            for k in t
        }
        for n in g["tools"]:
            text = g[g["export"]][n] if g["export"] != "original" else g["original"][n]
            status = {"winner": "tested", "baseline": "tested baseline", "original": "kept"}[g["export"]]
            status = "kept" if text == g["original"][n] else status
            chosen[n] = {
                "description": text,
                "status": status,
                "reason": g["why"],
                "group": g["group"],
                "style": g["style"][n] if g["export"] == "winner" else g["export"],
                "evidence": evidence,
            }
    by = {t.name: t for t in tools}
    loose = [t for t in tools if t.name not in chosen and descriptions.classify(t)] if drafts else []
    texts = await asyncio.gather(*(descriptions.draft(proj.role("writer"), t, tools) for t in loose))
    for t, text in zip(loose, texts, strict=True):
        chosen[t.name] = {
            "description": text.strip() or t.description,
            "status": "draft",
            "reason": f"{descriptions.classify(t)} description, not in a confused group, untested",
        }
    out = []
    for t in tools:
        c = chosen.get(t.name, {"description": t.description, "status": "unchanged", "reason": "no confusion found"})
        out.append(
            {
                "tool": t.name,
                "op_id": t.op_id,
                "route": t.route,
                "original": by[t.name].description,
                "read_only": not t.is_write,
                "destructive": bool(t.destructive),
                **c,
                "suggest_exclude": exclude.get(t.name),
                "notes": notes.get(t.name, []),
            }
        )
    return out


def leaves(e: BaseException) -> list[BaseException]:
    """The underlying errors inside nested exception groups."""
    if isinstance(e, BaseExceptionGroup):
        return [x for sub in e.exceptions for x in leaves(sub)]
    return [e]


async def served(
    proj, server_py: Path, entries: list[dict], results: list[dict], held_by: dict[int, list[Request]], imp: Improver
) -> dict[str, Any]:
    """List the generated FastMCP server's tools and rerun the exported descriptions' held out trials against them,
    since its argument shapes and wording are what agents will see."""
    from misroute.config import ServerConfig
    from misroute.project import Project

    cmd = ["uv", "run", "-q", "--no-project", "--with", f"fastmcp=={export.FASTMCP}", "--with", "pyyaml"]
    cmd += ["python", str(server_py)]
    cfg = proj.cfg.model_copy(update={"server": ServerConfig(command=cmd)})
    fm = Project(cfg, root=proj.store.root)
    try:
        served_tools = await fm.tools()
    except Exception as e:
        return {"ok": False, "error": "; ".join(f"{type(x).__name__}: {x}" for x in leaves(e))}
    want = {e["tool"]: e["description"] for e in entries}
    got = {t.name: t for t in served_tools}
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    differ = sorted(n for n in set(want) & set(got) if want[n] != got[n].description)
    hints = {e["tool"]: e["read_only"] for e in entries}
    hints_differ = sorted(n for n in set(hints) & set(got) if hints[n] != (got[n].read_only is True))
    rerun = []
    for g in results:
        held = [r for r in held_by[g["group"]] if r.tool in got]
        ts = await imp.trials(served_tools, {}, held, "fastmcp")
        n = len(ts) or 1
        exported = g["confirm"]["rates"].get(g["export"], {})
        rerun.append(
            {
                "group": g["group"],
                "trials": len(ts),
                "error_rate": sum(t.error for t in ts) / n,
                "misroute_error_rate": exported.get("error_rate"),
            }
        )
    return {
        "ok": not (missing or differ or hints_differ),
        "tools": len(got),
        "missing": missing,
        "extra": extra,
        "descriptions_differ": differ,
        "hints_differ": hints_differ,
        "held_out": rerun,
    }
