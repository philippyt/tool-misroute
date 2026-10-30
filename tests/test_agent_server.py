from mcp import Client
from misroute.agent_server import create
from misroute.lint.descriptions import lint
from misroute.server.catalog import from_mcp


async def test_agent_server(project):
    async with Client(create(project)) as client:
        tools = (await client.list_tools()).tools
        assert {
            "lint_descriptions",
            "draft_description",
            "estimate_check",
            "check_description",
            "get_report",
        } <= {t.name for t in tools}
        assert lint([from_mcp(t) for t in tools]) == []
        res = await client.call_tool("lint_descriptions", {})
        assert not res.is_error and res.structured_content["schema_version"] == 1
        res = await client.call_tool("check_description", {"tool": "get_customer", "description": "x", "max_cost": -1})
        assert "above max_cost" in str(res.structured_content)
        res = await client.call_tool("get_report", {})
        assert not res.is_error
