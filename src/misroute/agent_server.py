import asyncio
import uuid
from pathlib import Path
from typing import Any
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from misroute import check as checker
from misroute.improve import run as improver
from misroute.lint import descriptions
from misroute.models.base import BudgetExceeded
from misroute.project import Project
from misroute.store.runs import SCHEMA_VERSION

READ = ToolAnnotations(read_only_hint=True)
PAID = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def create(proj: Project) -> MCPServer:
    app = MCPServer("misroute", instructions="Check MCP tool descriptions against model behaviour. Never edits tools.")

    @app.tool(
        description="Lists tools on the configured MCP server whose descriptions are missing, only a path, only the "
        "tool name, or very short. Free, makes no model calls. Run it first.",
        annotations=READ,
        structured_output=True,
    )
    async def lint_descriptions() -> dict[str, Any]:
        res = await proj.lint(draft=False)
        return {"schema_version": SCHEMA_VERSION, **res}

    @app.tool(
        description="Writes a suggested one sentence description for one tool, for a person to review. "
        "Costs one model call and never changes the server.",
        annotations=PAID,
        structured_output=True,
    )
    async def draft_description(tool: str) -> dict[str, Any]:
        tools = await proj.tools()
        by = {t.name: t for t in tools}
        if tool not in by:
            return {"error": f"unknown tool {tool}"}
        text = await descriptions.draft(proj.role("writer"), by[tool], tools)
        return {"schema_version": SCHEMA_VERSION, "tool": tool, "draft": text, "applied": False}

    @app.tool(
        description="Returns the number of model calls and the estimated dollar cost of check_description for "
        "one tool, without running it. Free.",
        annotations=READ,
        structured_output=True,
    )
    async def estimate_check(tool: str) -> dict[str, Any]:
        calls, cost = checker.estimate(
            proj, 2 + len(checker.neighbours(proj, tool)), proj.cfg.tested(), ["json"], await proj.tools()
        )
        return {"schema_version": SCHEMA_VERSION, "calls": calls, "cost_usd": round(cost, 4)}

    @app.tool(
        description="Tests whether a proposed description for one tool routes requests better than the current "
        "one, on held out requests. Returns better, worse, no_difference or insufficient_data with trial counts. "
        "Refuses to run when the estimate is above max_cost. Never changes the server.",
        annotations=PAID,
        structured_output=True,
    )
    async def check_description(
        tool: str, description: str, max_cost: float = 1.0, refresh: bool = False
    ) -> dict[str, Any]:
        _, cost = checker.estimate(
            proj, 2 + len(checker.neighbours(proj, tool)), proj.cfg.tested(), ["json"], await proj.tools()
        )
        if cost > max_cost:
            return {"error": f"estimate ${cost:.2f} is above max_cost ${max_cost:.2f}", "cost_usd": round(cost, 4)}
        try:
            res = await checker.check(proj, tool, description, refresh=refresh)
        except (BudgetExceeded, KeyError) as e:
            return {"error": str(e)}
        return {"schema_version": SCHEMA_VERSION, "estimate_usd": round(cost, 4), **res}

    @app.tool(
        description="Returns the latest discover, probe and fix results as JSON: risky tool pairs, wrong pick and "
        "hedge rates per model and mode, and fix verdicts. Free.",
        annotations=READ,
        structured_output=True,
    )
    async def get_report() -> dict[str, Any]:
        found: dict[str, Any] = proj.store.read("discover") or {}
        fixed: dict[str, Any] = proj.store.read("fix") or {}
        cells = [
            {k: c[k] for k in ("model", "mode", "trials", "wrong_rate", "hedge_rate", "failures")}
            for c in found.get("cells", [])
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "cells": cells,
            "spots": found.get("spots", [])[:10],
            "fix": [
                {k: r.get(k) for k in ("pair", "model", "mode", "baseline_kind", "errors", "verdict")}
                for r in fixed.get("pairs", [])
            ],
            "stale": proj.store.stale(),
        }

    @app.tool(
        description="Shows requests that went to the wrong one of two tools, in both directions, with what the model "
        "called. Reads stored runs, free. Use it after discover to see why a pair is confused.",
        annotations=READ,
        structured_output=True,
    )
    async def explain_pair(tool_a: str, tool_b: str, limit: int = 5) -> dict[str, Any]:
        from misroute.explain import explain

        return {"schema_version": SCHEMA_VERSION, **explain(proj.store, tool_a, tool_b, limit)}

    @app.tool(
        description="Drafts a boundary clause for two confused tools and tests it against a neutral edit, or against "
        "drafted descriptions when a tool has none. Returns the edits and a verdict. Refuses above max_cost. "
        "Never changes the server.",
        annotations=PAID,
        structured_output=True,
    )
    async def suggest_fix(tool_a: str, tool_b: str, trials: int = 48, max_cost: float = 2.0) -> dict[str, Any]:
        models, modes = proj.cfg.tested(), ["json"]
        _, cost = await proj.cost("fix", models, modes, 1, trials)
        if cost > max_cost:
            return {"error": f"estimate ${cost:.2f} is above max_cost ${max_cost:.2f}", "cost_usd": round(cost, 4)}
        try:
            res = await proj.fix(pairs=[(tool_a, tool_b)], models_=models, modes=modes, trials=trials)
        except (BudgetExceeded, KeyError) as e:
            return {"error": str(e)}
        rows = [
            {k: r.get(k) for k in ("model", "mode", "baseline_kind", "errors", "verdict", "p_holm", "edits")}
            for r in res["pairs"]
        ]
        return {"schema_version": SCHEMA_VERSION, "pair": f"{tool_a}|{tool_b}", "results": rows, "spent": res["spent"]}

    jobs: dict[str, dict[str, Any]] = {}

    @app.tool(
        description="Starts discover, probe, fix or improve in the background and returns a job id at once, for runs "
        "longer than a tool call may take. args takes models, modes, top, pairs as [[a, b]] and trials, and for "
        "improve groups, candidates, max_chars and out. Refuses above max_cost. Poll job_status with the id.",
        annotations=PAID,
        structured_output=True,
    )
    async def start_job(kind: str, max_cost: float = 5.0, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        if kind not in ("discover", "probe", "fix", "improve"):
            return {"error": "kind must be discover, probe, fix or improve"}
        models = args.get("models") or proj.cfg.tested()
        modes: list[str] = args.get("modes") or [str(m) for m in proj.cfg.modes]
        pairs = [tuple(p) for p in args.get("pairs") or []] or None
        top, trials = int(args.get("top", 3)), int(args.get("trials", 96))
        n_groups, n_cands = int(args.get("groups", 5)), max(1, min(int(args.get("candidates", 4)), 4))
        if kind == "improve":
            calls, cost = await improver.estimate(proj, models, modes, n_groups, n_cands)
        else:
            calls, cost = await proj.cost(kind, models, modes, len(pairs) if pairs else top, trials)
        if cost > max_cost:
            return {"error": f"estimate ${cost:.2f} is above max_cost ${max_cost:.2f}", "cost_usd": round(cost, 4)}
        job_id = uuid.uuid4().hex[:8]
        job: dict[str, Any] = {
            "kind": kind,
            "status": "running",
            "done": 0,
            "total": calls,
            "result": None,
            "error": None,
        }

        def tick(n: int) -> None:
            job["done"] += n

        proj.progress = tick
        runs = {
            "discover": lambda: proj.discover(models, modes),
            "probe": lambda: proj.probe(top, pairs, models, modes),
            "fix": lambda: proj.fix(top, pairs, models, modes, trials),
            "improve": lambda: improver.improve(
                proj,
                models,
                modes,
                n_groups,
                n_cands,
                int(args.get("max_chars", 300)),
                Path(args.get("out", "improve")),
            ),
        }

        async def go() -> None:
            try:
                job["result"] = await runs[kind]()
                job["status"] = "done"
            except Exception as e:
                job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"

        job["task"] = asyncio.create_task(go())
        jobs[job_id] = job
        return {
            "schema_version": SCHEMA_VERSION,
            "job": job_id,
            "estimate_calls": calls,
            "estimate_usd": round(cost, 4),
        }

    @app.tool(
        description="Returns the status of a background job: running with progress, done with the result, or failed "
        "with the error. Free.",
        annotations=READ,
        structured_output=True,
    )
    async def job_status(job: str) -> dict[str, Any]:
        j = jobs.get(job)
        if not j:
            return {"error": f"unknown job {job}"}
        return {"schema_version": SCHEMA_VERSION, "job": job, **{k: v for k, v in j.items() if k != "task"}}

    @app.tool(description="Cancels a running background job.", annotations=READ, structured_output=True)
    async def cancel_job(job: str) -> dict[str, Any]:
        j = jobs.get(job)
        if not j:
            return {"error": f"unknown job {job}"}
        j["task"].cancel()
        j["status"] = "cancelled"
        return {"schema_version": SCHEMA_VERSION, "job": job, "status": "cancelled"}

    return app
