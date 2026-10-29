from misroute.models.base import parse_json
from misroute.server.catalog import Tool

CLAUSE = """Two tools of an AI agent are easy to confuse. For each tool write one sentence that states the boundary:
what it covers and which kind of request belongs to the other tool instead. Do not name the other tool.

A: {a_name}: {a}
B: {b_name}: {b}

Reply with JSON only: {{"a": "sentence for A", "b": "sentence for B"}}"""

SHAM = """Rewrite each tool description below so it reads more clearly, with the same meaning, in about {n} more words.
Do not compare the two tools, do not mention what the tool is not for, and do not add new facts.

A: {a}
B: {b}

Reply with JSON only: {{"a": "full new description of A", "b": "full new description of B"}}"""


def joined(desc: str, clause: str) -> str:
    desc = desc.strip()
    if desc and desc[-1] not in ".!?":
        desc += "."
    return f"{desc} {clause.strip()}".strip()


async def write(writer, a: Tool, b: Tool, attempts: int = 3) -> dict[str, dict[str, str]]:
    """Returns {"clause": {name: desc}, "sham": {name: desc}} with the sham length matched to the clause edit."""
    data = parse_json(
        await writer.complete(CLAUSE.format(a_name=a.name, a=a.description, b_name=b.name, b=b.description), "clause")
    )
    clause = {a.name: joined(a.description, data["a"]), b.name: joined(b.description, data["b"])}
    extra = max(len(data["a"].split()), len(data["b"].split()))
    sham = {}
    for i in range(attempts):
        s = parse_json(await writer.complete(SHAM.format(a=a.description, b=b.description, n=extra), "sham", rep=i))
        sham = {a.name: str(s["a"]).strip(), b.name: str(s["b"]).strip()}
        if all(matched(sham[t], clause[t]) for t in sham):
            break
    return {"clause": clause, "sham": {k: pad(v, len(clause[k])) for k, v in sham.items()}}


def matched(x: str, y: str, tol: float = 0.1) -> bool:
    return abs(len(x) - len(y)) <= tol * max(len(y), 1)


def pad(text: str, target: int) -> str:
    if len(text) > target * 1.1:
        cut = text[: int(target * 1.1)]
        return cut[: cut.rfind(" ")].rstrip(",;") + "." if " " in cut else cut
    return text
