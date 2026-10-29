from typing import Any
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from misroute import check as checker
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
    async def check_description(tool: str, description: str, max_cost: float = 1.0, refresh: bool = False) -> dict[str, Any]:
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

    return app
