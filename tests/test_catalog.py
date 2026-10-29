from misroute.lint.descriptions import classify
from misroute.modes.stubs import stub
from misroute.server.catalog import Tool


def test_write_detection():
    assert Tool("update_case").is_write
    assert Tool("createIssue").is_write
    assert not Tool("get_case").is_write
    assert not Tool("delete_thing", read_only=True).is_write
    assert Tool("fetch", destructive=True).is_write


def test_hash_changes_with_description():
    assert Tool("a", "x").hash != Tool("a", "y").hash


def test_lint():
    assert classify(Tool("get_doc")) == "missing"
    assert classify(Tool("get_doc", "GET /api/docs/{id}")) == "path only"
    assert classify(Tool("get_doc", "get doc")) == "name only"
    assert classify(Tool("get_doc", "Returns the stored document with its metadata.")) is None


def test_stub():
    t = Tool(
        "find", "Find things.", {"properties": {"q": {"type": "string"}, "n": {"type": "integer"}}, "required": ["q"]}
    )
    assert stub(t).startswith("async def find(q: str, n: int | None = None) -> dict:")


async def test_undescribed_tools_use_drafted_baseline(project):
    pair = [Tool("get_attachment", ""), Tool("get_document", "GET /docs/{id}")]
    drafts = await project.drafts(pair, pair)
    assert set(drafts) == {"get_attachment", "get_document"}
    from misroute.fix.clause import write

    edits = await write(project.role("writer"), *pair, drafts)
    assert edits["baseline_kind"] == "description" and edits["baseline"] == drafts
    assert all(edits["clause"][k].startswith(drafts[k].rstrip(".")) for k in drafts)


def test_tool_filter():
    from misroute.server.connect import keep

    assert keep("list_issues", ["*issue*"], [])
    assert not keep("list_commits", ["*issue*"], [])
    assert not keep("delete_issue", ["*issue*"], ["delete_*"])


def test_openapi_tools(tmp_path):
    import json
    from misroute.server.openapi import tools

    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/api/items/{id}": {
                "get": {
                    "operationId": "Items_Get",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                }
            },
            "/api/items": {
                "post": {
                    "operationId": "Items_Create",
                    "summary": "Create an item.",
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}},
                }
            },
            "/api/items/geo": {"post": {"operationId": "Items_GetAsGeoJson"}},
        },
        "components": {"schemas": {"Item": {"type": "object", "properties": {"name": {"type": "string"}}}}},
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    by = {t.name: t for t in tools(path)}
    assert by["Items_Get"].description == "GET /api/items/{id}" and by["Items_Get"].required == ["id"]
    assert by["Items_Create"].is_write and by["Items_Create"].params["body"]["properties"]["name"]
    assert not by["Items_GetAsGeoJson"].is_write
