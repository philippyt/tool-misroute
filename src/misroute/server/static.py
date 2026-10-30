import inspect
import json
from pathlib import Path
from typing import Any, get_type_hints
from misroute.server.catalog import Tool

PY = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def from_dict(d: dict[str, Any]) -> Tool:
    """One tool in MCP, OpenAI or Anthropic format."""
    if d.get("type") == "function" and "function" in d:
        d = d["function"]
    schema = d.get("inputSchema") or d.get("input_schema") or d.get("parameters") or {"type": "object"}
    ann = d.get("annotations") or {}
    return Tool(
        name=d["name"],
        description=d.get("description") or "",
        input_schema=dict(schema),
        output_schema=d.get("outputSchema") or d.get("output_schema"),
        read_only=ann.get("readOnlyHint"),
        destructive=ann.get("destructiveHint"),
    )


def from_function(fn: Any) -> Tool:
    hints = get_type_hints(fn)
    props, required = {}, []
    for name, p in inspect.signature(fn).parameters.items():
        if name in ("self", "ctx", "context"):
            continue
        props[name] = {"type": PY.get(hints.get(name, str), "string")}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    schema = {"type": "object", "properties": props, "required": required}
    return Tool(fn.__name__, inspect.getdoc(fn) or "", schema)


def from_object(obj: Any) -> Tool:
    """A dict, a plain function, or a framework tool object with name and description attributes."""
    if isinstance(obj, Tool):
        return obj
    if isinstance(obj, dict):
        return from_dict(obj)
    name = getattr(obj, "name", None)
    if name and hasattr(obj, "description"):
        schema = None
        for attr in ("params_json_schema", "input_schema", "inputSchema", "parameters"):
            if isinstance(getattr(obj, attr, None), dict):
                schema = getattr(obj, attr)
                break
        args = getattr(obj, "args_schema", None)
        if schema is None and hasattr(args, "model_json_schema"):
            schema = args.model_json_schema()
        return Tool(str(name), str(obj.description or ""), dict(schema or {"type": "object"}))
    if callable(obj):
        return from_function(obj)
    raise TypeError(f"cannot read a tool from {type(obj).__name__}")


def tools(path: str | Path) -> list[Tool]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("tools", data) if isinstance(data, dict) else data
    return [from_dict(r) for r in rows]
