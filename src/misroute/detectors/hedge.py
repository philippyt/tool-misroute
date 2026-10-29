from collections import Counter
from misroute.trial import Trial


def co_calls(trials: list[Trial]) -> Counter:
    """Pairs of the target and another tool called with it in one turn or program."""
    out: Counter = Counter()
    for t in trials:
        if t.hedge:
            for other in set(t.calls) - {t.target}:
                out[(min(t.target, other), max(t.target, other))] += 1
    return out


def hedge_rate(trials: list[Trial], tools: set[str] | None = None) -> float:
    ts = [t for t in trials if tools is None or t.target in tools]
    return sum(t.hedge for t in ts) / len(ts) if ts else 0.0
