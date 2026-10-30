from misroute.models.base import parse_json
from misroute.server.catalog import Tool

CLAUSE = """Two tools of an AI agent are easy to confuse. For each tool write one sentence that states the boundary:
what it covers and which kind of request belongs to the other tool instead. Do not name the other tool.

A: {a_name}: {a}
B: {b_name}: {b}

Reply with JSON only: {{"a": "sentence for A", "b": "sentence for B"}}"""

NEUTRAL = """Rewrite each tool description below so it reads more clearly, with the same meaning.
Make A about {a_len} characters and B about {b_len} characters long.
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


async def write(writer, a: Tool, b: Tool, drafts: dict[str, str] | None = None, attempts: int = 3) -> dict:
    """Edits for one pair. With drafts for undescribed tools the baseline is the drafted description, since a
    neutral rewrite of a missing description would add information. Otherwise the baseline is a neutral rewrite
    of the same length as the clause edit."""
    base = {t.name: (drafts or {}).get(t.name, t.description) for t in (a, b)}
    data = parse_json(
        await writer.complete(CLAUSE.format(a_name=a.name, a=base[a.name], b_name=b.name, b=base[b.name]), "clause")
    )
    clause = {a.name: joined(base[a.name], data["a"]), b.name: joined(base[b.name], data["b"])}
    if drafts:
        return {"clause": clause, "baseline": base, "baseline_kind": "description"}
    neutral = {}
    for i in range(attempts):
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
        s = parse_json(await writer.complete(prompt, "neutral", rep=i))
        neutral = {a.name: str(s["a"]).strip(), b.name: str(s["b"]).strip()}
        if all(matched(neutral[t], clause[t]) for t in neutral):
            break
    if not all(neutral.values()):
        raise ValueError("writer returned an empty neutral edit")
    baseline = {k: pad(v, len(clause[k])) for k, v in neutral.items()}
    return {"clause": clause, "baseline": baseline, "baseline_kind": "neutral"}


NEUTRAL_ONE = """Rewrite this tool description so it reads more clearly, with the same meaning, in about {n} characters.
Do not mention other tools, do not say what the tool is not for, and do not add facts beyond the name,
parameters and description.

A: {name}({params}): {description}

Reply with JSON only: {{"a": "full new description"}}"""


async def neutral_one(writer, tool: Tool, length: int, attempts: int = 3) -> str:
    text = ""
    for i in range(attempts):
        prompt = NEUTRAL_ONE.format(
            n=length, name=tool.name, params=", ".join(tool.params), description=tool.description
        )
        try:
            found = str(parse_json(await writer.complete(prompt, "neutral", rep=i))["a"]).strip()
        except (ValueError, KeyError, TypeError):
            continue
        text = found or text
        if matched(text, " " * length):
            break
    if not text:
        raise ValueError("writer returned no usable neutral edit")
    return pad(text, length)


def matched(x: str, y: str, tol: float = 0.1) -> bool:
    return abs(len(x) - len(y)) <= tol * max(len(y), 1)


def pad(text: str, target: int) -> str:
    if len(text) > target * 1.1:
        cut = text[: int(target * 1.1)]
        return cut[: cut.rfind(" ")].rstrip(",;") + "." if " " in cut else cut
    return text
