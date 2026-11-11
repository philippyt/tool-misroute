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
    assert by["Items_Get"].description == "Executes GET /api/items/{id}" and by["Items_Get"].required == ["id"]
    assert by["Items_Get"].route == "GET /api/items/{id}" and by["Items_Get"].op_id == "Items_Get"
    assert by["Items_Create"].is_write and by["Items_Create"].params["name"]
    assert not by["Items_GetAsGeoJson"].is_write


def test_openapi_names_match_fastmcp():
    from misroute.server.openapi import args, name_of, repair

    assert name_of("post", "/x/y-z/{id}", {}) == "POST_xy_zid"
    assert name_of("get", "/a", {"operationId": "Weird op.name-with Dashes!"}) == "Weird_op_name_with_Dashes"
    assert len(name_of("get", "/a", {"operationId": "A" * 80})) == 56
    body = {"content": {"application/json": {"schema": {"type": "object", "properties": {"id": {}, "a": {}}}}}}
    op = {"parameters": [{"name": "id", "in": "path", "required": True}], "requestBody": body}
    assert args({}, [], op) == {
        "type": "object",
        "properties": {"id__path": {"description": ""}, "id": {}, "a": {}},
        "required": ["id__path"],
    }
    doc = {"components": {"securitySchemes": {"aad": {"flows": {"implicit": {"authorizationUrl": "x"}}}}}}
    assert repair(doc) and doc["components"]["securitySchemes"]["aad"]["flows"]["implicit"]["scopes"] == {}


SWAGGER2 = {
    "swagger": "2.0",
    "info": {"title": "Old", "version": "1"},
    "host": "api.example.test",
    "basePath": "/v1",
    "schemes": ["https"],
    "securityDefinitions": {
        "key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
        "login": {"type": "oauth2", "flow": "accessCode", "authorizationUrl": "https://a", "tokenUrl": "https://t"},
    },
    "parameters": {"Page": {"in": "query", "name": "page", "type": "integer", "minimum": 1}},
    "paths": {
        "/orders": {
            "parameters": [{"in": "header", "name": "X-Api-Key", "type": "string", "required": True}],
            "get": {"operationId": "Orders_List", "parameters": [{"$ref": "#/parameters/Page"}]},
            "post": {
                "operationId": "Orders_Create",
                "parameters": [
                    {"in": "body", "name": "order", "required": True, "schema": {"$ref": "#/definitions/Order"}}
                ],
                "responses": {"201": {"description": "made", "schema": {"$ref": "#/definitions/Order"}}},
            },
        },
        "/orders/{orderId}/files": {
            "post": {
                "operationId": "Orders_Upload",
                "consumes": ["multipart/form-data"],
                "parameters": [
                    {"in": "path", "name": "OrderId", "type": "integer", "required": True},
                    {"in": "formData", "name": "file", "type": "file", "required": True},
                    {"in": "header", "name": "X-Trace", "type": "string", "required": True},
                ],
                "produces": ["application/pdf"],
                "responses": {"200": {"description": "ok", "schema": {"type": "file"}}},
            }
        },
    },
    "definitions": {
        "Base": {"type": "object", "properties": {"id": {"type": "string"}}},
        "Order": {
            "allOf": [{"$ref": "#/definitions/Base"}],
            "properties": {"customer": {"type": "string", "enum": ["acme", "globex"]}},
            "required": ["customer"],
        },
    },
}


def test_swagger_2_becomes_a_usable_catalog(tmp_path):
    import json
    from misroute.improve import audit, export
    from misroute.server import openapi

    path = tmp_path / "old.json"
    path.write_text(json.dumps(SWAGGER2), encoding="utf-8")
    doc = openapi.load(path)
    assert doc["openapi"].startswith("3") and doc["servers"] == [{"url": "https://api.example.test/v1"}]
    assert "converted from Swagger 2.0 to OpenAPI 3.0" in doc[openapi.FIXES]
    assert any("OrderId renamed to orderId" in f for f in doc[openapi.FIXES])
    assert doc["components"]["securitySchemes"]["login"]["flows"]["authorizationCode"]["scopes"] == {}
    by = {t.name: t for t in openapi.tools(path)}
    made = by["Orders_Create"]
    assert set(made.params) == {"id", "customer"} and made.required == ["customer"]
    assert made.params["customer"]["enum"] == ["acme", "globex"]
    assert set(made.output_schema["properties"]) == {"id", "customer"} and made.is_write
    assert by["Orders_List"].params["page"]["type"] == "integer" and "X-Api-Key" not in by["Orders_List"].params
    assert set(by["Orders_Upload"].params) == {"orderId", "X-Trace", "file"}
    notes = {(n.tool, n.kind) for n in audit.notes(list(by.values()), doc)}
    assert {("Orders_Upload", "upload"), ("Orders_Upload", "binary"), ("Orders_Upload", "header")} <= notes
    entries = [
        {"tool": t.name, "op_id": t.op_id, "route": t.route, "description": "d", "original": "o", "status": "kept"}
        | {"reason": "", "read_only": not t.is_write, "destructive": False}
        for t in by.values()
    ]
    files = export.write(tmp_path / "out", entries, str(path), doc, fastmcp=True)
    served = json.loads((tmp_path / "out" / "openapi.json").read_text(encoding="utf-8"))
    assert served["openapi"].startswith("3") and openapi.FIXES not in served
    assert "openapi.json" in (tmp_path / "out" / "server.py").read_text(encoding="utf-8") and files["spec"]


def test_a_clean_openapi_3_file_is_served_as_it_is(tmp_path):
    import json
    from misroute.server import openapi

    ok = {"200": {"description": "ok"}}
    spec = {"openapi": "3.0.0", "paths": {"/a": {"get": {"operationId": "A_Get", "responses": ok}}}}
    path = tmp_path / "spec.json"
    path.write_text("﻿" + json.dumps(spec), encoding="utf-8")
    assert openapi.load(path)[openapi.FIXES] == []
    spec["paths"]["/a"]["post"] = {"operationId": "A_Make", "responses": {"200": {}}}
    spec["paths"]["/a"]["put"] = {"operationId": "A_Set"}
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert openapi.load(path)[openapi.FIXES] == [
        "1 operations listed no responses, which converters require",
        "1 responses had no description, which converters require",
    ]
