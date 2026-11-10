import pytest
from misroute.detectors.codemode import classify
from misroute.models.base import Call, Turn
from misroute.modes import code_mode
from misroute.sandbox.fake_tools import FakeTools
from misroute.sandbox.monty_runner import Sandbox
from misroute.server.catalog import Tool
from misroute.trial import Trial

TOOLS = [
    Tool("get_product", "Product price.", {"properties": {"id": {"type": "string"}}, "required": ["id"]}),
    Tool("list_lines", "Cart lines, paged.", {"properties": {"cursor": {"type": "string"}}}),
]

CASES = {
    "r = await get_product(id='a')\nr": None,
    "async def get_product(id):\n    return {'price': 10.99}\nr = await get_product('a')\nr": "faked",
    "async def main():\n    return await get_product(id='a')\nmain": "no_call",
    "r = await get_price(id='a')": "unknown_function",
    "r = await get_product(id=": "syntax_error",
    "r = await list_lines()\nr['items']": "unused_pagination",
    "c = None\nout = []\nwhile True:\n    r = await list_lines(cursor=c)\n    out += r['items']\n"
    "    c = r['next_cursor']\n    if not c:\n        break\nlen(out)": None,
}


@pytest.mark.parametrize(("code", "expected"), list(CASES.items()))
async def test_failures(code, expected):
    fakes = FakeTools(TOOLS)
    async with Sandbox(max_processes=1) as box:
        res = await box.run(code, fakes.functions())
    assert classify(code, res, fakes.log, TOOLS) == expected


async def test_pasted_stub_still_measures_choice():
    class Pasted:
        async def chat(self, user, tools, system=None, rep=0):
            code = "async def get_product(id: str) -> dict:\n    '''Product price.'''\n    ...\n"
            code += "r = await get_product('a')\nr"
            return Turn([Call("run_code", {"code": code})])

    async with Sandbox(max_processes=1) as box:
        t = await code_mode.run(Pasted(), box, Trial("m", "code", "get_product", "r", "x"), TOOLS)
    assert t.failure == "stub_copied" and t.calls == [] and t.rerun == ["get_product"]


async def test_odd_cursor_is_logged():
    fakes = FakeTools(TOOLS)
    async with Sandbox(max_processes=1) as box:
        res = await box.run("r = await list_lines(cursor={'page': 2})\nr", fakes.functions())
    assert res.error is None and fakes.names() == ["list_lines"] and fakes.log[0].cursor == "invented"


async def test_cursor_checks():
    loop = "c = None\nwhile True:\n    r = await list_lines(cursor=c)\n"
    loop += "    c = r['next_cursor']\n    if not c:\n        break"
    cases = {
        loop: None,
        "r = await list_lines(cursor='p2')": "invented_cursor",
        "a = await list_lines()\nb = await list_lines(cursor=a['next_cursor'])\n"
        "c = await list_lines(cursor=a['next_cursor'])\nd = await list_lines(cursor=c['next_cursor'])": "reused_cursor",
    }
    for code, expected in cases.items():
        fakes = FakeTools(TOOLS)
        async with Sandbox(max_processes=1) as box:
            res = await box.run(code, fakes.functions())
        assert classify(code, res, fakes.log, TOOLS) == expected, code


READ = Tool(
    "azure_read",
    "Read an Azure resource.",
    {
        "properties": {
            "operation": {"type": "string", "enum": ["app_logs", "deployments"]},
            "page": {"type": "integer"},
            "limit": {"type": "integer", "maximum": 100},
            "after": {"type": "string", "format": "date-time"},
        },
        "required": ["operation"],
    },
)


async def test_a_page_number_is_not_an_invented_cursor():
    cases = {
        "r = await azure_read(operation='app_logs', page=1)\nr": None,
        "r = await azure_read(operation='app_logs', after='2026-11-01T00:00:00Z')\nr": "unused_pagination",
        "r = await azure_read(operation='app_logs', limit=50)\nr": None,
        "r = await azure_read(operation='app_logs')\nr": "unused_pagination",
        "a = await azure_read(operation='app_logs')\nb = await azure_read(operation='app_logs', page=2)\n"
        "c = await azure_read(operation='app_logs', page=3)": None,
    }
    for code, expected in cases.items():
        fakes = FakeTools([READ])
        async with Sandbox(max_processes=1) as box:
            res = await box.run(code, fakes.functions())
        assert res.error is None and classify(code, res, fakes.log, [READ]) == expected, code
        assert all(r.cursor is None for r in fakes.log)
    fakes = FakeTools([READ])
    assert fakes.call(READ, {"operation": "app_logs", "page": 1})["items"][0]["id"] == "azure_read_1"
    assert fakes.call(READ, {"operation": "app_logs", "page": 3})["has_more"] is False


def test_arguments_are_checked_against_the_schema():
    from misroute.sandbox.fake_tools import arg_errors

    assert arg_errors(READ, {"operation": "app_logs", "limit": 100}) == []
    found = arg_errors(READ, {"operation": "console_logs", "limit": 200, "tail": True})
    assert len(found) == 3 and any("console_logs" in e for e in found) and any("200" in e for e in found)
    assert any("no parameter tail" in e for e in found)
    assert arg_errors(READ, {}) == ["azure_read: 'operation' is a required property"]
    assert arg_errors(Tool("bare"), {"x": 1}) == []


def test_fixtures_answer_before_the_generated_fake(tmp_path):
    from misroute.sandbox.fake_tools import load_fixtures

    path = tmp_path / "fixtures.yaml"
    path.write_text(
        "admin_api_catalog:\n  routes: [{method: POST, path: /api/repositories}]\n"
        "azure_read:\n  - args: {operation: deployments}\n    result: {deployments: [gpt]}\n"
        "  - result: {lines: [started]}\n",
        encoding="utf-8",
    )
    catalog = Tool("admin_api_catalog", "Routes of the admin API.", output_schema={"type": "object"})
    fixed = FakeTools([catalog, READ], load_fixtures(path))
    assert fixed.call(catalog, {})["routes"][0]["path"] == "/api/repositories"
    assert fixed.call(READ, {"operation": "deployments"}) == {"deployments": ["gpt"]}
    assert fixed.call(READ, {"operation": "app_logs"}) == {"lines": ["started"]}
    assert FakeTools([catalog]).call(catalog, {})["ok"] is True
