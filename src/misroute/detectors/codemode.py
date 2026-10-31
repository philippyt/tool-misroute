import re
from collections import Counter
from misroute.sandbox.fake_tools import Record, cursor_param
from misroute.sandbox.monty_runner import RunResult
from misroute.server.catalog import Tool


def defined(code: str) -> set[str]:
    return set(re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", code, re.M))


def unused_pagination(log: list[Record], tools: dict[str, Tool]) -> bool:
    for i, rec in enumerate(log):
        if not rec.paged:
            continue
        cp = cursor_param(tools[rec.tool])
        if not any(r.tool == rec.tool and r.args.get(cp) for r in log[i + 1 :]):
            return True
    return False


def classify(code: str, res: RunResult, log: list[Record], tools: list[Tool]) -> str | None:
    by_name = {t.name: t for t in tools}
    if res.error_type == "SyntaxError":
        return "syntax_error"
    if defined(code) & set(by_name):
        return "faked"
    if res.error_type == "NameError":
        return "unknown_function"
    if res.error_type == "NotImplementedError":
        return "sandbox_limit"
    if res.error:
        return "runtime_error"
    if not log:
        return "no_call"
    for status in ("invented", "reused"):
        if any(r.cursor == status for r in log):
            return f"{status}_cursor"
    if unused_pagination(log, by_name):
        return "unused_pagination"
    return None


def rates(trials: list, mode: str = "code") -> dict[str, float]:
    ts = [t for t in trials if t.mode == mode]
    c = Counter(t.failure for t in ts if t.failure)
    return {k: v / len(ts) for k, v in sorted(c.items())} if ts else {}


def rerun_choice(trials: list) -> dict[str, float] | None:
    """Tool choice recovered by rerunning programs without pasted stubs. Reported apart from the real outcome."""
    ts = [t for t in trials if t.rerun is not None]
    if not ts:
        return None
    first = [t.rerun[0] if t.rerun else None for t in ts]
    return {
        "trials": len(ts),
        "right": sum(f == t.target for f, t in zip(first, ts, strict=True)) / len(ts),
        "wrong": sum(f not in (None, t.target) for f, t in zip(first, ts, strict=True)) / len(ts),
    }


def unanswered(trials: list) -> dict[str, float]:
    """Share of each tool's requests the model answered without calling any tool, for tools where it happened."""
    by: dict[str, list] = {}
    for t in trials:
        by.setdefault(t.target, []).append(t.failure in ("no_tool_call", "no_code"))
    rates = {k: sum(v) / len(v) for k, v in by.items() if v and any(v)}
    return dict(sorted(rates.items(), key=lambda kv: -kv[1]))


def unrouted(trials: list) -> list[dict]:
    """Per tool rows for requests that ended with no tool call at all. A tool whose requests the model answers
    from prose fails routing as surely as one whose requests land on a sibling, so these rank with the pair
    spots instead of trailing them as a footnote."""
    by: dict[str, list] = {}
    for t in trials:
        by.setdefault(t.target, []).append(t)
    out = []
    for tool, ts in by.items():
        hits = [t for t in ts if t.failure in ("no_tool_call", "no_code")]
        if not hits:
            continue
        out.append(
            {
                "tool": tool,
                "rate": len(hits) / len(ts),
                "count": len(hits),
                "trials": len(ts),
                "where": sorted({f"{t.model} {t.mode}" for t in hits}),
                "example": min((t.text for t in hits), key=len),
            }
        )
    return sorted(out, key=lambda r: -r["rate"])
