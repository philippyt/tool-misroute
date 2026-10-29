from typing import Any
from misroute.server.catalog import Tool

PY = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "array": "list", "object": "dict"}

GUIDE = (
    "Run a Python program in a sandbox. The functions below are async and must be called with await. "
    "Use loops and arithmetic in the program instead of calling run_code once per item. "
    "The last expression and anything printed is returned to you.\n\n"
)

SINGLE = (
    "Call only the one function that answers the request. Call another function only if the result "
    "of the first shows that it was the wrong one.\n\n"
)


def py_type(schema: dict[str, Any]) -> str:
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "object")
    return PY.get(t or "object", "object")


def stub(tool: Tool) -> str:
    required = set(tool.required)
    req = [f"{k}: {py_type(v)}" for k, v in tool.params.items() if k in required]
    opt = [f"{k}: {py_type(v)} | None = None" for k, v in tool.params.items() if k not in required]
    args = ", ".join(req + opt)
    doc = (tool.description or "").replace('"""', "'''").strip() or "No description."
    return f'async def {tool.name}({args}) -> dict:\n    """{doc}"""\n    ...\n'


def run_code_tool(tools: list[Tool], single_call: bool = False) -> dict[str, Any]:
    body = "\n".join(stub(t) for t in tools)
    return {
        "name": "run_code",
        "description": GUIDE + (SINGLE if single_call else "") + "```python\n" + body + "```",
        "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    }
