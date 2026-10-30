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


INSTRUCTIONS = """misroute finds the tools agents confuse on an MCP server or OpenAPI spec and tests description
changes on held out requests. It never edits tools: propose results to a person.
Start with lint_descriptions, free. To test one description, call estimate_check, then check_description. To find
confused tools or improve many descriptions, use start_job with kind discover or improve, then job_status. Paid tools
refuse above max_cost. Report verdict, trials, held_out and cost_usd with any claim. insufficient_data means no
answer. improve entries with status draft are untested. The guide resource has the full reference."""


def create(proj: Project) -> MCPServer:
    app = MCPServer("misroute", instructions=INSTRUCTIONS)

    @app.resource("misroute://guide", name="guide", description="Agent guide and reference", mime_type="text/markdown")
    def guide() -> str:
        from misroute.cli import guide_text

        return guide_text()

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
        description="Returns the latest discover, probe, fix and improve results as JSON: risky tool pairs, wrong "
        "pick and hedge rates per model and mode, fix verdicts, and improve entries with their status and files. "
        "Free.",
        annotations=READ,
        structured_output=True,
    )
    async def get_report() -> dict[str, Any]:
        found: dict[str, Any] = proj.store.read("discover") or {}
        fixed: dict[str, Any] = proj.store.read("fix") or {}
        improved: dict[str, Any] = proj.store.read("improve") or {}
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
            "improve": [
                {k: e.get(k) for k in ("tool", "status", "description", "original", "reason")}
                for e in improved.get("entries", [])
                if e.get("status") != "unchanged"
            ],
            "improve_files": improved.get("files", {}),
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
        "longer than a tool call may take. args for all kinds: models (provider:model or configured names) and "
        "modes. discover, probe and fix: top, pairs as [[a, b]] and trials. improve: helper (a model from another "
        'family that writes and judges requests), languages such as ["no", "en"], requests_from (interface or '
        "descriptions), domain, real_requests, max_groups, styles, max_chars, out, fastmcp and drafts. improve "
        "writes files for review and applies nothing. Refuses above max_cost, set estimate_only to get the cost "
        "without running. Poll job_status with the id.",
        annotations=PAID,
        structured_output=True,
    )
    async def start_job(
        kind: str, max_cost: float = 5.0, args: dict[str, Any] | None = None, estimate_only: bool = False
    ) -> dict[str, Any]:
        args = args or {}
        if kind not in ("discover", "probe", "fix", "improve"):
            return {"error": "kind must be discover, probe, fix or improve"}
        modes: list[str] = args.get("modes") or ([str(m) for m in proj.cfg.modes] if kind != "improve" else ["json"])
        pairs = [tuple(p) for p in args.get("pairs") or []] or None
        top, trials = int(args.get("top", 3)), int(args.get("trials", 96))
        target = proj
        if kind == "improve":
            try:
                cfg, models = improver.configure(
                    proj.cfg,
                    args.get("models"),
                    args.get("helper"),
                    args.get("requests_from"),
                    args.get("languages"),
                    args.get("domain"),
                    args.get("real_requests"),
                )
                styles = improver.pick_styles(args.get("styles"))
            except ValueError as e:
                return {"error": str(e)}
            target = Project(
                cfg, server=proj.server if proj.server is not proj.cfg.server else None, root=proj.store.root
            )
            target._tools = proj._tools
            calls, cost = await improver.estimate(target, models, modes, int(args.get("max_groups", 5)), len(styles))
        else:
            models = args.get("models") or proj.cfg.tested()
            calls, cost = await proj.cost(kind, models, modes, len(pairs) if pairs else top, trials)
        if estimate_only:
            return {"schema_version": SCHEMA_VERSION, "estimate_calls": calls, "estimate_usd": round(cost, 4)}
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

        target.progress = tick
        runs = {
            "discover": lambda: proj.discover(models, modes),
            "probe": lambda: proj.probe(top, pairs, models, modes),
            "fix": lambda: proj.fix(top, pairs, models, modes, trials),
            "improve": lambda: improver.improve(
                target,
                models,
                modes,
                int(args.get("max_groups", 5)),
                styles,
                int(args.get("max_chars", 300)),
                Path(args.get("out", "improve")),
                bool(args.get("fastmcp", False)) and bool(target.cfg.server.openapi),
                bool(args.get("drafts", True)),
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
