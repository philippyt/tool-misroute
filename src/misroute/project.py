import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from misroute import models
from misroute.config import Config, ServerConfig, load
from misroute.detectors import codemode, confusion, hedge, nearmiss, rank, removal
from misroute.fix import clause, stats
from misroute.lint import descriptions
from misroute.models.base import Budget, BudgetExceeded, ProviderRefused
from misroute.modes import code_mode, json_mode
from misroute.requests import gate, generate, validate
from misroute.sandbox.fake_tools import load_fixtures
from misroute.sandbox.monty_runner import Sandbox
from misroute.server import openapi, static
from misroute.server.catalog import Tool, catalog_hash, effect_unknown, replace_descriptions
from misroute.server.connect import connect, keep, list_tools
from misroute.store.cache import Metered, fill_price
from misroute.store.runs import Store
from misroute.trial import Request, Trial

Progress = Callable[[int], None]
PILOT_CALLS = 3
CONDS = ("original", "baseline", "clause")


def rates_by(trials: list[Trial], attr: str) -> dict[str, dict[str, Any]]:
    """Wrong and error rates per value of a trial field such as lang or kind, empty when there is one value."""
    values = sorted({str(getattr(t, attr)) for t in trials})
    if len(values) < 2:
        return {}
    out = {}
    for v in values:
        ts = [t for t in trials if str(getattr(t, attr)) == v]
        out[v] = {
            "trials": len(ts),
            "wrong_rate": sum(t.wrong for t in ts) / len(ts),
            "error_rate": sum(t.error for t in ts) / len(ts),
        }
    return out


