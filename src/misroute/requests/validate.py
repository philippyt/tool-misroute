import random
import re
from misroute.requests.generate import brief
from misroute.server.catalog import Tool
from misroute.trial import Request

PROMPT = """An AI agent has these tools:
{tools}

Which single tool is the right one for this request?
Request: {request}

Reply with the tool name only."""


async def pick(adapter, req: Request, tools: list[Tool], seed: int = 0, source: str = "description") -> str | None:
    """The tool the validator picks for the request, or None when its reply names no tool. With source spec it sees
    each tool's interface instead of its description."""
    shuffled = tools[:]
    random.Random(f"{req.id}{seed}").shuffle(shuffled)
    listing = "\n".join(f"- {t.name}: {shown(t, source)}" for t in shuffled)
    text = req.texts()[0][1]
    reply = await adapter.complete(PROMPT.format(tools=listing, request=text), kind="validate", rep=seed)
    names = {t.name for t in tools}
    found = [w for w in re.findall(r"[\w.\-]+", reply) if w in names]
    return found[0] if found else None


def shown(tool: Tool, source: str) -> str:
    return brief(tool) if source == "spec" else tool.description or "no description"


async def keep_valid(
    adapter, reqs: list[Request], tools: list[Tool], source: str = "description"
) -> tuple[list[Request], list[str | None]]:
    """Requests the validator routes to their own tool, and the validator's pick for each rejected one."""
    ok, picks = [], []
    for r in reqs:
        chosen = await pick(adapter, r, tools, source=source)
        if chosen == r.tool:
            ok.append(r)
        else:
            picks.append(chosen)
    return ok, picks
