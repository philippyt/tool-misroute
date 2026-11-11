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

Which of these tools does it take to give the user everything the request asks for? Reply with the tool names
only, separated by commas, the most important first. Name a tool only when the request asks for that tool's own
result or effect, not when it would merely be used to look something up on the way.
Reply none, a colon and a short reason instead when the request cannot be tested fairly:
- no tool here can do what it asks
- it asks to filter, narrow or return something the tool's parameters and returned fields do not support
- it names a value that a parameter does not allow, such as one outside its listed values or range
- it depends on something it does not contain, such as an earlier reply, a cursor or a next page"""


def recipe(domain: str = "", slots: int = 0) -> str:
    """A short hash of everything that shapes a written request besides the tool: the generator and validator
    instructions, the domain and the number of slot values. Stored requests are reused only under the recipe
    that wrote them, so a change to any of these writes them again."""
    parts = [generate.SLOT, generate.GOAL, generate.BORROWED, generate.SPEC, generate.SPEC_BORROWED, PROMPT]
    parts += [generate.FEEDBACK, str(generate.ROUNDS), str(generate.RENDER)]
    return hashlib.sha256("".join([*parts, domain.strip(), str(slots)]).encode()).hexdigest()[:8]


def read(reply: str, names: set[str]) -> tuple[list[str], str]:
    """The tools a reply names, in its order and once each, or none with the reason given after none."""
    text = reply.strip()
    if re.match(r"\W*none\b", text, re.I):
        return [], text.split(":", 1)[1].strip()[:200] if ":" in text else ""
    return list(dict.fromkeys(w for w in re.findall(r"[\w.\-]+", text) if w in names)), ""


async def judge(
    adapter, req: Request, tools: list[Tool], seed: int = 0, source: str = "description", domain: str = ""
) -> tuple[list[str], str]:
    """Every tool the validator says the request needs, most important first, and when it names none, why. With
    source spec it sees each tool's interface instead of its description, and in both it sees the parameters, so
    it can tell a request the tool cannot express from one it can. Asking for all the tools, not for one, is what
    exposes a request that wants two things: a judge told to pick one tool picks one and says nothing."""
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
    """The tool the validator puts first for the request, or None when it names none."""
    needed, _ = await judge(adapter, req, tools, seed, source, domain)
    return needed[0] if needed else None


def shown(tool: Tool, source: str) -> str:
    if source == "spec":
        return brief(tool)
    listed = ", ".join(generate.param(k, v) for k, v in tool.params.items())
    return (tool.description or "no description") + (f" Parameters: {listed}" if listed else "")


async def keep_valid(
    adapter,
    reqs: list[Request],
    tools: list[Tool],
    source: str = "description",
    domain: str = "",
    lookups: frozenset[str] = frozenset(),
) -> tuple[list[Request], list[dict]]:
    """Requests the validator says need their own tool and no other, and for each rejected one its text, the
    tool the validator put first and why: the reason it gave for none, or the tools it said the request needs.
    Lookup tools do not count as a second tool, since an agent is meant to call them on the way."""
    found = await asyncio.gather(*(judge(adapter, r, tools, source=source, domain=domain) for r in reqs))
    ok, rejected = [], []
    for r, (needed, reason) in zip(reqs, found, strict=True):
        counted = [n for n in needed if n not in lookups or n == r.tool]
        if counted == [r.tool]:
            ok.append(r)
            continue
        if len(counted) > 1:
            reason = f"asks for more than one thing, needs {' and '.join(counted)}"
        other = next((n for n in counted if n != r.tool), None)
        rejected.append({"request": r.texts()[0][1], "picked": other, "reason": reason})
    return ok, rejected
