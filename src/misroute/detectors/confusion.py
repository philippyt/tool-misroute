from collections import Counter, defaultdict
from misroute.trial import Trial

NONE = "(none)"
READ_FIRST = "(read first)"
LOOKUP_ONLY = "(lookup only)"
ENDED = {"read_first": READ_FIRST, "lookup_only": LOOKUP_ONLY}


def matrix(trials: list[Trial]) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for t in trials:
        out[t.target][ENDED.get(t.failure or "") or t.chosen or NONE] += 1
    return dict(out)


def directions(trials: list[Trial], a: str, b: str) -> dict[str, dict[str, float]]:
    out = {}
    for src, dst in ((a, b), (b, a)):
        ts = [t for t in trials if t.target == src]
        n = len(ts) or 1
        out[f"{src}>{dst}"] = {
            "n": len(ts),
            "wrong": sum(t.chosen == dst for t in ts) / n,
            "hedge": sum(t.hedge for t in ts) / n,
            "leak": sum(t.chosen not in (src, dst, None) for t in ts) / n,
            "none": sum(t.chosen is None for t in ts) / n,
        }
    return out


def leakage(trials: list[Trial], a: str, b: str) -> Counter:
    return Counter(t.chosen for t in trials if t.target in (a, b) and t.chosen not in (a, b, None))


def operations(trials: list[Trial]) -> dict[str, dict[str, dict]]:
    """For merged tools, per tool and wanted selector value: trials, how many reached the tool, how many of
    those picked the wrong value, and which values were picked instead."""
    out: dict[str, dict[str, dict]] = {}
    for t in trials:
        if not t.operation:
            continue
        row = out.setdefault(t.target, {}).setdefault(
            t.operation, {"trials": 0, "reached_tool": 0, "wrong_operation": 0, "picked": Counter()}
        )
        row["trials"] += 1
        row["reached_tool"] += t.chosen == t.target
        if t.wrong_operation:
            row["wrong_operation"] += 1
            row["picked"][t.operation_picked or NONE] += 1
    return {k: {v: {**r, "picked": dict(r["picked"])} for v, r in rows.items()} for k, rows in out.items()}


def wrong_rate(trials: list[Trial]) -> float:
    return sum(t.wrong for t in trials) / len(trials) if trials else 0.0
