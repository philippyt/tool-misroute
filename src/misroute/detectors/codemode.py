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
