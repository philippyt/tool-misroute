import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from misroute.server.catalog import Tool

CURSOR_PARAMS = ("cursor", "page_token", "after", "next_cursor", "starting_after", "continuation_token")
NUMBER_PARAMS = ("page", "page_number", "offset", "skip")
OFFSET_PARAMS = ("offset", "skip")
SIZE_PARAMS = ("limit", "page_size", "per_page", "size", "top", "max_results")
PAGE_SIZE = 3
PAGES = 3
TOTAL = PAGE_SIZE * PAGES


@dataclass
class Record:
    tool: str
    args: dict[str, Any]
    paged: bool = False
    cursor: str | None = None


def example(schema: dict[str, Any] | None, depth: int = 0) -> Any:
    if not schema or depth > 4:
        return "value"
    if schema.get("enum"):
        return schema["enum"][0]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "object" or "properties" in schema:
        return {k: example(v, depth + 1) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        return [example(schema.get("items"), depth + 1)]
    return {"integer": 1, "number": 1.0, "boolean": True, "null": None}.get(t or "string", "value")


def types(schema: Any) -> set[str]:
    t = schema.get("type") if isinstance(schema, dict) else None
    return set(t) if isinstance(t, list) else {t} if t else set()


def paging(tool: Tool) -> tuple[str, str] | None:
    """The parameter that pages a tool and how: a page number, an offset or an opaque cursor. A numeric page or
    offset is the caller's to choose, only a cursor has to come from an earlier reply. A date or a number named
    after is a filter, not a cursor."""
    for name, schema in tool.params.items():
        low, t = name.lower().removeprefix("x-").replace("-", "_"), types(schema)
        if low in NUMBER_PARAMS and "string" not in t:
            return name, "offset" if low in OFFSET_PARAMS else "page"
        dated = isinstance(schema, dict) and schema.get("format") in ("date", "date-time")
        if low in CURSOR_PARAMS and not t & {"integer", "number"} and not dated:
            return name, "cursor"
    return None


def cursor_param(tool: Tool) -> str | None:
    found = paging(tool)
    return found[0] if found else None


def page_size(tool: Tool, args: dict[str, Any]) -> int:
    for name in tool.params:
        value = args.get(name)
        if name.lower() in SIZE_PARAMS and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return PAGE_SIZE


def token(tool: str, start: int) -> str:
    return "mr" + hashlib.sha256(f"{tool}|{start}".encode()).hexdigest()[:10]


def arg_errors(tool: Tool, args: dict[str, Any]) -> list[str]:
    """Ways a call's arguments break the tool's input schema: a missing required value, a value outside an enum
    or a limit, a wrong type, or a parameter the tool does not have."""
    schema = tool.input_schema
    if not schema:
        return []
    out = [f"{tool.name}: no parameter {k}" for k in args if tool.params and k not in tool.params]
    try:
        found = sorted(Draft202012Validator(schema).iter_errors(args), key=lambda e: list(e.path))
    except SchemaError:
        return out
    for e in found:
        where = ".".join(str(p) for p in e.path)
        out.append(f"{tool.name}{'.' + where if where else ''}: {e.message}"[:200])
    return list(dict.fromkeys(out))


def load_fixtures(path: str | Path | None) -> dict[str, Any]:
    """Recorded results per tool name from a JSON or YAML file. A value is the result itself, or a list of cases
    {args, result} where the first case whose args all match the call answers it and a case without args answers
    the rest."""
    if not path:
        return {}
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text) if str(path).lower().endswith(".json") else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"fixtures file {path} must map tool names to results")
    return data


def is_cases(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(c, dict) and "result" in c for c in value)


@dataclass
class FakeTools:
    """Recorded fakes standing in for the real server. A fixture answers when one matches the call, else pages
    come from a fixed list of items and everything else from the output schema."""

    tools: list[Tool]
    fixtures: dict[str, Any] = field(default_factory=dict)
    log: list[Record] = field(default_factory=list)
    issued: dict[str, tuple[str, int]] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)

    def fixture(self, tool: Tool, args: dict[str, Any]) -> tuple[bool, Any]:
        if tool.name not in self.fixtures:
            return False, None
        value = self.fixtures[tool.name]
        if not is_cases(value):
            return True, value
        for case in value:
            if all(args.get(k) == v for k, v in (case.get("args") or {}).items()):
                return True, case["result"]
        return False, None

    def start(self, tool: Tool, kind: str, value: Any, size: int) -> tuple[int | None, str | None]:
        if value in (None, ""):
            return 0, None
        if kind != "cursor":
            if isinstance(value, bool) or not str(value).lstrip("-").isdigit():
                return None, "invented"
            n = int(value)
            return (max(n, 0) if kind == "offset" else (max(n, 1) - 1) * size), None
        key = str(value)
        if self.issued.get(key, ("", 0))[0] != tool.name:
            return None, "invented"
        status = "reused" if key in self.used else "issued"
        self.used.add(key)
        return self.issued[key][1], status

    def result(self, tool: Tool, args: dict[str, Any]) -> tuple[Any, str | None]:
        hit, fixed = self.fixture(tool, args)
        if hit:
            return fixed, None
        found = paging(tool)
        if found:
            name, kind = found
            size = page_size(tool, args)
            start, status = self.start(tool, kind, args.get(name), size)
            if start is None:
                return {"items": [], "next_cursor": None, "error": f"unknown {kind}"}, status
            end = min(start + size, TOTAL)
            items = [{"id": f"{tool.name}_{i + 1}", "value": i} for i in range(start, end)]
            more = end < TOTAL
            if kind != "cursor":
                return {"items": items, "has_more": more, "total": TOTAL}, status
            nxt = token(tool.name, end) if more else None
            if nxt:
                self.issued[nxt] = (tool.name, end)
            return {"items": items, "next_cursor": nxt}, status
        made = example(tool.output_schema) if tool.output_schema else None
        if made not in (None, {}, [], "value"):
            return made, None
        return {"id": f"{tool.name}_1", "tool": tool.name, "ok": True}, None

    def call(self, tool: Tool, args: dict[str, Any]) -> Any:
        out, status = self.result(tool, args)
        paged = isinstance(out, dict) and bool(out.get("next_cursor") or out.get("has_more"))
        self.log.append(Record(tool.name, args, paged, status))
        return out

    def functions(self) -> dict[str, Any]:
        def make(tool: Tool):
            async def call(*args: Any, **kwargs: Any) -> Any:
                if args:
                    kwargs = {**dict(zip(tool.params, args, strict=False)), **kwargs}
                return self.call(tool, kwargs)

            return call

        return {t.name: make(t) for t in self.tools}

    def names(self) -> list[str]:
        return [r.tool for r in self.log]
