import json
from misroute.config import Config
from misroute.project import Project
from misroute.requests import generate, validate
from misroute.server.catalog import Tool
from misroute.trial import Request

SECRET = "zebracorn ledger"
TOOL = Tool(
    "Items_Get",
    f"Fetch one {SECRET} item.",
    {"type": "object", "properties": {"id": {"type": "string", "description": "Item number"}}},
    {"type": "object", "properties": {"name": {}, "price": {}}},
    route="GET /api/items/{id}",
    op_id="Items_Get",
)
OTHER = Tool("Items_Delete", f"Remove a {SECRET}.", route="DELETE /api/items/{id}", destructive=True)


class Recorder:
    def __init__(self, reply: str):
        self.reply, self.prompts = reply, []

    async def complete(self, prompt: str, kind: str | None = None, rep: int = 0) -> str:
        self.prompts.append(prompt)
        return self.reply


async def test_spec_prompt_never_shows_descriptions():
    rec = Recorder(json.dumps({"slots": ["a1"], "requests": ["what does {slot} cost"]}))
    reqs = await generate.generate(rec, TOOL, 2, 1, OTHER, source="spec", lang="no", domain="Kommunal drift")
    prompt = rec.prompts[0]
    assert SECRET not in prompt
    assert "GET /api/items/{id}" in prompt and "id (string; Item number)" in prompt and "price" in prompt
    assert "Norwegian" in prompt and "Domain: Kommunal drift" in prompt and "DELETE /api/items/{id}" in prompt
    assert reqs[0].lang == "no" and reqs[0].kind == "borrowed"

    await generate.generate(rec, TOOL, 2, 1)
    assert SECRET in rec.prompts[1] and "English" in rec.prompts[1]


async def test_spec_validator_never_shows_descriptions():
    rec = Recorder("Items_Get")
    req = Request("r", "Items_Get", "price of {slot}", ["a1"])
    assert await validate.pick(rec, req, [TOOL, OTHER], source="spec") == "Items_Get"
    assert SECRET not in rec.prompts[0] and "changes data" in rec.prompts[0]


def test_real_requests_by_operation_id_or_route(tmp_path):
    path = tmp_path / "real.jsonl"
    rows = [
        {"tool": "Items_Get", "text": "a"},
        {"tool": "GET /api/items/{id}", "text": "b", "lang": "no"},
        {"tool": "nope", "text": "c"},
    ]
    path.write_text("\n".join(map(json.dumps, rows)))
    found = generate.load_real(path, [TOOL, OTHER])
    assert [(r.tool, r.lang) for r in found] == [("Items_Get", "en"), ("Items_Get", "no")]


async def test_spec_requests_in_two_languages(tmp_path):
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/api/invoices/{id}": {"get": {"operationId": "Invoices_Get"}},
            "/api/invoices/{id}/lines": {"get": {"operationId": "Invoices_GetLines"}},
            "/api/customers/{id}": {"get": {"operationId": "Customers_Get"}},
        },
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    cfg = Config.model_validate(
        {
            "server": {"openapi": str(path)},
            "models": [{"name": "mock", "provider": "mock"}],
            "modes": ["json"],
            "request_source": "spec",
            "request_languages": ["en", "no"],
            "requests_per_tool": 3,
            "repeats": 1,
        }
    )
    project = Project(cfg, root=tmp_path / ".misroute")
    res = await project.discover()
    assert set(res["by_language"]) == {"en", "no"} and res["request_source"] == "spec"
    reqs = await project.request_set(await project.tools())
    assert {r.lang for rs in reqs.values() for r in rs} == {"en", "no"}
