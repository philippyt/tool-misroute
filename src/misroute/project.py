import asyncio
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from misroute import models
from misroute.config import Config, load
from misroute.detectors import codemode, confusion, hedge, nearmiss, rank, removal
from misroute.fix import clause, stats
from misroute.lint import descriptions
from misroute.models.base import Budget
from misroute.modes import code_mode, json_mode
from misroute.requests import gate, generate, validate
from misroute.sandbox.monty_runner import Sandbox
from misroute.server.catalog import Tool, catalog_hash, replace_descriptions
from misroute.server.connect import connect, list_tools
from misroute.store.cache import Metered
from misroute.store.runs import Store
from misroute.trial import Request, Trial

Progress = Callable[[int], None]
CONDS = ("original", "sham", "clause")


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

    @classmethod
    def from_yaml(cls, path: str | Path, **kw: Any) -> "Project":
        return cls(load(path), **kw)

    def adapter(self, name: str) -> Metered:
        if name not in self._adapters:
            mc = self.cfg.model(name)
            self._adapters[name] = Metered(models.build(mc), mc, self.budget, self.store.root)
        return self._adapters[name]

    def role(self, role: str) -> Metered:
        return self.adapter(self.cfg.role(role).name)

    async def tools(self) -> list[Tool]:
        if self._tools is None:
            async with connect(self.server) as client:
                self._tools = await list_tools(client)
        return self._tools

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
        out = {"tools": len(tools), "findings": [asdict(f) for f in found], "drafts": drafts}
        self.store.write("lint", out)
        return out

    async def requests_for(self, tool: Tool, tools: list[Tool], sibling: Tool | None = None) -> list[Request]:
        gen, val = self.role("generator"), self.role("validator")
        n, kept = self.cfg.requests_per_tool, []
        for attempt in range(3):
            try:
                reqs = await generate.generate(gen, tool, n, self.cfg.slots, sibling, attempt)
            except (ValueError, KeyError, TypeError):
                continue
            ok, _ = await validate.keep_valid(val, reqs, tools)
            kept += [r for r in ok if r.id not in {k.id for k in kept}]
            if len(kept) >= n:
                break
        return kept[:n]

    async def request_set(self, tools: list[Tool], only: list[Tool] | None = None) -> dict[str, list[Request]]:
        cached = self.store.read("requests") or {}
        out: dict[str, list[Request]] = {}
        wanted = only or tools
        missing = [t for t in wanted if t.name not in cached]
        fresh = await asyncio.gather(*(self.requests_for(t, tools) for t in missing))
        for t, reqs in zip(missing, fresh, strict=True):
            cached[t.name] = [asdict(r) for r in reqs]
        self.store.write("requests", cached)
        for t in wanted:
            out[t.name] = [Request(**r) for r in cached[t.name]]
        if self.cfg.real_requests:
            for r in generate.load_real(self.cfg.real_requests, wanted):
                out[r.tool].append(r)
        return out

    async def run_trials(self, trials: list[Trial], tools_for: Callable[[Trial], list[Tool]]) -> list[Trial]:
        async with Sandbox() as box:

            async def one(t: Trial) -> Trial:
                adapter = self.adapter(t.model)
                ts = tools_for(t)
                if t.mode == "code":
                    await code_mode.run(adapter, box, t, ts)
                else:
                    await json_mode.run(adapter, t, ts)
                    if adapter.cfg.scoring:
                        t.probs = await adapter.score_names(t.text, [x.spec() for x in ts])
                self.progress(1)
                return t

            return list(await asyncio.gather(*(one(t) for t in trials)))

    def estimate(self, n_calls: int, model: str, tools: list[Tool]) -> float:
        a = self.adapter(model)
        return n_calls * a.estimate("x" * 200, [t.spec() for t in tools])

    async def cost(
        self, phase: str, models_: list[str], modes: list, n_pairs: int = 3, trials: int = 96
    ) -> tuple[int, float]:
        if phase == "discover":
            return await self.discover_cost(models_, modes)
        tools = await self.tools()
        per_model = (
            n_pairs * len(modes) * (4 * self.cfg.requests_per_tool * 2 * 2 if phase == "probe" else trials * len(CONDS))
        )
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
                                out.append(Trial(m, mode, r.tool, r.id, text, r.kind, slot, o, rep, "discover"))
        return out

    async def discover_cost(self, models_: list[str], modes: list[str]) -> tuple[int, float]:
        tools = await self.tools()
        per = len(tools) * self.cfg.requests_per_tool * self.cfg.rotations
        gen = len(tools) * (1 + self.cfg.requests_per_tool)
        calls, cost = gen, self.estimate(gen, self.cfg.role("generator").name, [])
        for m in models_:
            for mode in modes:
                n = per * self.repeats(m, mode)
                calls += n
                cost += self.estimate(n, m, tools)
        return calls, cost

    async def discover(self, models_: list[str] | None = None, modes: list[str] | None = None, removal_top: int = 5):
        models_ = models_ or [m.name for m in self.cfg.models]
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
        gated = await asyncio.to_thread(gate.screen, tools, reqs)
        failed = {(min(a, b), max(a, b)) for a, b, _ in gated}
        spots = rank.rank(trials, {}, writes, failed)
        cand = list(dict.fromkeys(n for s in spots[:removal_top] for n in (s.a, s.b)))[:removal_top]
        removed = await self.removal(cand, reqs, models_, tools)
        moved = removal.neighbours(removed)
        spots = rank.rank(trials, moved, writes, failed)
        result = self.summarize(trials, tools, models_, modes)
        result.update(
            spots=[s.to_json() for s in spots],
            noise=noise,
            near_misses=[{"where": w, **asdict(n)} for w, n in near],
            gate_failed=[list(g) for g in gated],
            removal={k: dict(v) for k, v in moved.items()},
            short={k: len(v) for k, v in reqs.items() if len(v) < self.cfg.requests_per_tool},
            catalog=catalog_hash(tools),
            spent=self.budget.spent,
        )
        run = self.store.run_id("discover", result["catalog"])
        self.store.save(run, "discover", result["catalog"], trials + removed, {"models": models_, "modes": modes})
        result["run"] = run
        self.store.write("discover", result)
        return result

    async def removal(self, cand: list[str], reqs: dict[str, list[Request]], models_: list[str], tools: list[Tool]):
        trials = []
        for m in models_:
            for name in cand:
                for r in reqs.get(name, []):
                    slot, text = r.texts()[0]
                    trials.append(Trial(m, "json", name, r.id, text, r.kind, slot, 0, 0, "removal", f"removed:{name}"))
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
                        "wrong_rate": confusion.wrong_rate(ts),
                        "hedge_rate": hedge.hedge_rate(ts),
                        "write_hedge_rate": hedge.hedge_rate(ts, writes),
                        "failures": codemode.rates(ts, mode),
                        "stub_rerun": codemode.rerun_choice(ts),
                        "matrix": {k: dict(v) for k, v in confusion.matrix(ts).items()},
                    }
                )
        return {"cells": cells, "writes": sorted(writes)}

    def pairs_from(self, top: int, pairs: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
        if pairs:
            return pairs
        found = self.store.read("discover")
        if not found:
            raise RuntimeError("run discover first or pass pairs")
        return [(s["a"], s["b"]) for s in found["spots"][:top]]

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
                            out.append(Trial(m, mode, r.tool, r.id, text, r.kind, slot, o, 0, **kw))
        return out

    async def probe(self, top: int = 5, pairs=None, models_=None, modes=None, slots: int = 2) -> dict:
        models_ = models_ or [m.name for m in self.cfg.models]
        modes = modes or list(self.cfg.modes)
        tools = await self.tools()
        by = {t.name: t for t in tools}
        rows, trials = [], []
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

    async def fix(self, top: int = 3, pairs=None, models_=None, modes=None, trials: int = 96) -> dict:
        models_ = models_ or [m.name for m in self.cfg.models]
        modes = modes or list(self.cfg.modes)
        tools = await self.tools()
        by = {t.name: t for t in tools}
        writer = self.role("writer")
        pair_rows, all_trials = [], []
        for a, b in self.pairs_from(top, pairs):
            pr = await self.pair_requests(by[a], by[b], tools)
            reqs = pr["a"] + pr["b"]
            slots = max(1, min(self.cfg.slots, round(trials / (2 * max(len(reqs), 1)))))
            edits = await clause.write(writer, by[a], by[b])
            conds = {"original": tools, **{k: replace_descriptions(tools, v) for k, v in edits.items()}}
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
        result = {"run": run, "pairs": pair_rows, "across": summary, "spent": self.budget.spent}
        self.store.write("fix", result)
        return result

    def verdict(self, a, b, model, mode, cell, edits, gate_p, by) -> dict:
        err = {c: {t.key: t.error for t in cell if t.condition == c} for c in CONDS}
        fixed, broke, p = stats.mcnemar(err["sham"], err["clause"])
        return {
            "pair": f"{a}|{b}",
            "model": model,
            "mode": mode,
            "write": by[a].is_write or by[b].is_write,
            "gate_p": gate_p,
            "n": len(err["clause"]),
            "errors": {c: sum(v.values()) for c, v in err.items()},
            "flagged": sum(err["original"].values()) >= stats.FLAG_MIN_ERRORS,
            "fixed": fixed,
            "broke": broke,
            "p": p,
            "directions": {c: confusion.directions([t for t in cell if t.condition == c], a, b) for c in CONDS},
            "leakage": {c: dict(confusion.leakage([t for t in cell if t.condition == c], a, b)) for c in CONDS},
            "edits": edits,
        }

    @staticmethod
    def adjust(rows: list[dict]) -> None:
        flagged = [r for r in rows if r["flagged"]]
        for r, ph in zip(flagged, stats.holm([r["p"] for r in flagged]), strict=True):
            r["p_holm"] = ph
        for r in rows:
            d = r["errors"]["clause"] - r["errors"]["sham"]
            if not r["flagged"]:
                r["verdict"] = "not flagged"
            elif r.get("p_holm", 1) < 0.05:
                r["verdict"] = "clause beats sham" if d < 0 else "clause worse than sham"
            else:
                r["verdict"] = "no effect, consider a guard rule or merging the tools"

    @staticmethod
    def across(rows: list[dict]) -> list[dict]:
        out = []
        for key in sorted({(r["model"], r["mode"]) for r in rows}):
            rs = [r for r in rows if (r["model"], r["mode"]) == key and r["flagged"]]
            d = [r["errors"]["clause"] - r["errors"]["sham"] for r in rs]
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
