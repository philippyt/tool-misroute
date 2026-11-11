import json
import shutil
import pytest
import yaml
from typer.testing import CliRunner
from misroute import compare
from misroute.cli import app
from misroute.config import Config
from misroute.improve import export
from misroute.models.base import Call, Turn
from misroute.modes import json_mode
from misroute.project import Project
from misroute.server import merge, openapi
from misroute.trial import Trial


def listing(what: str, parent: str, key: str) -> dict:
    return {
        "get": {
            "operationId": f"Files_For{parent.title()}",
            "summary": f"Attachments stored on one {what}.",
            "parameters": [{"name": key, "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"content": {"application/json": {"schema": {"type": "array"}}}}},
        }
    }


def query(name: str, what: str) -> dict:
    body = {"type": "object", "properties": {"mine": {"type": "boolean"}, "area": {"type": "integer"}}}
    return {
        "post": {
            "operationId": f"Orders_{name}",
            "summary": what,
            "requestBody": {"content": {"application/json": {"schema": body}}},
            "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
        }
    }


def bare(name: str, what: str) -> dict:
    ok = {"200": {"content": {"application/json": {"schema": {"type": "array"}}}}}
    return {"get": {"operationId": f"{name}_GetAll", "summary": what, "responses": ok}}


CREATE = {
    "post": {
        "operationId": "Orders_Create",
        "summary": "Create a planned work order.",
        "requestBody": {
            "content": {"application/json": {"schema": {"type": "object", "properties": {"text": {"type": "string"}}}}}
        },
    }
}
EXPORT = {
    "get": {
        "operationId": "Export_Download",
        "summary": "Download everything as a spreadsheet file.",
        "responses": {"200": {"content": {"application/octet-stream": {"schema": {"type": "string"}}}}},
    }
}
SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Works", "version": "1"},
    "paths": {
        "/api/orders/{orderId}/files": listing("work order", "orders", "orderId"),
        "/api/incidents/{incidentId}/files": listing("reported incident", "incidents", "incidentId"),
        "/api/sites/{siteId}/files": listing("inspection site", "sites", "siteId"),
        "/api/orders": CREATE,
        "/api/orders/search": query("Search", "Find work orders that match a filter, one page at a time."),
        "/api/orders/count": query("Count", "Count how many work orders match a filter."),
        "/api/statuses": bare("Statuses", "Every status a work order can have."),
        "/api/gradings": bare("Gradings", "Every severity grading for incidents."),
        "/api/areas": bare("Areas", "Every maintenance area."),
        "/api/export": EXPORT,
    },
}


@pytest.fixture
def spec(tmp_path):
    path = tmp_path / "works.json"
    path.write_text(json.dumps(SPEC), encoding="utf-8")
    return path


def planned(tmp_path, spec, **more) -> tuple[Project, dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = merge.propose(openapi.tools(spec), openapi.load(spec))
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    cfg = Config.model_validate(
        {
            "server": {"openapi": str(spec), "catalog": str(path)},
            "models": [{"name": "mock", "provider": "mock", "options": more}],
            "modes": ["json"],
            "rotations": 2,
            "repeats": 1,
        }
    )
    return Project(cfg, root=tmp_path / ".misroute"), plan


def test_a_proposed_plan_merges_reads_and_keeps_every_operation_reachable(spec):
    source = openapi.tools(spec)
    plan = merge.propose(source, openapi.load(spec))
    assert set(plan["tools"]) == {"files_by_parent", "orders_view", "reference_data"}
    assert plan["tools"]["files_by_parent"]["operations"]["orders"] == {
        "operation": "Files_ForOrders",
        "args": {"id": "orderId"},
    }
    assert plan["tools"]["orders_view"]["operations"] == {"search": "Orders_Search", "count": "Orders_Count"}
    assert list(plan["exclude"]) == ["Export_Download"]
    final = merge.apply(source, plan)
    assert [t.name for t in final] == ["files_by_parent", "Orders_Create", "orders_view", "reference_data"]
    files = final[0]
    assert files.selector == "parent" and files.required == ["parent", "id"] and not files.is_write
    assert files.params["parent"]["enum"] == ["orders", "incidents", "sites"] and "orderId" not in files.params
    assert "incidents: Attachments stored on one reported incident." in files.params["parent"]["description"]
    cover = merge.coverage(source, plan)
    assert cover["operations"] == 10 and cover["tools"] == 4 and not cover["unreachable"]
    assert cover["merged"]["reference_data"] == {
        "statuses": "Statuses_GetAll",
        "gradings": "Gradings_GetAll",
        "areas": "Areas_GetAll",
    }


def test_a_plan_that_loses_or_hides_an_operation_is_refused(spec):
    source = openapi.tools(spec)
    twice = {
        "tools": {
            "a": {"operations": {"x": "Orders_Search", "y": "Orders_Count"}},
            "b": {"operations": {"x": "Orders_Search", "y": "Statuses_GetAll"}},
        }
    }
    cases = {
        "mixes reads and writes": {
            "tools": {"orders": {"operations": {"make": "Orders_Create", "find": "Orders_Search"}}}
        },
        "is in both": twice,
        "not an operation": {"exclude": {"Nope": "gone"}},
        "fewer than two": {"tools": {"one": {"operations": {"x": "Orders_Count"}}}},
        "also excluded": {
            "tools": {"v": {"operations": {"x": "Orders_Search", "y": "Orders_Count"}}},
            "exclude": {"Orders_Count": "no"},
        },
    }
    for why, plan in cases.items():
        with pytest.raises(ValueError, match=why):
            merge.apply(source, plan)


class Picks:
    def __init__(self, name, args):
        self.name, self.args = name, args

    async def chat(self, user, tools, system=None, rep=0):
        return Turn([Call(self.name, self.args, "c")])


async def test_a_merged_tool_is_right_only_with_the_right_selector_value(spec):
    source = openapi.tools(spec)
    final = merge.apply(source, merge.propose(source, openapi.load(spec)))

    async def run(args):
        trial = Trial("m", "json", "files_by_parent", "r", "files on incident 4", operation="incidents")
        return await json_mode.run(Picks("files_by_parent", args), trial, final)

    right = await run({"parent": "incidents", "id": 4})
    assert right.operation_picked == "incidents" and not right.error and not right.wrong_operation
    other = await run({"parent": "orders", "id": 4})
    assert other.wrong_operation and other.error and not other.wrong and other.chosen == "files_by_parent"
    invented = await run({"parent": "tickets", "id": 4})
    assert invented.wrong_operation and invented.bad_args
    missing = await run({"id": 4})
    assert missing.wrong_operation and missing.operation_picked is None


async def test_requests_are_written_per_operation_and_shared_by_both_designs(tmp_path, spec):
    proj, _ = planned(tmp_path, spec)
    merged = await proj.tools()
    reqs = await proj.request_set(merged)
    files = reqs["files_by_parent"]
    assert {r.operation for r in files} == {"orders", "incidents", "sites"}
    assert all(r.tool == "files_by_parent" for r in files) and all(not r.operation for r in reqs["Orders_Create"])
    calls = proj.adapter("mock").calls
    mirror = merge.apply(await proj.source_tools(), {"exclude": (proj.plan() or {})["exclude"]})
    again = await proj.request_set(mirror)
    assert proj.adapter("mock").calls == calls
    assert {r.id for r in again["Files_ForIncidents"]} == {r.id for r in files if r.operation == "incidents"}


async def test_designs_are_compared_on_the_same_requests(tmp_path, spec):
    proj, _ = planned(tmp_path, spec)
    res = await compare.designs(proj, ["mock"])
    cell = res["cells"][0]
    assert res["tools"] == {"mirror": 9, "merged": 4} and res["coverage"]["excluded"]
    assert cell["rates"]["merged"]["trials"] == cell["rates"]["mirror"]["trials"] > 0
    assert {t["tool"] for t in res["merged_tools"]} == {"files_by_parent", "orders_view", "reference_data"}
    worse, _ = planned(tmp_path / "w", spec, selector_miss=1.0)
    bad = await compare.designs(worse, ["mock"])
    assert bad["cells"][0]["rates"]["merged"]["wrong_operation_rate"] > cell["rates"]["merged"]["wrong_operation_rate"]
    assert sum(t["wrong_operation"] for t in bad["merged_tools"]) > 0
    assert bad["cells"][0]["rates"]["merged"]["accuracy"] < cell["rates"]["merged"]["accuracy"]


def test_compare_design_writes_a_proposal_then_tests_it(tmp_path, spec, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = {"server": {"openapi": str(spec)}, "models": ["mock:m"], "modes": ["json"], "rotations": 1, "repeats": 1}
    (tmp_path / "misroute.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    runner = CliRunner()
    res = runner.invoke(app, ["compare", "--design", "catalog.yaml", "--json", "--yes"])
    data = json.loads(res.stdout)
    assert data["proposed"] and data["verdict"] in ("better", "worse", "no_difference", "insufficient_data")
    assert set(yaml.safe_load((tmp_path / "catalog.yaml").read_text())["tools"]) == set(data["coverage"]["merged"])
    again = json.loads(runner.invoke(app, ["compare", "--design", "catalog.yaml", "--json", "--yes"]).stdout)
    assert not again["proposed"] and again["cost_usd"] == 0
    assert runner.invoke(app, ["compare", "--json"]).exit_code == 3


async def test_the_merged_catalog_is_exported_with_what_the_server_needs(tmp_path, spec):
    proj, plan = planned(tmp_path, spec)
    source, final, doc = await proj.source_tools(), await proj.tools(), openapi.load(spec)
    table = merge.calls(source, final, plan, doc)
    sites = table["files_by_parent"]["operations"]["sites"]
    assert sites["method"] == "GET" and sites["path"] == "/api/sites/{siteId}/files"
    assert sites["params"] == [{"name": "siteId", "in": "path", "arg": "id"}] and sites["body"] is None
    search = table["orders_view"]["operations"]["search"]
    assert search["body"] == "fields" and {f["name"] for f in search["fields"]} == {"mine", "area"}
    entries = [
        {"tool": t.name, "op_id": t.op_id, "route": t.route, "description": t.description, "original": "o"}
        | {"status": "kept", "reason": "", "read_only": not t.is_write, "destructive": False}
        for t in final
    ]
    files = export.write(tmp_path / "out", entries, str(spec), doc, True, table)
    text = (tmp_path / "out" / "server.py").read_text(encoding="utf-8")
    compile(text, "server.py", "exec")
    assert "class Merged(Tool)" in text and files["merged"].endswith("merged.json")
    export.write(tmp_path / "out", entries, str(spec), doc, True)
    assert (tmp_path / "out" / "merged.json").exists()


@pytest.mark.fastmcp
@pytest.mark.skipif(not shutil.which("uv"), reason="needs uv")
async def test_generated_server_serves_the_merged_catalog(tmp_path, spec):
    from misroute.improve.run import improve

    proj, _ = planned(tmp_path, spec)
    res = await improve(proj, out=tmp_path / "out", fastmcp=True)
    served = res["fastmcp"]
    assert served["ok"], served
    assert served["tools"] == 4 and not served["arguments_differ"] and res["catalog_plan"]["tools"] == 4
