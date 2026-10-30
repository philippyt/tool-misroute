import json
import sys
from importlib import resources
import jsonschema
import pytest
import yaml
from mcp import Client
from mcp.server.mcpserver import MCPServer
from typer.testing import CliRunner
from misroute import mcpconfig
from misroute.agent_server import create
from misroute.cli import app, target_server
from misroute.config import Config, ModelConfig
from misroute.explain import explain
from misroute.export import junit, sarif
from misroute.guard.rules import Rules
from misroute.guard.wrapper import Guard
from misroute.models.presets import expand
from misroute.project import Project
from misroute.report.html import render
from misroute.server.static import from_object
from misroute.store.cache import fill_price
from misroute.testing.routes import assert_routes, check_routes

runner = CliRunner()
TOOLS = [
    {
        "name": "get_case_comments",
        "description": "Written comments that caseworkers posted on a case.",
        "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}}},
    },
    {
        "type": "function",
        "function": {
            "name": "get_case_events",
            "description": "Timeline events of a case.",
            "parameters": {"type": "object", "properties": {"case_id": {"type": "string"}}},
        },
    },
    {
        "name": "get_customer",
        "description": "Customer contact details and address.",
        "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}},
    },
]
MOCK = {"name": "mock", "provider": "mock", "options": {"confuse": {"get_case_comments": {"get_case_events": 0.5}}}}


def schema_of(name):
    return json.loads(resources.files("misroute.schemas").joinpath(f"{name}.json").read_text(encoding="utf-8"))


def test_shorthand_and_prices(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9000/v1")
    assert expand("anthropic:claude-opus-5-5")["api_key_env"] == "ANTHROPIC_API_KEY"
    assert expand("vllm:Qwen/Qwen3-14B")["base_url"] == "http://localhost:9000/v1"
    with pytest.raises(ValueError):
        expand("nope:x")
    cfg = Config.model_validate({"server": {"url": "http://x"}, "models": ["ollama:qwen3.5:9b"], "writer": "mock:w"})
    assert [m.name for m in cfg.models] == ["qwen3.5-9b", "w"] and cfg.writer == "w"
    local = cfg.models[0]
    assert fill_price(local) and local.price_in == 0
    assert fill_price(ModelConfig(name="p", provider="anthropic", model="x", price_in=1, price_out=2))


def test_static_tools_and_objects(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({"tools": TOOLS}))
    proj = Project(Config.model_validate({"server": {"tools": str(path)}, "models": [MOCK]}), root=tmp_path)
    import asyncio

    tools = asyncio.run(proj.tools())
    assert [t.name for t in tools] == ["get_case_comments", "get_case_events", "get_customer"]
    assert tools[1].params == {"case_id": {"type": "string"}}

    def lookup_order(order_id: str, verbose: bool = False) -> dict:
        """Status of one order."""
        return {}

    class LangChainLike:
        def __init__(self):
            self.name, self.description, self.args_schema = "search_orders", "Search orders by text.", None
            self.parameters = {"type": "object", "properties": {"q": {"type": "string"}}}

    fn, obj = from_object(lookup_order), from_object(LangChainLike())
    assert fn.required == ["order_id"] and fn.description == "Status of one order."
    assert obj.params == {"q": {"type": "string"}}


def test_mcp_client_configs(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {"url": "https://x/mcp", "headers": {"Authorization": "Bearer abc", "X-Team": "a"}},
                    "local": {"command": "npx", "args": ["srv"], "env": {"API_KEY": "s"}},
                }
            }
        )
    )
    found = mcpconfig.found(str(path))
    assert [f[1] for f in found] == ["remote", "local"]
    remote, secrets = mcpconfig.to_server(found[0][3])
    assert remote["auth"] == "bearer_env:MISROUTE_MCP_TOKEN" and "Authorization" not in remote["headers"]
    assert secrets == {"MISROUTE_MCP_TOKEN": "abc"}
    local, secrets = mcpconfig.to_server(found[1][3])
    assert (
        local["command"] == ["npx", "srv"] and local["env"] == {"API_KEY": "${API_KEY}"} and secrets == {"API_KEY": "s"}
    )
    assert target_server("https://x/mcp")[0] == {"url": "https://x/mcp"}
    assert "tools" in target_server(str(write(tmp_path / "t.json", {"tools": TOOLS})))[0]
    assert "openapi" in target_server(str(write(tmp_path / "o.json", {"openapi": "3.0.0", "paths": {}})))[0]


def write(path, data):
    path.write_text(json.dumps(data))
    return path


