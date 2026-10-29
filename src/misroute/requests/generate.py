import hashlib
import json
from pathlib import Path
from misroute.models.base import parse_json
from misroute.server.catalog import Tool
from misroute.trial import Request

GOAL = """Write requests that a user of an AI agent would send, where the right tool to answer is the one below.
Write each request from the user's goal. Do not reuse the tool's name or the distinctive words of its description.
Vary the wording and length. Every request must contain the placeholder {{slot}} exactly once, standing for one
concrete value such as a record id, customer, repository or place, so the request is complete when it is filled in.
Also give {slots} realistic values for that placeholder.

Tool: {name}
Description: {description}
Parameters: {params}
Count: {count}

Reply with JSON only: {{"slot_kind": "...", "slots": ["..."], "requests": ["..."]}}"""

BORROWED = """Write requests that a user of an AI agent would send, where the right tool is the first tool below.
Each request must borrow one or two words from the second tool's description while still clearly needing the first tool.
Every request must contain the placeholder {{slot}} exactly once, standing for one concrete value, and give {slots}
realistic values for it.

Tool: {name}
Description: {description}
Sibling: {sibling_name}: {sibling}
Count: {count}

Reply with JSON only: {{"slot_kind": "...", "slots": ["..."], "requests": ["..."]}}"""


def rid(tool: str, kind: str, template: str) -> str:
    return hashlib.sha256(f"{tool}|{kind}|{template}".encode()).hexdigest()[:10]


async def generate(
    adapter, tool: Tool, count: int, slots: int, sibling: Tool | None = None, attempt: int = 0
) -> list[Request]:
    kind = "borrowed" if sibling else "goal"
    tpl = BORROWED if sibling else GOAL
    prompt = tpl.format(
        name=tool.name,
        description=tool.description or "none",
        params=list(tool.params),
        count=count,
        slots=slots,
        sibling_name=sibling.name if sibling else "",
        sibling=sibling.description if sibling else "",
    )
    data = parse_json(await adapter.complete(prompt, kind=kind, rep=attempt))
    values = [str(s) for s in data.get("slots", [])][:slots]
    out = []
    for text in data.get("requests", [])[:count]:
        text = str(text)
        if "{slot}" not in text:
            text = text + " ({slot})" if values else text
        out.append(
            Request(rid(tool.name, kind, text), tool.name, text, values, kind, sibling.name if sibling else None)
        )
    return out


def load_real(path: str | Path, tools: list[Tool]) -> list[Request]:
    names = {t.name for t in tools}
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("tool") in names:
            out.append(Request(rid(row["tool"], "real", row["text"]), row["tool"], row["text"], [], "real"))
    return out
