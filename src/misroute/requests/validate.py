import asyncio
import hashlib
import random
import re
from misroute.requests import generate
from misroute.requests.generate import brief
from misroute.server.catalog import Tool
from misroute.trial import Request

PROMPT = """An AI agent has these tools:
{tools}

{domain}A user sends this as the first message of a conversation.
Request: {request}

Which single tool is the right one? Reply with the tool name only.
Reply none, a colon and a short reason instead when the request cannot be tested fairly:
- no tool here can do what it asks
- it asks to filter, narrow or return something the right tool's parameters and returned fields do not support
- it depends on something it does not contain, such as an earlier reply, a cursor or a next page"""


def recipe(domain: str = "", slots: int = 0) -> str:
    """A short hash of everything that shapes a written request besides the tool: the generator and validator
    instructions, the domain and the number of slot values. Stored requests are reused only under the recipe
    that wrote them, so a change to any of these writes them again."""
    parts = [generate.SLOT, generate.GOAL, generate.BORROWED, generate.SPEC, generate.SPEC_BORROWED, PROMPT]
    return hashlib.sha256("".join([*parts, domain.strip(), str(slots)]).encode()).hexdigest()[:8]


def read(reply: str, names: set[str]) -> tuple[str | None, str]:
    """The tool a reply names, or None with the reason given after none."""
    text = reply.strip()
    if re.match(r"\W*none\b", text, re.I):
        return None, text.split(":", 1)[1].strip()[:200] if ":" in text else ""
    found = [w for w in re.findall(r"[\w.\-]+", text) if w in names]
    return (found[0], "") if found else (None, "")


async def judge(
    adapter, req: Request, tools: list[Tool], seed: int = 0, source: str = "description", domain: str = ""
) -> tuple[str | None, str]:
    """The tool the validator picks for the request and, when it picks none, why. With source spec it sees each
    tool's interface instead of its description, and in both it sees the parameters, so it can tell a request the
    tool cannot express from one it can."""
    shuffled = tools[:]
    random.Random(f"{req.id}{seed}").shuffle(shuffled)
    listing = "\n".join(f"- {t.name}: {shown(t, source)}" for t in shuffled)
    text = req.texts()[0][1]
    context = f"Domain: {domain.strip()}\n\n" if domain.strip() else ""
    reply = await adapter.complete(
        PROMPT.format(tools=listing, request=text, domain=context), kind="validate", rep=seed
    )
    return read(reply, {t.name for t in tools})


async def pick(
    adapter, req: Request, tools: list[Tool], seed: int = 0, source: str = "description", domain: str = ""
) -> str | None:
    """The tool the validator picks for the request, or None when it names none."""
    return (await judge(adapter, req, tools, seed, source, domain))[0]


def shown(tool: Tool, source: str) -> str:
    if source == "spec":
        return brief(tool)
    return (tool.description or "no description") + (f" Parameters: {', '.join(tool.params)}" if tool.params else "")


async def keep_valid(
    adapter, reqs: list[Request], tools: list[Tool], source: str = "description", domain: str = ""
) -> tuple[list[Request], list[dict]]:
    """Requests the validator routes to their own tool, and for each rejected one its text, the tool the
    validator picked instead and the reason it gave when it picked none."""
    found = await asyncio.gather(*(judge(adapter, r, tools, source=source, domain=domain) for r in reqs))
    ok, rejected = [], []
    for r, (chosen, reason) in zip(reqs, found, strict=True):
        if chosen == r.tool:
            ok.append(r)
        else:
            rejected.append({"request": r.texts()[0][1], "picked": chosen, "reason": reason})
    return ok, rejected
