import asyncio
from misroute.models.base import parse_json
from misroute.requests.generate import brief, view
from misroute.server.catalog import Tool

STYLES = {
    "boundary": "what it covers, then one clause on which requests belong to a neighbouring operation instead",
    "returns": "lead with what it returns, naming the main fields",
    "example": "one sentence on what it does, then one short example request it answers",
    "minimal": "one short plain sentence",
}

WRITE = """Write alternative descriptions for one API operation that an AI agent reads when choosing which tool to call.
Write one description per style below, in English, each at most {max_chars} characters.
Say what it returns or changes in plain words. Do not invent fields, filters or behaviour the interface does not
show. Do not name other operations.
{styles}
{domain}
Operation: {name}
{view}
Current description: {description}
Operations it is confused with:
{siblings}

Reply with JSON only: {{"candidates": [{{"style": "...", "text": "..."}}]}}"""

FAITHFUL = """Does this description of an API operation claim anything its interface does not support, such as fields,
filters, actions or data that are not there? Plain summaries and reasonable paraphrase are fine.

Operation: {name}
{view}
Description: {text}

Reply with JSON only: {{"faithful": true, "reason": "..."}} or {{"faithful": false, "reason": "..."}}"""


def cap(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if end > limit // 2:
        return cut[: end + 1]
    return cut[: cut.rfind(" ")].rstrip(",;:") + "." if " " in cut else cut


async def write(
    writer, tool: Tool, siblings: list[Tool], styles: list[str], max_chars: int, domain: str = ""
) -> list[dict[str, str]]:
    """One candidate per style, capped at max_chars, deduplicated."""
    prompt = WRITE.format(
        max_chars=max_chars,
        styles="\n".join(f"- {s}: {STYLES[s]}" for s in styles),
        domain=f"Domain: {domain.strip()}\n" if domain.strip() else "",
        name=tool.name,
        view=view(tool),
        description=tool.description or "none",
        siblings="\n".join(f"- {s.name}: {brief(s)}. Current: {s.description or 'none'}" for s in siblings) or "none",
    )
    try:
        data = parse_json(await writer.complete(prompt, kind="candidates"))
    except (ValueError, KeyError, TypeError):
        return []
    out, seen = [], set()
    for c in data.get("candidates", []) if isinstance(data, dict) else []:
        text = cap(str(c.get("text", "")), max_chars) if isinstance(c, dict) else ""
        style = str(c.get("style", "")) if isinstance(c, dict) else ""
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append({"style": style if style in STYLES else "other", "text": text})
    return out[: len(styles)]


async def faithful(validator, tool: Tool, text: str) -> tuple[bool, str]:
    try:
        data = parse_json(
            await validator.complete(FAITHFUL.format(name=tool.name, view=view(tool), text=text), kind="faithful")
        )
    except (ValueError, KeyError, TypeError):
        return True, "unchecked, the validator reply did not parse"
    ok = data.get("faithful") if isinstance(data, dict) else True
    ok = ok if isinstance(ok, bool) else str(ok).lower() not in ("false", "no", "0")
    return ok, str(data.get("reason", "")) if isinstance(data, dict) else ""


async def screen(validator, tool: Tool, found: list[dict[str, str]]) -> tuple[list[dict], list[dict]]:
    """Candidates the validator finds faithful to the interface, and the dropped ones with the reason."""
    checks = await asyncio.gather(*(faithful(validator, tool, c["text"]) for c in found))
    kept = [c for c, (ok, _) in zip(found, checks, strict=True) if ok]
    dropped = [{**c, "reason": why} for c, (ok, why) in zip(found, checks, strict=True) if not ok]
    return kept, dropped
