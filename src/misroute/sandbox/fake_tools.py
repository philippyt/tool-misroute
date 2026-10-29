import re
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


@dataclass
class FakeTools:
    """Recorded fakes standing in for the real server in code mode."""

    tools: list[Tool]
    log: list[Record] = field(default_factory=list)

    def result(self, tool: Tool, args: dict[str, Any]) -> Any:
        cp = cursor_param(tool)
        if cp:
            digits = re.search(r"p(\d+)$", str(args.get(cp) or ""))
            page = int(digits.group(1)) if digits else 1
            items = [{"id": f"{tool.name}_{page}_{i}", "value": i} for i in range(3)]
            return {"items": items, "next_cursor": f"p{page + 1}" if page < PAGES else None}
        if tool.output_schema:
            return example(tool.output_schema)
        return {"id": f"{tool.name}_1", "tool": tool.name, "ok": True}

    def functions(self) -> dict[str, Any]:
        def make(tool: Tool):
            async def call(*args: Any, **kwargs: Any) -> Any:
                if args:
                    kwargs = {**dict(zip(tool.params, args, strict=False)), **kwargs}
                out = self.result(tool, kwargs)
                cp = cursor_param(tool)
                self.log.append(
                    Record(tool.name, kwargs, paged=bool(cp and isinstance(out, dict) and out.get("next_cursor")))
                )
                return out

            return call

        return {t.name: make(t) for t in self.tools}

    def names(self) -> list[str]:
        return [r.tool for r in self.log]
