import asyncio
import hashlib
import random
import re
from typing import Any
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


async def panel_keep_valid(
    panel: list[tuple[Any, str, str]],
    reqs: list[Request],
    tools: list[Tool],
    source: str = "description",
    domain: str = "",
    lookups: frozenset[str] = frozenset(),
) -> tuple[list[Request], list[dict], list[dict]]:
    """Validate requests across a multi-family validator panel.

    panel holds (adapter, model_name, family_name) tuples. For each request, validators from a different family
    than the request's generator_family vote; if none differ, all panel members vote. Two validators must agree
    unanimously, three or more require a strict majority. Returns (kept, rejected, vote_records).
    """
    ok: list[Request] = []
    rejected: list[dict] = []
    records: list[dict] = []

    async def eval_one(r: Request) -> tuple[Request, list[tuple[str, list[str], str]]]:
        chosen = [p for p in panel if not r.generator_family or p[2] != r.generator_family] or panel
        res = await asyncio.gather(*(judge(ad, r, tools, source=source, domain=domain) for ad, _, _ in chosen))
        return r, [(name, needed, reason) for (_, name, _), (needed, reason) in zip(chosen, res, strict=True)]

    evaluated = await asyncio.gather(*(eval_one(r) for r in reqs))
    for r, votes in evaluated:
        accepts = 0
        vote_bools: dict[str, bool] = {}
        first_other = None
        first_reason = ""
        for vname, needed, reason in votes:
            counted = [n for n in needed if n not in lookups or n == r.tool]
            accepted = counted == [r.tool]
            vote_bools[vname] = accepted
            if accepted:
                accepts += 1
            else:
                if len(counted) > 1 and not reason:
                    reason = f"asks for more than one thing, needs {' and '.join(counted)}"
                if first_other is None:
                    first_other = next((n for n in counted if n != r.tool), None)
                if not first_reason and reason:
                    first_reason = reason
        k = len(votes)
        passed = accepts == k if k <= 2 else accepts > k / 2
        records.append({"request_id": r.id, "tool": r.tool, "votes": vote_bools, "passed": passed})
        if passed:
            ok.append(r)
        else:
            rejected.append(
                {
                    "request": r.texts()[0][1],
                    "picked": first_other,
                    "reason": first_reason or f"panel consensus failed ({accepts}/{k} accepted)",
                }
            )
    return ok, rejected, records


def agreement_stats(records: list[dict]) -> dict[str, Any]:
    """Inter-validator agreement over panel vote records: raw unanimous agreement and Fleiss kappa."""
    multi = [list(r["votes"].values()) for r in records if len(r.get("votes") or {}) >= 2]
    if not multi:
        return {}
    unanimous = sum(1 for vs in multi if len(set(vs)) == 1)
    # Pairwise agreement averaged across requests (handles variable panel sizes cleanly)
    pair_agree = 0.0
    total_yes = 0
    total_votes = 0
    for vs in multi:
        k = len(vs)
        yes = sum(1 for v in vs if v)
        no = k - yes
        pair_agree += (yes * (yes - 1) + no * (no - 1)) / (k * (k - 1))
        total_yes += yes
        total_votes += k
    p_bar = pair_agree / len(multi)
    p_yes = total_yes / total_votes if total_votes else 0.0
    p_e = p_yes * p_yes + (1.0 - p_yes) * (1.0 - p_yes)
    kappa = (p_bar - p_e) / (1.0 - p_e) if (1.0 - p_e) > 1e-12 else None
    return {
        "requests_judged": len(multi),
        "unanimous_rate": unanimous / len(multi),
        "pairwise_agreement": p_bar,
        "fleiss_kappa": kappa,
    }
