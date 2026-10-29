from misroute.models.base import parse_json
from misroute.server.catalog import Tool

CLAUSE = """Two tools of an AI agent are easy to confuse. For each tool write one sentence that states the boundary:
what it covers and which kind of request belongs to the other tool instead. Do not name the other tool.

A: {a_name}: {a}
B: {b_name}: {b}

Reply with JSON only: {{"a": "sentence for A", "b": "sentence for B"}}"""

SHAM = """Rewrite each tool description below so it reads more clearly, with the same meaning.
If a description is empty or only a path, write a plain description from the tool name and parameters instead.
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


async def write(writer, a: Tool, b: Tool, attempts: int = 3) -> dict[str, dict[str, str]]:
    """Returns {"clause": {name: desc}, "sham": {name: desc}} with the sham length matched to the clause edit."""
    data = parse_json(
        await writer.complete(CLAUSE.format(a_name=a.name, a=a.description, b_name=b.name, b=b.description), "clause")
    )
    clause = {a.name: joined(a.description, data["a"]), b.name: joined(b.description, data["b"])}
    sham = {}
    for i in range(attempts):
        prompt = SHAM.format(
            a_name=a.name,
            a_params=", ".join(a.params),
            a=a.description or "none",
            a_len=len(clause[a.name]),
            b_name=b.name,
            b_params=", ".join(b.params),
            b=b.description or "none",
            b_len=len(clause[b.name]),
        )
        s = parse_json(await writer.complete(prompt, "sham", rep=i))
        sham = {a.name: str(s["a"]).strip(), b.name: str(s["b"]).strip()}
        if all(matched(sham[t], clause[t]) for t in sham):
            break
    if not all(sham.values()):
        raise ValueError("writer returned an empty sham")
    return {"clause": clause, "sham": {k: pad(v, len(clause[k])) for k, v in sham.items()}}


def matched(x: str, y: str, tol: float = 0.1) -> bool:
    return abs(len(x) - len(y)) <= tol * max(len(y), 1)


def pad(text: str, target: int) -> str:
    if len(text) > target * 1.1:
        cut = text[: int(target * 1.1)]
        return cut[: cut.rfind(" ")].rstrip(",;") + "." if " " in cut else cut
    return text
