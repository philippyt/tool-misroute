import asyncio
import math
from collections import defaultdict
from pathlib import Path
from typing import Any
from misroute.check import decide, split
from misroute.config import Config
from misroute.fix import clause, stats
from misroute.improve import audit, candidates, export
from misroute.lint import descriptions
from misroute.server import openapi
from misroute.server.catalog import Tool, replace_descriptions
from misroute.trial import Request, Trial

MAX_GROUP = 6
RACE_SLOTS = 2
MIN_SCORE = 0.1
TOP_SHARE = 0.3
PAIRS = (("winner", "baseline"), ("winner", "original"), ("baseline", "original"))


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


def groups(spots: list[dict], limit: int, max_size: int = MAX_GROUP) -> list[list[str]]:
    """Confused groups: tools connected by pairs with a wrong or hedge signal, strongest first. Only pairs scoring at
    least 0.1 and 30% of the top pair count, so weak noise does not chain everything into one group."""
    edges = [s for s in spots if s["signals"].get("wrong") or s["signals"].get("hedge")]
    floor = max(MIN_SCORE, TOP_SHARE * max((s["score"] for s in edges), default=0))
    edges = [s for s in edges if s["score"] >= floor]
    parent: dict[str, str] = {}

    def root(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s in edges:
        parent[root(s["a"])] = root(s["b"])
    weight: dict[str, float] = defaultdict(float)
    members: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in edges:
        r = root(s["a"])
        weight[r] += s["score"]
        members[r][s["a"]] += s["score"]
        members[r][s["b"]] += s["score"]
    ranked = sorted(weight, key=lambda r: -weight[r])[:limit]
    return [sorted(members[r], key=lambda n: (-members[r][n], n))[:max_size] for r in ranked]


def partners(spots: list[dict], group: list[str]) -> dict[str, str]:
    best: dict[str, tuple[float, str]] = {}
    for s in spots:
        if s["a"] in group and s["b"] in group:
            for x, y in ((s["a"], s["b"]), (s["b"], s["a"])):
                if s["score"] > best.get(x, (-1.0, ""))[0]:
                    best[x] = (s["score"], y)
    return {k: v[1] for k, v in best.items()}


class Improver:
    def __init__(
        self, proj, models_: list[str], modes: list[str], max_chars: int = 300, styles: list[str] | None = None
    ):
        self.proj, self.models, self.modes = proj, models_, modes
        self.max_chars = max_chars
        self.styles = styles or list(candidates.STYLES)

    async def trials(
        self, tools: list[Tool], desc: dict[str, str], reqs: list[Request], slots: int, label: str, cheap: bool = False
    ) -> list[Trial]:
        ts = replace_descriptions(tools, desc)
        orders = [ts, ts[::-1]]
        todo = self.proj.pair_trials(reqs, self.models, self.modes, slots, phase="improve", condition=label)
        if cheap:
            todo = [t for t in todo if t.order == 0]
        return await self.proj.run_trials(todo, lambda t: orders[t.order])

    async def requests(self, tools: list[Tool], group: list[str], spots: list[dict]) -> list[Request]:
        by = {t.name: t for t in tools}
        base = await self.proj.request_set(tools, [by[n] for n in group])
        near = partners(spots, group)
        borrowed = await asyncio.gather(
            *(self.proj.requests_for(by[n], tools, by[near[n]]) for n in group if n in near)
        )
        out = [r for n in group for r in base.get(n, [])] + [r for rs in borrowed for r in rs]
        return list({r.id: r for r in out}.values())

    async def race(
        self, tools: list[Tool], group: list[str], cands: dict[str, list[dict]], reqs: list[Request]
    ) -> dict[str, Any]:
        """Coordinate pass on tuning requests: each tool's candidates with the others fixed at the current best.
        A cheap round with one slot and one order halves the field, survivors run in full. Fewer errors win and
        the shorter description breaks ties."""
        by = {t.name: t for t in tools}
        best = {n: by[n].description for n in group}
        memo: dict[tuple, int] = {}

        async def score(desc: dict[str, str], cheap: bool) -> int:
            k = (cheap, tuple(sorted(desc.items())))
            if k not in memo:
                ts = await self.trials(tools, desc, reqs, 1 if cheap else RACE_SLOTS, "race", cheap)
                memo[k] = sum(t.error for t in ts)
            return memo[k]

        best_score = start = await score(best, False)
        tried = []
        for name in group:
            options = [c["text"] for c in cands.get(name, [])]
            if not options:
                continue
            cheap = await asyncio.gather(*(score({**best, name: o}, True) for o in options))
            field = sorted(zip(cheap, map(len, options), options, strict=True))[: max(1, math.ceil(len(options) / 2))]
            for c_score, _, o in field:
                s = await score({**best, name: o}, False)
                tried.append({"tool": name, "text": o, "cheap_errors": c_score, "errors": s})
                if (s, len(o)) < (best_score, len(best[name])):
                    best, best_score = {**best, name: o}, s
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
            ts = await self.trials(tools, desc, reqs, self.proj.cfg.slots, cond)
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


def examples(seen: dict[str, dict[tuple, Trial]], limit: int = 24) -> list[dict]:
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
    return unique[:limit]


def beats(test: dict) -> bool:
    return test["verdict"] == "better" and test["p_holm"] < 0.05


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


async def estimate(
    proj, models_: list[str], modes: list[str], n_groups: int, n_cands: int, size: int = 3
) -> tuple[int, float]:
    """Upper bound for every stage, assuming groups of three tools."""
    tools = await proj.tools()
    calls, cost = await proj.discover_cost(models_, modes)
    langs = len(proj.cfg.request_languages or ["en"])
    size = min(size, len(tools))
    reqs = size * proj.cfg.requests_per_tool * langs * 2
    tuning, held = reqs // 2 + 1, reqs // 2 + 1
    race = tuning * RACE_SLOTS * 2 * (1 + size * math.ceil(n_cands / 2)) + tuning * size * n_cands
    confirm = held * proj.cfg.slots * 2 * 3
    per_group = (race + confirm) * len(modes)
    side = n_groups * size * (2 + n_cands + proj.cfg.requests_per_tool * langs)
    for m in models_:
        calls += n_groups * per_group
        cost += proj.estimate(n_groups * per_group, m, tools)
    calls += side
    cost += proj.estimate(side, proj.cfg.role("writer").name, [])
    return calls, cost


async def improve(
    proj,
    models_: list[str] | None = None,
    modes: list[str] | None = None,
    n_groups: int = 5,
    styles: list[str] | None = None,
    max_chars: int = 300,
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
    results, held_by = [], {}
    for i, group in enumerate(groups(spots, n_groups)):
        reqs = await imp.requests(tools, group, spots)
        tuning = [r for r in reqs if split(r, 0) == "tuning"]
        held = held_by[i] = [r for r in reqs if split(r, 0) == "held_out"]
        written, dropped = {}, {}
        for n in group:
            sibs = [by[x] for x in group if x != n]
            raw = await candidates.write(proj.role("writer"), by[n], sibs, imp.styles, max_chars, domain)
            written[n], dropped[n] = await candidates.screen(proj.role("validator"), by[n], raw)
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


async def served(
    proj, server_py: Path, entries: list[dict], results: list[dict], held_by: dict[int, list[Request]], imp: Improver
) -> dict[str, Any]:
    """List the generated FastMCP server's tools and rerun the exported descriptions' held out trials against them,
    since its argument shapes and wording are what agents will see."""
    from misroute.config import ServerConfig
    from misroute.project import Project

    cmd = ["uv", "run", "-q", "--with", f"fastmcp=={export.FASTMCP}", "--with", "pyyaml", "python", str(server_py)]
    cfg = proj.cfg.model_copy(update={"server": ServerConfig(command=cmd)})
    fm = Project(cfg, root=proj.store.root)
    try:
        served_tools = await fm.tools()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
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
        ts = await imp.trials(served_tools, {}, held, proj.cfg.slots, "fastmcp")
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
