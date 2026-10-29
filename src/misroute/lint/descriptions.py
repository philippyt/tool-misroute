import re
from dataclasses import dataclass
from misroute.server.catalog import Tool, words

PATH = re.compile(r"^\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)?\s*/[\w{}\-./:]*\s*$", re.I)


@dataclass
class Finding:
    tool: str
    issue: str
    write: bool


def classify(tool: Tool) -> str | None:
    text = tool.description.strip()
    if not text:
        return "missing"
    if PATH.match(text):
        return "path only"
    desc = set(words(text))
    if desc and desc <= set(words(tool.name)) | {"a", "an", "the", "of", "for", "by", "to", "get", "list"}:
        return "name only"
    if len(text.split()) < 4:
        return "too short"
    return None


def lint(tools: list[Tool]) -> list[Finding]:
    out = [Finding(t.name, issue, t.is_write) for t in tools if (issue := classify(t))]
    return sorted(out, key=lambda f: (not f.write, f.issue, f.tool))


DRAFT_PROMPT = """Write a one sentence description for an API operation that an AI agent will read when choosing tools.
Say what it returns or changes, in plain words, without repeating the path.

Operation name: {name}
Current description: {description}
Parameters: {params}
Other operations on the same server: {others}

Reply with the sentence only."""


async def draft(adapter, tool: Tool, tools: list[Tool]) -> str:
    others = ", ".join(t.name for t in tools if t.name != tool.name)[:2000]
    prompt = DRAFT_PROMPT.format(
        name=tool.name, description=tool.description or "none", params=list(tool.params), others=others
    )
    return (await adapter.complete(prompt, kind="describe")).strip()
