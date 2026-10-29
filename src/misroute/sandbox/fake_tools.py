import hashlib
from dataclasses import dataclass, field
from typing import Any
from misroute.server.catalog import Tool

CURSOR_PARAMS = ("cursor", "page", "page_token", "after", "next_cursor", "offset", "starting_after")
PAGES = 3


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


def cursor_param(tool: Tool) -> str | None:
    return next((p for p in tool.params if p.lower() in CURSOR_PARAMS), None)


def token(tool: str, page: int) -> str:
    return "mr" + hashlib.sha256(f"{tool}|{page}".encode()).hexdigest()[:10]


@dataclass
class FakeTools:
    """Recorded fakes standing in for the real server in code mode. Pages use cursors the fake issued itself."""

    tools: list[Tool]
    log: list[Record] = field(default_factory=list)
    issued: dict[str, tuple[str, int]] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)

    def page(self, tool: Tool, value: Any) -> tuple[int | None, str | None]:
        if value in (None, ""):
            return 1, None
        key = str(value)
        if self.issued.get(key, ("", 0))[0] != tool.name:
            return None, "invented"
        status = "reused" if key in self.used else "issued"
        self.used.add(key)
        return self.issued[key][1], status

    def result(self, tool: Tool, args: dict[str, Any]) -> tuple[Any, str | None]:
        cp = cursor_param(tool)
        if cp:
            page, status = self.page(tool, args.get(cp))
            if page is None:
                return {"items": [], "next_cursor": None, "error": "unknown cursor"}, status
            nxt = token(tool.name, page + 1) if page < PAGES else None
            if nxt:
                self.issued[nxt] = (tool.name, page + 1)
            items = [{"id": f"{tool.name}_{page}_{i}", "value": i} for i in range(3)]
            return {"items": items, "next_cursor": nxt}, status
        if tool.output_schema:
            return example(tool.output_schema), None
        return {"id": f"{tool.name}_1", "tool": tool.name, "ok": True}, None

    def functions(self) -> dict[str, Any]:
        def make(tool: Tool):
            async def call(*args: Any, **kwargs: Any) -> Any:
                if args:
                    kwargs = {**dict(zip(tool.params, args, strict=False)), **kwargs}
                out, status = self.result(tool, kwargs)
                paged = isinstance(out, dict) and bool(out.get("next_cursor"))
                self.log.append(Record(tool.name, kwargs, paged, status))
                return out

            return call

        return {t.name: make(t) for t in self.tools}

    def names(self) -> list[str]:
        return [r.tool for r in self.log]
