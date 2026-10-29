import pytest
from misroute.detectors.codemode import classify
from misroute.sandbox.fake_tools import FakeTools
from misroute.sandbox.monty_runner import Sandbox
from misroute.server.catalog import Tool

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
