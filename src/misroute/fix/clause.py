from misroute.models.base import parse_json
from misroute.server.catalog import Tool

CLAUSE = """Two tools of an AI agent are easy to confuse. For each tool write one sentence that states the boundary:
what it covers and which kind of request belongs to the other tool instead. Do not name the other tool.

A: {a_name}: {a}
B: {b_name}: {b}

Reply with JSON only: {{"a": "sentence for A", "b": "sentence for B"}}"""

NEUTRAL = """Rewrite each tool description below so it reads more clearly, with the same meaning.
Make A at least {a_len} characters and B at least {b_len} characters long.
Do not compare the two tools, do not mention what a tool is not for, and do not add facts beyond the name,
parameters and description.

A: {a_name}({a_params}): {a}
B: {b_name}({b_params}): {b}

Reply with JSON only: {{"a": "full new description of A", "b": "full new description of B"}}"""


def joined(desc: str, clause: str) -> str:
    desc = desc.strip()
    if desc and desc[-1] not in ".!?":
        desc += "."
    return f"{desc} {clause.strip()}".strip()


async def write(writer, a: Tool, b: Tool, drafts: dict[str, str] | None = None) -> dict:
    """Edits for one pair. With drafts for undescribed tools the baseline is the drafted description, since a
    neutral rewrite of a missing description would add information. Otherwise the baseline is a neutral rewrite
    cut to the length of the clause edit."""
    base = {t.name: (drafts or {}).get(t.name, t.description) for t in (a, b)}
    data = parse_json(
        await writer.complete(CLAUSE.format(a_name=a.name, a=base[a.name], b_name=b.name, b=base[b.name]), "clause")
    )
    clause = {a.name: joined(base[a.name], data["a"]), b.name: joined(base[b.name], data["b"])}
    if drafts:
        return {"clause": clause, "baseline": base, "baseline_kind": "description"}
    prompt = NEUTRAL.format(
        a_name=a.name,
        a_params=", ".join(a.params),
        a=a.description,
        a_len=len(clause[a.name]),
        b_name=b.name,
        b_params=", ".join(b.params),
        b=b.description,
        b_len=len(clause[b.name]),
    )
    best = {a.name: "", b.name: ""}
    rep = 0
    while any(len(best[k]) < len(clause[k]) for k in best):
        try:
            s = parse_json(await writer.complete(prompt, "neutral", rep=rep))
            found = {a.name: str(s["a"]).strip(), b.name: str(s["b"]).strip()}
        except (ValueError, KeyError, TypeError):
            break
        rep += 1
        if all(len(found[k]) <= len(best[k]) for k in best):
            break
        best = {k: found[k] if len(found[k]) > len(best[k]) else best[k] for k in best}
    if not all(best.values()):
        raise ValueError("writer returned no usable neutral edit")
    baseline = {k: fit(v, len(clause[k])) for k, v in best.items()}
    return {"clause": clause, "baseline": baseline, "baseline_kind": "neutral"}


NEUTRAL_ONE = """Rewrite this tool description so it reads more clearly, with the same meaning, in at least {n}
characters. Do not mention other tools, do not say what the tool is not for, and do not add facts beyond
the name, parameters and description.

A: {name}({params}): {description}

Reply with JSON only: {{"a": "full new description"}}"""


async def neutral_one(writer, tool: Tool, length: int) -> str:
    """A neutral rewrite cut to length characters. Retries while the rewrite is too short to cut and still
    getting longer."""
    prompt = NEUTRAL_ONE.format(n=length, name=tool.name, params=", ".join(tool.params), description=tool.description)
    best, rep = "", 0
    while len(best) < length:
        try:
            found = str(parse_json(await writer.complete(prompt, "neutral", rep=rep))["a"]).strip()
        except (ValueError, KeyError, TypeError):
            found = ""
        rep += 1
        if len(found) <= len(best):
            break
        best = found
    if not best:
        raise ValueError("writer returned no usable neutral edit")
    return fit(best, length)


def fit(text: str, target: int) -> str:
    """Cut text to at most target characters at a word boundary, ending with a full stop."""
    text = " ".join(text.split())
    if len(text) <= target:
        return text
    cut = text[: max(target - 1, 1)]
    if text[len(cut)] != " " and " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(",;:. ") + "."
