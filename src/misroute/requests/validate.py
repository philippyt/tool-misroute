import random
import re
from misroute.server.catalog import Tool
from misroute.trial import Request

PROMPT = """An AI agent has these tools:
{tools}

Which single tool is the right one for this request?
Request: {request}

Reply with the tool name only."""


async def check(adapter, req: Request, tools: list[Tool], seed: int = 0) -> bool:
    shuffled = tools[:]
    random.Random(f"{req.id}{seed}").shuffle(shuffled)
    listing = "\n".join(f"- {t.name}: {t.description or 'no description'}" for t in shuffled)
    text = req.texts()[0][1]
    reply = await adapter.complete(PROMPT.format(tools=listing, request=text), kind="validate", rep=seed)
    names = {t.name for t in tools}
    found = [w for w in re.findall(r"[\w.\-]+", reply) if w in names]
    return bool(found) and found[0] == req.tool


async def keep_valid(adapter, reqs: list[Request], tools: list[Tool]) -> tuple[list[Request], list[Request]]:
    ok, bad = [], []
    for r in reqs:
        (ok if await check(adapter, r, tools) else bad).append(r)
    return ok, bad
