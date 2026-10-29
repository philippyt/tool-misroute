from collections import Counter
from misroute.trial import Trial


def co_calls(trials: list[Trial]) -> Counter:
    """Unordered tool pairs called together in one turn or program."""
    out: Counter = Counter()
    for t in trials:
        names = sorted(set(t.calls))
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                out[(a, b)] += 1
    return out


def hedge_rate(trials: list[Trial], tools: set[str] | None = None) -> float:
    ts = [t for t in trials if tools is None or t.target in tools]
    return sum(t.hedge for t in ts) / len(ts) if ts else 0.0
