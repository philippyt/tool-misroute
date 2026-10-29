from typing import Any
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from misroute.config import Config
from misroute.guard.rules import Rules
from misroute.guard.wrapper import Guard, Held
from misroute.server.catalog import from_mcp
from misroute.server.connect import connect


def siblings(cfg: Config, findings: dict | None) -> list[list[str]]:
    pairs = [list(p) for p in cfg.guard.siblings]
    for s in (findings or {}).get("spots", []):
        pairs.append([s["a"], s["b"]])
    return pairs


def build(upstream: Any, guard: Guard) -> Server:
    async def list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        page = await upstream.list_tools(cursor=params.cursor if params else None)
        return types.ListToolsResult(tools=page.tools, next_cursor=page.next_cursor)

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        out = await guard.call_tool(params.name, dict(params.arguments or {}))
        if isinstance(out, Held):
            return types.CallToolResult(content=[types.TextContent(type="text", text=out.message)], is_error=True)
        return out

    return Server("misroute-guard", on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(cfg: Config, findings: dict | None = None) -> None:
    async with connect(cfg.server) as upstream:
        tools = [from_mcp(t) for t in (await upstream.list_tools()).tools]
        rules = Rules(siblings(cfg, findings), {t.name for t in tools if t.is_write}, cfg.guard)

        async def call(name: str, args: dict[str, Any]) -> Any:
            return await upstream.call_tool(name, args)

        guard = Guard(call, rules, cfg.guard.log, cfg.guard.otel)
        server = build(upstream, guard)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