def orders(tools: list[Tool], k: int) -> list[list[Tool]]:
    n = len(tools)
    out = []
    for i in range(max(k, 1)):
        shift = (i * n // max(k, 1)) % max(n, 1)
        o = tools[shift:] + tools[:shift]
        out.append(o[::-1] if i % 2 else o)
    return out


def pair_orders(tools: list[Tool]) -> list[list[Tool]]:
    return [tools, tools[::-1]]


class Project:
    def __init__(self, cfg: Config, server: Any = None, root: str | Path | None = None):
        self.cfg = cfg
        self.server = server if server is not None else cfg.server
        self.store = Store(root or cfg.workdir)
        self.budget = Budget(cfg.budget_usd)
        self._adapters: dict[str, Metered] = {}
        self._tools: list[Tool] | None = None
        self.progress: Progress = lambda n: None
        self.add_total: Progress = lambda n: None
        self.rejected: dict[str, Counter] = {}
        self.dropped: dict[str, list[dict]] = {}
        self.request_use = {"written": 0, "reused": 0}
        self.unreadable: Counter = Counter()
        self._fixtures: dict[str, Any] | None = None
        self._turns: dict[str, Any] | None = None

    @classmethod
    def from_yaml(cls, path: str | Path, **kw: Any) -> "Project":
        return cls(load(path), **kw)

    @classmethod
    def from_tools(
        cls, tools: list[Any], model: str | list[str], root: str | Path = ".misroute", **settings: Any
    ) -> "Project":
        """Test tools held in Python: dicts in MCP, OpenAI or Anthropic format, functions or framework tool objects.
        model takes provider:model shorthand, for example anthropic:claude-sonnet-5-5."""
        models = [model] if isinstance(model, str) else list(model)
        cfg = Config.model_validate({"server": {"tools": "(python)"}, "models": models, **settings})
        proj = cls(cfg, root=root)
        proj._tools = [static.from_object(t) for t in tools]
        return proj

    def adapter(self, name: str) -> Metered:
        if name not in self._adapters:
            mc = self.cfg.model(name)
            self._adapters[name] = Metered(models.build(mc), mc, self.budget, self.store.root)
        return self._adapters[name]

    def role(self, role: str) -> Metered:
        return self.adapter(self.cfg.role(role).name)

    def usage(self) -> dict[str, dict[str, Any]]:
        """Measured calls, tokens and dollars per model, with its roles, to hold against the estimate."""
        out = {}
        for name, a in self._adapters.items():
            roles = [r for r in ("generator", "validator", "writer") if self.cfg.role(r).name == name]
            tested = ["tested"] if name in self.cfg.tested() else []
            out[name] = {"roles": tested + roles, **a.usage()}
        return out

    def unpriced(self) -> list[str]:
        return [m.name for m in self.cfg.models if not fill_price(m)]

    def cap(self, max_cost: float) -> None:
        self.budget.limit = min(self.cfg.budget_usd, self.budget.spent + self.budget.reserved + max_cost)

    async def tools(self) -> list[Tool]:
        """The catalog under test: the server's tools, then those of every further server under servers, the way
        an agent with several servers connected sees them. A name two servers share gets the later server's label
        in front."""
        if self._tools is None:
            found = await self.load_tools(self.cfg.server, self.server)
            for extra in self.cfg.servers:
                taken = {t.name for t in found}
                for t in await self.load_tools(extra, extra):
                    found.append(replace(t, name=f"{extra.label}_{t.name}") if t.name in taken else t)
            self._tools = found
        return self._tools

    async def load_tools(self, cfg: ServerConfig, src: Any) -> list[Tool]:
        if (cfg.openapi or cfg.tools) and src is cfg:
            listed = openapi.tools(cfg.openapi) if cfg.openapi else static.tools(str(cfg.tools))
            found = [t for t in listed if keep(t.name, cfg.include_tools, cfg.exclude_tools)]
        else:
            async with connect(src) as client:
                found = await list_tools(client, cfg.include_tools, cfg.exclude_tools)
        return [replace(t, server=cfg.label) for t in found] if self.cfg.servers else found

    def system(self) -> str | None:
        """The agent's own system prompt, as text or a file, so routing is measured under the instructions the
        model has in production."""
        text = self.cfg.system_prompt or ""
        if text and len(text) < 260 and "\n" not in text and Path(text).is_file():
            text = Path(text).read_text(encoding="utf-8")
        return text.strip() or None

    async def catalog(self) -> str:
        return catalog_hash(await self.tools())

    async def lint(self, draft: bool = False) -> dict[str, Any]:
        tools = await self.tools()
        found = descriptions.lint(tools)
        drafts = {}
        if draft and found:
            gen = self.role("writer")
            by_name = {t.name: t for t in tools}
            drafts = dict(
                zip(
                    [f.tool for f in found],
                    await asyncio.gather(*(descriptions.draft(gen, by_name[f.tool], tools) for f in found)),
                    strict=True,
                )
            )
        out = {
            "tools": len(tools),
            "findings": [asdict(f) for f in found],
            "drafts": drafts,
            "effect_unknown": effect_unknown(tools),
        }
        self.store.write("lint", out)
        return out

    def lookups(self, tools: list[Tool]) -> frozenset[str]:
        """Tools named by lookup_tools, by name or pattern: entry points an agent is meant to call before another
        tool, whose calls are not scored as choices."""
        return frozenset(t.name for t in tools if self.cfg.lookup_tools and keep(t.name, self.cfg.lookup_tools, []))

    def fixtures(self) -> dict[str, Any]:
        if self._fixtures is None:
            self._fixtures = load_fixtures(self.cfg.fixtures)
        return self._fixtures

    def domain(self) -> str:
        text = self.cfg.domain or ""
        if text and len(text) < 260 and "\n" not in text and Path(text).is_file():
            return Path(text).read_text(encoding="utf-8")
        return text

    async def requests_for(
        self, tool: Tool, tools: list[Tool], sibling: Tool | None = None, seed: int = 0
    ) -> list[Request]:
        """requests_per_tool validated requests in each configured language."""
        langs = self.cfg.request_languages or ["en"]
        found = await asyncio.gather(*(self.requests_in(tool, tools, sibling, seed, lang) for lang in langs))
        return [r for rs in found for r in rs]

    async def requests_in(
        self, tool: Tool, tools: list[Tool], sibling: Tool | None, seed: int, lang: str
    ) -> list[Request]:
        gen, val = self.role("generator"), self.role("validator")
        n, kept, src = self.cfg.requests_per_tool, [], self.cfg.request_source
        turned_down: list[dict] = []
        for attempt in range(generate.ROUNDS):
            if len(kept) >= n:
                break
            try:
                reqs = await generate.generate(
                    gen, tool, n, self.cfg.slots, sibling, f"{seed}:{attempt}", src, lang, self.domain(), turned_down
                )
            except (ValueError, KeyError, TypeError):
                self.unreadable[tool.name] += 1
                break
            ok, bad = await validate.keep_valid(val, reqs, tools, src, self.domain(), self.lookups(tools))
            self.rejected.setdefault(tool.name, Counter()).update(b["picked"] or "(no tool)" for b in bad)
            self.dropped.setdefault(tool.name, []).extend(bad)
            turned_down += bad
            kept += [r for r in ok if r.id not in {k.id for k in kept}]
        return kept[:n]

    async def request_set(self, tools: list[Tool], only: list[Tool] | None = None) -> dict[str, list[Request]]:
        cached = self.store.read("requests") or {}
        cached.pop("schema_version", None)
        out: dict[str, list[Request]] = {}
        wanted = only or tools
        key = self.request_key
        full = self.cfg.requests_per_tool * len(self.cfg.request_languages or ["en"])
        missing = [t for t in wanted if len(cached.get(key(t)) or []) < full]
        self.request_use["written"] += len(missing)
        self.request_use["reused"] += len(wanted) - len(missing)
        fresh = await asyncio.gather(*(self.requests_for(t, tools) for t in missing))
        for t, reqs in zip(missing, fresh, strict=True):
            cached[key(t)] = [asdict(r) for r in reqs]
        self.store.write("requests", cached)
        for t in wanted:
            out[t.name] = [Request(**r) for r in cached[key(t)]]
        if self.cfg.real_requests:
            for r in generate.load_real(self.cfg.real_requests, wanted):
                out[r.tool].append(r)
        return out

    def request_key(self, tool: Tool) -> str:
        """Where a tool's written requests are stored: under the source, the languages and the recipe that wrote
        them, so requests from other instructions or another domain are never reused."""
        langs = self.cfg.request_languages or ["en"]
        made = validate.recipe(self.domain(), self.cfg.slots)
        return f"{tool.name}|{self.cfg.request_source}|{','.join(langs)}|{made}"

    async def run_trials(self, trials: list[Trial], tools_for: Callable[[Trial], list[Tool]]) -> list[Trial]:
        stopped: list[BudgetExceeded] = []
        fixed, system = self.fixtures(), self.system()
        async with Sandbox() as box:

            async def one(t: Trial) -> Trial | None:
                adapter = self.adapter(t.model)
                ts = tools_for(t)
                looks = self.lookups(ts)
                try:
                    if t.mode == "code":
                        await code_mode.run(
                            adapter, box, t, ts, self.cfg.max_turns, fixtures=fixed, lookups=looks, system=system
                        )
                    else:
                        await json_mode.run(adapter, t, ts, self.cfg.max_turns, fixed, looks, system)
                        if adapter.cfg.scoring:
                            t.probs = await adapter.score_names(t.text, [x.spec() for x in ts])
                except ProviderRefused:
                    raise
                except BudgetExceeded as e:
                    stopped.append(e)
                    return None
                self.progress(1)
                return t

            done = [t for t in await asyncio.gather(*(one(t) for t in trials)) if t is not None]
        self.note_turns(done)
        if stopped:
            raise await self.stop_early(stopped[0], done, trials)
        return done

    def note_turns(self, done: list[Trial]) -> None:
        """Keep how many model calls a trial took per model at this max_turns, so the next estimate uses what
        was measured instead of assuming every trial runs all its turns."""
        seen = self.store.read("turns") or {}
        seen.pop("schema_version", None)
        for t in done:
            if t.phase == "removal" or not t.turns:
                continue
            row = seen.setdefault(t.model, {"max_turns": self.cfg.max_turns, "trials": 0, "calls": 0})
            if row["max_turns"] != self.cfg.max_turns:
                row.update(max_turns=self.cfg.max_turns, trials=0, calls=0)
            row["trials"] += 1
            row["calls"] += t.turns
        if done:
            self.store.write("turns", seen)
        self._turns = None

    def turns_per_trial(self, model: str) -> float:
        """Measured model calls per trial for this model at this max_turns, else max_turns, the upper end."""
        if self._turns is None:
            self._turns = self.store.read("turns") or {}
        row = self._turns.get(model)
        if isinstance(row, dict) and row.get("trials") and row.get("max_turns") == self.cfg.max_turns:
            return row["calls"] / row["trials"]
        return float(self.cfg.max_turns)

    def turns_measured(self, models_: list[str]) -> bool:
        return self.cfg.max_turns == 1 or all(self.turns_per_trial(m) < self.cfg.max_turns for m in models_)

    async def stop_early(self, why: BudgetExceeded, done: list[Trial], trials: list[Trial]) -> BudgetExceeded:
        cat = await self.catalog()
        phase = f"{trials[0].phase} partial"
        run = self.store.run_id(phase.replace(" ", "-"), cat)
        self.store.save(run, phase, cat, done, {"stopped": str(why)})
        return BudgetExceeded(
            f"{why}. {len(done)} of {len(trials)} trials finished and were saved as {run}, a rerun with more budget "
            "reuses them at no cost"
        )

    def cross_server(self, spots: list[dict], tools: list[Tool]) -> None:
        """Mark pairs whose tools come from different servers, which no single server's owner would find alone."""
        home = {t.name: t.server for t in tools}
        for s in spots:
            s["cross_server"] = bool(self.cfg.servers) and home.get(s["a"]) != home.get(s["b"])

    def estimate(self, n_calls: int, model: str, tools: list[Tool], turns: int | None = None) -> float:
        """Dollars for n_calls trials of up to max_turns model calls each. Helper calls and removal trials take
        one call, so they pass turns 1."""
        a = self.adapter(model)
        per_call = a.estimate("x" * 200, [t.spec() for t in tools])
        return n_calls * (self.turns_per_trial(model) if turns is None else turns) * per_call

    async def pilot(self, models_: list[str], modes: list[str], per_mode: int = PILOT_CALLS) -> int:
        """A few real trials per model and mode, so the estimate that follows uses reply sizes this model
        actually gives on this catalog instead of a guess. Returns the number of trials run."""
        tools = await self.tools()
        step = max(len(tools) // per_mode, 1)
        picked = tools[::step][:per_mode]
        todo = [
            Trial(m, mode, t.name, f"pilot{i}", f"I need this: {t.description or t.name}", phase="pilot")
            for m in models_
            for mode in modes
            for i, t in enumerate(picked)
        ]
        return len(await self.run_trials(todo, lambda t: tools))

    async def cost(
        self,
        phase: str,
        models_: list[str],
        modes: list,
        top: int | None = None,
        pairs: list[tuple[str, str]] | None = None,
        trials: int = 96,
    ) -> tuple[int, float]:
        """Model calls and dollars for a phase, over the pairs it would run. A probe pair runs base and borrowed
        requests for both tools in every slot and both orders."""
        if phase == "discover":
            return await self.discover_cost(models_, modes)
        tools = await self.tools()
        try:
            n_pairs = len(self.pairs_from(top, pairs))
        except RuntimeError:
            n_pairs = len(pairs or [])
        langs = len(self.cfg.request_languages or ["en"])
        per_pair = (
            2 * 2 * self.cfg.requests_per_tool * langs * self.cfg.slots * 2 if phase == "probe" else trials * len(CONDS)
        )
        per_model = n_pairs * len(modes) * per_pair
        return per_model * len(models_), sum(self.estimate(per_model, m, tools) for m in models_)

    def repeats(self, model: str, mode: str) -> int:
        mc = self.cfg.model(model)
        return 1 if mc.scoring or mode == "code" else self.cfg.repeats

    def discover_trials(self, reqs: dict[str, list[Request]], models_: list[str], modes: list[str]) -> list[Trial]:
        out = []
        for m in models_:
            for mode in modes:
                for rs in reqs.values():
                    for r in rs:
                        texts = r.texts()
                        for o in range(self.cfg.rotations):
                            slot, text = texts[o % len(texts)]
                            for rep in range(self.repeats(m, mode)):
                                t = Trial(m, mode, r.tool, r.id, text, r.kind, slot, o, rep, "discover", lang=r.lang)
                                out.append(t)
        return out

    async def discover_cost(self, models_: list[str], modes: list[str]) -> tuple[int, float]:
        tools = await self.tools()
        langs = len(self.cfg.request_languages or ["en"])
        requests = len(tools) * self.cfg.requests_per_tool * langs
        written, judged = len(tools) * langs, requests
        calls = written + judged
        cost = self.estimate(written, self.cfg.role("generator").name, [], 1)
        cost += self.estimate(judged, self.cfg.role("validator").name, tools, 1)
        for m in models_:
            removal_trials = requests if "json" in modes else 0
            calls += removal_trials
            cost += self.estimate(removal_trials, m, tools, 1)
            for mode in modes:
                n = requests * self.cfg.rotations * self.repeats(m, mode)
                calls += round(n * self.turns_per_trial(m))
                cost += self.estimate(n, m, tools)
        return calls, cost

    async def discover(self, models_: list[str] | None = None, modes: list[str] | None = None):
        models_ = models_ or self.cfg.tested()
        modes = modes or list(self.cfg.modes)
        tools = await self.tools()
        rots = orders(tools, self.cfg.rotations)
        reqs = await self.request_set(tools)
        trials = await self.run_trials(self.discover_trials(reqs, models_, modes), lambda t: rots[t.order])
        near: list[tuple[str, nearmiss.NearMiss]] = []
        noise = {}
        for m in models_:
            ts = [t for t in trials if t.model == m and t.mode == "json"]
            if self.cfg.model(m).scoring:
                near += [(f"{m} json", x) for x in nearmiss.from_probs(ts)]
            else:
                noise[m], found = nearmiss.from_repeats(ts)
                near += [(f"{m} json", x) for x in found]
        writes = {t.name for t in tools if t.is_write}
        spots = rank.rank(trials, {}, writes)
        cand = list(dict.fromkeys(n for s in spots if s.flagged for n in (s.a, s.b)))
        removed = await self.removal(cand, reqs, models_, tools)
        moved = removal.neighbours(removed)
        spots = rank.rank(trials, moved, writes)
        for s in spots:
            s.thin = min(len(reqs.get(s.a, [])), len(reqs.get(s.b, []))) < stats.MIN_DISCORDANT_TEMPLATES
        result = self.summarize(trials, tools, models_, modes)
        found = [s.to_json() for s in spots]
        self.cross_server(found, tools)
        result.update(
            spots=found,
            servers={t.name: t.server for t in tools} if self.cfg.servers else {},
            system_prompt=bool(self.system()),
            unrouted=codemode.unrouted(trials),
            noise=noise,
            near_misses=[{"where": w, **asdict(n)} for w, n in near],
            removal={k: dict(v) for k, v in moved.items()},
            short=self.shortfall(reqs),
            rejected_requests={k: v for k, v in self.dropped.items() if v},
            request_use=dict(self.request_use),
            thin_interface=[t.name for t in tools if not t.output_schema] if self.cfg.request_source == "spec" else [],
            roles_warning=self.cfg.role_warnings(),
            request_source=self.cfg.request_source,
            by_language=rates_by(trials, "lang"),
            by_kind=rates_by(trials, "kind"),
            untested=sorted(k for k, v in reqs.items() if not v),
            effect_unknown=effect_unknown(tools),
            lookup_tools=sorted(self.lookups(tools)),
            lookup_candidates=rank.lookup_candidates(trials, self.lookups(tools)),
            max_turns=self.cfg.max_turns,
            catalog=catalog_hash(tools),
            spent=self.budget.spent,
            usage=self.usage(),
        )
        run = self.store.run_id("discover", result["catalog"])
        self.store.save(run, "discover", result["catalog"], trials + removed, {"models": models_, "modes": modes})
        result["run"] = run
        self.store.write("discover", result)
        return result

    def shortfall(self, reqs: dict[str, list[Request]]) -> dict[str, dict]:
        """Tools whose generated requests the validator mostly rejected, with the tools it picked instead."""
        saved = self.store.read("rejections") or {}
        saved.update({k: dict(v) for k, v in self.rejected.items()})
        saved.pop("schema_version", None)
        self.store.write("rejections", saved)
        want = self.cfg.requests_per_tool * len(self.cfg.request_languages or ["en"])
        return {
            k: {"kept": len(v), "validator_picked": saved.get(k, {}), "unreadable_replies": self.unreadable.get(k, 0)}
            for k, v in reqs.items()
            if len(v) < want
        }

    async def removal(self, cand: list[str], reqs: dict[str, list[Request]], models_: list[str], tools: list[Tool]):
        trials = []
        for m in models_:
            for name in cand:
                for r in reqs.get(name, []):
                    slot, text = r.texts()[0]
                    trials.append(
                        Trial(
                            m, "json", name, r.id, text, r.kind, slot, 0, 0, "removal", f"removed:{name}", lang=r.lang
                        )
                    )
        return await self.run_trials(trials, lambda t: [x for x in tools if x.name != t.target])

    def summarize(self, trials: list[Trial], tools: list[Tool], models_: list[str], modes: list[str]) -> dict:
        writes = {t.name for t in tools if t.is_write}
        cells = []
        for m in models_:
            for mode in modes:
                ts = [t for t in trials if t.model == m and t.mode == mode]
                if not ts:
                    continue
                cells.append(
                    {
                        "model": m,
                        "mode": mode,
                        "trials": len(ts),
                        "accuracy": sum(not t.error for t in ts) / len(ts),
                        "decided": sum(t.decided for t in ts),
                        "accuracy_decided": sum(not t.error for t in ts) / max(sum(t.decided for t in ts), 1),
                        "unread_pages_rate": sum(t.unread_pages for t in ts) / len(ts),
                        "reached_rate": sum(t.reached for t in ts) / len(ts),
                        "wrong_rate": confusion.wrong_rate(ts),
                        "hedge_rate": hedge.hedge_rate(ts),
                        "write_hedge_rate": hedge.hedge_rate(ts, writes),
                        "failures": codemode.rates(ts, mode),
                        "bad_args_rate": sum(bool(t.bad_args) for t in ts) / len(ts),
                        "bad_args": [
                            {"problem": k, "trials": v}
                            for k, v in Counter(e for t in ts for e in set(t.bad_args)).most_common(10)
                        ],
                        "stub_rerun": codemode.rerun_choice(ts),
                        "unanswered": codemode.unanswered(ts),
                        "matrix": {k: dict(v) for k, v in confusion.matrix(ts).items()},
                    }
                )
        return {"cells": cells, "writes": sorted(writes)}

    def pairs_from(self, top: int | None, pairs: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
        """Given pairs, else the top pairs from discover, else every pair discover flagged by the flag rule."""
        if pairs:
            return pairs
        found = self.store.read("discover")
        if not found:
            raise RuntimeError("run discover first or pass pairs")
        spots = found["spots"]
        chosen = spots[:top] if top else [s for s in spots if s.get("flagged")]
        return [(s["a"], s["b"]) for s in chosen]

    async def pair_requests(self, a: Tool, b: Tool, tools: list[Tool]) -> dict[str, Any]:
        base = await self.request_set(tools, [a, b])
        borrowed = await asyncio.gather(self.requests_for(a, tools, b), self.requests_for(b, tools, a))
        ra, rb = base[a.name] + borrowed[0], base[b.name] + borrowed[1]
        p = gate.gate(a, b, ra, rb)
        return {"a": ra, "b": rb, "gate_p": p, "gate_ok": p > gate.ALPHA}

    def pair_trials(self, reqs: list[Request], models_: list[str], modes: list, slots: int, **kw) -> list[Trial]:
        out = []
        for m in models_:
            for mode in modes:
                for r in reqs:
                    for slot, text in r.texts()[:slots]:
                        for o in range(2):
                            out.append(Trial(m, mode, r.tool, r.id, text, r.kind, slot, o, 0, lang=r.lang, **kw))
        return out

    async def probe(
        self, top: int | None = None, pairs=None, models_=None, modes=None, slots: int | None = None
    ) -> dict:
        models_ = models_ or self.cfg.tested()
        modes = modes or list(self.cfg.modes)
        tools = await self.tools()
        by = {t.name: t for t in tools}
        rows, trials = [], []
        slots = slots or self.cfg.slots
        for a, b in self.pairs_from(top, pairs):
            pr = await self.pair_requests(by[a], by[b], tools)
            ts = await self.run_trials(
                self.pair_trials(pr["a"] + pr["b"], models_, modes, slots, phase="probe", pair=f"{a}|{b}"),
                lambda t: pair_orders(tools)[t.order],
            )
            trials += ts
            for m in models_:
                for mode in modes:
                    cell = [t for t in ts if t.model == m and t.mode == mode]
                    rows.append(
                        {
                            "pair": f"{a}|{b}",
                            "model": m,
                            "mode": mode,
                            "write": by[a].is_write or by[b].is_write,
                            "gate_p": pr["gate_p"],
                            "directions": confusion.directions(cell, a, b),
                            "borrowed_wrong": confusion.wrong_rate([t for t in cell if t.kind == "borrowed"]),
                            "goal_wrong": confusion.wrong_rate([t for t in cell if t.kind == "goal"]),
                            "real_wrong": confusion.wrong_rate([t for t in cell if t.kind == "real"]),
                            "failures": codemode.rates(cell, mode),
                        }
                    )
        cat = catalog_hash(tools)
        run = self.store.run_id("probe", cat)
        self.store.save(run, "probe", cat, trials, {"models": models_, "modes": modes})
        result = {"run": run, "rows": rows, "spent": self.budget.spent}
        self.store.write("probe", result)
        return result

    async def fix(self, top: int | None = None, pairs=None, models_=None, modes=None, trials: int = 96) -> dict:
        models_ = models_ or self.cfg.tested()
        modes = modes or list(self.cfg.modes)
        tools = await self.tools()
        by = {t.name: t for t in tools}
        writer = self.role("writer")
        pair_rows, all_trials = [], []
        for a, b in self.pairs_from(top, pairs):
            pr = await self.pair_requests(by[a], by[b], tools)
            reqs = pr["a"] + pr["b"]
            slots = max(1, min(self.cfg.slots, round(trials / (2 * max(len(reqs), 1)))))
            drafts = await self.drafts([by[a], by[b]], tools)
            edits = await clause.write(writer, by[a], by[b], drafts)
            conds = {
                "original": tools,
                "baseline": replace_descriptions(tools, edits["baseline"]),
                "clause": replace_descriptions(tools, edits["clause"]),
            }
            cell_trials = []
            for name, ts in conds.items():
                os_ = pair_orders(ts)
                cell_trials += await self.run_trials(
                    self.pair_trials(reqs, models_, modes, slots, phase="fix", pair=f"{a}|{b}", condition=name),
                    lambda t, os_=os_: os_[t.order],
                )
            all_trials += cell_trials
            for m in models_:
                for mode in modes:
                    cell = [t for t in cell_trials if t.model == m and t.mode == mode]
                    pair_rows.append(self.verdict(a, b, m, mode, cell, edits, pr["gate_p"], by))
        self.adjust(pair_rows)
        summary = self.across(pair_rows)
        cat = catalog_hash(tools)
        run = self.store.run_id("fix", cat)
        self.store.save(run, "fix", cat, all_trials, {"models": models_, "modes": modes, "trials": trials})
        result = {
            "run": run,
            "pairs": pair_rows,
            "across": summary,
            "spent": self.budget.spent,
            "roles_warning": self.cfg.role_warnings(),
        }
        self.store.write("fix", result)
        return result

    async def drafts(self, pair: list[Tool], tools: list[Tool]) -> dict[str, str]:
        """Drafted descriptions for tools that lint flags, from the lint run when there is one."""
        flagged = [t for t in pair if descriptions.classify(t)]
        if not flagged:
            return {}
        known = (self.store.read("lint") or {}).get("drafts", {})
        writer = self.role("writer")
        return {t.name: known.get(t.name) or await descriptions.draft(writer, t, tools) for t in flagged}

    def verdict(self, a, b, model, mode, cell, edits, gate_p, by) -> dict:
        err = {c: {t.key: t.error for t in cell if t.condition == c} for c in CONDS}
        hedges = {c: {t.key: t.hedge for t in cell if t.condition == c} for c in ("baseline", "clause")}
        test = stats.template_test(err["baseline"], err["clause"])
        # The baseline test asks whether the clause's content matters beyond a
        # same-length edit. The deploy decision is a different question: is the
        # clause better than the description running now?
        vs_original = stats.template_test(err["original"], err["clause"])
        return {
            "pair": f"{a}|{b}",
            "baseline_kind": edits["baseline_kind"],
            "model": model,
            "mode": mode,
            "write": by[a].is_write or by[b].is_write,
            "gate_p": gate_p,
            "n": len(err["clause"]),
            "errors": {c: sum(v.values()) for c, v in err.items()},
            "flagged": sum(err["original"].values()) >= stats.FLAG_MIN_ERRORS,
            **test,
            "vs_original": vs_original,
            "hedge_p": stats.template_test(hedges["baseline"], hedges["clause"])["p"],
            "directions": {c: confusion.directions([t for t in cell if t.condition == c], a, b) for c in CONDS},
            "leakage": {c: dict(confusion.leakage([t for t in cell if t.condition == c], a, b)) for c in CONDS},
            "edits": edits,
        }

    @staticmethod
    def adjust(rows: list[dict]) -> None:
        """Verdicts answer the deploy question. Beating the baseline proves the clause's content matters;
        only beating the original too makes it worth shipping. A clause that beats the baseline while the
        original beats them both means the content is load bearing but the clause adds nothing on top."""
        flagged = [r for r in rows if r["flagged"]]
        for r, ph in zip(flagged, stats.holm([r["p"] for r in flagged]), strict=True):
            r["p_holm"] = ph
        for r in rows:
            d = r["errors"]["clause"] - r["errors"]["baseline"]
            d_orig = r["errors"]["clause"] - r["errors"]["original"]
            beats_original = d_orig < 0 and r["vs_original"]["p"] < 0.05
            if not r["flagged"]:
                r["verdict"] = "not flagged"
            elif 0 < r["discordant_templates"] < stats.MIN_DISCORDANT_TEMPLATES:
                r["verdict"] = f"too few request templates differ ({r['discordant_templates']}), run more requests"
            elif r.get("p_holm", 1) < 0.05 and d < 0:
                r["verdict"] = (
                    "deploy: clause beats the baseline and the original"
                    if beats_original
                    else "do not deploy: content matters but the clause does not beat the original description"
                )
            elif r.get("p_holm", 1) < 0.05:
                r["verdict"] = "clause hurts"
            else:
                r["verdict"] = "no difference, consider a guard rule or merging the tools"

    @staticmethod
    def across(rows: list[dict]) -> list[dict]:
        out = []
        for key in sorted({(r["model"], r["mode"]) for r in rows}):
            rs = [r for r in rows if (r["model"], r["mode"]) == key and r["flagged"]]
            d = [r["errors"]["clause"] - r["errors"]["baseline"] for r in rs]
            out.append(
                {
                    "model": key[0],
                    "mode": key[1],
                    "pairs": len(rs),
                    "mean_diff": sum(d) / len(d) if d else 0.0,
                    "p": stats.signflip(d, "less") if d else 1.0,
                    "underpowered": len(rs) < stats.MIN_PAIRS_SIGNFLIP,
                }
            )
        return out

    def breaches(self) -> list[str]:
        found = self.store.read("discover")
        if not found:
            return ["no discover results"]
        lim, out = self.cfg.limits, []
        for c in found["cells"]:
            where = f"{c['model']} {c['mode']}"
            if lim.max_wrong_rate is not None and c["wrong_rate"] > lim.max_wrong_rate:
                out.append(f"{where} wrong rate {c['wrong_rate']:.3f} above {lim.max_wrong_rate}")
            if lim.max_write_hedge_rate is not None and c["write_hedge_rate"] > lim.max_write_hedge_rate:
                out.append(f"{where} write hedge rate {c['write_hedge_rate']:.3f} above {lim.max_write_hedge_rate}")
            fail = sum(c["failures"].values()) if c["mode"] == "code" else 0
            if lim.max_code_failure_rate is not None and fail > lim.max_code_failure_rate:
                out.append(f"{where} code failure rate {fail:.3f} above {lim.max_code_failure_rate}")
        return out
