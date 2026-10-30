import hashlib
import json
import re
from pathlib import Path
from misroute.models.base import parse_json
from misroute.server.catalog import Tool
from misroute.trial import Request

GOAL = """Write requests that a user of an AI agent would send, where the right tool to answer is the one below.
Write each request from the user's goal. Do not reuse the tool's name or the distinctive words of its description.
Vary the wording and length. Write the requests in {language}.
Only ask for what this tool can actually return, judging by its description, parameters
and output. Do not ask for aggregates, statistics or data the tool does not hold.
Every request must contain the placeholder {{slot}} exactly once, standing for one concrete value such as a record id,
customer, repository or place, so the request is complete when it is filled in.
Also give {slots} realistic values for that placeholder.

Tool: {name}
Description: {description}
Parameters: {params}
Returns: {returns}
Count: {count}

Reply with JSON only: {{"slot_kind": "...", "slots": ["..."], "requests": ["..."]}}"""

BORROWED = """Write requests that a user of an AI agent would send, where the right tool is the first tool below.
Each request must borrow one or two words from the second tool's description while still clearly needing the first tool.
Write the requests in {language}.
Every request must contain the placeholder {{slot}} exactly once, standing for one concrete value, and give {slots}
realistic values for it.

Tool: {name}
Description: {description}
Sibling: {sibling_name}: {sibling}
Count: {count}

Reply with JSON only: {{"slot_kind": "...", "slots": ["..."], "requests": ["..."]}}"""


SPEC = """Write requests that a user of an AI agent would send, where the right tool to answer is the operation below.
You see the operation's interface, not its documentation. Work out from its name, route, parameters and returned
fields what a person would use it for, and write each request from that person's goal, in everyday words. Do not
reuse the operation name or the path. Vary the wording and length. Write the requests in {language}.
Only ask for what the returned fields can answer. Do not ask for aggregates or data the operation does not hold.
Every request must contain the placeholder {{slot}} exactly once, standing for one concrete value such as a record id,
person, address or place, so the request is complete when it is filled in.
Also give {slots} realistic values for that placeholder.
{domain}
Tool: {name}
{view}
Count: {count}

Reply with JSON only: {{"slot_kind": "...", "slots": ["..."], "requests": ["..."]}}"""

SPEC_BORROWED = """Write requests that a user of an AI agent would send, where the right tool is the first operation
below. You see interfaces, not documentation. Each request must borrow one or two words that fit the second
operation's interface while still clearly needing the first. Write the requests in {language}.
Every request must contain the placeholder {{slot}} exactly once, standing for one concrete value, and give {slots}
realistic values for it.
{domain}
Tool: {name}
{view}
Sibling: {sibling_name}: {sibling}
Count: {count}

Reply with JSON only: {{"slot_kind": "...", "slots": ["..."], "requests": ["..."]}}"""

LANGUAGES = {
    "en": "English",
    "no": "Norwegian bokmal",
    "nb": "Norwegian bokmal",
    "nn": "Norwegian nynorsk",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "nl": "Dutch",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
}


def language(code: str) -> str:
    return LANGUAGES.get(code.lower(), code)


def fields(schema: dict | None, limit: int = 20) -> list[str]:
    schema = schema or {}
    if schema.get("type") == "array" or "items" in schema:
        schema = schema.get("items") or {}
    for key in ("data", "items", "results", "value"):
        inner = (schema.get("properties") or {}).get(key)
        if isinstance(inner, dict) and (inner.get("type") == "array" or "items" in inner):
            schema = inner.get("items") or {}
            break
    return list(schema.get("properties") or {})[:limit]


def param(name: str, schema: dict) -> str:
    bits = [str(schema["type"])] if schema.get("type") else []
    if schema.get("enum"):
        bits.append("one of " + ", ".join(map(str, schema["enum"][:8])))
    if schema.get("description"):
        bits.append(str(schema["description"])[:80])
    return f"{name} ({'; '.join(bits)})" if bits else name


def view(tool: Tool) -> str:
    """What a tool exposes besides its description: route, read or write, parameters and returned fields."""
    lines = [f"Route: {tool.route}"] if tool.route else []
    lines.append(f"Effect: {'changes data' if tool.is_write else 'reads data'}")
    lines.append("Parameters: " + (", ".join(param(k, v) for k, v in tool.params.items()) or "none"))
    lines.append("Returns: " + (", ".join(fields(tool.output_schema)) or "not documented"))
    return "\n".join(lines)


def brief(tool: Tool) -> str:
    """One line spec view for tool listings."""
    parts = [tool.route] if tool.route else []
    parts.append("changes data" if tool.is_write else "reads data")
    if tool.params:
        parts.append("params " + ", ".join(list(tool.params)[:10]))
    if out := fields(tool.output_schema, 10):
        parts.append("returns " + ", ".join(out))
    return "; ".join(parts)


def lenient(text: str) -> dict:
    """The reply as JSON, or the complete strings of a reply cut off mid list, so one cut request does not cost
    the whole batch."""
    try:
        data = parse_json(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    out: dict = {}
    for key in ("slots", "requests"):
        m = re.search(rf'"{key}"\s*:\s*\[(.*?)(?:\]|$)', text, re.S)
        if m:
            out[key] = [json.loads(f'"{s}"') for s in re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))]
    if not out.get("requests"):
        raise ValueError("no readable requests in reply")
    return out


def rid(tool: str, kind: str, template: str, lang: str = "en") -> str:
    tag = "" if lang == "en" else f"|{lang}"
    return hashlib.sha256(f"{tool}|{kind}|{template}{tag}".encode()).hexdigest()[:10]


async def generate(
    adapter,
    tool: Tool,
    count: int,
    slots: int,
    sibling: Tool | None = None,
    attempt: int = 0,
    source: str = "description",
    lang: str = "en",
    domain: str = "",
) -> list[Request]:
    """Requests for a tool. With source spec the generator sees the tool's interface and never its description."""
    kind = "borrowed" if sibling else "goal"
    spec = source == "spec"
    tpl = (SPEC_BORROWED if sibling else SPEC) if spec else (BORROWED if sibling else GOAL)
    prompt = tpl.format(
        name=tool.name,
        description=tool.description or "none",
        params=list(tool.params),
        returns=json.dumps(tool.output_schema)[:1500] if tool.output_schema else "not documented",
        view=view(tool),
        domain=f"Domain: {domain.strip()}\n" if domain.strip() else "",
        language=language(lang),
        count=count,
        slots=slots,
        sibling_name=sibling.name if sibling else "",
        sibling=(brief(sibling) if spec else sibling.description) if sibling else "",
    )
    data = lenient(await adapter.complete(prompt, kind=kind, rep=attempt))
    values = [str(s) for s in data.get("slots", [])][:slots]
    out = []
    for text in data.get("requests", [])[:count]:
        text = str(text)
        if "{slot}" not in text:
            text = text + " ({slot})" if values else text
        sib = sibling.name if sibling else None
        out.append(Request(rid(tool.name, kind, text, lang), tool.name, text, values, kind, sib, lang))
    return out


def load_real(path: str | Path, tools: list[Tool]) -> list[Request]:
    """Real requests from JSON lines with text, tool and optional lang. The tool can be named by its tool name,
    operationId or METHOD /path."""
    names = {k: t.name for t in tools for k in (t.name, t.op_id, t.route) if k}
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        name = names.get(str(row.get("tool", "")).strip())
        if name:
            lang = row.get("lang", "en")
            out.append(Request(rid(name, "real", row["text"], lang), name, row["text"], [], "real", lang=lang))
    return out