def test_init_quick_doctor_explain_schema(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "tools.json", {"tools": TOOLS})
    settings = {"requests_per_tool": 3, "rotations": 2, "repeats": 1}
    config = {"server": {"tools": "tools.json"}, "models": [MOCK], **settings}
    (tmp_path / "misroute.yaml").write_text(yaml.safe_dump(config))
    res = runner.invoke(app, ["quick", "--no-open", "--json"])
    assert res.exit_code == 0, res.stdout
    data = json.loads(res.stdout)
    assert data["discover"]["spots"] and (tmp_path / ".misroute" / "report.html").exists()
    jsonschema.validate({"schema_version": 1, "command": "discover", **data["discover"]}, schema_of("discover"))
    res = runner.invoke(app, ["doctor", "--json"])
    doc = json.loads(res.stdout)
    jsonschema.validate(doc, schema_of("doctor"))
    assert res.exit_code == 0 and doc["ok"], doc
    res = runner.invoke(app, ["explain", "--pair", "get_case_comments,get_case_events", "--json"])
    ex = json.loads(res.stdout)
    jsonschema.validate(ex, schema_of("explain"))
    assert ex["directions"]["get_case_comments>get_case_events"]["wrong"] > 0
    lint = json.loads(runner.invoke(app, ["lint", "--json"]).stdout)
    jsonschema.validate(lint, schema_of("lint"))
    assert json.loads(runner.invoke(app, ["schema", "check"]).stdout)["title"] == "misroute check output"
    servers = tmp_path / "servers.json"
    write(
        servers, {"mcpServers": {"fake": {"command": sys.executable, "args": ["-m", "misroute.testing.fake_server"]}}}
    )
    res = runner.invoke(app, ["init", "--path", "other.yaml", "--from", str(servers), "--model", "mock:m"])
    assert res.exit_code == 0 and yaml.safe_load((tmp_path / "other.yaml").read_text())["server"]["command"][0]


def test_lint_strict_and_sarif(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "tools.json", {"tools": [{"name": "get_doc", "description": "GET /docs/{id}"}]})
    (tmp_path / "misroute.yaml").write_text(yaml.safe_dump({"server": {"tools": "tools.json"}, "models": ["mock:m"]}))
    res = runner.invoke(app, ["lint", "--strict", "--sarif", "out.sarif"])
    assert res.exit_code == 1
    doc = json.loads((tmp_path / "out.sarif").read_text())
    assert doc["runs"][0]["results"][0]["ruleId"] == "description-path-only"


def test_exports(tmp_path):
    sarif([("r", "warning", "m", "a.json")], tmp_path / "x.sarif")
    xml = junit("suite", [("a", "passed", ""), ("b", "failed", "worse"), ("c", "skipped", "few")], tmp_path / "x.xml")
    text = xml.read_text()
    assert 'tests="3" failures="1" skipped="1"' in text and "<failure" in text


async def test_explain_and_report_from_project(project, tmp_path):
    await project.discover(modes=["json"])
    ex = explain(project.store, "get_case_comments", "get_case_events")
    assert ex["directions"]["get_case_comments>get_case_events"]["examples"]
    log = tmp_path / "guard.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"type": "call", "tool": "update_case"},
                {"type": "event", "kind": "hedge", "tool": "update_case_meta", "other": "update_case"},
                {"type": "held", "tool": "update_case_meta"},
            ]
        )
    )
    html = render(project.store, tmp_path / "r.html", log).read_text(encoding="utf-8")
    assert "Production" in html and "1 writes held" in html


async def test_agent_jobs_and_suggest_fix(project):
    async with Client(create(project)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"start_job", "job_status", "cancel_job", "explain_pair", "suggest_fix"} <= names
        started = (
            await client.call_tool("start_job", {"kind": "discover", "max_cost": 5, "args": {"modes": ["json"]}})
        ).structured_content
        import asyncio

        for _ in range(200):
            st = (await client.call_tool("job_status", {"job": started["job"]})).structured_content
            if st["status"] != "running":
                break
            await asyncio.sleep(0.1)
        assert st["status"] == "done" and st["result"]["spots"]
        fix = await client.call_tool(
            "suggest_fix", {"tool_a": "get_case_comments", "tool_b": "get_case_events", "max_cost": 5}
        )
        assert fix.structured_content["results"][0]["edits"]["clause"]
        refused = await client.call_tool("start_job", {"kind": "fix", "max_cost": -1})
        assert "above max_cost" in str(refused.structured_content)


async def test_guard_decorator_and_middleware(tmp_path):
    rules = Rules(
        [["update_case", "update_case_meta"]],
        {"update_case", "update_case_meta"},
        Config.model_validate({"server": {"url": "http://x"}, "models": ["mock:m"]}).guard,
    )
    guard = Guard(None, rules, tmp_path / "g.jsonl")

    @guard.tool
    def update_case(case_id: str, status: str) -> str:
        return "ok"

    @guard.tool
    async def update_case_meta(case_id: str, tags: list) -> str:
        return "ok"

    assert update_case("1", status="closed") == "ok" and update_case.__name__ == "update_case"
    with pytest.raises(PermissionError, match="held for confirmation"):
        await update_case_meta("1", ["x"])

    server = MCPServer("s", middleware=[Guard(None, rules, tmp_path / "m.jsonl").middleware()])

    @server.tool()
    def update_case_a(case_id: str) -> str:
        return "ok"

    server.tool(name="update_case")(lambda case_id: "ok")
    server.tool(name="update_case_meta")(lambda case_id: "ok")
    async with Client(server) as client:
        assert not (await client.call_tool("update_case", {"case_id": "1"})).is_error
        res = await client.call_tool("update_case_meta", {"case_id": "1"})
        assert res.is_error and "held" in res.content[0].text


def test_assert_routes(tmp_path):
    res = check_routes(TOOLS, "mock:m", root=str(tmp_path))
    assert res["cells"][0]["accuracy"] > 0
    with pytest.raises(AssertionError, match="Most confused"):
        assert_routes(TOOLS, [MOCK], min_accuracy=1.0, root=str(tmp_path / "b"))
